"""Helper for browser-business-live.mjs: use the real download importer.

The caller provides an already-open *local synthetic* browser session. This
uses real HTTP requests to the runner and a disposable DATA_DIR. It does not
pretend that the test-mode runner has production isolation or company access.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from agent_service import browser_downloads, provider
from agent_service.actor_context import actor_scope
from agent_service.config import DATA
from agent_service.store import get, put, seal, uid, unseal

value=json.load(sys.stdin)
pid='p_'+uid();tid='t_'+uid();sid=value['session_id'];actor='owner'
provider.settings=lambda:{'base_url':'','model':'','share_data':True}
put('project',{'name':'Synthetic live browser fixture'},pid)
turn=put('turn',{'project_id':pid,'actor_id':actor,'text':'从网页下载数据并分析',
                 'interaction_mode':'work','state':'agent_tool',
                 'agent_trace':[{'id':'actual-live-download','status':'started'}]},tid)
put('browser_session',{'project_id':pid,'actor_id':actor,'turn_id':tid,'mode':'agent',
     'workspace_id':value['workspace_id'],'state':'open','agent_grant_origin':value['origin'],
     'agent_grant_expires':time.time()+600,'expires_at':time.time()*1000+60000,
     'snapshot_secret':seal(value['snapshot'])},sid)
with actor_scope(actor):
    result=browser_downloads.execute(turn,'download_browser_source',
        {'session_id':sid,'revision':value['snapshot']['revision'],'ref':value['export_ref']})
    # Same durable step cannot issue a second export.
    replay=browser_downloads.execute(turn,'download_browser_source',
        {'session_id':sid,'revision':value['snapshot']['revision'],'ref':value['export_ref']})
assert replay==result
source=get('source',result['source_id'])
assert source['project_id']==pid and source['status']=='queued'
path=DATA/'uploads'/unseal(source['secret_config'])['stored_name']
hash_value=hashlib.sha256()
with path.open('rb') as stream:
    for chunk in iter(lambda:stream.read(512*1024),b''):hash_value.update(chunk)
assert path.stat().st_size==value['bytes'] and hash_value.hexdigest()==value['sha256']
print(json.dumps({'stored_bytes':path.stat().st_size,'sha256':hash_value.hexdigest(),
                  'source_status':source['status'],'retry_reused_source':True}))
