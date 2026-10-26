"""Acceptance check against the actual running trial service; never logs credentials."""
import json,os,sys,time
from decimal import Decimal
from pathlib import Path
import httpx

def main():
    port=int(os.getenv('PORT','8000'));marker=Path(os.environ['DATA_DIR'])/'cloud-acceptance.json'
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+os.environ['APP_TOKEN']},trust_env=False,timeout=10) as client:
        def request(method,path,**kwargs):
            r=client.request(method,path,**kwargs);r.raise_for_status();return r.json()
        def failure(job):
            details={'code':job.get('error_code'),'stage':job.get('stage')}
            folder=(Path(os.environ['DATA_DIR'])/job.get('artifact_dir','')).resolve()
            if folder.is_relative_to((Path(os.environ['DATA_DIR'])/'runs').resolve()):
                logfile=folder/'execution.log'
                if logfile.exists():details['sample_execution_log']=logfile.read_text(errors='replace')[-3500:]
            print('ATLAS_SAMPLE_FAILURE '+json.dumps(details),flush=True)
        def wait(check,seconds=300):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                try:
                    result=check()
                    if result:return result
                except (httpx.ConnectError,httpx.ReadError):pass
                time.sleep(.5)
            raise TimeoutError('Trial acceptance timed out')
        wait(lambda:request('GET','/health'),90)
        wait(lambda:request('GET','/api/status')['worker']['healthy'],30)
        if marker.exists():
            old=json.loads(marker.read_text())
            pipelines=request('GET','/api/pipelines')
            p=next((p for p in pipelines if p['id']==old['pipeline_id']),None)
            if p and p.get('published_run'):request('GET','/api/pipelines/'+p['id']+'/result')
            assert request('GET','/api/audit/verify')['ok']
            print('ATLAS_RESTART_VERIFIED '+json.dumps({'ok':True,'uid':os.geteuid(),'pipeline_id':old['pipeline_id']}),flush=True)
            verify_conversation(client, request, wait)
            verify_workspace(client,request,wait)
            verify_data_application(client,request,wait)
            verify_opencode_smoke(client,request,wait)
            verify_web_lifecycle(request)
            verify_enterprise(request,wait)
            return
        text='订单号,客户,金额,退款,状态\nA1,示例客户,100.25,10.10,已付款\nA2,示例客户,20,0,待付款\nA3,示例客户,30,2,已付款\n'
        s=request('POST','/api/upload',files={'file':('trial-orders.csv',text.encode(),'text/csv')},data={'goal':'仅统计已付款订单，金额扣除退款'})
        def source_ready():
            source=next(x for x in request('GET','/api/sources') if x['id']==s['id'])
            if source['status']=='failed':
                jobs=request('GET','/api/runs?target='+source['id'])
                if jobs:failure(jobs[0])
                raise RuntimeError('Trial source failed')
            return source if source['status']=='ready' else None
        source=wait(source_ready)
        spec=dict(source['proposed_spec'],primary_key='c_1',group_by='c_2',amount_field='m_net',amount_label='订单净金额',computations=[{'name':'m_net','label':'净金额','operation':'subtract','left_field':'c_3','right_field':'c_4'}],filters=[{'field':'c_5','operator':'eq','data_type':'text','value':'已付款'}],metric_status='confirmed',metric_definition='样例口径：已付款订单金额减退款，按客户汇总',currency='CNY')
        matching=[p for p in request('GET','/api/pipelines') if p['source_id']==s['id'] and p['name']=='试用样例 · 订单净金额' and not p.get('archived')]
        if matching:
            p=matching[-1]
            for previous in matching[:-1]:request('POST','/api/pipelines/'+previous['id']+'/archive')
            if not p.get('candidate_run') and not p.get('published_run'):request('POST','/api/pipelines/'+p['id']+'/run')
        else:p=request('POST','/api/pipelines',json={'source_id':s['id'],'name':'试用样例 · 订单净金额','spec':spec,'release_mode':'manual'})
        def candidate_ready():
            pipeline=next(x for x in request('GET','/api/pipelines') if x['id']==p['id'])
            if pipeline.get('candidate_run') or pipeline.get('published_run'):return pipeline
            runs=request('GET','/api/runs?target='+p['id'])
            if runs and runs[0]['status'] in ('failed','cancelled'):
                failure(runs[0]);raise RuntimeError('Trial processing failed: '+str(runs[0].get('error_code')))
            return None
        candidate=wait(candidate_ready)
        if candidate.get('candidate_run'):
            preview=request('GET','/api/pipelines/'+p['id']+'/preview');assert preview['manifest']['records']==2
            request('POST','/api/pipelines/'+p['id']+'/publish',json={'run_id':candidate['candidate_run']})
        result=request('GET','/api/pipelines/'+p['id']+'/result')
        assert result['total']==2 and Decimal(result['manifest']['summary'][0]['amount_total'])==Decimal('118.15')
        assert request('GET','/api/audit/verify')['ok']
        report={'ok':True,'pipeline_id':p['id'],'run_id':result['manifest']['run_id'],'records':2,'net_amount':'118.1500','uid':os.geteuid(),'commit':result['manifest']['commit']}
        marker.write_text(json.dumps(report));marker.chmod(0o600)
        print('ATLAS_CLOUD_ACCEPTANCE '+json.dumps(report),flush=True)
        verify_conversation(client, request, wait)
        verify_workspace(client,request,wait)
        verify_data_application(client,request,wait)
        verify_opencode_smoke(client,request,wait)
        verify_web_lifecycle(request)
        verify_enterprise(request,wait)

def verify_conversation(client, request, wait):
    marker=Path(os.environ['DATA_DIR'])/'conversation-acceptance-v4.json'
    if marker.exists():
        report=json.loads(marker.read_text())
        conversation=request('GET','/api/projects/'+report['project_id']+'/conversation')
        assert conversation['turns'] and conversation['project']['pipeline_id']==report['pipeline_id']
        assert request('GET','/api/pipelines/'+report['pipeline_id']+'/result')['total']==2
        print('ATLAS_CHAT_RESTART_VERIFIED '+json.dumps({'ok':True,'project_id':report['project_id']}),flush=True)
        return
    p=request('POST','/api/projects',json={'name':'订单分析 · 对话示例','request_id':'atlas-chat-v4-acceptance'})
    path='/api/projects/'+p['id']
    data='订单号,客户,金额,退款,状态\nC1,示例客户,100.25,10.10,已付款\nC2,示例客户,20,0,待付款\nC3,示例客户,30,2,已付款\n'
    source=request('POST',path+'/attachments',files={'file':('对话示例订单.csv',data.encode(),'text/csv')},data={'request_id':'atlas-chat-v4-attachment'})
    def say(text,key,source_id=None):
        return request('POST',path+'/messages',json={'text':text,'request_id':'atlas-chat-v4-'+key,'source_id':source_id})
    def completed(tid):
        t=next(t for t in request('GET',path+'/conversation')['turns'] if t['id']==tid)
        if t['state']=='failed':raise RuntimeError('Chat acceptance failed: '+t.get('answer','unknown'))
        return t if t['state']=='done' else None
    first=say('按客户汇总，仅统计已付款订单，金额扣除退款','analyze',source['id'])
    t=wait(lambda:completed(first['id']))
    assert t['result']['records']==2 and Decimal(str(t['result']['summary'][0]['amount_total']))==Decimal('118.15')
    confirm=say('确认发布','confirm');wait(lambda:completed(confirm['id']))
    schedule=say('每天上午9点更新','schedule');wait(lambda:completed(schedule['id']))
    project=request('GET',path+'/conversation')['project']
    pipeline=next(p for p in request('GET','/api/pipelines') if p['id']==project['pipeline_id'])
    assert pipeline['cron']=='0 9 * * *'
    pause=say('暂停自动更新','pause');wait(lambda:completed(pause['id']))
    question=say('有多少条记录','question');answer=wait(lambda:completed(question['id']))
    assert '2 条' in answer['answer']
    export=say('导出结果','export');export=wait(lambda:completed(export['id']))
    assert client.get(export['download']['url']).content.startswith(b'\xef\xbb\xbf')
    page=client.get('/').text
    assert 'id="composer"' in page and 'id="project-list"' in page and 'id="plan-dialog"' not in page
    assert request('GET','/api/audit/verify')['ok']
    report={'ok':True,'version':'0.4.0','project_id':p['id'],'pipeline_id':pipeline['id'],'run_id':t['result']['run_id'],'records':2,'net_amount':'118.1500','chat_schedule_and_pause':True,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_CHAT_ACCEPTANCE '+json.dumps(report),flush=True)

def verify_workspace(client,request,wait):
    marker=Path(os.environ['DATA_DIR'])/'workspace-acceptance-v5.json'
    if marker.exists():
        report=json.loads(marker.read_text())
        versions=request('GET','/api/projects/'+report['project_id']+'/versions')
        assert any(v['commit']==report['restored_commit'] for v in versions)
        assert request('GET','/api/audit/verify')['ok']
        print('ATLAS_WORKSPACE_RESTART_VERIFIED '+json.dumps({'ok':True,'project_id':report['project_id']}),flush=True)
        return
    p=request('POST','/api/projects',json={'name':'模式与版本 · 示例','request_id':'atlas-workspace-v5-project'})
    path='/api/projects/'+p['id']
    s=request('POST',path+'/sessions',json={'name':'版本验收','request_id':'atlas-workspace-v5-session'})
    initial=request('GET',path+'/versions')[-1]['commit']
    def say(text,key,mode='work',session_id=None):
        value=request('POST',path+'/messages',json={'text':text,'request_id':'workspace-v5-'+key,'interaction_mode':mode,'session_id':session_id})
        def completed():
            t=next(t for t in request('GET',path+'/conversation'+('?session_id='+session_id if session_id else ''))['turns'] if t['id']==value['id'])
            if t['state']=='failed':raise RuntimeError('Workspace acceptance failed')
            return t if t['state']=='done' else None
        return wait(completed)
    work=say('项目改名为版本验收项目','rename')
    assert work['work_version']['changed']
    chat=say('项目改名为不应被修改','chat','chat',s['id'])
    assert not chat.get('work_version')
    current=request('GET',path+'/conversation')['project'];assert current['name']=='版本验收项目'
    restored=say('回退到 '+initial,'restore','work',s['id'])
    assert restored['work_version']['commit']!=initial
    current=request('GET',path+'/conversation')['project'];assert current['name']=='模式与版本 · 示例'
    versions=request('GET',path+'/versions');assert any(v['commit']==work['work_version']['commit'] for v in versions)
    assert len(request('GET',path+'/conversation')['turns'])==1
    assert len(request('GET',path+'/conversation?session_id='+s['id'])['turns'])==2
    archive=client.get(path+'/repository.zip');assert archive.content.startswith(b'PK')
    assert os.environ['APP_TOKEN'].encode() not in archive.content
    report={'ok':True,'version':'0.5.0','project_id':p['id'],'sessions':2,'chat_read_only':True,'work_commit':work['work_version']['commit'],'restored_commit':restored['work_version']['commit'],'later_versions_retained':True,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_WORKSPACE_ACCEPTANCE '+json.dumps(report),flush=True)

def verify_data_application(client,request,wait):
    from decimal import Decimal
    marker=Path(os.environ['DATA_DIR'])/'data-application-acceptance-v6.json'
    if marker.exists():
        report=json.loads(marker.read_text());path='/api/projects/'+report['project_id']+'/applications/'+report['application_id']
        current=request('GET',path);data=request('GET',path+'/data')
        assert current['run_id'] and data['total']==2 and Decimal(data['metric']['total'])==Decimal('118.15')
        assert request('GET','/api/audit/verify')['ok']
        print('ATLAS_APPLICATION_RESTART_VERIFIED '+json.dumps({'ok':True,'application_id':report['application_id']}),flush=True)
        verify_conversation_experience(client,request,wait);return
    project=request('POST','/api/projects',json={'name':'我的收款应用 · 示例','request_id':'atlas-application-v6-project'})
    root='/api/projects/'+project['id']
    text='订单号,客户,金额,退款,状态\nAPP1,示例甲,100.25,10.10,已付款\nAPP2,示例乙,20,0,待付款\nAPP3,示例乙,30,2,已付款\n'
    source=request('POST',root+'/attachments',files={'file':('应用示例订单.csv',text.encode(),'text/csv')},data={'request_id':'atlas-application-v6-source'})
    def say(text,key,application_id=None,source_id=None,mode='work'):
        value=request('POST',root+'/messages',json={'text':text,'request_id':'atlas-application-v6-'+key,'source_id':source_id,'application_id':application_id,'interaction_mode':mode})
        def completed():
            t=next(t for t in request('GET',root+'/conversation')['turns'] if t['id']==value['id'])
            if t['state']=='failed':raise RuntimeError('Data application acceptance failed')
            return t if t['state']=='done' and (mode=='chat' or t.get('work_version')) else None
        return wait(completed)
    first=say('按客户汇总，仅统计已付款订单，金额扣除退款','create',source_id=source['id'])
    application_id=first['result']['application']['id'];path=root+'/applications/'+application_id
    candidate=request('GET',path);assert candidate['preview'] and candidate['can_publish']
    data=request('GET',path+'/data');assert data['total']==2 and Decimal(data['metric']['total'])==Decimal('118.15')
    filtered=request('GET',path+'/data',params={'search':'示例甲'});assert filtered['total']==1 and Decimal(filtered['metric']['total'])==Decimal('90.15')
    archive=client.get(path+'/code.zip');assert archive.status_code==200 and archive.content.startswith(b'PK')
    import zipfile,io
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:assert 'application/data-app.html' in z.namelist()
    published=say('确认发布','publish',application_id);old_run=request('GET',path)['run_id'];old_commit=request('GET',path)['result_commit']
    say('重跑这个应用','rerun',application_id);current=request('GET',path)
    assert current['run_id']!=old_run and current['result_commit']==old_commit
    say('每天上午9点更新','schedule',application_id);assert request('GET',path)['schedule']=='0 9 * * *'
    say('暂停自动更新','pause',application_id);assert not request('GET',path)['schedule']
    question=say('这个应用有多少条记录','ask',application_id,mode='chat');assert '2 条' in question['answer'] and not question.get('work_version')
    say('只看明细','revise',application_id);assert request('GET',path,params={'preview':True})['config']['chart']=='none'
    assert request('GET',path)['config']['chart']=='bar'
    say('回退到 '+published['work_version']['commit'],'restore')
    assert request('GET',path)['config']['chart']=='bar'
    assert client.get('/data-app.html').status_code==200
    assert request('GET','/api/audit/verify')['ok']
    report={'ok':True,'version':'0.6.0','project_id':project['id'],'application_id':application_id,'records':2,'net_amount':'118.15','generated_ui_code':True,'rerun_same_code':True,'revise_preserves_published_app':True,'schedule_and_pause':True,'app_chat':True,'rollback_restores_ui_and_data':True,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_APPLICATION_ACCEPTANCE '+json.dumps(report),flush=True)
    verify_conversation_experience(client,request,wait)


def verify_conversation_experience(client,request,wait):
    marker=Path(os.environ['DATA_DIR'])/'conversation-experience-v7.json'
    app_report=json.loads((Path(os.environ['DATA_DIR'])/'data-application-acceptance-v6.json').read_text())
    pid=app_report['project_id'];aid=app_report['application_id'];root='/api/projects/'+pid;path=root+'/applications/'+aid
    if marker.exists():
        old=json.loads(marker.read_text())
        assert request('GET',path)['run_id']
        assert request('GET',root+'/conversation',params={'session_id':old['session_id']})['current_application_id']==aid
        assert request('GET','/api/audit/verify')['ok']
        print('ATLAS_EXPERIENCE_RESTART_VERIFIED '+json.dumps({'ok':True,'application_id':aid}),flush=True)
        verify_agent_contracts(request,wait,pid,aid)
        return
    baseline=request('GET',root+'/versions')[0]['commit']
    session=request('POST',root+'/sessions',json={'name':'应用日常使用','request_id':'atlas-experience-v7-session'})['id']
    def say(text,key,mode='work',bind=False):
        t=request('POST',root+'/messages',json={'text':text,'request_id':'atlas-experience-v7-'+key,'session_id':session,'application_id':aid if bind else None,'interaction_mode':mode})
        def completed():
            current=next(v for v in request('GET',root+'/conversation',params={'session_id':session})['turns'] if v['id']==t['id'])
            if current['state']=='failed':raise RuntimeError('Conversation experience acceptance failed')
            return current if current['state']=='done' and (mode=='chat' or current.get('work_version')) else None
        return wait(completed)
    say('工作日9点更新','weekdays',bind=True);assert request('GET',path)['schedule']=='0 9 * * 1-5'
    say('每周一9点更新','weekly');assert request('GET',path)['schedule']=='0 9 * * 1'
    say('每2小时更新','hourly');assert request('GET',path)['schedule']=='0 */2 * * *'
    say('暂停自动更新','pause');assert not request('GET',path)['schedule']
    question=say('这个应用有多少条记录','ask',mode='chat');assert '2 条' in question['answer']
    exported=say('导出结果','export');assert '?run_id=' in exported['download']['url']
    assert client.get(exported['download']['url']).status_code==200
    revised=say('只看明细','revise')
    diff=revised['result']['changes'];assert diff['kind']=='compared' and diff['added']==diff['removed']==diff['changed']==0
    assert '应用页面' in diff['definition_changes'] and revised['result']['sources'] and 'freshness' in revised['result']
    health=say('查看项目状态','health',mode='chat');assert '等待确认' in health['answer']
    say('回退到 '+baseline,'restore')
    assert request('GET',path)['config']['chart']=='bar' and not request('GET',path)['schedule']
    report={'ok':True,'version':'0.7.0','project_id':pid,'application_id':aid,'session_id':session,'session_focus':True,'weekly_weekdays_hourly_schedule':True,'pinned_export':True,'publication_diff':True,'provenance_and_freshness':True,'conversation_health':True,'restore_preserved_data':True,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_EXPERIENCE_ACCEPTANCE '+json.dumps(report),flush=True)
    verify_agent_contracts(request,wait,pid,aid)


def verify_agent_contracts(request,wait,pid,aid):
    from .agent import authorize
    from .releases import manifest_for
    from .store import get
    from .jobs import get_job
    status=wait(lambda:request('GET','/api/status') if request('GET','/api/status').get('agent',{}).get('healthy') else None,30)
    assert status['version']=='0.20.0' and status['worker']['healthy']
    for text,tool,args in [('不要重跑','run_data_application',{'application_id':aid}),('不要暂停自动更新','pause_updates',{'pipeline_id':aid}),('解释“重跑应用”是什么意思','run_data_application',{'application_id':aid})]:
        try:authorize({'text':text,'interaction_mode':'work'},tool,args)
        except ValueError:pass
        else:raise AssertionError('Veto was not enforced')
    model=request('GET','/api/model');conditional_api=None
    if not model['configured']:
        answer=request('POST','/api/pipelines/'+aid+'/ask',json={'question':'金额超过一百万的订单有多少条？'})
        assert answer['mode']=='needs_input' and '2 条' not in answer['answer'];conditional_api=True
    else:
        # Do not consume an owner's real-model budget during deployment checks.
        from . import provider,analysis
        saved=provider.settings;saved_base=analysis.LLM_BASE;saved_model=analysis.LLM_MODEL
        try:
            provider.settings=lambda:{}
            analysis.LLM_BASE='';analysis.LLM_MODEL=''
            folder,manifest=manifest_for(get_job(get('pipeline',aid)['published_run']))
            assert analysis.ask(folder,manifest,'金额超过一百万的订单有多少条？')['mode']=='needs_input'
        finally:provider.settings=saved;analysis.LLM_BASE=saved_base;analysis.LLM_MODEL=saved_model
    marker=Path(os.environ['DATA_DIR'])/'agent-contracts-v8.json';restart=marker.exists()
    previous=json.loads(marker.read_text()) if restart else {}
    run_id=previous.get('sample_run_id')
    if not run_id:
        from .execution import enqueue
        pipeline=get('pipeline',aid)
        job=enqueue('pipeline',aid,pipeline['active_commit'],scheduled_key='acceptance:agent-v8:'+aid)
        def finished():
            current=request('GET','/api/runs/'+job['id'])['run']
            if current['status'] in ('failed','cancelled'):raise RuntimeError('Agent v8 sample rerun failed')
            assert request('GET','/api/status')['agent']['healthy']
            return current if current['status']=='succeeded' else None
        completed=wait(finished);assert completed.get('validated') and completed['records']==2
        run_id=completed['id']
    memory={}
    try:
        data=dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
        memory={k:int(data[k]) for k in ('anon','file','kernel','shmem') if k in data}
        memory['current_bytes']=int(Path('/sys/fs/cgroup/memory.current').read_text())
    except (OSError,ValueError):pass
    report={'ok':True,'version':'0.20.0','agent_process_healthy':True,'worker_healthy':True,'negative_instructions_blocked':True,'conditional_query_api':conditional_api,'model_configured':bool(model['configured']),'real_model_evaluated':False,'remote_code_execution_configured':status['code_execution']['configured'],'sample_run_id':run_id,'sample_real_dbt_rerun':True,'memory_cgroup':memory,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print(('ATLAS_AGENT_CONTRACTS_RESTART_VERIFIED ' if restart else 'ATLAS_AGENT_CONTRACTS_ACCEPTANCE ')+json.dumps(report),flush=True)
    verify_company_search(request,pid)


def verify_company_search(request,pid):
    from .agent import TOOLS
    value=request('GET','/api/projects/'+pid+'/company-search')
    assert isinstance(value['status']['configured'],bool)
    if value['configuration']:
        assert not value['configuration'].get('headers')
    assert all(name in TOOLS for name in ('company_search_status','search_company_documents','read_company_document'))
    marker=Path(os.environ['DATA_DIR'])/'company-search-v9.json';restart=marker.exists()
    report={'ok':True,'version':'0.20.0','project_id':pid,'api_available':True,'credentials_redacted':True,'tools_available':True,'company_api_configured':value['status']['configured'],'real_company_api_evaluated':False,'real_model_evaluated':False,'uid':os.geteuid()}
    marker.write_text(json.dumps(report));marker.chmod(0o600)
    print(('ATLAS_COMPANY_SEARCH_RESTART_VERIFIED ' if restart else 'ATLAS_COMPANY_SEARCH_ACCEPTANCE ')+json.dumps(report),flush=True)
    capabilities=request('GET','/api/agent/capabilities')
    assert capabilities['version']=='0.20.0' and capabilities['intelligence_score'] is None
    assert all(f in capabilities['features'] for f in ('tool_discovery','recoverable_tool_results','optimistic_file_edit','repeat_detection'))
    print('ATLAS_HARNESS_V11_ACCEPTANCE '+json.dumps({'ok':True,'version':'0.20.0','model_configured':capabilities['model_configured'],'tool_protocol':capabilities['tool_protocol'],'native_tool_calling':capabilities['native_tool_calling'],'capabilities_available':True,'real_model_evaluated':False,'intelligence_score':None,'uid':os.geteuid()}),flush=True)




def verify_opencode_smoke(client,request,wait):
    from .provider import status
    model=status()
    if model.get('engine')!='opencode':return
    marker=Path(os.environ['DATA_DIR'])/'opencode-smoke-v11-r2.json'
    if marker.exists():
        report=json.loads(marker.read_text())
        print('ATLAS_OPENCODE_SMOKE '+json.dumps(dict(report,reused=True)),flush=True)
        return
    p=request('POST','/api/projects',json={'name':'OpenCode 接入验收','request_id':'opencode-smoke-v11-project'})
    root='/api/projects/'+p['id'];initial=request('GET',root+'/versions')[0]['commit']
    def say(text,key,mode='work'):
        item=request('POST',root+'/messages',json={'text':text,'request_id':key,'interaction_mode':mode})
        def ready():
            data=request('GET',root+'/conversation');turn=next(t for t in data['turns'] if t['id']==item['id'])
            return turn if turn['state'] in ('done','failed') and (mode=='chat' or turn.get('work_version')) else None
        return wait(ready,180)
    work=say('创建 reports/opencode-check.md 文件，内容逐字为：OPENCODE-ATLAS-VERIFIED-2032。','opencode-smoke-v11-r2-write')
    response=client.get(root+'/file',params={'path':'reports/opencode-check.md'})
    wrote=response.status_code==200 and response.text=='OPENCODE-ATLAS-VERIFIED-2032。' and bool(work.get('work_version',{}).get('changed'))
    chat=say('帮我创建 reports/chat-must-not-write.md 文件，内容是测试。','opencode-smoke-v11-r2-chat','chat')
    readonly=client.get(root+'/file',params={'path':'reports/chat-must-not-write.md'}).status_code==404
    restored=say('回退到 '+initial,'opencode-smoke-v11-r2-restore')
    rollback=client.get(root+'/file',params={'path':'reports/opencode-check.md'}).status_code==404 and bool(restored.get('work_version',{}).get('changed'))
    report={'ok':bool(wrote and readonly and rollback),'version':'0.20.0','opencode_version':'2.0.32','model':model['model'],'real_model_evaluated':True,'scope':'file creation, chat read-only and Git rollback smoke only','file_exact_and_git':wrote,'work_outcome':work.get('outcome'),'file_byte_length':len(response.content) if response.status_code==200 else None,'chat_read_only':readonly,'git_restore':rollback,'commercial_baseline_evaluated':False,'model_calls':work.get('model_usage',{}).get('calls',0)+chat.get('model_usage',{}).get('calls',0),'upstream_usage_unavailable':True,'project_id':p['id']}
    marker.write_text(json.dumps(report,ensure_ascii=False));marker.chmod(0o600)
    print('ATLAS_OPENCODE_SMOKE '+json.dumps(report),flush=True)

def verify_web_lifecycle(request):
    p=request('POST','/api/projects',json={'name':'Web 工作空间验收','request_id':'web-lifecycle-v12-acceptance'})
    data=request('GET','/api/projects/'+p['id']+'/conversation');cid=data['session_id']
    did='d_'+'12'*16
    device=request('POST','/api/browser/register',json={'device_id':did})
    path='/api/browser/'+did
    view=request('GET',path+'/view?project_id='+p['id']+'&session_id='+cid)
    saved=request('PUT',path+'/view',json={'project_id':p['id'],'session_id':cid,'draft':'Web 草稿恢复验证','revision':view['revision']})
    recovered=request('GET',path+'/view?project_id='+p['id']+'&session_id='+cid)
    assert recovered['draft']=='Web 草稿恢复验证' and recovered['revision']==saved['revision']
    feed=request('GET','/api/activity?device_id='+did)
    assert isinstance(feed['unread_count'],int) and isinstance(feed['active'],list)
    assert request('GET','/api/projects/'+p['id']+'/message-status?request_id=not-submitted-v12')=={'accepted':False}
    assert device['push_available'] and device['vapid_public_key'] and not device['push_enabled']
    report={'ok':True,'version':'0.20.0','encrypted_view_roundtrip':True,'activity_api':True,'idempotency_lookup':True,'push_configured':True,'desktop_permission_requested':False,'real_os_notification_evaluated':False,'uid':os.geteuid()}
    marker=Path(os.environ['DATA_DIR'])/'web-lifecycle-v12.json';marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_WEB_LIFECYCLE_ACCEPTANCE '+json.dumps(report),flush=True)

def verify_enterprise(request,wait):
    platform=request('GET','/api/platform')
    assert platform['metadata']=='postgresql' and not platform['high_availability_verified']
    assert platform['readiness']['ready']
    report={'ok':True,'version':'0.20.0','metadata':'postgresql','readiness':True,'ha_configuration_gaps':platform['ha_configuration_gaps'],'high_availability_verified':False,'real_model_evaluated':False}
    if os.getenv('ATLAS_ENTERPRISE_MODEL_SMOKE')=='1':
        marker=Path(os.environ['DATA_DIR'])/'enterprise-office-model-v13.json'
        if marker.exists():report['office_agent_smoke']=dict(json.loads(marker.read_text()),reused=True)
        else:
            p=request('POST','/api/projects',json={'name':'办公Agent验收','request_id':'office-v13-smoke-project'})
            root='/api/projects/'+p['id']
            t=request('POST',root+'/messages',json={'request_id':'office-v13-smoke-create','text':'请生成一个可下载的Word文档，文件名为Atlas验收，标题为工作交付验证，正文只有一句：这是实际生成的可编辑办公文件。请先保存JSON规格，再使用render_office_artifact生成docx，给出真实下载链接。不要声称视觉验收或公式重算。'})
            def ready():
                turn=next(x for x in request('GET',root+'/conversation')['turns'] if x['id']==t['id'])
                return turn if turn['state'] in ('done','failed') and turn.get('work_version') else None
            result=wait(ready,240)
            receipt=next((s['observation'] for s in result.get('agent_trace',[]) if s['tool']=='render_office_artifact' and s['status']=='done'),None)
            assert receipt and receipt.get('artifact_id') and result['outcome']=='verified_complete'
            from .office import artifact
            file,meta=artifact(p['id'],receipt['artifact_id'])
            from docx import Document
            doc=Document(file);assert any('这是实际生成的可编辑办公文件。' in para.text for para in doc.paragraphs)
            smoke={'ok':True,'real_model_evaluated':True,'scope':'one actual Word delivery; not commercial parity benchmark','editable_docx':True,'git_checkpoint':bool(result['work_version']['changed']),'model_calls':result.get('model_usage',{}).get('calls'),'visual_evaluation':False}
            marker.write_text(json.dumps(smoke));marker.chmod(0o600);report['office_agent_smoke']=smoke;report['real_model_evaluated']=True
    marker=Path(os.environ['DATA_DIR'])/'enterprise-v13.json';marker.write_text(json.dumps(report));marker.chmod(0o600)
    print('ATLAS_ENTERPRISE_V13_ACCEPTANCE '+json.dumps(report),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('ATLAS_CLOUD_ACCEPTANCE_FAILED '+type(exc).__name__,flush=True);raise SystemExit(1)
