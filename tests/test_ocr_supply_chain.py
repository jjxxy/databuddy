"""Source-level checks for the optional offline OCR supply-chain inventory."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_ocr_wheel_lock_exactly_matches_official_inventory():
    inventory=json.loads((ROOT/'vendor/metadata/ocr-wheel-inventory.json').read_text(encoding='utf-8'))
    assert inventory['upstream_hash_verified'] is True and len(inventory['wheels'])==21
    expected={f"{x['name'].lower().replace('_','-')}=={x['version']} --hash=sha256:{x['sha256']}" for x in inventory['wheels']}
    lock={line.lower().replace('_','-') for line in (ROOT/'requirements-ocr-windows.lock').read_text(encoding='utf-8').splitlines() if line.strip()}
    assert lock==expected and len(expected)==21
    assert all(x['origin'].startswith('https://files.pythonhosted.org/') and x['wheel'].endswith('.whl') for x in inventory['wheels'])


def test_ocr_flatbuffers_license_matches_recorded_hash():
    inventory=json.loads((ROOT/'vendor/metadata/ocr-wheel-inventory.json').read_text(encoding='utf-8'))
    flat=next(x for x in inventory['wheels'] if x['name']=='flatbuffers')['supplemental_license']
    text=(ROOT/'vendor/metadata/ocr-licenses/flatbuffers-v25.12.19-LICENSE.txt').read_bytes()
    assert hashlib.sha256(text).hexdigest()==flat['sha256']
    assert flat['upstream']=='https://raw.githubusercontent.com/google/flatbuffers/v25.12.19/LICENSE'
    model=next(x for x in inventory['wheels'] if x['name']=='rapidocr')
    assert any(name.endswith('MODEL_LICENSES.md') for name in model['license_files'])
