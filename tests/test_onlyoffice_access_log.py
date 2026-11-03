"""ONLYOFFICE bearer paths must not appear in API access logs."""
import json
import logging
import os
import socket
import subprocess
import sys
import time

import httpx
from uvicorn.logging import AccessFormatter
from agent_service.access_log import OnlyOfficeTicketFilter, install


def _record(msg,args=()):
    return logging.LogRecord('uvicorn.access',logging.INFO,'',0,msg,args,None)


def test_default_uvicorn_access_format_redacts_ticket_only():
    token='fixture-private-token_A234'
    record=_record('%s - "%s %s HTTP/%s" %d',('127.0.0.1:1234','GET','/api/onlyoffice/file/'+token+'?x=abc','1.1',200))
    assert OnlyOfficeTicketFilter().filter(record)
    output=AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert token not in output and 'GET /api/onlyoffice/file/[REDACTED] HTTP/1.1' in output
    assert '200' in output
    normal=_record('%s - "%s %s HTTP/%s" %d',('127.0.0.1:1234','GET','/api/projects/p','1.1',200))
    assert OnlyOfficeTicketFilter().filter(normal)
    assert '/api/projects/p' in AccessFormatter('%(client_addr)s %(request_line)s').format(normal)


def test_structured_fields_and_encoded_slash_are_sanitized_without_mutating_input():
    token='fixture-private-token_B345'
    payload={'path':'/api/onlyoffice/file%2F'+token+'?param=secret','params':{'token':token},'status':200}
    record=_record(payload);record.request={'path':'/api/onlyoffice/file/'+token,'token':token}
    assert OnlyOfficeTicketFilter().filter(record)
    output=json.dumps({'message':record.msg,'request':record.request})
    assert token not in output and '[REDACTED]' in output and record.msg['status']==200
    assert payload['params']['token']==token and token in payload['path']


def test_install_is_idempotent():
    logger=logging.getLogger('uvicorn.access');before=len(logger.filters)
    install();first=len(logger.filters);install()
    assert len(logger.filters)==first and first in (before,before+1)


def test_real_uvicorn_process_keeps_access_logs_without_ticket(tmp_path):
    # Actual Uvicorn logging configuration and service import ordering, not
    # just formatting a synthetic record. No real artifact is accessed.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,DATA_DIR=str(tmp_path/'data'),DATABASE_URL='sqlite:///'+str(tmp_path/'metadata.sqlite'),
             APP_TOKEN='access-log-test-token-not-a-secret',LLM_BASE_URL='',LLM_MODEL='')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','agent_service.api:app','--host','127.0.0.1','--port',str(port)],
                             env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        deadline=time.monotonic()+20
        with httpx.Client(timeout=1,trust_env=False) as client:
            while True:
                if process.poll() is not None:raise AssertionError('uvicorn stopped before start')
                try:
                    response=client.get(f'http://127.0.0.1:{port}/')
                    if response.status_code==200:break
                except httpx.TransportError:pass
                if time.monotonic()>deadline:raise AssertionError('uvicorn did not start')
                time.sleep(.1)
            token='f' * 43
            response=client.get(f'http://127.0.0.1:{port}/api/onlyoffice/file/{token}?extra=must-not-appear')
            assert response.status_code==404
    finally:
        process.terminate()
        output,_=process.communicate(timeout=15)
    assert token not in output and 'must-not-appear' not in output
    assert '/api/onlyoffice/file/[REDACTED]' in output
    assert 'GET / HTTP/1.1' in output, 'normal access logging remains enabled'
