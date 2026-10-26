"""Real noisy MCP shutdown must release both the process and its reader thread."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from agent_service import mcp_stdio
from agent_service.store import uid


@pytest.mark.skipif(os.name != 'nt', reason='Windows bounded-queue MCP reader')
def test_noisy_mcp_session_shutdown_does_not_leak_reader_thread(tmp_path, monkeypatch):
    server = tmp_path / 'noisy_mcp.py'
    server.write_text('''import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if request['method']=='notifications/initialized':continue
    if request['method']=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
        for index in range(5):
            print(json.dumps({'jsonrpc':'2.0','method':'notifications/progress','params':{'index':index}}),flush=True)
''', encoding='utf-8')
    monkeypatch.setenv('ATLAS_LOCAL_MODE', '1')
    monkeypatch.setenv('ATLAS_LOCAL_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('ATLAS_NETWORK_MODE', 'connected')
    monkeypatch.setenv('ATLAS_STDIO_TEMPLATES', json.dumps({
        'noisy-review': {'command': sys.executable, 'args': [str(server)]}}))
    before = set(threading.enumerate())
    client = mcp_stdio.Session({'transport':'stdio','command':'noisy-review','args':[], 'env':{}})
    with client:
        # More messages than the bounded response queue: the OS reader has to
        # wait for queue capacity, and shutdown must wake that wait.
        time.sleep(.15)
    assert client.process.poll() is not None
    deadline = time.monotonic() + 2
    remaining = []
    while time.monotonic() < deadline:
        remaining = [thread for thread in threading.enumerate()
                     if thread not in before and thread.is_alive()]
        if not remaining:
            break
        time.sleep(.02)
    assert remaining == [], 'MCP session left a live transport thread after shutdown'
