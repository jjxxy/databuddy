"""Fictitious HR denominator/date acceptance with a hidden Decimal reference."""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.util
import json
from pathlib import Path
import re

# The runner deliberately loads each case by filename, outside a Python package.
_spec = importlib.util.spec_from_file_location('depth_inventory_table_helpers', Path(__file__).with_name('dirty_inventory.py'))
_helpers = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_helpers)
tables, find_table, number, publications = _helpers.tables, _helpers.find_table, _helpers.number, _helpers.publications

SOURCE_COLUMNS = ('employee_id', 'department', 'employment_type', 'employee_status', 'join_date', 'leave_date', 'pay_2026_10')
RESULT_COLUMNS = ('department', 'headcount', 'payroll_total', 'mean_pay', 'as_of')
AS_OF = date(2026, 10, 31)
CENT = Decimal('0.01')


def included(row):
    return (row['employment_type'] in ('正式', '实习') and date.fromisoformat(row['join_date']) <= AS_OF and
            (not row['leave_date'] or date.fromisoformat(row['leave_date']) > AS_OF))


def fixture(folder):
    folder = Path(folder);folder.mkdir(parents=True, exist_ok=True)
    departments = ('研发', '销售', '运营', '职能', '服务')
    rows = []
    for index in range(1, 2001):
        employment_type = '外包' if index % 11 == 0 else '实习' if index % 4 == 0 else '正式'
        joined = '2026-11-01' if index % 17 == 0 else '2026-10-20' if index % 19 == 0 else '2024-01-01'
        left = '' if joined > AS_OF.isoformat() else ('2026-10-15' if index % 7 == 0 else
                '2026-10-31' if index % 29 == 0 else '2026-11-02' if index % 23 == 0 else '')
        # Exported after the period: status alone is not an as-of-date selector.
        status = '已离职' if left else '在职'
        base = 1200 + (index * 97) % 3000 if employment_type == '实习' else 4200 + (index * 137) % 18000
        pay = Decimal(base) + Decimal((index * 17) % 100) / 100
        if index % 47 == 0 or joined > AS_OF.isoformat():pay = Decimal(0)
        rows.append(dict(employee_id=f'SYN_EMP_{index:05}', department=departments[(index * 7) % 5],
            employment_type=employment_type, employee_status=status, join_date=joined, leave_date=left,
            pay_2026_10=format(pay, '.2f')))
    # Keep an exact half-cent mean in one even-sized group, to distinguish the
    # user's HALF_UP policy from banker's rounding, using ordinary cent inputs.
    groups = {department: [row for row in rows if row['department'] == department and included(row)] for department in departments}
    rounding_group = next((group for group in groups.values() if len(group) % 2 == 0), None)
    if rounding_group:
        count = len(rounding_group)
        cents = sum(int(Decimal(row['pay_2026_10']) * 100) for row in rounding_group)
        whole = cents // count
        if whole % 2:whole += 1
        target = whole * count + count // 2
        selected = next(row for row in reversed(rounding_group) if Decimal(row['pay_2026_10']) > 0)
        selected['pay_2026_10'] = format(Decimal(selected['pay_2026_10']) + Decimal(target - cents) / 100, '.2f')
    golden = {}
    for row in rows:
        if not included(row):continue
        item = golden.setdefault(row['department'], {'headcount': 0, 'payroll_total': Decimal(0)})
        item['headcount'] += 1;item['payroll_total'] += Decimal(row['pay_2026_10'])
    for item in golden.values():
        item['mean_pay'] = (item['payroll_total'] / item['headcount']).quantize(CENT, rounding=ROUND_HALF_UP)
        item['as_of'] = AS_OF.isoformat()
    source = folder / '虚构人事与10月工资.csv'
    with source.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=SOURCE_COLUMNS);writer.writeheader();writer.writerows(rows)
    reference = {'input_rows': len(rows), 'summary': golden,
        'included_ids': [row['employee_id'] for row in rows if included(row)],
        'zero_pay_included': sum(included(row) and Decimal(row['pay_2026_10']) == 0 for row in rows),
        'post_cutoff_departures_included': sum(included(row) and bool(row['leave_date']) for row in rows),
        'future_joiners_excluded': sum(row['join_date'] > AS_OF.isoformat() for row in rows),
        'rounding_tie_exercised': bool(rounding_group)}
    return source, reference


def validate_summary(table, golden):
    errors, seen = [], set()
    for row in table['rows']:
        department = str(row.get('department', '')).strip();expected = golden['summary'].get(department)
        if department in seen or expected is None:
            errors.append('duplicate or unexpected department: ' + department);continue
        seen.add(department)
        try:
            for field in ('headcount', 'payroll_total', 'mean_pay'):
                if number(row.get(field)) != Decimal(expected[field]):raise ValueError(field + ' differs')
            value = row.get('as_of')
            if isinstance(value, datetime):value = value.date().isoformat()
            if str(value).strip() != AS_OF.isoformat():raise ValueError('as_of differs')
        except (ValueError, TypeError) as error:errors.append(department + ': ' + str(error))
    if seen != set(golden['summary']):errors.append('department coverage differs')
    return errors


def run(ctx):
    evidence = Path(ctx.evidence);evidence.mkdir(parents=True, exist_ok=True)
    source, golden = fixture(evidence / 'fixtures')
    (evidence / 'golden-hr.json').write_text(json.dumps(golden, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    ctx.upload(source)
    before = publications(ctx)
    first = ctx.chat('老板临时要看各部门人数和人均工资，你帮我把这份表弄成能用的统计。'
                     '离职、月底才离职、外包和实习这些别混着算，文件里的在职状态也别直接照抄。先把影响人数和人均的口径问清楚。')
    answer = str(first.get('answer', ''))
    checks = {'initial_needs_input': first.get('outcome') == 'needs_input',
        'asks_inclusion_scope': bool(re.search(r'外包|实习|正式|范围', answer)),
        'asks_cutoff_or_departure_policy': bool(re.search(r'日期|统计日|时点|月底|期间|离职', answer)),
        'asks_denominator_policy': bool(re.search(r'分母|人数|人均|零工资|工资为.?0', answer)),
        'initial_no_publication': publications(ctx) == before}
    final = ctx.chat('口径定好了：统计日2026-10-31，仅纳入employment_type为正式或实习的员工，外包全部排除。'
        '以日期判断在岗：join_date不晚于统计日，且leave_date为空或晚于统计日；离职日期当日已经不算在岗。'
        'employee_status只是导出时的参考状态，不拿它代替日期判断。人均的分母是上述在岗人数，10月工资为0的人也算人数。'
        '工资合计使用原文件pay_2026_10的实际10月应发金额，单位元，不做入离职天数折算，不补造工资。'
        '人均=工资合计/在岗人数，保留两位小数，按ROUND_HALF_UP四舍五入。'
        '请读取完整文件，交付可下载CSV，列名为department,headcount,payroll_total,mean_pay,as_of。'
        '每个部门一行，as_of写统计日期。文件是待我核对的结果，不要自动发布应用，也不要设置定时。')
    generated, read_errors = tables(ctx.collect())
    csv_tables = [table for table in generated if table['format'] == 'csv']
    result = find_table(csv_tables, RESULT_COLUMNS, validate_summary, golden)
    checks.update(department_csv_exact=result['passed'], followup_terminal=final.get('state') == 'done',
                  no_automatic_publication=publications(ctx) == before)
    ctx.record.update(fixture={'rows': golden['input_rows'], 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'synthetic_only': True, 'oracle_uploaded': False, 'rounding_tie_exercised': golden['rounding_tie_exercised']},
        table_validation={'department_summary': result}, table_read_errors=read_errors)
    return {'passed': all(checks.values()), 'checks': checks}
