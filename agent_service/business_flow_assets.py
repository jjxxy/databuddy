"""Inspectable, version-bound project files for the business-flow executor.

The node JSON is precisely the language interpreted by BusinessFlow. Handler
files are snapshots of server-owned Python functions, never executable uploads.
Input/output contracts reference protected project data without copying it.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from pathlib import Path

from fastapi import HTTPException

from . import workspace


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


# Only these server-owned modules or named functions can enter project code.
# Paths in user-authored plans are never interpreted as Python source paths.
_MODULES = {
    'runtime/tables/business_flow_data.py': '表格读取、快照与节点执行',
    'runtime/tables/data_workflows.py': '表格语法、计算与 SQL 执行器',
    'runtime/inputs/workbook_reader.py': '工作簿读取与表头解析',
    'runtime/inputs/read_input.py': '固定输入绑定与读取函数',
    'runtime/inputs/api_rows.py': '接口分页采集函数',
    'runtime/inputs/business_flow_api_capture.py': '接口来源绑定与采集快照',
    'runtime/checks/business_flow_quality_gate.py': '数据检查规则与执行',
    'runtime/checks/business_flow_checkpoint.py': '人工检查回执与恢复',
    'runtime/analysis/workflow_model_skill.py': '模型及技能调用适配',
    'runtime/materials/business_deliverables.py': '报告内容与生成步骤',
    'runtime/materials/office.py': '办公文件编制与校验',
    'runtime/materials/office_presentation.py': '幻灯片排版与校验（PPT 分支）',
}
_INPUT = 'runtime/inputs/'
_TABLES = 'runtime/tables/'
_MATERIALS = 'runtime/materials/'
_NODE_MODULES = {
    'source_table': [_INPUT + 'read_input.py', _INPUT + 'workbook_reader.py', _TABLES + 'business_flow_data.py'],
    'api_capture': [_INPUT + 'api_rows.py', _INPUT + 'business_flow_api_capture.py', _TABLES + 'business_flow_data.py'],
    'table_workflow': [_TABLES + 'data_workflows.py', _TABLES + 'business_flow_data.py'],
    'quality_gate': ['runtime/checks/business_flow_quality_gate.py', _TABLES + 'business_flow_data.py'],
    'human_review': ['runtime/checks/business_flow_checkpoint.py'],
    'model': ['runtime/analysis/workflow_model_skill.py'],
    'skill': ['runtime/analysis/workflow_model_skill.py'],
    'html_report': [_MATERIALS + 'business_deliverables.py'],
    'table_report_spec': [_MATERIALS + 'business_deliverables.py', _TABLES + 'data_workflows.py'],
    'office_document': [_MATERIALS + 'office.py', _MATERIALS + 'office_presentation.py', _MATERIALS + 'business_deliverables.py'],
}


def handler_sources(kinds, handlers):
    from . import business_flow_engine, workflow_model_skill
    sources = {}
    for kind in sorted(kinds):
        # Approval is implemented by the engine; the registered sentinel is
        # deliberately never called. Model and skill wrappers share _run.
        fn = business_flow_engine.run if kind == 'human_review' else handlers[kind].run
        functions = [fn, workflow_model_skill._run] if kind in ('model', 'skill') else [fn]
        sources['handlers/' + kind + '.py'] = '\n'.join(
            f'# {func.__module__}.{func.__name__}\n' + inspect.getsource(func)
            for func in functions)
    from .connectors import api_rows
    from .project_data import read_input
    slices = {_INPUT + 'read_input.py': read_input, _INPUT + 'api_rows.py': api_rows}
    for path in sorted({path for kind in kinds for path in _NODE_MODULES.get(kind, [])}):
        if path in slices:
            fn = slices[path]
            sources[path] = f'# {fn.__module__}.{fn.__name__}\n' + inspect.getsource(fn)
        else:
            sources[path] = Path(__file__).with_name(Path(path).name).read_text(encoding='utf-8')
    return sources


def handler_lock(sources):
    return {'version': 1, 'sources': {path: workspace.flow_blob(text) for path, text in sources.items()}}


_CATEGORIES = {
    'source_table': ('input', '取得资料'), 'api_capture': ('input', '取得资料'),
    'table_workflow': ('process', '清洗与计算'), 'quality_gate': ('checks', '核对数据'),
    'human_review': ('checks', '核对数据'), 'model': ('analysis', '分析与说明'),
    'skill': ('analysis', '分析与说明'), 'table_report_spec': ('deliver', '编制成果'),
    'html_report': ('deliver', '编制成果'), 'office_document': ('deliver', '编制成果'),
}


def overview_groups(nodes, phase_by_kind):
    """Collapse a topologically ordered plan into at most six business steps."""
    # Merge only consecutive blocks of an already topologically sorted graph.
    # Grouping all nodes of a kind globally could turn A -> B -> A into a cycle.
    blocks = []
    for node in nodes:
        category = phase_by_kind.get(node['kind'], (node['kind'], node['title']))
        if not blocks or blocks[-1][-1][0] != category:
            blocks.append([])
        blocks[-1].append((category, node))
    while len(blocks) > 6:
        i = min(range(len(blocks) - 1), key=lambda j: len(blocks[j]) + len(blocks[j + 1]))
        blocks[i:i + 2] = [blocks[i] + blocks[i + 1]]
    groups, owner = [], {}
    for block in blocks:
        gid = 'group_' + str(len(groups))
        category, first = block[0]
        label = category[1]
        if len(block) == 1:
            label = first['title']
        elif any(item[0] != category for item in block):
            label = first['title'] + '至' + block[-1][1]['title']
        ids, deps = [], []
        for _, node in block:
            ids.append(node['id'])
            for upstream in node['depends_on']:
                source_group = owner[upstream]
                if source_group != gid and source_group not in deps:
                    deps.append(source_group)
            owner[node['id']] = gid
        groups.append({'id': gid, 'label': label, 'nodeIds': ids, 'deps': deps})
    return groups


def _safe_id(value):
    return value[:8] + '_' + hashlib.sha256(value.encode()).hexdigest()[:16]


def _definitions(document, *, stored):
    nodes, files, labels, io_nodes = [], {}, {}, []
    detail_nodes, detail_output_ids = [], {}
    for node in document['nodes']:
        nid, kind = node['id'], node['kind']
        # The hash distinguishes case-only names on Windows. A fixed prefix
        # also keeps user-chosen ids such as CON and AUX safe on a portable disk.
        directory = 'nodes/n_' + _safe_id(nid)
        name = directory + '/rule.json'
        row = {'id': nid, 'title': node['title'], 'kind': kind, 'path': name, 'depends_on': node['depends_on']}
        if stored:
            row['handler_path'] = 'handlers/' + kind + '.py'
            labels[row['handler_path']] = kind + ' · 处理函数'
            row['paths'] = list(_NODE_MODULES.get(kind, []))
            labels.update({path: _MODULES[path] for path in row['paths']})
        if kind == 'table_workflow':
            plan = node['config']['plan']
            # Aliases are local to this table plan. Only declared upstream
            # node bindings create graph edges; runtime parameters are values.
            upstream = {alias: ref['name'] for alias, ref in node['inputs'].items()
                        if alias in plan['inputs'] and ref['source'] == 'node'}
            for stage in plan['stages']:
                # Keep relative stage order in paths; each rule is the exact
                # fragment consumed by the registered table adapter.
                base = directory + '/stages/s_' + _safe_id(stage['id'])
                title = stage.get('description') or stage['id']
                path = base + '.json'
                files[path] = _json(stage)
                labels[path] = title + ' · 加工规则'
                row.setdefault('paths', []).append(path)
                stage_id = nid + ':' + stage['id']
                aliases = stage.get('inputs') or [stage.get('input'), stage.get('right')]
                detail = {'id': stage_id, 'owner_id': nid, 'stage_id': stage['id'],
                          'kind': stage['kind'], 'title': title, 'path': path,
                          'depends_on': list(dict.fromkeys(upstream[k] for k in aliases if k in upstream)),
                          'paths': []}
                if stage['kind'] == 'sql':
                    path = base + '.sql'
                    files[path] = stage['sql']
                    labels[path] = title + ' · SQL'
                    row['paths'].append(path)
                    detail['paths'].append(path)
                if stored:
                    detail['paths'].extend(_NODE_MODULES[kind])
                detail_nodes.append(detail)
                upstream[stage['id']] = stage_id
            detail_output_ids[nid] = upstream[plan['output']]
        nodes.append(row)
        files[name] = _json(node)
        labels[name] = node['title'] + ' · 执行规则'
        io = {'node_id': nid, 'inputs': node['inputs'], 'declared_output': nid in document['outputs']}
        if kind in ('source_table', 'api_capture'):
            io['source_binding'] = {k: node['config'][k] for k in
                ('source_id', 'revision', 'content_hash', 'config_sha256') if k in node['config']}
        io_nodes.append(io)
    groups = overview_groups(nodes, _CATEGORIES)
    files['overview.json'] = _json({'version': 1, 'name': document['name'], 'groups': groups})
    files['dag.json'] = _json({'version': 1, 'name': document['name'], 'nodes': nodes, 'outputs': document['outputs']})
    files['io.json'] = _json({'inputs': document['inputs'], 'parameters': document.get('input_specs') or {},
                              'nodes': io_nodes, 'outputs': document['outputs']})
    labels = {'overview.json': '业务概览 DAG', 'dag.json': '详细执行 DAG', 'io.json': '输入与输出引用', **labels}
    # Child file associations are a projection of the verified per-stage
    # rules. Do not change the generated bytes of older locked Git trees.
    return {'nodes': nodes, 'groups': groups, 'detail_nodes': detail_nodes,
            'detail_output_ids': detail_output_ids,
            'files': [{'path': path, 'language': {'.json': 'json', '.py': 'python', '.sql': 'sql'}[Path(path).suffix], 'label': label}
                      for path, label in labels.items()]}, files


def version_files(document, sources):
    _, files = _definitions(document, stored=True)
    return {**files, **sources}


def _read_index(pid, fid, commit, document, lock):
    """One Git tree read verifies every materialized file against the plan.

    This also rejects stale files from renamed/removed nodes and symlinks,
    without a Git subprocess per node in the execution path.
    """
    stored = 'code_assets' in lock
    index, files = _definitions(document, stored=stored)
    prefix = 'files/business-flows/' + fid + '/'
    if not stored:
        workspace.verify_flow_tree(workspace.folder(pid), prefix, commit, None)
        return index, files, 'derived'
    code_lock = lock['code_assets']
    source_paths = {item['path'] for item in index['files'] if item['language'] == 'python'}
    if (not isinstance(code_lock, dict) or code_lock.get('version') != 1
            or not isinstance(code_lock.get('sources'), dict) or code_lock['sources'].keys() != source_paths):
        raise ValueError('流程节点代码版本锁无效')
    expected = {name: workspace.flow_blob(value) for name, value in files.items()}
    expected.update(code_lock['sources'])
    workspace.verify_flow_tree(workspace.folder(pid), prefix, commit, expected)
    return index, files, 'git'


def load_version(pid, fid, commit, document):
    from .business_flows import _lock_path
    try:
        locked = json.loads(workspace.git_content(workspace.folder(pid), commit, 'files/' + _lock_path(fid)))
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise ValueError('流程版本锁不可读取') from None
    if not isinstance(locked, dict):
        raise ValueError('流程版本锁无效')
    return _read_index(pid, fid, commit, document, locked)


def _content(pid, fid, commit, path, files):
    # Callers first verify the immutable Git tree and allowlist the exact path.
    if path in files:
        return files[path]
    return workspace.git_content(workspace.folder(pid), commit, 'files/business-flows/' + fid + '/' + path).decode('utf-8')


def export_files(pid, fid, commit, document):
    index, files, _ = load_version(pid, fid, commit, document)
    return {f['path']: _content(pid, fid, commit, f['path'], files) for f in index['files']}


def view(pid, fid, actor, expected_digest, commit, path=None):
    from .business_flows import _owned, _read_commit
    record = _owned(pid, fid, actor)
    if not commit or not expected_digest or not any(
            (expected_digest, commit) == (record.get(prefix + '_digest'), record.get(prefix + '_commit'))
            for prefix in ('candidate', 'published')):
        raise HTTPException(409, '流程版本已变化，请重新打开')
    _, (index, files, storage) = _read_commit(pid, fid, commit, expected_digest, include_assets=True)
    identity = {'flow_id': fid, 'digest': expected_digest, 'commit': commit, 'storage': storage}
    if path is None:
        return {**identity, **index}
    item = next((f for f in index['files'] if f['path'] == path), None)
    if item is None:
        raise HTTPException(404, '选定版本中没有该流程文件')
    return {**identity, **item, 'content': _content(pid, fid, commit, path, files)}
