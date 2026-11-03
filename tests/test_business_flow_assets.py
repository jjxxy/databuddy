"""The human-facing project tree is an exact, versioned view of executable rules."""
import inspect
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_service import business_flow_assets as assets, business_flows as flows, workspace
from agent_service.api import app
from agent_service.business_flow_data import source_table
from agent_service.codegen import git
from agent_service.config import TOKEN
from agent_service.store import now, patch, put, uid
from test_project_data import project, source


def saved_flow():
    pid = project()
    src = source(pid)
    tid = 't_' + uid()
    put('turn', {'project_id': pid, 'actor_id': 'owner', 'state': 'done',
                 'text': '建立流程', 'interaction_mode': 'work', 'created_at': now()}, tid)
    pin = {'source_id': src['id'], 'revision': src['revision'], 'content_hash': src['content_hash']}
    # Windows reserved names and case-only pairs are distinct node IDs.
    doc = {'name': '核对原始资料', 'nodes': [
        {'id': nid, 'kind': 'source_table', 'title': '读取' + nid, 'config': pin}
        for nid in ('CON', 'con')], 'outputs': ['con']}
    return pid, tid, doc, flows.create(pid, 'owner', tid, doc, uid())


def test_version_files_in_git_and_authorized_ui_routes():
    pid, tid, doc, first = saved_flow()
    fid = first['id']
    commit, sha = first['candidate_commit'], first['candidate_digest']
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + TOKEN}
    base = f'/api/projects/{pid}/business-flows/{fid}/code-asset'
    params = {'expected_digest': sha, 'commit': commit}
    result = client.get(base + 's', headers=headers, params=params)
    assert result.status_code == 200, result.text
    index = result.json()
    assert (index['flow_id'], index['digest'], index['commit'], index['storage']) == (fid, sha, commit, 'git')
    paths = [node['path'] for node in index['nodes']]
    assert len(set(p.casefold() for p in paths)) == 2
    assert index['groups'] == [{'id': 'group_0', 'label': '取得资料', 'nodeIds': ['CON', 'con'], 'deps': []}]

    def read(path):
        r = client.get(base, headers=headers, params={**params, 'path': path})
        assert r.status_code == 200, r.text
        return r.json()['content']

    assert json.loads(read('overview.json'))['groups'] == index['groups']
    assert json.loads(read('dag.json'))['nodes'] == index['nodes']
    assert json.loads(read('io.json'))['nodes'][0]['source_binding'] == doc['nodes'][0]['config']
    assert json.loads(read(paths[0])) == first['candidate_document']['nodes'][0]
    handler = index['nodes'][0]['handler_path']
    assert inspect.getsource(source_table) in read(handler)
    assert client.get(base, headers=headers, params={**params, 'path': '../project-state.json'}).status_code == 404
    assert client.get(base + 's', headers=headers, params={**params, 'commit': '0' * 40}).status_code == 409
    with pytest.raises(ValueError):
        assets.view(pid, fid, 'other-user', sha, commit)
    with pytest.raises(ValueError):
        assets.view(project(), fid, 'owner', sha, commit)

    patch('business_flow', fid, published_digest=sha, published_commit=commit, published_turn_id=tid)
    changed = {**doc, 'nodes': doc['nodes'][1:]}
    new = flows.update(pid, fid, 'owner', tid, changed, uid(), sha)
    assert assets.view(pid, fid, 'owner', sha, commit, paths[0])['storage'] == 'git'
    with pytest.raises(HTTPException) as exc:
        assets.view(pid, fid, 'owner', new['candidate_digest'], new['candidate_commit'], paths[0])
    assert exc.value.status_code == 404
    assert not workspace.path_for(pid, 'business-flows/' + fid + '/' + paths[0]).exists()


def test_vintage_commit_has_rules_but_never_misattributes_python_source():
    pid, _, _, saved = saved_flow()
    fid = saved['id']
    repo = workspace.folder(pid)
    lock_path = workspace.path_for(pid, flows._lock_path(fid))
    lock = json.loads(lock_path.read_bytes())
    del lock['code_assets']
    lock_path.write_text(json.dumps(lock), encoding='utf-8')
    git(repo, 'rm', '-r', '--', 'files/business-flows/' + fid)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-m', 'historical unified format')
    commit = git(repo, 'rev-parse', 'HEAD')
    patch('business_flow', fid, candidate_commit=commit)
    view = assets.view(pid, fid, 'owner', saved['candidate_digest'], commit)
    assert view['storage'] == 'derived'
    assert all('handler_path' not in node for node in view['nodes'])
    assert all(f['language'] == 'json' for f in view['files'])


def test_divergent_node_rules_block_inspection_export_and_execution():
    pid, _, _, saved = saved_flow()
    fid, sha, prior = saved['id'], saved['candidate_digest'], saved['candidate_commit']
    path = assets.view(pid, fid, 'owner', sha, prior)['nodes'][0]['path']
    workspace.path_for(pid, 'business-flows/' + fid + '/' + path).write_text('{}', encoding='utf-8')
    repo = workspace.folder(pid)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-m', 'simulate externally modified checkout')
    changed = git(repo, 'rev-parse', 'HEAD')
    patch('business_flow', fid, candidate_commit=changed)
    with pytest.raises(ValueError, match='固定版本'):
        assets.view(pid, fid, 'owner', sha, changed)
    with pytest.raises(ValueError, match='固定版本'):
        assets.export_files(pid, fid, changed, saved['candidate_document'])
    with pytest.raises(ValueError, match='固定版本'):
        flows.submit_run(pid, fid, 'owner', uid(), sha, {})

def test_large_alternating_overview_keeps_real_edges_and_all_nodes_exactly_once():
    nodes = [{'id': 'n' + str(i), 'kind': 'source_table' if i % 2 == 0 else 'table_workflow',
              'title': '业务步骤' + str(i), 'depends_on': ['n' + str(i - 1)] if i % 2 else []}
             for i in range(32)]
    groups = assets.overview_groups(nodes, {'source_table': ('in', '取数'), 'table_workflow': ('process', '加工')})
    assert len(groups) <= 6
    assert [nid for g in groups for nid in g['nodeIds']] == [n['id'] for n in nodes]
    owners = {nid: g['id'] for g in groups for nid in g['nodeIds']}
    expected = {(owners[d], owners[n['id']]) for n in nodes for d in n['depends_on'] if owners[d] != owners[n['id']]}
    assert {(d, g['id']) for g in groups for d in g['deps']} == expected


def test_multifile_nodes_save_nested_sql_and_presentation_renderer(monkeypatch):
    from agent_service import office, office_presentation
    from test_project_data import row_schema
    pid, tid, first_doc, initial = saved_flow()
    fid = initial['id']
    sql = 'select id, office, amount, note from selected order by id'
    plan = {'name': '销售明细', 'inputs': {'orders': row_schema()}, 'stages': [
        {'id': 'selected', 'kind': 'select', 'description': '选取结账所需字段', 'input': 'orders',
         'columns': ['id', 'office', 'amount', 'note']},
        {'id': 'ordered', 'kind': 'sql', 'description': '按业务编号排序', 'inputs': ['selected'],
         'sql': sql, 'output_schema': row_schema()},
        {'id': 'parallel', 'kind': 'sql', 'description': '另查原始明细', 'inputs': ['orders'],
         'sql': 'select * from orders', 'output_schema': row_schema()}], 'output': 'ordered'}
    doc = {'name': '编制汇报', 'nodes': [first_doc['nodes'][0],
           {'id': 'calculate', 'kind': 'table_workflow', 'title': '整理销售明细', 'depends_on': ['CON'],
            'inputs': {'orders': {'source': 'node', 'name': 'CON'}}, 'config': {'plan': plan}},
           {'id': 'content', 'kind': 'table_report_spec', 'title': '准备汇报内容', 'depends_on': ['calculate'],
            'inputs': {'table': {'source': 'node', 'name': 'calculate'}}, 'config': {'title': '销售汇报', 'format': 'presentation'}},
           {'id': 'slides', 'kind': 'office_document', 'title': '排版并生成汇报', 'depends_on': ['content'],
            'inputs': {'spec': {'source': 'node', 'name': 'content'}}, 'config': {'name': '销售汇报', 'renderer_version': office.RENDERER}}],
           'outputs': ['slides']}
    saved = flows.update(pid, fid, 'owner', tid, doc, uid(), initial['candidate_digest'])
    args = (pid, fid, 'owner', saved['candidate_digest'], saved['candidate_commit'])
    def no_local_snapshot(*args, **kwargs):
        raise AssertionError('opening a historical file consulted the current implementation')
    monkeypatch.setattr(assets, 'handler_sources', no_local_snapshot)
    index = assets.view(*args)
    by_id = {n['id']: n for n in index['nodes']}
    ordered, parallel = index['detail_nodes'][1:]
    assert index['detail_output_ids'] == {'calculate': 'calculate:ordered'}
    assert [(s['id'], s['depends_on']) for s in index['detail_nodes']] == [
        ('calculate:selected', ['CON']), ('calculate:ordered', ['calculate:selected']),
        ('calculate:parallel', ['CON'])]
    assert ordered['owner_id'] == 'calculate' and ordered['stage_id'] == 'ordered'
    sql_path = next(p for p in ordered['paths'] if p.endswith('.sql'))
    stage_path = ordered['path']
    assert '/stages/' in sql_path and stage_path in by_id['calculate']['paths']
    assert sql_path not in parallel['paths'] and stage_path not in parallel['paths']
    assert 'runtime/tables/data_workflows.py' in ordered['paths']
    assert assets.view(*args, sql_path)['content'] == sql
    assert json.loads(assets.view(*args, stage_path)['content']) == plan['stages'][1]
    # The exact same mappings can be recovered for a pre-source-snapshot
    # revision, but no shared Python files may be attributed to it.
    vintage, _ = assets._definitions(saved['candidate_document'], stored=False)
    assert vintage['detail_output_ids'] == index['detail_output_ids']
    assert vintage['detail_nodes'][1]['paths'] == [sql_path]
    assert 'runtime/materials/office_presentation.py' in by_id['slides']['paths']
    assert 'runtime/materials/office_presentation.py' not in by_id['calculate']['paths']
    assert assets.view(*args, 'runtime/materials/office_presentation.py')['content'] == inspect.getsource(office_presentation)
    shared = 'runtime/tables/business_flow_data.py'
    assert shared in by_id['CON']['paths'] and shared in by_id['calculate']['paths']
    assert sum(f['path'] == shared for f in index['files']) == 1
    with pytest.raises(HTTPException):
        assets.view(*args, 'runtime/inputs/provider.py')
