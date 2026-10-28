"""Offline OCR candidates for a single image, never verified financial facts.

RapidOCR 3.10.0 and its three prepackaged models are optional build-time
components. Explicit local paths prevent its downloader being invoked. This
module does not perform source authorization or save results.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import io
import math
from pathlib import Path

MODEL_FILES = {
    'det': ('PP-OCRv6_det_small.onnx', '090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f'),
    'cls': ('ch_ppocr_mobile_v2.0_cls_mobile.onnx', 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c'),
    'rec': ('PP-OCRv6_rec_small.onnx', '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884'),
}
MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000


class OCRError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _models():
    try:
        if importlib.metadata.version('rapidocr') != '3.10.0':
            raise OCRError('本地 OCR 版本未验证，请使用随包提供的组件', 'OCR_RUNTIME_UNAVAILABLE')
        importlib.metadata.version('onnxruntime')
        spec = importlib.util.find_spec('rapidocr')
        if not spec or not spec.origin:
            raise OCRError('本地 OCR 组件不可用', 'OCR_RUNTIME_UNAVAILABLE')
        model_dir = Path(spec.origin).resolve().parent / 'models'
        paths = {}
        for task, (name, expected) in MODEL_FILES.items():
            path = model_dir / name
            if not path.is_file() or path.is_symlink():
                raise OCRError('本地 OCR 模型缺失，未尝试联网下载', 'OCR_MODEL_UNAVAILABLE')
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise OCRError('本地 OCR 模型校验失败，未执行识别', 'OCR_MODEL_MISMATCH')
            paths[task] = str(path)
        return paths
    except importlib.metadata.PackageNotFoundError:
        raise OCRError('本地 OCR 组件不可用，未尝试联网下载', 'OCR_RUNTIME_UNAVAILABLE') from None
    except OSError:
        raise OCRError('本地 OCR 模型不可读取', 'OCR_MODEL_UNAVAILABLE') from None


def recognize_image(path, expected_sha256, *, page=1):
    """Extract line candidates with image coordinates and mandatory human review.

    The caller must independently authorize the source and its stored revision.
    PNG, JPEG and WebP page 1 only; detection gaps and confidence scores do not
    establish completeness, correctness, or a financial approval.
    """
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256):
        raise OCRError('需要已登记原件的 SHA-256 摘要', 'OCR_SOURCE_DIGEST_REQUIRED')
    if type(page) is not int or page != 1:
        raise OCRError('图片仅支持第 1 页；PDF 尚不支持自动 OCR', 'OCR_PAGE_UNSUPPORTED')
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        raise OCRError('原始图片不可读取', 'OCR_SOURCE_UNAVAILABLE') from None
    if len(raw) > MAX_BYTES:
        raise OCRError('图片文件超过读取预算', 'OCR_IMAGE_BUDGET_EXCEEDED')
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise OCRError('图片原件与登记摘要不一致，请重新导入', 'OCR_SOURCE_CHANGED')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ('PNG', 'JPEG', 'WEBP') or getattr(img, 'n_frames', 1) != 1:
                raise OCRError('仅支持单帧 PNG、JPEG 或 WebP 图片', 'OCR_IMAGE_UNSUPPORTED')
            width, height = img.size
            if not width or not height or width * height > MAX_PIXELS:
                raise OCRError('图片像素超过读取预算', 'OCR_IMAGE_BUDGET_EXCEEDED')
            # Keep raw pixel orientation, so coordinates refer to the original
            # image, not an unrecorded EXIF rotation. Supply a decoded image,
            # not a URL or path, to prevent the library fetching remote data.
            image = img.convert('RGB')
            image.load()
    except OCRError:
        raise
    except ImportError:
        raise OCRError('图片读取组件不可用', 'OCR_RUNTIME_UNAVAILABLE') from None
    except Exception:
        raise OCRError('图片损坏或格式不受支持', 'OCR_IMAGE_INVALID') from None
    return _recognize_decoded(image,digest,page=1,coordinate_space='original_image_pixels_without_exif_rotation')


def _recognize_decoded(image,digest,*,page,coordinate_space):
    width,height=image.size
    paths = _models()
    try:
        from rapidocr import RapidOCR
        params = {'Det.model_path': paths['det'], 'Cls.model_path': paths['cls'],
                  'Rec.model_path': paths['rec'], 'Global.text_score': 0.0}
        result = RapidOCR(params=params)(image)
        texts, scores, boxes = result.txts, result.scores, result.boxes
        if texts is None and scores is None and boxes is None:
            texts, scores, boxes = [], [], []
        if texts is None or scores is None or boxes is None or not (len(texts) == len(scores) == len(boxes)):
            raise ValueError('inconsistent OCR output')
        if len(texts) > 1000:
            raise OCRError('识别区域超过处理预算，请拆分图片', 'OCR_OUTPUT_BUDGET_EXCEEDED')
        lines = []
        total_characters = 0
        for text, score, box in zip(texts, scores, boxes):
            if not isinstance(text, str) or len(text) > 3000:
                raise OCRError('识别文本超过处理预算', 'OCR_OUTPUT_BUDGET_EXCEEDED')
            total_characters += len(text)
            if total_characters > 300_000:
                raise OCRError('识别文本总量超过处理预算，请拆分图片', 'OCR_OUTPUT_BUDGET_EXCEEDED')
            confidence = float(score)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError('invalid score')
            points = [[float(point[0]), float(point[1])] for point in box]
            if (len(points) != 4 or any(not math.isfinite(x) or not math.isfinite(y)
                                        or x < 0 or y < 0 or x > width or y > height for x, y in points)):
                raise ValueError('invalid image coordinates')
            lines.append({'text': text, 'box': points, 'confidence': confidence,
                          'human_review_required': True})
    except OCRError:
        raise
    except Exception:
        raise OCRError('本地识别未完成，请对照原图人工处理', 'OCR_INFERENCE_FAILED') from None
    return {'source_file_sha256': digest, 'page': page, 'image_width': width, 'image_height': height,
            'coordinate_space': coordinate_space,
            'engine': 'RapidOCR 3.10.0 / ONNX Runtime CPU',
            'model_sha256': {task: sha for task, (_, sha) in MODEL_FILES.items()},
            'lines': lines, 'complete': False, 'financial_facts_verified': False,
            'human_review_required': True, 'ocr_executed': True,
            'limitations': ['识别可能遗漏或误读，分数不能证明金额准确', '须对照原图逐项核对后才能用于财务处理']}


def recognize_pdf(path,expected_sha256,*,page):
    """Render exactly one bounded PDF page in this time-limited worker."""
    if not isinstance(expected_sha256,str) or len(expected_sha256)!=64 or any(c not in '0123456789abcdef' for c in expected_sha256):
        raise OCRError('需要已登记原件的 SHA-256 摘要','OCR_SOURCE_DIGEST_REQUIRED')
    if type(page) is not int or not 1<=page<=500:
        raise OCRError('PDF 页码超出支持范围','OCR_PAGE_UNSUPPORTED')
    try:
        with Path(path).open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    except OSError:raise OCRError('PDF 原件不可读取','OCR_SOURCE_UNAVAILABLE') from None
    if len(raw)>MAX_BYTES:raise OCRError('PDF 超过文件大小限制','OCR_IMAGE_BUDGET_EXCEEDED')
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=expected_sha256:raise OCRError('PDF 原件已变化，请重新打开','OCR_SOURCE_CHANGED')
    try:
        if importlib.metadata.version('pypdfium2')!='5.14.0':raise OCRError('PDF 渲染组件版本不可用','OCR_RUNTIME_UNAVAILABLE')
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_raw
    except (ImportError,importlib.metadata.PackageNotFoundError):
        raise OCRError('本地 PDF 渲染组件不可用，请人工核对','OCR_RUNTIME_UNAVAILABLE') from None
    document=None;pdf_page=None;bitmap=None
    try:
        document=pdfium.PdfDocument(raw)
        # PDFium accepts an empty user password for some encrypted files. A
        # successful open is therefore not proof that a PDF is unencrypted.
        # Its documented API returns -1 only for an unprotected document.
        if pdfium_raw.FPDF_GetSecurityHandlerRevision(document)!=-1:
            raise OCRError('PDF 已加密，请提供未加密原件或人工核对','OCR_PDF_INVALID')
        count=len(document)
        if count>500 or page>count:raise OCRError('PDF 页数或指定页码超出支持范围','OCR_PAGE_UNSUPPORTED')
        pdf_page=document[page-1]
        width,height=pdf_page.get_size();rotation=pdf_page.get_rotation()
        if (not math.isfinite(width) or not math.isfinite(height) or width<=0 or height<=0
                or math.ceil(width*2)>6000 or math.ceil(height*2)>6000
                or math.ceil(width*2)*math.ceil(height*2)>16_000_000):
            raise OCRError('PDF 页面渲染尺寸超过限制，请拆分或人工核对','OCR_IMAGE_BUDGET_EXCEEDED')
        bitmap=pdf_page.render(scale=2.0)
        image=bitmap.to_pil().convert('RGB');image.load()
        if image.width>6000 or image.height>6000 or image.width*image.height>16_000_000:
            raise OCRError('PDF 页面渲染尺寸超过限制','OCR_IMAGE_BUDGET_EXCEEDED')
        rendered=io.BytesIO();image.save(rendered,format='PNG')
        raster_hash=hashlib.sha256(rendered.getvalue()).hexdigest()
    except OCRError:raise
    except Exception:raise OCRError('PDF 文件损坏、加密或无法渲染，请人工核对','OCR_PDF_INVALID') from None
    finally:
        if bitmap is not None:bitmap.close()
        if pdf_page is not None:pdf_page.close()
        if document is not None:document.close()
    result=_recognize_decoded(image,digest,page=page,coordinate_space='rendered_pdf_page_pixels_not_pdf_coordinates')
    result.update(rendered_image_sha256=raster_hash,render_scale=2.0,pdf_rotation_degrees=rotation,pdf_page_count=count)
    result['limitations'].append('框坐标对应本次页面渲染图像，不能直接映射到 PDF 原件坐标')
    result['limitations'].append('PDF 页面 UserUnit、裁剪及旋转会影响与原件坐标的对应；渲染比例不等于已验证的实际 DPI')
    result['limitations'].append('页面表单或注释可能未被渲染，须对照原 PDF 人工检查，识别结果不保证完整')
    return result


def _worker_main():
    """Minimal bounded stdin/stdout protocol; never print the input or paths."""
    import json
    import sys
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise OCRError('识别请求超过限制', 'OCR_REQUEST_TOO_LARGE')
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict) or set(value) != {'path', 'expected_sha256', 'page'}:
            raise OCRError('识别请求格式不正确', 'OCR_REQUEST_INVALID')
        result = (recognize_pdf if Path(value['path']).suffix.lower()=='.pdf' else recognize_image)(
            value['path'], value['expected_sha256'], page=value['page'])
        response = {'ok': True, 'result': result}
    except OCRError as error:
        response = {'ok': False, 'code': error.code, 'message': str(error)}
    except Exception:
        response = {'ok': False, 'code': 'OCR_INFERENCE_FAILED', 'message': '本地识别未完成，请对照原图人工处理'}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))
    sys.stdout.flush()


if __name__ == '__main__':
    _worker_main()
