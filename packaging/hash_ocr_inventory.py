"""Bind OCR wheel distributions to delivered file hashes, not wheel hashes."""
import hashlib
import importlib.metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    metadata = ROOT / 'vendor/metadata'
    inventory = json.loads((metadata / 'runtime-manifest.json').read_text(encoding='utf-8'))
    files = {entry['path']: entry for entry in inventory['files']}
    site = ROOT / 'runtime/ocr/Lib/site-packages'
    packages = []
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        delivered = []
        for name in distribution.files or []:
            path = Path(distribution.locate_file(name)).resolve()
            if not path.is_relative_to(ROOT):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative in files:
                delivered.append(files[relative])
        delivered = sorted({entry['path']: entry for entry in delivered}.values(), key=lambda e: e['path'])
        if not delivered:
            raise ValueError('OCR distribution has no delivered files: ' + distribution.metadata['Name'])
        canonical = 'delivered-files-v1\n' + ''.join(e['path']+'\0'+e['sha256']+'\0'+str(e['bytes'])+'\n' for e in delivered)
        packages.append({'name': distribution.metadata['Name'], 'version': distribution.version,
            'sha256': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
            'sha256_kind': 'delivered-file-manifest-v1', 'delivered_files': len(delivered),
            'delivered_bytes': sum(e['bytes'] for e in delivered),
            'file_hash_reference': 'runtime-manifest.json#files'})
    if len(packages) != 21:
        raise ValueError('Unexpected number of OCR distributions')
    value = {'hash_definition': 'SHA256(UTF8("delivered-files-v1\\n" + sorted(path + NUL + file_sha256 + NUL + decimal_size + LF)))',
             'not_an_upstream_wheel_hash': True, 'packages': sorted(packages,key=lambda p:p['name'].lower())}
    (metadata / 'ocr-python-packages.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'packages':len(packages),'hash_kind':'delivered-file-manifest-v1'}))


if __name__ == '__main__':
    main()
