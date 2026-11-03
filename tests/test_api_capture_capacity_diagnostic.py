"""Capacity failures are explicit, private and cannot publish partial data."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from agent_service import business_flows,provider
from agent_service.execution import process_once
from agent_service.store import all_items,delete,get,put,seal,uid,now,unseal
from test_project_data import project


def test_large_api_capture_reports_capacity_and_does_not_publish(monkeypatch):
    monkeypatch.setattr(provider,'settings',lambda:{'base_url':'','model':'','share_data':True})
    monkeypatch.setenv('API_ALLOWED_HOSTS','127.0.0.1')
    secret_value='PRIVATE-CAPACITY-TEST-'+'x'*(2*1024*1024)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            body=json.dumps({'items':[{'description':secret_value}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    pid=None
    try:
        pid=project();tid='t_'+uid()
        put('turn',{'project_id':pid,'actor_id':'owner','state':'done','outcome':'needs_input',
                    'text':'采集数据','interaction_mode':'work','created_at':now()},tid)
        cfg={'url':f'http://127.0.0.1:{server.server_port}/records','pagination':'none','records_path':'items'}
        secret=seal(cfg);sid='s_'+uid()
        put('source',{'project_id':pid,'name':'large API','kind':'api','status':'ready',
                      'revision':1,'secret_config':secret},sid)
        pin={'source_id':sid,'revision':1,'config_sha256':hashlib.sha256(secret.encode()).hexdigest()}
        doc={'name':'API 容量诊断','inputs':[],
             'nodes':[{'id':'collect','kind':'api_capture','title':'读取数据','config':pin}],
             'outputs':['collect']}
        flow=business_flows.create(pid,'owner',tid,doc,uid())
        rid=business_flows.submit_run(pid,flow['id'],'owner',uid(),flow['candidate_digest'],{})['run_id']
        assert process_once()
        result=business_flows.get_run(pid,flow['id'],rid,'owner')
        assert result['status']=='failed'
        assert result['steps'][0]['error_code']=='API_SNAPSHOT_CAPACITY'
        assert '2 MiB' in result['steps'][0]['error_message']
        assert '未生成或发布截断结果' in result['steps'][0]['error_message']
        assert 'PRIVATE-CAPACITY-TEST' not in json.dumps(result,ensure_ascii=False)
        assert not result['steps'][0]['table_available']
        state=unseal(get('run',rid)['secret_flow_checkpoint'])
        assert state['status']=='failed' and 'outputs' not in state
        from agent_service.business_flow_repair import guidance
        diagnosis=guidance(state)
        assert diagnosis['action']=='inspect_capacity' and diagnosis['retry_allowed'] is False
        assert '2 MiB' in diagnosis['next_step'] and 'PRIVATE-CAPACITY-TEST' not in str(diagnosis)
        assert not get('business_flow',flow['id']).get('published_run')
    finally:
        server.shutdown();server.server_close();thread.join()
        # A failed workflow intentionally schedules an agent diagnosis. Do not
        # let this test-owned background turn consume later tests' model mocks.
        if pid:
            for turn in all_items('turn'):
                if turn.get('project_id')==pid:delete('turn',turn['id'])
