"""Operator-selected default agent runtime; tasks snapshot it at submission.

The setting is metadata only. It contains no executable paths, credentials or
model settings. A conditional database update prevents concurrent operators
from silently overwriting one another, including on multi-process deployments.
"""
import json

from sqlalchemy import select, update

from .store import engine, items, uid

_KEY = 'agent-runtime-default'

def _names():
    # The registry is the single authority for installed adapter identities.
    from .agent_runtime import runtime_names
    return runtime_names()


class SettingsConflict(ValueError):
    pass


def _validated(value):
    name = value.get('name')
    if name == 'atlas_legacy':name = 'nexus_legacy'
    revision = value.get('revision')
    if name is not None and name not in _names():
        raise ValueError('已保存的执行器配置无效，请联系管理员')
    if not isinstance(revision, str) or not revision:
        raise ValueError('已保存的执行器配置版本无效，请联系管理员')
    return {'name': name, 'revision': revision}


def get_state():
    with engine.connect() as connection:
        row = connection.execute(select(items.c.body).where(
            items.c.id == _KEY, items.c.kind == 'system')).first()
    if not row:
        return {'name': None, 'revision': '0'}
    return _validated(json.loads(row[0]))


def get_selected():
    return get_state()['name']


def set_selected(name, expected_revision):
    """Set default for new work; None restores the deployment default."""
    if name is not None and (not isinstance(name, str) or name not in _names()):
        raise ValueError('未知的 Agent 执行器')
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError('请提供当前配置版本')
    from .locks import assert_owned
    assert_owned()
    value = {'id': _KEY, 'name': name, 'revision': uid()}
    encoded = json.dumps(value, ensure_ascii=False)
    with engine.begin() as connection:
        row = connection.execute(select(items.c.body).where(
            items.c.id == _KEY, items.c.kind == 'system')).first()
        if row:
            previous = row[0]
            if _validated(json.loads(previous))['revision'] != expected_revision:
                raise SettingsConflict('执行器配置已变化，请刷新后重试')
            result = connection.execute(update(items).where(
                items.c.id == _KEY, items.c.kind == 'system', items.c.body == previous
            ).values(body=encoded))
            if result.rowcount != 1:
                raise SettingsConflict('执行器配置已变化，请刷新后重试')
        else:
            if expected_revision != '0':
                raise SettingsConflict('执行器配置已变化，请刷新后重试')
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
            result = connection.execute(insert(items).values(id=_KEY,kind='system',body=encoded)
                                        .on_conflict_do_nothing(index_elements=['id']))
            if result.rowcount != 1:
                raise SettingsConflict('执行器配置已变化，请刷新后重试')
    return _validated(value)
