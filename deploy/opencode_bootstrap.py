"""Private, volume-free OpenCode 2 service. No Atlas files or connector keys."""
import hashlib,json,os,pwd,shutil,subprocess,tarfile,tempfile,urllib.request
from pathlib import Path

VERSION='2.1.2'
BINARY_SHA256='cf5841e6666aeb469abec8b39ca494cef07a637756454a34a742b2f4996e1675'

def config(base):
    return {'update':'disable','permissions':[{'action':'*','resource':'*','effect':'deny'}],
            'plugins':[],'mcp':{'servers':{}},'instructions':[],
            'providers':{'atlas':{'name':'Atlas model','env':['ATLAS_MODEL_KEY'],
                'package':'@opencode/ai/providers/openai-compatible',
                'settings':{'baseURL':base,'timeout':45000,'headerTimeout':45000,'chunkTimeout':45000,'encoding':'none'},
                'compatibility':{'reasoningField':'reasoning_content'},
                'body':{'max_tokens':8192,'thinking':{'type':'enabled'},'reasoning_effort':'high'},
                'models':{'deepseek-v4-pro':{'modelID':'deepseek-v4-pro','name':'DeepSeek V4 Pro'}}}},
            'model':'atlas/deepseek-v4-pro'}

def main():
    if len(os.environ.get('OPENCODE_SERVER_PASSWORD',''))<24 or not os.getenv('ATLAS_MODEL_KEY'):raise ValueError('Missing service/model credentials')
    # Installer only; the API and binary run without root, on an empty workdir.
    subprocess.run(['apt-get','update','-qq'],check=True)
    subprocess.run(['apt-get','install','-y','--no-install-recommends','ca-certificates','libstdc++6','passwd'],check=True,stdout=subprocess.DEVNULL)
    root=Path('/opt/opencode');root.mkdir(parents=True,exist_ok=True)
    try:pwd.getpwuid(10001)
    except KeyError:subprocess.run(['useradd','-u','10001','-d','/opt/opencode/work','-M','opencode'],check=True)
    with tempfile.TemporaryDirectory() as temp:
        tarball=Path(temp)/'runtime.tgz'
        with urllib.request.urlopen(f'https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-{VERSION}.tgz',timeout=60) as source,tarball.open('wb') as out:
            shutil.copyfileobj(source,out)
        with tarfile.open(tarball) as archive:
            entry=archive.getmember('package/bin/opencode')
            with archive.extractfile(entry) as source,(root/'opencode').open('wb') as out:shutil.copyfileobj(source,out)
    binary=root/'opencode'
    if hashlib.file_digest(binary.open('rb'),'sha256').hexdigest()!=BINARY_SHA256:raise ValueError('OpenCode binary checksum mismatch')
    binary.chmod(0o755)
    work=root/'work';work.mkdir(mode=0o700,exist_ok=True)
    global_config=work/'config/opencode';global_config.mkdir(parents=True,exist_ok=True)
    (global_config/'opencode.json').write_text(json.dumps(config('https://api.deepseek.com')))
    (global_config/'opencode.json').chmod(0o600)
    for path in [root,work,*work.rglob('*')]:os.chown(path,10001,10001)
    runtime_env={'PATH':'/usr/bin:/bin','XDG_DATA_HOME':str(work/'data'),'XDG_CONFIG_HOME':str(work/'config'),
                 'XDG_STATE_HOME':str(work/'state'),'XDG_CACHE_HOME':str(work/'cache'),'OPENCODE_DISABLE_MODELS_FETCH':'true',
                 'OPENCODE_SERVER_PASSWORD':os.environ['OPENCODE_SERVER_PASSWORD'],'ATLAS_MODEL_KEY':os.environ['ATLAS_MODEL_KEY']}
    os.chdir(work);os.setgroups([]);os.setgid(10001);os.setuid(10001)
    print('ATLAS_OPENCODE_READY version='+VERSION+' binary_sha256='+BINARY_SHA256,flush=True)
    os.execve(binary,[str(binary),'serve','--hostname','::','--port',os.environ.get('PORT','8080')],runtime_env)

if __name__=='__main__':main()
