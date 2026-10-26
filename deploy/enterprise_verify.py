"""Disposable, private verification service. Never invokes the real model or MCP."""
import os,base64,hashlib,io,json,zipfile,subprocess,sys,time,secrets
from pathlib import Path

def main():
    parts=int(os.environ['ATLAS_VERIFY_PARTS']);raw=base64.b64decode(''.join(os.environ[f'ATLAS_VERIFY_PART_{i:02d}'] for i in range(parts)),validate=True)
    if hashlib.sha256(raw).hexdigest()!=os.environ['ATLAS_VERIFY_SHA256']:raise ValueError('Verification bundle hash mismatch')
    root=Path('/opt/atlas-verify');root.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(x.file_size for x in z.infolist())>5000000:raise ValueError('Verification bundle too large')
        if any(Path(n).is_absolute() or '..' in Path(n).parts for n in z.namelist()):raise ValueError('Invalid verification path')
        z.extractall(root)
    subprocess.run(['apt-get','update','-qq'],check=True);subprocess.run(['apt-get','install','-y','--no-install-recommends','git','passwd'],check=True)
    subprocess.run(['useradd','-m','-u','10001','verifier'],check=True)
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','-r',str(root/'requirements-cloud.lock'),'pytest==9.1.1'],check=True)
    os.environ['PYTHONPATH']=str(root);os.chdir(root)
    from sqlalchemy import create_engine,text
    from sqlalchemy.engine import make_url
    target=make_url(os.environ['DATABASE_URL']);admin=create_engine(target,isolation_level='AUTOCOMMIT',connect_args={'connect_timeout':5});database='atlas_verify_'+secrets.token_hex(8);created=False
    try:
        for i in range(30):
            try:
                with admin.connect() as c:c.execute(text('SELECT 1'))
                break
            except Exception:
                if i==29:raise
                time.sleep(2)
        with admin.connect() as c:c.execute(text('CREATE DATABASE '+database));created=True
        env=dict(os.environ,DATABASE_URL=target.set(database=database).render_as_string(hide_password=False),HOME='/home/verifier')
        env.pop('ATLAS_MIGRATE_SQLITE',None)
        # The test process runs without root, and gets no model or user connector credentials.
        def drop():os.setgroups([]);os.setgid(10001);os.setuid(10001)
        result=subprocess.run([sys.executable,'-m','pytest','-q','tests/test_enterprise_work.py','tests/test_enterprise_deploy.py','tests/test_enterprise_migration.py','tests/test_web_lifecycle.py','tests/test_conversation_reliability.py','tests/test_store.py'],env=env,preexec_fn=drop)
        print('ATLAS_ENTERPRISE_POSTGRES_VERIFIED '+json.dumps({'ok':result.returncode==0,'source_sha256':os.environ['ATLAS_VERIFY_SHA256'],'real_postgresql':True,'isolated_database':True,'cross_node_lock_and_termination_fencing':result.returncode==0,'sqlite_migration':result.returncode==0,'real_model_evaluated':False}),flush=True)
        return result.returncode
    finally:
        if created:
            with admin.connect() as c:c.execute(text('DROP DATABASE '+database+' WITH (FORCE)'))
        admin.dispose()
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print('ATLAS_ENTERPRISE_VERIFICATION_FAILED '+type(exc).__name__,flush=True);raise SystemExit(1)
