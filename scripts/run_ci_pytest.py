"""Run the full backend suite and annotate safe failed test identifiers in CI.

Pytest output and its exit status are preserved. Only a strictly validated
repository test path and Python identifier are copied into public annotations;
parameter values, traceback text, assertions and environment data are excluded.
"""
import os
import re
import subprocess
import sys

_FAILURE = re.compile(r'^FAILED (tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py)::((?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*)(?:\[|\s|$)')
_MAX_FAILURES = 20


def run(command=None):
    command = command or [sys.executable, '-m', 'pytest', 'tests', '-q']
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', bufsize=1)
    except OSError as error:
        print('Unable to start the backend test command: '+type(error).__name__, file=sys.stderr)
        return 1
    failures = []
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = _FAILURE.match(line[:8192])
        if match:
            path, name = match.groups()
            if len(path) <= 240 and len(name) <= 240 and (path, name) not in failures and len(failures) < _MAX_FAILURES:
                failures.append((path, name))
    process.stdout.close()
    status = process.wait()
    if status != 0 and os.environ.get('GITHUB_ACTIONS') == 'true':
        if failures:
            for path, name in failures:
                print('::error file='+path+'::Backend test failed: '+name+'; inspect the test job log for details.')
        else:
            print('::error::Backend test command failed; inspect the test job log for details.')
    return status if status >= 0 else 128-status


if __name__ == '__main__':
    sys.exit(run())
