import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_service import business_deliverables as delivery, office, workspace, workspace_artifacts
from agent_service.actor_context import actor_scope
from agent_service.business_flow_engine import run
from agent_service.config import TOKEN
from agent_service.api import app
from agent_service.store import put, uid


@pytest.fixture
def execution():
    pid='p_'+uid();tid='t_'+uid()
    put('project',{'name':'生成报告','created_at':'2026-01-01'},pid)
    put('turn',{'project_id':pid,'actor_id':'owner','interaction_mode':'work','state':'done',
                'text':'根据已确认的数据生成报告'},tid)
    return {'project_id':pid,'actor_id':'owner','originating_turn_id':tid}


def config(name,version):return {'name':name,'renderer_version':version}


def test_presentation_is_a_real_project_artifact_with_bound_spec(execution):
    spec={'kind':'presentation','title':'月度收入','sections':[{'heading':'东部','body':'净收入：90.15 元'}]}
    result=delivery.render_office(execution,config('月度汇报',office.RENDERER),{'spec':spec})
    assert result['format']=='pptx' and result['structural_validation'] is True
    assert result['visual_validation'] is False and result['financial_verified'] is False
    path,record=workspace_artifacts.file(execution['project_id'],result['artifact_id'])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==result['sha256']
    from pptx import Presentation
    deck=Presentation(path)
    assert len(deck.slides)==2 and deck.slides[1].shapes.title.text=='东部'
    raw=json.dumps(spec,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    assert result['spec_sha256']==hashlib.sha256(raw).hexdigest()
    assert record['download_url']==result['download_url']
    # Actual business content must not be copied into project Git files.
    assert not any('东部' in workspace.read_file(execution['project_id'],item['path'])
                   for item in workspace.list_files(execution['project_id']))


def test_html_report_escapes_untrusted_content_and_has_a_versioned_artifact(execution):
    spec={'title':'<script>alert(1)</script>','sections':[{'heading':'<img onerror=x>','body':'收入 90.15\n<iframe src=x>', 'bullets':['<b>未经核验</b>']}]}
    result=delivery.render_html(execution,config('业务报告',delivery.HTML_RENDERER),{'spec':spec})
    path,record=workspace_artifacts.file(execution['project_id'],result['artifact_id'])
    data=path.read_text(encoding='utf-8')
    assert '<script>alert' not in data and '<img onerror' not in data and '<iframe' not in data
    assert '&lt;script&gt;' in data and '&lt;img onerror=x&gt;' in data and '&lt;b&gt;' in data
    assert hashlib.sha256(path.read_bytes()).hexdigest()==result['sha256']
    assert not result['financial_verified'] and not result['visual_validation']
    response=TestClient(app).get(result['download_url'],headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==200 and response.headers['content-type'].startswith('text/plain')
    preview_url='/api/projects/'+execution['project_id']+'/artifacts/'+result['artifact_id']+'/preview'
    preview=TestClient(app).get(preview_url,headers={'Authorization':'Bearer '+TOKEN},params={'limit':2})
    assert preview.status_code==200,preview.text
    page=preview.json();assert page['render']=='document' and page['verified'] and page['next_offset']==2
    assert page['blocks'][0]['text']=='<script>alert(1)</script>'
    assert page['layout_fidelity'] is False
    next_page=TestClient(app).get(preview_url,headers={'Authorization':'Bearer '+TOKEN},params={'offset':2})
    assert '<iframe src=x>' in [block['text'] for block in next_page.json()['blocks']]
    from agent_service.store import get,patch
    identity=workspace_artifacts._record(execution['project_id'],result['artifact_id'])['private']['flow_artifact_id']
    saved=get('business_flow_artifact',identity)
    assert '90.15' not in saved['secret_preview']
    with actor_scope('u_'+'a'*64):
        with pytest.raises((KeyError,ValueError)):delivery.preview_blocks(execution['project_id'],identity)
    patch('business_flow_artifact',identity,secret_preview='tampered')
    with pytest.raises(ValueError,match='无法验证'):delivery.preview_blocks(execution['project_id'],identity)
    patch('business_flow_artifact',identity,secret_preview=saved['secret_preview'])
    assert not any('90.15' in workspace.read_file(execution['project_id'],item['path'])
                   for item in workspace.list_files(execution['project_id']))
    assert any(item['id']==result['artifact_id'] for item in workspace_artifacts.catalog(execution['project_id'])['items'])
    other=delivery.render_html(execution,config('另一个名称',delivery.HTML_RENDERER),{'spec':spec})
    assert other['artifact_id']!=result['artifact_id']
    assert workspace_artifacts.file(execution['project_id'],result['artifact_id'])[1]['name']=='业务报告.html'


def test_missing_or_foreign_actor_and_renderer_version_are_rejected(execution):
    spec={'title':'报告','sections':[{'body':'内容'}]}
    bad=dict(execution,project_id='p_foreign')
    with pytest.raises((KeyError,ValueError)):delivery.render_html(bad,config('报告',delivery.HTML_RENDERER),{'spec':spec})
    with pytest.raises(ValueError):delivery.render_html(dict(execution,originating_turn_id=None),config('报告',delivery.HTML_RENDERER),{'spec':spec})
    with actor_scope('u_'+'a'*64):
        with pytest.raises(ValueError):delivery.render_html(execution,config('报告',delivery.HTML_RENDERER),{'spec':spec})
    with pytest.raises(ValueError):delivery.render_html(execution,config('报告','other-version'),{'spec':spec})
    with pytest.raises(ValueError):delivery.render_html(execution,config('../bad',delivery.HTML_RENDERER),{'spec':spec})
    with pytest.raises(ValueError,match='凭证'):
        delivery.render_html(execution,config('报告',delivery.HTML_RENDERER),{'spec':{'title':'报告','sections':[{'body':'api_key = private'}]}})
    from agent_service.store import patch
    patch('turn',execution['originating_turn_id'],outcome='cancelled')
    with pytest.raises(ValueError,match='停止'):delivery.render_html(execution,config('报告',delivery.HTML_RENDERER),{'spec':spec})


def test_versioned_engine_captures_actual_output_and_checkpoint(execution):
    plan={'version':1,'name':'生成 HTML','inputs':['content'],'nodes':[{'id':'report','kind':'html_report','title':'生成报告',
        'inputs':{'spec':{'source':'input','name':'content'}},'config':config('汇总',delivery.HTML_RENDERER)}], 'outputs':['report']}
    inputs={'content':{'title':'月度结果','sections':[{'heading':'收入','body':'数据待核验'}]}}
    checkpoints=[]
    state=run(plan,inputs,delivery.handlers(),execution,persist=lambda x:checkpoints.append(x))
    assert state['status']=='succeeded' and state['steps'][0]['handler_version']==delivery.HTML_RENDERER
    assert state['steps'][0]['output_sha256'] and state['outputs']['report']['financial_verified'] is False
    path,_=workspace_artifacts.file(execution['project_id'],state['outputs']['report']['artifact_id'])
    assert '数据待核验' in path.read_text(encoding='utf-8')
    assert checkpoints[0]['status']=='running' and checkpoints[-1]['status']=='succeeded'


def test_real_uploaded_income_is_processed_and_rendered_to_html_and_ppt():
    """Real uploaded data -> typed sum -> full report, no model-made number."""
    from decimal import Decimal
    from agent_service.execution import process_once
    from agent_service.store import get
    from agent_service.business_flow_data import source_table, table_workflow, validate_source, validate_table
    from agent_service.business_flow_engine import Handler
    client=TestClient(app);h={'Authorization':'Bearer '+TOKEN}
    def req(method,path,**kwargs):
        response=client.request(method,path,headers=h,**kwargs)
        assert response.status_code==200,response.text
        return response.json()
    pid=req('POST','/api/projects',json={'name':'真实报表','request_id':uid()})['id'];root='/api/projects/'+pid
    source=req('POST',root+'/attachments',files={'file':('income.csv','办事处,收入\n东部,9.00\n东部,9.00\n'.encode(),'text/csv')},
               data={'request_id':uid(),'local_processing':'true'})
    assert process_once(); source=get('source',source['id'])
    turn=req('POST',root+'/messages',json={'text':'按办事处计算并生成 HTML 和 PPT 报告','request_id':uid(),'source_id':source['id']})
    context={'project_id':pid,'turn_id':turn['id'],'actor_id':'owner'}
    plan={'version':1,'name':'收入汇总','inputs':{'table':{'meaning':'收入明细','grain':['atlas_row'],
        'columns':[{'name':'atlas_row','data_type':'text','meaning':'本次资料行号'},
                   {'name':'c_1','data_type':'text','meaning':'办事处'},
                   {'name':'c_2','data_type':'decimal','meaning':'收入','unit':'CNY','aggregation':'additive'}]}},
        'stages':[{'id':'total','kind':'aggregate','description':'按办事处汇总','input':'table','by':['c_1'],
                   'meaning':'各办事处收入','aggregations':[{'function':'sum','column':'c_2',
                       'output':{'name':'amount','data_type':'decimal','meaning':'收入合计','unit':'CNY'}}]}], 'output':'total'}
    doc={'name':'真实收入报告','inputs':[], 'nodes':[
        {'id':'read','kind':'source_table','title':'读取资料','config':{'source_id':source['id'],'revision':source['revision'],'content_hash':source['content_hash']}},
        {'id':'aggregate','kind':'table_workflow','title':'汇总收入','depends_on':['read'],'inputs':{'table':{'source':'node','name':'read'}},'config':{'plan':plan}},
        {'id':'htmlspec','kind':'table_report_spec','title':'组织报告','depends_on':['aggregate'],'inputs':{'table':{'source':'node','name':'aggregate'}},'config':{'title':'收入报告','format':'html'}},
        {'id':'html','kind':'html_report','title':'生成网页报告','depends_on':['htmlspec'],'inputs':{'spec':{'source':'node','name':'htmlspec'}},'config':config('收入报告',delivery.HTML_RENDERER)},
        {'id':'pptspec','kind':'table_report_spec','title':'组织汇报','depends_on':['aggregate'],'inputs':{'table':{'source':'node','name':'aggregate'}},'config':{'title':'收入汇报','format':'presentation'}},
        {'id':'ppt','kind':'office_document','title':'生成演示文稿','depends_on':['pptspec'],'inputs':{'spec':{'source':'node','name':'pptspec'}},'config':config('收入汇报',office.RENDERER)}],
        'outputs':['aggregate','html','ppt']}
    handlers=delivery.handlers() | {'source_table':Handler(source_table,True,True,validate_source),
                                   'table_workflow':Handler(table_workflow,True,True,validate_table)}
    result=run(doc,{},handlers,context,persist=lambda value:None)
    assert result['status']=='succeeded',result
    assert Decimal(result['outputs']['aggregate']['rows'][0]['amount'])==Decimal('18.00')
    html_path,_=workspace_artifacts.file(pid,result['outputs']['html']['artifact_id'])
    ppt_path,_=workspace_artifacts.file(pid,result['outputs']['ppt']['artifact_id'])
    assert '18' in html_path.read_text(encoding='utf-8')
    from pptx import Presentation
    presentation=Presentation(ppt_path)
    text=' '.join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert '18' in text and '不代表业务口径或财经数字已审核' in text
    assert result['outputs']['html']['financial_verified'] is False and result['outputs']['ppt']['financial_verified'] is False
    assert not any('18.00' in workspace.read_file(pid,item['path']) for item in workspace.list_files(pid))


def test_table_report_never_silently_truncates_or_accepts_tampered_rows(execution):
    from agent_service.data_workflows import digest
    schema={'columns':[{'name':'amount','data_type':'decimal','meaning':'收入','label':'收入','unit':'CNY',
             'value_format':'native','number_format':'','period':'','aggregation':'additive','nullable':True}],
            'grain':[],'meaning':'收入','source_ref':''}
    rows=[{'amount':'9007199254740993.123456789012'}]
    table={'kind':'table_workflow_v1','complete':True,'rows':rows,'schema':schema,'row_count':1,
           'output_sha256':digest({'schema':schema,'rows':rows}),
           'requires_source_review':True,'requires_semantic_review':True,'financial_evidence_status':'requires_business_review'}
    result=delivery.table_report_spec(execution,{'title':'核对','format':'html'},{'table':table})
    assert '9007199254740993.123456789012' in result['spec']['sections'][1]['body']
    assert result['requires_source_review'] and result['requires_semantic_review']
    ai='建议收入为 999 元，需人工核对。'
    commentary={'text':ai,'output_sha256':hashlib.sha256(ai.encode()).hexdigest(),
                'nondeterministic':True,'evidence_status':'unverified'}
    for fmt in ('html','presentation'):
        report=delivery.table_report_spec(execution,{'title':'核对','format':fmt},{'table':table,'commentary':commentary})
        assert report['ai_commentary_unverified'] is True
        assert report['spec']['sections'][-1]['heading']=='AI 生成说明（未经核验）'
        assert '不能作为财务计算依据' in report['spec']['sections'][-1]['body']
        rendered=(delivery.render_html(execution,config('AI报表',delivery.HTML_RENDERER),{'spec':report}) if fmt=='html'
                  else delivery.render_office(execution,config('AI汇报',office.RENDERER),{'spec':report}))
        path,_=workspace_artifacts.file(execution['project_id'],rendered['artifact_id'])
        if fmt=='html':content=path.read_text(encoding='utf-8')
        else:
            from pptx import Presentation
            content=' '.join(s.text for slide in Presentation(path).slides for s in slide.shapes if s.has_text_frame)
        assert '999' in content and '未经核验' in content and '9007199254740993' in content
        assert rendered['financial_verified'] is False
    with pytest.raises(ValueError,match='回执无效'):
        delivery.table_report_spec(execution,{'title':'核对','format':'html'},{'table':table,'commentary':dict(commentary,text='篡改')})
    bad=dict(table,rows=[{'amount':'0'}])
    with pytest.raises(ValueError,match='摘要'):delivery.table_report_spec(execution,{'title':'核对','format':'html'},{'table':bad})
    rows=[{'amount':str(i)} for i in range(101)]
    large=dict(table,rows=rows,row_count=len(rows),output_sha256=digest({'schema':schema,'rows':rows}))
    with pytest.raises(ValueError,match='未截断'):delivery.table_report_spec(execution,{'title':'核对','format':'html'},{'table':large})
