import hashlib
import json
import threading
import pytest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs

from agent_service import business_flows,provider
from agent_service.execution import process_once
from agent_service.store import get,put,seal,uid,now,unseal,patch
from test_project_data import project


def test_actual_api_capture_to_business_table_and_repeated_runs(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    def model_forbidden(*args,**kwargs):raise AssertionError('非 AI 流程不应调用模型')
    monkeypatch.setattr(provider,'complete',model_forbidden)
    monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    calls=[];amounts=['9.00','4.00']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            page=int(parse_qs(urlsplit(self.path).query)['page'][0]);calls.append(page)
            data=json.dumps({'items':[{'region':['东部','西部'][page-1],'amount':amounts[page-1]}] if page<=2 else [],'total':2},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        pid=project();tid='t_'+uid();put('turn',{'project_id':pid,'actor_id':'owner','state':'done','outcome':'needs_input','text':'采集收入','interaction_mode':'work','created_at':now()},tid)
        cfg={'url':f'http://127.0.0.1:{server.server_port}/records','pagination':'page','page_param':'page','page_size':1,'records_path':'items','total_path':'total','max_pages':4}
        secret=seal(cfg);sid='s_'+uid();put('source',{'project_id':pid,'name':'收入 API','kind':'api','status':'ready','revision':1,'secret_config':secret},sid)
        pin={'source_id':sid,'revision':1,'config_sha256':hashlib.sha256(secret.encode()).hexdigest()}
        from agent_service.business_flow_tools import execute as agent_tool
        prepared=agent_tool(get('turn',tid),'prepare_business_flow_source',{'source_id':sid})
        assert {key:prepared[key] for key in pin}==pin and prepared['kind']=='api'
        plan={'version':1,'name':'汇总','inputs':{'table':{'meaning':'收入','grain':['nexus_row'],'columns':[{'name':'nexus_row','data_type':'text','meaning':'行号'},{'name':'c_1','data_type':'text','meaning':'地区'},{'name':'c_2','data_type':'decimal','meaning':'金额','unit':'CNY','aggregation':'additive'}]}},'stages':[{'id':'sum','kind':'aggregate','description':'合计','input':'table','by':[],'meaning':'总金额','aggregations':[{'function':'sum','column':'c_2','output':{'name':'amount','data_type':'decimal','meaning':'金额','unit':'CNY'}}]}],'output':'sum'}
        doc={'name':'API 财务汇总','inputs':[],'nodes':[{'id':'collect','kind':'api_capture','title':'采集 API','config':pin},{'id':'sum','kind':'table_workflow','title':'汇总','depends_on':['collect'],'inputs':{'table':{'source':'node','name':'collect'}},'config':{'plan':plan}}],'outputs':['sum']}
        flow=business_flows.create(pid,'owner',tid,doc,uid());rid=business_flows.submit_run(pid,flow['id'],'owner',uid(),flow['candidate_digest'],{})['run_id']
        assert process_once();result=business_flows.get_run(pid,flow['id'],rid,'owner');assert result['status']=='succeeded',result
        job=get('run',rid);state=unseal(job['secret_flow_checkpoint']);first=state['steps'][0]['output'];assert first['evidence']['complete'] is True
        assert state['steps'][1]['output']['rows'][0]['amount']=='13.00'
        assert calls==[1,2]
        record=get('flow_api_snapshot',first['snapshot_id']);assert '9.00' not in record['secret_payload']
        from agent_service.business_flow_api_capture import run as capture
        context={'project_id':pid,'actor_id':'owner','turn_id':tid,'run_id':rid,'node_id':'collect'}
        assert capture(context,pin,{})['content_hash']==first['content_hash']
        from agent_service.business_flow_api_capture import verify
        with pytest.raises(ValueError):verify({**context,'project_id':project()},first)
        with pytest.raises(ValueError):verify({**context,'actor_id':'u_'+'a'*64},first)
        from agent_service.actor_context import actor_scope
        with actor_scope('u_'+'a'*64):
            with pytest.raises(ValueError):verify(context,first)
            with pytest.raises(ValueError):business_flows.get_run_table(pid,flow['id'],rid,'collect')
        page=business_flows.get_run_table(pid,flow['id'],rid,'collect',0,1)
        assert page['total']==2 and page['rows'][0]['c_2']=='9.00' and page['next_offset']==1
        assert page['financial_verified'] is False
        patch('project',pid,deleted_at=now())
        with pytest.raises(ValueError):verify(context,first)
        patch('project',pid,deleted_at=None)
        assert verify(context,first)['content_hash']==first['content_hash']
        assert calls==[1,2]  # Recovery reads the encrypted snapshot, not the API.
        review=result['review'];assert review['required'] and not review['blocked_reason']
        items=[business_flows.ReviewItem(node_id=i['node_id'],kinds=i['kinds'],note='已核对来源和业务口径') for i in review['items']]
        business_flows.review_run(pid,flow['id'],rid,'owner',review['checkpoint_sha256'],items)
        business_flows.publish(pid,flow['id'],'owner',rid,flow['candidate_digest'])
        business_flows.set_schedule(pid,flow['id'],'owner','0 8 * * *',{},flow['candidate_digest'],{})
        amounts[0]='19.00';rid2=business_flows.submit_run(pid,flow['id'],'owner',uid(),flow['candidate_digest'],{})['run_id']
        assert process_once();state2=unseal(get('run',rid2)['secret_flow_checkpoint'])
        assert state2['steps'][1]['output']['rows'][0]['amount']=='23.00'
        assert first['content_hash']!=state2['steps'][0]['output']['content_hash']
        from datetime import datetime,timezone,timedelta
        patch('business_flow',flow['id'],next_due=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        business_flows.queue_due();assert process_once()
        last=business_flows.get_view(pid,flow['id'],'owner')['latest_run']
        assert last['status']=='succeeded',last
        scheduled=unseal(get('run',last['id'])['secret_flow_checkpoint'])
        assert scheduled['steps'][1]['output']['rows'][0]['amount']=='23.00'
        assert first['evidence']['collection_scope_verified'] is False
        before=list(calls)
        patch('source',sid,revision=2)
        with pytest.raises(ValueError):capture(context,pin,{})
        assert calls==before
        patch('source',sid,revision=1)
        for field in ('evidence','collected_at'):
            tampered=unseal(record['secret_payload'])
            if field=='evidence':tampered['evidence']['page_evidence'][0]['response_sha256']='0'*64
            else:tampered['collected_at']='2000-01-01T00:00:00+00:00'
            patch('flow_api_snapshot',first['snapshot_id'],secret_payload=seal(tampered))
            with pytest.raises(ValueError,match='证据摘要'):verify(context,first)
            assert calls==before
        tampered=unseal(record['secret_payload']);tampered['rows'][0]['c_2']='999.00'
        patch('flow_api_snapshot',first['snapshot_id'],secret_payload=seal(tampered))
        with pytest.raises(ValueError):capture(context,pin,{})
        assert calls==before
        patch('flow_api_snapshot',first['snapshot_id'],status='collecting')
        with pytest.raises(ValueError,match='未留下完整快照'):capture(context,pin,{})
        assert calls==before
        from agent_service.connectors import read_source,SourceError
        incomplete={**get('source',sid),'secret_config':seal({**cfg,'max_pages':1})}
        with pytest.raises(SourceError,match='分页上限'):read_source(incomplete)
    finally:server.shutdown();server.server_close();thread.join()
