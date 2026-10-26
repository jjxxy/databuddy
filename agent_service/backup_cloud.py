"""Consistent application snapshots in an encrypted, deduplicated Restic repository.

Restic owns cryptography, repository integrity and S3 transport. The repository
password is kept outside snapshots. Restores never overwrite an occupied workspace.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from .store import put,now

def configured():return all(os.getenv(k) for k in ('ATLAS_S3_ENDPOINT','ATLAS_S3_BUCKET','ATLAS_S3_ACCESS_KEY_ID','ATLAS_S3_SECRET_ACCESS_KEY','ATLAS_BACKUP_KEY'))
def file_sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()

def environment():
    from .network_policy import mode
    if mode()=='local_only':raise ValueError('仅本地连接模式不启用远程S3备份；请使用本地一致备份')
    endpoint=os.environ['ATLAS_S3_ENDPOINT'];p=urlsplit(endpoint)
    if p.scheme!='https' or p.username or p.password or p.query or p.fragment:raise ValueError('备份端点必须为HTTPS且不携带凭证')
    if len(os.environ['ATLAS_BACKUP_KEY'])<32:raise ValueError('备份仓密码至少32个随机字符')
    # No app/model/database credentials are inherited by the backup executable.
    env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','HOME','SSL_CERT_FILE','SSL_CERT_DIR')}
    env.update(RESTIC_REPOSITORY='s3:'+endpoint.rstrip('/')+'/'+os.environ['ATLAS_S3_BUCKET']+'/atlas',RESTIC_PASSWORD=os.environ['ATLAS_BACKUP_KEY'],AWS_ACCESS_KEY_ID=os.environ['ATLAS_S3_ACCESS_KEY_ID'],AWS_SECRET_ACCESS_KEY=os.environ['ATLAS_S3_SECRET_ACCESS_KEY'],AWS_DEFAULT_REGION=os.getenv('ATLAS_S3_REGION','auto'),RESTIC_CACHE_DIR=os.getenv('RESTIC_CACHE_DIR','/tmp/atlas-restic-cache'),GOGC='50')
    return env

def command(args,env,check=True):
    from .network_policy import mode
    if mode()=='local_only':raise ValueError('仅本地连接模式不运行远程备份进程；请使用本地一致备份')
    binary=shutil.which('restic')
    if not binary:raise ValueError('未安装Restic，未创建备份')
    style=os.getenv('ATLAS_S3_URL_STYLE','path');lookup='dns' if style=='virtual-host' else 'path'
    result=subprocess.run([binary,'-o','s3.bucket-lookup='+lookup,*args],env=env,capture_output=True,text=True,timeout=int(os.getenv('ATLAS_BACKUP_TIMEOUT_SECONDS','3600')))
    if len(result.stdout)>2000000 or len(result.stderr)>2000000:raise ValueError('备份命令返回超过限制')
    if check and result.returncode:raise ValueError('Restic操作未完成，请检查备份服务权限和日志状态')
    return result

def save():
    if not configured():raise ValueError('异卷备份尚未配置')
    env=environment()
    from .maintenance import backup
    from .locks import FileLock
    from .config import DATA
    with FileLock(str(DATA/'cloud-backup.lock'),timeout=0),tempfile.TemporaryDirectory(prefix='atlas-backup-') as folder:
        plain=Path(folder)/'workspace.zip';backup(plain);digest=file_sha(plain);size=plain.stat().st_size
        if command(['snapshots','--json'],env,False).returncode:
            # Restic init refuses an existing repository, including one with a wrong password.
            command(['init'],env)
        result=command(['backup',str(plain),'--tag','atlas-workspace','--host','atlas-workspace','--json'],env)
        summaries=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        summary=next((v for v in reversed(summaries) if v.get('message_type')=='summary'),{})
        snapshot=summary.get('snapshot_id','')
        if not re.fullmatch(r'[0-9a-f]{8,64}',snapshot):raise ValueError('Restic未返回有效快照编号')
        restored=Path(folder)/'readback';command(['restore',snapshot,'--target',str(restored)],env)
        files=list(restored.rglob('workspace.zip'))
        if len(files)!=1 or file_sha(files[0])!=digest:raise ValueError('异卷备份回读校验失败')
        value={'ok':True,'backend':'restic','snapshot_id':snapshot,'sha256':digest,'plaintext_bytes':size,'offsite_readback_verified':True,'finished_at':now(),'independent_disaster_recovery_verified':False}
        put('system',value,'backup-health')
        days=int(os.getenv('ATLAS_BACKUP_RETENTION_DAYS','0'))
        if days>0:command(['forget','--tag','atlas-workspace','--host','atlas-workspace','--group-by','host,tags','--keep-daily',str(days),'--prune'],env)
        return value

def restore(snapshot):
    if not re.fullmatch(r'[0-9a-f]{8,64}',snapshot):raise ValueError('Restic快照编号无效')
    from .maintenance import restore as restore_workspace
    with tempfile.TemporaryDirectory(prefix='atlas-restore-') as folder:
        command(['restore',snapshot,'--target',folder],environment());files=list(Path(folder).rglob('workspace.zip'))
        if len(files)!=1:raise ValueError('快照不包含唯一工作空间备份')
        return restore_workspace(files[0])

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['save','restore']);parser.add_argument('--snapshot');args=parser.parse_args()
    if args.operation=='restore' and not args.snapshot:parser.error('restore requires --snapshot')
    value=save() if args.operation=='save' else restore(args.snapshot);print('ATLAS_OFFSITE_BACKUP '+json.dumps(value),flush=True)
if __name__=='__main__':
    try:main()
    except Exception as exc:print('ATLAS_OFFSITE_BACKUP_FAILED '+type(exc).__name__,flush=True);raise SystemExit(1)
