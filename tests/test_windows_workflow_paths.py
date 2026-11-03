"""Exercise a managed Git project whose node files exceed Win32 MAX_PATH."""
import os
import pytest
from agent_service import workspace
from agent_service.platform_files import native_path
from agent_service.store import put, uid


@pytest.mark.skipif(os.name != 'nt', reason='Windows long paths and per-repository Git policy')
def test_nested_project_code_roundtrips_on_existing_windows_repository(tmp_path, monkeypatch):
    monkeypatch.delenv('NEXUS_LOCAL_WORKSPACE', raising=False)
    state = tmp_path / ('state-' + 's' * 35)
    native_path(state).mkdir()
    monkeypatch.setattr(workspace, 'DATA', state)
    pid = 'p_' + uid()
    put('project', {'name': '长路径项目'}, pid)
    repo = workspace._init(pid)
    workspace.git(repo, 'config', '--local', 'core.longpaths', 'false')  # older existing checkout
    prefix = 'data-flows/wf_' + 'a' * 40
    name = 'nodes/' + 'nested_' * 7 + 'summary/implementation/' + 'aggregate_' * 4 + 'rules.py'
    full = prefix + '/' + name
    assert len(str(state / 'workspaces' / pid / 'files' / full)) > 260
    with workspace.lock(pid):
        workspace.write_flow_assets(pid, prefix, {name:'answer = 118.15\n'})
    workspace.write_file(pid, '说明/金额口径.md', '按当期金额')
    first = workspace.checkpoint(pid)
    assert workspace.git(repo, 'config', '--local', '--bool', 'core.longpaths') == 'true'
    assert full in {row['path'] for row in workspace.list_files(pid)}
    assert workspace.git_content(repo, first['commit'], 'files/' + full) == b'answer = 118.15\n'
    with workspace.lock(pid):
        workspace.write_flow_assets(pid, prefix, {name:'answer = 0\n'})
    workspace.write_file(pid, '说明/金额口径.md', '改口径')
    second = workspace.checkpoint(pid)
    restored = workspace.restore(pid, first['commit'], 'restore-long-path')
    assert restored['commit'] not in {first['commit'], second['commit']}
    assert workspace.read_file(pid, full) == 'answer = 118.15\n'
    assert workspace.read_file(pid, '说明/金额口径.md') == '按当期金额'

@pytest.mark.skipif(os.name != 'nt', reason='Windows namespace semantics')
def test_native_path_accepts_unc_share_and_rejects_devices():
    assert str(native_path(r'\\fileserver\reports\monthly')) == r'\\?\UNC\fileserver\reports\monthly'
    assert native_path(native_path(r'C:\projects\finance')) == native_path(r'C:\projects\finance')
    for name in (r'\\.\PIPE\untrusted', r'\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1'):
        with pytest.raises(OSError):
            native_path(name)
