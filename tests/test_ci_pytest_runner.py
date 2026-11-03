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


def test_only_whitelisted_exception_name_is_exposed_without_values(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    lines=['________ test_public[private-parameter] ________',
           'E   sqlalchemy.exc.OperationalError: private database address',
           'FAILED tests/test_public.py::test_public[private-parameter] - OperationalError: private database address']
    code='import sys; [print(x) for x in %r]; sys.exit(1)' % lines
    assert run_ci_pytest.run([sys.executable,'-c',code])==1
    annotations=[line for line in capsys.readouterr().out.splitlines() if line.startswith('::error')]
    assert annotations==['::error file=tests/test_public.py::Backend test failed: test_public [OperationalError]; inspect the test job log for details.']
    assert all('private' not in line for line in annotations)


def test_ambiguous_or_unrecognized_exception_names_are_never_annotated(monkeypatch,capsys):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    lines=['________ test_same[private-id] ________','E   ValueError: first secret',
           '________ test_same[other-private-id] ________','E   OperationalError: second secret',
           '________ test_unknown ________','E   SecretType: private value',
           'FAILED tests/test_safe.py::test_same[private-id] - ValueError: secret',
           'FAILED tests/test_safe.py::test_unknown - SecretType: private']
    code='import sys; [print(x) for x in %r]; sys.exit(1)' % lines
    assert run_ci_pytest.run([sys.executable,'-c',code])==1
    annotations=[line for line in capsys.readouterr().out.splitlines() if line.startswith('::error')]
    assert annotations==[
        '::error file=tests/test_safe.py::Backend test failed: test_same; inspect the test job log for details.',
        '::error file=tests/test_safe.py::Backend test failed: test_unknown; inspect the test job log for details.']
    assert all('private' not in line and 'secret' not in line.lower() for line in annotations)
