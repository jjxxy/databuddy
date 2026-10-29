"""Deployment preflight, per-instance heartbeats and readiness evidence."""
import os,socket,time,hashlib
from pathlib import Path
from sqlalchemy import text
from .config import DATA,DATABASE_URL
from .store import get,put,all_items,now,engine
from .release_info import RUNTIME_REVISION
INSTANCE=os.getenv('ATLAS_INSTANCE_ID') or os.getenv('RAILWAY_REPLICA_ID') or socket.gethostname()

def heartbeat(role,**extra):
    from .production_release import process_build_fingerprint
    try:build=process_build_fingerprint()
    except Exception:build=None # Qualification fails closed; normal heartbeat still works.
    value={'role':role,'instance':INSTANCE,'heartbeat':now(),'version':'0.20.0',**extra,
           'build_fingerprint':build,'runtime_revision':RUNTIME_REVISION}
    put('runtime_node',value,'node_'+hashlib.sha256((role+':'+INSTANCE).encode()).hexdigest()[:40])
    # Compatibility for earlier clients; aggregate health uses runtime_node.
    put('system',value,role+'-health')

def nodes(role):
    from .activity import timestamp
    return [dict(n,healthy=time.time()-timestamp(n['heartbeat'])<20) for n in all_items('runtime_node') if n.get('role')==role]

def healthy(role):
    current=nodes(role)
    if current:return any(n['healthy'] for n in current)
    from .activity import timestamp
    try:return time.time()-timestamp(get('system',role+'-health')['heartbeat'])<20
    except KeyError:return False

def preflight():
    profile=os.getenv('ATLAS_DEPLOYMENT_PROFILE','single')
    if profile not in ('single','ha'):raise ValueError('未知部署类型')
    if profile=='ha':
        if engine.dialect.name!='postgresql':raise ValueError('HA部署必须使用共享PostgreSQL，不支持SQLite')
        if os.getenv('ATLAS_STORAGE_MODE')!='rwx':raise ValueError('HA部署必须配置支持POSIX锁的共享RWX文件系统')
        expected=os.getenv('ATLAS_STORAGE_ID','')
        marker=DATA/'storage.identity'
        if not expected or not marker.is_file() or marker.read_text().strip()!=expected:raise ValueError('共享存储身份校验失败，请先运行存储初始化作业')
        if not (DATA/'connector.key').is_file():raise ValueError('HA部署必须先初始化共享加密密钥；禁止每节点各自生成')
        from .store import unseal
        try:
            record=get('system','shared-storage')
            if record['storage_id']!=expected or unseal(record['key_proof'])!={'storage_id':expected}:raise ValueError('共享存储或加密密钥不匹配')
        except KeyError:raise ValueError('共享存储初始化尚未通过') from None
    return profile

def readiness():
    checks={'database':False,'storage':False}
    try:
        preflight()
        with engine.connect() as c:checks['database']=c.execute(text('SELECT 1')).scalar()==1
        # An actual read/write roundtrip, unique per instance; no user content.
        path=DATA/('.ready-'+hashlib.sha256(INSTANCE.encode()).hexdigest()[:16]);path.write_text('ready');checks['storage']=path.read_text()=='ready';path.unlink()
    except Exception:pass
    return {'ready':all(checks.values()),'checks':checks,'profile':os.getenv('ATLAS_DEPLOYMENT_PROFILE','single'),'high_availability_verified':False}
