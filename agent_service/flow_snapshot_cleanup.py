"""Conservative, explicit maintenance for orphaned failed-flow snapshots.

No automatic eviction: an authenticated terminal checkpoint is the authority.
Never delete anything when its identity, state, references or ownership are unclear.
"""
import json
import re
import time
from pathlib import Path

from sqlalchemy import select

from .business_flow_data import _index_key, _owner_claim, _table_scope
from .business_flow_engine import digest
from .config import DATA
from .jobs import jobs, render
from .locks import FileLock, assert_owned
from .store import engine, items, transaction, unseal

_MIN_AGE_SECONDS = 24 * 60 * 60
_FILE = re.compile(r'^([a-f0-9]{64})\.enc$')
_HASH = re.compile(r'^[a-f0-9]{64}$')


def _referenced_hashes(value):
    """Keep every hash appearing anywhere, including future checkpoint fields."""
    found = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and _HASH.fullmatch(item):
            found.add(item)
    return found


def _checked_state(job):
    if job.get('kind') != 'business_flow' or job.get('status') not in ('failed', 'cancelled'):
        raise ValueError('仅支持已结束的失败或取消业务流程')
    if not job.get('secret_flow_checkpoint'):
        raise ValueError('缺少已认证检查点，不能判断引用')
    try:
        state = unseal(job['secret_flow_checkpoint'])
    except Exception:
        raise ValueError('检查点无法认证，未清理数据') from None
    if (not isinstance(state, dict) or state.get('version') != 1
            or state.get('status') != 'failed' or state.get('flow_sha256') != job.get('commit')
            or not isinstance(state.get('steps'), list) or not state['steps']):
        raise ValueError('检查点不是明确失败的完整记录，未清理数据')
    for index, step in enumerate(state['steps']):
        expected = 'failed' if index == len(state['steps']) - 1 else 'succeeded'
        if not isinstance(step, dict) or step.get('status') != expected:
            raise ValueError('检查点含未完成、未知或不一致步骤，未清理数据')
        if step['status'] == 'succeeded':
            if 'output' not in step or digest(step['output']) != step.get('output_sha256'):
                raise ValueError('步骤回执不一致，未清理数据')
    return state


def cleanup_failed_run(run_id, *, apply=False):
    """Preview or delete only proven-unreferenced files for one failed run.

    Call only from an administrative maintenance command with filesystem and
    database access; never expose this directly to an unauthenticated client.
    """
    if not isinstance(run_id, str) or not run_id or len(run_id) > 64:
        raise ValueError('运行编号无效')
    if type(apply) is not bool:
        raise ValueError('操作参数无效')
    root = DATA / 'business-flow-tables'
    with FileLock(str(DATA / 'business-flow-tables.lock'), timeout=10):
        # Discover without a row lock, then use the same target -> job lock
        # order as queue, cancellation and result publication. The discovery
        # is not trusted: the locked job must still name the same target.
        with engine.connect() as connection:
            discovered = connection.execute(select(jobs.c.target).where(jobs.c.id == run_id)).first()
        if not discovered:
            raise ValueError('运行记录不存在，未清理数据')
        target_id = discovered[0]
        with transaction() as c:
            flow_row = c.execute(select(items.c.body).where(items.c.kind == 'business_flow',
                items.c.id == target_id).with_for_update()).first()
            if not flow_row:
                raise ValueError('业务流程记录不存在，未清理数据')
            row = c.execute(select(jobs).where(jobs.c.id == run_id).with_for_update()).mappings().first()
            if not row or row['target'] != target_id:
                raise ValueError('运行记录已变化，未清理数据')
            job = render(row)
            if time.time() - row['updated'] < _MIN_AGE_SECONDS:
                raise ValueError('运行结束未满 24 小时，未清理数据')
            state = _checked_state(job)
            flow = json.loads(flow_row[0])
            if (flow.get('project_id') != job.get('project_id') or flow.get('actor_id') != job.get('actor_id')
                    or run_id in (flow.get('published_run'), flow.get('candidate_run'), flow.get('pending_refresh_run'))):
                raise ValueError('运行归属或正式结果状态不明确，未清理数据')
            context = {'project_id': job.get('project_id'), 'actor_id': job.get('actor_id'),
                       'turn_id': job.get('flow_turn_id'), 'run_id': run_id}
            scope = _table_scope(context)
            if not root.exists():
                return {'run_id': run_id, 'files': 0, 'bytes': 0, 'applied': apply}
            folder = root / scope
            if (root.is_symlink() or root.resolve() != root.absolute() or folder.is_symlink()
                    or (folder.exists() and (not folder.is_dir() or folder.resolve() != folder.absolute()))):
                raise ValueError('快照目录不安全，未清理数据')
            if not folder.exists():
                return {'run_id': run_id, 'files': 0, 'bytes': 0, 'applied': apply}
            marker = folder / '.owner.json'
            try:
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 2048:
                    raise ValueError()
                if json.loads(marker.read_bytes()) != _owner_claim(context, scope, _index_key()):
                    raise ValueError()
            except (ValueError, OSError, TypeError):
                raise ValueError('快照归属无法核验，未清理数据') from None
            protected = _referenced_hashes(state)
            candidates = []
            for path in folder.iterdir():
                match = _FILE.fullmatch(path.name)
                if not match or match.group(1) in protected:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError('快照目录包含不安全项目，未清理数据')
                candidates.append((path, path.stat().st_size))
            # Hold both locks while deleting so a new checkpoint cannot publish
            # a reference to a file between the scan and deletion.
            total = sum(size for _, size in candidates)
            if apply:
                for path, _ in candidates:
                    assert_owned()
                    path.unlink()
            return {'run_id': run_id, 'files': len(candidates), 'bytes': total, 'applied': apply}
