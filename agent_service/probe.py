import json,os,sys
from pathlib import Path
from .planner import planner
from .store import get
from .connectors import SourceError

if __name__=='__main__':
    try:
        source=get('source',sys.argv[1])
        expected=os.getenv('NEXUS_PROBE_INPUT_SHA256')
        if expected:
            from .sources import probe_fingerprint
            if expected!=probe_fingerprint(source):
                raise SourceError('探查启动期间来源或版本已变化，本次未读取或提交结果','SOURCE_VERSION_CHANGED')
        planner.invoke({'source':source})
    except Exception as e:
        Path(sys.argv[2]).write_text(json.dumps({'message':str(e) if isinstance(e,SourceError) else '来源探查失败，请检查连接、权限及文件格式','code':getattr(e,'code','PROBE_FAILED'),'retryable':getattr(e,'retryable',False)},ensure_ascii=False))
        sys.exit(3)
