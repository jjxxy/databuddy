from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest

from agent_service import business_flows as bf, config, locks


NOW = datetime(2026, 1, 6, 1, 0, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def scheduler(monkeypatch):
    flows = {}
    submitted = []
    monkeypatch.setattr(bf, 'datetime', Clock)
    monkeypatch.setattr(config, 'TIMEZONE', 'Asia/Shanghai')
    monkeypatch.setattr(bf.workspace, 'lock', lambda _pid: nullcontext())
    monkeypatch.setattr(locks, 'FileLock', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(bf, 'actor_scope', lambda _actor: nullcontext())
    monkeypatch.setattr(bf, 'all_items', lambda _kind: [dict(v) for v in flows.values()])
    monkeypatch.setattr(bf, 'get', lambda _kind, ident: dict(flows[ident]))
    monkeypatch.setattr(bf, 'patch', lambda _kind, ident, **updates: flows[ident].update(updates))
    monkeypatch.setattr(bf, 'unseal', lambda _value: {})
    def submit(*args):
        submitted.append(args)
        return {'status': 'queued'}
    monkeypatch.setattr(bf, 'submit_run', submit)
    def add(ident, due=None):
        flows[ident] = {'id': ident, 'project_id': ident, 'actor_id': 'a', 'cron': '0 9 * * *',
                        'next_due': (due or NOW).isoformat(), 'published_digest': 'sha', 'schedule_inputs': 'sealed'}
        return flows[ident]
    return flows, submitted, add


def test_busy_or_rejected_preserves_due_and_other_flows_continue(scheduler, monkeypatch):
    flows, submitted, add = scheduler
    failed, normal = add('bad'), add('good')
    def submit(pid, *args):
        if pid == 'bad':
            raise ValueError('已有任务运行')
        submitted.append(pid)
        return {'status': 'queued'}
    monkeypatch.setattr(bf, 'submit_run', submit)
    bf.queue_due()
    assert failed['next_due'] == NOW.isoformat()
    assert failed['refresh_retry_after'] == (NOW + timedelta(seconds=30)).isoformat()
    assert '未启动' in failed['refresh_error']
    assert normal['next_due'] == (NOW + timedelta(days=1)).isoformat()
    bf.queue_due()
    assert submitted == ['good']


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'unknown'])
def test_reused_unsuccessful_scheduled_job_does_not_advance(scheduler, monkeypatch, status):
    _, _, add = scheduler
    flow = add('flow')
    monkeypatch.setattr(bf, 'submit_run', lambda *args: {'status': status})
    bf.queue_due()
    assert flow['next_due'] == NOW.isoformat()
    assert '历史运行未成功' in flow['refresh_error']


def test_missed_periods_submit_only_once_and_remain_visible(scheduler):
    _, submitted, add = scheduler
    flow = add('flow', NOW - timedelta(days=3))
    bf.queue_due()
    assert len(submitted) == 1
    assert flow['schedule_gap_pending'] is True
    assert '多个未执行' in flow['refresh_error']
    assert flow['next_due'] == (NOW + timedelta(days=1)).isoformat()


def test_concurrent_disabled_schedule_rechecked_before_admission(scheduler, monkeypatch):
    flows, submitted, add = scheduler
    flow = add('flow')
    snapshots = [dict(flow)]
    monkeypatch.setattr(bf, 'all_items', lambda _kind: snapshots)
    flow['cron'] = None
    bf.queue_due()
    assert submitted == []
    assert flows['flow']['next_due'] == NOW.isoformat()


def test_invalid_due_and_bad_source_do_not_block_other_projects(scheduler):
    _, submitted, add = scheduler
    bad = add('bad')
    bad['next_due'] = 'invalid'
    add('good')
    bf.queue_due()
    assert bad['next_due'] == 'invalid'
    assert bad['cron']
    assert bad['refresh_error']
    assert len(submitted) == 1


def test_daily_schedule_tracks_new_york_dst(scheduler, monkeypatch):
    import sys
    now = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(sys.modules[__name__], 'NOW', now)
    monkeypatch.setattr(config, 'TIMEZONE', 'America/New_York')
    _, submitted, add = scheduler
    flow = add('flow', now)
    bf.queue_due()
    assert len(submitted) == 1
    assert flow['next_due'] == datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc).isoformat()
