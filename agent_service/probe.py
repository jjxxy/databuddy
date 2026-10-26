import json,sys
from pathlib import Path
from .planner import planner
from .store import get
from .connectors import SourceError

if __name__=='__main__':
    try:planner.invoke({'source':get('source',sys.argv[1])})
    except Exception as e:
        Path(sys.argv[2]).write_text(json.dumps({'message':str(e) if isinstance(e,SourceError) else '来源探查失败，请检查连接、权限及文件格式','code':getattr(e,'code','PROBE_FAILED'),'retryable':getattr(e,'retryable',False)},ensure_ascii=False))
        sys.exit(3)
