"""Independent reference arithmetic for synthetic fixtures; never calls the agent.

Only regenerates expected CSV/JSON, not Excel sources. Python 3 standard library.
"""
import csv
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADERS = ['月份','办事处','收入元','直接成本元','毛利元','毛利率','预算收入元',
           '预算成本元','收入预算差额元','收入达成率','成本预算差额元','收入笔数','成本笔数']


def amount(value, unit):
    text = str(value).replace('¥', '').replace(',', '')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    return Decimal(text) * {'元': Decimal(1), '万元': Decimal(10000)}[unit]


def reference(month, *, corrected=False, include_rd=False):
    source = json.loads((HERE / 'fixture_definition.json').read_text(encoding='utf-8'))
    data = source['months'][month]
    budgets = {r[0]: (Decimal(str(r[1]))*10000, Decimal(str(r[2]))*10000) for r in data['budgets']}
    totals = {office: [Decimal(0), Decimal(0), 0, 0] for office in budgets}
    for rid, day, province, value, unit, _vat, status, _product in data['sales']:
        if status != '已入账' or day.replace('/', '-')[:7] != month:
            continue
        if corrected and rid == 'S10003':
            value, unit = '102000.40', '元'
        target = totals[source['province_office'][province]]
        target[0] += amount(value, unit)
        target[2] += 1
    for _rid, day, province, category, value, unit, status in data['costs']:
        if status != '已入账' or day.replace('/', '-')[:7] != month:
            continue
        if category not in ('材料费', '外包费') and not (include_rd and category == '研发费'):
            continue
        target = totals[source['province_office'][province]]
        target[1] += amount(value, unit)
        target[3] += 1
    offices = ['华东办事处', '华北办事处', '华南办事处']
    totals['全部'] = [sum(t[i] for t in totals.values()) for i in range(4)]
    budgets['全部'] = tuple(sum(t[i] for t in budgets.values()) for i in range(2))
    rows = []
    money = lambda x: str(x.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    ratio = lambda x, y: str((x/y).quantize(Decimal('.000000000001'), rounding=ROUND_HALF_UP)) if y else ''
    for office in offices + ['全部']:
        revenue, cost, rn, cn = totals[office]
        br, bc = budgets[office]
        rows.append([month, office, money(revenue), money(cost), money(revenue-cost),
                     ratio(revenue-cost, revenue), money(br), money(bc), money(revenue-br),
                     ratio(revenue, br), money(cost-bc), rn, cn])
    return [dict(zip(HEADERS, r)) for r in rows]


def main():
    scenarios = {
        'baseline_sep': reference('2026-09'),
        'baseline_oct': reference('2026-10'),
        'corrected_oct': reference('2026-10', corrected=True),
        'include_rd_sep': reference('2026-09', include_rd=True),
    }
    scenarios['baseline_both'] = scenarios['baseline_sep'] + scenarios['baseline_oct']
    (HERE/'expected').mkdir(exist_ok=True)
    for name, rows in scenarios.items():
        with (HERE/'expected'/f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(rows)
    (HERE/'expected'/'gold.json').write_text(json.dumps(scenarios, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('Reference scenarios:', ', '.join(scenarios))


if __name__ == '__main__':
    main()
