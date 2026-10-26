"""Explicit, bounded acceptance against an already provisioned enterprise cluster.

Creates one isolated probe project. Pod deletion requires --disrupt-agent plus
an explicit kubectl context and namespace, and targets only the owning Atlas pod.
This is a failover experiment, never an uptime-SLO certification.
"""
import argparse,json,os,re,subprocess,time
from urllib.parse import urlsplit
import httpx

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--token-env',default='ATLAS_VERIFY_TOKEN');p.add_argument('--disrupt-agent',action='store_true');p.add_argument('--context');p.add_argument('--namespace');a=p.parse_args()
    target=urlsplit(a.url)
    if target.scheme!='https' or target.username or target.password or target.query or target.fragment:p.error('Use an HTTPS application origin without credentials')
    if a.disrupt_agent and (not a.context or not a.namespace):p.error('Disruption requires explicit --context and --namespace')
    token=os.environ.get(a.token_env,'')
    if not token:p.error('Set the credential environment variable; do not place tokens on the command line')
    with httpx.Client(base_url=a.url.rstrip('/'),headers={'Authorization':'Bearer '+token},timeout=15,trust_env=False,follow_redirects=False) as client:
        def request(method,path,**kwargs):
            response=client.request(method,path,**kwargs);response.raise_for_status();return response.json()
        platform=request('GET','/api/platform')
        if platform['ha_configuration_gaps'] or not platform['readiness']['ready']:raise ValueError('Infrastructure preflight failed: '+','.join(platform['ha_configuration_gaps']))
        project=request('POST','/api/projects',json={'name':'HA故障恢复验收','request_id':'ha-probe-'+str(time.time_ns())});root='/api/projects/'+project['id'];body={'text':'请创建 ha-probe.txt 文件，内容逐字为：HA-FAULT-RECOVERY。','request_id':'ha-request-'+str(time.time_ns()),'interaction_mode':'work'}
        turn=request('POST',root+'/messages',json=body);assert request('POST',root+'/messages',json=body)['id']==turn['id']
        started=time.monotonic();deadline=started+240;disrupted=False;owner=None
        while time.monotonic()<deadline:
            try:
                value=request('GET',root+'/conversation');current=next(t for t in value['turns'] if t['id']==turn['id'])
                if a.disrupt_agent and not disrupted and current.get('execution_node') and current['state'] not in ('done','failed'):
                    owner=current['execution_node']
                    if not re.fullmatch(r'[a-z0-9-]{1,63}',owner):raise ValueError('Unexpected execution pod identity')
                    cmd=['kubectl','--context',a.context,'--namespace',a.namespace]
                    pod=json.loads(subprocess.check_output([*cmd,'get','pod',owner,'-o','json'],text=True))
                    labels=pod['metadata']['labels']
                    if labels.get('app.kubernetes.io/name')!='atlas' or labels.get('role')!='agent':raise ValueError('Refusing to disrupt a non-Atlas agent pod')
                    subprocess.run([*cmd,'delete','pod',owner,'--wait=false'],check=True,capture_output=True);disrupted=True
                if current['state'] in ('done','failed') and current.get('work_version'):break
            except (httpx.ConnectError,httpx.ReadTimeout,httpx.RemoteProtocolError):pass
            time.sleep(.3)
        else:raise TimeoutError('The probe did not recover within the observation window')
        if current.get('outcome')!='verified_complete':raise ValueError('The probe did not complete with evidence')
        response=client.get(root+'/file',params={'path':'ha-probe.txt'});response.raise_for_status();assert response.text=='HA-FAULT-RECOVERY。'
        final=request('GET','/api/platform');report={'ok':True,'project_id':project['id'],'accepted_request_id_stable':True,'file_exact_and_git':True,'agent_pod_disrupted':disrupted,'owner_before':owner,'execution_node_after':current.get('execution_node'),'elapsed_seconds':round(time.monotonic()-started,2),'healthy_nodes':final['healthy_nodes'],'high_availability_slo_verified':False,'model_cost_incurred':True}
        if a.disrupt_agent and not disrupted:raise ValueError('Probe completed before disruption; failover was not tested')
        print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
