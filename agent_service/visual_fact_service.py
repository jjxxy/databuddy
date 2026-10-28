"""Human-reviewed transcription of a specific image or PDF page.

This manual submission does not execute OCR or independently verify pixels.
Whether the user consulted OCR before submitting is unknown. The original,
revision and digest are checked again before explicit visual review.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import select, update

from . import workspace
from .evidence_tables import ExtractedFact, validate_facts
from .fact_service import MAX_FILE_BYTES, _blockers, _source, _period
from .models import StrictModel
from .semantic_alignment import canonical_sha256
from .store import all_items, get, items, now, put, seal, transaction, unseal
from .config import DATA

KIND = 'visual_fact_proposal'
MAX_FACTS = 200


class VisualFact(StrictModel):
    value_as_printed: str = Field(min_length=1, max_length=100)
    measure: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=40)
    period: str = Field(min_length=1, max_length=100)
    dimensions: dict[Annotated[str, Field(min_length=1, max_length=100)],
                     Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=dict, max_length=20)


class VisualProposal(StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    page: int = Field(ge=1, le=2000, strict=True)
    quote: str = Field(min_length=1, max_length=3000)
    facts: list[VisualFact] = Field(min_length=1, max_length=MAX_FACTS)


class OCRInput(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    page: int = Field(default=1, strict=True, ge=1, le=500)


class VisualReview(StrictModel):
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approved', 'rejected']
    confirmed_visual_review: bool = Field(strict=True)


def _original(pid, sid, page):
    source, path = _source(pid, sid)
    if type(source.get('revision', 1)) is not int or source.get('revision', 1) < 1:
        raise ValueError('来源版本无效，请重新导入')
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('文档超过25MB读取预算')
    digest = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r'[a-f0-9]{64}', str(source.get('content_hash', ''))) or source['content_hash'] != digest:
        raise ValueError('来源原件与登记摘要不一致，请重新导入并核对')
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError('加密 PDF 需要可读取的授权原件')
            pages = len(reader.pages)
            if pages > 2000 or page > pages:
                raise ValueError('PDF 页码超出可核对范围')
        except ValueError:
            raise
        except Exception:
            raise ValueError('PDF 文件损坏或无法确认页码') from None
    elif suffix in ('.png', '.jpg', '.jpeg', '.webp'):
        if page != 1:
            raise ValueError('图片仅支持第 1 页')
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}[suffix]
                if image.format != expected or image.width * image.height > 100_000_000:
                    raise ValueError('图片格式或尺寸不受支持')
                image.verify()
        except ImportError:
            raise ValueError('图片核对组件不可用') from None
        except Exception:
            raise ValueError('图片损坏、格式不符或尺寸超出限制') from None
    else:
        raise ValueError('当前仅支持 PDF、PNG、JPEG 和 WebP 原件的人工转录')
    return source, digest, path


def _ocr_worker_python():
    """Use the independent, bundled interpreter for portable desktop OCR."""
    import os
    import sys
    if os.getenv('ATLAS_DESKTOP_MODE') != '1':
        return sys.executable
    from .offline_ocr import OCRError
    root=Path(__file__).resolve().parent.parent
    target=root/'runtime'/'ocr'/'python.exe'
    try:
        resolved=target.resolve(strict=True)
        if resolved!=target or not resolved.is_file():
            raise OSError('OCR runtime is not the bundled file')
    except OSError:
        raise OCRError('随包 OCR 运行组件不可用，请重新解压完整交付包或人工核对', 'OCR_RUNTIME_UNAVAILABLE') from None
    return str(resolved)


def _bounded_worker(command,request,*,cwd,env,timeout=60,stdout_limit=4*1024*1024,stderr_limit=64*1024):
    """Drain both pipes concurrently; never buffer more than bounded output."""
    import subprocess
    import threading
    import time
    from .offline_ocr import OCRError
    process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
    exceeded=threading.Event();failed=threading.Event();out=bytearray();err=bytearray()
    def drain(stream,target,limit):
        try:
            while True:
                chunk=stream.read(65536)
                if not chunk:break
                if len(target)+len(chunk)>limit:
                    exceeded.set();return
                target.extend(chunk)
        except (OSError,ValueError):failed.set()
    def write_request():
        try:process.stdin.write(request);process.stdin.close()
        except (OSError,ValueError):failed.set()
    readers=[threading.Thread(target=drain,args=(process.stdout,out,stdout_limit),daemon=True),
             threading.Thread(target=drain,args=(process.stderr,err,stderr_limit),daemon=True)]
    writer=threading.Thread(target=write_request,daemon=True)
    for thread in [*readers,writer]:thread.start()
    deadline=time.monotonic()+timeout
    try:
        while process.poll() is None:
            if exceeded.is_set():raise OCRError('本地识别输出超过限制，未返回结果','OCR_OUTPUT_BUDGET_EXCEEDED')
            if time.monotonic()>=deadline:raise subprocess.TimeoutExpired(command,timeout)
            time.sleep(.02)
        for thread in [*readers,writer]:
            thread.join(timeout=max(0,min(2.0,deadline-time.monotonic())))
        if any(thread.is_alive() for thread in [*readers,writer]):raise subprocess.TimeoutExpired(command,timeout)
        if exceeded.is_set():raise OCRError('本地识别输出超过限制，未返回结果','OCR_OUTPUT_BUDGET_EXCEEDED')
        if failed.is_set():raise OCRError('本地识别通信未完成','OCR_INFERENCE_FAILED')
        return subprocess.CompletedProcess(command,process.returncode,stdout=bytes(out),stderr=bytes(err))
    finally:
        if process.poll() is None:
            process.kill()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:pass


def _run_ocr_worker(request):
    """Minimize inherited secrets; this is not an operating-system sandbox."""
    import os
    import shutil
    import subprocess
    import tempfile
    root=Path(tempfile.gettempdir()).resolve(strict=True)
    scratch=Path(tempfile.mkdtemp(prefix='atlas-ocr-',dir=str(root)))
    expected=root/scratch.name
    try:
        if scratch!=expected or scratch.resolve(strict=True)!=expected or not scratch.name.startswith('atlas-ocr-'):
            raise OSError('OCR scratch directory is not confined')
        env={'PATH':'','TEMP':str(scratch),'TMP':str(scratch)}
        for name in ('SystemRoot','WINDIR'):
            if os.environ.get(name):env[name]=os.environ[name]
        return _bounded_worker([_ocr_worker_python(), '-E', '-s', '-X', 'utf8', '-m', 'agent_service.offline_ocr'],
            request,cwd=Path(__file__).resolve().parent.parent,env=env)
    finally:
        # Only remove the exact freshly-created directory if it still resolves
        # under the same trusted temp root; never recurse into a moved target.
        try:
            if scratch.parent==root and scratch.name.startswith('atlas-ocr-') and scratch.resolve(strict=True)==expected:
                shutil.rmtree(scratch)
        except OSError:
            pass


def _ocr_original(pid,sid,page):
    """Verify PDF bytes only here; PDF parsing belongs to the bounded worker."""
    source,path=_source(pid,sid)
    if path.suffix.lower()!='.pdf':return _original(pid,sid,page)
    revision=source.get('revision',1)
    if type(revision) is not int or revision<1:raise ValueError('来源版本无效，请重新导入')
    with path.open('rb') as stream:raw=stream.read(MAX_FILE_BYTES+1)
    if len(raw)>MAX_FILE_BYTES:raise ValueError('文档超过25MB读取预算')
    digest=hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r'[a-f0-9]{64}',str(source.get('content_hash',''))) or source['content_hash']!=digest:
        raise ValueError('来源原件与登记摘要不一致，请重新导入并核对')
    return source,digest,path


def recognize_source(pid, sid, value):
    """Identify one verified image or PDF page through a bounded worker.

    The model returns untrusted visual candidates only; this function neither
    creates a proposal nor approves values. Source metadata and bytes are
    checked both before and after inference.
    """
    import subprocess
    from filelock import Timeout
    from .locks import FileLock
    from .offline_ocr import OCRError
    value = OCRInput.model_validate(value)
    source, path = _source(pid, sid)
    if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.pdf'):
        raise OCRError('仅支持 PDF、PNG、JPEG 和 WebP 原件', 'OCR_IMAGE_UNSUPPORTED')
    if path.suffix.lower()!='.pdf' and value.page!=1:
        raise OCRError('图片仅支持第 1 页','OCR_PAGE_UNSUPPORTED')
    if source.get('revision', 1) != value.expected_revision or source.get('content_hash') != value.expected_sha256:
        raise OCRError('资料版本已变化，请重新打开原件', 'OCR_SOURCE_CHANGED')
    try:
        with FileLock(str(DATA / 'visual-ocr-execution.lock'), timeout=0):
            source, digest, path = _ocr_original(pid, sid, value.page)
            if digest != value.expected_sha256 or source.get('revision', 1) != value.expected_revision:
                raise OCRError('资料版本已变化，请重新打开原件', 'OCR_SOURCE_CHANGED')
            request = json.dumps({'path': str(path), 'expected_sha256': digest, 'page': value.page}, ensure_ascii=False).encode('utf-8')
            if len(request) > 8192:
                raise OCRError('资料路径超过识别限制', 'OCR_REQUEST_TOO_LARGE')
            try:
                result = _run_ocr_worker(request)
            except subprocess.TimeoutExpired:
                raise OCRError('本地识别超时，请拆分资料后重试或人工核对', 'OCR_TIMEOUT') from None
            except OSError:
                raise OCRError('本地识别服务不可用，请人工核对原件', 'OCR_RUNTIME_UNAVAILABLE') from None
    except Timeout:
        raise OCRError('已有资料正在识别，请稍后重试', 'OCR_BUSY') from None
    if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
        raise OCRError('本地识别未完成，请对照原图人工处理', 'OCR_INFERENCE_FAILED')
    try:
        response = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(response, dict) or type(response.get('ok')) is not bool:
            raise ValueError('invalid worker result')
        if not response['ok']:
            code = response.get('code')
            messages = {
                'OCR_RUNTIME_UNAVAILABLE': '本地 OCR 组件尚未安装，请人工核对原件',
                'OCR_MODEL_UNAVAILABLE': '本地 OCR 模型不可用，请人工核对原件',
                'OCR_MODEL_MISMATCH': '本地 OCR 模型校验失败，请人工核对原件',
                'OCR_SOURCE_CHANGED': '资料原件已变化，请重新打开原件',
                'OCR_SOURCE_UNAVAILABLE': '资料原件不可读取，请重新打开原件',
                'OCR_IMAGE_UNSUPPORTED': '图片格式不受支持，请人工核对原件',
                'OCR_IMAGE_INVALID': '图片损坏或格式不受支持',
                'OCR_PDF_INVALID': 'PDF 文件损坏、加密或无法渲染，请人工核对',
                'OCR_PAGE_UNSUPPORTED': '页码超出支持范围，PDF 最多 500 页',
                'OCR_IMAGE_BUDGET_EXCEEDED': '页面或图片过大，请拆分后重试',
                'OCR_OUTPUT_BUDGET_EXCEEDED': '图片内容过多，请拆分后重试',
            }
            raise OCRError(messages.get(code, '本地识别未完成，请对照原图人工处理'), code or 'OCR_INFERENCE_FAILED')
        output = response['result']
        if (not isinstance(output, dict) or output.get('source_file_sha256') != digest or
                output.get('page') != value.page or output.get('human_review_required') is not True or
                output.get('financial_facts_verified') is not False or output.get('complete') is not False):
            raise ValueError('invalid OCR evidence')
        if path.suffix.lower()=='.pdf':
            if (output.get('coordinate_space')!='rendered_pdf_page_pixels_not_pdf_coordinates'
                    or output.get('render_scale')!=2.0 or type(output.get('pdf_rotation_degrees')) is not int
                    or type(output.get('pdf_page_count')) is not int or not value.page<=output['pdf_page_count']<=500
                    or not re.fullmatch(r'[a-f0-9]{64}',str(output.get('rendered_image_sha256','')))
                    or type(output.get('image_width')) is not int or type(output.get('image_height')) is not int
                    or not 1<=output['image_width']<=6000 or not 1<=output['image_height']<=6000
                    or output['image_width']*output['image_height']>16_000_000):
                raise ValueError('invalid PDF OCR evidence')
    except OCRError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise OCRError('本地识别未完成，请对照原图人工处理', 'OCR_INFERENCE_FAILED') from None
    current, current_digest, current_path = _ocr_original(pid, sid, value.page)
    if (current.get('revision', 1) != value.expected_revision or current_digest != digest or current_path != path):
        raise OCRError('识别期间资料已变化，请重新打开原件', 'OCR_SOURCE_CHANGED')
    return dict(output, source_id=sid, source_revision=value.expected_revision)


def _public(record, payload):
    return {**{k: v for k, v in record.items() if k != 'secret_payload'},
            'quote': payload['quote'], 'facts': payload['facts'],
            'source_revision': payload['source_revision'],
            'source_file_sha256': payload['source_file_sha256'],
            'ocr_executed': False, 'ocr_scope': 'manual_transcription_submission_only; upstream_unknown',
            'upstream_ocr_known': False, 'pixels_independently_verified': False}


def _owned(pid, ident):
    get('project', pid)
    record = get(KIND, ident)
    if record.get('project_id') != pid:
        raise ValueError('人工转录记录不属于当前项目')
    payload = unseal(record['secret_payload'])
    if canonical_sha256(payload) != record['content_sha256']:
        raise ValueError('人工转录记录完整性校验失败')
    return record, payload


def propose(pid, value):
    value = VisualProposal.model_validate(value)
    source, file_hash, _ = _original(pid, value.source_id, value.page)
    quote = value.quote
    if not quote.strip():
        raise ValueError('人工转录内容不能为空')
    facts = [ExtractedFact.model_validate({**item.model_dump(), 'quote': quote,
             'start': 0, 'end': len(quote)}).model_dump() for item in value.facts]
    facts = validate_facts(quote, facts, source_id=value.source_id)
    payload = {'source_id': value.source_id, 'source_revision': source.get('revision', 1),
               'source_file_sha256': file_hash, 'page': value.page, 'quote': quote, 'facts': facts}
    digest = canonical_sha256(payload)
    ident = 'visual_' + canonical_sha256([pid, digest])[:40]
    with workspace.lock(pid):
        try:
            existing, stored = _owned(pid, ident)
            return _public(existing, stored)
        except KeyError:
            pass
        record = {'project_id': pid, 'source_id': value.source_id, 'page': value.page,
                  'status': 'pending', 'content_sha256': digest, 'secret_payload': seal(payload),
                  'fact_count': len(facts), 'approval_blockers': _blockers(facts),
                  'output_source_id': None, 'created_at': now(), 'review': None,
                  'scope': 'human_transcription_unverified_until_explicit_visual_review; upstream_ocr_unknown'}
        return _public(put(KIND, record, ident), payload)


def detail(pid, ident):
    return _public(*_owned(pid, ident))


def list_proposals(pid, offset=0, limit=20):
    get('project', pid)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('无效的人工转录分页参数')
    records = [record for record in all_items(KIND) if record.get('project_id') == pid]
    records.sort(key=lambda record: (record['created_at'], record['id']), reverse=True)
    return {'items': [{k: v for k, v in record.items() if k not in ('secret_payload', 'review')}
                      for record in records[offset:offset + limit]],
            'total': len(records), 'next_offset': offset + limit if offset + limit < len(records) else None}


def _prepare_source(pid, record, payload):
    source_id = 'vfacts_' + canonical_sha256([pid, record['id']])[:40]
    dims = sorted({key for fact in payload['facts'] for key in fact['dimensions']})
    rows = []
    for index, fact in enumerate(payload['facts']):
        row = {'fact_id': canonical_sha256([record['id'], index]),
               'measure': fact['measure'], 'value': fact['normalized_value'],
               'value_as_printed': fact['value_as_printed'], 'unit': fact['unit'],
               'period': _period(fact['period']), 'period_as_printed': fact['period'],
               'dimensions': fact['dimensions'], 'source_id': payload['source_id'],
               'source_locator': 'page:' + str(payload['page']),
               'source_file_sha256': payload['source_file_sha256'],
               'visual_fact_review_id': record['id'],
               'evidence_scope': 'manual_transcription_visual_review; upstream_ocr_unknown'}
        row.update({'dim:' + name: fact['dimensions'].get(name) for name in dims})
        rows.append(row)
    raw = json.dumps(rows, ensure_ascii=False, indent=2).encode('utf-8')
    uploads = DATA / 'uploads'; uploads.mkdir(parents=True, exist_ok=True)
    name = source_id + '.json'; path = uploads / name
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('人工转录结果文件与本次核对记录不一致')
    else:
        with path.open('xb') as stream:
            path.chmod(0o600); stream.write(raw); stream.flush()
    digest = hashlib.sha256(raw).hexdigest()
    return {'id': source_id, 'project_id': pid, 'name': '人工核对数据-' + record['id'][-12:] + '.json',
            'kind': 'file', 'secret_config': seal({'stored_name': name}), 'created_at': now(),
            'status': 'ready', 'content_hash': digest, 'bytes': len(raw), 'revision': 1,
            'revision_id': 'svf_' + canonical_sha256([source_id, digest])[:40],
            'origin_source_id': payload['source_id'], 'visual_fact_review_id': record['id'],
            'provenance_scope': 'manual_transcription_visual_review; upstream_ocr_unknown',
            'requires_semantic_review': True}


def review(pid, ident, value, actor):
    value = VisualReview.model_validate(value)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 2000:
        raise ValueError('需要已鉴权的核对人')
    if value.decision == 'approved' and not value.confirmed_visual_review:
        raise ValueError('请明确确认已对照原件视觉核对全部转录内容')
    with workspace.lock(pid):
        record, payload = _owned(pid, ident)
        if record['content_sha256'] != value.expected_sha256:
            raise ValueError('人工转录版本已变化，请重新核对')
        if record['status'] != 'pending':
            if record['status'] == value.decision:
                return _public(record, payload)
            raise ValueError('该转录已有不可变核对记录，请创建新提案')
        result = None
        if value.decision == 'approved':
            source, digest, original_path = _original(pid, record['source_id'], payload['page'])
            if source.get('revision', 1) != payload['source_revision'] or digest != payload['source_file_sha256']:
                raise ValueError('来源版本或原件已变化，请重新核对')
            missing = _blockers(payload['facts'])
            if missing:
                raise ValueError('转录事实缺少明确口径、单位、期间或维度，请补充并重新核对')
            result = _prepare_source(pid, record, payload)
        with transaction() as connection:
            locked = connection.execute(select(items.c.body).where(items.c.id == ident, items.c.kind == KIND).with_for_update()).first()
            current = json.loads(locked[0]) if locked else {}
            if current.get('content_sha256') != value.expected_sha256 or current.get('status') != 'pending':
                raise ValueError('转录核对状态已变化，请重新读取')
            if result:
                original = connection.execute(select(items.c.body).where(items.c.id == record['source_id'], items.c.kind == 'source').with_for_update()).first()
                original = json.loads(original[0]) if original else {}
                if (original.get('project_id') != pid or original.get('archived') or
                    original.get('revision', 1) != payload['source_revision'] or
                    original.get('content_hash') != payload['source_file_sha256']):
                    raise ValueError('来源版本已变化，请重新核对')
                with original_path.open('rb') as stream:
                    latest = stream.read(MAX_FILE_BYTES + 1)
                if len(latest) > MAX_FILE_BYTES or hashlib.sha256(latest).hexdigest() != payload['source_file_sha256']:
                    raise ValueError('来源原件已变化，请重新核对')
                connection.execute(items.insert().values(id=result['id'], kind='source', body=json.dumps(result, ensure_ascii=False)))
                version = dict(result, id=result['revision_id'], source_id=result['id'], recorded_at=now())
                connection.execute(items.insert().values(id=result['revision_id'], kind='source_version', body=json.dumps(version, ensure_ascii=False)))
            record.update(status=value.decision,
                review={'decision': value.decision, 'actor_hash': hashlib.sha256(actor.encode()).hexdigest(),
                        'reviewed_at': now(), 'content_sha256': value.expected_sha256,
                        'visual_review_confirmed': value.decision == 'approved'},
                output_source_id=result['id'] if result else None)
            connection.execute(update(items).where(items.c.id == ident, items.c.kind == KIND).values(body=json.dumps(record, ensure_ascii=False)))
        return _public(record, payload)
