"""Local-only MCP subprocess transport using operator-approved exact templates.

This is not an OS sandbox. Only trusted, preinstalled commands may be added to
ATLAS_STDIO_TEMPLATES by the local machine's operator. Models cannot add them.
"""
import hashlib,json,os,re,selectors,signal,subprocess,threading,time,sys
import queue
from pathlib import Path
from .process_control import spawn,stop

MAX_MESSAGE=1024*1024
VERSIONS=('2025-06-18','2025-03-26')
RESERVED={'PATH','HOME','PYTHONPATH','PYTHONHOME','NODE_OPTIONS','LD_PRELOAD','LD_LIBRARY_PATH','APP_TOKEN','ATLAS_OPERATOR_TOKEN','DATABASE_URL'}


def templates():
    try:value=json.loads(os.getenv('ATLAS_STDIO_TEMPLATES','{}'))
    except (ValueError,TypeError):raise ValueError('本地MCP命令模板配置不是有效JSON对象') from None
    if not isinstance(value,dict) or len(value)>40:raise ValueError('本地MCP命令模板配置无效')
    builtin={'atlas-local-clock':{'command':sys.executable,'args':[str(Path(__file__).with_name('mcp_local_clock.py').resolve())]}} if local_mode() else {}
    if os.getenv('ATLAS_NETWORK_MODE','connected')=='local_only':return builtin
    return {**builtin,**value}


def local_mode():
    return os.getenv('ATLAS_LOCAL_MODE')=='1' and bool(os.getenv('ATLAS_LOCAL_WORKSPACE'))


def validate(config):
    if not local_mode():raise ValueError('标准输入输出MCP只允许在本地电脑模式使用')
    root=Path(os.environ['ATLAS_LOCAL_WORKSPACE']).resolve()
    if not root.is_dir():raise ValueError('本地工作文件夹不可用')
    alias=config.get('command','');template=templates().get(alias)
    if not isinstance(template,dict):raise ValueError('此MCP命令未由本机管理员预置，不能启动任意程序')
    command=template.get('command');fixed=template.get('args',[]);extra=config.get('args',[])
    if not isinstance(command,str) or not Path(command).is_absolute() or not Path(command).is_file():raise ValueError('预置MCP命令须为已安装程序的绝对路径')
    if not isinstance(fixed,list) or not isinstance(extra,list) or any(not isinstance(v,str) or '\x00' in v or len(v)>4000 for v in fixed+extra):raise ValueError('MCP命令参数无效')
    if extra and not template.get('allow_extra_args',False):raise ValueError('此预置MCP不允许额外命令参数')
    if len(fixed+extra)>100:raise ValueError('MCP命令参数过多')
    variables=config.get('env',{})
    if not isinstance(variables,dict) or len(variables)>40:raise ValueError('MCP环境配置无效')
    for key,value in variables.items():
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}',key) or key.upper() in RESERVED or key.upper().startswith(('ATLAS_','OPENCODE_','LLM_')):raise ValueError('MCP不能覆盖系统或模型环境配置')
        if not isinstance(value,str) or len(value)>8000 or '\x00' in value:raise ValueError('MCP环境值无效')
    installed_files={}
    for candidate in [command,*fixed]:
        path=Path(candidate)
        if path.is_absolute() and path.is_file() and path.stat().st_size<=32*1024*1024:
            installed_files[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    identity=hashlib.sha256(json.dumps({'template':template,'installed_files':installed_files},sort_keys=True).encode()).hexdigest()
    if config.get('template_sha256') and config['template_sha256']!=identity:raise ValueError('MCP命令模板已变更，请重新连接核对')
    return {'transport':'stdio','command':alias,'args':extra,'env':variables,'template_sha256':identity},[command,*fixed,*extra],root


class Session:
    def __init__(self,config):self.config=config;self.process=None;self.version=VERSIONS[0];self.buffer=bytearray();self.serial=threading.Lock()
    def __enter__(self):
        _,argv,cwd=validate(self.config)
        env={'PATH':os.defpath,'LANG':'C.UTF-8',**self.config.get('env',{})}
        self.process=spawn(argv,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
        if os.name=='nt':
            self.responses=queue.Queue(maxsize=2)
            self.reader_stop=threading.Event()
            def read():
                while not self.reader_stop.is_set():
                    try:line=self.process.stdout.readline(MAX_MESSAGE+1)
                    except (OSError,ValueError):return
                    while not self.reader_stop.is_set():
                        try:self.responses.put(line,timeout=.1);break
                        except queue.Full:continue
                    if not line or len(line)>MAX_MESSAGE:return
            self.reader_thread=threading.Thread(target=read,daemon=True)
            self.reader_thread.start()
        else:
            os.set_blocking(self.process.stdout.fileno(),False)
            os.set_blocking(self.process.stdin.fileno(),False)
        try:
            result=self.request('initialize',{'protocolVersion':self.version,'capabilities':{},'clientInfo':{'name':'atlas-local','version':'0.20.0'}})
            if result.get('protocolVersion') not in VERSIONS or 'tools' not in result.get('capabilities',{}):raise ValueError('本地MCP协议或工具能力尚不兼容')
            self.version=result['protocolVersion'];self.request('notifications/initialized',notification=True)
            return self
        except BaseException:
            self.__exit__(None,None,None);raise
    def request(self,method,params=None,notification=False):
        from .store import uid
        if not self.process or self.process.poll() is not None:raise ValueError('本地MCP进程不可用')
        identity=uid();payload={'jsonrpc':'2.0','method':method}
        if not notification:payload['id']=identity
        if params is not None:payload['params']=params
        raw=json.dumps(payload,ensure_ascii=False).encode()+b'\n'
        if len(raw)>MAX_MESSAGE:raise ValueError('本地MCP请求超过限制')
        with self.serial:
            try:
                deadline=time.monotonic()+max(.05,min(120,float(os.getenv('ATLAS_STDIO_TIMEOUT_SECONDS','30'))))
                if os.name=='nt':return self._windows_request(raw,identity,notification,deadline)
                offset=0
                with selectors.DefaultSelector() as writer:
                    writer.register(self.process.stdin,selectors.EVENT_WRITE)
                    while offset<len(raw):
                        if time.monotonic()>=deadline:raise ValueError('本地MCP请求发送超时，进程已停止')
                        if not writer.select(max(0,min(.2,deadline-time.monotonic()))):continue
                        try:offset+=os.write(self.process.stdin.fileno(),raw[offset:offset+65536])
                        except BlockingIOError:continue
                if notification:return {}
                ignored=0
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdout,selectors.EVENT_READ)
                    while time.monotonic()<deadline:
                        if b'\n' in self.buffer:
                            line,_,tail=self.buffer.partition(b'\n');self.buffer=bytearray(tail)
                            if not line.strip():continue
                            try:value=json.loads(line)
                            except (ValueError,UnicodeError):raise ValueError('本地MCP标准输出必须是JSON-RPC消息') from None
                            if not isinstance(value,dict):raise ValueError('本地MCP响应无效')
                            if value.get('id')!=identity:
                                ignored+=1
                                if ignored>64:raise ValueError('本地MCP返回过多无关消息')
                                continue
                            if 'error' in value or not isinstance(value.get('result'),dict):raise ValueError('本地MCP拒绝了本次请求')
                            return value['result']
                        if not selector.select(max(0,min(.2,deadline-time.monotonic()))):continue
                        chunk=os.read(self.process.stdout.fileno(),65536)
                        if not chunk:raise ValueError('本地MCP在返回结果前退出')
                        self.buffer.extend(chunk)
                        if len(self.buffer)>MAX_MESSAGE:raise ValueError('本地MCP消息超过1MB限制')
                raise ValueError('本地MCP响应超时，进程已停止')
            except (OSError,BrokenPipeError):raise ValueError('本地MCP通信未完成') from None
    def _windows_request(self,raw,identity,notification,deadline):
        written=threading.Event();errors=[]
        def write():
            try:
                offset=0
                while offset<len(raw):
                    count=self.process.stdin.write(raw[offset:])
                    if not count:raise OSError('pipe closed')
                    offset+=count
                self.process.stdin.flush()
            except OSError:errors.append(True)
            finally:written.set()
        threading.Thread(target=write,daemon=True).start()
        if not written.wait(max(0,deadline-time.monotonic())):raise ValueError('本地MCP请求发送超时，进程已停止')
        if errors:raise ValueError('本地MCP通信未完成')
        if notification:return {}
        ignored=0
        while time.monotonic()<deadline:
            try:line=self.responses.get(timeout=max(.001,deadline-time.monotonic()))
            except queue.Empty:break
            if not line:raise ValueError('本地MCP在返回结果前退出')
            if len(line)>MAX_MESSAGE:raise ValueError('本地MCP消息超过1MB限制')
            try:value=json.loads(line)
            except (ValueError,UnicodeError):raise ValueError('本地MCP标准输出必须是JSON-RPC消息') from None
            if not isinstance(value,dict):raise ValueError('本地MCP响应无效')
            if value.get('id')!=identity:
                ignored+=1
                if ignored>64:raise ValueError('本地MCP返回过多无关消息')
                continue
            if 'error' in value or not isinstance(value.get('result'),dict):raise ValueError('本地MCP拒绝了本次请求')
            return value['result']
        raise ValueError('本地MCP响应超时，进程已停止')
    def __exit__(self,*args):
        if self.process:
            if os.name=='nt':self.reader_stop.set()
            stop(self.process,timeout=2)
            self.process.kill()
            for stream in (self.process.stdin,self.process.stdout):
                if stream:stream.close()
            if os.name=='nt':self.reader_thread.join(timeout=2)
