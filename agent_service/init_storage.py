"""One-time shared-storage bootstrap; run as an init Job before replicas start."""
import os
from .config import DATA
from .store import cipher,get,put,seal,unseal
from .locks import FileLock

def main():
    expected=os.environ['ATLAS_STORAGE_ID']
    if not expected or len(expected)>100:raise ValueError('需要唯一存储标识')
    with FileLock(str(DATA/'storage-init.lock'),timeout=30):
        marker=DATA/'storage.identity'
        if marker.exists() and marker.read_text().strip()!=expected:raise ValueError('共享文件系统已属于其他工作空间')
        try:
            current=get('system','shared-storage')
            if current['storage_id']!=expected or unseal(current['key_proof'])!={'storage_id':expected}:raise ValueError('数据库与共享密钥不匹配')
        except KeyError:
            cipher();put('system',{'storage_id':expected,'key_proof':seal({'storage_id':expected})},'shared-storage')
        if not marker.exists():
            with marker.open('x') as f:f.write(expected+'\n');f.flush();os.fsync(f.fileno())
    print('ATLAS_SHARED_STORAGE_INITIALIZED',flush=True)
if __name__=='__main__':main()
