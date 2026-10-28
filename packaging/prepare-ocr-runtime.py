"""Build the optional Windows OCR interpreter from preverified local wheels.

Maintainer-only: never invoked by application startup. The destination must be
new; no existing runtime, user workspace, or running desktop is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
MODELS = {
    'PP-OCRv6_det_small.onnx': '090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f',
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c',
    'PP-OCRv6_rec_small.onnx': '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884',
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--embedded-python-zip', type=Path, required=True)
    parser.add_argument('--build-python', type=Path, required=True)
    args = parser.parse_args()
    wheels = args.wheelhouse.resolve(strict=True)
    archive = args.embedded_python_zip.resolve(strict=True)
    python = args.build_python.resolve(strict=True)
    if not wheels.is_dir() or sha256(archive) != PYTHON_SHA256:
        raise ValueError('OCR wheelhouse or CPython archive integrity is invalid')
    runtime = ROOT / 'runtime'
    target = runtime / 'ocr'
    runtime.mkdir(parents=True, exist_ok=True)
    if runtime.resolve() != runtime or runtime.is_symlink():
        raise ValueError('Runtime directory must be inside this checkout')
    if target.exists() or target.is_symlink():
        raise FileExistsError('OCR runtime already exists; choose a new staging checkout')
    build = Path(tempfile.mkdtemp(prefix='ocr-build-', dir=runtime)).resolve()
    if not build.is_relative_to(runtime.resolve()):
        raise ValueError('OCR build directory escaped runtime')
    with zipfile.ZipFile(archive) as files:
        for member in files.infolist():
            if not (build / member.filename).resolve().is_relative_to(build):
                raise ValueError('Unsafe Python archive path')
        files.extractall(build)
    (build / 'python312._pth').write_text('python312.zip\n.\nLib\nLib/site-packages\n..\\..\\\nimport site\n', encoding='utf-8')
    site = build / 'Lib' / 'site-packages'
    # All transitive wheels are pinned and hashed; the client never invokes pip.
    subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--no-index',
                    '--find-links', str(wheels), '--only-binary=:all:', '--require-hashes',
                    '--no-deps', '--no-compile', '--target', str(site),
                    '-r', str(ROOT / 'requirements-ocr-windows.lock')], check=True)
    # Pip's Windows CLI launchers embed the build-machine Python path. The app
    # calls only the module through python.exe; never ship these launchers.
    scripts = site / 'bin'
    if scripts.is_dir() and not scripts.is_symlink():
        for item in scripts.iterdir():
            if not item.is_file() or item.is_symlink():raise ValueError('Unexpected OCR script payload')
            item.unlink()
        scripts.rmdir()
    for name, expected in MODELS.items():
        path = site / 'rapidocr' / 'models' / name
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('OCR model hash mismatch: ' + name)
    pdf_wheel = wheels / 'pypdfium2-5.14.0-py3-none-win_amd64.whl'
    if not pdf_wheel.is_file() or sha256(pdf_wheel) != '149fd5c6397b8df8bf7911a93506eff0be874f877afe7ac936cf5d37d21a6a06':
        raise ValueError('Pinned PDFium wheel is missing or changed')
    prefix = 'pypdfium2-5.14.0.dist-info/licenses/'
    with zipfile.ZipFile(pdf_wheel) as package:
        license_files = [item for item in package.infolist() if item.filename.startswith(prefix) and not item.is_dir()]
        if not license_files or not any(item.filename.endswith('/BUILD_LICENSES/pdfium.txt') for item in license_files):
            raise ValueError('PDFium license inventory is incomplete')
        for item in license_files:
            installed = site / item.filename
            if not installed.resolve().is_relative_to(site.resolve()) or not installed.is_file():
                raise ValueError('PDFium license was not installed: ' + item.filename)
            if hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(package.read(item)).digest():
                raise ValueError('PDFium license differs from its wheel: ' + item.filename)
    supplemental = ROOT / 'vendor/metadata/ocr-licenses/flatbuffers-v25.12.19-LICENSE.txt'
    if not supplemental.is_file() or sha256(supplemental)!='cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30':
        raise ValueError('FlatBuffers upstream license is missing or changed')
    licenses = build / 'third-party-licenses'; licenses.mkdir()
    shutil.copy2(supplemental, licenses / supplemental.name)
    # Verify local dependency versions and model hash before the runtime is put
    # into its final position. Full OCR and packaging tests are separate gates.
    executable = build / 'python.exe'
    check = subprocess.run([str(executable), '-I', '-B', '-c',
        'import importlib.metadata as m; assert m.version("rapidocr")=="3.10.0"; '
        'assert m.version("onnxruntime")=="1.31.1"; assert m.version("pypdfium2")=="5.14.0"; '
        'import cv2,numpy,onnxruntime,PIL,pypdfium2'],
        capture_output=True, text=True, check=True)
    build.rename(target)
    print(json.dumps({'ok': True, 'runtime': str(target), 'models_verified': len(MODELS),
                      'network_during_install': False, 'user_runtime_changed': False}))


if __name__ == '__main__':
    main()
