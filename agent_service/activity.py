"""Durable workspace activity, device read receipts and opt-in Web Push outbox.

No prompts, result text, project names or credentials enter notification payloads.
The workspace currently has one owner token; devices are not separate users.
"""
import base64,hashlib,ipaddress,json,logging,os,re,socket,time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import Table,Column,Integer,String,Float,Text,UniqueConstraint,select,update,delete,func
from sqlalchemy.exc import IntegrityError
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pydantic import Field
from .models import StrictModel
from .config import TOKEN
from .store import metadata,engine,transaction,items,get,put,patch,all_items,now,uid,seal,unseal

log=logging.getLogger(__name__)
events=Table('activity_events',metadata,Column('seq',Integer,primary_key=True,autoincrement=True),Column('event_key',String(120),unique=True,nullable=False),Column('project_id',String(64),index=True,nullable=False),Column('session_id',String(64)),Column('turn_id',String(64)),Column('run_id',String(64)),Column('kind',String(32),nullable=False),Column('created',Float,nullable=False,index=True),sqlite_autoincrement=True)
reads=Table('activity_reads',metadata,Column('device_id',String(64),primary_key=True),Column('seq',Integer,primary_key=True),Column('created',Float,nullable=False))
outbox=Table('push_outbox',metadata,Column('device_id',String(64),primary_key=True),Column('seq',Integer,primary_key=True),Column('state',String(20),nullable=False),Column('attempts',Integer,nullable=False,default=0),Column('available',Float,nullable=False),Column('lease',String(64)),Column('lease_until',Float),Column('revision',String(64)))
from .store import create_tables
create_tables()
LABELS={'completed':'工作已完成','needs_input':'工作需要你补充信息','approval':'结果等待你确认发布','partial':'工作暂停，仍有未完成项','failed':'工作未完成，请查看原因','cancelled':'工作已停止','updated':'定时更新已完成'}
DEVICE_RE=r'^d_[a-f0-9]{32}$'

class DeviceInput(StrictModel):
    device_id:str=Field(pattern=DEVICE_RE)
class Preferences(StrictModel):
    desktop:bool=False
    quiet:bool=False
    timezone:str=Field(default='Asia/Shanghai',max_length=60)
class SubscriptionInput(StrictModel):
    endpoint:str=Field(max_length=3000)
    keys:dict[str,str]
class ReadInput(StrictModel):
    sequences:list[int]=Field(default_factory=list,max_length=200)
    through:int|None=Field(default=None,ge=0)
class ViewInput(StrictModel):
    project_id:str=Field(max_length=64)
    session_id:str=Field(max_length=64)
    mode:str=Field(default='work',pattern='^(chat|work)$')
    draft:str=Field(default='',max_length=12000)
    source_ids:list[str]=Field(default_factory=list,max_length=20)
    pending:dict|None=None
    anchor_turn_id:str|None=Field(default=None,max_length=64)
    offset:float=Field(default=0,ge=-100000,le=100000)
    at_bottom:bool=True
    revision:int=Field(default=0,ge=0)
    writer:str=Field(default='',max_length=64)

class ViewConflict(ValueError):pass

def token_binding():return hashlib.sha256(TOKEN.encode()).hexdigest()
def max_seq(c):return c.execute(select(func.max(events.c.seq))).scalar() or 0

def device(device_id):
    if not re.fullmatch(DEVICE_RE,device_id):raise ValueError('设备标识无效')
    d=get('browser_device',device_id)
    if d.get('token_binding')!=token_binding():raise ValueError('设备登录状态已变化，请重新登录')
    return d

def register(device_id):
    if not re.fullmatch(DEVICE_RE,device_id):raise ValueError('设备标识无效')
    with transaction() as c:
        row=c.execute(select(items.c.body).where(items.c.id==device_id,items.c.kind=='browser_device').with_for_update()).first()
        d=json.loads(row[0]) if row else {}
        if not d or d.get('token_binding')!=token_binding():
            d={'id':device_id,'created_at':now(),'token_binding':token_binding(),'desktop':False,'quiet':False,'timezone':'Asia/Shanghai','push_enabled':False,'push_revision':uid(),'first_seq':max_seq(c),'first_created':time.time()}
        if not row and c.execute(select(func.count()).select_from(items).where(items.c.kind=='browser_device')).scalar()>=50:raise ValueError('设备数量已达50个上限，请管理员清理长期未使用的设备')
        d['seen_at']=now()
        if row:c.execute(update(items).where(items.c.id==device_id).values(body=json.dumps(d)))
        else:c.execute(items.insert().values(id=device_id,kind='browser_device',body=json.dumps(d)))
    return public_device(d)

def public_device(d):
    return {k:d.get(k) for k in ('id','desktop','quiet','timezone','push_enabled','push_error','last_location')}|{'vapid_public_key':vapid()['public_key'],'push_available':bool(public_origin()),'workspace_scope':'single_owner'}

def public_origin():
    raw=os.getenv('PUBLIC_APP_URL','')
    if not raw and os.getenv('RAILWAY_PUBLIC_DOMAIN'):raw='https://'+os.environ['RAILWAY_PUBLIC_DOMAIN']
    p=urlsplit(raw)
    return p.scheme+'://'+p.netloc if p.scheme=='https' and p.hostname and not p.username and not p.password and not p.query and not p.fragment else ''

def vapid():
    from .config import DATA
    from .locks import FileLock
    with FileLock(str(DATA/'webpush-key.lock'),timeout=5):
        try:return unseal(get('system','webpush-vapid')['secret_config'])
        except KeyError:
            key=ec.generate_private_key(ec.SECP256R1())
            encode=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
            v={'private_key':encode(key.private_numbers().private_value.to_bytes(32,'big')),'public_key':encode(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))}
            put('system',{'secret_config':seal(v)},'webpush-vapid');return v

def preferences(device_id,value):
    device(device_id)
    try:ZoneInfo(value.timezone)
    except Exception:raise ValueError('时区无效') from None
    values=value.model_dump()
    if not value.desktop:values.update(push_enabled=False,subscription=None,push_revision=uid())
    return public_device(patch('browser_device',device_id,**values))

def validate_subscription(value):
    p=urlsplit(value['endpoint']);host=(p.hostname or '').lower()
    allowed=host in ('fcm.googleapis.com','web.push.apple.com') or host.endswith('.push.services.mozilla.com') or host.endswith('.notify.windows.com')
    if p.scheme!='https' or not allowed or p.port not in (None,443) or p.username or p.password or p.fragment:raise ValueError('不支持此浏览器推送地址')
    if set(value.get('keys',{}))!={'p256dh','auth'}:raise ValueError('推送密钥格式无效')
    try:
        decode=lambda s:base64.urlsafe_b64decode(s+'='*((-len(s))%4))
        key=decode(value['keys']['p256dh']);auth=decode(value['keys']['auth'])
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}',v) for v in value['keys'].values()):raise ValueError()
        if len(auth)!=16:raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),key)
    except Exception:raise ValueError('推送密钥格式无效') from None
    return value

def subscribe(device_id,value):
    d=device(device_id)
    if not d.get('desktop'):raise ValueError('请先开启此设备的完成提醒')
    if not public_origin():raise ValueError('此部署尚未配置HTTPS站点地址，不能启用Web Push')
    subscription=validate_subscription(value.model_dump())
    with engine.connect() as c:since=max_seq(c)
    return public_device(patch('browser_device',device_id,subscription=seal(subscription),push_enabled=True,push_error=None,push_revision=uid(),push_since=since,push_started=time.time()))

def unsubscribe(device_id):
    device(device_id)
    patch('browser_device',device_id,subscription=None,push_enabled=False,desktop=False,push_revision=uid(),push_error=None)
    return {'ok':True}

def classify(turn):
    outcome=turn.get('outcome')
    if outcome=='cancelled':return 'cancelled'
    if outcome in ('partial','paused'):return 'partial'
    if turn['state']=='failed' or outcome in ('failed','blocked'):return 'failed' if outcome!='blocked' else 'needs_input'
    if outcome in ('needs_input',):return 'needs_input'
    if (turn.get('result') or {}).get('candidate'):return 'approval'
    return 'completed'

def timestamp(value):
    try:return datetime.fromisoformat(value).timestamp()
    except (ValueError,TypeError):return 0

def reconcile():
    """Catch up after any crash between terminal state, Git finalize and event insert."""
    turns=all_items('turn');linked={t['run_id'] for t in turns if t.get('run_id')}
    candidates=[]
    for t in turns:
        if t.get('state') not in ('done','failed') or not t.get('finished_at'):continue
        if t.get('interaction_mode','work')=='work' and t.get('before_version') and not t.get('work_version'):continue
        if re.fullmatch(r'(停止|取消|停一下|停止处理|取消处理)[。！!\s]*',t.get('text','')):continue
        candidates.append({'event_key':'turn:'+t['id'],'project_id':t['project_id'],'session_id':t.get('session_id'),'turn_id':t['id'],'kind':classify(t),'created':timestamp(t['finished_at'])})
    from .jobs import jobs,render
    with engine.connect() as c:
        scheduled=[render(r) for r in c.execute(select(jobs).where(jobs.c.kind=='pipeline',jobs.c.status.in_(('succeeded','validated','failed','cancelled')),jobs.c.updated>time.time()-30*86400).order_by(jobs.c.updated.desc()).limit(5000)).mappings()]
    for job in scheduled:
        if job['id'] in linked or not job.get('scheduled_key') or job['scheduled_key'].startswith('acceptance:') or job['kind']!='pipeline' or job['status'] not in ('succeeded','validated','failed','cancelled'):continue
        try:p=get('pipeline',job['target'])
        except KeyError:continue
        if not p.get('project_id'):continue
        candidates.append({'event_key':'run:'+job['id'],'project_id':p['project_id'],'run_id':job['id'],'kind':'failed' if job['status']=='failed' else 'cancelled' if job['status']=='cancelled' else 'approval' if p.get('candidate_run')==job['id'] else 'updated','created':timestamp(job.get('finished_at',job['created_at']))})
    from .work_tools import reconcile_actions
    reconcile_actions()
    for action in all_items('work_action'):
        if action['state'] not in ('succeeded','failed','unknown','rejected','cancelled','expired'):continue
        candidates.append({'event_key':'action:'+action['id'],'project_id':action['project_id'],'session_id':action['session_id'],'turn_id':action['turn_id'],'kind':'completed' if action['state']=='succeeded' else 'cancelled' if action['state'] in ('rejected','cancelled') else 'partial','created':timestamp(action.get('finished_at') or action.get('decided_at') or action['created_at'])})
    cutoff=time.time()-30*86400
    for value in sorted(candidates,key=lambda x:(x['created'],x['event_key']))[-5000:]:
        if value['created']<cutoff:continue
        try:
            with engine.begin() as c:
                if not c.execute(select(events.c.seq).where(events.c.event_key==value['event_key'])).first():c.execute(events.insert().values(**value))
        except IntegrityError:pass
    with transaction() as c:
        retained=list(c.execute(select(events.c.seq).order_by(events.c.created.desc(),events.c.seq.desc()).limit(5000)).scalars())
        expired=select(events.c.seq).where((events.c.created<cutoff)|(~events.c.seq.in_(retained)))
        c.execute(reads.delete().where(reads.c.seq.in_(expired)));c.execute(outbox.delete().where(outbox.c.seq.in_(expired)));c.execute(events.delete().where((events.c.created<cutoff)|(~events.c.seq.in_(retained))))


def event_json(row):return {k:row.get(k) for k in ('seq','project_id','session_id','turn_id','run_id','kind','created')}|{'label':LABELS[row['kind']]}

def feed(device_id,cursor=0,limit=50):
    d=device(device_id);limit=max(1,min(limit,100))
    with engine.connect() as c:
        maximum=max_seq(c)
        if cursor>maximum:cursor=0 # a restored database may have an older high-water mark
        read_ids=select(reads.c.seq).where(reads.c.device_id==device_id)
        unread=c.execute(select(events).where(events.c.seq>d['first_seq'],events.c.created>=d.get('first_created',0),~events.c.seq.in_(read_ids)).order_by(events.c.seq.desc()).limit(200)).mappings().all()
        if cursor:
            rows=c.execute(select(events).where(events.c.seq>cursor).order_by(events.c.seq).limit(limit+1)).mappings().all()
            more=len(rows)>limit;rows=rows[:limit];next_cursor=rows[-1]['seq'] if rows else max(cursor,maximum)
        else:
            rows=list(reversed(c.execute(select(events).order_by(events.c.seq.desc()).limit(limit)).mappings().all()));more=False;next_cursor=maximum
    unread_ids={r['seq'] for r in unread}
    active=[]
    for t in all_items('turn'):
        if t.get('state') not in ('done','failed'):
            active.append({'turn_id':t['id'],'project_id':t['project_id'],'session_id':t.get('session_id'),'phase':'queued' if t['state']=='queued' else 'running','created_at':t['created_at']})
    services={}
    for role in ('worker','agent'):
        try:services[role]=time.time()-timestamp(get('system',role+'-health')['heartbeat'])<20
        except KeyError:services[role]=False
    from .runtime import healthy
    services={role:healthy(role) for role in ('worker','agent')}
    if os.getenv('AGENT_ORCHESTRATION_MODE')!='separate':services['agent']=services['worker']
    return {'services':services,'events':[dict(event_json(r),unread=r['seq'] in unread_ids) for r in rows],'unread':[event_json(r) for r in unread],'unread_count':len(unread),'unread_count_capped':len(unread)==200,'cursor':next_cursor,'has_more':more,'active':active[:200],'active_count':len(active),'server_time':now()}

def mark_read(device_id,value):
    device(device_id)
    with transaction() as c:
        maximum=max_seq(c)
        ids=set(value.sequences)
        if value.through is not None:
            ids.update(c.execute(select(events.c.seq).where(events.c.seq<=min(value.through,maximum))).scalars().all())
        valid=c.execute(select(events.c.seq).where(events.c.seq.in_(ids))).scalars().all() if ids else []
        for seq in valid:
            if not c.execute(select(reads.c.seq).where(reads.c.device_id==device_id,reads.c.seq==seq)).first():c.execute(reads.insert().values(device_id=device_id,seq=seq,created=time.time()))
    return {'ok':True}


def view_key(device_id,pid,cid):return 'bv_'+hashlib.sha256((device_id+':'+pid+':'+cid).encode()).hexdigest()[:40]
def read_view(device_id,pid,cid):
    device(device_id)
    from .conversation import check_session
    get('project',pid);check_session(pid,cid)
    try:return unseal(get('browser_view',view_key(device_id,pid,cid))['secret_view'])
    except KeyError:return {'revision':0,'draft':'','pending':None,'at_bottom':True}

def save_view(device_id,value):
    device(device_id)
    from .conversation import check_session,redact,TurnInput,owned
    get('project',value.project_id);check_session(value.project_id,value.session_id)
    raw=value.model_dump()
    if len(value.source_ids)!=len(set(value.source_ids)) or any(not isinstance(identifier,str) or not 1<=len(identifier)<=64 for identifier in value.source_ids):raise ValueError('草稿附件列表无效')
    for identifier in value.source_ids:owned('source',identifier,value.project_id)
    if redact(value.draft)!=value.draft or re.search(r'https?:|password|api[_-]?key|token|secret|authorization|Bearer',value.draft,re.I):raw['draft']=''
    if value.pending:
        pending=TurnInput.model_validate(value.pending)
        for identifier in pending.source_ids or ([pending.source_id] if pending.source_id else []):owned('source',identifier,value.project_id)
        raw['pending_hint']={'request_id':pending.request_id}
        if pending.session_id!=value.session_id:raise ValueError('待确认消息不属于当前对话')
        if redact(pending.text)!=pending.text or re.search(r'https?:|password|api[_-]?key|token|secret|authorization|Bearer',pending.text,re.I):raw['pending']=None
    if value.anchor_turn_id:
        t=get('turn',value.anchor_turn_id)
        if t['project_id']!=value.project_id or (t.get('session_id') or __import__('agent_service.conversation',fromlist=['default_session']).default_session(value.project_id))!=value.session_id:raise ValueError('阅读位置不属于当前对话')
    key=view_key(device_id,value.project_id,value.session_id)
    with transaction() as c:
        row=c.execute(select(items.c.body).where(items.c.id==key,items.c.kind=='browser_view').with_for_update()).first()
        old=unseal(json.loads(row[0])['secret_view']) if row else {'revision':0}
        if old['revision']!=value.revision:raise ViewConflict('此对话已在另一标签页更新；保留当前输入，请重新载入服务器草稿后再保存')
        raw.update(revision=old['revision']+1,updated_at=now())
        record={'id':key,'device_id':device_id,'updated':time.time(),'secret_view':seal(raw)}
        if row:c.execute(update(items).where(items.c.id==key).values(body=json.dumps(record)))
        else:c.execute(items.insert().values(id=key,kind='browser_view',body=json.dumps(record)))
        drow=c.execute(select(items.c.body).where(items.c.id==device_id,items.c.kind=='browser_device').with_for_update()).first();d=json.loads(drow[0]);d['last_location']={'project_id':value.project_id,'session_id':value.session_id};c.execute(update(items).where(items.c.id==device_id).values(body=json.dumps(d)))
    return {'revision':raw['revision'],'draft_saved':raw['draft']==value.draft,'pending_saved':raw['pending'] is not None}


def queue_pushes():
    devices=[d for d in all_items('browser_device') if d.get('token_binding')==token_binding() and d.get('desktop') and d.get('push_enabled') and d.get('subscription')]
    for d in devices:
        with transaction() as c:
            already=select(outbox.c.seq).where(outbox.c.device_id==d['id'])
            seen=select(reads.c.seq).where(reads.c.device_id==d['id'])
            rows=c.execute(select(events.c.seq).where(events.c.seq>d.get('push_since',0),events.c.created>max(time.time()-86400,d.get('push_started',0)),~events.c.seq.in_(already),~events.c.seq.in_(seen)).order_by(events.c.seq).limit(100)).scalars().all()
            for seq in rows:c.execute(outbox.insert().values(device_id=d['id'],seq=seq,state='queued',attempts=0,available=time.time(),revision=d.get('push_revision')))


def pinned_push(subscription,payload):
    """pywebpush owns crypto; this transport pins DNS and never follows redirects."""
    validate_subscription(subscription)
    from .network_policy import check_url
    check_url(subscription['endpoint'])
    from pywebpush import webpush
    import requests
    class Transport:
        def post(self,endpoint,data,headers,timeout):
            check_url(endpoint)
            original=httpx.URL(endpoint)
            ips=list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(original.host,443,type=socket.SOCK_STREAM)))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise ValueError('推送目标解析为非公网地址')
            target=original.copy_with(host=ips[0]);headers=dict(headers,Host=original.netloc.decode())
            with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as c:
                with c.stream('POST',target,content=data,headers=headers,extensions={'sni_hostname':original.host}) as r:
                    chunks=[];size=0
                    for chunk in r.iter_bytes():
                        size+=len(chunk)
                        if size>65536:raise ValueError('推送响应超过限制')
                        chunks.append(chunk)
                    response=requests.Response();response.status_code=r.status_code;response.reason=r.reason_phrase;response._content=b''.join(chunks);response.headers.update(r.headers)
                    return response
    return webpush(subscription_info=subscription,data=json.dumps(payload,ensure_ascii=False),vapid_private_key=vapid()['private_key'],vapid_claims={'sub':public_origin()},requests_session=Transport(),timeout=8,ttl=86400,headers={'Urgency':'normal','Topic':hashlib.sha256((payload['project_id']+':'+(payload.get('session_id') or '')).encode()).hexdigest()[:32]})


def dispatch_once():
    t=time.time()
    from .maintenance import guard
    with guard(),transaction() as c:
        row=c.execute(select(outbox).where(((outbox.c.state.in_(('queued','retrying')))&(outbox.c.available<=t))|((outbox.c.state=='sending')&(outbox.c.lease_until<t))).order_by(outbox.c.seq).limit(1).with_for_update(skip_locked=True)).mappings().first()
        if not row:return False
        row=dict(row);lease=uid();c.execute(update(outbox).where(outbox.c.device_id==row['device_id'],outbox.c.seq==row['seq']).values(state='sending',lease=lease,lease_until=t+45))
        event=c.execute(select(events).where(events.c.seq==row['seq'])).mappings().first()
        read=c.execute(select(reads.c.seq).where(reads.c.device_id==row['device_id'],reads.c.seq==row['seq'])).first()
    state='skipped';error=None;delay=0;attempts=row['attempts']
    try:
        try:d=device(row['device_id'])
        except (KeyError,ValueError):d={}
        if event and not read and event['created']>t-86400 and d.get('desktop') and d.get('push_enabled') and d.get('subscription') and d.get('push_revision')==row['revision']:
            local=datetime.now(ZoneInfo(d.get('timezone','Asia/Shanghai')))
            if d.get('quiet') and (local.hour>=22 or local.hour<8):state='retrying';delay=300
            else:
                attempts+=1
                pinned_push(unseal(d['subscription']),event_json(dict(event)))
                state='sent'
    except Exception as exc:
        status=getattr(getattr(exc,'response',None),'status_code',None)
        if status in (404,410):
            with guard():patch('browser_device',row['device_id'],subscription=None,push_enabled=False,push_error='订阅已失效，请重新开启提醒',push_revision=uid())
            state='expired'
        else:state='failed' if attempts>=6 else 'retrying';delay=min(3600,10*3**min(attempts,5));error=type(exc).__name__
        log.warning('Push delivery deferred: %s',error or state)
    with guard(),transaction() as c:c.execute(update(outbox).where(outbox.c.device_id==row['device_id'],outbox.c.seq==row['seq'],outbox.c.lease==lease).values(state=state,attempts=attempts,available=t+delay,lease=None,lease_until=None))
    return True


def run(stop):
    last=0
    while not stop.is_set():
        try:
            if time.monotonic()-last>3:
                from .maintenance import guard
                with guard():reconcile();queue_pushes()
                last=time.monotonic()
            worked=dispatch_once()
        except Exception as exc:
            log.warning('Activity reconciliation deferred: %s',type(exc).__name__);worked=False
        stop.wait(.4 if worked else 2)
