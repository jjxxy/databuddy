"""Immutable image entrypoint for one command, inside the gVisor sandbox."""
import base64
import json
import os
import signal
import subprocess
import sys
import threading


def execute(command, timeout, *, workspace='/workspace', logs='/tmp'):
    process = subprocess.Popen(['/bin/bash', '-c', command], cwd=workspace,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)

    def capture(stream, path):
        remaining = 64000
        with open(path, 'wb', buffering=0) as output:
            while chunk := stream.read(4096):
                if remaining:
                    output.write(chunk[:remaining])
                    remaining = max(0, remaining - len(chunk))

    threads = [threading.Thread(target=capture, args=(stream, path), daemon=True)
               for stream, path in ((process.stdout, os.path.join(logs, 'atlas-stdout')), (process.stderr, os.path.join(logs, 'atlas-stderr')))]
    for thread in threads:
        thread.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        code = 124
    # A command must not leave detached/background children running while its
    # files are checkpointed. The runner separately stops namespace survivors.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for thread in threads:
        thread.join(timeout=1)
    return code if 0 <= code <= 255 else 128 + abs(code)


def main():
    return execute(base64.b64decode(sys.argv[1]).decode('utf-8'), int(sys.argv[2]))


if __name__ == '__main__':
    raise SystemExit(main())
