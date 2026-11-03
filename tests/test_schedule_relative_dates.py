from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent_service.schedule_parameters import resolve_schedule_parameters


def document():
    return {'name': '期间参数', 'inputs': ['period'],
            'input_specs': {'period': {'type': 'date', 'label': '期间'}},
            'nodes': [{'id': 'm', 'kind': 'model', 'title': '使用日期',
                       'inputs': {'content': {'source': 'input', 'name': 'period'}}}],
            'outputs': ['m']}


@pytest.mark.parametrize(('rule', 'expected'), [
    ('scheduled_day', '2024-03-01'), ('month_start', '2024-03-01'),
    ('previous_month_start', '2024-02-01'), ('previous_month_end', '2024-02-29')])
def test_relative_dates_use_due_local_date_and_leap_year(rule, expected):
    # The UTC date is still February but the scheduled local date is March.
    due = datetime(2024, 2, 29, 16, tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    stored = {'period': {'relative_date': rule}}
    assert resolve_schedule_parameters(document(), stored, due) == {'period': expected}
    assert stored == {'period': {'relative_date': rule}}


def test_invalid_rule_and_naive_due_are_rejected():
    with pytest.raises(ValueError):
        resolve_schedule_parameters(document(), {'period': {'relative_date': []}}, datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        resolve_schedule_parameters(document(), {'period': {'relative_date': 'holiday'}}, datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        resolve_schedule_parameters(document(), {'period': '2024-01-01'}, datetime(2024, 1, 1))
