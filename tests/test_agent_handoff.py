"""Exact, task-scoped Agent exchange uses full verified bytes and typed values."""
import base64
import copy
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest
from sqlalchemy import select

from agent_service import agent_handoff as handoff,project_data as data,office,workspace
from agent_service.actor_context import actor_scope
from agent_service.config import DATA
from agent_service.jobs import jobs,engine,save,get_job
from agent_service.result_delivery import write_handoff,read_all_handoff
from agent_service.sources import revision
from agent_service.store import put,get,patch,delete,all_items,seal,uid,now


@pytest.fixture(autouse=True)
def clean():
    kinds=('source','source_version','agent_handoff_import','agent_handoff_artifact_import','agent_handoff_result_import','agent_imported_result','agent_task_artifact','project','turn','data_workflow','office_artifact','business_report')
    previous={kind:{item['id'] for item in all_items(kind)} for kind in kinds}
    with engine.connect() as connection:previous_jobs=set(connection.execute(select(jobs.c.id)).scalars())
    yield
    with engine.begin() as connection:
        query=jobs.delete()
        if previous_jobs:query=query.where(jobs.c.id.not_in(previous_jobs))
        connection.execute(query)
    for kind in kinds:
        for item in all_items(kind):
            if item['id'] not in previous[kind]:delete(kind,item['id'])


def project():return put('project',{'name':'Agent data exchange','created_at':now()},'p_'+uid())['id']


def turn(pid=None,**extra):
    return put('turn',{'project_id':pid or project(),'text':'请生成并保存报告','created_at':now(),'state':'done','outcome':'answered','actor_id':'owner','agent_trace':[],**extra},'t_'+uid())


def source(pid,raw=b'id,amount\n1,100.000000000001\n',name='收入.csv'):
    stored=uid()+Path(name).suffix;path=DATA/'uploads'/stored;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    return revision(put('source',{'project_id':pid,'name':name,'kind':'file','bytes':len(raw),'content_hash':hashlib.sha256(raw).hexdigest(),
        'secret_config':seal({'stored_name':stored}),'status':'ready','created_at':now()},'s_'+uid()))


def result(pid,count=40):
    # Create actual source-bound workflow evidence instead of fabricating a
    # successful run with no way to prove what inputs it came from.
    from agent_service.data_workflows import WorkflowPlan,digest,execute
    raw_rows=[{'id':'ID'+str(i),'amount':'7.500000000001','day':'2026-10-01','paid':True} for i in range(count)]
    source_record=source(pid,json.dumps(raw_rows,ensure_ascii=False).encode(),'evidence.json')
    rows=[{'id':v['id'],'amount':Decimal(v['amount']),'day':date.fromisoformat(v['day']),'paid':v['paid']} for v in raw_rows]
    schema={'columns':[{'name':'id','data_type':'text','meaning':'Unique record'},
        {'name':'amount','data_type':'decimal','meaning':'Net revenue','unit':'CNY','period':'2026-10','aggregation':'additive'},
        {'name':'day','data_type':'date','meaning':'Posting day'},
        {'name':'paid','data_type':'boolean','meaning':'Paid flag'}], 'grain':['id'],'meaning':'One invoice per id'}
    mapping={'c_1':'id','c_2':'amount','c_3':'day','c_4':'paid'}
    doc=data.WorkflowDocument.model_validate({'version':1,'plan':{'name':'Exchange source rows','inputs':{'src':schema},
        'stages':[{'id':'keep','kind':'select','description':'Preserve verified source rows','input':'src',
                   'columns':['id','amount','day','paid']}],'output':'keep'},
        'bindings':{'src':{'source_id':source_record['id'],'column_map':mapping}}})
    document=doc.model_dump();plan_sha=digest(document)
    input_rows,_,proof,provenance=data.read_input(pid,doc.bindings['src'],frozen_source=source_record)
    executed=execute(doc.plan,{'src':input_rows})
    assert len(executed['output']['rows'])==len(rows)
    assert all(actual['id']==expected['id'] and Decimal(actual['amount'])==expected['amount'] and
               actual['day']==expected['day'].isoformat() and actual['paid']==expected['paid']
               for actual,expected in zip(executed['output']['rows'],rows))
    schema=executed['output']['schema'];rid='r_'+uid();wid='w_'+uid();folder=DATA/'runs'/rid;folder.mkdir(parents=True)
    context={'project_id':pid,'plan_sha256':plan_sha,'input_evidence':{'src':proof},
             'output_semantics':schema,'requires_semantic_review':False,'requires_source_review':False}
    manifest=write_handoff(folder/'handoff',rows,schema,context=context,provenance=provenance,stages=executed['stages'])
    meta={'run_id':rid,'workflow_id':wid,'project_id':pid,'plan_sha256':plan_sha,'handoff_sha256':manifest['manifest_sha256'],
        'records':count,'created_at':now(),'source_revisions':{source_record['id']:source_record['revision']},
        'complete':True,'requires_semantic_review':False,'semantic_review_reasons':[]}
    (folder/'data-result.json').write_text(json.dumps(meta))
    save({'id':rid,'kind':'data_workflow','target':wid,'commit':plan_sha,'project_id':pid,'status':'validated','validated':True,
        'records':count,'artifact_dir':str(folder.relative_to(DATA)),'data_result':meta,'created_at':now(),
        'workflow_document':document,'source_snapshot':{source_record['id']:source_record},
        'source_revisions':{source_record['id']:source_record['revision']}})
    return rid,rows,schema


def resign(bundle):
    manifest={k:([{a:b for a,b in item.items() if a!='content_base64'} for item in v] if k=='items' else v)
              for k,v in bundle.items() if k!='manifest_sha256'}
    bundle['manifest_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return bundle


def replace_item_bytes(bundle,raw):
    item=bundle['items'][0];item['content_base64']=base64.b64encode(raw).decode();item['bytes']=len(raw);item['sha256']=hashlib.sha256(raw).hexdigest()
    if item['kind']=='source':item['source_version']['content_sha256']=item['sha256']
    return resign(bundle)


def test_complete_source_bytes_freeze_and_import_retries_without_replacement(monkeypatch):
    pid=project();s=source(pid);t=turn(pid,source_ids=[s['id']],attached_sources=[{k:s[k] for k in ('id','revision','revision_id','content_hash')}])
    original=data.source_path(s).read_bytes();bundle=handoff.build_input(t,[s['id']],[])
    assert base64.b64decode(bundle['items'][0]['content_base64'])==original
    assert handoff.validate_bundle(bundle)['integrity_verified']
    assert handoff.build_input(t,[s['id']],[])==bundle
    queued=[];monkeypatch.setattr('agent_service.execution.enqueue',lambda kind,sid:queued.append((kind,sid)))
    target=project();first=handoff.import_bundle(target,bundle,scope_id='gateway:test',request_id='r1')
    assert first['source_ids'] and not first['result_ids']
    imported=get('source',first['source_ids'][0])
    assert imported['project_id']==target and imported['skip_model_planning'] is True
    assert data.source_path(imported).read_bytes()==original and imported['revision_id']
    assert handoff.import_bundle(target,bundle,scope_id='gateway:test',request_id='r1')==first
    assert queued==[('probe',imported['id'])]
    changed=replace_item_bytes(copy.deepcopy(bundle),b'id,amount\n2,900\n')
    with pytest.raises(handoff.HandoffError,match='同一交接请求'):handoff.import_bundle(target,changed,scope_id='gateway:test',request_id='r1')
    assert data.source_path(imported).read_bytes()==original


def test_source_revision_and_file_byte_changes_block_export():
    pid=project();s=source(pid);t=turn(pid,source_ids=[s['id']],attached_sources=[{k:s[k] for k in ('id','revision','revision_id','content_hash')}])
    patch('source',s['id'],revision=s['revision']+1)
    with pytest.raises(ValueError,match='附件已变更'):handoff.build_input(t,[s['id']],[])
    patch('source',s['id'],revision=s['revision'])
    data.source_path(s).write_bytes(b'TAMPERED')
    with pytest.raises(ValueError):handoff.build_input(t,[s['id']],[])


def test_sources_and_results_cannot_cross_project_and_database_credentials_never_leave():
    pid=project();other=project();s=source(other);rid,_,_=result(other)
    t=turn(pid)
    with pytest.raises(ValueError):handoff.build_input(t,[s['id']],[])
    with pytest.raises(ValueError):handoff.build_input(t,[],[rid])
    db=put('source',{'project_id':pid,'name':'Private DB','kind':'postgres','secret_config':seal({'password':'NEVER-SENT'})},'s_'+uid())
    with pytest.raises(ValueError,match='连接配置不会发送'):handoff.build_input(t,[db['id']],[])


def test_full_typed_result_retains_every_row_exact_decimal_date_and_semantics():
    pid=project();rid,rows,schema=result(pid);bundle=handoff.build_input(turn(pid),[],[rid])
    receipt=handoff.validate_bundle(bundle)
    assert receipt['items'][0]['result_metadata']['records']==40 and receipt['financial_verified'] is False
    target=project();imported=handoff.import_bundle(target,bundle,scope_id='peer:finance',request_id='exact-result')
    assert not imported['source_ids'] and len(imported['result_ids'])==1
    imported_id=imported['result_ids'][0];folder,metadata=data.result_folder(target,imported_id)
    actual=read_all_handoff(folder/'handoff',metadata['handoff_sha256'],native_types=True)
    assert len(actual['rows'])==40 and actual['rows']==rows
    assert sum(row['amount'] for row in actual['rows'])==Decimal('300.000000000040')
    assert actual['context']['output_semantics']==schema
    assert metadata['requires_semantic_review'] and 'EXTERNAL_AGENT_RESULT_REVIEW_REQUIRED' in metadata['semantic_review_reasons']
    # Imported complete bytes remain inspectable, but the receiving project
    # lacks the original frozen workflow and cannot launder them as verified input.
    with pytest.raises(ValueError,match='精度依据'):
        data.read_input(target,{'result_id':imported_id})
    assert handoff.import_bundle(target,bundle,scope_id='peer:finance',request_id='exact-result')==imported


def test_real_queued_workflow_result_exchanges_more_than_preview_rows(monkeypatch):
    from test_project_data import source as financial_source,document,registered,run,COUNT,TOTAL
    pid=project();s=financial_source(pid);rid=run(pid,registered(pid,document(s['id'],exact_total=True)))['id']
    bundle=handoff.build_input(turn(pid),[],[rid]);target=project()
    accepted=handoff.import_bundle(target,bundle,scope_id='real-peer',request_id='full')
    page=data.read_result(target,accepted['result_ids'][0],limit=100)
    assert page['total']==COUNT and len(page['rows'])==100 and page['next_offset']==100
    folder,metadata=data.result_folder(target,accepted['result_ids'][0])
    actual=read_all_handoff(folder/'handoff',metadata['handoff_sha256'],native_types=True)
    assert len(actual['rows'])==COUNT and sum(Decimal(row['amount']) for row in actual['rows'])==Decimal(TOTAL)


@pytest.mark.parametrize('name',['../income.csv','/tmp/income.csv','x\\income.csv','llm.json','.env','secrets.json'])
def test_peer_names_cannot_select_paths_or_secret_configuration(name):
    pid=project();bundle=handoff.build_input(turn(pid),[source(pid)['id']],[])
    bundle['items'][0]['name']=name;resign(bundle)
    with pytest.raises(handoff.HandoffError):handoff.validate_bundle(bundle)


def test_malformed_base64_hashes_duplicate_ids_and_invalid_counts_are_rejected():
    pid=project();good=handoff.build_input(turn(pid),[source(pid)['id']],[])
    for mutate in ('base64','hash','duplicate','count','incomplete'):
        bundle=copy.deepcopy(good)
        if mutate=='base64':bundle['items'][0]['content_base64']='?'*len(bundle['items'][0]['content_base64'])
        elif mutate=='hash':bundle['items'][0]['sha256']='0'*64
        elif mutate=='duplicate':bundle['items'].append(copy.deepcopy(bundle['items'][0]))
        elif mutate=='count':bundle['items'][0]['bytes']=True
        else:bundle['complete']=False
        resign(bundle)
        with pytest.raises(handoff.HandoffError):handoff.validate_bundle(bundle)


@pytest.mark.parametrize('variant',['traversal','duplicate','symlink','extra'])
def test_result_archive_never_extracts_unexpected_or_special_files(variant):
    pid=project();rid,_,_=result(pid);bundle=handoff.build_input(turn(pid),[],[rid]);raw=base64.b64decode(bundle['items'][0]['content_base64'])
    malicious=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original,zipfile.ZipFile(malicious,'w') as output:
        for name in original.namelist():
            if variant=='symlink' and name=='rows.jsonl':
                info=zipfile.ZipInfo(name);info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16;output.writestr(info,'/etc/passwd')
            else:output.writestr(name,original.read(name))
        if variant=='traversal':output.writestr('../outside.json','PRIVATE')
        elif variant=='duplicate':output.writestr('manifest.json',original.read('manifest.json'))
        elif variant=='extra':output.writestr('unexpected.py','raise RuntimeError()')
    replace_item_bytes(bundle,malicious.getvalue())
    with pytest.raises(handoff.HandoffError):handoff.validate_bundle(bundle)


def test_declared_result_count_or_semantics_cannot_disagree_with_actual_full_content():
    pid=project();rid,_,_=result(pid);bundle=handoff.build_input(turn(pid),[],[rid])
    wrong=copy.deepcopy(bundle);wrong['items'][0]['result_metadata']['records']=25;resign(wrong)
    with pytest.raises(handoff.HandoffError,match='行数'):handoff.validate_bundle(wrong)
    wrong=copy.deepcopy(bundle);wrong['items'][0]['result_metadata']['output_semantics']['columns'][1]['unit']='USD';resign(wrong)
    with pytest.raises(handoff.HandoffError,match='语义'):handoff.validate_bundle(wrong)


def test_oversize_inputs_are_not_replaced_with_a_preview(monkeypatch):
    monkeypatch.setenv('ATLAS_AGENT_HANDOFF_MAX_BYTES','1024')
    pid=project();s=source(pid,raw=b'id,n\n'+b'x,1\n'*300)
    with pytest.raises(handoff.HandoffError,match='超'):handoff.build_input(turn(pid),[s['id']],[])
    first=source(pid,raw=b'id,n\n'+b'x,1\n'*150);second=source(pid,raw=b'id,n\n'+b'y,2\n'*150)
    with pytest.raises(handoff.HandoffError,match='超'):handoff.build_input(turn(pid),[first['id'],second['id']],[])


def test_import_request_identity_is_bound_to_service_actor_and_project(monkeypatch):
    pid=project();bundle=handoff.build_input(turn(pid),[source(pid)['id']],[]);target=project()
    monkeypatch.setattr('agent_service.execution.enqueue',lambda *args:None)
    with actor_scope('u_'+'a'*64):first=handoff.import_bundle(target,bundle,scope_id='peer',request_id='shared')
    with actor_scope('u_'+'b'*64):second=handoff.import_bundle(target,bundle,scope_id='peer',request_id='shared')
    assert first['source_ids']!=second['source_ids']


def test_only_task_referenced_real_office_and_text_artifacts_are_exposed():
    pid=project();t=turn(pid);other=turn(pid)
    workspace.write_file(pid,'reports/spec.json',json.dumps({'kind':'document','title':'Exact report','sections':[{'heading':'Revenue','body':'300.000000000002 CNY'}]}))
    own=office.render(t,'reports/spec.json','Exact report')
    unrelated=office.render(other,'reports/spec.json','Other task report')
    t=patch('turn',t['id'],agent_trace=[{'id':'office','status':'done','tool':'render_office_artifact','arguments':{},'observation':own},
        {'id':'text','status':'done','tool':'write_file','arguments':{'path':'reports/frozen.md','content':'Historical exact report'},'observation':{'path':'reports/frozen.md'}}])
    output=handoff.build_output(t)
    assert len(output['artifacts'])==2 and output['integrity_verified'] and not output['financial_verified']
    assert all('relative_path' not in artifact for artifact in output['artifacts'])
    assert unrelated['artifact_id'] not in json.dumps(output)
    assert handoff.validate_bundle(output['output_bundle'])['files']==2
    for artifact in output['artifacts']:
        content=b'';offset=0
        while True:
            page=handoff.read_artifact(t,artifact['id'],offset,17);content+=base64.b64decode(page['content_base64'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        assert hashlib.sha256(content).hexdigest()==artifact['sha256'] and len(content)==artifact['bytes']
        with pytest.raises(ValueError,match='不属于'):handoff.read_artifact(other,artifact['id'])
    damaged=output['artifacts'][0];record=get('agent_task_artifact',damaged['id']);(DATA/record['relative_path']).write_bytes(b'TAMPERED')
    with pytest.raises(handoff.HandoffError,match='完整性'):handoff.read_artifact(t,damaged['id'])


def test_task_full_result_output_can_be_consumed_as_exact_typed_input():
    pid=project();rid,rows,_=result(pid);t=turn(pid,run_id=rid)
    output=handoff.build_output(t)
    assert output['output_bundle']['items'][0]['kind']=='result'
    imported=handoff.import_bundle(project(),output['output_bundle'],scope_id='return',request_id='typed')
    run=get_job(imported['result_ids'][0]);folder,metadata=data.result_folder(run['project_id'],run['id'])
    assert read_all_handoff(folder/'handoff',metadata['handoff_sha256'],native_types=True)['rows']==rows


def test_large_output_uses_complete_paged_artifacts_instead_of_truncated_inline(monkeypatch):
    pid=project();t=turn(pid,agent_trace=[{'id':'text','status':'done','tool':'write_file','arguments':{'path':'large.md','content':'x'*2000},'observation':{}}])
    monkeypatch.setenv('ATLAS_AGENT_HANDOFF_MAX_BYTES','1024')
    output=handoff.build_output(t)
    assert output['output_bundle'] is None and len(output['artifacts'])==1 and output['artifacts'][0]['bytes']==2000
    assert '未截断' in output['inline_reason']
    page=handoff.read_artifact(t,output['artifacts'][0]['id'],0,2500)
    assert page['page_is_complete_artifact'] and len(base64.b64decode(page['content_base64']))==2000


def test_tampered_immutable_result_output_is_not_silently_omitted():
    pid=project();rid,_,_=result(pid);t=turn(pid,run_id=rid);folder,_=data.result_folder(pid,rid)
    (folder/'handoff/manifest.json').write_text('{}')
    with pytest.raises(ValueError):handoff.build_output(t)


def test_accept_remote_mixed_output_imports_only_full_typed_results_and_binds_parent_manifest():
    pid=project();rid,rows,schema=result(pid);t=turn(pid,run_id=rid)
    t=patch('turn',t['id'],agent_trace=[{'id':'text','status':'done','tool':'write_file','arguments':{'path':'report.md','content':'Untrusted remote narrative'},'observation':{'path':'report.md'}}])
    bundle=handoff.build_output(t)['output_bundle'];assert {i['kind'] for i in bundle['items']}=={'result','artifact'}
    target=project();accepted=handoff.import_results(target,bundle,scope_id='remote:peer',request_id='same-task')
    assert accepted['source_ids']==[] and len(accepted['result_ids'])==1 and accepted['ignored_non_result_files']==1
    assert accepted['parent_manifest_sha256']==bundle['manifest_sha256'] and not accepted['financial_verified']
    folder,metadata=data.result_folder(target,accepted['result_ids'][0]);actual=read_all_handoff(folder/'handoff',metadata['handoff_sha256'],native_types=True)
    assert actual['rows']==rows and actual['context']['output_semantics']==schema and metadata['requires_semantic_review']
    assert handoff.import_results(target,bundle,scope_id='remote:peer',request_id='same-task')==accepted
    changed=copy.deepcopy(bundle);artifact=next(item for item in changed['items'] if item['kind']=='artifact');artifact['name']='changed.md';resign(changed)
    with pytest.raises(ValueError,match='同一交接请求'):handoff.import_results(target,changed,scope_id='remote:peer',request_id='same-task')


def test_remote_ordinary_artifacts_import_exact_bytes_under_real_parent_task_only():
    remote=project();rid,_,_=result(remote);s=source(remote);remote_turn=turn(remote)
    workspace.write_file(remote,'report.json',json.dumps({'kind':'document','title':'远端报告','sections':[{'heading':'收入','body':'300.000000000002 CNY'}]},ensure_ascii=False))
    rendered=office.render(remote_turn,'report.json','远端报告');path,_=office.artifact(remote,rendered['artifact_id']);original=path.read_bytes()
    inputs=handoff.build_input(remote_turn,[s['id']],[rid])
    bundle=handoff._bundle(inputs['items']+[handoff._item('office-result','artifact','远端报告.docx',original),handoff._item('summary-result','artifact','月报.md',b'# Exact\n300.000000000002 CNY')])
    target=project();parent=turn(target);accepted=handoff.import_artifacts(parent,bundle,scope_id='mesh:private-peer-task',request_id='remote-task-17')
    assert accepted['files']==2 and accepted['ignored_non_artifact_files']==2 and len(accepted['artifact_ids'])==2
    assert accepted['parent_manifest_sha256']==bundle['manifest_sha256'] and accepted['financial_verified'] is False
    assert handoff.import_artifacts(parent,bundle,scope_id='mesh:private-peer-task',request_id='remote-task-17')==accepted
    assert not [v for v in all_items('source') if v.get('project_id')==target]
    assert not [v for v in all_items('run') if v.get('project_id')==target]
    records=[get('agent_task_artifact',aid) for aid in accepted['artifact_ids']]
    assert all(v['turn_id']==parent['id'] and v['project_id']==target and v['external_agent_import'] and v['requires_semantic_review'] for v in records)
    own=next(v for v in records if v['name'].endswith('.docx'))
    assert (DATA/own['relative_path']).read_bytes()==original and own['sha256']==hashlib.sha256(original).hexdigest()
    assert 'relative_path' not in json.dumps(accepted) and 'private-peer' not in json.dumps(accepted)
    assert base64.b64decode(handoff.read_artifact(parent,own['id'])['content_base64'])==original
    with pytest.raises(ValueError,match='不属于'):handoff.read_artifact(turn(target),own['id'])
    with pytest.raises(ValueError,match='不属于'):handoff.read_artifact(turn(project()),own['id'])


def test_remote_artifact_import_checks_actor_actual_project_live_parent_and_entire_bundle_first():
    pid=project();parent=turn(pid);good=handoff._bundle([handoff._item('good','artifact','report.md',b'Approved bytes only'),handoff._item('last','artifact','notes.txt',b'Last bytes')])
    for kind in ('bad-base64','unsafe-name'):
        bundle=copy.deepcopy(good)
        if kind=='bad-base64':bundle['items'][-1]['content_base64']='?'*len(bundle['items'][-1]['content_base64'])
        else:bundle['items'][-1]['name']='../escape.md';resign(bundle)
        with pytest.raises(ValueError):handoff.import_artifacts(parent,bundle,scope_id='mesh:bounded',request_id=kind)
    assert not [v for v in all_items('agent_task_artifact') if v.get('project_id')==pid]
    with pytest.raises(ValueError,match='不属于'):handoff.import_artifacts(dict(parent,project_id=project()),good,scope_id='mesh:bounded',request_id='forged')
    with actor_scope('u_'+'c'*64):
        with pytest.raises(ValueError,match='不属于'):handoff.import_artifacts(parent,good,scope_id='mesh:bounded',request_id='actor')
    patch('project',pid,deleted_at=now())
    with pytest.raises(ValueError,match='回收站'):handoff.import_artifacts(parent,good,scope_id='mesh:bounded',request_id='trash')


def test_remote_artifact_retry_cannot_change_manifest_revive_archive_or_hide_tamper():
    parent=turn();raw=b'# Revenue\n300.000000000002';bundle=handoff._bundle([handoff._item('report','artifact','report.md',raw)])
    accepted=handoff.import_artifacts(parent,bundle,scope_id='mesh:fixed',request_id='retry')
    changed=copy.deepcopy(bundle);changed['items'][0]['name']='changed.md';resign(changed)
    with pytest.raises(ValueError,match='同一交接请求'):handoff.import_artifacts(parent,changed,scope_id='mesh:fixed',request_id='retry')
    aid=accepted['artifact_ids'][0];record=get('agent_task_artifact',aid);path=DATA/record['relative_path'];path.write_bytes(b'corrupted')
    with pytest.raises(ValueError,match='完整性'):handoff.import_artifacts(parent,bundle,scope_id='mesh:fixed',request_id='retry')
    path.write_bytes(raw);patch('agent_task_artifact',aid,archived=True)
    with pytest.raises(ValueError,match='归档'):handoff.import_artifacts(parent,bundle,scope_id='mesh:fixed',request_id='retry')
    assert get('agent_task_artifact',aid)['archived'] is True
