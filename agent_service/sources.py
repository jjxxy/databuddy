"""Immutable connection revisions and source snapshots for task reproducibility."""
from .store import get,put,uid,patch,now
import hashlib,json

# A probe may take minutes. Its original input must still describe the current
# source when it writes; otherwise stale output could revert a replaced file,
# alter privacy/provenance, or attach observations to a different version.
PROBE_INPUT_KEYS=('project_id','kind','name','goal','revision','revision_id','content_hash','bytes',
                  'secret_config','skip_model_planning','origin_source_id','origin_source_revision',
                  'origin_agent_handoff','external_provenance','semantic_alignment_required','requires_source_review',
                  'requires_semantic_review','fact_review_id','visual_fact_review_id','browser_download_id',
                  'local_origin','archived','deleted_at','status','profile')


def probe_fingerprint(source):
    """Bind a probe subprocess to the captured inputs without exposing them."""
    value=[source.get(key) for key in PROBE_INPUT_KEYS]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def patch_probe_if_current(source,values,*,remove=()):
    """Atomically update probe fields if all captured inputs are still current.

    This serializes with other database writers; it does not protect the gap
    between a file check and filesystem publication in unrelated code.
    """
    from sqlalchemy import select,update
    from . import store
    ident=source['id']
    with store.transaction() as connection:
        row=connection.execute(select(store.items.c.body).where(
            store.items.c.kind=='source',store.items.c.id==ident).with_for_update()).first()
        if row is None:return None
        current=store._decode(row[0])
        if not isinstance(current,dict) or any(current.get(key)!=source.get(key) for key in PROBE_INPUT_KEYS):
            return None
        updated=dict(current)
        for key in remove:updated.pop(key,None)
        updated.update(values)
        connection.execute(update(store.items).where(store.items.c.kind=='source',store.items.c.id==ident)
                           .values(body=store._encode('source',updated,connection)))
        return updated

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
