"""Server-owned task contracts and evidence receipts. Model prose is not proof."""
import hashlib,re
from .store import get,patch,all_items,now

def literal_file(text):
    """Only an explicit literal-content instruction; never infer prose intent."""
    match=re.search(r'内容逐字为[：:] ?([\s\S]+)$',text)
    if not match:return None
    paths=set(re.findall(r'(?<![A-Za-z0-9_./-])([A-Za-z0-9_][A-Za-z0-9_./-]{0,180}\.(?:md|txt|py|sql|json|csv|yaml|yml))(?![A-Za-z0-9_.])',text[:match.start()]))
    if len(paths)!=1:return None
    content=match[1]
    if content.startswith('```'):
        fenced=re.fullmatch(r'```[^\n]*\n([\s\S]*?)\n```\s*',content)
        if not fenced:return None
        content=fenced[1]
    elif '\n' in content:return None
    path=next(iter(paths))
    if '..' in path.split('/') or path.startswith('/'):return None
    return {'path':path,'sha256':hashlib.sha256(content.encode()).hexdigest(),'bytes':len(content.encode())}

def ensure(turn):
    if turn.get('task_contract'):return turn['task_contract']
    text=turn.get('work_request',turn['text']);kind='answer'
    if turn.get('interaction_mode','work')=='work':
        from .intents import denied
        for k,pattern,action in [
            ('metric_confirmation',r'^确认指标[：:]','confirm_metric'),
            ('publish',r'^(确认|确认发布|发布|发布结果|可以发布|确认使用|就这样)[。！!\s]*$','publish'),
            ('pause',r'(暂停|关闭|停止|取消).*(更新|调度|定时)','pause'),
            ('execution',r'(运行|执行|测试).*(脚本|\.py)','refresh'),
            ('run',r'重跑|重新运行|刷新|更新数据','refresh'),
            ('file',r'(创建|写|保存|修改|新增|生成|删除).*(报告|文件|代码|脚本|\.md|\.py|\.sql)','write'),
            ('build',r'分析这|整理|汇总|创建.*应用|生成.*应用|修改.*应用|调整.*应用|(?:做|搭建|构建|开发|实现).*(?:应用|看板|仪表盘|系统|报表)|扣除退款|筛选','analyze'),
            ('schedule',r'每天|工作日|每周|每\d+小时','schedule'),
        ]:
            if re.search(pattern,text) and not denied(text,action):kind=k;break
        if re.search(r'脚本|\.py',text) and re.search(r'执行|运行|测试',text) and not denied(text,'refresh'):kind='execution'
    if turn.get('interaction_mode','work')=='work' and re.search(r'Word|Excel|PPT|PowerPoint|word|excel|ppt|docx|xlsx|pptx|幻灯片|演示文稿',text) and re.search(r'创建|生成|写|做|制作|导出',text):kind='office'
    applications=[p for p in all_items('pipeline') if p.get('project_id')==turn['project_id'] and not p.get('archived')]
    explicit=[p['id'] for p in applications if any(name and name in text for name in (p.get('name'),(p.get('application_spec') or {}).get('title')))]
    allowed=list(dict.fromkeys(([turn['application_id']] if turn.get('application_id') else [])+explicit))
    allowed=[target for target in allowed if not any(re.search(r'(?:不要|别|不允许).{0,8}(?:动|操作|修改|调整|重跑|刷新).{0,4}'+re.escape(name),text) for p in applications if p['id']==target for name in (p.get('name'),(p.get('application_spec') or {}).get('title')) if name)]
    if not allowed and not explicit and not turn.get('application_id') and len(applications)==1:allowed=[applications[0]['id']]
    if kind=='answer' and allowed and re.search(r'多少|几条|合计|平均|排名|最大|最小',text) and not re.search(r'解释|如何|怎么|什么意思',text):kind='query'
    requirements=[] if kind=='answer' else [kind]
    local_export_requested=turn.get('interaction_mode','work')=='work' and re.search(r'(?:保存|导出|写入|同步).{0,30}(?:本地|所选|已选).{0,8}(?:文件夹|目录)|(?:本地文件夹|本地目录|所选文件夹).{0,20}(?:保存|导出|写入)',text) and not denied(text,'local_export')
    if local_export_requested:requirements.append('local_export')
    if kind in ('build','run','file'):
        from .cadence import parse
        try:
            if parse(text,embedded=True):requirements.append('schedule')
        except ValueError:pass
    from .task_budget import KEYS,effective_limits
    contract={'version':1,'kind':kind,'requirements':requirements,'goal':text[:12000],'application_ids':allowed,
              'source_id':turn.get('source_id'),'created_at':now(),
              'acceptance':{'metric_confirmation':'正式指标与用户原文匹配，服务端保存对应代码版本的确认回执','office':'生成实际Office文件，哈希和格式验证通过','execution':'独立隔离执行回执成功，执行输入与当前文件一致','answer':'有依据的回答','query':'执行真实只读查询，或返回范围一致的内置统计','file':'文件内容与写入回执一致','build':'真实运行通过验证并产生候选结果','run':'本轮真实运行成功','schedule':'已保存有效调度或待发布计划','publish':'本轮候选已发布','pause':'调度已停止'}[kind],
              'budget_policy':'unlimited','budgets':dict.fromkeys(KEYS,None),
              'initial_applications':[p['id'] for p in applications]}
    if turn.get('gateway_client_id') or turn.get('gateway_limits') or turn.get('collaboration_parent_turn_id'):
        contract['budget_policy']='bounded'
        contract['budgets']=effective_limits(dict(turn,task_contract=contract))
    exact=literal_file(text) if kind=='file' else None
    if local_export_requested:contract['acceptance']+='；本地成果清单及全部文件哈希回读一致，且没有部分失败'
    if exact:
        contract['literal_file']=exact;contract['requirements'].append('file_exact')
        contract['acceptance']+='；指定文件必须逐字匹配用户原文（包括标点）'
    from .company_search import requires_evidence
    if requires_evidence(dict(turn,task_contract=contract)):
        contract['requirements'].append('company_evidence')
        contract['acceptance']+='；公司依据需读取原文并通过引用核对'
    patch('turn',turn['id'],task_contract=contract)
    return contract

def check_target(turn,target):
    contract=ensure(turn);allowed=contract['application_ids']
    if not target:return
    if target not in allowed:
        # A fresh app created in this task is a legitimate subsequent target.
        p=get('pipeline',target)
        origin=turn.get('agent_origin',turn['id'])
        operations={s.get('operation_id') for s in turn.get('agent_trace',[])}
        from .conversation import stable
        operations.update(stable('op_',origin,s['id']) for s in turn.get('agent_trace',[]) if s.get('id'))
        if p.get('project_id')==turn['project_id'] and p.get('origin_turn') in operations|{origin,turn['id']}:return
        raise ValueError('目标应用不在本轮任务范围；请在对话中明确指定该应用')

def verify(turn):
    contract=ensure(turn);kind=contract['kind'];trace=turn.get('agent_trace',[])
    done=[s for s in trace if s.get('status')=='done'];receipts=[]
    if turn.get('knowledge_citations'):receipts.append({'kind':'company_evidence','evidence_ids':list(dict.fromkeys(c['evidence_id'] for c in turn['knowledge_citations']))})
    for s in done:
        obs=s.get('observation') or {}
        if s['tool']=='run_python_file' and obs.get('remote_receipt') and obs.get('exit_code')==0:
            from .workspace import read_file
            try:
                if obs.get('file_hashes') and all(hashlib.sha256(read_file(turn['project_id'],path).encode()).hexdigest()==digest for path,digest in obs['file_hashes'].items()):receipts.append({'kind':'execution','execution_id':obs['execution_id'],'input_sha256':obs['input_sha256']})
            except (ValueError,FileNotFoundError):pass
        if (s['tool']=='query_sql' and isinstance(obs.get('rows'),list)) or (s['tool']=='ask_data_application' and (obs.get('mode')=='llm_sql' or obs.get('scope')=='entire_current_version')):
            receipts.append({'kind':'query','tool':s['tool'],'sql':obs.get('sql') or s['arguments'].get('sql'),'run_id':(obs.get('result') or {}).get('run_id')})
        if s['tool']=='render_office_artifact' and obs.get('artifact_id'):
            from .office import artifact
            try:
                file,meta=artifact(turn['project_id'],obs['artifact_id'])
                if meta['sha256']==obs.get('sha256'):receipts.append({'kind':'office','artifact_id':meta['id'],'sha256':meta['sha256']})
            except (ValueError,KeyError,FileNotFoundError):pass
        if s['tool']=='write_file':
            from .workspace import read_file
            try:
                content=read_file(turn['project_id'],s['arguments']['path'])
                if content==s['arguments']['content']:receipts.append({'kind':'file','path':s['arguments']['path'],'sha256':hashlib.sha256(content.encode()).hexdigest()})
            except (ValueError,FileNotFoundError):pass
        if s['tool']=='delete_file':
            from .workspace import path_for
            if not path_for(turn['project_id'],s['arguments']['path']).exists():receipts.append({'kind':'file','deleted':s['arguments']['path']})
        if s['tool']=='edit_file' and obs.get('after_sha256'):
            from .workspace import read_file
            try:
                if hashlib.sha256(read_file(turn['project_id'],s['arguments']['path']).encode()).hexdigest()==obs['after_sha256']:receipts.append({'kind':'file','path':s['arguments']['path'],'sha256':obs['after_sha256']})
            except (ValueError,FileNotFoundError):pass
        if s['tool']=='export_local_workspace' and obs.get('id'):
            from .local_workspace import verify_export
            try:
                checked=verify_export(turn['project_id'],obs['id'])
                if not checked['files']:continue
                receipts.append({'kind':'file','export_id':checked['id'],'manifest_sha256':checked['manifest_sha256'],
                                 'files':checked['files'],'bytes':checked['bytes']})
                receipts.append({'kind':'local_export','export_id':checked['id'],'manifest_sha256':checked['manifest_sha256']})
            except (ValueError,KeyError,OSError):pass
    run_id=turn.get('run_id')
    if run_id:
        from .jobs import get_job
        job=get_job(run_id)
        if job['status'] in ('validated','succeeded') and job.get('validated'):
            from pathlib import Path
            from .config import DATA
            import json
            folder=(DATA/job.get('artifact_dir','')).resolve()
            manifest=folder/'output.json'
            if folder.is_relative_to((DATA/'runs').resolve()) and manifest.is_file():
                data=json.loads(manifest.read_text(encoding='utf-8'))
                if data.get('commit')==job.get('commit') and data.get('run_id')==run_id:
                    receipts.append({'kind':'run','run_id':run_id,'application_id':job['target'],'commit':job.get('commit')})
                    p=get('pipeline',job['target']);pending=p.get('pending_schedule') or {}
                    if pending.get('authorized_turn')==turn['id']:
                        from .cadence import parse
                        plan=parse(turn.get('work_request') or turn.get('workflow_goal') or turn['text'],embedded=True)
                        if plan and plan['cron']==pending.get('cron'):receipts.append({'kind':'schedule','application_id':p['id'],'cron':plan['cron'],'pending_publication':True})
    for effect in turn.get('mutation_receipts',[]):
        p=get('pipeline',effect['application_id']);kind_effect=effect['kind']
        valid=(kind_effect=='publish' and p.get('published_run')==effect.get('run_id')) or (kind_effect=='schedule' and (p.get('cron')==effect.get('cron') or (p.get('pending_schedule') or {}).get('cron')==effect.get('cron'))) or (kind_effect=='pause' and not p.get('cron') and not p.get('pending_schedule'))
        if valid:receipts.append(effect)
    evidence=turn.get('data_evidence') or {}
    if evidence.get('mode')=='llm_sql' or evidence.get('scope')=='entire_current_version':receipts.append({'kind':'query','sql':evidence.get('sql'),'run_id':evidence.get('run_id')})
    if set(contract.get('requirements',[kind])) & {'schedule','publish','pause'}:
        for s in done:
            mapping={'schedule_daily':'schedule','schedule_updates':'schedule','publish_result':'publish','pause_updates':'pause'}
            effect=mapping.get(s['tool'])
            if effect not in contract.get('requirements',[kind]):continue
            target=s['arguments'].get('pipeline_id') or turn.get('application_id') or get('project',turn['project_id']).get('pipeline_id')
            if not target:continue
            p=get('pipeline',target)
            valid=(effect=='schedule' and bool(p.get('cron') or p.get('pending_schedule'))) or (effect=='publish' and bool(p.get('published_run')) and not p.get('candidate_run')) or (effect=='pause' and not p.get('cron') and not p.get('pending_schedule'))
            if valid:receipts.append({'kind':effect,'application_id':target})
    for s in done:
        if s['tool']=='confirm_metric':
            from .metric_registry import current
            obs=s.get('observation') or {}
            try:
                metric=current(turn['project_id'],s['arguments']['application_id'])
                if metric['status']=='confirmed' and metric['id']==obs.get('id') and metric['commit']==obs.get('commit') and metric.get('confirmation')==obs.get('confirmation'):
                    receipts.append({'kind':'metric_confirmation','metric_id':metric['id'],'commit':metric['commit']})
            except (ValueError,KeyError,OSError):pass
    for s in done:
        if s['tool']=='run_data_workflow':
            from .project_data import result_folder
            obs=s.get('observation') or {}
            try:
                folder,meta=result_folder(turn['project_id'],obs['run_id'])
                if meta['plan_sha256']==obs.get('plan_sha256') and meta['complete']:
                    receipts.append({'kind':'run','run_id':meta['run_id'],'workflow_id':meta['workflow_id'],'plan_sha256':meta['plan_sha256']})
            except (KeyError,ValueError,OSError):pass
        if s['tool']=='export_data_workflow_result':
            from .project_data import export_result
            try:
                file=export_result(turn['project_id'],s['arguments']['run_id'],s['arguments'].get('precision_policy','text_exact'))
                receipts.append({'kind':'office','format':'xlsx','sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
            except (KeyError,ValueError,OSError):pass
        if s['tool']=='render_business_report':
            from .business_reports import report_file
            obs=s.get('observation') or {}
            try:
                report_file(turn['project_id'],obs['id'],'manifest')
                file=report_file(turn['project_id'],obs['id'],'docx')
                receipts.append({'kind':'office','format':'docx','report_id':obs['id'],'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
                receipts.append({'kind':'file','report_id':obs['id'],'run_id':obs['run_id']})
                receipts.append({'kind':'query','report_id':obs['id'],'run_id':obs['run_id'],'scope':'verified_full_result_report_metrics'})
            except (KeyError,ValueError,OSError):pass
    required=['run' if k=='build' else k for k in contract.get('requirements',[])]
    exact=contract.get('literal_file')
    if exact:
        from .workspace import read_file
        try:
            actual=read_file(turn['project_id'],exact['path'])
            if hashlib.sha256(actual.encode()).hexdigest()==exact['sha256']:receipts.append({'kind':'file_exact',**exact})
        except (ValueError,FileNotFoundError):pass
    missing=[k for k in required if not any(x['kind']==k for x in receipts)]
    for action in __import__('agent_service.work_tools',fromlist=['actions']).actions(turn['project_id']):
        if action['turn_id'] in (turn['id'],turn.get('agent_origin'),turn.get('resumed_from')) and action['state']!='succeeded':missing.append('external_action_'+action['state'])
    verified=not missing
    result={'verified':verified,'criteria':contract['acceptance'],'missing':missing,'receipts':receipts,'checked_at':now()}
    patch('turn',turn['id'],verification=result)
    return result
