"""Local OpenCode session runtime and authenticated, task-scoped tool bridge.

OpenCode owns the model/tool loop. Atlas owns project authorization, exact data,
durable tool receipts, approvals and final evidence verification. No native
OpenCode filesystem, shell, browser, subagent or arbitrary MCP tool is exposed.
"""
from .workspace_lifecycle import assert_turn_live

import contextvars
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

from .opencode import VERSION
from .provider_compatibility import (bounded_request_options,managed_model_limits,
    response_token_allowance,configured_input_character_limit)

# Fallback for a provider with unknown response metadata. Managed DeepSeek uses
# its verified upstream allowance; compaction has its own summary response size.
OUTPUT_LIMIT = 3200
BUILTIN_PLUGINS = {'opencode.agent', 'opencode.config.agent', 'opencode.provider.dynamic',
                   'opencode.config.provider', 'opencode.provider.opencode', 'opencode.config.policy'}
log = logging.getLogger(__name__)


def is_enabled():
    return os.getenv('ATLAS_OPENCODE_RUNTIME') == '1'


def _root():
    from .config import DATA
    return DATA / 'opencode-runtime'


def _identity(value):
    # Credential changes must create another provider identity, including for a
    # pinned older turn. Never publish this credential-derived hash in the UI.
    record = {key: value.get(key, '') for key in ('base_url', 'model', 'api_key')}
    return 'atlas_' + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24]


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    path.chmod(0o600)


def prepare(configuration, step_schema):
    """Return a supervisor command spec, called after dropping privileges."""
    binary = os.getenv('ATLAS_OPENCODE_BINARY') or shutil.which('opencode')
    if not binary or not Path(binary).is_file():
        raise ValueError('OpenCode运行文件缺失，请使用包含OpenCode的部署镜像')
    root = _root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    work = root / 'work'
    work.mkdir(exist_ok=True, mode=0o700)
    plugin = root / 'plugin'
    plugin.mkdir(exist_ok=True, mode=0o700)
    source = Path(__file__).with_name('opencode_plugin.mjs').read_bytes()
    (plugin / 'index.js').write_bytes(source)
    (plugin / 'index.js').chmod(0o600)
    context_source=Path(__file__).with_name('opencode_context.mjs').read_bytes()
    (plugin / 'context.mjs').write_bytes(context_source)
    (plugin / 'context.mjs').chmod(0o600)
    context_sha256=hashlib.sha256(context_source).hexdigest()
    entries = list(configuration.get('models', []))
    # Pending/resumable tasks retain their selected immutable model revision.
    from .store import all_items
    from .model_catalog import settings_for_turn
    for turn in all_items('turn'):
        if turn.get('model_catalog_revision') and (turn.get('state') not in ('done', 'failed') or turn.get('resume_available')):
            try:
                selected = settings_for_turn(turn)
                if selected:entries.append(selected)
            except ValueError:
                pass  # A deliberately disabled model stays unavailable.
    providers = {}
    for value in entries:
        from .network_policy import check_url
        check_url(value['base_url'])
        if value.get('engine', 'atlas') != 'atlas':
            raise ValueError('后台模型目录应配置模型上游；OpenCode由部署自动运行')
        identity = _identity(value)
        providers[identity] = {
            'name': 'Atlas managed model',
            'package': '@opencode/ai/providers/openai-compatible',
            'settings': {'baseURL': value['base_url'].rstrip('/'), 'apiKey': value.get('api_key', ''),
                         'timeout': 45000, 'headerTimeout': 45000, 'chunkTimeout': 45000},
            'compatibility': {'reasoningField': 'reasoning_content'},
            'body': {'max_tokens': response_token_allowance(value), **bounded_request_options(value['base_url'], value['model'])},
            'models': {value['model']: {'modelID': value['model'], 'name': 'Atlas managed model',
                                       **({'limit':managed_model_limits(value['base_url'],value['model'])} if managed_model_limits(value['base_url'],value['model']) else {}),
                                       'capabilities': {'tools': True, 'input': ['text'], 'output': ['text']}}}}
    if not providers:raise ValueError('需要先在后台配置至少一个模型')
    default = next((value for value in entries if value.get('id') == configuration.get('default_model_id')), entries[0])
    config = {
        'update': 'disable',
        'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'},
                        {'action': 'atlas_step', 'resource': '*', 'effect': 'allow'}],
        # Disable discovery by default, then enable only the minimum bundled
        # provider/agent configuration and this checked local bridge. The two
        # policy/Console plugins are mandatory upstream and cannot be disabled.
        'plugins': ['-*', 'opencode.agent', 'opencode.config.agent', 'opencode.provider.dynamic',
                    'opencode.config.provider', {'package': str(plugin), 'options': {'step_schema': step_schema,'context_helper_sha256':context_sha256}},
                    'atlas.business-tools'],
        'mcp': {'servers': {}}, 'instructions': [], 'providers': providers,
        'compaction': {'auto':True,'keep':{'tokens':8000},'buffer':16000},
        'model': _identity(default) + '/' + default['model'],
    }
    _write(root / 'config/opencode/opencode.json', config)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    password = secrets.token_urlsafe(36)
    connection = {'base_url': f'http://127.0.0.1:{port}', 'api_key': password,
                  'providers': list(providers), 'version': VERSION,
                  'plugin_sha256': hashlib.sha256(source).hexdigest(),
                  'context_helper_sha256':context_sha256,
                  'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}
    _write(root / 'connection.json', connection)
    # No APP_TOKEN, database URL, connector credential, or arbitrary host env.
    env = {'PATH': os.getenv('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': str(root / 'home'),
           'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_DATA_HOME': str(root / 'data'),
           'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_STATE_HOME': str(root / 'state'),
           'OPENCODE_SERVER_PASSWORD': password, 'OPENCODE_DISABLE_MODELS_FETCH': 'true',
           'OPENCODE_DISABLE_FREE_MODELS': 'true'}
    if os.name=='nt':
        for key in ('SystemRoot','SYSTEMROOT','WINDIR'):
            if os.getenv(key):env[key]=os.environ[key]
        env.update(TEMP=str(root/'cache'),TMP=str(root/'cache'),USERPROFILE=str(root/'home'))
        for directory in ('home','cache'):(root/directory).mkdir(parents=True,exist_ok=True)
    return {'command': [str(Path(binary).resolve()), 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
            'env': env, 'cwd': str(work)}


def connection(value=None):
    if os.getenv('ATLAS_DESKTOP_MODEL_DISABLED')=='1':raise ValueError(os.getenv('ATLAS_DESKTOP_MODEL_NOTICE','云端模型不可用，本地能力可用。'))
    raw = json.loads((_root() / 'connection.json').read_text(encoding='utf-8'))
    if value is not None:
        identity = _identity(value)
        if identity not in raw.get('providers', []):
            raise ValueError('所选模型未加载到OpenCode运行服务，请由管理员重启服务')
        raw['model_ref'] = {'providerID': identity, 'id': value['model']}
    return raw


def _check_upstream(payload, selected):
    from .network_policy import check_url
    check_url(selected['base_url'])
    expected = httpx.URL(selected['base_url'].rstrip('/') + '/chat/completions')
    try:actual = httpx.URL(payload.get('target_url', ''))
    except (TypeError, httpx.InvalidURL):raise ValueError('OpenCode模型上游地址无效') from None
    def identity(url):
        return (url.scheme, url.host, url.port or (443 if url.scheme == 'https' else 80), url.path)
    if (identity(actual) != identity(expected) or actual.query or actual.fragment
            or payload.get('model_id') != selected['model']):
        raise ValueError('OpenCode请求与任务冻结的模型上游不匹配，未发送业务内容')


def _input_character_limit(payload, selected):
    """Unset means no Atlas character gate; native token-window checks remain.

    Explicit operator character caps still apply to that deployment, including
    real compaction requests identified by the authenticated plugin hook.
    """
    return configured_input_character_limit(selected.get('model'),compaction=payload.get('kind')=='compaction')


def _new_tool_definitions(result,known):
    """Send changed schemas once; originals remain in native/tool history."""
    if not isinstance(result,dict) or not isinstance(result.get('available_tools'),list):return result
    changed=[]
    for tool in result['available_tools']:
        key=tool.get('name') if isinstance(tool,dict) else None
        digest=hashlib.sha256(json.dumps(tool,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if key and known.get(key)!=digest:
            changed.append(tool);known[key]=digest
    return dict(result,available_tools=changed,available_tools_scope='new_or_changed; existing definitions remain available')


def _needs_compaction(native_context,content,requested=False,character_cap=None):
    combined=len(json.dumps(native_context,ensure_ascii=True))+len(json.dumps(content,ensure_ascii=True))
    # The native runtime checks the selected model's real token window before
    # every request. A legacy 60k character trigger must not pre-empt that.
    return bool(requested or character_cap is not None and combined>character_cap)


def _request(value, method, path, body=None, timeout=5, limit=32*1024*1024):
    # Only the supervisor-created loopback endpoint is accepted, never a model
    # supplied callback or URL. Runtime auth is not a workspace credential.
    # This byte guard covers serialized native history, not model characters.
    # A 2 MB history guard could interrupt the verified 1M-token model before
    # its own automatic compaction; retain a separate, generous memory bound.
    url = httpx.URL(value['base_url'])
    if url.host != '127.0.0.1' or url.scheme != 'http':raise ValueError('Invalid local OpenCode endpoint')
    with httpx.Client(timeout=timeout, trust_env=False, auth=('opencode', value['api_key'])) as client:
        with client.stream(method, str(url).rstrip('/') + path, json=body) as response:
            response.raise_for_status(); data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:raise ValueError('OpenCode会话上下文超过读取限制')
    return json.loads(data) if data else None


def readiness_diagnostic():
    """Return bounded diagnostics without model configuration or exception text."""
    if not is_enabled():return {'ready':True,'stage':'disabled','code':'disabled','retryable':False}
    stage = 'connection_metadata'
    def failed(code, *, retryable=False, **extra):
        return {'ready':False,'stage':stage,'code':code,'retryable':retryable,**extra}
    try:
        value = connection()
        stage = 'version'
        if _request(value, 'GET', '/api/info')['version'] != VERSION:return failed('version_mismatch')
        stage = 'plugin_catalog'
        plugins = _request(value, 'GET', '/api/plugin')['data']
        if {item['id'] for item in plugins} != BUILTIN_PLUGINS | {'atlas.business-tools'}:return failed('plugin_set_mismatch')
        active = next((item for item in plugins if item['id'] == 'atlas.business-tools'), None)
        if not active or active.get('state', {}).get('status') != 'active':return failed('plugin_inactive')
        if active.get('source', {}).get('path') != str(_root() / 'plugin/index.js'):return failed('plugin_path_mismatch')
        stage = 'plugin_integrity'
        if hashlib.sha256((_root() / 'plugin/index.js').read_bytes()).hexdigest() != value['plugin_sha256']:return failed('plugin_integrity_mismatch')
        stage = 'configuration_catalog'
        loaded = _request(value, 'GET', '/api/config')
        documents = [item for item in loaded if item.get('type') == 'document']
        expected_path = str(_root() / 'config/opencode/opencode.json')
        if len(documents) != 1 or documents[0].get('path') != expected_path:return failed('configuration_document_mismatch')
        # The exact supervisor config is checked before any project prompt.
        stage = 'configuration_integrity'
        actual = json.loads((_root() / 'config/opencode/opencode.json').read_text(encoding='utf-8'))
        if hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest() != value['config_sha256']:return failed('configuration_integrity_mismatch')
        document = documents[0]['info']
        stage = 'effective_configuration'
        matches = (all(document.get(key) == actual[key] for key in ('providers', 'permissions', 'plugins', 'instructions', 'compaction'))
                and document.get('mcp', {}) in ({}, {'servers': {}})
                and not any(document.get(key) for key in ('agents', 'commands', 'references', 'skills', 'policies')))
        if not matches:return failed('effective_configuration_mismatch')
        return {'ready':True,'stage':'complete','code':'ready','retryable':False}
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return failed('local_http_status',retryable=status in (429,502,503,504),
                      error_type='http_status_error',http_status=status)
    except httpx.TimeoutException:
        return failed('local_timeout',retryable=True,error_type='timeout')
    except httpx.NetworkError:
        return failed('local_connection_error',retryable=True,error_type='network_error')
    except httpx.HTTPError:
        return failed('local_http_error',error_type='http_error')
    except OSError:
        return failed('runtime_files_unavailable',error_type='file_error')
    except (ValueError, KeyError, TypeError, AttributeError):
        return failed('invalid_readiness_data',error_type='invalid_data')


def ready():
    return readiness_diagnostic()['ready']


def require_ready():
    """Retry one local transport interruption, never configuration drift."""
    for attempt in range(2):
        diagnostic = readiness_diagnostic()
        if diagnostic['ready']:return diagnostic
        log.warning('OpenCode readiness failed code=%s stage=%s error_type=%s http_status=%s retryable=%s attempt=%d',
                    diagnostic['code'],diagnostic['stage'],diagnostic.get('error_type','none'),
                    diagnostic.get('http_status','none'),diagnostic['retryable'],attempt+1)
        if attempt == 0 and diagnostic['retryable']:
            time.sleep(.2)
            continue
        message = ('AI运行服务暂不可用，资料和已有工作已保留。请稍后点“继续”重试。' if diagnostic['retryable']
                   else 'AI运行服务连接校验未通过，资料和已有工作已保留。请联系管理员检查后再点“继续”。')
        raise ValueError(message) from None


healthy = ready


def wait_ready(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():return True
        time.sleep(.2)
    raise ValueError('OpenCode运行服务未就绪，未启动业务处理')


def run_session(turn, prompt, info, callback):
    """Run a native tool loop; callback(step, durable_call_id) returns JSON.

    Callback executes serially in the caller's copied context, preserving its
    task/model and execution-lock fencing context. Returning a terminal Atlas
    turn stops the model loop. Free-text OpenCode answers are never completion
    evidence and are not forwarded to the user.
    """
    from . import provider
    info=dict(info)
    # The exact Step schema already appears in the authorized native function
    # definition. Avoid charging for a duplicate copy in every user message.
    info.pop('response_schema',None)
    prompt+='原生atlas_step函数参数就是本轮response_schema，以函数定义为准，不在正文重复该结构。'
    from .store import get, patch, now
    from .locks import assert_owned
    from .company_search import assert_model_allowed
    require_ready()
    value = connection(provider.settings())
    token = secrets.token_urlsafe(36)
    context = contextvars.copy_context()
    serial = threading.Lock()
    stop = threading.Event()
    closing = threading.Event()
    failure = []
    context_rebuild = [False]
    known_tools={tool['name']:hashlib.sha256(json.dumps(tool,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                 for tool in info.get('tools',[]) if isinstance(tool,dict) and tool.get('name')}
    from .memory_service import generation as memory_generation
    starting_memory_generation=memory_generation(turn['project_id'])
    previous_generation=turn.get('opencode_memory_generation')
    session_id = [turn.get('opencode_session_id')]
    if session_id[0] and previous_generation!=starting_memory_generation:
        # A preference forgotten/edited outside this task may remain inside
        # OpenCode's native history. Never reuse that copy after revocation.
        retired=list(turn.get('opencode_retired_sessions',[]))
        retired.append(session_id[0]);session_id[0]=None
        patch('turn',turn['id'],opencode_session_id=None,opencode_retired_sessions=retired[-10:])
    patch('turn',turn['id'],opencode_memory_generation=starting_memory_generation)
    started = time.monotonic()
    calls = [0]
    from .task_budget import effective_limits,exceeded_reason
    duration_cap=effective_limits(turn)['duration_ms']
    remaining_ms=None if duration_cap is None else duration_cap-turn.get('model_usage',{}).get('duration_ms',0)
    if remaining_ms is not None and remaining_ms<=0:
        raise provider.BudgetExceeded('此任务已达到配置的处理时长上限，已有成果已保存')

    def dispatch(payload):
        assert_owned()
        current = get('turn', turn['id'])
        if current['project_id'] != turn['project_id']:raise ValueError('会话项目不匹配')
        if current['state'] not in ('agent', 'agent_tool'):
            stop.set();return {'state': current['state'], 'terminal': True, 'outcome': current.get('outcome')}
        if payload.get('op') == 'model_request':
            if context_rebuild[0]:return {'terminal':True,'context_rebuild_required':True}
            if memory_generation(turn['project_id'])!=starting_memory_generation:
                raise provider.BudgetExceeded('记忆已被修改或撤回，旧模型上下文已停止发送；回复继续后使用新的上下文')
            assert_model_allowed(turn['project_id'])
            from . import agent_gateway,agent_team,agent_mesh,collaboration_budget
            assert_turn_live(current);agent_gateway.assert_turn_allowed(current);agent_team.assert_turn_allowed(current)
            agent_mesh.assert_project_model_allowed(turn['project_id'])
            selected=provider.settings()
            _check_upstream(payload, selected)
            size = payload.get('input_characters')
            if type(size) is not int or size<0:raise ValueError('模型请求字符计量无效')
            character_cap=_input_character_limit(payload,selected)
            if character_cap is not None and size>character_cap:
                raise provider.BudgetExceeded('此请求超过管理员配置的上下文字符上限，已有成果已保存')
            budget=effective_limits(current)
            usage = current.get('model_usage', {})
            output_allowance=payload.get('max_output_tokens',OUTPUT_LIMIT)
            if type(output_allowance) is not int or output_allowance<=0:
                raise ValueError('模型回复长度计量无效')
            reported = payload.get('reported_tokens') or {}
            reported_output = reported.get('output')
            # OpenCode reports cumulative usage on its session. A positive
            # increase releases the previous call's reserved token allowance;
            # absent/zero usage remains conservatively reserved.
            if type(reported_output) is int and reported_output > usage.get('opencode_reported_output', 0):
                usage = dict(usage, output_tokens_budgeted=max(0, usage.get('output_tokens_budgeted', 0) - usage.get('opencode_last_reserved_output_tokens',OUTPUT_LIMIT))
                             + reported_output - usage.get('opencode_reported_output', 0), opencode_reported_output=reported_output)
            if budget['output_tokens'] is not None:
                available=budget['output_tokens']-usage.get('output_tokens_budgeted',0)
                if available<128:raise provider.BudgetExceeded('此任务已达到配置的模型输出上限，已有成果已保存')
                output_allowance=min(output_allowance,available)
            elapsed = round((time.monotonic() - started) * 1000)
            reason=exceeded_reason(budget,usage,input_characters=size,output_tokens=output_allowance,elapsed_ms=elapsed)
            if reason:
                label={'model_calls':'调用次数','input_characters':'累计输入','output_tokens':'累计输出','duration_ms':'处理时长'}[reason]
                raise provider.BudgetExceeded('此任务已达到配置的'+label+'上限，已有成果已保存')
            from .quotas import reserve
            agent_gateway.admit_model_request(current,size,output_allowance)
            collaboration_budget.admit(current,size,output_allowance)
            reserve(output_allowance)
            compacting=payload.get('kind')=='compaction'
            projection=payload.get('context_projection')
            if not isinstance(projection,dict) or any(type(v) is not int or v<0 or v>2000000 for v in projection.values()):projection=None
            protocol=payload.get('native_protocol')
            if (not isinstance(protocol,dict) or protocol.get('tool_choice') not in ('auto','none','required','named','absent','other')
                    or type(protocol.get('function_count')) is not int or not 0<=protocol['function_count']<=128
                    or any(type(protocol.get(key)) is not bool for key in ('atlas_step_only','choice_forced'))):protocol=None
            patch('turn', turn['id'], model_usage=dict(usage, calls=usage.get('calls', 0) + 1,
                  input_characters=usage.get('input_characters', 0) + size,
                  output_tokens_budgeted=usage.get('output_tokens_budgeted', 0) + output_allowance,
                  opencode_last_reserved_output_tokens=output_allowance,
                  opencode_compaction_calls=usage.get('opencode_compaction_calls',0)+int(compacting),
                  opencode_compaction_input_characters=usage.get('opencode_compaction_input_characters',0)+(size if compacting else 0),
                  largest_opencode_request_characters=max(size,usage.get('largest_opencode_request_characters',0)),
                  largest_opencode_request_components=projection if size>=usage.get('largest_opencode_request_characters',0) else usage.get('largest_opencode_request_components'),
                  last_opencode_context_projection=projection,
                  context_projection_saved_characters=usage.get('context_projection_saved_characters',0)+(projection or {}).get('saved_characters',0),
                  opencode_context_helper_sha256=payload.get('context_helper_sha256') if isinstance(payload.get('context_helper_sha256'),str) and len(payload['context_helper_sha256'])==64 else usage.get('opencode_context_helper_sha256'),
                  last_opencode_native_protocol=protocol,
                  usage_incomplete=True, budget_scope='opencode_http_requests_reserved_output_tokens'))
            calls[0] += 1
            return {'ok': True,'max_output_tokens':output_allowance}
        if payload.get('op') != 'step':raise ValueError('不支持的会话操作')
        call_id = payload.get('call_id')
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 160:raise ValueError('无效工具调用标识')
        message_id = payload.get('message_id')
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 160:raise ValueError('无效工具消息标识')
        # Some compatible providers reuse a call ID in another message. A call
        # is unique only inside its durable OpenCode session/message pair.
        result = callback(payload.get('step'), session_id[0] + ':' + message_id + ':' + call_id)
        result = _new_tool_definitions(result,known_tools)
        current = get('turn', turn['id'])
        submitted=payload.get('step') or {}
        mutation=submitted.get('tool') if isinstance(submitted,dict) else None
        durable_call=session_id[0]+':'+message_id+':'+call_id
        successful=any(item.get('opencode_call_id')==durable_call and item.get('tool')==mutation and item.get('status')=='done' for item in current.get('agent_trace',[]))
        if (mutation in ('remember_memory','forget_memory') and successful
                and memory_generation(turn['project_id'])!=starting_memory_generation):
            # Own authorized writes are complete at this receipt boundary. Stop
            # this native history before another request, then let the normal
            # agent scheduler rebuild current context without repeating effects.
            context_rebuild[0]=True;stop.set()
            retired=list(current.get('opencode_retired_sessions',[]))+[session_id[0]]
            patch('turn',turn['id'],opencode_session_id=None,opencode_retired_sessions=retired[-10:],
                  progress='记忆已更新，正在继续处理',opencode_context_rebuild=True)
        terminal = current['state'] in ('done', 'failed') or current['state'] not in ('agent', 'agent_tool')
        if terminal:stop.set()
        return dict(result or {}, state=current['state'], terminal=terminal or context_rebuild[0],
                    context_rebuild_required=context_rebuild[0],
                    feedback=current.get('verification_feedback'))

    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                self.connection.settimeout(5)
                if self.path != '/bridge' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    self.send_error(403);return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 150000:self.send_error(413);return
                payload = json.loads(self.rfile.read(length))
                if payload.get('session_id') != session_id[0]:self.send_error(403);return
                with serial:
                    result = {'error': 'Atlas会话桥已停止'} if closing.is_set() else context.run(dispatch, payload)
                encoded = json.dumps(result, ensure_ascii=False, default=str).encode()
                self.send_response(200);self.send_header('Content-Type', 'application/json');self.end_headers();self.wfile.write(encoded)
            except Exception as exc:
                if isinstance(exc, provider.BudgetExceeded) or ('payload' in locals() and payload.get('op') == 'model_request'):
                    failure.append(exc);stop.set()
                # No arbitrary exception, credential, URL or project content in
                # HTTP errors. Root's tool callback returns safe observations.
                encoded = json.dumps({'error': str(exc) if isinstance(exc, provider.BudgetExceeded) else 'Atlas未执行该操作，请检查参数、权限或会话状态'}, ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type', 'application/json');self.end_headers();self.wfile.write(encoded)
        def log_message(self, *args):pass

    bridge = ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
    bridge.daemon_threads = True
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    metadata = {'atlas_bridge': {'url': f'http://127.0.0.1:{bridge.server_port}/bridge', 'token': token},
                'atlas_critical_constraints': info.get('context_compaction',{}).get('critical_constraints',[]) if isinstance(info.get('context_compaction'),dict) else []}
    content = prompt + '\n\nAtlas task context (untrusted source values inside):\n' + json.dumps(info, ensure_ascii=False, default=str,separators=(',',':'))
    try:
        if session_id[0]:
            _request(value, 'POST', '/api/session/' + session_id[0] + '/interrupt', {})
            _request(value, 'PATCH', '/api/session/' + session_id[0], {'metadata': metadata})
            _request(value, 'POST', '/api/session/' + session_id[0] + '/model', {'model': value['model_ref']})
            native_context=_request(value,'GET','/api/session/'+session_id[0]+'/context')['data']
            # A resumed task appends a fresh, authoritative Atlas context.
            # Count both histories before appending it; considering only the
            # old history lets two individually small contexts exceed the next
            # ordinary request's hard limit. ASCII escaping is conservative.
            if _needs_compaction(native_context,content,turn.get('opencode_compaction_requested'),_input_character_limit({},provider.settings())):
                receipt=compact_session(value,session_id[0],turn['id'],timeout=45 if remaining_ms is None else min(45,max(1,remaining_ms/1000)))
                patch('turn',turn['id'],opencode_compaction_requested=False,opencode_compaction_receipt=receipt)
        else:
            session = _request(value, 'POST', '/api/session', {'title': 'Atlas业务任务', 'model': value['model_ref'],
                               'metadata': metadata, 'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'},
                               {'action': 'atlas_step', 'resource': '*', 'effect': 'allow'}]})['data']
            session_id[0] = session['id']
            patch('turn', turn['id'], opencode_session_id=session_id[0], opencode_version=VERSION)
            patch('turn', turn['id'],opencode_context_rebuild=False)
        _request(value, 'POST', '/api/session/' + session_id[0] + '/prompt', {'text': content, 'resume': True}, timeout=15)
        idle_checks = 0
        while not stop.wait(.15):
            with serial:
                assert_owned()
                current = get('turn', turn['id'])
            if remaining_ms is not None and (time.monotonic() - started) * 1000 >= remaining_ms:
                raise provider.BudgetExceeded('此任务已达到配置的处理时长上限，已有成果和步骤已保存')
            if current['state'] in ('done', 'failed') or current['state'] not in ('agent', 'agent_tool'):break
            if idle_checks % 5 == 0:
                messages = _request(value, 'GET', '/api/session/' + session_id[0] + '/context')['data']
                # A bridge callback may have stopped/completed the task while
                # the context request was in flight. Preserve its real result.
                if stop.is_set():break
                if messages and messages[-1].get('type') == 'idle':
                    # An unverified free-text finish is a protocol error, never
                    # a business completion or a reason to fabricate receipts.
                    raise provider.ModelOutputError('OpenCode会话已结束，但尚未提交通过验收的结果；已有步骤已保留')
            idle_checks += 1
        if failure:raise failure[0]
        with serial:
            assert_owned()
            current = get('turn', turn['id'])
        return {'session_id': session_id[0], 'version': VERSION, 'integration': 'native_session_tool_loop',
                'model_requests': calls[0], 'state': current['state'],'context_rebuild_required':context_rebuild[0]}
    finally:
        closing.set()
        bridge.shutdown();bridge.server_close();thread.join(timeout=2)
        with serial:
            from .locks import LockLost
            try:assert_owned()
            except LockLost:pass  # A replacement owner may already use this session.
            else:
                if session_id[0]:
                    try:_request(value, 'POST', '/api/session/' + session_id[0] + '/interrupt', {}, timeout=3)
                    except (httpx.HTTPError, ValueError):pass
                current = get('turn', turn['id'])
                usage = current.get('model_usage', {})
                patch('turn', turn['id'], model_usage=dict(usage, duration_ms=usage.get('duration_ms', 0) + round((time.monotonic() - started) * 1000)))


def _compaction_identity(operation_id,messages):
    # Reuse admission only for the same history boundary. A later compaction
    # of this turn must not accept an earlier completed summary as its receipt.
    boundary=[(item.get('id'),item.get('type')) for item in messages]
    return 'msg_'+hashlib.sha256(json.dumps([operation_id,boundary],sort_keys=True).encode()).hexdigest()[:40]


def compact_session(value,session_id,operation_id,timeout=45):
    """Admit idempotently, then verify an actual completed compaction message.

    HTTP 200 only means accepted. The caller must own a live Atlas bridge so
    the compaction request passes exactly the normal model/privacy budget gate.
    """
    before=_request(value,'GET','/api/session/'+session_id+'/context')['data']
    identity=_compaction_identity(operation_id,before)
    _request(value,'POST','/api/session/'+session_id+'/compact',{'id':identity})
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        messages=_request(value,'GET','/api/session/'+session_id+'/context')['data']
        for item in messages:
            if item.get('type')=='compaction' and item.get('id')==identity and item.get('status')=='failed':
                raise ValueError('OpenCode原生压缩失败，原始工作与回执已保留，请继续或缩小本次任务')
            if item.get('type')=='compaction' and item.get('id')==identity and item.get('status')=='completed' and isinstance(item.get('summary'),str) and item['summary'].strip():
                return {'requested':True,'completed':True,'request_id':identity,'summary_message_id':item['id'],
                        'method':'opencode_summary','model_pinned':True,'originals_retained':True,
                        'history_messages_before':len(before),'context_messages_after':len(messages)}
        time.sleep(.15)
    raise ValueError('OpenCode已受理压缩，但未获得完成回执；原任务保留，未把受理当作完成')
