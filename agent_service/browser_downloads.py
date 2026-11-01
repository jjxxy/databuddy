"""Import an explicitly requested browser export into the current project.

The independent browser is the only process with network access. A durable
step-derived identifier binds the approved control and is replayed to that
same live browser session after a lost response. Source bytes never enter Git.
"""
import base64
import binascii
import hashlib
import io
import json
import os
import re
import time
import tempfile
import zipfile
from urllib.parse import urlsplit
from pathlib import Path

from pydantic import Field
from .actor_context import current_actor
from .config import DATA
from .locks import FileLock
from .models import StrictModel
from .store import get, now, patch, put, seal, unseal

_LIMIT = 2 * 1024 * 1024
_SUFFIXES = {'.csv', '.xlsx', '.json', '.jsonl', '.pdf', '.docx', '.pptx', '.txt', '.md'}
_EXPORT = re.compile(r'下载|导出|download|export', re.I)


class DownloadSource(StrictModel):
    session_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    ref: int = Field(ge=1, le=160)


DEFINITIONS = {
    'download_browser_source': (DownloadSource, True,
        '从用户明确要求获取资料的已授权浏览器页面下载当前可见的下载/导出控件，将真实文件保存为项目资料。需先观察页面；仅支持受控小文件。下载不等于数据已经核验，结果未确认时不得换编号重试。'),
}


def _content(result):
    if not isinstance(result, dict) or result.get('state') == 'unknown':
        raise ValueError('下载结果尚未确认；未重复点击，请核对浏览器和业务系统')
    name = result.get('filename')
    if not isinstance(name, str):
        raise ValueError('下载文件名无效')
    name = Path(name.replace('\\', '/')).name
    name = ''.join(c for c in name if ord(c) >= 32 and c not in '\x7f')[:120]
    suffix = Path(name).suffix.lower()
    if not name or suffix not in _SUFFIXES:
        raise ValueError('下载文件格式不在允许的资料类型中')
    try:
        raw = base64.b64decode(result.get('content_base64', ''), validate=True)
    except (TypeError, ValueError, binascii.Error):
        raise ValueError('下载文件内容无效') from None
    if not raw or len(raw) > _LIMIT or type(result.get('bytes')) is not int or result['bytes'] != len(raw):
        raise ValueError('下载文件为空、超过 2 MiB 或大小不一致')
    digest = hashlib.sha256(raw).hexdigest()
    if result.get('content_sha256') != digest:
        raise ValueError('下载文件摘要不一致')
    if suffix in {'.csv', '.json', '.jsonl', '.txt', '.md'}:
        prefix = raw[:1024].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
        if prefix.startswith((b'<!doctype html', b'<html', b'<head', b'<body')):
            raise ValueError('下载结果是网页而不是预期资料，请检查登录和导出结果')
    if suffix == '.pdf' and not raw.startswith(b'%PDF-'):
        raise ValueError('下载结果不是有效 PDF 文件')
    if suffix in {'.xlsx', '.docx', '.pptx'}:
        required = {'.xlsx': 'xl/workbook.xml', '.docx': 'word/document.xml', '.pptx': 'ppt/presentation.xml'}[suffix]
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = set(archive.namelist())
                if '[Content_Types].xml' not in names or required not in names or sum(x.file_size for x in archive.infolist()) > 200*1024*1024:
                    raise ValueError('下载的 Office 文件结构无效或解压体积过大')
        except zipfile.BadZipFile:
            raise ValueError('下载的 Office 文件损坏') from None
    return name, suffix, raw, digest


def execute(turn, name, args):
    if name != 'download_browser_source': raise ValueError('未知浏览器下载工具')
    from . import browser_tools
    from .intents import require
    from .workspace_lifecycle import assert_turn_live
    from .conversation import stable
    from .execution import enqueue
    from .sources import revision
    assert_turn_live(turn)
    if turn.get('actor_id', 'owner') != current_actor(): raise ValueError('浏览器任务不属于当前用户')
    require(turn, 'acquire_report_data')
    browser_tools._scope(turn)
    trace = get('turn', turn['id']).get('agent_trace') or []
    if not trace or trace[-1].get('status') != 'started':
        raise ValueError('下载缺少当前操作回执')
    step_id = trace[-1]['id']
    sid = args['session_id']; pid = turn['project_id']; actor = current_actor()
    receipt_id = stable('bd_', pid, actor, turn['id'], step_id)
    source_id = stable('s_', pid, actor, receipt_id)
    fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with FileLock(str(DATA / ('browser-' + sid + '.lock')), timeout=5):
        value = browser_tools.owned(pid, sid)
        if value.get('mode') != 'agent': raise ValueError('用户正在接管浏览器；请等待用户交还')
        snapshot = unseal(value['snapshot_secret'])
        parsed = urlsplit(snapshot.get('url', ''))
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if value.get('agent_grant_origin') != origin or value.get('agent_grant_expires', 0) < time.time():
            raise ValueError('请先在浏览器面板授权 Agent 操作当前网站')
        try: receipt = get('browser_download', receipt_id)
        except KeyError: receipt = None
        if receipt:
            if (receipt.get('actor_id') != actor or receipt.get('project_id') != pid or
                    receipt.get('session_id') != sid or receipt.get('fingerprint') != fingerprint):
                raise ValueError('同一下载操作不能更换目标')
            if receipt.get('state') == 'saved':
                try: source = get('source', source_id)
                except KeyError: raise ValueError('下载资料记录缺失，请人工核对；未重新下载') from None
                if source.get('archived'):raise ValueError('下载资料已经归档')
                stored_name=unseal(source['secret_config']).get('stored_name')
                existing=DATA/'uploads'/str(stored_name)
                if (not isinstance(stored_name,str) or Path(stored_name).name!=stored_name or
                        existing.is_symlink() or not existing.is_file() or
                        hashlib.sha256(existing.read_bytes()).hexdigest()!=source.get('content_hash')):
                    raise ValueError('已保存的下载文件不可验证，请人工核对；未重新下载')
                return _view(source)
        else:
            if snapshot.get('revision') != args['revision']: raise ValueError('页面已变化，请重新查看后选择下载控件')
            controls = snapshot.get('controls') or []
            if args['ref'] > len(controls): raise ValueError('下载控件不存在')
            control = controls[args['ref']-1]
            if (not control.get('visible') or control.get('disabled') or control.get('sensitive') or
                    not _EXPORT.search(str(control.get('label') or '')+' '+str(control.get('href') or ''))):
                raise ValueError('当前控件未明确标识为下载或导出，请用户接管核对')
            put('browser_download', {'project_id':pid, 'actor_id':actor, 'turn_id':turn['id'],
                'session_id':sid, 'fingerprint':fingerprint, 'source_id':source_id,
                'state':'started', 'created_at':now()}, receipt_id)
        if value.get('state') != 'open' or value.get('unknown_pending'):
            raise ValueError('上次页面操作结果未确认，请用户接管核对，未自动重试')
        # The runner's action-id cache binds this exact request; a successful
        # response can be recovered without another click while the session is
        # alive. An unknown result is never assigned a new identifier.
        body = {'workspace_id':value['workspace_id'],
                'action_id':hashlib.sha256(receipt_id.encode()).hexdigest(),
                'revision':args['revision'], 'ref':args['ref'], 'agent_authorized':True}
        patch('browser_session', sid, state='action_unknown', unknown_pending=True)
        result = browser_tools._request(sid, 'download', body)
        name, suffix, raw, digest = _content(result)
        uploads = DATA/'uploads'; uploads.mkdir(exist_ok=True)
        stored = receipt_id + suffix; path = uploads/stored
        fd, temporary = tempfile.mkstemp(prefix='.browser-download-', dir=uploads)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(raw); f.flush(); os.fsync(f.fileno())
            try: os.link(temporary, path)
            except FileExistsError:
                if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('已保存下载文件与回执不一致，请人工核对')
        finally:
            Path(temporary).unlink(missing_ok=True)
        try:
            source = get('source', source_id)
            if (source.get('project_id') != pid or source.get('content_hash') != digest or
                    unseal(source['secret_config']).get('stored_name') != stored):
                raise ValueError('已有资料与下载回执不一致')
        except KeyError:
            source = put('source', {'name':name, 'kind':'file', 'project_id':pid,
                'secret_config':seal({'stored_name':stored}), 'content_hash':digest, 'bytes':len(raw),
                'goal':'', 'created_at':now(), 'status':'queued', 'skip_model_planning':True,
                'browser_download_id':receipt_id}, source_id)
        if not source.get('revision'): source = revision(source)
        if source.get('status') not in ('ready','failed'): enqueue('probe', source_id)
        patch('browser_download', receipt_id, state='saved', content_hash=digest, saved_at=now())
        patch('browser_session', sid, state='open', unknown_pending=False)
        return _view(source)


def _view(source):
    return {'source_id':source['id'], 'name':source['name'], 'bytes':source['bytes'],
            'content_hash':source['content_hash'], 'revision':source.get('revision'),
            'status':source['status'], 'notice':'已保存下载文件为项目资料；需要等待资料处理并核对期间、字段和数据完整性。'}
