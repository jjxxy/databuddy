"""Render production manifests from explicit infrastructure inputs, fail closed.

No cluster is inferred and no dummy image or storage is treated as deployable.
The owner supplies a built image digest, HA RWX CSI claim, secrets and TLS host.
"""
import argparse,json,re
from pathlib import Path
import yaml

def render(c):
    for key in ('namespace','image','gatewayImage','rwxClaim','storageId','secretName','hostname','tlsSecret'):
        if not c.get(key):raise ValueError('缺少部署输入：'+key)
    for key in ('image','gatewayImage'):
        if not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}',c[key]):raise ValueError('生产镜像必须固定sha256摘要：'+key)
    ns=c['namespace'];name='atlas';docs=[]
    base={'app.kubernetes.io/name':name}
    def meta(n,extra=None):return {'name':n,'namespace':ns,'labels':dict(base,**(extra or {}))}
    docs.append({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns}})
    if c.get('provisionPostgres'):
        if not c.get('databaseStorageClass'):raise ValueError('需要数据库持久盘StorageClass')
        docs.append({'apiVersion':'postgresql.cnpg.io/v1','kind':'Cluster','metadata':meta('atlas-db'),'spec':{'instances':3,'postgresql':{'synchronous':{'method':'any','number':1,'dataDurability':'required'}},'storage':{'size':c.get('databaseSize','10Gi'),'storageClass':c['databaseStorageClass']},'bootstrap':{'initdb':{'database':'atlas','owner':'atlas','secret':{'name':c['secretName']}}},'affinity':{'enablePodAntiAffinity':True,'topologyKey':'kubernetes.io/hostname'},'resources':{'requests':{'cpu':'250m','memory':'512Mi'},'limits':{'cpu':'2','memory':'2Gi'}}}})
    env=[{'name':'DATA_DIR','value':'/data'},{'name':'ATLAS_DEPLOYMENT_PROFILE','value':'ha'},{'name':'ATLAS_STORAGE_MODE','value':'rwx'},{'name':'ATLAS_STORAGE_ID','value':c['storageId']},{'name':'ATLAS_AUTH_MODE','value':'oidc'},{'name':'OIDC_ACCEPT_PROXY_HEADER','value':'1'},{'name':'SCHEDULER_MODE','value':'worker'},{'name':'AGENT_ORCHESTRATION_MODE','value':'separate'},{'name':'ATLAS_OPENCODE_RUNTIME','value':'0'},{'name':'ATLAS_AGENT_EFFORT','value':'deep'},{'name':'PUBLIC_APP_URL','value':'https://'+c['hostname']},{'name':'DB_POOL_SIZE','value':'3'},{'name':'DB_MAX_OVERFLOW','value':'2'},{'name':'ATLAS_INSTANCE_ID','valueFrom':{'fieldRef':{'fieldPath':'metadata.name'}}}]
    # Unconfigured deployments inherit the application's unlimited default.
    # Explicit operator quotas, including zero (deny requests), remain grants.
    for key,env_name in (('dailyModelCalls','MAX_DAILY_MODEL_CALLS'),('dailyReservedTokens','MAX_DAILY_RESERVED_TOKENS')):
        if key not in c:continue
        value=c[key]
        if type(value) is not int or value<0:raise ValueError('每日模型预算必须为非负整数：'+key)
        env.append({'name':env_name,'value':str(value)})
    security={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}}
    mount=[{'name':'data','mountPath':'/data'},{'name':'tmp','mountPath':'/tmp'}];volumes=[{'name':'data','persistentVolumeClaim':{'claimName':c['rwxClaim']}},{'name':'tmp','emptyDir':{}}]
    container_security={'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'readOnlyRootFilesystem':True}
    def pod(role,command):
        return {'serviceAccountName':'atlas','automountServiceAccountToken':False,'terminationGracePeriodSeconds':45,'securityContext':security,'containers':[{'name':role,'image':c['image'],'command':command,'env':env,'envFrom':[{'secretRef':{'name':c['secretName']}}],'volumeMounts':mount,'securityContext':container_security,'resources':{'requests':{'cpu':'200m','memory':'384Mi'},'limits':{'cpu':'2','memory':'2Gi'}}}],'volumes':volumes}
    docs.append({'apiVersion':'v1','kind':'ServiceAccount','metadata':meta('atlas'),'automountServiceAccountToken':False})
    docs.append({'apiVersion':'batch/v1','kind':'Job','metadata':meta('atlas-init'),'spec':{'backoffLimit':3,'template':{'metadata':{'labels':dict(base,role='init')},'spec':dict(pod('init',['python','-m','agent_service.init_storage']),restartPolicy='OnFailure')}}})
    for role,command in [('api',['uvicorn','agent_service.api:app','--host','0.0.0.0','--port','8000']),('agent',['python','-m','agent_service.orchestrator']),('worker',['python','-m','agent_service.worker'])]:
        labels=dict(base,role=role);spec=pod(role,command)
        spec['affinity']={'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':labels},'topologyKey':'kubernetes.io/hostname'}]}}
        spec['topologySpreadConstraints']=[{'maxSkew':1,'topologyKey':'topology.kubernetes.io/zone','whenUnsatisfiable':'DoNotSchedule','labelSelector':{'matchLabels':labels}}]
        # The app refuses an uninitialized RWX volume. Init order is enforced by the deploy script.
        if role=='api':
            spec['containers'][0].update(ports=[{'containerPort':8000}],readinessProbe={'httpGet':{'path':'/ready','port':8000},'periodSeconds':5,'timeoutSeconds':8},livenessProbe={'httpGet':{'path':'/health','port':8000},'periodSeconds':10,'failureThreshold':6},startupProbe={'httpGet':{'path':'/ready','port':8000},'periodSeconds':5,'failureThreshold':60})
        else:
            check="from agent_service.runtime import readiness,nodes,INSTANCE; import sys; sys.exit(0 if readiness()['ready'] and any(n['instance']==INSTANCE and n['healthy'] for n in nodes('"+role+"')) else 1)"
            spec['containers'][0]['readinessProbe']={'exec':{'command':['python','-c',check]},'periodSeconds':10,'timeoutSeconds':10}
            spec['containers'][0]['livenessProbe']={'exec':{'command':['python','-c',check]},'periodSeconds':20,'timeoutSeconds':10,'failureThreshold':6,'initialDelaySeconds':60}
        docs.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':meta('atlas-'+role),'spec':{'replicas':2,'selector':{'matchLabels':labels},'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxSurge':1,'maxUnavailable':0}},'template':{'metadata':{'labels':labels},'spec':spec}}})
        docs.append({'apiVersion':'policy/v1','kind':'PodDisruptionBudget','metadata':meta('atlas-'+role),'spec':{'minAvailable':1,'selector':{'matchLabels':labels}}})
    docs.append({'apiVersion':'v1','kind':'Service','metadata':meta('atlas-api'),'spec':{'selector':dict(base,role='api'),'ports':[{'port':8000,'targetPort':8000}]}})
    # OIDC gateway handles the browser redirect/cookie; API validates the signed token again.
    labels=dict(base,role='gateway')
    gateway_env=[{'name':'OAUTH2_PROXY_OIDC_ISSUER_URL','valueFrom':{'secretKeyRef':{'name':c['secretName'],'key':'OIDC_ISSUER'}}},{'name':'OAUTH2_PROXY_CLIENT_ID','valueFrom':{'secretKeyRef':{'name':c['secretName'],'key':'OIDC_AUDIENCE'}}},{'name':'OAUTH2_PROXY_CLIENT_SECRET','valueFrom':{'secretKeyRef':{'name':c['secretName'],'key':'OIDC_CLIENT_SECRET'}}},{'name':'OAUTH2_PROXY_COOKIE_SECRET','valueFrom':{'secretKeyRef':{'name':c['secretName'],'key':'OIDC_COOKIE_SECRET'}}}]
    gateway={'serviceAccountName':'atlas','automountServiceAccountToken':False,'securityContext':security,'affinity':{'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':labels},'topologyKey':'kubernetes.io/hostname'}]}},'containers':[{'name':'gateway','image':c['gatewayImage'],'args':['--provider=oidc','--http-address=0.0.0.0:4180','--upstream=http://atlas-api:8000/','--redirect-url=https://'+c['hostname']+'/oauth2/callback','--email-domain=*','--code-challenge-method=S256','--cookie-secure=true','--cookie-samesite=lax','--cookie-refresh=5m','--cookie-expire=8h','--pass-authorization-header=true','--set-authorization-header=true','--pass-access-token=true','--skip-provider-button=true'],'env':gateway_env,'ports':[{'containerPort':4180}],'securityContext':container_security,'resources':{'requests':{'cpu':'100m','memory':'64Mi'},'limits':{'cpu':'1','memory':'256Mi'}},'readinessProbe':{'httpGet':{'path':'/ready','port':4180},'periodSeconds':5}}]}
    docs.extend([{'apiVersion':'apps/v1','kind':'Deployment','metadata':meta('atlas-gateway'),'spec':{'replicas':2,'selector':{'matchLabels':labels},'template':{'metadata':{'labels':labels},'spec':gateway}}},{'apiVersion':'policy/v1','kind':'PodDisruptionBudget','metadata':meta('atlas-gateway'),'spec':{'minAvailable':1,'selector':{'matchLabels':labels}}},{'apiVersion':'v1','kind':'Service','metadata':meta('atlas-gateway'),'spec':{'selector':labels,'ports':[{'port':4180,'targetPort':4180}]}}])
    docs.append({'apiVersion':'networking.k8s.io/v1','kind':'Ingress','metadata':dict(meta('atlas'),annotations={'nginx.ingress.kubernetes.io/ssl-redirect':'true','nginx.ingress.kubernetes.io/proxy-read-timeout':'180'}),'spec':{'ingressClassName':c.get('ingressClass','nginx'),'tls':[{'hosts':[c['hostname']],'secretName':c['tlsSecret']}],'rules':[{'host':c['hostname'],'http':{'paths':[{'path':'/','pathType':'Prefix','backend':{'service':{'name':'atlas-gateway','port':{'number':4180}}}}]}}]}})
    # No public route to the API; other namespace pods cannot bypass the identity gateway.
    docs.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':meta('atlas-api-ingress'),'spec':{'podSelector':{'matchLabels':dict(base,role='api')},'policyTypes':['Ingress'],'ingress':[{'from':[{'podSelector':{'matchLabels':dict(base,role='gateway')}}],'ports':[{'protocol':'TCP','port':8000}]}]}})
    if c.get('offsiteBackup'):
        backup_pod=pod('backup',['python','-m','agent_service.backup_cloud','save']);backup_pod['restartPolicy']='Never'
        docs.append({'apiVersion':'batch/v1','kind':'CronJob','metadata':meta('atlas-backup'),'spec':{'schedule':'0 2 * * *','timeZone':'Asia/Shanghai','concurrencyPolicy':'Forbid','successfulJobsHistoryLimit':2,'failedJobsHistoryLimit':3,'jobTemplate':{'spec':{'backoffLimit':2,'template':{'metadata':{'labels':dict(base,role='backup')},'spec':backup_pod}}}}})
    return docs

def main():
    p=argparse.ArgumentParser();p.add_argument('config',type=Path);p.add_argument('output',type=Path);a=p.parse_args();docs=render(json.loads(a.config.read_text()));a.output.write_text(yaml.safe_dump_all(docs,allow_unicode=True,sort_keys=False));print(json.dumps({'resources':len(docs),'output':str(a.output)}))
if __name__=='__main__':main()
