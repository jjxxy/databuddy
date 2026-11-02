"""Owner-configured model connection. Credentials are never returned or put in Git."""
from .workspace_lifecycle import assert_turn_live

import json,os,time,re
from contextvars import ContextVar
from contextlib import contextmanager
from urllib.parse import urlsplit
import httpx
from typing import Literal
from pydantic import Field,SecretStr,model_validator
from .models import StrictModel
from .config import LLM_BASE,LLM_KEY,LLM_MODEL
from .provider_compatibility import bounded_request_options,response_token_allowance,configured_input_character_limit,managed_model_limits

class ProviderInput(StrictModel):
    engine:Literal['nexus','opencode']='nexus'
    base_url:str=Field(min_length=8,max_length=500)
    model:str=Field(min_length=1,max_length=120)
    api_key:SecretStr=Field(default=SecretStr(''))
    share_data:bool=False
    vision:bool=False
    tool_protocol:Literal['json','native']='json'
    upstream_base_url:str=Field(default='',max_length=500)
    # Optional, per-model provider limits. These are frozen in the same
    # encrypted catalog revision as model credentials; unknown remains None.
    context_window_tokens:int|None=Field(default=None,ge=256,le=100000000,strict=True)
    max_output_tokens:int|None=Field(default=None,ge=1,le=100000000,strict=True)

    @model_validator(mode='after')
    def validate_window(self):
        if self.context_window_tokens is not None and self.max_output_tokens is not None and self.max_output_tokens>=self.context_window_tokens:
            raise ValueError('模型输出上限必须小于上下文窗口')
        return self

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
    if os.getenv('NEXUS_DESKTOP_MODEL_DISABLED')=='1':
        return {'configured':False,'base_url':'','model':'','share_data':False,'engine':'nexus','tool_protocol':'native','upstream_base_url':'','opencode_version':None}
    value=settings()
    return {'base_url':value.get('base_url',''),'configured':bool(value.get('base_url') and value.get('model')),'model':value.get('model',''),'share_data':bool(value.get('share_data')),'tool_protocol':value.get('tool_protocol','json'),'engine':value.get('engine','nexus'),'upstream_base_url':value.get('upstream_base_url',''),'opencode_version':value.get('opencode_version')}

_scope=ContextVar('nexus_model_scope',default=None)
_project_scope=ContextVar('nexus_project_scope',default=None)

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


_CONTEXT_ERROR_CODES=frozenset(('context_length_exceeded','context_window_exceeded','context_overflow',
    'contextoverflowerror','contextlengthexceeded','model_context_window_exceeded',
    'input_token_limit_exceeded','max_prompt_tokens_exceeded'))


def _context_overflow_code(value):
    """Inspect only structured error fields when a gateway misuses HTTP 5xx."""
    if not isinstance(value,dict):return False
    if any(str(value.get(key) or '').lower() in _CONTEXT_ERROR_CODES for key in ('code','type','name')):return True
    return _context_overflow_code(value.get('error')) or _context_overflow_code(value.get('data'))


def context_overflow_error(value):
    """Classify a provider error envelope, never arbitrary document/tool text."""
    if isinstance(value,httpx.HTTPStatusError):
        status=value.response.status_code
        if status not in (400,413,422,500,502,503,504):return False
        try:value=value.response.json()
        except (ValueError,httpx.ResponseNotRead):return False
        if status in (500,502,503,504):return _context_overflow_code(value)
    if isinstance(value,dict):
        if 'error' in value:return context_overflow_error(value['error'])
        if any(str(value.get(key) or '').lower() in _CONTEXT_ERROR_CODES for key in ('code','type','name')):return True
        return context_overflow_error(value.get('message','')) or (isinstance(value.get('data'),dict) and context_overflow_error(value['data']))
    if not isinstance(value,str):return False
    return bool(re.search(r'maximum context (?:length|window)|context (?:length|window).{0,60}(?:exceed|limit)|(?:prompt|input).{0,35}(?:too long|too large|exceeds.{0,25}token)|(?:prompt|input)[ _-]tokens?.{0,35}(?:exceed|above).{0,25}(?:limit|maximum|window)|too many (?:input )?tokens|单次模型上下文超过限制|上下文(?:长度)?超过(?:模型)?(?:限制|上限)',value[:4000],re.I))


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


class _SSEParser:
    """Incrementally reconstruct a complete SSE decision without retaining reasoning."""
    # Bound any one event while allowing arbitrarily many heartbeat/thinking
    # events without buffering the entire stream. User-facing content and
    # executable tool arguments have their own independent bounds.
    EVENT_LIMIT=4*1024*1024

    def __init__(self):
        self.buffer=bytearray();self.content=[];self.content_size=0
        self.calls={};self.finish=None;self.usage={};self.done=False;self.seen=False
        self.ended=False;self.first=True;self.error=None

    def feed(self,data):
        if self.done:return
        # Process bounded pieces even if an upstream yields an enormous chunk.
        for offset in range(0,len(data),65536):
            self.buffer.extend(data[offset:offset+65536])
            while True:
                match=re.search(rb'\r?\n\r?\n',self.buffer)
                if not match:break
                if match.start()>self.EVENT_LIMIT:raise ModelOutputError('模型响应过长，任务尚未完成',code='response_too_large')
                raw=bytes(self.buffer[:match.start()]);del self.buffer[:match.end()]
                self.ended=True;self._block(raw)
                if self.done:
                    self.buffer.clear();return
            if len(self.buffer)>self.EVENT_LIMIT:raise ModelOutputError('模型响应过长，任务尚未完成',code='response_too_large')
            if self.buffer:self.ended=False

    def _block(self,raw):
        try:
            text=raw.decode('utf-8-sig' if self.first else 'utf-8')
        except UnicodeDecodeError:raise ModelOutputError('模型服务返回了无效响应',code='invalid_response_json') from None
        self.first=False
        lines=[line[5:].lstrip(' ') for line in text.splitlines() if line.startswith('data:')]
        if not lines:return
        data='\n'.join(lines)
        if not data.strip():return
        if data.strip()=='[DONE]':self.done=True;return
        try:event=json.loads(data)
        except (ValueError,TypeError):raise ModelOutputError('模型流式响应格式不完整',code='invalid_response_json') from None
        if not isinstance(event,dict):raise ModelOutputError('模型流式响应格式不完整',code='invalid_response_json')
        if event.get('error'):
            self.error=event['error'];self.done=True;return
        if isinstance(event.get('usage'),dict):self.usage=event['usage']
        choices=event.get('choices',[])
        if not isinstance(choices,list) or len(choices)>1:raise ModelOutputError('模型流式响应包含多个结果',code='invalid_model_response')
        if not choices:return
        if self.finish is not None:raise ModelOutputError('模型流式响应结束后仍有内容',code='invalid_model_response')
        choice=choices[0]
        if not isinstance(choice,dict) or choice.get('index',0)!=0:raise ModelOutputError('模型流式响应格式不完整',code='invalid_model_response')
        self.seen=True;delta=choice.get('delta') or {}
        if not isinstance(delta,dict):raise ModelOutputError('模型流式响应格式不完整',code='invalid_model_response')
        piece=delta.get('content')
        if piece is not None:
            if not isinstance(piece,str):raise ModelOutputError('模型流式响应内容无效',code='invalid_model_response')
            self.content_size+=len(piece)
            if self.content_size>100000:raise ModelOutputError('模型响应过长，任务尚未完成',code='response_too_large')
            if piece:self.content.append(piece)
        fragments=delta.get('tool_calls') or []
        if not isinstance(fragments,list) or len(fragments)>64:raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
        for fragment in fragments:
            if not isinstance(fragment,dict):raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
            index=fragment.get('index',0)
            if type(index) is not int or not 0<=index<64:raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
            if index not in self.calls:self.calls[index]={'type':'function','function':{'name':'','arguments':''}}
            item=self.calls[index]
            if fragment.get('type') not in (None,'function'):raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
            call_id=fragment.get('id')
            if call_id:
                if not isinstance(call_id,str) or len(call_id)>1000 or item.get('id') not in (None,call_id):raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
                item['id']=call_id
            fn=fragment.get('function') or {}
            if not isinstance(fn,dict):raise ModelOutputError('模型工具调用格式无效',code='native_invalid_response')
            for key in ('name','arguments'):
                part=fn.get(key)
                if part is None:continue
                if not isinstance(part,str):raise ModelOutputError('模型工具调用格式无效',code='native_invalid_arguments')
                item['function'][key]+=part
                if len(item['function'][key])>100000:raise ModelOutputError('模型工具调用参数过长',code='native_invalid_arguments')
        reason=choice.get('finish_reason')
        if reason is not None:
            if reason not in ('stop','tool_calls','length','content_filter'):raise ModelOutputError('模型流式响应未正常完成',code='invalid_model_response')
            self.finish=reason

    def result(self):
        if self.error is not None:return {'error':self.error}
        if not self.seen or self.finish is None or not (self.done or self.ended):
            raise ModelOutputError('模型流式响应未完整结束',code='invalid_model_response')
        # A stop completion is not confirmation of a tool-call completion.
        # Never execute even syntactically complete arguments when a gateway
        # ends a partial tool response as ordinary text.
        if self.calls and self.finish=='stop':raise ModelOutputError('模型工具调用未完整结束，未执行操作',code='native_invalid_response')
        message={'content':''.join(self.content) if self.content else None}
        if self.calls:message['tool_calls']=[self.calls[index] for index in sorted(self.calls)]
        return {'choices':[{'finish_reason':self.finish,'message':message}],'usage':self.usage}


def _stream_response_payload(body):
    """Normalize a bounded SSE response; a tool call is data until validated."""
    parser=_SSEParser();parser.feed(body);return parser.result()


def complete(messages,max_tokens=None,configuration=None,purpose='agent_planning',tools=None):
    if os.getenv('NEXUS_DESKTOP_MODEL_DISABLED')=='1':raise ValueError(os.getenv('NEXUS_DESKTOP_MODEL_NOTICE','云端模型不可用，本地能力可用。'))
    from .store import get,patch,put,now,uid
    value=configuration or settings()
    from .network_policy import check_url
    if value.get('base_url'):check_url(value['base_url'])
    if value.get('engine')=='opencode' and value.get('upstream_base_url'):check_url(value['upstream_base_url'])
    if not value.get('base_url') or not value.get('model'):raise ValueError('请先连接一个语言模型')
    size=len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False));tid=_scope.get();usage={};started=time.monotonic()
    pid=_project_scope.get()
    if tid:pid=get('turn',tid)['project_id']
    if not pid and os.getenv('NEXUS_MODEL_SOURCE_ID'):
        try:pid=get('source',os.environ['NEXUS_MODEL_SOURCE_ID']).get('project_id')
        except KeyError:pass
    if pid:
        from .company_search import assert_model_allowed
        assert_model_allowed(pid)
        from .agent_mesh import assert_project_model_allowed
        assert_project_model_allowed(pid)
    if max_tokens is None:max_tokens=response_token_allowance(value)
    else:
        # Callers such as visual inspection may supply their own allowance.
        # A configured per-model limit still applies to those requests.
        limits=managed_model_limits(value.get('base_url',''),value.get('model',''),
            context_window_tokens=value.get('context_window_tokens'),max_output_tokens=value.get('max_output_tokens'))
        if limits.get('output') is not None:max_tokens=min(max_tokens,limits['output'])
        if limits.get('context') is not None and max_tokens>=limits['context']:
            max_tokens=min(max_tokens,max(1,limits['context']//4))
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
    event={'purpose':purpose,'model':value['model'],'engine':value.get('engine','nexus'),'protocol':'native' if tools else 'json','turn_id':tid,'source_id':os.getenv('NEXUS_MODEL_SOURCE_ID'),'started_at':now(),'input_characters':size,'max_output_tokens':max_tokens,'status':'failed'}
    try:
        if value.get('engine')=='opencode':
            from .opencode import complete as opencode_complete
            content,tokens,receipt=opencode_complete(messages,value,tools,max_tokens)
            event.update(status='succeeded',usage=tokens,usage_reported=bool(tokens),opencode=receipt)
            if tid:
                current=get('turn',tid).get('model_usage',{})
                for k,v in tokens.items():current[k]=current.get(k,0)+v
                # OpenCode may issue internal requests; reserve our full allowance.
                current.update(usage_incomplete=True,budget_scope='nexus_requests_not_upstream_billing')
                patch('turn',tid,model_usage=current)
            return content
        body={'model':value['model'],'messages':messages,'max_tokens':max_tokens,'temperature':0,'stream':False}
        body.update(bounded_request_options(value['base_url'],value['model']))
        if tools:body.update(tools=tools,tool_choice='required',parallel_tool_calls=False)
        from .model_transport import model_http_timeout
        with httpx.Client(timeout=model_http_timeout(),trust_env=False) as client:
            request_headers={'Authorization':'Bearer '+value['api_key']} if value.get('api_key') else {}
            with client.stream('POST',value['base_url'].rstrip('/')+'/chat/completions',headers=request_headers,json=body) as incoming:
                raw=bytearray();pending=bytearray()
                streaming=incoming.status_code<400 and 'text/event-stream' in incoming.headers.get('content-type','').lower()
                parser=_SSEParser() if streaming else None
                decided=streaming or incoming.status_code>=400
                # Non-streaming DeepSeek gateways may send leading blank lines
                # as keepalives while waiting for the model. They carry no JSON
                # information and must not exhaust the response size limit.
                oversized_error=False;last_liveness_check=time.monotonic()
                for chunk in incoming.iter_bytes():
                    # A heartbeat is network progress, not permission to keep
                    # consuming a cancelled or revoked task. A fully silent
                    # socket can only be interrupted when its read times out.
                    if tid and time.monotonic()-last_liveness_check>=2:
                        current=get('turn',tid)
                        if turn.get('state') not in ('done','failed') and current.get('state') in ('done','failed'):
                            raise httpx.ReadError('模型任务已停止',request=incoming.request)
                        assert_turn_live(current);agent_gateway.assert_turn_allowed(current);agent_team.assert_turn_allowed(current)
                        last_liveness_check=time.monotonic()
                    for offset in range(0,len(chunk),65536):
                        part=chunk[offset:offset+65536]
                        if parser is not None:
                            parser.feed(part)
                            if parser.done:break
                            continue
                        if not decided:
                            pending.extend(part)
                            pending[:]=pending.lstrip(b' \t\r\n')
                            prefix=bytes(pending)
                            if prefix.startswith(b'\xef\xbb\xbf'):
                                prefix=prefix[3:]
                            elif prefix and b'\xef\xbb\xbf'.startswith(prefix):continue
                            if not prefix:continue
                            event_prefixes=(b'data:',b'event:',b'id:',b'retry:')
                            if prefix.startswith((b'data:',b'event:',b'id:',b'retry:',b':')):
                                parser=_SSEParser();parser.feed(prefix);pending.clear();decided=True
                                if parser.done:break
                                continue
                            if any(candidate.startswith(prefix) for candidate in event_prefixes):continue
                            decided=True;part=bytes(pending);pending.clear()
                        elif incoming.status_code<400 and not raw:
                            part=part.lstrip(b' \t\r\n')
                        if len(raw)+len(part)>4*1024*1024:
                            if incoming.status_code>=400:raw.clear();oversized_error=True;break
                            raise ModelOutputError('模型响应过长，任务尚未完成',code='response_too_large')
                        raw.extend(part)
                    if oversized_error or parser is not None and parser.done:break
                headers=httpx.Headers(incoming.headers)
                # iter_bytes() has already decoded content encoding.
                headers.pop('content-encoding',None);headers.pop('content-length',None)
                r=httpx.Response(incoming.status_code,headers=headers,content=bytes(raw or pending),request=incoming.request)
            r.raise_for_status()
            if parser is not None:payload=parser.result()
            else:
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
                elif context_overflow_error(payload['error']):
                    # Some OpenAI-compatible gateways return an error envelope
                    # with HTTP 200. Route a verified window overflow to the
                    # same checkpoint/retry path as an HTTP 400/422 rejection.
                    raise ContextWindowExceeded(characters=size) from None
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
    if not raw['api_key'] and old.get('engine','nexus')==raw['engine'] and old.get('base_url','').rstrip('/')==raw['base_url']:raw['api_key']=old.get('api_key','')
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
