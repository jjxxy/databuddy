"""Export a credential-free source snapshot and the manifest-verified r14 app.

The source ZIP is suitable for uploading to a private ChatGPT Page. The separate
Windows ZIP includes the existing portable dependencies. Neither contains live
workspace data or model configuration. No application service is restarted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TREES = ('agent_service', 'tests', 'ui', 'desktop', 'packaging', 'scripts',
                'acceptance', 'docs', 'evals', 'examples', '.github', 'vendor/metadata')
OMIT_PARTS = {'.git', '.venv', '.atlas-native', 'node_modules', '__pycache__',
              '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist'}
ROOT_EXTENSIONS = {'.py', '.md', '.toml', '.lock', '.yaml', '.yml', '.sh', '.cmd',
                   '.ps1', '.in', '.constraints', '.txt'}
SAFE_EVIDENCE = (
    'r14-source-inventory.json', 'r14-production-changes-since-r10.json',
    'budget-context-r14-tests.json', 'budget-context-r14-final-v02.xml',
    'budget-context-r14-final-v02.log', 'model-capabilities-v08.json',
    'backend-r14-delivery/backend-verification.json',
    'explicit-source-text-projection-v13.xml', 'explicit-source-text-projection-v13.log',
)


def native(path: Path) -> Path:
    absolute = path.absolute()
    return Path('\\\\?\\' + str(absolute)) if os.name == 'nt' else absolute


def sha_file(path: Path) -> str:
    result = hashlib.sha256()
    with native(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def safe_source(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in OMIT_PARTS for part in relative.parts):
        return False
    if path.is_symlink():
        raise ValueError('Source archive does not accept symbolic links: ' + relative.as_posix())
    if path.name.lower() in ('llm.json', '.env', '.atlas-local-folder'):
        return False
    if path.suffix.lower() in ('.pyc', '.pyo', '.log', '.lnk', '.sqlite', '.sqlite3', '.db', '.pem', '.key'):
        return False
    return path.is_file()


def collect_source() -> dict[str, Path]:
    selected = {}
    for tree in SOURCE_TREES:
        base = ROOT / tree
        if base.exists():
            for folder, directories, files in os.walk(base, followlinks=False):
                directories[:] = [name for name in directories if name not in OMIT_PARTS]
                for name in files:
                    path = Path(folder) / name
                    if safe_source(path):
                        selected[path.relative_to(ROOT).as_posix()] = path
    for path in ROOT.iterdir():
        if not safe_source(path):
            continue
        if (path.suffix.lower() in ROOT_EXTENSIONS or path.name.endswith('.example.json')
                or path.name.startswith('VALIDATION-') and path.suffix == '.json'
                or path.name in {'package.json', 'package-lock.json', 'BUILD-INFO.json',
                                 '.gitignore', '.dockerignore', 'LICENSE', 'NOTICE'}):
            selected[path.name] = path
    for path in (ROOT / 'deploy').rglob('*'):
        if safe_source(path) and (path.suffix in ('.py', '.md') or path.name.endswith('.example.json')):
            selected[path.relative_to(ROOT).as_posix()] = path
    for relative in SAFE_EVIDENCE:
        path = ROOT / 'verification' / relative
        if path.is_file():
            selected['verification/' + relative] = path
    return selected


def local_model_secrets() -> set[bytes]:
    # Used only for an exact-match exclusion check; never log configuration.
    path = ROOT / 'llm.json'
    found = set()
    if path.is_file():
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if 'api_key' in str(key).lower() and isinstance(item, str) and len(item) >= 16:
                        found.add(item.encode())
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(json.loads(path.read_text(encoding='utf-8-sig')))
    return found


def zip_info(name: str, date: str) -> zipfile.ZipInfo:
    parts = name.split('/')
    if name.startswith('/') or any(part in ('', '.', '..') or ':' in part for part in parts):
        raise ValueError('Unsafe archive member')
    year, month, day = map(int, date.split('-'))
    value = zipfile.ZipInfo(name, (year, month, day, 12, 0, 0))
    value.compress_type = zipfile.ZIP_DEFLATED
    value.external_attr = (stat.S_IFREG | 0o644) << 16
    return value


def write_member(archive, name, path, date, expected=None):
    digest = hashlib.sha256()
    before = native(path).stat()
    with native(path).open('rb') as source, archive.open(zip_info(name, date), 'w', force_zip64=True) as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
            target.write(chunk)
    after = native(path).stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('A packaged file changed while being read: ' + path.name)
    actual = digest.hexdigest()
    if expected is not None and actual != expected:
        raise ValueError('Portable manifest mismatch: ' + name)
    return actual


def source_zip(output, date, secrets):
    members = collect_source()
    inventory = json.loads((ROOT / 'verification/r14-source-inventory.json').read_text(encoding='utf-8'))
    for relative, expected in inventory['verified_source_hashes'].items():
        if relative not in members or sha_file(members[relative]) != expected:
            raise ValueError('Source snapshot differs from the r14 inventory: ' + relative)
    metadata = {'project': 'Atlas DataBuddy', 'version': '0.20.0', 'portable_baseline': 'r14',
                'export_date': date, 'kind': 'source_with_tests_and_dependency_locks',
                'bundled_runtime_included': False, 'credentials_included': False,
                'personal_workspace_data_included': False, 'full_finance_G1_passed': False,
                'r14_backend_source_files_verified': len(inventory['verified_source_hashes'])}
    readme = (
        'Atlas DataBuddy v0.20.0 源码快照（对应 r14）\n\n'
        '此包用于在 ChatGPT 网页端继续审查、修改和开发项目。包含源码、前端、测试、合成验收样本、'
        '部署模板、锁文件和运行库元数据。\n'
        '这是源码包，不包含已安装的 Python/Node 依赖及便携运行时；直接运行请使用单独的 Windows 便携 ZIP。\n'
        '真实模型密钥、个人工作空间、原始对话记录和部署账户状态未打包。需要模型配置时使用 llm.example.json，'
        '不要在聊天里粘贴密钥。\n'
        '当前完整财经月结 G1 尚未通过验收；最新状态见 CODEX-DELIVERY-v08.md，不能按历史验证记录宣称已全部通过。\n'
        'SOURCE-MANIFEST.sha256 校验本包源码；EXPORT-INFO.json 说明导出范围。\n'
    ).encode('utf-8')
    generated = {'下载说明.txt': readme,
                 'EXPORT-INFO.json': (json.dumps(metadata, ensure_ascii=False, indent=2) + '\n').encode()}
    target = output / ('Atlas-DataBuddy-v0.20.0-r14-source-' + date.replace('-', '') + '.zip')
    assert not target.exists(), 'Choose a new output path instead of replacing an existing archive'
    hashes = {}
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        for relative, path in sorted(members.items()):
            raw = native(path).read_bytes()
            if any(secret in raw for secret in secrets):
                raise ValueError('A private model credential appeared in selected source: ' + relative)
            hashes[relative] = hashlib.sha256(raw).hexdigest()
            archive.writestr(zip_info('Atlas-source/' + relative, date), raw)
        for relative, raw in generated.items():
            hashes[relative] = hashlib.sha256(raw).hexdigest()
            archive.writestr(zip_info('Atlas-source/' + relative, date), raw)
        manifest = ''.join(value + '  ' + name + '\n' for name, value in sorted(hashes.items())).encode()
        archive.writestr(zip_info('Atlas-source/SOURCE-MANIFEST.sha256', date), manifest)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(hashes) + 1
        for relative, expected in hashes.items():
            assert hashlib.sha256(archive.read('Atlas-source/' + relative)).hexdigest() == expected
    assert target.stat().st_size <= 10 * 1024 * 1024, 'Source ZIP exceeds the private Pages upload limit'
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': sha_file(target),
            'files': len(hashes) + 1, 'crc_verified': True, 'source_hashes_verified': True,
            'known_private_model_key_matches': 0, 'r14_backend_files_verified': len(inventory['verified_source_hashes'])}


def portable_zip(output, date):
    app = ROOT / 'dist/Atlas-DataBuddy-win32-x64-r14'
    manifest = app / 'FILES.sha256'
    members = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        digest, relative = line.split('  ', 1)
        assert len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
        assert relative not in members
        members[relative] = digest
    assert 'resources/app/llm.json' not in members
    members['FILES.sha256'] = sha_file(manifest)
    target = output / ('Atlas-DataBuddy-v0.20.0-r14-Windows-portable-' + date.replace('-', '') + '.zip')
    assert not target.exists()
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for index, (relative, expected) in enumerate(sorted(members.items()), 1):
            write_member(archive, 'Atlas/' + relative, app / relative, date, expected)
            if index % 3000 == 0:
                print(json.dumps({'portable_progress_files': index, 'total_files': len(members)}), flush=True)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(members)
    return {'path': str(target), 'bytes': target.stat().st_size, 'sha256': sha_file(target),
            'files': len(members), 'crc_verified': True, 'all_manifest_hashes_verified': True,
            'credentials_included': False, 'personal_workspace_data_included': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    assert not output.exists(), 'Output directory must be new'
    output.mkdir(parents=True)
    report = {'project': 'Atlas DataBuddy', 'version': '0.20.0', 'baseline': 'r14',
              'export_date': args.date, 'cloud_page_idempotency_key': str(uuid.uuid4())}
    report['source'] = source_zip(output, args.date, local_model_secrets())
    (output / 'packaging-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'source_complete': report['source']}, ensure_ascii=False), flush=True)
    report['portable'] = portable_zip(output, args.date)
    checksums = ''.join(entry['sha256'] + '  ' + Path(entry['path']).name + '\n'
                        for entry in (report['source'], report['portable']))
    (output / 'SHA256SUMS.txt').write_text(checksums, encoding='utf-8')
    (output / 'packaging-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
