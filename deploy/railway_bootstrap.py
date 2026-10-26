"""Pinned source-bundle distribution through Railway's authenticated service API."""
import base64,hashlib,io,os,pwd,shutil,subprocess,sys,zipfile
from pathlib import Path

def announce(message):print('ATLAS_BOOTSTRAP '+message,flush=True)
def main():
    if len(os.getenv('APP_TOKEN',''))<16:raise ValueError('Missing strong APP_TOKEN')
    mount=os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    data=Path(os.getenv('DATA_DIR','/data')).resolve()
    if not mount or not data.is_relative_to(Path(mount).resolve()):raise ValueError('Persistent data volume is required')
    count=int(os.environ['ATLAS_CODE_PARTS'])
    if not 1<=count<=100:raise ValueError('Source bundle part count is invalid')
    encoded=''.join(os.environ[f'ATLAS_CODE_PART_{i:02d}'] for i in range(count))
    raw=base64.b64decode(encoded,validate=True)
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=os.environ['ATLAS_CODE_SHA256']:raise ValueError('Source bundle checksum mismatch')
    base=Path('/opt/atlas');release=base/'source';release.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(v.file_size for v in archive.infolist())>10*1024*1024:raise ValueError('Source bundle exceeds limit')
        for entry in archive.infolist():
            relative=Path(entry.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename or ((entry.external_attr>>16)&0o170000)==0o120000:raise ValueError('Invalid source bundle path')
        archive.extractall(release)
    announce('source verified '+digest[:16])
    if not shutil.which('git') or not shutil.which('restic'):
        subprocess.run(['apt-get','update','-qq'],check=True)
        subprocess.run(['apt-get','install','-y','--no-install-recommends','git','passwd','restic'],check=True)
    try:pwd.getpwuid(10001)
    except KeyError:subprocess.run(['useradd','-m','-u','10001','agent'],check=True)
    runtime=base/'runtime'
    subprocess.run([sys.executable,'-m','venv',str(runtime)],check=True)
    python=runtime/'bin/python'
    announce('installing locked runtime dependencies')
    subprocess.run([str(python),'-m','pip','install','--no-cache-dir','--disable-pip-version-check','-r',str(release/'requirements-cloud.lock')],check=True)
    os.environ['PYTHONPATH']=str(release)
    os.environ['ATLAS_DROP_PRIVILEGES']='1'
    os.environ['ATLAS_CLOUD_VERIFY']='1'
    os.environ.setdefault('XDG_CACHE_HOME',str(data/'cache'))
    os.chdir(release)
    announce('starting the supervised application')
    os.execv(str(python),[str(python),'-m','agent_service.cloud'])

if __name__=='__main__':main()
