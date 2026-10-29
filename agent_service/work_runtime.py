"""Work mode's execution-plane bridge. No generated code runs on API hosts."""
import base64
import hashlib
import json
import ipaddress
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from . import provider, workspace
from .actor_context import current_actor, actor_scope
from .config import DATA
from .models import StrictModel
from .store import all_items, get, now, patch, put, seal, unseal
from work_runner.protocol import Command, InputFile, MAX_FILE, MAX_WORKSPACE, TERMINAL, digest, file_path


class Empty(StrictModel):
    pass


class Environment(StrictModel):
    artifact_id: str | None = Field(default=None,pattern=r'^(?:wa_[a-f0-9]{44}|waf_[a-f0-9]{48})$')


class RunCommand(StrictModel):
    command: str = Field(min_length=1, max_length=32000)
    project_files: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=120, ge=1, le=900)


class Execution(StrictModel):
    execution_id: str = Field(pattern=r'^[a-f0-9]{64}$')


class WorkFile(Execution):
    path: str = Field(min_length=1, max_length=240)


class ReadFile(WorkFile):
    offset: int = Field(default=0, ge=0, le=MAX_FILE)
    limit: int = Field(default=12000, ge=1, le=20000)


DEFINITIONS = {
    'work_environment_status': (Environment, False, '检查工作环境与真实执行记录；可传选中工作成果的artifact_id，定位完整execution_id、文件path及输入版本，供继续读取和修改。不返回连接密钥'),
    'run_work_command': (RunCommand, True, '在独立gVisor环境执行Bash/Python/Node，后台运行、持久化日志与文件。project_files显式复制项目文本，source_ids只复制本轮附件原件到inputs/<source_id>/<文件名>。无网络、无应用凭证；已有检查点自动恢复。返回后后台等待完成，不要重复启动。适合通用编程、图表和文件处理；财经正式数据仍走受验证数据工作流'),
    'list_work_files': (Execution, False, '读取该次隔离执行的不可变文件清单，包含路径、大小、sha256。执行成功不代表业务内容正确'),
    'read_work_file': (ReadFile, False, '按字符分页读取隔离执行检查点中的UTF-8文件；二进制文件请publish_work_file交付，不把日志当作完整数据'),
    'publish_work_file': (WorkFile, True, '将指定执行检查点的文件核对sha256后保存为可预览下载成果；保留原版本，返回真实下载链接。生成文件需此步才能交付；不会自动批准财务口径或正式发布数据'),
}


class RunnerUnavailable(ValueError):
    """Temporary transport/backpressure failure; retry the same execution ID."""


def status():
    configured = bool(os.getenv('ATLAS_WORK_RUNNER_URL') and len(os.getenv('ATLAS_WORK_RUNNER_TOKEN', '')) >= 32)
    return {'configured': configured, 'execution': 'remote_gvisor', 'network': 'none',
            'persistent_checkpoints': True, 'deployment_verified': False,
            'max_command_seconds': 900, 'max_file_bytes': MAX_FILE,
            'browser': 'offline_chromium_in_sandbox_or_authorized_mcp',
            'note': '隔离服务需独立部署；配置状态不等于真实环境验收'}


def _request(method, path, *, payload=None, params=None):
    if not status()['configured']:
        raise ValueError('尚未配置云端工作执行服务；请由管理员按 WORK-MODE.md 部署，应用主机不会执行生成代码')
    raw = os.environ['ATLAS_WORK_RUNNER_URL'].rstrip('/')
    parsed = urlsplit(raw)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or (parsed.scheme != 'https' and os.getenv('ATLAS_WORK_RUNNER_ALLOW_HTTP') != '1')):
        raise ValueError('工作执行服务须为无凭证HTTPS地址；专用私网HTTP需要管理员显式启用')
    from .connectors import safe_api_url
    original = httpx.URL(raw + '/v1/' + path)
    target = original.copy_with(host=safe_api_url(str(original)))
    if parsed.scheme == 'http' and not ipaddress.ip_address(target.host).is_private:
        raise ValueError('HTTP工作执行服务仅可用于管理员批准的私网地址')
    with httpx.Client(timeout=httpx.Timeout(12, connect=5), trust_env=False, follow_redirects=False) as client:
        with client.stream(method, target, headers={
            'Authorization': 'Bearer ' + os.environ['ATLAS_WORK_RUNNER_TOKEN'], 'Host': original.netloc.decode(),
        }, json=payload, params=params, extensions={'sni_hostname': original.host}) as response:
            if response.status_code == 404:
                raise KeyError('执行记录或文件不存在')
            if response.status_code == 429 or response.status_code >= 500:
                raise RunnerUnavailable('工作执行服务暂不可用，保留原任务编号等待恢复')
            if response.status_code >= 300:
                raise ValueError('工作执行服务拒绝请求，请检查接口、执行参数和访问权限')
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 36 * 1024 * 1024:
                    raise ValueError('工作执行服务响应超过预算')
            return json.loads(data)


def _scope(turn):
    from .workspace_lifecycle import assert_turn_live
    assert_turn_live(turn)
    if turn.get('actor_id', 'owner') != current_actor():
        raise ValueError('工作环境不属于当前账号')
    from .company_search import assert_model_allowed
    assert_model_allowed(turn['project_id'])
    if not provider.settings().get('share_data'):
        raise ValueError('读取或发送工作环境内容需要管理员启用当前模型的项目内容读取')
    return digest(('atlas-work-v1\0' + current_actor() + '\0' + turn['project_id']).encode())


def _owned(pid, eid):
    value = get('work_execution', eid)
    if value['project_id'] != pid or value['actor_id'] != current_actor():
        raise KeyError(eid)
    from .workspace_lifecycle import assert_turn_live
    assert_turn_live(get('turn', value['turn_id']))
    return value


def _public(value):
    return {key: item for key, item in value.items() if key not in ('secret_request', 'actor_id', 'workspace_id')}


def _receipt(value, result):
    if (result.get('execution_id') != value['id'] or result.get('workspace_id') != value['workspace_id']
            or result.get('request_sha256') != value['request_sha256']
            or result.get('state') not in TERMINAL | {'queued', 'preparing', 'starting', 'running'}):
        raise ValueError('工作执行回执与当前任务不匹配')
    return result


def _input(path, content):
    return InputFile(path=file_path(path), content_base64=base64.b64encode(content).decode(), sha256=digest(content))


def start(turn, arguments):
    from .intents import require
    require(turn, 'run_work_command')
    wid = _scope(turn)
    trace = get('turn', turn['id']).get('agent_trace', [])
    if not trace or trace[-1].get('status') != 'started':
        raise ValueError('缺少已持久化的执行步骤')
    eid = digest((turn.get('agent_origin', turn['id']) + ':' + trace[-1]['id']).encode())
    try:
        value = _owned(turn['project_id'], eid)
    except KeyError:
        inputs = [_input(path, workspace.read_file(turn['project_id'], path).encode()) for path in arguments.project_files]
        input_metadata=[dict(kind='project',path=f.path,sha256=f.sha256) for f in inputs]
        permitted = set(turn.get('source_ids', [])) | {turn.get('source_id')}
        from .conversation import validate_attachment_versions
        validate_attachment_versions(turn)
        from .connectors import verified_file_source
        for sid in arguments.source_ids:
            source = get('source', sid)
            if sid not in permitted or source.get('project_id') != turn['project_id'] or source.get('archived') or source['kind'] != 'file':
                raise ValueError('只能向工作环境发送本轮明确附加的项目文件来源')
            _, content, _ = verified_file_source(source)
            if not source.get('content_hash') or digest(content) != source['content_hash']:
                raise ValueError('来源原件与登记摘要不一致，未发送')
            name = re.sub(r'[^\w.\-\u4e00-\u9fff]', '_', Path(source['name']).name)[:120]
            entry=_input('inputs/' + sid + '/' + name, content)
            inputs.append(entry)
            input_metadata.append(dict(kind='source',source_id=sid,revision=source.get('revision'),path=entry.path,sha256=entry.sha256))
        command = Command(execution_id=eid, workspace_id=wid, command=arguments.command,
                          files=inputs, timeout_seconds=arguments.timeout_seconds)
        value = put('work_execution', {'project_id': turn['project_id'], 'turn_id': turn['id'],
            'session_id': turn['session_id'], 'actor_id': current_actor(), 'workspace_id': wid,
            'state': 'submitting', 'created_at': now(), 'request_sha256': command.sha256(),
            'secret_request': seal(command.model_dump()), 'input_files': input_metadata,
            'stdout': '', 'stderr': '', 'cancel_requested': False}, eid)
    # Persist waiting before remote I/O. A lost HTTP response is recovered with
    # the same request ID, rather than producing a second command.
    patch('turn', turn['id'], state='waiting_work_command', work_execution_id=eid,
          model_retry_at=time.time()+1,
          progress='正在云端执行，关闭页面后后台仍会继续')
    try:
        if value.get('secret_request'):
            result=_request('POST','executions',payload=unseal(value['secret_request']))
        else:
            result=_request('GET','executions/'+eid,params={'workspace_id':wid})
        result = _receipt(value,result)
        patch('work_execution', eid, secret_request=None, **{key: result[key] for key in ('state', 'stdout', 'stderr') if key in result})
    except (httpx.HTTPError, RunnerUnavailable, KeyError):
        patch('work_execution', eid, connection_notice='正在恢复执行连接；不会重复创建同一步任务')
    except ValueError:
        patch('work_execution', eid, state='unknown', cancel_requested=True,
              error='执行服务配置或回执未通过验证；不得自动重放')
        raise
    return None


def refresh(value):
    try:
        result = _request('GET', 'executions/' + value['id'], params={'workspace_id': value['workspace_id']})
    except KeyError:
        if value.get('cancel_requested'):
            return patch('work_execution', value['id'], state='cancelled')
        if not value.get('secret_request'):
            raise ValueError('已确认的远端执行记录已丢失，未重新创建命令')
        result = _request('POST', 'executions', payload=unseal(value['secret_request']))
    result = _receipt(value, result)
    fields = {key: result[key] for key in ('state', 'exit_code', 'stdout', 'stderr', 'output_files',
        'snapshot_complete', 'snapshot_sha256', 'error') if key in result}
    return patch('work_execution', value['id'], **fields, secret_request=None,checked_at=now(), connection_notice=None)


def resume(turn):
    if turn['state'] != 'waiting_work_command':
        return False
    value = _owned(turn['project_id'], turn['work_execution_id'])
    if time.time() < value.get('next_poll_at', 0):
        patch('turn',turn['id'],model_retry_at=value['next_poll_at'])
        return True
    try:
        with provider.task_scope(turn['id']):
            _scope(turn)
    except ValueError:
        cancel_turn(turn)
        from .agent import observe
        observe(turn, {'executed': False, 'error': '工作环境访问授权已变化，已请求停止执行；请核对权限后继续'}, True)
        return True
    try:
        value = refresh(value)
    except (httpx.HTTPError, RunnerUnavailable, KeyError):
        patch('work_execution', value['id'], next_poll_at=time.time() + 5,
              connection_notice='执行连接中断；保留原任务编号等待恢复')
        patch('turn',turn['id'],model_retry_at=time.time()+5)
        return True
    except ValueError:
        cancel_turn(turn)
        patch('work_execution',value['id'],state='unknown',error='执行服务权限或回执发生变化，需要核对原任务')
        from .agent import observe
        observe(turn,{'executed':False,'error':'执行服务权限或回执未通过校验，未自动重放，请核对原任务'},True)
        return True
    patch('work_execution', value['id'], next_poll_at=time.time() + 1)
    if value['state'] not in TERMINAL:
        patch('turn',turn['id'],model_retry_at=time.time()+1)
        return True
    patch('turn',turn['id'],model_retry_at=0)
    from .agent import observe
    result = _public(value)
    result.update(execution_id=value['id'], remote_receipt=True,
                  log_scope='最多每路64000字节输出片段；全量结果请读取工作文件',
                  next_step='检查退出码、日志及文件，按需read_work_file并publish_work_file交付；失败时修正后再运行')
    # A nonzero program exit is a completed tool call containing a failed
    # program result. This lets the agent repair code and run a new command.
    # Completion validation below still requires a later successful result.
    observe(turn, result)
    return True


def file_bytes(turn, eid, name):
    _scope(turn)
    value = _owned(turn['project_id'], eid)
    if not value.get('snapshot_complete'):
        raise ValueError('执行尚无完整文件检查点')
    name = file_path(name)
    expected = next((f for f in value.get('output_files', []) if f['path'] == name), None)
    if expected is None:
        raise KeyError(name)
    raw = _request('GET', 'executions/' + eid + '/file', params={'workspace_id': value['workspace_id'], 'path': name})
    content = base64.b64decode(raw['content_base64'], validate=True)
    if (raw.get('execution_id') != eid or raw.get('workspace_id') != value['workspace_id']
            or raw.get('path') != name or len(content) > MAX_FILE or len(content) != expected['bytes']
            or digest(content) != expected['sha256'] or raw.get('sha256') != expected['sha256']):
        raise ValueError('工作文件摘要、路径或执行版本不匹配')
    return content, expected


def _artifact_path(checksum,name):
    suffix=Path(name).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):suffix='.bin'
    return DATA/'work-artifacts'/(checksum+suffix)


def artifact(pid, aid):
    value = get('work_artifact', aid)
    if value['project_id'] != pid or value['actor_id'] != current_actor():
        raise KeyError(aid)
    _owned(pid, value['execution_id'])
    path = _artifact_path(value['sha256'],value['name'])
    from .platform_files import open_regular_read
    with os.fdopen(open_regular_read(path), 'rb') as stream:
        content = stream.read(MAX_FILE + 1)
    if digest(content) != value['sha256'] or len(content) != value['bytes']:
        raise ValueError('成果文件已变化或损坏，未交付')
    return path, value


def inputs_current(value):
    """An old success cannot validate changed project code or source bytes."""
    from .connectors import verified_file_source
    try:
        for entry in value.get('input_files',[]):
            if entry.get('kind')=='source':
                source=get('source',entry['source_id'])
                if source.get('project_id')!=value['project_id'] or source.get('archived') or source.get('revision')!=entry.get('revision'):
                    return False
                _,content,_=verified_file_source(source)
            else:
                content=workspace.read_file(value['project_id'],entry['path']).encode()
            if digest(content)!=entry['sha256']:return False
        return True
    except (KeyError,ValueError,OSError):
        return False


def execute(turn, name, args):
    if name == 'work_environment_status':
        if args.get('artifact_id'):
            _scope(turn)
            identity=args['artifact_id']
            if identity.startswith('wa_'):
                from .workspace_artifacts import _record
                record=_record(turn['project_id'],identity)
                if record['kind']!='work':raise ValueError('此标识不是云端执行成果，请使用对应项目文件或办公成果工具')
                identity=record['private']['work_artifact_id']
            _,saved=artifact(turn['project_id'],identity)
            execution=_owned(turn['project_id'],saved['execution_id'])
            return {**status(),'artifact':{key:saved[key] for key in ('id','name','path','execution_id','sha256','bytes','financial_approval')},
                    'execution_state':execution['state'],'input_files':execution.get('input_files',[]),
                    'inputs_match_current':inputs_current(execution),
                    'next_step':'使用完整execution_id和path读取实际文件；修改时先核对输入版本，保留原成果，不猜测文件内容'}
        return {**status(), 'executions': [{key: v[key] for key in ('id', 'state', 'turn_id', 'created_at')} for v in all_items('work_execution')
                if v['project_id'] == turn['project_id'] and v['actor_id'] == current_actor()][-10:]}
    if name == 'run_work_command':
        return start(turn, RunCommand.model_validate(args))
    _scope(turn)
    value = _owned(turn['project_id'], args['execution_id'])
    if name == 'list_work_files':
        return {'execution_id': value['id'], 'state': value['state'], 'files': value.get('output_files', []),
                'snapshot_complete': value.get('snapshot_complete', False)}
    content, expected = file_bytes(turn, value['id'], args['path'])
    if name == 'read_work_file':
        try:
            text = content.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError('这是二进制文件，请publish_work_file交付后查看或下载') from None
        offset, limit = args.get('offset', 0), args.get('limit', 12000)
        page = text[offset:offset + limit]
        return {**expected, 'content': page, 'offset': offset, 'total_characters': len(text),
                'next_offset': offset + len(page) if offset + len(page) < len(text) else None}
    aid = 'waf_' + digest((turn['project_id'] + '\0' + current_actor() + '\0' + value['id'] + '\0' + expected['path'] + expected['sha256']).encode())[:48]
    from .agent_handoff import _atomic_bytes
    root = DATA / 'work-artifacts'
    root.mkdir(mode=0o700, exist_ok=True)
    target = _artifact_path(expected['sha256'],Path(expected['path']).name)
    _atomic_bytes(target, content)
    extension = Path(expected['path']).suffix.lower().lstrip('.')
    structure_verified = False
    if extension in ('docx', 'pptx', 'xlsx'):
        from .office import _verify_structure
        _verify_structure(target, extension)
        structure_verified = True
    meta = put('work_artifact', {**expected, 'project_id': turn['project_id'], 'actor_id': current_actor(),
        'turn_id': turn['id'], 'execution_id': value['id'], 'name': Path(expected['path']).name,
        'created_at': now(), 'status': 'needs_review', 'structure_verified': structure_verified, 'financial_approval': False}, aid)
    artifact(turn['project_id'], aid)
    url = '/api/projects/' + turn['project_id'] + '/work-artifacts/' + aid
    patch('turn', turn['id'], agent_download={'url': url, 'name': meta['name']})
    return {'artifact_id': aid, 'name': meta['name'], 'sha256': meta['sha256'], 'bytes': meta['bytes'],
            'download_url': url, 'status': 'needs_review', 'financial_approval': False}


def cancel_turn(turn):
    for value in all_items('work_execution'):
        if value['turn_id'] == turn['id'] and value['state'] not in TERMINAL:
            patch('work_execution', value['id'], cancel_requested=True)


def reconcile():
    """Cancellation also works when a conversation/project was deleted."""
    for value in all_items('work_execution'):
        if value['state'] in TERMINAL and not (value.get('cancel_requested') and not value.get('cancel_confirmed')):
            continue
        try:
            turn = get('turn', value['turn_id'])
            project = get('project', value['project_id'])
            if value.get('cancel_requested') or turn.get('outcome') == 'cancelled' or project.get('deleted_at'):
                result = _request('POST', 'executions/' + value['id'] + '/cancel', params={'workspace_id': value['workspace_id']})
                state=_receipt(value,result)['state']
                patch('work_execution', value['id'], state=state, cancel_requested=True,cancel_confirmed=state in TERMINAL)
        except (ValueError, KeyError, httpx.HTTPError):
            continue


def install(app, auth):
    from fastapi import Depends
    from fastapi.responses import FileResponse

    @app.get('/api/projects/{pid}/work-environment', dependencies=[Depends(auth)])
    def environment(pid: str):
        from .workspace_lifecycle import assert_project_live, visible_turn
        assert_project_live(pid)
        executions = [v for v in all_items('work_execution') if v['project_id'] == pid and v['actor_id'] == current_actor()
                      and visible_turn(get('turn', v['turn_id']))]
        return {**status(), 'executions': [_public(v) for v in sorted(executions, key=lambda v: v['created_at'], reverse=True)[:30]]}

    @app.get('/api/projects/{pid}/work-artifacts/{aid}', dependencies=[Depends(auth)])
    def download(pid: str, aid: str):
        path, value = artifact(pid, aid)
        return FileResponse(path, filename=value['name'], media_type='application/octet-stream', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', "Content-Security-Policy": "default-src 'none'; sandbox"})
