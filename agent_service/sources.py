"""Immutable connection revisions and source snapshots for task reproducibility."""
from .store import get,put,uid,patch,now

def revision(source):
    key=uid();number=source.get('revision',0)+1
    put('source_version',dict(source,source_id=source['id'],id=key,revision=number,recorded_at=now()))
    return patch('source',source['id'],revision=number,revision_id=key)

def snapshot(source_ids):
    values={}
    for sid in dict.fromkeys(source_ids):
        s=get('source',sid)
        if s.get('archived'):raise ValueError('来源已归档，不再采集')
        values[sid]={k:v for k,v in s.items() if k not in ('suggestions','proposed_spec','warning')}
    return values
