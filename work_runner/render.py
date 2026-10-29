"""Fixed, offline rendition helper, installed only in the sandbox image.

LibreOffice/Chromium/Poppler run inside the workload's existing gVisor limits.
The API host never opens an untrusted Office document or runs a browser.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_path(raw, root):
    value = Path(raw)
    if value.is_absolute() or not value.parts or any(part in ('.', '..') for part in value.parts):
        raise ValueError('Expected a relative workspace path')
    current = root
    for part in value.parts:
        current = current / part
        if current.is_symlink():raise ValueError('Links are not allowed')
    if not current.resolve().is_relative_to(root.resolve()):raise ValueError('Path leaves workspace')
    return current


def run(args, timeout=90):
    # Tool diagnostics stay bounded in the outer guest log. stdout/stderr here
    # are not data transfer; unbounded child diagnostics must not fill memory.
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:raise ValueError('Rendition program failed; no success receipt was written')


def render(source, output, max_pages, *, first_page=1, root=None):
    root = root or Path('/workspace')
    source = checked_path(source, root)
    output = checked_path(output, root)
    if not source.is_file() or source.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('Input file is missing or too large')
    if not 1 <= max_pages <= 8:raise ValueError('Page budget out of range')
    if not 1 <= first_page <= 2000:raise ValueError('First page out of range')
    suffix = source.suffix.lower()
    if suffix not in ('.pdf', '.docx', '.pptx', '.xlsx', '.html', '.htm'):
        raise ValueError('Supported: PDF, DOCX, PPTX, XLSX and HTML')
    checksum = digest(source.read_bytes())
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Old files in a restored checkpoint must not become this run's receipt.
    manifest = output / 'manifest.json'
    if manifest.is_symlink():raise ValueError('Links are not allowed')
    manifest.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='atlas-render-', dir='/tmp') as scratch:
        scratch = Path(scratch)
        if suffix == '.pdf':pdf = source
        elif suffix in ('.html', '.htm'):
            pdf = scratch / 'page.pdf'
            run(['node', '/opt/atlas-work/render_html.mjs', str(source), str(pdf)])
        else:
            profile = scratch / 'office-profile'
            run(['libreoffice', '-env:UserInstallation=' + profile.as_uri(), '--headless', '--nologo',
                 '--nodefault', '--norestore', '--convert-to', 'pdf', '--outdir', str(scratch), str(source)])
            pdf = scratch / (source.stem + '.pdf')
        if not pdf.is_file() or pdf.stat().st_size > 25 * 1024 * 1024:
            raise ValueError('PDF rendition missing or over budget')
        from pypdf import PdfReader
        reader = PdfReader(str(pdf), strict=True)
        if reader.is_encrypted:raise ValueError('Encrypted PDFs require an explicitly decrypted input')
        total = len(reader.pages)
        count = min(total - first_page + 1, max_pages)
        if count < 1:raise ValueError('No pages to render')
        images = []
        for page in range(first_page, first_page + count):
            target = output / ('page-' + str(page))
            png = target.with_suffix('.png')
            if png.is_symlink():raise ValueError('Links are not allowed')
            png.unlink(missing_ok=True)
            run(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', '1600',
                 '-png', str(pdf), str(target)], timeout=30)
            raw = png.read_bytes()
            if len(raw) > 6 * 1024 * 1024 or raw[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('Invalid or oversized PNG rendition')
            width, height = struct.unpack('>II', raw[16:24])
            if not 1 <= width <= 1600 or not 1 <= height <= 1600:raise ValueError('Invalid image dimensions')
            images.append({'page': page, 'path': str(png.relative_to(root)), 'sha256': digest(raw),
                           'bytes': len(raw), 'width': width, 'height': height})
        if digest(source.read_bytes()) != checksum:raise ValueError('Input changed while rendering')
        result = {'schema': 'atlas-rendition-v1', 'input_sha256': checksum, 'input_format': suffix[1:],
                  'total_pages': total, 'first_page': first_page, 'rendered_pages': count, 'complete': count == total,
                  'images': images, 'network': 'none', 'financial_approval': False,
                  'scope': 'offline_rendition; visual_inspection_and_business_review_are_separate'}
        manifest.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        print(json.dumps({'manifest': str(manifest.relative_to(root)), 'rendered_pages': count,
                          'total_pages': total, 'complete': count == total}), flush=True)
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--output', required=True)
    parser.add_argument('--max-pages', type=int, default=4)
    parser.add_argument('--first-page', type=int, default=1)
    args = parser.parse_args()
    render(args.source, args.output, args.max_pages, first_page=args.first_page)
