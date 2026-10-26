"""Atomic daily request reservations. Reservations are not a billing estimate."""
import json,os
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select,update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .store import engine,items,transaction
from .config import TIMEZONE

def _configured_limit(name):
    raw=os.getenv(name)
    if raw is None or not raw.strip():return None
    value=int(raw)
    if value<0:raise ValueError('每日模型预算不能为负数')
    return value


def reserve(max_tokens):
    if type(max_tokens) is not int or max_tokens<0:raise ValueError('模型请求预算计量必须是非负整数')
    calls=_configured_limit('MAX_DAILY_MODEL_CALLS');tokens=_configured_limit('MAX_DAILY_RESERVED_TOKENS')
    day=datetime.now(ZoneInfo(TIMEZONE)).date().isoformat();key='quota-model-'+day
    with transaction() as c:
        insert=pg_insert if engine.dialect.name=='postgresql' else sqlite_insert
        c.execute(insert(items).values(id=key,kind='model_quota',body=json.dumps({'id':key,'calls':0,'reserved_output_tokens':0,'day':day})).on_conflict_do_nothing(index_elements=['id']))
        row=c.execute(select(items.c.body).where(items.c.id==key).with_for_update()).one();value=json.loads(row[0])
        if (calls is not None and value['calls']>=calls
                or tokens is not None and value['reserved_output_tokens']+max_tokens>tokens):
            from .provider import BudgetExceeded
            raise BudgetExceeded('工作空间今日模型请求预算已用完，任务已保留；请调整预算或次日继续')
        value.update(calls=value['calls']+1,reserved_output_tokens=value['reserved_output_tokens']+max_tokens)
        c.execute(update(items).where(items.c.id==key).values(body=json.dumps(value)))
    return value
