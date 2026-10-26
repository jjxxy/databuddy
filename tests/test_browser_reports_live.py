"""Real Chromium integration against a synthetic local iframe/report server.

This is not a company-website test. Optional dev requirements: Python Playwright
and its Chromium/headless-shell binary. Missing dependency/binary skips explicitly;
other launch failures fail rather than masquerading as successful verification.
No product dependencies or browser credential/session configuration are changed.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

import pytest

from agent_service.browser_reports import BrowserRecipe,BrowserReportError,export_with_page
from agent_service.report_acquisition import ReportRequest,validate_download


def _frame(mode):
    # Static server-owned fixture code; inputs are read back and independently
    # verified against the CSV body after the actual browser download completes.
    return '''<!doctype html><html><meta charset="utf-8"><body>
    <label>Office <select id="office"><option value="all">All</option>
    <option value="Shanghai">Shanghai</option><option value="Beijing">Beijing</option></select></label>
    <label>Period <input id="period" type="date" value="2026-10-01"></label>
    <button id="query" type="button">Query</button>
    <div id="context">Income report</div><div id="ready">Ready</div>
    <button id="export" type="button">Export</button>
    <script>
    const mode=MODE;
    document.querySelector('#query').addEventListener('click',()=>{
        if(mode==='reset')document.querySelector('#office').value='all';
        document.querySelector('#context').textContent='Income report | CNY | '+
            document.querySelector('#office').value+' | '+document.querySelector('#period').value;
    });
    document.querySelector('#export').addEventListener('click',()=>{
        location.href='/export.csv?'+new URLSearchParams({office:document.querySelector('#office').value,
            period:document.querySelector('#period').value,mode});
    });
    </script></body></html>'''.replace('MODE',json.dumps(mode))


@pytest.fixture(scope='module')
def report_server():
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            parsed=urlsplit(self.path);params=parse_qs(parsed.query)
            if 'fixture_auth=fixture-only' not in self.headers.get('Cookie',''):
                body=b'<html>fixture login required</html>'
                self.send_response(401);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if parsed.path=='/':
                mode=params.get('mode',['success'])[0]
                body=('<!doctype html><html><body><iframe id="report" src="/frame?mode='+mode+'" style="width:900px;height:500px"></iframe></body></html>').encode()
                kind='text/html; charset=utf-8'
            elif parsed.path=='/frame':
                body=_frame(params.get('mode',['success'])[0]).encode();kind='text/html; charset=utf-8'
            elif parsed.path=='/export.csv':
                requests.append({'office':params.get('office',[''])[0],'period':params.get('period',[''])[0],
                                 'mode':params.get('mode',['success'])[0]})
                mode=params.get('mode',['success'])[0]
                if mode=='html':body=b'<form action="login">Login required</form>';kind='text/html'
                else:
                    body=('office,amount,period\n'+params.get('office',[''])[0]+',10.123456789012,'+params.get('period',[''])[0]+'\n').encode()
                    kind='text/csv'
                self.send_response(200);self.send_header('Content-Type',kind)
                self.send_header('Content-Disposition','attachment; filename="fixture-report.csv"')
                self.send_header('Content-Length',str(len(body)+(100 if mode=='interrupted' else 0)));self.end_headers()
                if mode=='interrupted':
                    self.wfile.write(body[:10]);self.wfile.flush();self.close_connection=True
                else:self.wfile.write(body)
                return
            else:
                self.send_response(404);self.end_headers();return
            self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),requests
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


@pytest.fixture(scope='module')
def chromium_browser():
    sync=pytest.importorskip('playwright.sync_api',reason='Optional real-browser fixture requires Python Playwright')
    with sync.sync_playwright() as p:
        options={'headless':True,'args':['--no-sandbox','--disable-dev-shm-usage','--disable-background-networking','--disable-component-update','--disable-sync']}
        executable=os.getenv('ATLAS_TEST_BROWSER_EXECUTABLE')
        if executable:options['executable_path']=executable
        try:browser=p.chromium.launch(**options)
        except sync.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Optional Chromium fixture binary is missing; install the matching Playwright browser or set ATLAS_TEST_BROWSER_EXECUTABLE')
            raise RuntimeError('Chromium fixture launch failed: '+str(exc)[:1200]) from None
        try:yield browser
        finally:browser.close()


@pytest.fixture
def page(chromium_browser,report_server):
    origin,_=report_server;allowed=urlsplit(origin)
    context=chromium_browser.new_context(accept_downloads=True,service_workers='block')
    rejected=[]
    def route(request_route):
        target=urlsplit(request_route.request.url)
        if target.scheme=='http' and target.hostname=='127.0.0.1' and target.port==allowed.port:
            request_route.continue_()
        else:
            rejected.append(request_route.request.url);request_route.abort()
    context.route('**/*',route)
    context.add_cookies([{'name':'fixture_auth','value':'fixture-only','url':origin}])
    value=context.new_page()
    try:yield value,rejected
    finally:context.close()


def recipe(origin,mode='success'):
    return BrowserRecipe(url=origin+'/?mode='+mode,iframe_selector='#report',
        filters=[{'label':'office','selector':'#office','kind':'select','value':'Shanghai'},
                 {'label':'period','selector':'#period','kind':'date','value':'2026-10-01'}],
        query_button_selector='#query',ready_selector='#ready',context_selector='#context',
        required_context_text=['Income report','CNY','Shanghai','2026-10-01'],export_selector='#export',
        timeout_ms=5000,download_timeout_ms=10000)


def validator(origin):
    request=ReportRequest(scope_id='synthetic_local_fixture',request_id='fixture_export',url=origin+'/export.csv',format='csv',
                          filters={'office':'Shanghai','period':'2026-10-01'},
                          filter_columns={'office':'office','period':'period'},expected_columns=['office','amount','period'],expected_rows=1,
                          context={'unit':'CNY'})
    return lambda path:validate_download(path,request)


def test_actual_chromium_iframe_filters_full_download_and_idempotence(page,report_server,tmp_path):
    origin,requests=report_server;value,rejected=page;before=len(requests)
    target=tmp_path/'report.csv';configured=recipe(origin)
    receipt=export_with_page(value,configured,target,validator=validator(origin),network_policy_verified=True)
    assert receipt['browser_filters_verified'] and receipt['validation']['filters_verified']
    assert receipt['validation']['rows']==1 and not receipt['validation']['context_verified']
    assert target.read_bytes()==b'office,amount,period\nShanghai,10.123456789012,2026-10-01\n'
    assert requests[-1]['office']=='Shanghai' and requests[-1]['period']=='2026-10-01'
    assert len(requests)==before+1
    assert export_with_page(value,configured,target,validator=validator(origin),network_policy_verified=True)['reused']
    assert len(requests)==before+1 and not rejected


def test_actual_chromium_filter_reset_blocks_export(page,report_server,tmp_path):
    origin,requests=report_server;value,_=page;before=len(requests)
    with pytest.raises(BrowserReportError,match='回读值'):
        export_with_page(value,recipe(origin,'reset'),tmp_path/'report.csv',validator=validator(origin),network_policy_verified=True)
    assert len(requests)==before
    assert not list(tmp_path.glob('*.csv'))


@pytest.mark.parametrize('mode',['html','interrupted'])
def test_actual_chromium_bad_or_interrupted_download_is_not_a_result(page,report_server,tmp_path,mode):
    origin,requests=report_server;value,_=page;before=len(requests)
    target=tmp_path/'report.csv'
    with pytest.raises(BrowserReportError):
        export_with_page(value,recipe(origin,mode),target,validator=validator(origin),network_policy_verified=True)
    # Chromium may internally retry an interrupted GET; the adapter never repeats
    # the export click or promotes those partial responses.
    assert len(requests)>=before+1
    assert not target.exists() and not target.with_name(target.name+'.browser-receipt.json').exists()
    assert not list(tmp_path.glob('*.part-*'))
