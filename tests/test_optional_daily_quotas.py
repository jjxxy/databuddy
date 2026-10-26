"""Daily model accounting has no default ceiling; explicit operator caps remain."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from agent_service import quotas
from agent_service.provider import BudgetExceeded
from agent_service.store import get,put,delete


@pytest.fixture(autouse=True)
def isolated_day(monkeypatch):
    from datetime import datetime,timezone
    class FixedClock:
        @staticmethod
        def now(tz):return datetime(2099,12,31,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(quotas,'datetime',FixedClock)
    monkeypatch.delenv('MAX_DAILY_MODEL_CALLS',raising=False)
    monkeypatch.delenv('MAX_DAILY_RESERVED_TOKENS',raising=False)
    delete('model_quota','quota-model-2099-12-31')
    yield
    delete('model_quota','quota-model-2099-12-31')


def test_no_configured_daily_cap_keeps_accounting_beyond_old_defaults():
    put('model_quota',{'calls':5000,'reserved_output_tokens':16000000,'day':'2099-12-31'},'quota-model-2099-12-31')
    result=quotas.reserve(64000)
    assert result['calls']==5001 and result['reserved_output_tokens']==16064000


def test_explicit_daily_call_cap_remains_atomic_under_concurrency(monkeypatch):
    monkeypatch.setenv('MAX_DAILY_MODEL_CALLS','3')
    def reserve(_):
        try:quotas.reserve(10);return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=6) as pool:accepted=list(pool.map(reserve,range(12)))
    assert sum(accepted)==3
    assert get('model_quota','quota-model-2099-12-31')['calls']==3


def test_explicit_token_cap_without_call_cap_is_enforced(monkeypatch):
    monkeypatch.setenv('MAX_DAILY_RESERVED_TOKENS','100')
    quotas.reserve(100)
    with pytest.raises(BudgetExceeded):quotas.reserve(1)
    assert get('model_quota','quota-model-2099-12-31')['reserved_output_tokens']==100


def test_empty_setting_denotes_no_cap(monkeypatch):
    monkeypatch.setenv('MAX_DAILY_MODEL_CALLS','')
    monkeypatch.setenv('MAX_DAILY_RESERVED_TOKENS','')
    assert quotas.reserve(20000000)['reserved_output_tokens']==20000000


@pytest.mark.parametrize('setting',['MAX_DAILY_MODEL_CALLS','MAX_DAILY_RESERVED_TOKENS'])
def test_explicit_zero_setting_preserves_operator_block(monkeypatch,setting):
    monkeypatch.setenv(setting,'0')
    put('model_quota',{'calls':7,'reserved_output_tokens':500,'day':'2099-12-31'},'quota-model-2099-12-31')
    with pytest.raises(BudgetExceeded):quotas.reserve(1)
    assert get('model_quota','quota-model-2099-12-31')['calls']==7
    assert get('model_quota','quota-model-2099-12-31')['reserved_output_tokens']==500


@pytest.mark.parametrize('value',[-1,True,1.5])
def test_invalid_reservation_cannot_refund_or_corrupt_counters(value):
    with pytest.raises(ValueError):quotas.reserve(value)


def test_negative_configured_cap_fails_closed(monkeypatch):
    monkeypatch.setenv('MAX_DAILY_MODEL_CALLS','-1')
    with pytest.raises(ValueError):quotas.reserve(1)
