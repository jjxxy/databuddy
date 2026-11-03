"""Run the complete UI suite once and expose a safe failure location in CI.

The normal npm output remains available in job logs. The public annotation
contains only a validated test file name and line number, never assertion
values, source text, tokens, or captured environment variables.
"""
import os
import re
import subprocess
import sys


_LOCATION = re.compile(r'(?:^|[/\\])tests[/\\]ui[/\\]([A-Za-z0-9_.-]+\.(?:cjs|mjs|js))(?::([1-9][0-9]*))?')


def run(command=None):
    command = command or ['npm', 'test', '--prefix', 'tests/ui']
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', bufsize=1)
    except OSError as error:
        print('Unable to start the UI test command: '+type(error).__name__, file=sys.stderr)
        return 1
    location = None
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        for match in _LOCATION.finditer(line[-8192:]):
            location = match.group(1), match.group(2)
    process.stdout.close()
    status = process.wait()
    if status != 0 and os.environ.get('GITHUB_ACTIONS') == 'true':
        if location:
            name, number = location
            where = 'file=tests/ui/'+name + (',line='+number if number else '')
            print('::error '+where+'::UI test failed; inspect the test job log for details.')
        else:
            print('::error::UI test command failed; inspect the test job log for details.')
    return status if status >= 0 else 128-status


if __name__ == '__main__':
    sys.exit(run())
