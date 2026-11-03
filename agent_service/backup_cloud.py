"""Consistent application snapshots in an encrypted, deduplicated Restic repository.

Restic owns cryptography, repository integrity and S3 transport. The repository
password is kept outside snapshots. Restores never overwrite an occupied workspace.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from .store import put,now

_CORE=('S3_ENDPOINT','S3_BUCKET','S3_ACCESS_KEY_ID','S3_SECRET_ACCESS_KEY','BACKUP_KEY')

def _setting(name,default=None):
    current=os.getenv('NEXUS_'+name);legacy=os.getenv('ATLAS_'+name)
    if current is not None and legacy is not None and current!=legacy:
        raise ValueError('新旧备份配置冲突：'+name)
    return current if current is not None else legacy if legacy is not None else default

def _prefix():
    requested=os.getenv('NEXUS_BACKUP_REPOSITORY_PREFIX')
    if requested is not None:
        if requested not in ('atlas','nexus'):raise ValueError('备份仓路径标识只能为 atlas 或 nexus')
        return requested
    # An existing legacy configuration must continue using its old repository.
    # Migrating credentials alone must not silently switch backup histories.
    if any(os.getenv('ATLAS_'+key) is not None for key in _CORE):return 'atlas'
    from .store import get
    try:previous=get('system','backup-health')
    except KeyError:return 'nexus'
    if previous.get('backend')!='restic':return 'nexus'
    saved=previous.get('repository_prefix')
    if saved in ('atlas','nexus'):return saved
    # Old receipts have no location. Neither infer a new empty repo nor init it:
    # the administrator can explicitly select the location after checking it.
    raise ValueError('历史备份仓路径不明确，请设置 NEXUS_BACKUP_REPOSITORY_PREFIX 为 atlas 或 nexus')

def configured():
    ready=all(_setting(key) for key in _CORE)
    if ready:_prefix()
    return ready
def file_sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()

def environment():
    from .network_policy import mode
    if mode()=='local_only':raise ValueError('仅本地连接模式不启用远程S3备份；请使用本地一致备份')
    if not configured():raise ValueError('异卷备份尚未配置')
    endpoint=_setting('S3_ENDPOINT');p=urlsplit(endpoint)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:raise ValueError('备份端点必须为HTTPS且不携带凭证')
    if len(_setting('BACKUP_KEY'))<32:raise ValueError('备份仓密码至少32个随机字符')
    # No app/model/database credentials are inherited by the backup executable.
    env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','HOME','SSL_CERT_FILE','SSL_CERT_DIR')}
    env.update(RESTIC_REPOSITORY='s3:'+endpoint.rstrip('/')+'/'+_setting('S3_BUCKET')+'/'+_prefix(),RESTIC_PASSWORD=_setting('BACKUP_KEY'),AWS_ACCESS_KEY_ID=_setting('S3_ACCESS_KEY_ID'),AWS_SECRET_ACCESS_KEY=_setting('S3_SECRET_ACCESS_KEY'),AWS_DEFAULT_REGION=_setting('S3_REGION','auto'),RESTIC_CACHE_DIR=os.getenv('RESTIC_CACHE_DIR','/tmp/nexus-restic-cache'),GOGC='50')
    return env

def command(args,env,check=True):
    from .network_policy import mode
    if mode()=='local_only':raise ValueError('仅本地连接模式不运行远程备份进程；请使用本地一致备份')
    binary=shutil.which('restic')
    if not binary:raise ValueError('未安装Restic，未创建备份')
    style=_setting('S3_URL_STYLE','path');lookup='dns' if style=='virtual-host' else 'path'
    result=subprocess.run([binary,'-o','s3.bucket-lookup='+lookup,*args],env=env,capture_output=True,text=True,timeout=int(_setting('BACKUP_TIMEOUT_SECONDS','3600')))
    if len(result.stdout)>2000000 or len(result.stderr)>2000000:raise ValueError('备份命令返回超过限制')
    if check and result.returncode:raise ValueError('Restic操作未完成，请检查备份服务权限和日志状态')
    return result

def save():
    if not configured():raise ValueError('异卷备份尚未配置')
    env=environment()
    from .maintenance import backup
    from .locks import FileLock
    from .config import DATA
    with FileLock(str(DATA/'cloud-backup.lock'),timeout=0),tempfile.TemporaryDirectory(prefix='nexus-backup-') as folder:
        plain=Path(folder)/'workspace.zip';backup(plain);digest=file_sha(plain);size=plain.stat().st_size
        if command(['snapshots','--json'],env,False).returncode:
            # Restic init refuses an existing repository, including one with a wrong password.
            command(['init'],env)
        result=command(['backup',str(plain),'--tag','nexus-workspace','--host','nexus-workspace','--json'],env)
        summaries=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        summary=next((v for v in reversed(summaries) if v.get('message_type')=='summary'),{})
        snapshot=summary.get('snapshot_id','')
        if not re.fullmatch(r'[0-9a-f]{8,64}',snapshot):raise ValueError('Restic未返回有效快照编号')
        restored=Path(folder)/'readback';command(['restore',snapshot,'--target',str(restored)],env)
        files=list(restored.rglob('workspace.zip'))
        if len(files)!=1 or file_sha(files[0])!=digest:raise ValueError('异卷备份回读校验失败')
        value={'ok':True,'backend':'restic','snapshot_id':snapshot,'sha256':digest,'plaintext_bytes':size,'offsite_readback_verified':True,'finished_at':now(),'independent_disaster_recovery_verified':False,'repository_prefix':_prefix()}
        put('system',value,'backup-health')
        days=int(_setting('BACKUP_RETENTION_DAYS','0'))
        if days>0:command(['forget','--tag','nexus-workspace','--host','nexus-workspace','--group-by','host,tags','--keep-daily',str(days),'--prune'],env)
        return value

def restore(snapshot):
    if not re.fullmatch(r'[0-9a-f]{8,64}',snapshot):raise ValueError('Restic快照编号无效')
    from .maintenance import restore as restore_workspace
    with tempfile.TemporaryDirectory(prefix='nexus-restore-') as folder:
        command(['restore',snapshot,'--target',folder],environment());files=list(Path(folder).rglob('workspace.zip'))
        if len(files)!=1:raise ValueError('快照不包含唯一工作空间备份')
        return restore_workspace(files[0])

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['save','restore']);parser.add_argument('--snapshot');args=parser.parse_args()
    if args.operation=='restore' and not args.snapshot:parser.error('restore requires --snapshot')
    value=save() if args.operation=='save' else restore(args.snapshot);print('NEXUS_OFFSITE_BACKUP '+json.dumps(value),flush=True)
if __name__=='__main__':
    try:main()
    except Exception as exc:print('NEXUS_OFFSITE_BACKUP_FAILED '+type(exc).__name__,flush=True);raise SystemExit(1)
