"""Package only runtime code and locked dependencies, never user data/secrets.

Output files are private staging inputs for authenticated Railway set-variables.
ATLAS_BOOTSTRAP_B64 contains deploy/railway_bootstrap.py; ATLAS_CODE_PART_* hold
6000-character base64 chunks. Apply skipDeploys=True, then redeploy the service.
"""
import argparse,base64,hashlib,io,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True);output.chmod(0o700)
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path in sorted((ROOT/'agent_service').rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc':continue
            archive.write(path,path.relative_to(ROOT).as_posix())
        archive.write(ROOT/'requirements-cloud.lock','requirements-cloud.lock')
    raw=stream.getvalue();encoded=base64.b64encode(raw).decode()
    variables={'ATLAS_CODE_SHA256':hashlib.sha256(raw).hexdigest(),'ATLAS_BOOTSTRAP_B64':base64.b64encode((ROOT/'deploy/railway_bootstrap.py').read_bytes()).decode()}
    chunks=[encoded[i:i+6000] for i in range(0,len(encoded),6000)]
    variables['ATLAS_CODE_PARTS']=str(len(chunks))
    for i,chunk in enumerate(chunks):variables[f'ATLAS_CODE_PART_{i:02d}']=chunk
    (output/'variables.json').write_text(json.dumps(variables))
    (output/'variables.json').chmod(0o600)
    report={'bytes':len(raw),'parts':len(chunks),'sha256':variables['ATLAS_CODE_SHA256']}
    (output/'manifest.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':main()
