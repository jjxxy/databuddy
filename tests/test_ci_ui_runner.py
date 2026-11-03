"""CI diagnostics must preserve the original suite and failure status."""
import os
import subprocess
import sys
from scripts import run_ci_ui


def test_success_runs_command_once_without_error_annotation(monkeypatch,capsys,tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    marker=tmp_path/'calls.txt'
    code='from pathlib import Path; p=Path(%r); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("passed")' % str(marker)
    assert run_ci_ui.run([sys.executable,'-c',code])==0
    assert marker.read_text()=='x'
    out=capsys.readouterr().out
    assert 'passed' in out and '::error' not in out


def test_failure_propagates_code_and_annotates_only_safe_location(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    source='import sys; print("AssertionError: secret-test-value"); print("  at /tmp/repo/tests/ui/example-test.cjs:17:3"); sys.exit(7)'
    assert run_ci_ui.run([sys.executable,'-c',source])==7
    output=capsys.readouterr().out
    assert 'AssertionError: secret-test-value' in output  # Original log is retained.
    annotation=next(line for line in output.splitlines() if line.startswith('::error'))
    assert annotation=='::error file=tests/ui/example-test.cjs,line=17::UI test failed; inspect the test job log for details.'
    assert 'secret-test-value' not in annotation


def test_signal_failure_is_not_reported_as_success(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    code='import os,signal; os.kill(os.getpid(), signal.SIGTERM)'
    assert run_ci_ui.run([sys.executable,'-c',code])==128+15
    assert '::error::UI test command failed' in capsys.readouterr().out
