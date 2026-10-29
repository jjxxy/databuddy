"""Opt-in image/audio providers. No provider credentials reach the model or UI."""
import base64
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import Field

from .models import StrictModel
from .actor_context import current_actor
from .config import DATA
from .store import all_items, get, now, patch, put, seal, unseal
from .locks import FileLock


class Empty(StrictModel):
    pass


class Image(StrictModel):
    prompt: str = Field(min_length=2, max_length=12000)
    size: Literal['1024x1024', '1536x1024', '1024x1536'] = '1024x1024'
    quality: Literal['low', 'medium', 'high'] = 'medium'
    transparent: bool = False
    reference_artifact_id: str | None = Field(default=None, pattern=r'^wa_[a-f0-9]{44}$')


class Speech(StrictModel):
    text: str = Field(min_length=1, max_length=4000)


DEFINITIONS = {
    'media_capabilities': (Empty, False, '检查图片生成、图片编辑、语音转写、朗读与实时语音配置，不返回密钥。配置不等于服务验收'),
    'generate_image': (Image, True, '通过管理员配置的真实图像服务生成或编辑PNG，保存为项目成果；编辑须传真实wa_图片成果编号。精确数据图表用代码绘制；此调用可能计费，超时不自动重发'),
    'synthesize_speech': (Speech, True, '将文本通过真实语音模型转换为MP3项目成果。界面标记为AI语音；不仿冒特定个人声音，超时不自动重发'),
}


def status():
    configured = bool(os.getenv('ATLAS_MEDIA_BASE_URL') and os.getenv('ATLAS_MEDIA_API_KEY'))
    return {name: configured and bool(os.getenv(variable)) for name, variable in (
        ('images', 'ATLAS_IMAGE_MODEL'), ('transcription', 'ATLAS_TRANSCRIBE_MODEL'),
        ('speech', 'ATLAS_SPEECH_MODEL'), ('realtime', 'ATLAS_REALTIME_MODEL'))}


def project_scope(pid):
    from .workspace_lifecycle import assert_project_live
    from .company_search import assert_model_allowed
    from .agent_mesh import assert_project_model_allowed
    from . import provider
    assert_project_live(pid); assert_model_allowed(pid); assert_project_model_allowed(pid)
    project=get('project',pid)
    if project.get('company_private') or project.get('work_private') or project.get('mesh_model_policies'):
        raise ValueError('媒体服务是独立目的地，内部或协作受限资料项目暂不启用；请使用独立公开项目')
    if not provider.settings().get('share_data'):
        raise ValueError('需要管理员允许当前模型读取项目内容，才能使用媒体服务')


def _request(path, *, payload=None, data=None, files=None, limit=24 * 1024 * 1024, include_headers=False, missing_ok=False):
    """Fixed administrator endpoint, pinned DNS, TLS/SNI, no redirects or URL downloads."""
    from .connectors import safe_api_url
    raw = os.getenv('ATLAS_MEDIA_BASE_URL', '').rstrip('/')
    parsed = urlsplit(raw)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ('http', 'https')):
        raise ValueError('管理员尚未正确配置媒体服务')
    allowed = {s.strip() for s in os.getenv('API_ALLOWED_HOSTS', '').split(',')}
    if parsed.scheme != 'https' and parsed.hostname not in allowed:
        raise ValueError('媒体服务需要HTTPS或管理员明确允许的本地服务')
    ip = safe_api_url(raw)
    host = '[' + ip + ']' if ':' in ip else ip
    if parsed.port: host += ':' + str(parsed.port)
    pinned = urlunsplit((parsed.scheme, host, parsed.path + '/' + path, '', ''))
    headers = {'Host': parsed.netloc, 'Authorization': 'Bearer ' + os.environ['ATLAS_MEDIA_API_KEY']}
    try:
        with httpx.Client(timeout=10 if path.endswith('/hangup') else 180, trust_env=False, follow_redirects=False) as client:
            with client.stream('POST', pinned, headers=headers, json=payload, data=data, files=files,
                               extensions={'sni_hostname': parsed.hostname}) as response:
                if missing_ok and response.status_code in (404,410):return b''
                response.raise_for_status()
                content = bytearray()
                for block in response.iter_bytes():
                    content.extend(block)
                    if len(content) > limit: raise ValueError('媒体响应超过大小限制')
                return (bytes(content),dict(response.headers)) if include_headers else bytes(content)
    except (httpx.HTTPError, OSError):
        raise ValueError('媒体服务未返回可确认的结果；为避免重复计费，不自动重新发起') from None


def _job(pid, kind, request_id):
    identity = 'mj_' + hashlib.sha256((current_actor() + pid + kind + request_id).encode()).hexdigest()[:48]
    try:
        prior = get('media_job', identity)
        if prior['state'] == 'succeeded': return prior, True
        raise ValueError('此媒体请求结果待核对，不自动重复计费；请查看记录后明确发起新请求')
    except KeyError: pass
    with FileLock(str(DATA / 'media-admission.lock'), timeout=10):
        recent = [v for v in all_items('media_job') if v['actor_id'] == current_actor()
                  and v['created_at'][:10] == now()[:10]]
        cap = max(1, min(1000, int(os.getenv('ATLAS_MEDIA_DAILY_REQUESTS', '40'))))
        if len(recent) >= cap: raise ValueError('当前账号已达到媒体服务每日请求上限')
        value = put('media_job', {'actor_id': current_actor(), 'project_id': pid, 'kind': kind,
                                 'state': 'pending', 'created_at': now()}, identity)
    return value, False


def perform(pid, kind, request_id, operation):
    # Cross-process lock plus pending receipt before any billable request.
    key = hashlib.sha256((current_actor() + pid + kind + request_id).encode()).hexdigest()
    with FileLock(str(DATA / ('media-' + key + '.lock')), timeout=2):
        project_scope(pid)
        job, cached = _job(pid, kind, request_id)
        if cached: return unseal(job['result_secret'])
        try:
            result = operation()
            project_scope(pid)
            patch('media_job', job['id'], state='succeeded', result_secret=seal(result), finished_at=now())
            return result
        except Exception:
            patch('media_job', job['id'], state='unknown', finished_at=now())
            raise


def save(pid, content, name, *, turn_id=None, provenance='ai_generated'):
    from .workspace_artifacts import _register, _root, _public
    from .agent_handoff import _atomic_bytes
    if not content or len(content) > 16 * 1024 * 1024: raise ValueError('媒体成果大小无效')
    checksum = hashlib.sha256(content).hexdigest()
    target = _root() / (checksum + Path(name).suffix)
    with FileLock(str(DATA / 'media-storage.lock'), timeout=10):
        used = sum(v.get('bytes', 0) for v in all_items('media_artifact') if v['actor_id'] == current_actor())
        if used + len(content) > int(os.getenv('ATLAS_MEDIA_STORAGE_MB', '256')) * 1024 * 1024:
            raise ValueError('媒体成果已达到账号存储上限')
        _atomic_bytes(target, content)
        identity = 'ma_' + hashlib.sha256((current_actor() + pid + checksum + name + (turn_id or '')).encode()).hexdigest()[:48]
        value = put('media_artifact', {'project_id': pid, 'actor_id': current_actor(), 'name': name,
            'sha256': checksum, 'bytes': len(content), 'turn_id': turn_id, 'created_at': now(),
            'provenance': provenance, 'snapshot': str(target.relative_to(DATA))}, identity)
        record = _register(pid, 'media', identity, name, checksum, bytes=len(content), origin=turn_id,
            status='needs_review', private={'media_id': identity}, semantic_status='not_applicable', metric_status='not_applicable')
    return dict(_public(record), artifact_id=record['id'], ai_generated=provenance == 'ai_generated')


def verify_image(content):
    from .work_runtime import image_size
    from PIL import Image as Bitmap
    image_size(content)
    if len(content)>16*1024*1024:raise ValueError('图片超过大小限制')
    try:
        with Bitmap.open(io.BytesIO(content)) as bitmap:
            if bitmap.format not in ('PNG','JPEG'):raise ValueError('图片格式无效')
            bitmap.verify()
        with Bitmap.open(io.BytesIO(content)) as bitmap:bitmap.load()
    except Exception:raise ValueError('图片字节无法完整解码') from None


def artifact(pid, identity):
    from .workspace_lifecycle import assert_project_live, visible_turn
    from .workspace_artifacts import _info
    assert_project_live(pid)
    value = get('media_artifact', identity)
    if value['project_id'] != pid or value['actor_id'] != current_actor(): raise KeyError(identity)
    if value.get('turn_id') and not visible_turn(get('turn', value['turn_id'])): raise KeyError(identity)
    path = DATA / value['snapshot']; _info(path, value['sha256'])
    return path, value


def execute(turn, name, args):
    from .work_runtime import _scope
    _scope(turn)
    if name == 'media_capabilities': return status()
    pid = turn['project_id']
    def operation():
        if name == 'generate_image':
            if not status()['images']: raise ValueError('管理员尚未配置图像模型')
            options = Image(**args)
            body = {'model': os.environ['ATLAS_IMAGE_MODEL'], 'prompt': options.prompt, 'n': 1,
                    'size': options.size, 'quality': options.quality, 'output_format': 'png',
                    'background': 'transparent' if options.transparent else 'opaque'}
            if options.reference_artifact_id:
                from .workspace_artifacts import file
                path, _ = file(pid, options.reference_artifact_id)
                content = path.read_bytes(); verify_image(content)
                raw = _request('images/edits', data={k: str(v) for k, v in body.items()},
                               files={'image': (path.name, content, 'image/png' if content.startswith(b'\x89PNG') else 'image/jpeg')})
            else: raw = _request('images/generations', payload=body)
            try:
                encoded = json.loads(raw)['data'][0]['b64_json']
                content = base64.b64decode(encoded, validate=True)
                if not content.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError()
                verify_image(content)
            except (ValueError, KeyError, IndexError, TypeError):
                raise ValueError('图像服务没有返回有效PNG字节，未把远程URL当作成果') from None
            _scope(get('turn', turn['id']))
            if get('turn', turn['id']).get('outcome') == 'cancelled': raise ValueError('任务已停止，未发布图像')
            return save(pid, content, 'generated-image.png', turn_id=turn['id'])
        if not status()['speech']: raise ValueError('管理员尚未配置语音模型')
        body = {'model': os.environ['ATLAS_SPEECH_MODEL'], 'input': Speech(**args).text,
                'voice': os.getenv('ATLAS_SPEECH_VOICE', 'alloy'), 'response_format': 'mp3'}
        content = _request('audio/speech', payload=body, limit=16 * 1024 * 1024)
        if not (content.startswith(b'ID3') or len(content) > 2 and content[0] == 255 and content[1] & 224 == 224):
            raise ValueError('语音服务未返回MP3字节')
        _scope(get('turn', turn['id']))
        if get('turn', turn['id']).get('outcome') == 'cancelled': raise ValueError('任务已停止，未发布语音')
        return save(pid, content, 'ai-speech.mp3', turn_id=turn['id'])
    request_id = turn['id'] + hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
    result = perform(pid, name, request_id, operation)
    patch('turn', turn['id'], agent_download={'url': result['download_url'], 'name': result['name']})
    return result


class VoiceOffer(StrictModel):
    request_id: str = Field(pattern=r'^[a-f0-9-]{24,64}$')
    sdp: str = Field(min_length=10, max_length=64000)


def end_call(value):
    if value['actor_id']!=current_actor():raise KeyError(value['id'])
    with FileLock(str(DATA/('voice-'+value['id']+'.lock')),timeout=1):
        value=get('voice_call',value['id'])
        if value['state']=='closed':return
        patch('voice_call',value['id'],close_requested=True)
        if value['provider_sha256']!=hashlib.sha256(os.getenv('ATLAS_MEDIA_BASE_URL','').rstrip('/').encode()).hexdigest():
            raise ValueError('语音服务地址已变化，请管理员核对旧会话')
        call_id=unseal(value['call_secret'])
        _request('realtime/calls/'+call_id+'/hangup',limit=1024,missing_ok=True)
        patch('voice_call',value['id'],state='closed',closed_at=now())


def reconcile_calls():
    """Server-side deadline survives page closure, API restarts and client edits."""
    from .actor_context import actor_scope
    from filelock import Timeout
    due=[v for v in all_items('voice_call') if v['state']!='closed' and (v.get('close_requested') or time.time()>=v['deadline'])]
    for value in due[:2]:
        try:
            with actor_scope(value['actor_id']):end_call(value)
        except (ValueError,KeyError,Timeout):pass


def install(app, auth):
    from fastapi import Depends, HTTPException, Request
    from starlette.datastructures import UploadFile

    @app.get('/api/projects/{pid}/media', dependencies=[Depends(auth)])
    def capabilities(pid: str):
        from .workspace_lifecycle import assert_project_live
        assert_project_live(pid)
        try:project_scope(pid)
        except ValueError as error:return {**{name:False for name in status()},'notice':str(error)}
        return status()

    @app.post('/api/projects/{pid}/voice/transcribe', dependencies=[Depends(auth)])
    async def transcribe(pid: str, request: Request):
        project_scope(pid)
        if not status()['transcription']: raise ValueError('管理员尚未配置语音转写')
        # Bound the transport before multipart parsing can spool a large file.
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>13*1024*1024:raise HTTPException(413,'录音请求超过大小限制')
        async def receive():return {'type':'http.request','body':bytes(raw),'more_body':False}
        async with Request(request.scope,receive).form(max_files=1,max_fields=1) as form:
            audio=form.get('audio');request_id=form.get('request_id')
            if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9-]{24,64}',request_id):raise HTTPException(422,'请求编号无效')
            if not isinstance(audio,UploadFile):raise HTTPException(422,'请提供录音文件')
            content=await audio.read(12*1024*1024+1)
            if not 1<=len(content)<=12*1024*1024:raise ValueError('录音最多12MiB')
            fmt=Path(audio.filename or '').suffix.lower().lstrip('.')
            if fmt not in ('webm','mp3','mp4','m4a','wav','ogg','flac'):raise ValueError('录音格式不支持')
        def operation():
            raw = _request('audio/transcriptions', data={'model': os.environ['ATLAS_TRANSCRIBE_MODEL']},
                           files={'file': ('recording.' + fmt, content, 'application/octet-stream')}, limit=256 * 1024)
            text = json.loads(raw).get('text')
            if not isinstance(text, str) or len(text) > 12000: raise ValueError('转写结果无效')
            # Raw microphone bytes are not retained. Transcript is owner encrypted.
            return {'text': text}
        import asyncio
        result = await asyncio.to_thread(perform, pid, 'transcribe', request_id, operation)
        return result

    @app.post('/api/projects/{pid}/voice/connect', dependencies=[Depends(auth)])
    def voice_connect(pid: str, value: VoiceOffer):
        if not status()['realtime']: raise ValueError('管理员尚未配置实时语音')
        def operation():
            config = {'type': 'realtime', 'model': os.environ['ATLAS_REALTIME_MODEL'],
                      'instructions': '你是Atlas语音助手。语音回答仅供讨论；执行工作须用户把文字发送到项目对话。不要声称已执行工具或访问项目文件。',
                      'tools': [], 'max_output_tokens': 1024,
                      'audio': {'output': {'voice': os.getenv('ATLAS_SPEECH_VOICE', 'alloy')}}}
            if os.getenv('ATLAS_TRANSCRIBE_MODEL'):
                config['audio']['input'] = {'transcription': {'model': os.environ['ATLAS_TRANSCRIBE_MODEL']}}
            raw,headers = _request('realtime/calls', files={'sdp': (None, value.sdp), 'session': (None, json.dumps(config))}, limit=128 * 1024,include_headers=True)
            call_id=headers.get('location','').rstrip('/').split('/')[-1]
            if not re.fullmatch(r'rtc_[a-zA-Z0-9_-]{1,120}',call_id):raise ValueError('语音服务缺少可管理的call_id，未返回连接；请管理员检查兼容接口')
            cid='vc_'+hashlib.sha256((current_actor()+pid+value.request_id).encode()).hexdigest()[:48]
            put('voice_call',{'project_id':pid,'actor_id':current_actor(),'call_secret':seal(call_id),'state':'open',
                'provider_sha256':hashlib.sha256(os.environ['ATLAS_MEDIA_BASE_URL'].rstrip('/').encode()).hexdigest(),
                'created_at':now(),'deadline':time.time()+300},cid)
            answer = raw.decode()
            if not answer.startswith('v=0'): raise ValueError('实时语音没有返回有效连接描述')
            return {'sdp': answer, 'max_seconds': 300,'call_id':cid}
        return perform(pid, 'realtime', value.request_id, operation)

    @app.post('/api/projects/{pid}/voice/calls/{cid}/end', dependencies=[Depends(auth)])
    def voice_end(pid: str,cid: str):
        value=get('voice_call',cid)
        if value['project_id']!=pid or value['actor_id']!=current_actor():raise HTTPException(404,'语音会话不存在')
        end_call(value)
        return {'closed':True}
