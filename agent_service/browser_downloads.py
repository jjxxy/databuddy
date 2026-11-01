"""Import an explicitly requested browser export into the current project.

The independent browser is the only process with network access. A durable
step-derived identifier binds the approved control and is replayed to that
same live browser session after a lost response. Source bytes never enter Git.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import time
import zipfile
from urllib.parse import urlsplit
from pathlib import Path

from pydantic import Field
from .actor_context import current_actor
from .config import DATA, MAX_UPLOAD
from .locks import FileLock
from .models import StrictModel
from .store import get, now, patch, put, seal, unseal

_LIMIT = min(MAX_UPLOAD, 256 * 1024 * 1024)
_CHUNK = 512 * 1024
_TEMP_QUOTA = 512 * 1024 * 1024
_SUFFIXES = {'.csv', '.xlsx', '.json', '.jsonl', '.pdf', '.docx', '.pptx', '.txt', '.md'}
_EXPORT = re.compile(r'下载|导出|download|export', re.I)


class DownloadIntegrityError(ValueError):
    pass


class DownloadSource(StrictModel):
    session_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    ref: int = Field(ge=1, le=160)


DEFINITIONS = {
    'download_browser_source': (DownloadSource, True,
        '从用户明确要求获取资料的已授权浏览器页面下载当前可见的下载/导出控件，将真实文件保存为项目资料。需先观察页面；文件大小受上传配置和浏览器临时空间限制。下载不等于数据已经核验，结果未确认时不得换编号重试。'),
}


def _metadata(result, expected_id):
    if not isinstance(result, dict) or result.get('state') == 'unknown':
        raise ValueError('下载结果尚未确认；未重复点击，请核对浏览器和业务系统')
    name=result.get('filename')
    if not isinstance(name,str):raise ValueError('下载文件名无效')
    name=Path(name.replace('\\','/')).name
    name=''.join(c for c in name if ord(c)>=32 and c!='\x7f')[:120]
    suffix=Path(name).suffix.lower()
    if not name or suffix not in _SUFFIXES:raise ValueError('下载文件格式不在允许的资料类型中')
    size=result.get('bytes');digest=result.get('content_sha256')
    if type(size) is not int or not 0<size<=_LIMIT:raise ValueError('下载文件为空或超过上传体积限制')
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('下载文件摘要无效')
    if result.get('download_id')!=expected_id:raise ValueError('下载回执与操作不一致')
    return {'filename':name,'suffix':suffix,'bytes':size,'content_sha256':digest,'download_id':expected_id}


def _hash_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(_CHUNK),b''):digest.update(chunk)
    return digest.hexdigest()


def _validate_file(path,meta):
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=meta['bytes'] or _hash_file(path)!=meta['content_sha256']:
        raise DownloadIntegrityError('下载文件大小或摘要不一致')
    with path.open('rb') as f:prefix=f.read(1024).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    suffix=meta['suffix']
    if suffix in {'.csv','.json','.jsonl','.txt','.md'} and prefix.startswith((b'<!doctype html',b'<html',b'<head',b'<body')):
        raise DownloadIntegrityError('下载结果是网页而不是预期资料，请检查登录和导出结果')
    if suffix=='.pdf' and not prefix.startswith(b'%pdf-'):raise DownloadIntegrityError('下载结果不是有效 PDF 文件')
    if suffix in {'.xlsx','.docx','.pptx'}:
        required={'.xlsx':'xl/workbook.xml','.docx':'word/document.xml','.pptx':'ppt/presentation.xml'}[suffix]
        try:
            with zipfile.ZipFile(path) as archive:
                entries=archive.infolist()
                if len(entries)>20000:raise DownloadIntegrityError('下载的 Office 文件条目过多')
                names={entry.filename for entry in entries}
                if '[Content_Types].xml' not in names or required not in names or sum(x.file_size for x in entries)>200*1024*1024:
                    raise DownloadIntegrityError('下载的 Office 文件结构无效或解压体积过大')
        except zipfile.BadZipFile:raise DownloadIntegrityError('下载的 Office 文件损坏') from None


def _transfer(turn,sid,value,receipt_id,meta):
    """Copy bounded chunks; recover only this exact downloaded file, never click again."""
    from . import browser_tools
    uploads=DATA/'uploads';uploads.mkdir(exist_ok=True)
    partial=uploads/('.browser-download-'+receipt_id+'.part')
    target=uploads/(receipt_id+meta['suffix'])
    with FileLock(str(DATA/'browser-download-quota.lock'),timeout=60):
        # Browser sessions expire after 30 minutes. An abandoned part older
        # than two hours cannot be resumed and must not consume quota forever.
        for item in uploads.glob('.browser-download-*.part'):
            try:
                if item!=partial and not item.is_symlink() and item.is_file() and item.stat().st_mtime < time.time()-7200:
                    item.unlink()
            except FileNotFoundError:pass
        if target.exists():
            _validate_file(target,meta)
            partial.unlink(missing_ok=True)
            return target.name
        if partial.is_symlink():raise ValueError('临时下载文件无效')
        flags=os.O_RDWR|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(partial,flags,0o600)
        try:
            info=os.fstat(fd)
            import stat
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>meta['bytes']:
                raise ValueError('临时下载文件无效或大小不一致')
            used=sum(p.stat().st_size for p in uploads.glob('.browser-download-*.part') if p.is_file() and not p.is_symlink())
            if used + meta['bytes']-info.st_size > _TEMP_QUOTA:
                raise ValueError('下载临时空间不足，请处理已有待恢复文件后重试')
            with os.fdopen(fd,'ab',closefd=False) as out:
                offset=info.st_size
                while offset<meta['bytes']:
                    from .locks import assert_owned
                    from .workspace_lifecycle import assert_turn_live
                    assert_owned()
                    current=get('turn',turn['id']);assert_turn_live(current)
                    if current.get('outcome')=='cancelled':raise ValueError('任务已停止，未继续读取下载文件')
                    browser_tools._scope(current)
                    amount=min(_CHUNK,meta['bytes']-offset)
                    result=browser_tools._request(sid,'download-chunk',{'workspace_id':value['workspace_id'],
                        'download_id':meta['download_id'],'offset':offset,'length':amount})
                    if (not isinstance(result,dict) or result.get('download_id')!=meta['download_id']
                            or result.get('offset')!=offset or result.get('bytes')!=amount):
                        raise ValueError('下载分块回执无效')
                    try:chunk=base64.b64decode(result.get('content_base64',''),validate=True)
                    except (TypeError,ValueError,binascii.Error):raise ValueError('下载分块内容无效') from None
                    if len(chunk)!=amount:raise ValueError('下载分块大小不一致')
                    out.write(chunk);offset+=len(chunk)
                out.flush();os.fsync(out.fileno())
        finally:os.close(fd)
        try:_validate_file(partial,meta)
        except ValueError:
            partial.unlink(missing_ok=True)
            raise
        try:os.link(partial,target)
        except FileExistsError:_validate_file(target,meta)
        partial.unlink(missing_ok=True)
        return target.name


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
            if receipt.get('state')=='invalid':
                raise ValueError('下载文件校验失败且上次操作结果未确认，请用户接管核对')
            if receipt.get('state') == 'saved':
                try: source = get('source', source_id)
                except KeyError: raise ValueError('下载资料记录缺失，请人工核对；未重新下载') from None
                if source.get('archived'):raise ValueError('下载资料已经归档')
                stored_name=unseal(source['secret_config']).get('stored_name')
                existing=DATA/'uploads'/str(stored_name)
                if (not isinstance(stored_name,str) or Path(stored_name).name!=stored_name or
                        existing.is_symlink() or not existing.is_file() or
                        _hash_file(existing)!=source.get('content_hash')):
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
        action_id=hashlib.sha256(receipt_id.encode()).hexdigest()
        if receipt and receipt.get('state')=='transferring' and receipt.get('secret_transfer'):
            # Metadata identifies the already completed export. Only immutable
            # reads of that same session file may be resumed; never re-click.
            if value.get('unknown_pending') and value.get('unknown_pending_owner')!='download:'+receipt_id:
                raise ValueError('另一项页面操作结果未确认，请先接管核对')
            meta=_metadata(unseal(receipt['secret_transfer']),action_id)
        else:
            if value.get('state') != 'open' or value.get('unknown_pending'):
                raise ValueError('上次页面操作结果未确认，请用户接管核对，未自动重试')
            body={'workspace_id':value['workspace_id'],'action_id':action_id,
                  'revision':args['revision'],'ref':args['ref'],'agent_authorized':True}
            patch('browser_session',sid,state='action_unknown',unknown_pending=True,
                  unknown_pending_owner='download:'+receipt_id)
            result=browser_tools._request(sid,'download',body)
            meta=_metadata(result,action_id)
            patch('browser_download',receipt_id,state='transferring',secret_transfer=seal(meta))
        try:stored=_transfer(turn,sid,value,receipt_id,meta)
        except DownloadIntegrityError:
            patch('browser_download',receipt_id,state='invalid')
            raise
        name=meta['filename'];digest=meta['content_sha256']
        try:
            source = get('source', source_id)
            if (source.get('project_id') != pid or source.get('content_hash') != digest or
                    unseal(source['secret_config']).get('stored_name') != stored):
                raise ValueError('已有资料与下载回执不一致')
        except KeyError:
            source = put('source', {'name':name, 'kind':'file', 'project_id':pid,
                'secret_config':seal({'stored_name':stored}), 'content_hash':digest, 'bytes':meta['bytes'],
                'goal':'', 'created_at':now(), 'status':'queued', 'skip_model_planning':True,
                'browser_download_id':receipt_id}, source_id)
        if not source.get('revision'): source = revision(source)
        if source.get('status') not in ('ready','failed'): enqueue('probe', source_id)
        patch('browser_download', receipt_id, state='saved', content_hash=digest, saved_at=now())
        patch('browser_session', sid, state='open', unknown_pending=False,unknown_pending_owner=None)
        try:browser_tools._request(sid,'download-release',{'workspace_id':value['workspace_id'],'download_id':action_id})
        except Exception:pass  # Session close/expiry is the cleanup backstop.
        return _view(source)


def _view(source):
    return {'source_id':source['id'], 'name':source['name'], 'bytes':source['bytes'],
            'content_hash':source['content_hash'], 'revision':source.get('revision'),
            'status':source['status'], 'notice':'已保存下载文件为项目资料；需要等待资料处理并核对期间、字段和数据完整性。'}


def can_resume_started(turn, step):
    """Only resume an already captured download, never replay the export click."""
    if step.get('tool')!='download_browser_source' or step.get('status')!='started':return False
    from .conversation import stable
    args=step.get('arguments')
    if not isinstance(args,dict) or not isinstance(step.get('id'),str):return False
    try:
        parsed=DownloadSource.model_validate(args)
        actor=turn.get('actor_id','owner');pid=turn['project_id'];tid=turn['id']
        rid=stable('bd_',pid,actor,tid,step['id'])
        receipt=get('browser_download',rid)
        fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (receipt.get('project_id')!=pid or receipt.get('actor_id')!=actor or receipt.get('turn_id')!=tid
            or receipt.get('session_id')!=parsed.session_id or receipt.get('fingerprint')!=fingerprint):return False
        if receipt.get('state')=='saved':return True
        if receipt.get('state')!='transferring' or not receipt.get('secret_transfer'):return False
        session=get('browser_session',parsed.session_id)
        if (session.get('project_id')!=pid or session.get('actor_id')!=actor or session.get('mode')!='agent'
            or session.get('expires_at',0)<time.time()*1000
            or session.get('unknown_pending') and session.get('unknown_pending_owner')!='download:'+rid):return False
        _metadata(unseal(receipt['secret_transfer']),hashlib.sha256(rid.encode()).hexdigest())
        return True
    except Exception:return False
