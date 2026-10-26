"""Opt-in real-model acceptance; dry-run never reports an intelligence score.

Use ATLAS_URL and APP_TOKEN environment variables. Each case creates a separate
project and retains it for review. No synthetic model or LLM judge is used.
"""
import argparse,hashlib,json,math,os,time,uuid
from decimal import Decimal
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]

def interval(passed,total):
    if total==0:return None
    z=1.959963984540054;p=passed/total;den=1+z*z/total
    center=(p+z*z/(2*total))/den;half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [max(0,center-half),min(1,center+half)]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--protocol-test',action='store_true',help='Simulated integration only; never produces an intelligence score');parser.add_argument('--limit',type=int);parser.add_argument('--repeat',type=int,default=1);parser.add_argument('--case',action='append',default=[]);parser.add_argument('--label',default='Atlas');parser.add_argument('--baseline',type=Path);parser.add_argument('--output',type=Path,default=Path('eval-results.json'));args=parser.parse_args()
    cases=json.loads((ROOT/'evals/tasks.json').read_text())
    assert len({c['id'] for c in cases})==len(cases)
    assert all(c['check'] in {'file','unchanged','rule','scalar','needs_input','run','build'} and c['mode'] in ('chat','work') and c['prompt'] for c in cases)
    manifest_hash=hashlib.sha256(json.dumps(cases,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if not 1<=args.repeat<=3:raise ValueError('repeat must be 1..3; repeated real-model tasks incur costs')
    if args.case:
        if set(args.case)-{c['id'] for c in cases}:raise ValueError('Unknown evaluation case')
        cases=[c for c in cases if c['id'] in args.case]
    args.limit=args.limit if args.limit is not None else min(3,len(cases))
    if args.protocol_test and args.baseline:raise ValueError('Simulated protocol results cannot be compared as a real-model benchmark')
    if not args.execute:
        print(json.dumps({'status':'not_executed','cases':len(cases),'case_manifest_sha256':manifest_hash,'checks_valid':True,'real_model_evaluated':False,'score':None}));return
    if not 1<=args.limit<=len(cases):raise ValueError('limit must be within case count')
    url=os.environ['ATLAS_URL'].rstrip('/');token=os.environ['APP_TOKEN']
    if not url.startswith('https://') and not url.startswith('http://127.0.0.1:'):raise ValueError('Use HTTPS or localhost')
    report={'status':'simulated_protocol' if args.protocol_test else 'real_run','real_model_evaluated':not args.protocol_test,'simulated_model':args.protocol_test,'label':args.label,'case_manifest_sha256':manifest_hash,'model':None,'cases':[],'pricing':'not estimated; provider-reported tokens only; setup calls listed separately'}
    with httpx.Client(base_url=url,headers={'Authorization':'Bearer '+token},timeout=15,trust_env=False,follow_redirects=False) as client:
        def request(method,path,**kw):
            r=client.request(method,path,**kw);r.raise_for_status();return r.json()
        def file_snapshot(root):
            return {f['path']:hashlib.sha256(client.get(root+'/file',params={'path':f['path']}).raise_for_status().content).hexdigest() for f in request('GET',root+'/files')}
        def wait(check,seconds=360):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                value=check()
                if value:return value
                time.sleep(.5)
            raise TimeoutError('Evaluation task exceeded timeout')
        model=request('GET','/api/model')
        if not model['configured']:raise ValueError('Connect a real model before evaluation; no score produced')
        report['model']=model['model']
        report['tool_protocol']=model.get('tool_protocol','json');report['project_content_reading']=model.get('share_data',False);report['service_version']=request('GET','/api/status')['version']
        for case,trial in [(c,i+1) for c in cases[:args.limit] for i in range(args.repeat)]:
            started=time.monotonic();key=uuid.uuid4().hex;result={'id':case['id'],'trial':trial,'passed':False,'setup_usage':[]}
            try:
                pid=request('POST','/api/projects',json={'name':'Agent 评测 · '+case['id'],'request_id':key})['id'];root='/api/projects/'+pid;aid=None;sid=None
                result['project_id']=pid
                if case.get('fixture'):
                    source=request('POST',root+'/attachments',files={'file':('orders.csv','订单号,状态,金额\nA1,已付款,100\nA2,已付款,50\nA3,待付款,30\n'.encode(),'text/csv')},data={'request_id':key+'-source'})
                    sid=source['id']
                    source=wait(lambda:next((s for s in request('GET','/api/sources') if s['id']==sid and s['status'] in ('ready','failed')),None))
                    if source['status']!='ready':raise ValueError('Fixture source failed')
                    if case['fixture'] in ('application','candidate'):
                        spec=dict(source['proposed_spec'],primary_key='c_1',group_by=None,amount_field=None,filters=[],computations=[])
                        app=request('POST','/api/pipelines',json={'source_id':sid,'name':'评测订单','spec':spec,'release_mode':'manual'})
                        aid=app['id']
                        ready=wait(lambda:next((p for p in request('GET','/api/pipelines') if p['id']==aid and p.get('candidate_run')),None))
                        if case['fixture']=='application':request('POST','/api/pipelines/'+aid+'/publish',json={'run_id':ready['candidate_run']})
                for setup in case.get('setup',[]):
                    content=setup['content']
                    if setup.get('prefix_lines'):content=('background archive line with no policy values, for retrieval testing.\n'*setup['prefix_lines'])+content
                    msg=request('POST',root+'/messages',json={'text':'创建 '+setup['path']+' 文件，内容逐字为：\n'+content,'interaction_mode':'work','request_id':uuid.uuid4().hex})
                    prepared=wait(lambda:next((t for t in request('GET',root+'/conversation')['turns'] if t['id']==msg['id'] and t['state'] in ('done','failed')),None))
                    result['setup_usage'].append(prepared.get('model_usage',{}))
                    file=client.get(root+'/file',params={'path':setup['path']})
                    if file.status_code!=200 or file.text.strip()!=content.strip():raise ValueError('Evaluation setup file does not match fixture')
                session=request('POST',root+'/sessions',json={'name':'独立验收对话','request_id':key+'-session'})['id'] if case.get('new_session') else None
                before_files=file_snapshot(root);before_apps=request('GET',root+'/applications');before_runs={r['id'] for a in before_apps for r in request('GET','/api/runs',params={'target':a['id']})}
                before_definitions={p['id']:p for p in request('GET','/api/pipelines') if p['id'] in {a['id'] for a in before_apps}}
                message=request('POST',root+'/messages',json={'text':case['prompt'],'interaction_mode':case['mode'],'application_id':aid,'source_id':sid if not aid else None,'request_id':key+'-turn','session_id':session})
                turn=wait(lambda:next((t for t in request('GET',root+'/conversation',params={'session_id':session} if session else {})['turns'] if t['id']==message['id'] and t['state'] in ('done','failed')),None))
                after_files=file_snapshot(root);after_apps=request('GET',root+'/applications');after_runs=[r for a in after_apps for r in request('GET','/api/runs',params={'target':a['id']}) if r['id'] not in before_runs]
                check=case['check'];passed=False
                if check=='file':
                    r=client.get(root+'/file',params={'path':case['path']});passed=r.status_code==200 and r.text.strip()==case['content'] and turn.get('verification',{}).get('verified',False)
                elif check=='unchanged':
                    after_definitions={p['id']:p for p in request('GET','/api/pipelines') if p['id'] in {a['id'] for a in after_apps}}
                    keys=('spec','commit','active_commit','published_run','candidate_run','cron','pending_schedule')
                    stable=lambda definitions:{k:{field:v.get(field) for field in keys} for k,v in definitions.items()}
                    passed=before_files==after_files and stable(before_definitions)==stable(after_definitions) and not after_runs
                elif check=='rule':
                    r=client.get(root+'/file',params={'path':'notes/business-rules.json'});passed=r.status_code==200 and any(x['quote']==case['quote'] and x['source_turn_id']==turn['id'] for x in r.json())
                elif check=='scalar':
                    for step in turn.get('agent_trace',[]):
                        obs=step.get('observation') or {};rows=obs.get('rows');sql=obs.get('sql') or step.get('arguments',{}).get('sql','')
                        if step.get('status')=='done' and rows and len(rows)==len(rows[0])==1 and 'where' in sql.lower():
                            try:passed=Decimal(str(rows[0][0]))==Decimal(case['expected'])
                            except Exception:pass
                elif check=='needs_input':passed=turn.get('outcome') in ('needs_input','blocked') or any(s.get('observation',{}).get('mode')=='llm_unanswerable' for s in turn.get('agent_trace',[]))
                elif check=='run':passed=bool(after_runs) and any(r.get('validated') and r.get('commit')==before_definitions.get(r['target'],{}).get('active_commit') for r in after_runs) and turn.get('verification',{}).get('verified',False)
                elif check=='build':
                    created={a['id'] for a in after_apps}-{a['id'] for a in before_apps}
                    passed=any(r.get('validated') and r['target'] in created for r in after_runs) and turn.get('verification',{}).get('verified',False)
                    if case.get('expected_spec'):
                        passed=passed and any(all(next(p for p in request('GET','/api/pipelines') if p['id']==new)['spec'].get(k)==v for k,v in case['expected_spec'].items()) for new in created)
                result.update(passed=bool(passed and turn['state']=='done'),turn_id=turn['id'],outcome=turn.get('outcome'),usage=turn.get('model_usage',{}))
            except Exception as exc:result['error_type']=type(exc).__name__
            result['duration_seconds']=round(time.monotonic()-started,2);report['cases'].append(result)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False),flush=True)
    report['passed']=sum(c['passed'] for c in report['cases']);report['total']=len(report['cases'])
    report['success_rate']=report['passed']/report['total'] if report['total'] and not args.protocol_test else None
    report['wilson_interval_95']=None if args.protocol_test else interval(report['passed'],report['total'])
    report['limitations']=['Same-case repeats are correlated; interval is descriptive, not a universal capability guarantee','Fixture source planning usage may not be included in turn totals','No commercial parity conclusion without a verified common-task baseline']
    if args.baseline:
        baseline=json.loads(args.baseline.read_text())
        if baseline.get('real_model_evaluated') is not True or baseline.get('simulated_model') is not False or baseline.get('case_manifest_sha256')!=manifest_hash:raise ValueError('Baseline must be a real run of the same case manifest')
        old={(c['id'],c.get('trial',1)):c for c in baseline['cases']};pairs=[(c,old[(c['id'],c['trial'])]) for c in report['cases'] if (c['id'],c['trial']) in old]
        report['comparison']={'baseline_label':baseline.get('label'),'baseline_model':baseline.get('model'),'paired_cases':len(pairs),'wins':sum(a['passed'] and not b['passed'] for a,b in pairs),'losses':sum(b['passed'] and not a['passed'] for a,b in pairs),'ties':sum(a['passed']==b['passed'] for a,b in pairs)}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed']==report['total'] else 1

if __name__=='__main__':raise SystemExit(main())
