"""The Linux entrypoint must not load stale bytecode after a code update."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / 'deploy' / 'python-cache-entrypoint.sh'


@pytest.mark.skipif(not Path('/proc/sys/kernel/random/uuid').is_file(),
                    reason='the quickstart image runs Linux containers')
def test_container_entrypoint_ignores_old_pyc_and_allows_explicit_compilation(tmp_path):
    source = tmp_path / 'fixture_module.py'
    source.write_text('value="old"\n', encoding='utf-8')
    env = os.environ.copy()
    env.pop('PYTHONPYCACHEPREFIX', None)
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    env['TMPDIR'] = str(tmp_path)
    command = [sys.executable, '-c', 'import fixture_module; print(fixture_module.value)']
    subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    pyc = next((tmp_path / '__pycache__').glob('fixture_module.*.pyc'))
    old_pyc = pyc.read_bytes()
    stamp = source.stat().st_mtime_ns
    # Same timestamp and size is an actual CPython timestamp-pyc cache hit.
    source.write_text('value="new"\n', encoding='utf-8')
    os.utime(source, ns=(stamp, stamp))
    result = subprocess.run(command, cwd=tmp_path, env={**env, 'PYTHONDONTWRITEBYTECODE':'1'},
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == 'old'  # dontwrite alone does not prevent reads

    def launch(code):
        return subprocess.run(['/bin/sh', str(ENTRYPOINT), sys.executable, '-c', code],
                              cwd=tmp_path, env=env, check=True, capture_output=True, text=True)

    first = launch('import fixture_module,sys;print(fixture_module.value);print(sys.pycache_prefix)')
    value, prefix = first.stdout.splitlines()
    assert value == 'new'
    assert Path(prefix).parent == tmp_path
    assert not Path(prefix).exists()  # regular imports do not write bytecode
    assert pyc.read_bytes() == old_pyc  # even a root-owned old cache need not be deleted

    # Explicit py_compile can still create its output, e.g. for syntax checks.
    compiled = launch('import py_compile,sys;print(py_compile.compile("fixture_module.py",doraise=True));print(sys.pycache_prefix)')
    compiled_path, second_prefix = compiled.stdout.splitlines()
    assert Path(compiled_path).is_file()
    assert prefix != second_prefix
    assert Path(compiled_path).is_relative_to(Path(second_prefix))


def test_quickstart_uses_cache_entrypoint_and_keeps_default_supervisor():
    dockerfile = (ROOT / 'Dockerfile.quickstart').read_text(encoding='utf-8')
    assert 'ENTRYPOINT ["/bin/sh", "/app/deploy/python-cache-entrypoint.sh"]' in dockerfile
    assert 'CMD ["python", "-m", "agent_service.cloud"]' in dockerfile


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() != 0 or
                    not Path('/proc/sys/kernel/random/uuid').is_file(),
                    reason='requires Linux root to verify the UID switch')
def test_new_cache_namespace_stays_writable_after_dropping_root():
    import pwd

    try:
        nobody = pwd.getpwnam('nobody')
    except KeyError:
        pytest.skip('nobody account is unavailable')
    def mapped(path, identity):
        return any(start <= identity < start + count for start, _, count in
                   (map(int, line.split()) for line in Path(path).read_text().splitlines()))
    if not mapped('/proc/self/uid_map', nobody.pw_uid) or not mapped('/proc/self/gid_map', nobody.pw_gid):
        pytest.skip('container user namespace does not map the nobody identity')
    with tempfile.TemporaryDirectory(prefix='atlas-cache-permissions-') as folder:
        base = Path(folder)
        base.chmod(0o777)
        (base / 'fixture_module.py').write_text('value=42\n', encoding='utf-8')
        env = {**os.environ, 'TMPDIR': folder}
        code = ('import os,py_compile;'
                f'os.setgid({nobody.pw_gid});os.setuid({nobody.pw_uid});'
                'print(py_compile.compile("fixture_module.py",doraise=True))')
        result = subprocess.run(['/bin/sh', str(ENTRYPOINT), sys.executable, '-c', code],
                                cwd=folder, env=env, check=True, capture_output=True, text=True)
        compiled = Path(result.stdout.strip())
        assert compiled.is_file()
        assert compiled.stat().st_uid == nobody.pw_uid
