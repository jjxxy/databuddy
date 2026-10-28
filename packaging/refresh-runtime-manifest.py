"""Rebuild the staged Windows runtime inventory including the optional OCR worker."""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def native(path):
    return Path('\\\\?\\'+str(path)) if os.name=='nt' else path


def sha(path):
    h=hashlib.sha256();size=0
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):h.update(chunk);size+=len(chunk)
    return size,h.hexdigest()


def main():
    runtime=ROOT/'runtime'
    if not (runtime/'python/python.exe').is_file() or not (runtime/'ocr/python.exe').is_file():
        raise ValueError('Both the main and optional OCR runtime must be staged')
    manifest=ROOT/'vendor/metadata/runtime-manifest.json'
    prior=json.loads(manifest.read_text(encoding='utf-8'))
    base=native(runtime);paths=[]
    for folder,dirs,names in os.walk(base,followlinks=False):
        folder=Path(folder)
        for name in dirs:
            if (folder/name).is_symlink():raise ValueError('Runtime cannot contain symlink directories')
        dirs[:]=[d for d in dirs if d not in ('__pycache__','.pytest_cache')]
        for name in names:
            path=folder/name
            if path.is_symlink():raise ValueError('Runtime cannot contain symlink files')
            if name.lower().endswith('.pyc'):continue
            paths.append(path)
    def fingerprint(path):
        size,digest=sha(path)
        return {'path':'runtime/'+path.relative_to(base).as_posix(),'bytes':size,'sha256':digest}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        files=sorted(pool.map(fingerprint,paths),key=lambda x:x['path'])
    lock=ROOT/'requirements-ocr-windows.lock';_,lock_hash=sha(lock)
    components=[item for item in prior.get('components',[]) if item.get('component')!='Offline OCR Windows runtime']
    components.append({'component':'Offline OCR Windows runtime','path':'runtime/ocr',
        'python_version':'3.12.10','rapidocr_version':'3.10.0','onnxruntime_version':'1.31.1',
        'pypdfium2_version':'5.14.0','pdfium_version':'156.0.8076.0',
        'lock':'requirements-ocr-windows.lock','lock_sha256':lock_hash,
        'wheel_inventory':'vendor/metadata/ocr-wheel-inventory.json',
        'licenses':'runtime/ocr/third-party-licenses and each distribution license'})
    value={'platform':'win32-x64','build_time_network_only':True,'components':components,'files':files}
    manifest.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'files':len(files),'ocr_files':sum(e['path'].startswith('runtime/ocr/') for e in files)}))


if __name__=='__main__':main()
