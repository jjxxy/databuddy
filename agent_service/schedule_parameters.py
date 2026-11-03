"""Resolve explicit calendar-date schedule parameters against the due instant."""
from datetime import timedelta

from .business_flow_engine import BusinessFlow

_RELATIVE_DATES = frozenset(('scheduled_day', 'month_start', 'previous_month_start', 'previous_month_end'))


def resolve_schedule_parameters(document, inputs, scheduled_local):
    if scheduled_local.tzinfo is None or scheduled_local.utcoffset() is None:
        raise ValueError('定时业务日期缺少时区')
    definition = BusinessFlow.model_validate(document)
    result = dict(inputs)
    today = scheduled_local.date()
    first = today.replace(day=1)
    previous_end = first - timedelta(days=1)
    dates = {'scheduled_day': today, 'month_start': first,
             'previous_month_start': previous_end.replace(day=1), 'previous_month_end': previous_end}
    for name, spec in (definition.input_specs or {}).items():
        if name not in result:
            raise ValueError('定时参数记录不完整')
        value = result[name]
        if isinstance(value, dict):
            if spec.type != 'date' or set(value) != {'relative_date'} or not isinstance(value.get('relative_date'), str) or value['relative_date'] not in _RELATIVE_DATES:
                raise ValueError('定时业务日期规则无效')
            value = dates[value['relative_date']].isoformat()
            result[name] = value
        spec.validate_value(value)
    return result
