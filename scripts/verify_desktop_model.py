"""Opt-in real-model desktop acceptance using synthetic orders only.

Explicit --model-config is mandatory. Its contents are never read by this
controller, copied, printed, or written to evidence: only its path is passed to
the isolated packaged host. The host performs its normal configured model calls.
No test-side Office generation, publication or budget overrides are allowed.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import time
import traceback
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
helper_spec = importlib.util.spec_from_file_location('desktop_acceptance_host',
    ROOT / 'acceptance/offline/verify_backend.py')
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)

PROMPT = (
    '请读取附件中的全部订单，仅统计status严格等于paid的记录，按amount减refund计算净收入。'
    '请生成可编辑的Excel工作簿、Word报告和PPT简报三个实际可下载文件。'
    'Excel必须保留全部已付款订单明细和净收入合计；Word和PPT须写出根据真实数据计算的净收入合计、'
    '计算口径及源文件名。数据金额单位为元。使用现有工具完成，不需要联网搜索。'
    '只生成候选及报告，不要发布、不要确认任何正式结果。'
    '若尚未完成全部三份文件，请如实说明，不得把计划、链接占位或部分成果称为完成。'
)


def plain_xml(raw):
    return ' '.join(node.text or '' for node in ET.fromstring(raw).iter()
                    if node.tag.rsplit('}', 1)[-1] in ('t', 'v'))


def has_total(text):
    for number in re.findall(r'(?<![0-9.])[-+]?\d+(?:\.\d+)?(?![0-9.])', text.replace(',', '')):
        try:
            if Decimal(number) == Decimal('118.15'):
                return True
        except InvalidOperation:
            pass
    return False


def inspect_office(content, kind, source_id):
    if not content.startswith(b'PK'):
        raise AssertionError('Downloaded artifact is not an OOXML ZIP.')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None and '[Content_Types].xml' in archive.namelist()
        if kind == 'docx':
            names = ['word/document.xml']
        elif kind == 'pptx':
            names = sorted(name for name in archive.namelist()
                           if re.fullmatch(r'ppt/slides/slide\d+\.xml', name))
            assert names
        else:
            names = sorted(name for name in archive.namelist()
                           if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'))
            assert names
        if kind == 'xlsx':
            # Inspect resolved worksheet cells, not unused shared strings or
            # sheet coordinates that merely look like the expected order IDs.
            shared = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                shared = [''.join(node.itertext()) for node in
                          ET.fromstring(archive.read('xl/sharedStrings.xml'))]
            values = []
            for name in names:
                for cell in ET.fromstring(archive.read(name)).iter():
                    if cell.tag.rsplit('}', 1)[-1] != 'c':
                        continue
                    value = next((node.text for node in cell if node.tag.rsplit('}', 1)[-1] == 'v'), '')
                    if cell.get('t') == 's' and value:
                        values.append(shared[int(value)])
                    elif cell.get('t') == 'inlineStr':
                        values.append(''.join(node.text or '' for node in cell.iter()
                                              if node.tag.rsplit('}', 1)[-1] == 't'))
                    elif value:
                        values.append(value)
            text = ' '.join(values)
        else:
            text = ' '.join(plain_xml(archive.read(name)) for name in names)
        verified_total = has_total(text)
        provenance = 'offline-orders.csv' in text or source_id in text
        details = kind != 'xlsx' or ('A1' in text and 'A3' in text)
        return {'zip_crc_verified': True, 'ooxml_parts': len(names),
                'exact_118_15_present': verified_total, 'source_reference_present': provenance,
                'paid_order_details_present': details,
                'content_passed': verified_total and details and (provenance or kind == 'xlsx'),
                'visual_validation': False, 'formula_recalculation': False}


def public_trace(turn):
    rows = []
    for step in turn.get('agent_trace', []):
        observation = step.get('observation') or {}
        rows.append({'id': step.get('id'), 'tool': step.get('tool'), 'status': step.get('status'),
                     'observation_keys': sorted(observation) if isinstance(observation, dict) else [],
                     'error': str(observation.get('error', ''))[:1000] if isinstance(observation, dict) else None})
    return rows


def artifacts(host, pid, evidence, sid):
    items = []; offset = 0
    while True:
        catalog = host.json('GET', f'/api/projects/{pid}/artifacts?' + urlencode({'offset': offset, 'limit': 100}))
        items.extend(catalog['items'])
        if catalog.get('next_offset') is None:
            break
        offset = catalog['next_offset']
    downloads = []
    seen = set()
    for item in items:
        for option in item.get('downloads', []):
            kind = option.get('format')
            url = option.get('url')
            if kind not in ('xlsx', 'docx', 'pptx') or not url or (url, kind) in seen:
                continue
            seen.add((url, kind))
            code, content = host.call('GET', url)
            if code != 200:
                downloads.append({'format': kind, 'download_status': code, 'passed': False})
                continue
            checks = inspect_office(content, kind, sid)
            name = f'artifact-{len(downloads)+1:02d}.{kind}'
            (evidence / name).write_bytes(content)
            downloads.append({'format': kind, 'file': name, 'bytes': len(content),
                              'sha256': hashlib.sha256(content).hexdigest(),
                              'checks': checks, 'passed': checks['content_passed']})
    return downloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path,
                        default=ROOT / 'dist/Atlas-DataBuddy-win32-x64-r2/resources/app')
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=420)
    args = parser.parse_args(argv)
    app, config, evidence = args.app_root.resolve(), args.model_config.resolve(), args.evidence_dir.resolve()
    if not config.is_file():
        parser.error('Explicit model configuration file is missing.')
    if evidence.exists() and any(evidence.iterdir()):
        parser.error('Evidence directory must be new or empty.')
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'real_model_evaluated': False, 'synthetic_data_only': True,
              'app_root': str(app), 'prompt': PROMPT, 'default_budgets_used': True,
              'model_config_contents_logged': False, 'visual_validation': False,
              'os_disconnected_test': False, 'artifacts': []}
    host = None; turn = None; stage = 'boot'
    started = time.monotonic()
    # All test data and encrypted operational model state are removed after the
    # host has stopped. No plaintext model configuration copy is created.
    with tempfile.TemporaryDirectory(prefix='atlas-real-model-', ignore_cleanup_errors=True) as temporary:
        state = Path(temporary) / 'state'
        try:
            host = helper.Host(app, state, config, offline=False).start()
            stage = 'setup'
            models = host.json('GET', '/api/models')
            assert models.get('configured') is True
            model_id = models.get('default_model_id')
            report['model_id'] = model_id
            project = host.json('POST', '/api/projects',
                                {'name': '真实模型合成订单验收', 'request_id': helper.identity()})
            pid = project['id']; report['project_id'] = pid
            status, source = host.upload(pid)
            assert status == 200
            sid = source['id']; report['source_sha256'] = hashlib.sha256(helper.CSV).hexdigest()
            helper.wait(host, lambda: next((value for value in host.json('GET', '/api/sources')
                         if value['id'] == sid and value.get('status') == 'ready'), None), timeout=90)
            stage = 'real_model_task'
            turn = host.json('POST', f'/api/projects/{pid}/messages', {
                'text': PROMPT, 'interaction_mode': 'work', 'source_ids': [sid],
                'model_id': model_id, 'request_id': helper.identity()})
            tid = turn['id']; report['turn_id'] = tid
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                turn = host.json('GET', f'/api/projects/{pid}/turns/{tid}')
                if turn['state'] in ('done', 'failed'):
                    break
                time.sleep(2)
            report.update(state=turn.get('state'), outcome=turn.get('outcome'),
                          resume_available=turn.get('resume_available'),
                          answer=turn.get('answer'), model_usage=turn.get('model_usage', {}),
                          tool_trace=public_trace(turn), verification=turn.get('verification'))
            report['real_model_evaluated'] = turn.get('model_usage', {}).get('calls', 0) > 0
            budgets = turn.get('task_contract', {}).get('budgets', {})
            report['task_budgets'] = budgets
            assert budgets.get('model_calls') == 18 and budgets.get('output_tokens') == 24000
            stage = 'download_and_verify_office'
            report['artifacts'] = artifacts(host, pid, evidence, sid)
            # Only this synthetic project's authored specifications are kept.
            # This makes a failed renderer reproducible after private state is
            # removed, without preserving operational model/connector files.
            source_files=[]
            for number,file in enumerate(host.json('GET',f'/api/projects/{pid}/files')):
                path=file['path']
                if not path.lower().endswith('.json') or path.endswith('.manifest.json'):
                    continue
                code,content=host.call('GET',f'/api/projects/{pid}/file?'+urlencode({'path':path}))
                if code==200 and isinstance(content,bytes):
                    destination=f'workspace-spec-{number:02d}.json'
                    (evidence/destination).write_bytes(content)
                    source_files.append({'project_path':path,'file':destination,
                                         'sha256':hashlib.sha256(content).hexdigest()})
            report['synthetic_specifications']=source_files
            published = any(p.get('published_run') for p in host.json('GET', '/api/pipelines'))
            workflows = host.json('GET', f'/api/projects/{pid}/data-workflows')
            if isinstance(workflows, dict): workflows = workflows.get('items', [])
            published = published or any(value.get('published_run') for value in workflows)
            no_publish_tools = not any(step.get('tool') in ('publish_result', 'publish_data_workflow_result')
                                       for step in turn.get('agent_trace', []))
            report['no_automatic_publication'] = not published and no_publish_tools
            passed = {item['format'] for item in report['artifacts'] if item.get('passed')}
            report['formats_passed'] = sorted(passed)
            report['ok'] = (turn.get('state') == 'done' and turn.get('outcome') == 'verified_complete'
                            and passed == {'xlsx', 'docx', 'pptx'} and report['no_automatic_publication'])
        except BaseException as error:
            frames = traceback.extract_tb(error.__traceback__)
            report['failure'] = {'stage': stage, 'type': type(error).__name__,
                                 'file': Path(frames[-1].filename).name, 'line': frames[-1].lineno}
        finally:
            if host:
                try: host.stop()
                except Exception: report['shutdown_failed'] = True; report['ok'] = False
    report['duration_seconds'] = round(time.monotonic() - started, 2)
    (evidence / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Deliberately summarize rather than printing raw model/tool payloads.
    print(json.dumps({key: report.get(key) for key in ('ok', 'state', 'outcome', 'formats_passed',
          'model_usage', 'no_automatic_publication', 'failure', 'duration_seconds')}, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
