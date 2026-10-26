"""Developer-only Windows runtime assembler. Never called during app startup.

Downloads from official CPython/npm endpoints, verifies npm integrity, preserves
licenses, and vendors an already resolved wheel environment. Target users need
no installer, network, Python, Git, compiler, or package manager.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = '3.12.10'
OPENCODE_VERSION = '2.1.2'
PYTHON_URL = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip'
OPENCODE_LICENSE_URL = f'https://raw.githubusercontent.com/anomalyco/opencode/v{OPENCODE_VERSION}/LICENSE'


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=180) as response:
        return response.read(220_000_001)


def record(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def python_runtime() -> dict:
    folder = ROOT / 'runtime/python'
    folder.mkdir(parents=True, exist_ok=True)
    archive = ROOT / 'vendor/downloads' / f'python-{PYTHON_VERSION}-embed-amd64.zip'
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        archive.write_bytes(download(PYTHON_URL))
    data = archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as files:
        for member in files.infolist():
            if not (folder / member.filename).resolve().is_relative_to(folder.resolve()):
                raise ValueError('Unsafe CPython archive member')
        files.extractall(folder)
    (folder / 'python312._pth').write_text(
        # The trailing separator matters on Windows: a final '..' without it
        # is stripped by path canonicalization, resolving one level too shallow.
        'python312.zip\n.\nLib\nLib/site-packages\n..\\..\\\nimport site\n', encoding='utf-8')
    return {'component': 'CPython Windows embeddable', 'version': PYTHON_VERSION,
            'source_url': PYTHON_URL, 'sha256': hashlib.sha256(data).hexdigest(),
            'path': 'runtime/python', 'license': 'runtime/python/LICENSE.txt'}


def opencode_runtime() -> dict:
    endpoint = f'https://registry.npmjs.org/@opencode/cli-windows-x64/{OPENCODE_VERSION}'
    metadata = json.loads(download(endpoint))
    dist = metadata['dist']
    data = download(dist['tarball'])
    algorithm, encoded = dist['integrity'].split('-', 1)
    if algorithm != 'sha512' or hashlib.sha512(data).digest() != base64.b64decode(encoded):
        raise ValueError('OpenCode official package integrity verification failed')
    folder = ROOT / 'runtime/opencode'
    folder.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = [member for member in archive.getmembers()
                   if member.isfile() and Path(member.name).name.lower() == 'opencode.exe']
        if len(members) != 1 or members[0].size > 300_000_000:
            raise ValueError('Unexpected OpenCode Windows package layout')
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError('OpenCode binary missing')
        (folder / 'opencode.exe').write_bytes(stream.read())
        for member in archive.getmembers():
            if member.isfile() and Path(member.name).name.lower().startswith(('license', 'notice')):
                source = archive.extractfile(member)
                if source:
                    (folder / Path(member.name).name).write_bytes(source.read())
    # Platform-only npm packages omit license text; retain the exact source
    # release tag's license beside the executable instead of only its SPDX ID.
    license_data = download(OPENCODE_LICENSE_URL)
    if len(license_data) > 64_000 or b'MIT' not in license_data[:100]:
        raise ValueError('Unexpected OpenCode release license')
    (folder / 'LICENSE').write_bytes(license_data)
    record(ROOT / 'vendor/metadata/opencode-package.json', metadata)
    return {'component': '@opencode/cli-windows-x64', 'version': OPENCODE_VERSION,
            'source_url': dist['tarball'], 'integrity': dist['integrity'],
            'sha256': hashlib.sha256(data).hexdigest(), 'path': 'runtime/opencode/opencode.exe',
            'license': 'runtime/opencode/LICENSE', 'license_url': OPENCODE_LICENSE_URL}


def git_runtime(source: Path) -> dict:
    source = source.resolve()
    if not (source / 'cmd/git.exe').is_file() or not (source / 'LICENSE.txt').is_file():
        raise ValueError('Git source must be a complete portable Git distribution')
    folder = ROOT / 'runtime/git'
    shutil.copytree(source, folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('.git', '.gitconfig', '.git-credentials'))
    version = subprocess.run([str(folder / 'cmd/git.exe'), '--version'], check=True,
                             capture_output=True, text=True).stdout.strip()
    return {'component': 'Git for Windows portable distribution', 'version': version,
            'source': 'Bundled Codex developer runtime portable distribution',
            'upstream': 'https://gitforwindows.org/', 'path': 'runtime/git',
            'license': 'runtime/git/LICENSE.txt'}


def libraries(venv: Path) -> dict:
    source = venv / 'Lib/site-packages'
    if not (source / 'duckdb').is_dir() or not (source / 'dbt/cli/main.py').is_file():
        raise ValueError('Install requirements-windows.lock into the build virtualenv first')
    target = ROOT / 'runtime/python/Lib/site-packages'
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', 'pip', 'pip-*.dist-info',
                                                 'pytest', '_pytest', 'pytest-*.dist-info'))
    # pywin32's standard .pth imports this module; copying the wheel libraries
    # preserves its DLL bootstrap without installing anything into Windows.
    packages = []
    for metadata in sorted(target.glob('*.dist-info/METADATA')):
        name = version = ''
        for line in metadata.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('Name: '): name = line[6:]
            elif line.startswith('Version: '): version = line[9:]
            if name and version: break
        packages.append({'name': name, 'version': version})
    record(ROOT / 'vendor/metadata/python-packages.json', {'packages': packages})
    return {'component': 'Windows Python libraries', 'lock': 'requirements-windows.lock',
            'lock_sha256': hashlib.sha256((ROOT / 'requirements-windows.lock').read_bytes()).hexdigest(),
            'path': 'runtime/python/Lib/site-packages', 'packages': len(packages),
            'licenses': 'License files retained in each package dist-info directory'}


def manifest(components: list[dict]) -> None:
    root = ROOT / 'runtime'
    paths = [path for path in sorted(root.rglob('*'))
             if path.is_file() and '__pycache__' not in path.parts]
    def fingerprint(path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return {'path': path.relative_to(ROOT).as_posix(), 'bytes': path.stat().st_size,
                'sha256': digest.hexdigest()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        files = list(pool.map(fingerprint, paths))
    record(ROOT / 'vendor/metadata/runtime-manifest.json',
           {'platform': 'win32-x64', 'build_time_network_only': True,
            'components': components, 'files': files})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git-source', type=Path, required=True)
    parser.add_argument('--venv', type=Path, default=ROOT / '.venv')
    parser.add_argument('--tools-only', action='store_true')
    args = parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(python_runtime), pool.submit(opencode_runtime),
                 pool.submit(git_runtime, args.git_source)]
        components = [task.result() for task in tasks]
    if not args.tools_only:
        components.append(libraries(args.venv.resolve()))
    manifest(components)
    print(json.dumps({'ok': True, 'components': components}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
