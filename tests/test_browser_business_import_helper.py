"""Exercise the live-test import helper over HTTP against a controlled stub.

This verifies the helper wiring and source import, but is *not* a Chromium test.
The optional browser-business-live.mjs is responsible for real browser coverage.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path


def test_live_browser_import_helper_over_http(tmp_path):
    raw=b'office,amount\n'+b'Shanghai,123.45\n'*140000
    assert len(raw)>2*1024*1024
    sid='a'*64;wid='b'*64;token='local-fixture-token-'+'a'*32
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            if self.headers.get('Authorization')!='Bearer '+token:self.send_error(401);return
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            op=self.path.rsplit('/',1)[-1];calls.append(op)
            if body.get('workspace_id')!=wid:self.send_error(403);return
            if op=='download':
                response={'download_id':body['action_id'],'filename':'report.csv','bytes':len(raw),
                          'content_sha256':hashlib.sha256(raw).hexdigest()}
            elif op=='download-chunk':
                part=raw[body['offset']:body['offset']+body['length']]
                response={'download_id':body['download_id'],'offset':body['offset'],
                          'bytes':len(part),'content_base64':base64.b64encode(part).decode()}
            elif op=='download-release':response={'download_id':body['download_id'],'released':True}
            else:self.send_error(404);return
            encoded=json.dumps(response).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
            self.end_headers();self.wfile.write(encoded)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        root=Path(__file__).resolve().parents[1];origin='http://fixture.test'
        snapshot={'url':origin+'/report','revision':'c'*64,'controls':[{'ref':1,'label':'导出数据',
                    'visible':True,'disabled':False,'sensitive':False}]}
        input_value={'session_id':sid,'workspace_id':wid,'origin':origin,'snapshot':snapshot,
                     'export_ref':1,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        env={**os.environ,'DATA_DIR':str(tmp_path/'data'),'APP_TOKEN':'fixture-only-access-token-1234',
             'PYTHONPATH':str(root),'NEXUS_BROWSER_URL':'http://127.0.0.1:'+str(server.server_port),
             'NEXUS_BROWSER_TOKEN':token,'NEXUS_WEB_ENABLED':'1','API_ALLOWED_HOSTS':'127.0.0.1',
             'NEXUS_NETWORK_MODE':'connected'}
        process=subprocess.run([sys.executable,str(root/'tests/browser_business_import_helper.py')],
            cwd=root,input=json.dumps(input_value),text=True,capture_output=True,timeout=30,env=env)
        assert process.returncode==0,process.stderr
        out=json.loads(process.stdout.strip().splitlines()[-1])
        assert out['sha256']==input_value['sha256'] and out['stored_bytes']==len(raw)
        assert out['source_status']=='queued' and out['retry_reused_source']
        assert calls.count('download')==1 and calls.count('download-chunk')>1
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
