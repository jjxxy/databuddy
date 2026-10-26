"""Real-model multi-tab Excel depth acceptance; no expected answers reach the model."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET

OFFICES = ('华东', '华南', '华北', '西部')
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
NODE = Path('C:/Users/001/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
TOOLS = Path(__file__).with_name('complex_excel_tools')
FIXTURE_SHA256 = '8e54fd16b32606200e3a7396d1bb00656041def364fde2256c0723919b9e6b95'


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def input_and_golden():
    """Integer generators and Decimal arithmetic are independent of Excel's engine."""
    detail, refunds, budgets = [], [], []
    cent = Decimal('0.01')
    for i in range(1, 1001):
        day = '2026-09-30' if i % 50 == 0 else '2026-11-01' if i % 37 == 0 else f'2026-10-{(i * 7) % 31 + 1:02d}'
        status = '已取消' if i % 11 == 0 else '未付款' if i % 7 == 0 else '已付款'
        price = Decimal((i * 7919) % 150000 + 5000) / 100
        discount = Decimal((10000, 9500, 9000, 8750, 9800)[i % 5]) / 10000
        quantity = (i * 7) % 20 + 1
        detail.append({'order_id': f'SO-2026-{i:05d}', 'date': day, 'office': OFFICES[(i * 3) % 4],
                       'customer': f'客户{(i * 17) % 83 + 1:03d}', 'status': status, 'quantity': quantity,
                       'unit_price': float(price), 'discount': float(discount),
                       '_gross': (quantity * price * discount).quantize(cent, rounding=ROUND_HALF_UP), '_row': i + 4})
    for j in range(1, 121):
        yuan = Decimal((j * 233) % 30000 + 25) / 100
        refunds.append({'refund_id': f'RF-{j:04d}', 'order_id': f'SO-2026-{(j * 31) % 97 + 1:05d}',
                        'date': '2026-09-30' if j % 19 == 0 else f'2026-10-{(j * 7) % 31 + 1:02d}',
                        'status': '待审核' if j % 9 == 0 else '已作废' if j % 17 == 0 else '已完成',
                        'amount_wan': float(yuan / 10000), '_yuan': yuan, '_row': j + 4})
    for month in (9, 10, 11):
        for k, office in enumerate(OFFICES):
            amount = Decimal('100.123456') + k * Decimal('12.3456') + (month - 10) * 10
            budgets.append({'month': f'2026-{month:02d}', 'office': office, 'amount_wan': float(amount),
                            '_yuan': amount * 10000})
    eligible = {r['order_id']: r for r in detail if r['date'].startswith('2026-10-') and r['status'] == '已付款'}
    refund_by_order = defaultdict(lambda: Decimal(0))
    for refund in refunds:
        if refund['date'].startswith('2026-10-') and refund['status'] == '已完成' and refund['order_id'] in eligible:
            refund_by_order[refund['order_id']] += refund['_yuan']
    lines, summary = {}, {}
    for office in OFFICES:
        summary[office] = {'orders': 0, 'gross': Decimal(0), 'refund': Decimal(0), 'net': Decimal(0),
                           'budget': next(b['_yuan'] for b in budgets if b['month'] == '2026-10' and b['office'] == office)}
    for ident, row in eligible.items():
        values = {'office': row['office'], 'gross': row['_gross'], 'refund': refund_by_order[ident],
                  'net': row['_gross'] - refund_by_order[ident], 'source_row': row['_row']}
        lines[ident] = values
        group = summary[row['office']]
        group['orders'] += 1
        for key in ('gross', 'refund', 'net'):
            group[key] += values[key]
    for group in summary.values():
        group['variance'] = group['net'] - group['budget']
    totals = {key: sum(group[key] for group in summary.values()) for key in ('orders', 'gross', 'refund', 'net', 'budget', 'variance')}
    public = {key: [{k: v for k, v in row.items() if not k.startswith('_')} for row in rows]
              for key, rows in [('detail', detail), ('refunds', refunds), ('budgets', budgets)]}
    golden = {'lines': lines, 'summary': summary, 'totals': totals,
              'formula_caches': {f'I{row["_row"]}': row['_gross'] for row in detail},
              'input_rows': {'销售明细': len(detail), '退款记录': len(refunds), '月度预算': len(budgets)}}
    return public, golden


def _xlsx(path):
    """Read saved OOXML cells/caches without computing or authoring a workbook."""
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(item.itertext()) for item in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(archive.read('xl/workbook.xml')).find('s:sheets', NS):
            target = rels[sheet.get('{' + NS['r'] + '}id')]
            part = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            document = ET.fromstring(archive.read(part))
            rows, formula = [], {}
            for row in document.findall('s:sheetData/s:row', NS):
                cells = {}
                for cell in row.findall('s:c', NS):
                    coordinate = cell.get('r')
                    raw = cell.findtext('s:v', default='', namespaces=NS)
                    kind = cell.get('t')
                    value = strings[int(raw)] if kind == 's' and raw else ''.join(cell.find('s:is', NS).itertext()) if kind == 'inlineStr' else raw
                    cells[re.sub(r'\d', '', coordinate)] = value
                    if cell.find('s:f', NS) is not None:
                        formula[coordinate] = {'formula': cell.findtext('s:f', namespaces=NS), 'cache': raw, 'kind': kind}
                if cells:
                    rows.append({'row': int(row.get('r')), 'cells': cells})
            result[sheet.get('name')] = {'rows': rows, 'formulas': formula,
                                        'merged': [m.get('ref') for m in document.findall('s:mergeCells/s:mergeCell', NS)]}
        return result


def prepare_fixture(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    inputs, golden = input_and_golden()
    _json(directory / 'input-data.json', inputs)
    _json(directory / 'expected-private.json', golden)
    fixture = directory / 'complex_excel.xlsx'
    canonical = TOOLS / 'complex_excel.xlsx'
    if canonical.is_file():
        # Replays upload identical bytes; expected answers remain separate.
        if hashlib.sha256(canonical.read_bytes()).hexdigest() != FIXTURE_SHA256:
            raise RuntimeError('Canonical complex Excel fixture changed; preserve the original test bytes.')
        shutil.copyfile(canonical, fixture)
    else:
        if not (TOOLS / 'node_modules' / '@oai' / 'artifact-tool').exists():
            raise RuntimeError('Fixture authoring requires the loader-provided artifact-tool junction; repository dependencies are not used.')
        completed = subprocess.run([str(NODE), str(TOOLS / 'build_fixture.mjs'), str(directory)],
                                   cwd=TOOLS, capture_output=True, text=True, encoding='utf-8', timeout=300)
        (directory / 'fixture-builder.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
        if completed.returncode:
            raise RuntimeError('artifact-tool fixture build failed; see fixture-builder.log')
    workbook = _xlsx(fixture)
    caches = workbook['销售明细']['formulas']
    defects = [coordinate for coordinate, expected in golden['formula_caches'].items()
               if coordinate not in caches or not caches[coordinate]['cache'] or caches[coordinate]['kind'] == 'e'
               or Decimal(caches[coordinate]['cache']) != expected]
    if defects:
        raise RuntimeError('Fixture formula cache differs from independent Decimal arithmetic: ' + ','.join(defects[:8]))
    if len(caches) != 1000 or not all(workbook[name]['merged'] for name in golden['input_rows']):
        raise RuntimeError('Fixture lost required formulas or merged headers during export')
    _json(directory / 'fixture-verification.json', {'sheets': list(workbook), 'formula_caches_verified': len(caches),
          'merged_ranges': {name: sheet['merged'] for name, sheet in workbook.items()},
          'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(), 'golden_never_uploaded': True,
          'canonical_reused': canonical.is_file(), 'original_fixture_sha256': FIXTURE_SHA256})
    return fixture, golden


def _normal(value):
    return re.sub(r'[\s（）()：:]', '', str(value))


ALIASES = {'order_id': ('订单号', '订单编号'), 'office': ('办事处',), 'gross': ('订单收入元', '销售收入元'),
           'refund': ('已完成退款元', '退款金额元', '退款元'), 'net': ('净收入元', '订单净收入元'),
           'budget': ('净收入预算元', '预算元', '预算金额元'), 'variance': ('预算差异元', '差异元'),
           'orders': ('订单数', '有效订单数'), 'source_row': ('销售明细行号', '销售明细原始行', '原始行号')}


def _tables(workbook, required):
    found = []
    for name, sheet in workbook.items():
        for i, row in enumerate(sheet['rows']):
            mapping = {key: col for col, value in row['cells'].items() for key, aliases in ALIASES.items() if _normal(value) in aliases}
            if set(required) <= set(mapping):
                values = [{key: row['cells'].get(col, '') for key, col in mapping.items()} for row in sheet['rows'][i + 1:]]
                found.append((name, values))
                break
    return found


def _money(value):
    return Decimal(str(value).strip().replace(',', '').replace('￥', '').replace('元', '').replace('−', '-'))


def validate_outputs(artifacts, golden):
    checks, workbooks, documents = {}, [], []
    for item in artifacts:
        if not item.get('path'):
            continue
        path = Path(item['path'])
        if path.suffix.lower() == '.xlsx' and path.is_file():
            workbooks.append((path, _xlsx(path)))
        elif path.suffix.lower() == '.docx' and path.is_file():
            with zipfile.ZipFile(path) as archive:
                text = ''.join(ET.fromstring(archive.read('word/document.xml')).itertext())
                documents.append((path, text))
    checks['generated_xlsx_exists'] = bool(workbooks)
    checks['generated_word_exists'] = bool(documents)
    details, summaries, detail_errors, summary_errors = [], [], [], []
    for path, workbook in workbooks:
        for name, rows in _tables(workbook, ('order_id', 'office', 'gross', 'refund', 'net', 'source_row')):
            selected = [r for r in rows if r.get('order_id') in golden['lines']]
            by_id = {r['order_id']: r for r in selected}
            errors = []
            if len(selected) != len(by_id): errors.append('duplicate_order_rows')
            if set(by_id) != set(golden['lines']): errors.append(f'incomplete_orders:{len(by_id)}/{len(golden["lines"])}')
            extra = [r['order_id'] for r in rows if str(r.get('order_id', '')).startswith('SO-') and r['order_id'] not in golden['lines']]
            if extra: errors.append('ineligible_orders:' + ','.join(extra[:5]))
            for ident, row in by_id.items():
                expected = golden['lines'][ident]
                try:
                    if row['office'] != expected['office']: errors.append(ident + ':office')
                    for key in ('gross', 'refund', 'net'):
                        if _money(row[key]) != expected[key]: errors.append(ident + ':' + key)
                    if _money(row['source_row']) != expected['source_row']: errors.append(ident + ':source_row')
                except (ValueError, ArithmeticError): errors.append(ident + ':non_numeric')
            details.append({'file': path.name, 'sheet': name, 'rows': len(by_id), 'errors': errors[:20]})
            detail_errors.append(errors)
        for name, rows in _tables(workbook, ('office', 'orders', 'gross', 'refund', 'net', 'budget', 'variance')):
            by_office = {row['office']: row for row in rows if row.get('office') in OFFICES}
            errors = []
            if set(by_office) != set(OFFICES): errors.append('missing_office')
            if len([row for row in rows if row.get('office') in OFFICES]) != len(by_office): errors.append('duplicate_office')
            for office, row in by_office.items():
                for key in ('orders', 'gross', 'refund', 'net', 'budget', 'variance'):
                    try:
                        if _money(row[key]) != golden['summary'][office][key]: errors.append(office + ':' + key)
                    except (ValueError, ArithmeticError): errors.append(office + ':' + key + ':non_numeric')
            totals = [row for row in rows if row.get('office') in ('总计', '合计', '全部办事处')]
            if len(totals) != 1:
                errors.append('one_explicit_total_required')
            else:
                for key in ('orders', 'gross', 'refund', 'net', 'budget', 'variance'):
                    try:
                        if _money(totals[0][key]) != golden['totals'][key]: errors.append('total:' + key)
                    except (ValueError, ArithmeticError): errors.append('total:' + key + ':non_numeric')
            summaries.append({'file': path.name, 'sheet': name, 'offices': len(by_office), 'errors': errors[:20]})
            summary_errors.append(errors)
    checks['complete_order_file_values_and_row_provenance'] = bool(details) and any(not errors for errors in detail_errors)
    checks['all_office_values_and_unit_conversion'] = bool(summaries) and any(not errors for errors in summary_errors)
    required_numbers = [format(golden['totals'][key], '.2f') for key in ('gross', 'refund', 'net', 'variance')]
    checks['word_actual_numbers_and_source_tabs'] = any(all(number in text.replace(',', '') for number in required_numbers) and all(name in text for name in golden['input_rows']) for _, text in documents)
    return checks, {'detail_tables': details, 'summary_tables': summaries, 'xlsx_files': [p.name for p, _ in workbooks], 'word_files': [p.name for p, _ in documents]}


def _source_evidence(ctx):
    receipts, sheets = [], set()
    listing = ctx.api('GET', f'/api/projects/{ctx.project_id}/data-workflows')
    for flow in listing.get('items', []):
        for run_id in set(filter(None, [flow.get('candidate_run'), flow.get('published_run')])):
            value = ctx.api('GET', f'/api/projects/{ctx.project_id}/data-results/{run_id}?limit=1')
            for alias, evidence in value.get('context', {}).get('input_evidence', {}).items():
                selected = evidence.get('selection', {}).get('sheet')
                receipts.append({'run_id': run_id, 'input': alias, 'sheet': selected,
                                 'source_id': evidence.get('source_id'), 'rows': evidence.get('rows'),
                                 'content_hash': evidence.get('content_hash'), 'complete': evidence.get('complete', not evidence.get('truncated', False))})
                if selected: sheets.add(selected)
    return receipts, sheets


def run(ctx):
    fixture, golden = prepare_fixture(Path(ctx.evidence) / 'fixture')
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise RuntimeError('Depth replay requires the exact original workbook bytes, not a regenerated archive.')
    ctx.record['fixture_provenance'] = {'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
                                      'original_fixture_sha256': FIXTURE_SHA256,
                                      'exact_original_bytes': hashlib.sha256(fixture.read_bytes()).hexdigest() == FIXTURE_SHA256,
                                      'expected_values_uploaded': False}
    source = ctx.upload(fixture)
    source_id = source.get('id', source.get('source_id'))
    ctx.record['source_id'] = source_id
    prompt = '''请对刚上传的《complex_excel.xlsx》完成2026年10月四个办事处净收入与预算核对，并交付实际文件，不只在聊天中写结论。
逐一核对所有相关页签、合并/多级表头、日期、金额单位与公式缓存。销售收入按销售明细中订单日期属于2026年10月且订单状态为“已付款”的订单统计；其他月份、未付款和已取消订单不计入。使用底稿已有订单收入公式的缓存值，不自行猜测缓存新鲜度。
退款仅扣除“退款记录”中退款日期属于2026年10月、状态为“已完成”、且关联到上述合格订单的金额。同一订单可以有多笔退款，只扣一次合计，不能因关联把销售收入重复计算。销售收入是元；退款及月度预算明确是万元，统一换成元再核对。预算只取2026年10月，净收入=订单收入−已完成退款，预算差异=净收入−净收入预算。此处业务规则已经给出，不需要自选口径或外部数据。
交付Excel应包含全部合格订单的完整订单级底表和4办事处汇总及总计。订单底表列：订单号、办事处、订单收入（元）、已完成退款（元）、净收入（元）、销售明细行号。汇总列：办事处、订单数、订单收入（元）、已完成退款（元）、净收入（元）、净收入预算（元）、预算差异（元）。销售明细行号必须对应原工作簿真实行号。不得将样本/前25或前100行当全量。
另生成Word财务简报，写清总收入、退款、净收入、预算差异及主要办事处差异，并说明销售明细、退款记录、月度预算各自来源和万元换算。保存可复用的加工做法，保留真实来源与执行回执。所有结果保持待业务核对，不要自动批准语义或发布。'''
    (Path(ctx.evidence) / 'model-prompt.txt').write_text(prompt, encoding='utf-8')
    turn = ctx.chat(prompt)
    artifacts = ctx.collect()
    checks, observations = validate_outputs(artifacts, golden)
    try:
        receipts, sheets = _source_evidence(ctx)
        checks['executed_three_source_tabs'] = set(golden['input_rows']) <= sheets
        checks['source_receipts_bind_uploaded_file'] = bool(receipts) and all(r['source_id'] == source_id for r in receipts if r['sheet'])
        checks['executed_full_source_counts'] = all(any(r['sheet'] == sheet and r['rows'] == count and r['complete'] for r in receipts) for sheet, count in golden['input_rows'].items())
    except Exception as exc:
        receipts = []
        checks['executed_three_source_tabs'] = False
        observations['source_receipt_error'] = str(exc)
    observations.update({'source_receipts': receipts, 'turn_state': turn.get('state'), 'artifact_count': len(artifacts)})
    ctx.record['complex_excel'] = observations
    _json(Path(ctx.evidence) / 'complex-excel-checks.json', {'checks': checks, 'observations': observations})
    return {'passed': all(checks.values()), 'checks': checks}
