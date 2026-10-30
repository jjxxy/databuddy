"""Owner-configured model connection. Credentials are never returned or put in Git."""
from .workspace_lifecycle import assert_turn_live

import json,os,time,re
from contextvars import ContextVar
from contextlib import contextmanager
from urllib.parse import urlsplit
import httpx
from typing import Literal
from pydantic import Field,SecretStr
from .models import StrictModel
from .config import LLM_BASE,LLM_KEY,LLM_MODEL
from .provider_compatibility import bounded_request_options,response_token_allowance,configured_input_character_limit

class ProviderInput(StrictModel):
    engine:Literal['atlas','opencode']='atlas'
    base_url:str=Field(min_length=8,max_length=500)
    model:str=Field(min_length=1,max_length=120)
    api_key:SecretStr=Field(default=SecretStr(''))
    share_data:bool=False
    tool_protocol:Literal['json','native']='json'
    upstream_base_url:str=Field(default='',max_length=500)

def legacy_settings():
    from .store import get,unseal
    try:return unseal(get('system','model-provider')['secret_config'])
    except KeyError:return {'base_url':LLM_BASE,'model':LLM_MODEL,'api_key':LLM_KEY,'share_data':False}

def settings():
    tid=_scope.get()
    if tid:
        from .store import get
        from .model_catalog import settings_for_turn
        pinned=settings_for_turn(get('turn',tid))
        if pinned is not None:return pinned
    from .model_catalog import default_settings
    configured=default_settings()
    if configured is not None:return configured
    return legacy_settings()

def status():
    if os.getenv('ATLAS_DESKTOP_MODEL_DISABLED')=='1':
        return {'configured':False,'base_url':'','model':'','share_data':False,'engine':'atlas','tool_protocol':'native','upstream_base_url':'','opencode_version':None}
    value=settings()
    return {'base_url':value.get('base_url',''),'configured':bool(value.get('base_url') and value.get('model')),'model':value.get('model',''),'share_data':bool(value.get('share_data')),'tool_protocol':value.get('tool_protocol','json'),'engine':value.get('engine','atlas'),'upstream_base_url':value.get('upstream_base_url',''),'opencode_version':value.get('opencode_version')}

_scope=ContextVar('atlas_model_scope',default=None)
_project_scope=ContextVar('atlas_project_scope',default=None)

def current_turn_id():return _scope.get()

@contextmanager
def project_scope(project_id):
    token=_project_scope.set(project_id)
    try:yield
    finally:_project_scope.reset(token)

@contextmanager
def task_scope(turn_id):
    from .actor_context import actor_scope
    from .store import get
    token=_scope.set(turn_id)
    try:
        with actor_scope(get('turn',turn_id).get('actor_id','owner')):yield
    finally:_scope.reset(token)

class BudgetExceeded(ValueError):pass
class ModelOutputError(ValueError):
    def __init__(self,message,*,code='invalid_model_output',candidate=None):
        super().__init__(message)
        # Candidate is in-memory only: never put model responses into telemetry
        # or expose them in diagnostics. It is used only for safe chat replies.
        self.code=code;self.candidate=candidate

class ModelServiceError(ValueError):
    """A structured upstream error, whose body must not be exposed."""

class ContextWindowExceeded(BudgetExceeded):
    """A request window can be compressed; cumulative grants cannot."""
    def __init__(self,message='模型上下文较长，需要压缩后继续',*,limit=None,characters=None,source='provider'):
        super().__init__(message)
        self.limit=limit;self.characters=characters;self.source=source


def context_overflow_error(value):
    """Classify a provider error envelope, never arbitrary document/tool text."""
    if isinstance(value,httpx.HTTPStatusError):
        if value.response.status_code not in (400,413,422):return False
        try:value=value.response.json()
        except (ValueError,httpx.ResponseNotRead):return False
    if isinstance(value,dict):
        if 'error' in value:return context_overflow_error(value['error'])
        code=str(value.get('code') or value.get('type') or value.get('name') or '').lower()
        if code in ('context_length_exceeded','context_window_exceeded','context_overflow','contextoverflowerror','contextlengthexceeded'):return True
        return context_overflow_error(value.get('message','')) or (isinstance(value.get('data'),dict) and context_overflow_error(value['data']))
    if not isinstance(value,str):return False
    return bool(re.search(r'maximum context (?:length|window)|context (?:length|window).{0,60}(?:exceed|limit)|(?:prompt|input).{0,35}(?:too long|too large|exceeds.{0,25}token)|too many (?:input )?tokens|单次模型上下文超过限制|上下文(?:长度)?超过(?:模型)?(?:限制|上限)',value[:4000],re.I))


def _response_text(content):
    """Accept compatible text parts without accepting arbitrary binary parts."""
    if isinstance(content,str):return content if len(content)<=100000 else None
    if not isinstance(content,list) or not 1<=len(content)<=64:return None
    if not all(isinstance(part,dict) and part.get('type') in ('text','output_text')
               and isinstance(part.get('text'),str) for part in content):return None
    text=''.join(part['text'] for part in content)
    return text if len(text)<=100000 else None


def contains_reasoning_marker(text):
    """Never expose raw model reasoning tags, including incomplete tags."""
    return isinstance(text,str) and bool(re.search(r'<\s*/?\s*(?:think|analysis|reasoning)\b',text,re.I))


def _native_tool_unsupported(error):
    """Only a clear upstream unsupported-tools response can change protocol."""
    if not isinstance(error,httpx.HTTPStatusError) or error.response.status_code not in (400,422):return False
    try:payload=error.response.json()
    except (ValueError,UnicodeError):return False
    if not isinstance(payload,dict):return False
    detail=payload.get('error',payload)
    if not isinstance(detail,dict):return False
    code=str(detail.get('code') or '').lower()
    param=str(detail.get('param') or '').lower()
    message=str(detail.get('message') or '')[:1000].lower()
    unsupported=r'unsupported|not supported|does not support|doesn.t support|unknown|unrecognized|不支持|未知参数'
    subject=r'tool_choice|tools?|function[ _-]?call(?:ing)?|工具调用'
    return bool(re.search('(?:'+subject+')',param) and re.search('(?:'+unsupported+')',code+' '+message)
                or re.search('(?:'+subject+').{0,80}(?:'+unsupported+')',message)
                or re.search('(?:'+unsupported+').{0,80}(?:'+subject+')',message))


def complete(messages,max_tokens=None,configuration=None,purpose='agent_planning',tools=None):
    if os.getenv('ATLAS_DESKTOP_MODEL_DISABLED')=='1':raise ValueError(os.getenv('ATLAS_DESKTOP_MODEL_NOTICE','云端模型不可用，本地能力可用。'))
    from .store import get,patch,put,now,uid
    value=configuration or settings()
    from .network_policy import check_url
    if value.get('base_url'):check_url(value['base_url'])
    if value.get('engine')=='opencode' and value.get('upstream_base_url'):check_url(value['upstream_base_url'])
    if not value.get('base_url') or not value.get('model'):raise ValueError('请先连接一个语言模型')
    size=len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False));tid=_scope.get();usage={};started=time.monotonic()
    pid=_project_scope.get()
    if tid:pid=get('turn',tid)['project_id']
    if not pid and os.getenv('ATLAS_MODEL_SOURCE_ID'):
        try:pid=get('source',os.environ['ATLAS_MODEL_SOURCE_ID']).get('project_id')
        except KeyError:pass
    if pid:
        from .company_search import assert_model_allowed
        assert_model_allowed(pid)
        from .agent_mesh import assert_project_model_allowed
        assert_project_model_allowed(pid)
    if max_tokens is None:max_tokens=response_token_allowance(value)
    character_cap=configured_input_character_limit(value.get('model'))
    if character_cap is not None and size>character_cap:
        raise ContextWindowExceeded('此请求超过管理员配置的上下文字符上限，正在压缩后继续',limit=character_cap,characters=size,source='operator')
    if tid:
        from .task_budget import effective_limits,exceeded_reason
        turn=get('turn',tid);budget=effective_limits(turn)
        usage=turn.get('model_usage',{})
        if budget['output_tokens'] is not None:
            available=budget['output_tokens']-usage.get('output_tokens_budgeted',0)
            if available<128:raise BudgetExceeded('此任务已达到配置的模型输出上限，已有成果已保存')
            max_tokens=min(max_tokens,available)
        reason=exceeded_reason(budget,usage,input_characters=size,output_tokens=max_tokens)
        if reason:
            label={'model_calls':'调用次数','input_characters':'累计输入','output_tokens':'累计输出','duration_ms':'处理时长'}[reason]
            raise BudgetExceeded('此任务已达到配置的'+label+'上限，已有成果已保存')
        from . import agent_gateway,agent_team,collaboration_budget
        assert_turn_live(turn);agent_gateway.assert_turn_allowed(turn);agent_team.assert_turn_allowed(turn)
        agent_gateway.admit_model_request(turn,size,max_tokens)
        collaboration_budget.admit(turn,size,max_tokens)
        usage=dict(usage,calls=usage.get('calls',0)+1,input_characters=usage.get('input_characters',0)+size,output_tokens_budgeted=usage.get('output_tokens_budgeted',0)+max_tokens)
        patch('turn',tid,model_usage=usage)
    from .quotas import reserve
    reserve(max_tokens)
    event={'purpose':purpose,'model':value['model'],'engine':value.get('engine','atlas'),'protocol':'native' if tools else 'json','turn_id':tid,'source_id':os.getenv('ATLAS_MODEL_SOURCE_ID'),'started_at':now(),'input_characters':size,'max_output_tokens':max_tokens,'status':'failed'}
    try:
        if value.get('engine')=='opencode':
            from .opencode import complete as opencode_complete
            content,tokens,receipt=opencode_complete(messages,value,tools,max_tokens)
            event.update(status='succeeded',usage=tokens,usage_reported=bool(tokens),opencode=receipt)
            if tid:
                current=get('turn',tid).get('model_usage',{})
                for k,v in tokens.items():current[k]=current.get(k,0)+v
                # OpenCode may issue internal requests; reserve our full allowance.
                current.update(usage_incomplete=True,budget_scope='atlas_requests_not_upstream_billing')
                patch('turn',tid,model_usage=current)
            return content
        body={'model':value['model'],'messages':messages,'max_tokens':max_tokens,'temperature':0}
        body.update(bounded_request_options(value['base_url'],value['model']))
        if tools:body.update(tools=tools,tool_choice='required',parallel_tool_calls=False)
        with httpx.Client(timeout=50,trust_env=False) as client:
            r=client.post(value['base_url'].rstrip('/')+'/chat/completions',headers={'Authorization':'Bearer '+value.get('api_key','')},json=body)
            r.raise_for_status()
            try:payload=r.json()
            except (ValueError,UnicodeError):raise ModelOutputError('模型服务返回了无效响应',code='invalid_response_json') from None
            if not isinstance(payload,dict):raise ModelOutputError('模型服务返回的数据格式异常',code='invalid_response_json')
            if payload.get('error') and not payload.get('choices'):
                failure=payload['error'] if isinstance(payload['error'],dict) else {}
                code=str(failure.get('code') or failure.get('type') or '').lower()
                if code in ('insufficient_quota','billing_hard_limit_reached'):
                    message='模型服务额度不足，请检查模型账户额度或余额；任务和已有成果已保留。'
                elif code in ('invalid_api_key','authentication_error','unauthorized'):
                    message='模型服务认证失败，请检查模型连接或密钥；任务和已有成果已保留。'
                elif code in ('rate_limit_exceeded','rate_limit_error'):
                    message='模型服务请求过于频繁，请稍后继续；任务和已有成果已保留。'
                else:message='模型服务返回错误，请检查模型服务状态与配置；任务和已有成果已保留。'
                raise ModelServiceError(message)
            reported=payload.get('usage') if isinstance(payload.get('usage'),dict) else {}
            tokens={k:v for k,v in reported.items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0}
            event.update(usage=tokens,usage_reported=bool(tokens))
            if tid:
                usage=get('turn',tid).get('model_usage',{})
                for k,v in tokens.items():usage[k]=usage.get(k,0)+v
                if 'completion_tokens' in tokens:usage['output_tokens_budgeted']-=max_tokens-tokens['completion_tokens']
                usage['usage_incomplete']=usage.get('usage_incomplete',False) or not bool(tokens)
                patch('turn',tid,model_usage=usage)
            try:
                choice=payload['choices'][0];message=choice['message']
                if choice.get('finish_reason')=='length':raise ModelOutputError('模型输出未完整返回，正在尝试分步处理。',code='output_truncated')
                if choice.get('finish_reason')=='content_filter':raise ModelOutputError('模型服务未返回可用内容，已有任务和成果已保留。',code='content_filter')
                if tools:
                    calls=message.get('tool_calls') or []
                    if len(calls)!=1:
                        candidate=_response_text(message.get('content')) if not calls else None
                        if candidate:
                            # The upstream completed with usable text; record
                            # the incompatible tool protocol separately.
                            event.update(status='succeeded',protocol_warning='native_tool_call_missing')
                        raise ModelOutputError('模型没有返回单个工具调用，未执行任何操作',code='native_tool_call_missing' if not calls else 'native_multiple_tool_calls',candidate=candidate)
                    fn=calls[0]['function'];name=fn['name'];args=fn['arguments']
                    if name not in {x['function']['name'] for x in tools}:raise ModelOutputError('模型返回了未提供的工具',code='native_unknown_tool')
                    if isinstance(args,str):
                        if len(args)>100000:raise ModelOutputError('模型工具参数过长',code='native_invalid_arguments')
                        try:arguments=json.loads(args)
                        except json.JSONDecodeError:raise ModelOutputError('模型工具参数不是有效JSON',code='native_invalid_arguments') from None
                    elif isinstance(args,dict):
                        # Some OpenAI-compatible gateways decode arguments for
                        # us. This is equivalent data, not a new authorization.
                        if len(json.dumps(args,ensure_ascii=False))>100000:raise ModelOutputError('模型工具参数过长',code='native_invalid_arguments')
                        arguments=args
                    else:raise ModelOutputError('模型工具参数不是JSON对象',code='native_invalid_arguments')
                    if not isinstance(arguments,dict):raise ModelOutputError('工具参数必须为JSON对象',code='native_invalid_arguments')
                    content=json.dumps({**arguments,'kind':'final'} if name=='finish_task' else {'kind':'tool','tool':name,'arguments':arguments},ensure_ascii=False)
                else:content=_response_text(message['content'])
                if content is None:raise ModelOutputError('模型返回格式无效')
            except (KeyError,IndexError,TypeError,json.JSONDecodeError):raise ModelOutputError('模型没有返回完整的响应',code='native_invalid_response' if tools else 'invalid_model_response') from None
            event.update(status='succeeded')
            return content
    except httpx.HTTPError as error:
        if isinstance(error,httpx.HTTPStatusError) and context_overflow_error(error):
            raise ContextWindowExceeded(characters=size) from None
        if tools and _native_tool_unsupported(error):
            raise ModelOutputError('模型服务不支持原生工具调用',code='native_unsupported_tool_calling') from None
        # Preserve status/transport type for the agent's retry and sanitizing
        # boundary. Masking it as ValueError used to turn 429/503 into a
        # misleading format error and prevented automatic recovery.
        raise
    finally:
        event['duration_ms']=round((time.monotonic()-started)*1000)
        if tid:
            current=get('turn',tid).get('model_usage',{})
            patch('turn',tid,model_usage=dict(current,duration_ms=current.get('duration_ms',0)+event['duration_ms']))
        # No messages, responses, URLs, keys, or raw exceptions in telemetry.
        put('model_event',event,'model_'+uid())
        from .store import all_items,delete
        events=sorted(all_items('model_event'),key=lambda e:e.get('started_at',''),reverse=True)
        for expired in events[2000:]:delete('model_event',expired['id'])

def validate_configuration(value,old=None):
    from .connectors import safe_api_url
    raw=value.model_dump();raw['api_key']=value.api_key.get_secret_value();raw['base_url']=raw['base_url'].rstrip('/')
    old=legacy_settings() if old is None else old
    if not raw['api_key'] and old.get('engine','atlas')==raw['engine'] and old.get('base_url','').rstrip('/')==raw['base_url']:raw['api_key']=old.get('api_key','')
    parsed=urlsplit(raw['base_url'])
    if parsed.scheme!='https' and parsed.hostname not in {x.strip() for x in os.getenv('API_ALLOWED_HOSTS','').split(',') if x.strip()}:raise ValueError('模型接口请使用HTTPS；本地模型需在服务配置中允许其地址')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('模型地址不应携带凭证或查询参数')
    # Same private-network/metadata restrictions as connectors. Configuration is
    # owner-only; no user-controlled URL enters a model tool's network capability.
    safe_api_url(raw['base_url'])
    if raw['engine']=='opencode':
        from .opencode import validate_configuration
        raw['tool_protocol']='json'
        raw.update(validate_configuration(raw))
    try:complete([{'role':'user','content':'请仅回复 OK，用于验证模型连接。'}],32,raw)
    except Exception:raise ValueError('模型连接验证失败，请检查兼容接口地址、模型名称、密钥及服务权限') from None
    if raw['tool_protocol']=='native':
        probe=[{'type':'function','function':{'name':'connection_check','description':'连接验证，返回ok=true','parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}}}]
        try:
            result=json.loads(complete([{'role':'user','content':'调用connection_check，ok=true。'}],128,raw,purpose='tool_capability_probe',tools=probe))
            if result.get('tool')!='connection_check' or result.get('arguments')!={'ok':True}:raise ValueError()
        except Exception:raise ValueError('接口未通过原生工具调用验证，请管理员检查模型能力及网关的工具调用支持') from None
    return raw

def configure(value):
    from .store import put,seal,now,all_items
    # This legacy operator endpoint never updates a managed catalog implicitly.
    try:
        from .store import get
        get('system','model-catalog')
    except KeyError:pass
    else:raise ValueError('模型目录由后台配置文件统一管理，请修改配置文件后重启服务')
    if any(t.get('state') not in ('done','failed') for t in all_items('turn')):raise ValueError('请先停止或等待正在运行的任务结束，再切换模型或引擎')
    raw=validate_configuration(value)
    put('system',{'secret_config':seal(raw),'verified_at':now()},'model-provider')
    return status()
