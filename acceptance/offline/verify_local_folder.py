"""Verify native Windows folder mode with synthetic files and bundled runtimes.

Run with runtime/python/python.exe. No model calls or user credentials are used.
The host starts with an empty inherited PATH and application local-only policy;
this does not claim an OS firewall or disconnected-VM test.
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from urllib.parse import urlencode


def module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = module_from(Path(__file__).with_name('verify_backend.py'), 'folder_acceptance_backend')
ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acl(path):
    """Compare the selected business directory's original DACL byte for byte."""
    import ctypes
    from ctypes import wintypes
    security, kernel = ctypes.WinDLL('advapi32'), ctypes.WinDLL('kernel32')
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    descriptor, text = ctypes.c_void_p(), wintypes.LPWSTR()
    error = security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None,
                                           ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if not security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor,
                1, 4, ctypes.byref(text), None):
            raise ctypes.WinError()
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        kernel.LocalFree(descriptor)


def ready_sources(host, identifiers):
    values = {source['id']: source for source in host.json('GET', '/api/sources')}
    selected = [values.get(identifier) for identifier in identifiers]
    if any(source and source.get('status') == 'failed' for source in selected):
        raise AssertionError('A local snapshot failed deterministic profiling.')
    return selected if all(source and source.get('status') == 'ready' for source in selected) else None


def pipeline(host, source):
    spec = dict(source['proposed_spec'], primary_key='c_1', group_by='c_2', amount_field='m_net',
        computations=[{'name': 'm_net', 'label': '净金额', 'operation': 'subtract',
                       'left_field': 'c_3', 'right_field': 'c_4'}],
        filters=[{'field': 'c_5', 'operator': 'eq', 'data_type': 'text', 'value': 'paid'}])
    value = host.json('POST', '/api/pipelines', {'source_id': source['id'],
        'name': '本地文件夹离线加工', 'spec': spec, 'release_mode': 'manual'})
    identifier = value['id']
    def candidate():
        current = next(item for item in host.json('GET', '/api/pipelines') if item['id'] == identifier)
        runs = [run for run in host.json('GET', '/api/runs') if run.get('target') == identifier]
        if runs and all(run.get('status') == 'failed' for run in runs):
            for run in runs:
                host.lines.append(json.dumps(host.json('GET', '/api/runs/' + run['id']), ensure_ascii=False))
            raise AssertionError('The local-folder data pipeline failed.')
        return current if current.get('candidate_run') else None
    current = backend.wait(host, candidate)
    preview = host.json('GET', f'/api/pipelines/{identifier}/preview')
    assert preview['manifest']['records'] == 2
    host.json('POST', f'/api/pipelines/{identifier}/publish', {'run_id': current['candidate_run']})
    result = host.json('GET', f'/api/pipelines/{identifier}/result')
    assert result['total'] == 2
    assert Decimal(result['manifest']['summary'][0]['amount_total']) == Decimal('118.15')
    status, body = host.call('GET', f'/api/pipelines/{identifier}/export')
    assert status == 200
    label = next(field['label'] for field in result['manifest']['fields'] if field['name'] == 'm_net')
    assert_csv(body, label)
    return identifier, current['candidate_run'], label


def assert_csv(content, amount_label):
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    assert len(rows) == 2
    assert sum((Decimal(row[amount_label]) for row in rows), Decimal(0)) == Decimal('118.15')


def versioned_code(app, state, selected, project):
    """Use the public service methods, while stopped, without a model turn."""
    runtime = module_from(app / 'portable_runtime.py', 'folder_acceptance_runtime')
    identity = hashlib.sha256(os.path.normcase(str(selected)).encode('utf-8')).hexdigest()[:24]
    scoped = state / 'workspaces' / identity
    env = runtime.environment(scoped, 0, {}, None, True, selected)
    script = """import json,sys
sys.path.insert(0,sys.argv[1])
from agent_service import workspace
project=sys.argv[2]
name='reports/本地验收.md'
workspace.write_file(project,name,'收入118.15元，版本一\\n')
first=workspace.checkpoint(project,reason='离线文件夹验收版本一')
workspace.write_file(project,name,'收入118.15元，版本二\\n')
second=workspace.checkpoint(project,reason='离线文件夹验收版本二')
assert first['commit']!=second['commit']
assert workspace.git_content(workspace.folder(project),first['commit'],'files/'+name).decode('utf-8')=='收入118.15元，版本一\\n'
assert workspace.read_file(project,name)=='收入118.15元，版本二\\n'
print(json.dumps({'first':first['commit'],'second':second['commit'],'path':name}))
"""
    result = subprocess.run([str(app / 'runtime/python/python.exe'), '-c', script, str(app), project],
        cwd=app, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    if result.returncode:
        # All data is synthetic; no host access token is passed to this process.
        raise RuntimeError('Versioned local text failed: ' + result.stderr[-6000:])
    return json.loads(result.stdout.splitlines()[-1])


def verify_snapshot(selected, value, pipeline_id, run_id, label):
    folder = selected / value['relative_path']
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert value['complete'] and manifest['complete'] and not manifest['failures']
    assert manifest['originals_overwritten'] is False
    names = {record['path'] for record in manifest['files']}
    assert 'code/reports/本地验收.md' in names
    assert (folder / 'code/reports/本地验收.md').read_text(encoding='utf-8') == '收入118.15元，版本二\n'
    for record in manifest['files']:
        path = folder / record['path']
        assert path.stat().st_size == record['bytes'] and digest(path) == record['sha256']
    assert {path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file()} == names | {'manifest.json'}
    result = folder / f'applications/{pipeline_id}/{run_id}/result.csv'
    assert_csv(result.read_bytes(), label)
    return {path.relative_to(folder).as_posix(): digest(path) for path in folder.rglob('*') if path.is_file()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    app, evidence = args.app_root.resolve(), args.evidence_dir.resolve()
    if sys.platform != 'win32':
        parser.error('This acceptance harness targets native Windows only.')
    if evidence.exists() and any(evidence.iterdir()):
        parser.error('--evidence-dir must be new or empty')
    evidence.mkdir(parents=True, exist_ok=True)
    result = {'ok': False, 'platform': 'native Windows', 'synthetic_data_only': True,
        'real_ai_evaluated': False, 'os_firewall_or_disconnected_vm_evaluated': False, 'cases': {}}
    hosts, step = [], 'initialize'
    try:
        with backend.test_workspace(hosts) as temporary:
            root = Path(temporary)
            selected, state = root / '业务资料 中文 空格', root / 'private-state'
            selected.mkdir()
            orders, costs = selected / '收入订单.csv', selected / '成本.csv'
            orders.write_bytes(backend.CSV)
            costs.write_bytes(b'id,cost\n1,30.05\n')
            untouched = {path.name: digest(path) for path in (orders, costs)}
            original_acls = {str(path): acl(path) for path in (selected, orders, costs)}
            step = 'native_folder_boot_and_import'
            host = backend.Host(app, state, workspace=selected); hosts.append(host)
            host.start()
            status = host.json('GET', '/api/local-workspace')
            assert status['enabled'] and status['mode'] == 'local_folder' and status['imports_are_snapshots']
            assert status['network_mode'] == 'local_only'
            assert (selected / '.atlas/metadata.sqlite').is_file()
            assert not (state / 'data').exists()
            entries = host.json('GET', '/api/local-workspace/files')['entries']
            assert {entry['path'] for entry in entries} == {'收入订单.csv', '成本.csv'}
            assert host.call('GET', '/api/local-workspace', auth=False)[0] == 401
            project = host.json('POST', '/api/projects', {'name': '真实本地文件夹验收', 'request_id': backend.identity()})['id']
            request = {'paths': ['收入订单.csv', '成本.csv'], 'request_id': backend.identity()}
            imported = host.json('POST', f'/api/projects/{project}/local-import', request)
            assert len(imported['sources']) == 2 and not imported['failures'] and not imported['originals_overwritten']
            assert {source['content_hash'] for source in imported['sources']} == set(untouched.values())
            assert host.json('POST', f'/api/projects/{project}/local-import', request) == imported
            sources = backend.wait(host, lambda: ready_sources(host, [source['id'] for source in imported['sources']]))
            assert all(source['local_origin']['snapshot'] for source in sources)
            result['cases']['folder_import'] = {'files': 2, 'original_bytes_hashed': True,
                'same_request_idempotent': True, 'metadata_under_selected_folder': True}
            step = 'unsafe_and_missing_paths'
            for path in ('../outside.csv', '.atlas/metadata.sqlite', 'llm.json', '收入订单.csv:secret', 'NUL.csv', 'missing.csv'):
                rejected = host.json('POST', f'/api/projects/{project}/local-import',
                    {'paths': [path], 'request_id': backend.identity()})
                assert not rejected['sources'] and len(rejected['failures']) == 1
                assert 'Traceback' not in rejected['failures'][0]['error']
            result['cases']['unsafe_paths'] = {'traversal_private_config_ads_device_and_missing_rejected': True}
            step = 'offline_pipeline_exact_decimal'
            source = next(source for source in sources if source['local_origin']['path'] == '收入订单.csv')
            pipeline_id, run_id, label = pipeline(host, source)
            result['cases']['local_pipeline'] = {'rows': 2, 'exact_total': '118.15', 'manual_publish': True}
            assert {path.name: digest(path) for path in (orders, costs)} == untouched
            step = 'versioned_visible_text'
            host.stop()
            code = versioned_code(app, state, selected, project)
            host.start()
            current_status, current = host.call('GET', f'/api/projects/{project}/file?' + urlencode({'path': code['path']}))
            old_status, old = host.call('GET', f'/api/projects/{project}/file?' + urlencode({'path': code['path'], 'commit': code['first']}))
            assert current_status == old_status == 200
            assert current.decode('utf-8') == '收入118.15元，版本二\n' and old.decode('utf-8') == '收入118.15元，版本一\n'
            result['cases']['visible_versioned_code'] = {'two_distinct_checkpoints': True,
                'old_version_readable': True, 'text_under_selected_folder': True,
                'method': 'public service write/checkpoint methods while host stopped, then HTTP reads'}
            step = 'append_only_full_folder_export'
            first = host.json('POST', f'/api/projects/{project}/local-export')
            first_hashes = verify_snapshot(selected, first, pipeline_id, run_id, label)
            second = host.json('POST', f'/api/projects/{project}/local-export')
            assert first['relative_path'] != second['relative_path']
            verify_snapshot(selected, second, pipeline_id, run_id, label)
            assert verify_snapshot(selected, first, pipeline_id, run_id, label) == first_hashes
            result['cases']['exports'] = {'append_only_snapshots': 2, 'full_csv_rows': 2,
                'exact_total': '118.15', 'all_manifest_file_hashes_verified': True,
                'earlier_snapshot_unchanged': True, 'live_text_included': True}
            step = 'source_change_keeps_original_import_snapshot'
            orders.write_bytes(backend.CSV.replace(b'100.25', b'999.99'))
            assert host.json('POST', f'/api/projects/{project}/local-import', request) == imported
            newer = host.json('POST', f'/api/projects/{project}/local-import',
                {'paths': ['收入订单.csv'], 'request_id': backend.identity()})
            assert not newer['failures'] and newer['sources'][0]['id'] != source['id']
            backend.wait(host, lambda: ready_sources(host, [newer['sources'][0]['id']]))
            original_snapshots = list((selected / '.atlas/uploads').glob('*.csv'))
            assert any(digest(path) == untouched[orders.name] for path in original_snapshots)
            assert Decimal(host.json('GET', f'/api/pipelines/{pipeline_id}/result')['manifest']['summary'][0]['amount_total']) == Decimal('118.15')
            expected_originals = {orders.name: digest(orders), costs.name: untouched[costs.name]}
            result['cases']['snapshot_versions'] = {'user_modified_original_creates_new_source': True,
                'same_request_retains_old_snapshot': True, 'old_snapshot_bytes_preserved': True}
            step = 'restart_persistence_and_business_acl'
            first_url, first_token = host.url, host.token
            host.stop(); host.start()
            assert host.url == first_url and host.token == first_token
            assert Decimal(host.json('GET', f'/api/pipelines/{pipeline_id}/result')['manifest']['summary'][0]['amount_total']) == Decimal('118.15')
            assert verify_snapshot(selected, first, pipeline_id, run_id, label) == first_hashes
            assert {path.name: digest(path) for path in (orders, costs)} == expected_originals
            assert all(acl(Path(path)) == value for path, value in original_acls.items())
            assert host.json('GET', '/api/audit/verify')['ok']
            step = 'two_selected_folders_have_independent_state'
            other_selected = root / '另一份业务资料'
            other_selected.mkdir()
            (other_selected / '独立.csv').write_bytes(b'id,n\n1,7\n')
            other = backend.Host(app, state, workspace=other_selected); hosts.append(other)
            other.start()
            assert other.url != host.url and other.token != host.token
            assert other.json('GET', '/api/projects') == []
            assert (other_selected / '.atlas/metadata.sqlite').is_file()
            assert host.json('GET', f'/api/pipelines/{pipeline_id}/result')['total'] == 2
            assert not (state / 'data').exists()
            other.stop()
            result['cases']['folder_isolation'] = {'simultaneous_folders': 2,
                'distinct_access_tokens_and_ports': True, 'independent_databases': True,
                'first_workspace_stays_available': True}
            host.stop()
            result['cases']['restart'] = {'data_port_credentials_exports_preserved': True,
                'business_root_and_original_file_acls_unchanged': True, 'audit_verified': True}
            result.update(ok=True, inherited_path_empty=True, offline_policy='local_only',
                model_configuration_required=False, user_toolchain_required=False)
    except BaseException as error:
        frames = traceback.extract_tb(error.__traceback__)
        result['failure'] = {'step': step, 'type': type(error).__name__,
            'message': str(error), 'location': {'file': Path(frames[-1].filename).name,
            'line': frames[-1].lineno} if frames else None}
        for index, host in enumerate(hosts):
            try:
                (evidence / f'folder-diagnostic-{index}.log').write_text(host.diagnostic(), encoding='utf-8')
            except OSError:
                pass
    finally:
        for host in reversed(hosts):
            try: host.stop()
            except Exception: pass
    (evidence / 'local-folder-verification.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
