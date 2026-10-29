"""Progress checks for recoverable evidence, without a task/call quota.

Recovery is useful while it reveals a new authorized resource version or range.
New wrapper IDs and unsuccessful guesses are not new business evidence.
"""
import re

RECOVERY_TOOLS=frozenset({'read_tool_result','read_context_original','read_context_checkpoint'})
TRANSPARENT_TOOLS=RECOVERY_TOOLS|{'discover_tools','compact_work_context'}
REFERENCE_INVALID='RECOVERY_REFERENCE_INVALID'
NO_PROGRESS='RECOVERY_NO_PROGRESS'
BLOCKED_CODES=frozenset({REFERENCE_INVALID,NO_PROGRESS,'TOOL_NO_PROGRESS'})


def recovery_epoch(turn):
    """Only server-observed native/checkpoint compaction changes the evidence epoch."""
    session=turn.get('opencode_session_id')
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',session):session=None
    completed=turn.get('opencode_context_epoch',0)
    if type(completed) is not int or completed<0:completed=0
    result={'session_id':session,'compaction':completed}
    projected=turn.get('context_window_epoch',0)
    if type(projected) is int and projected>0:result['projection']=projected
    return result


def _recorded_epoch(value):
    raw=value.get('recovery_epoch')
    if not isinstance(raw,dict):return recovery_epoch({})
    return recovery_epoch({'opencode_session_id':raw.get('session_id'),
                           'opencode_context_epoch':raw.get('compaction'),'context_window_epoch':raw.get('projection')})


def recovery_descriptor(value):
    raw=value.get('recovery') if isinstance(value,dict) else None
    if not isinstance(raw,dict):return None
    if raw.get('kind') not in ('tool_result','context_original','context_checkpoint'):return None
    if raw.get('offset_unit')!='characters':return None
    resource=raw.get('resource_id');version=raw.get('version_sha256')
    if not isinstance(resource,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,256}',resource):return None
    if not isinstance(version,str) or not re.fullmatch(r'[a-f0-9]{64}',version):return None
    for field in ('offset','returned_characters','total_characters'):
        if type(raw.get(field)) is not int or raw[field]<0:return None
    next_offset=raw.get('next_offset')
    if next_offset is not None and (type(next_offset) is not int or next_offset<0):return None
    return {key:raw.get(key) for key in ('kind','resource_id','version_sha256','offset_unit','offset',
            'returned_characters','total_characters','next_offset')}


def _key(value):return value['kind'],value['resource_id'],value['version_sha256'],value['offset_unit']


def _uncovered(intervals,start,end):
    cursor=start
    for left,right in sorted(intervals):
        if right<=cursor:continue
        if left>cursor:return True
        cursor=max(cursor,right)
        if cursor>=end:return False
    return cursor<end


def _range(value):
    return value['offset'],min(value['total_characters'],value['offset']+value['returned_characters'])


def recovery_check(turn,*,value=None,invalid_reference=False):
    """Return a safe blocked receipt, or None for newly recoverable evidence.

    Callers resolve/authorize the candidate first. A correct new reference is
    consequently never rejected because earlier references were invalid.
    """
    trace=turn.get('agent_trace',[])[turn.get('agent_budget_base',0):]
    coverage={};empty=set();stalled=False;epoch=recovery_epoch(turn)
    for prior in trace:
        if prior.get('tool') not in TRANSPARENT_TOOLS:
            observation=prior.get('observation')
            if (prior.get('status')=='done'
                    and not (isinstance(observation,dict) and observation.get('executed') is False)):
                coverage={};empty=set();stalled=False
            continue
        if prior.get('tool') not in RECOVERY_TOOLS:continue
        observed=prior.get('observation') or {}
        if not isinstance(observed,dict):continue
        # Native compaction can legitimately replace previously visible pages
        # with a summary. Their first recovery in the new epoch is useful.
        if _recorded_epoch(observed)!=epoch:continue
        if observed.get('code') in (REFERENCE_INVALID,NO_PROGRESS):
            stalled=True;continue
        descriptor=recovery_descriptor(observed)
        if prior.get('status')!='done' or descriptor is None:continue
        key=_key(descriptor);start,end=_range(descriptor)
        intervals=coverage.setdefault(key,[])
        novel=_uncovered(intervals,start,end) if end>start else key not in empty and descriptor['total_characters']==0
        intervals.append((start,end))
        if descriptor['total_characters']==0:empty.add(key)
        stalled=not novel
    descriptor=recovery_descriptor(value)
    if not invalid_reference:
        # Older integrations without a trustworthy descriptor keep their
        # established behavior; do not guess resource identity from content.
        if descriptor is None:return None
        key=_key(descriptor);start,end=_range(descriptor)
        if end>start and _uncovered(coverage.get(key,[]),start,end):return None
        if descriptor['total_characters']==0 and key not in empty:return None
    code=REFERENCE_INVALID if invalid_reference else NO_PROGRESS
    message=('工具结果引用不可恢复，换编号或分页长度不能修复引用；请使用当前上下文提供的原始引用和所属轮次'
             if invalid_reference else '这段原始资料已经恢复，当前请求没有带来新增内容；请使用已读证据继续实际工作')
    result={'executed':False,'code':code,'error':message,'no_new_evidence':True,
            'next_step':'按已提供的有效引用读取尚未覆盖的区间，或处理业务任务；确实缺少资料时说明缺项，不要继续猜编号或反复回读',
            'recovery_guard':{'pause':stalled,'reason':'invalid_reference' if invalid_reference else 'covered_range'},
            'recovery_epoch':epoch}
    if descriptor:result['recovery']=descriptor
    return result


def repetition_turn(turn):
    """An unexecuted guard receipt must not disguise the original repetition."""
    trace=turn.get('agent_trace',[])[turn.get('agent_budget_base',0):]
    return dict(turn,agent_budget_base=0,agent_trace=[item for item in trace
        if not (isinstance(item.get('observation'),dict) and item['observation'].get('executed') is False
                and item['observation'].get('code') in BLOCKED_CODES)])
