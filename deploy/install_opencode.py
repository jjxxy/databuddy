"""Build-time installer for the verified OpenCode 2.1.2 Linux release.

Checks the official npm tarball SHA512 before extracting only its executable.
No installer shell script, floating latest tag, or runtime package installation.
"""
import base64
import hashlib
import io
from pathlib import Path
import platform
import shutil
import tarfile
import urllib.request

VERSION = '2.1.2'
INTEGRITY = {
    'x86_64': ('x64', 'O8/8JT8SNmMReDmpYDsm04KmkrQQ2dzaKgaw06zCoUfh50ARGHs58E6zCh16GIUyEL8abrdSe268K5/ExE89Gw=='),
    'aarch64': ('arm64', '294IA9S1sXOAzOBRPMcnFuYpXlFeUaKTlpTk0a7TMvuc7cJbfS52poTw3AlIu7ycbn8TGmTmj27OOxsMmBCLEg=='),
}


def install(destination=Path('/usr/local/bin/opencode')):
    try:arch, integrity = INTEGRITY[platform.machine()]
    except KeyError:raise ValueError('OpenCode image supports Linux amd64 and arm64') from None
    url = f'https://registry.npmjs.org/@opencode/cli-linux-{arch}/-/cli-linux-{arch}-{VERSION}.tgz'
    with urllib.request.urlopen(url, timeout=120) as source:
        data = source.read(160_000_001)
    if len(data) > 160_000_000 or not hashlib.sha512(data).digest() == base64.b64decode(integrity):
        raise ValueError('OpenCode release integrity check failed')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember('package/bin/opencode')
        if not member.isfile() or member.size > 300_000_000:raise ValueError('Unexpected OpenCode executable')
        with archive.extractfile(member) as source, destination.open('wb') as target:
            shutil.copyfileobj(source, target)
    destination.chmod(0o755)


if __name__ == '__main__':install()
