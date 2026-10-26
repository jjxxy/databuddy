"""Official finance kit A02–A05: real dialogue, original inputs, independent files."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / 'acceptance' / 'finance_monthly'
SHEETS = {'收入底稿': {'range': [2, 5, 9, 15], 'header_rows': [5, 6], 'data_rows': [7, 15], 'rows': 9},
          '成本底稿': {'range': [3, 5, 9, 15], 'header_rows': [5], 'data_rows': [6, 15], 'rows': 10},
          '预算': {'range': [1, 4, 5, 7], 'header_rows': [4], 'data_rows': [5, 7], 'rows': 3},
          '省份映射': {'range': [1, 1, 4, 8], 'header_rows': [1], 'data_rows': [2, 8], 'rows': 7}}
XNS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def official_prompts():
    readme = (KIT / 'README.md').read_text(encoding='utf-8')
    section = readme.split('## A02–A05：', 1)[1].split('若询问表格边界', 1)[0]
    primary = '\n\n'.join(line[2:] for line in section.splitlines() if line.startswith('> '))
    if primary.count('我要做2026年9月办事处经营分析。') != 1 or '先给我口径、异常清单和可核对预览' not in primary:
        raise RuntimeError('Official business prompt could not be extracted unchanged')
    a05 = '请根据已确认的结果表生成1页经营小结Word及3页汇报PPT；列出数值、期间、口径和来源，所有表格和图表只能引用已保存的完整结果，不推测销售变化的外部原因'
    if '提示“' + a05 + '”' not in readme:
        raise RuntimeError('Official A05 prompt changed; review before running')
    return primary, a05


def verify_files(ctx, artifacts, stage):
    """Invoke the official independent CLI on bytes actually downloaded."""
    directory = Path(ctx.evidence) / stage
    directory.mkdir(exist_ok=True)
    candidates, records = [], []
    for artifact in artifacts:
        if not artifact.get('path'):
            continue
        path = Path(artifact['path'])
        if path.suffix.lower() in ('.csv', '.xlsx'):
            candidates.append((path, artifact, None))
        elif path.suffix.lower() in ('.zip', '.handoff') and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    if not entry.filename.lower().endswith('.csv') or entry.file_size > 20 * 1024 * 1024:
                        continue
                    raw = archive.read(entry)
                    extracted = directory / ('downloaded-' + hashlib.sha256(raw).hexdigest()[:24] + '.csv')
                    extracted.write_bytes(raw)  # exact generated CSV, never rewritten or recomputed
                    candidates.append((extracted, artifact, entry.filename))
    for path, artifact, member in candidates:
        sheets = [None]
        if path.suffix.lower() == '.xlsx':
            try:
                with zipfile.ZipFile(path) as archive:
                    book = ET.fromstring(archive.read('xl/workbook.xml'))
                    sheets = [sheet.get('name') for sheet in book.find(XNS + 'sheets')]
            except (OSError, ET.ParseError, zipfile.BadZipFile, KeyError) as exc:
                records.append({'path': str(path), 'passed': False, 'error': str(exc)})
                continue
        for sheet in sheets:
            command = [sys.executable, '-I', '-X', 'utf8', str(KIT / 'verify_export.py'), str(path), '--scenario', 'baseline_sep']
            if sheet is not None:
                command += ['--sheet', sheet]
            completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
            try:
                result = json.loads(completed.stdout)
            except ValueError:
                result = {'passed': False, 'error': 'Official verifier produced no JSON', 'stderr': completed.stderr[:1000]}
            record = {'path': str(path), 'format': path.suffix.lower().lstrip('.'), 'sheet': sheet,
                      'archive_member': member, 'passed': completed.returncode == 0 and result.get('passed') is True,
                      'exit_code': completed.returncode, 'official_verifier': result,
                      'download_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'artifact_id': artifact.get('id'), 'artifact_sha256': artifact.get('sha256'),
                      'artifact_version': artifact.get('version'), 'artifact_kind': artifact.get('kind'),
                      'artifact_status': artifact.get('status')}
            records.append(record)
    save(directory / 'official-verifier-results.json', records)
    return records


def saved_results(ctx):
    output, pending, seen = [], [], set()
    listing = ctx.api('GET', f'/api/projects/{ctx.project_id}/data-workflows')
    for flow in listing.get('items', []):
        for run_id in set(filter(None, [flow.get('candidate_run'), flow.get('published_run')])):
            pending.append((run_id, flow, True, None))
    while pending:
        run_id, flow, current, upstream_ref = pending.pop(0)
        if run_id in seen:
            continue
        if len(seen) >= 30:
            raise RuntimeError('Result provenance graph exceeds the bounded acceptance traversal')
        seen.add(run_id)
        page = ctx.api('GET', f'/api/projects/{ctx.project_id}/data-results/{run_id}?limit=100')
        if upstream_ref and page.get('manifest_sha256') != upstream_ref['manifest_sha256']:
            raise RuntimeError('Upstream result receipt does not match its bound manifest hash')
        document = ctx.api('GET', f'/api/projects/{ctx.project_id}/data-workflows/{page["workflow_id"]}')
        output.append({'run_id': run_id, 'published': bool(current and flow.get('published_run') == run_id),
                       'current': current, 'upstream_of': upstream_ref.get('downstream') if upstream_ref else None,
                       'workflow_id': page['workflow_id'], 'path': flow.get('path'),
                       'plan_sha256': page.get('plan_sha256'), 'manifest_sha256': page.get('manifest_sha256'),
                       'context': page.get('context', {}), 'schema': page.get('schema'), 'stages': page.get('stages', []),
                       'rows': page.get('rows'), 'total': page.get('total'), 'workflow': document})
        for proof in page.get('context', {}).get('input_evidence', {}).values():
            if proof.get('result_id'):
                pending.append((proof['result_id'], {}, False, {'manifest_sha256': proof['manifest_sha256'], 'downstream': run_id}))
    return output


def publications(ctx):
    rows = []
    for flow in ctx.api('GET', f'/api/projects/{ctx.project_id}/data-workflows').get('items', []):
        if flow.get('published_run'):
            rows.append({'kind': 'data_workflow', 'run_id': flow['published_run'], 'plan_sha256': flow.get('active_sha256')})
    for pipeline in ctx.api('GET', '/api/pipelines'):
        if pipeline.get('project_id') == ctx.project_id and pipeline.get('published_run'):
            rows.append({'kind': 'pipeline', 'run_id': pipeline['published_run'], 'plan_sha256': pipeline.get('active_commit')})
    return rows


def _approved_candidates(records, results):
    hashes = {r.get('artifact_sha256') for r in records if r.get('passed') and r.get('artifact_kind') == 'result'}
    return [result for result in results if result.get('current', True) and result.get('manifest_sha256') in hashes]


def _clarification(answer, used):
    options = []
    if re.search(r'表头|范围|边界|合并|页签|坐标', answer) and 'layout' not in used:
        options.append(('layout', '按原始业务口径确认：收入底稿B5:I6是两级表头、B7:I15是原始明细；第16行控制数及B19:C21控制表不计入。成本底稿C5:I5是表头、C6:I15是明细。预算A4:E7，省份映射A1:D8。请保留真实坐标，按已提供口径继续。'))
    if re.search(r'0\.13|13%|百分|比例|税率', answer) and 'percentage' not in used:
        options.append(('percentage', '0.13为已存比例，带%文本除以100；仅对明确来源格式作逐类归一。收入已经不含税，不再除税。'))
    if re.search(r'缓存|新鲜|重算', answer) and 'cache' not in used:
        options.append(('cache', '请使用本次提供的源快照已有缓存生成待核对结果，并保留缓存新鲜度未知的说明；我没有要求你声称调用了Excel重算引擎。若缓存与同一行预算收入减预算成本不一致，请明确指出并停止正式采用。'))
    if re.search(r'金额单位|万元|入账|纳入|排除|研发|映射', answer) and 'business_rules' not in used:
        options.append(('business_rules', '业务定义以已上传业务口径.txt为准：金额按每行金额单位归一到元，万元乘10000；收入和直接成本均只计目标月已入账，成本仅材料费和外包费、排除研发费，负数保留；省份使用发生日期有效且唯一的源表映射，未知或冲突请逐项列出，不凭相似度补全。'))
    if not options:
        return None
    for key, _ in options:
        used.add(key)
    return '\n'.join(text for _, text in options)


def business_scope_confirmation(turn, used):
    """Confirm documented scope once, never numbers, missing business facts or adoption."""
    if turn.get('outcome') != 'needs_input' or 'scope_confirmation' in used:
        return None
    answer = turn.get('answer', '')
    if not re.search(r'确认|核对后|认可', answer) or not re.search(r'口径|规则|业务定义|处理范围|处理方案|预览', answer):
        return None
    remaining = re.sub(r'(?:未发现|没有|无)(?:[\u4e00-\u9fffA-Za-z]{0,8})?(?:重复|冲突|缺失|歧义|未映射)', '', answer)
    if re.search(r'缺少|缺失|未提供|未知|未映射|无有效映射|无映射|冲突|歧义|(?:单据|费用|映射|ID).{0,8}重复|重复.{0,8}(?:单据|费用|金额|ID)|单位不明|含义不明', remaining):
        return None
    used.add('scope_confirmation')
    return ('已确认仅按已上传《业务口径.txt》及原始源表明确的读取范围、单位和有效映射生成待核对候选。'
            '此回复只确认业务口径，不确认当前预览数值，不授权正式采用、发布或补全未知映射、冲突单据。'
            '请按原口径继续生成可独立核对的完整CSV和Excel候选，保留异常与来源证据。')


def audit_sources(ctx, results, workbook_source):
    trace = [step for turn in ctx.turns for step in turn.get('agent_trace', []) if step.get('status') == 'done']
    observed_names, receipts, budget_rows, budget_metadata = set(), [], {}, []
    for step in trace:
        observation = step.get('observation') or {}
        if not isinstance(observation, dict):
            continue
        if step.get('tool') == 'inspect_source_tables':
            observed_names.update(s.get('name') for s in observation.get('sheets', []) if isinstance(s, dict))
        if step.get('tool') == 'read_source_table':
            proof = observation.get('evidence', {})
            selection = proof.get('selection', {})
            file_hash = proof.get('content_hash') or selection.get('file_sha256')
            if selection.get('sheet'):
                observed_names.add(selection['sheet'])
            if (selection.get('sheet') == '预算' and proof.get('source_id') == workbook_source['id']
                    and file_hash == workbook_source.get('content_hash')):
                source_rows = proof.get('source_rows', [])
                offset = observation.get('offset', 0)
                for index, row in enumerate(observation.get('rows', [])):
                    if offset + index < len(source_rows):
                        budget_rows[source_rows[offset + index]] = row
                budget_metadata.append(proof)
    for result in results:
        for alias, proof in result.get('context', {}).get('input_evidence', {}).items():
            receipts.append({'run_id': result['run_id'], 'alias': alias, **proof})
    workbook_receipts = [proof for proof in receipts if proof.get('source_id') == workbook_source['id']]
    observed_names.update(proof.get('selection', {}).get('sheet') for proof in workbook_receipts)
    selections = {}
    for name, wanted in SHEETS.items():
        selections[name] = any(proof.get('selection', {}).get('sheet') == name
                               and all(proof['selection'].get(key) == wanted[key] for key in ('range', 'header_rows', 'data_rows'))
                               and proof.get('rows') == wanted['rows']
                               and proof.get('source_rows') == list(range(wanted['data_rows'][0], wanted['data_rows'][1] + 1))
                               and proof.get('executable_values_complete') is True
                               and proof.get('content_hash') == workbook_source.get('content_hash')
                               for proof in workbook_receipts)
    for proof in workbook_receipts:
        if proof.get('selection', {}).get('sheet') == '预算':
            budget_metadata.append(proof)
    formula_seen = all(any(proof.get('special_cells', {}).get(str(row), {}).get('c_5', {}).get('formula', {}).get('formula') == f'=B{row}-C{row}'
                           and proof.get('special_cells', {}).get(str(row), {}).get('c_5', {}).get('cache_status') == 'available'
                           for proof in budget_metadata) for row in (5, 6, 7))
    budget_cache_checked = set(budget_rows) == {5, 6, 7}
    if budget_cache_checked:
        try:
            budget_cache_checked = all(Decimal(str(row['c_5'])) == Decimal(str(row['c_2'])) - Decimal(str(row['c_3'])) for row in budget_rows.values())
        except (KeyError, InvalidOperation):
            budget_cache_checked = False
    documents = [result.get('workflow') for result in results]
    code_text = json.dumps(documents, ensure_ascii=False, default=str)
    rules_archived = bool(documents) and all(term in code_text for term in ('已入账', '材料费', '外包费', '2026-09'))
    stages_executed = any(result.get('stages') for result in results)
    return {'four_sheets_inspected': set(SHEETS) <= observed_names,
            'four_complete_coordinate_bindings': all(selections.values()), 'individual_selections': selections,
            'budget_formula_and_cache_receipts': formula_seen, 'budget_cache_arithmetic_checked': budget_cache_checked,
            'saved_rule_text_evidence': rules_archived, 'validated_stages_executed': stages_executed,
            'budget_cache_distinct_source_rows': sorted(budget_rows),
            'workflow_versions': [r.get('plan_sha256') for r in results], 'source_receipts': receipts}


def _clean_visible(text):
    text = re.sub(r'https?://\S+|(?<![a-fA-F0-9])[a-fA-F0-9]{24,64}(?![a-fA-F0-9])|(?<![A-Za-z0-9_])(?:r|run)_[A-Za-z0-9_-]{4,100}(?![A-Za-z0-9_-])', '', text)
    text = re.sub(r'[^\s|]+\.(?:xlsx|csv|json|docx|pptx)', '', text, flags=re.I)
    return text.replace('−', '-').replace('￥', '¥')


def _report_blocks(document, word):
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}' if word else '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    blocks = []
    for paragraph in document.iter(ns + 'p'):
        text = ''.join(node.text or '' for node in paragraph.iter(ns + 't'))
        if text.strip():
            blocks.append(text)
    for row in document.iter(ns + 'tr'):
        cells = [''.join(node.text or '' for node in cell.iter(ns + 't')) for cell in row if cell.tag == ns + 'tc']
        if cells:
            blocks.append(' | '.join(cells))
    for table in document.iter(ns + 'tbl'):
        rows = [[''.join(node.text or '' for node in cell.iter(ns + 't')) for cell in row if cell.tag == ns + 'tc']
                for row in table if row.tag == ns + 'tr']
        if len(rows) < 2:
            continue
        for row in rows[1:]:
            if row and row[0].strip() in ('全部', '合计', '总计'):
                blocks.extend(label + '：' + value for label, value in zip(rows[0][1:], row[1:]))
    return blocks


LABELS = re.compile(r'收入预算差额(?:元)?|收入(?:较|比)预算(?:少|低于|不足|超出|增加|减少)?|(?:总|营业)?收入(?:合计)?(?:元)?|直接成本(?:元)?|总成本(?:元)?|成本(?:合计)?(?:元)?|总毛利(?:元)?|毛利(?:元)?')


def _metric_values(blocks):
    """Bind each number to its own label, never a bag of numbers in a document."""
    values = {key: [] for key in ('收入元', '直接成本元', '毛利元', '收入预算差额元')}
    for raw in blocks:
        block = _clean_visible(raw)
        if re.search(r'华东|华北|华南', block) and not re.search(r'全部|全公司|总体|总计', block):
            continue
        labels = list(LABELS.finditer(block))
        for index, label in enumerate(labels):
            before = block[max(0, label.start() - 3):label.start()]
            after = block[label.end():label.end() + 5]
            title = label.group()
            if before.endswith('预算') or re.match(r'[（(]?(?:率|笔数|达成率|预算)', after):
                continue
            clause = re.split(r'[，；;。\n|]', block[:label.start()])[-1] + title
            if re.search(r'华东|华北|华南', clause) and not re.search(r'全部|合计|总计|总体|总收入|总成本|总毛利', clause):
                continue
            key = '收入预算差额元' if '预算' in title else '收入元' if '收入' in title else '直接成本元' if '成本' in title else '毛利元'
            end = labels[index + 1].start() if index + 1 < len(labels) else len(block)
            segment = block[label.end():end]
            segment = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', segment)
            number = re.search(r'[-+]?\d+(?:\.\d+)?', segment)
            if number is None or len(segment[:number.start()]) > 45:
                continue
            try:
                value = Decimal(number.group())
                if re.search(r'[（(]\s*$', segment[:number.start()]) and re.match(r'\s*[）)]', segment[number.end():]):
                    value = -value
                if '万元' in title or '万元' in segment[:number.end() + 6]:
                    value *= 10000
                sign_context = title + segment[:number.start()]
                if value > 0 and key == '收入预算差额元' and re.search(r'少|低于|不足|未达|减少|负', sign_context):
                    value = -value
                values[key].append(value)
            except InvalidOperation:
                continue
    return values


def _period_is_september(blocks):
    visible = [_clean_visible(text) for text in blocks if not re.match(r'\s*(?:来源|完整结果|交接摘要|加工方案|数据版本)', text)
               and not re.search(r'采集时间|采集日期|导出时间|更新时间|生成时间|文件时间|数据时间', text)]
    period = re.compile(r'2026(?:年|[-/.])\s*(\d{1,2})(?:月|(?!\d))')
    explicit = [match.group(1) for text in visible if re.search(r'(?:报告|分析|统计)?期间\s*[：:]', text) for match in period.finditer(text)]
    titles = [match.group(1) for text in visible if re.search(r'经营分析|经营小结|经营报告|月度.*(?:分析|小结)|办事处.*分析', text) for match in period.finditer(text)]
    if explicit or titles:
        return all(int(month) == 9 for month in explicit + titles)
    for text in visible:
        match = period.search(text)
        if match:
            return int(match.group(1)) == 9
    return False


def _version_reference(text, metadata, refs):
    inline = any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(run_id) + r'(?![A-Za-z0-9_-])', text)
                 or digest and digest in text for run_id, digest in refs.items())
    found_runs = set(re.findall(r'(?<![A-Za-z0-9_])(?:r|run)_[A-Za-z0-9_-]{4,100}(?![A-Za-z0-9_-])', text))
    found_runs.update(re.findall(r'(?:完整结果(?:编号)?|结果(?:编号|版本|ID|id)?|运行(?:编号|ID|id)?|run_id)\s*[：:]?\s*([a-f0-9]{24})(?![a-f0-9])', text))
    if found_runs - set(refs):
        return False
    handoffs = re.findall(r'交接摘要\s*[：:]\s*([a-f0-9]{64})', text)
    if any(digest not in set(refs.values()) for digest in handoffs):
        return False
    relevant = [ref for ref in metadata if ref.get('result_id')]
    if any(ref['result_id'] not in refs or ref.get('handoff_sha256') and ref['handoff_sha256'] != refs[ref['result_id']] for ref in relevant):
        return False
    return inline or any(ref['result_id'] in refs for ref in relevant)


def verify_reports(artifacts, approved_results, gold):
    total = next(row for row in gold if row['办事处'] == '全部')
    required = {key: Decimal(str(total[key])) for key in ('收入元', '直接成本元', '毛利元', '收入预算差额元')}
    refs = {r['run_id']: r.get('manifest_sha256') for r in approved_results if r['published']}
    word, slides, details = [], [], []
    for artifact in artifacts:
        if not artifact.get('path'):
            continue
        path = Path(artifact['path'])
        if path.suffix.lower() not in ('.docx', '.pptx'):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                if path.suffix.lower() == '.docx':
                    parts = ['word/document.xml']
                    cached_pages = None
                    if 'docProps/app.xml' in archive.namelist():
                        for node in ET.fromstring(archive.read('docProps/app.xml')).iter():
                            if node.tag.endswith('}Pages'):
                                cached_pages = node.text
                else:
                    parts = sorted(name for name in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', name))
                    cached_pages = len(parts)
                documents = [ET.fromstring(archive.read(part)) for part in parts]
                blocks = [block for document in documents for block in _report_blocks(document, path.suffix.lower() == '.docx')]
                text = '\n'.join(blocks)
                observed = _metric_values(blocks)
                numbers = all(observed[key] and all(value == amount for value in observed[key]) for key, amount in required.items())
                period = _period_is_september(blocks)
                version_ref = _version_reference(text, artifact.get('source_refs', []), refs)
                result = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'numbers_match_saved_complete_csv': numbers, 'period_present': period,
                          'saved_version_reference': version_ref, 'labelled_metric_values': observed,
                          'page_count': cached_pages, 'native_layout_verified': False}
                details.append(result)
                if path.suffix.lower() == '.docx':
                    word.append(result)
                else:
                    slides.append(result)
        except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
            details.append({'path': str(path), 'error': str(exc)})
    valid = lambda value: all(value.get(key) for key in ('numbers_match_saved_complete_csv', 'period_present', 'saved_version_reference'))
    return {'word_actual_file_values_and_version': any(valid(value) for value in word),
            'ppt_actual_file_values_and_version': any(valid(value) for value in slides),
            'ppt_exactly_three_slides': any(valid(value) and value['page_count'] == 3 for value in slides),
            'word_one_page_independently_verified': False,
            'layout_note': 'Word Pages metadata is only a cache. Actual one-page layout and unclipped content require independent rendering; not claimed here.',
            'files': details}


def run(ctx):
    primary, a05_prompt = official_prompts()
    save(Path(ctx.evidence) / 'input-manifest.json', {'inputs_only': [str(p.relative_to(KIT)) for p in sorted((KIT / 'inputs' / 'phase1').iterdir())],
         'expected_files_uploaded': False, 'official_readme_uploaded': False, 'oracle_never_in_model_context': True})
    workbook = ctx.upload(KIT / 'inputs' / 'phase1' / '2026-09_业务源表.xlsx')
    ctx.upload(KIT / 'inputs' / 'phase1' / '业务口径.txt')
    ctx.record['official_kit'] = {'A02_A05': True, 'only_phase1_sources': True, 'clarifications': [], 'approval': None,
                                'workbook_probe': {key: workbook.get(key) for key in ('id', 'status', 'error_code', 'error')}}
    turn = ctx.chat(primary)
    artifacts = ctx.collect()
    validation = verify_files(ctx, artifacts, 'preview-validation')
    results = saved_results(ctx)
    original_publications = publications(ctx)
    original_published = bool(original_publications)
    used = set()
    fact_clarifications = 0
    for index in range(3):
        if _approved_candidates(validation, results):
            break
        if turn.get('outcome') != 'needs_input':
            break
        reply = business_scope_confirmation(turn, used)
        if reply is None:
            if fact_clarifications >= 2:
                break
            reply = _clarification(turn.get('answer', ''), used)
            if reply:
                fact_clarifications += 1
        if not reply:
            break
        ctx.record['official_kit']['clarifications'].append(reply)
        turn = ctx.chat(reply)
        artifacts = ctx.collect()
        validation = verify_files(ctx, artifacts, f'clarification-{index + 1}-validation')
        results = saved_results(ctx)
        found_publications = publications(ctx)
        original_published |= bool(found_publications)
        original_publications.extend(found_publications)
    preview_candidates = _approved_candidates(validation, results)
    selected = None
    if preview_candidates and not original_published:
        selected = preview_candidates[0]
        confirmation = f'确认采用数据结果：运行 {selected["run_id"]}，方案 {selected["plan_sha256"]}'
        ctx.record['official_kit']['approval'] = {'message': confirmation,
            'checked_run': selected['run_id'], 'checked_plan_sha256': selected['plan_sha256'],
            'checked_artifact_hashes': [r['download_sha256'] for r in validation if r.get('passed')],
            'independent_verifier_passed_before_human_confirmation': True}
        turn = ctx.chat(confirmation)
        artifacts = ctx.collect()
        validation = verify_files(ctx, artifacts, 'confirmed-validation')
        results = saved_results(ctx)
    confirmed = _approved_candidates(validation, results)
    confirmed = [result for result in confirmed if result['published'] and selected
                 and result['run_id'] == selected['run_id'] and result['plan_sha256'] == selected['plan_sha256']]
    after_publications = publications(ctx)
    unauthorized_publications = [value for value in after_publications if not selected
                                or value['kind'] != 'data_workflow' or value['run_id'] != selected['run_id']
                                or value['plan_sha256'] != selected['plan_sha256']]
    source_audit = audit_sources(ctx, results, workbook)
    # Private controller-only oracle; never passed to chat(), upload(), or project APIs.
    gold = json.loads((KIT / 'expected' / 'gold.json').read_text(encoding='utf-8'))['baseline_sep']
    selected_validation = [value for value in validation if selected and value.get('artifact_sha256') == selected['manifest_sha256']]
    a04_checks = {'actual_csv_passes_official_baseline_sep': any(r.get('passed') and r.get('format') == 'csv' for r in selected_validation),
                  'actual_xlsx_passes_official_baseline_sep': any(r.get('passed') and r.get('format') == 'xlsx' for r in selected_validation),
                  'no_publication_before_independent_review': not original_published,
                  'confirmation_bound_to_checked_run_and_plan': bool(confirmed),
                  'no_other_result_published': not unauthorized_publications,
                  'confirmed_saved_result_exists': bool(confirmed)}
    report_checks = {'word_actual_file_values_and_version': False, 'ppt_actual_file_values_and_version': False,
                     'ppt_exactly_three_slides': False, 'word_one_page_independently_verified': False,
                     'not_attempted_reason': 'A04 has no independently checked and user-confirmed saved result'}
    if all(a04_checks.values()):
        ctx.chat(a05_prompt)
        artifacts = ctx.collect()
        report_checks = verify_reports(artifacts, confirmed, gold)
    a02 = {'passed': source_audit['four_sheets_inspected'] and source_audit['four_complete_coordinate_bindings'],
           'checks': {key: source_audit[key] for key in ('four_sheets_inspected', 'four_complete_coordinate_bindings', 'individual_selections')}}
    a03_checks = {key: source_audit[key] for key in ('budget_formula_and_cache_receipts', 'budget_cache_arithmetic_checked', 'saved_rule_text_evidence', 'validated_stages_executed')}
    a03_checks['independent_atomic_values_and_counts'] = a04_checks['actual_csv_passes_official_baseline_sep'] and a04_checks['actual_xlsx_passes_official_baseline_sep']
    a03_checks['no_publication_before_independent_review'] = not original_published
    a03 = {'passed': all(a03_checks.values()), 'checks': a03_checks}
    a04 = {'passed': all(a04_checks.values()), 'checks': a04_checks}
    a05 = {'passed': all(report_checks.get(key, False) for key in ('word_actual_file_values_and_version', 'ppt_actual_file_values_and_version', 'ppt_exactly_three_slides', 'word_one_page_independently_verified')),
           'checks': report_checks}
    result = {'A02': a02, 'A03': a03, 'A04': a04, 'A05': a05}
    ctx.record['official_kit'].update({'source_audit': source_audit, 'official_verifier_records': validation,
                                    'publication_before_confirmation': original_publications,
                                    'publication_after_confirmation': after_publications,
                                    'unauthorized_publications': unauthorized_publications, 'acceptance': result})
    save(Path(ctx.evidence) / 'finance-monthly-acceptance.json', result)
    return {'passed': all(value['passed'] for value in result.values()), 'checks': result,
            'scope': 'Official A02–A05 only; phase2 and negative cases not silently treated as passed'}
