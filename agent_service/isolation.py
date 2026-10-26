"""Optional remote runner protocol. Never executes generated code on Atlas hosts.

The runner is an owner-provisioned independent service, not an in-process sandbox.
Its isolation must be audited separately before enabling this capability.
"""
import hashlib,json,os
from urllib.parse import urlsplit
import httpx
from . import workspace,provider

def status():
    configured=bool(os.getenv('ATLAS_RUNNER_URL') and os.getenv('ATLAS_RUNNER_TOKEN') and os.getenv('ATLAS_RUNNER_ISOLATION') in ('microvm','gvisor'))
    return {'configured':configured,'execution':'remote_only','isolation':os.getenv('ATLAS_RUNNER_ISOLATION') if configured else None,'deployment_verified':False}

def run(turn,path,inputs,timeout_seconds):
    if not status()['configured']:raise ValueError('尚未配置独立隔离运行服务，脚本已保存但不能在应用主机执行')
    if not provider.settings().get('share_data'):raise ValueError('向隔离运行服务发送项目文件需要启用项目内容读取')
    from .intents import require
    require(turn,'refresh')
    if not path.endswith('.py'):raise ValueError('隔离入口仅支持项目内 Python 文件')
    files={name:workspace.read_file(turn['project_id'],name) for name in dict.fromkeys([path,*inputs])}
    if len(json.dumps(files).encode())>512000:raise ValueError('隔离输入超过512KB')
    base=os.environ['ATLAS_RUNNER_URL'].rstrip('/');parsed=urlsplit(base)
    if parsed.scheme!='https' or parsed.query or parsed.fragment or parsed.username or parsed.password:raise ValueError('隔离运行服务必须使用无凭证的HTTPS地址')
    from .connectors import safe_api_url
    original=httpx.URL(base+'/v1/execute');ip=safe_api_url(str(original));target=original.copy_with(host=ip)
    trace=turn.get('agent_trace',[])
    if not trace:raise ValueError('缺少已持久化的执行步骤')
    key=hashlib.sha256((turn.get('agent_origin',turn['id'])+':'+trace[-1]['id']).encode()).hexdigest()
    payload={'protocol':1,'idempotency_key':key,'entrypoint':path,'files':files,'timeout_seconds':timeout_seconds,'memory_mb':256,'network':'none','isolation':os.environ['ATLAS_RUNNER_ISOLATION']}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest();payload['input_sha256']=digest
    with httpx.Client(timeout=timeout_seconds+10,trust_env=False,follow_redirects=False) as client:
        with client.stream('POST',target,headers={'Authorization':'Bearer '+os.environ['ATLAS_RUNNER_TOKEN'],'Host':original.netloc.decode()},json=payload,extensions={'sni_hostname':original.host}) as response:
            response.raise_for_status();chunks=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>150000:raise ValueError('隔离响应超过限制')
                chunks.append(chunk)
            result=json.loads(b''.join(chunks))
    if result.get('idempotency_key')!=key or result.get('input_sha256')!=digest or result.get('isolation')!=payload['isolation'] or result.get('network')!='none':raise ValueError('隔离服务返回的执行回执不匹配，未通过验收')
    if not isinstance(result.get('exit_code'),int) or not result.get('execution_id'):raise ValueError('隔离服务没有提供执行ID和退出状态')
    return {'execution_id':str(result['execution_id'])[:100],'input_sha256':digest,'file_hashes':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in files.items()},'exit_code':result['exit_code'],'stdout':str(result.get('stdout',''))[:20000],'stderr':str(result.get('stderr',''))[:5000],'isolation':result['isolation'],'network':'none','remote_receipt':True,'limitations':['回执来自配置的外部执行器；隔离部署本身需独立验收','不向执行器提供数据库凭证、环境变量或宿主机目录']}
