import base64,hashlib,io,json,zipfile,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1];out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True);out.chmod(0o700);buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for folder in ('agent_service','tests'):
        for p in (root/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and (folder=='agent_service' or p.suffix=='.py'):z.write(p,p.relative_to(root).as_posix())
    z.write(root/'scripts/render_enterprise.py','scripts/render_enterprise.py');z.write(root/'requirements-cloud.lock','requirements-cloud.lock')
raw=buffer.getvalue();encoded=base64.b64encode(raw).decode();parts=[encoded[i:i+6000] for i in range(0,len(encoded),6000)];variables={'ATLAS_VERIFY_SHA256':hashlib.sha256(raw).hexdigest(),'ATLAS_VERIFY_PARTS':str(len(parts)),'ATLAS_VERIFY_BOOTSTRAP_B64':base64.b64encode((root/'deploy/enterprise_verify.py').read_bytes()).decode()};variables.update({f'ATLAS_VERIFY_PART_{i:02d}':v for i,v in enumerate(parts)});path=out/'variables.json';path.write_text(json.dumps(variables));path.chmod(0o600);print(json.dumps({'bytes':len(raw),'parts':len(parts),'sha256':variables['ATLAS_VERIFY_SHA256']}))
