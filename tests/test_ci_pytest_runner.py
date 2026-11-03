"""CI diagnostics must preserve the suite and not leak test values."""
import sys
from scripts import run_ci_pytest


def test_success_runs_original_command_once(monkeypatch,capsys,tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    marker=tmp_path/'calls.txt'
    code='from pathlib import Path; p=Path(%r); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("passed")' % str(marker)
    assert run_ci_pytest.run([sys.executable,'-c',code])==0
    assert marker.read_text()=='x'
    assert '::error' not in capsys.readouterr().out


def test_failure_annotations_exclude_parameters_and_tracebacks(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    code='import sys; print("AssertionError: secret-value"); print("FAILED tests/nested/test_safe.py::TestGroup::test_case[secret-value] - AssertionError: secret-value"); sys.exit(7)'
    assert run_ci_pytest.run([sys.executable,'-c',code])==7
    output=capsys.readouterr().out
    assert 'AssertionError: secret-value' in output
    annotations=[x for x in output.splitlines() if x.startswith('::error')]
    assert annotations==['::error file=tests/nested/test_safe.py::Backend test failed: TestGroup::test_case; inspect the test job log for details.']
    assert all('secret-value' not in line for line in annotations)


def test_invalid_fake_path_and_signal_do_not_report_success(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    code='import os,signal; print("FAILED tests/../../secret.py::test_x - secret",flush=True); os.kill(os.getpid(),signal.SIGTERM)'
    assert run_ci_pytest.run([sys.executable,'-c',code])==128+15
    assert '::error::Backend test command failed' in capsys.readouterr().out
