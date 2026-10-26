"""First-party, network-free MCP stdio utility for the local computer profile."""
import json,sys
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

for line in sys.stdin:
    try:
        request=json.loads(line);method=request.get('method')
        if method=='notifications/initialized':continue
        if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'Atlas local clock','version':'1'}}
        elif method=='tools/list':result={'tools':[{'name':'get_local_time','description':'读取本机系统时钟，返回指定时区的当前时间；不访问网络或业务文件。','inputSchema':{'type':'object','properties':{'timezone':{'type':'string','enum':['UTC','Asia/Shanghai']}},'additionalProperties':False}}]}
        elif method=='tools/call' and request.get('params',{}).get('name')=='get_local_time':
            region=request['params'].get('arguments',{}).get('timezone','Asia/Shanghai')
            if region not in ('UTC','Asia/Shanghai'):raise ValueError()
            result={'content':[{'type':'text','text':json.dumps({'time':datetime.now(ZoneInfo(region)).isoformat(),'timezone':region,'source':'local_system_clock'},ensure_ascii=False)}]}
        else:raise ValueError()
        if 'id' in request:print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result},ensure_ascii=False),flush=True)
    except Exception:
        if isinstance(locals().get('request'),dict) and 'id' in request:print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32602,'message':'Invalid request'}}),flush=True)
