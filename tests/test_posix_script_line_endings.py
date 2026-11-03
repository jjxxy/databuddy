"""Scripts executed by POSIX interpreters must not contain Windows CRLF."""
from pathlib import Path
import subprocess


def test_git_tracked_shell_scripts_have_lf_endings():
    root=Path(__file__).resolve().parents[1]
    names=subprocess.run(['git','ls-files','*.sh'],cwd=root,check=True,text=True,capture_output=True).stdout.splitlines()
    assert names
    bad=[name for name in names if b'\r' in (root/name).read_bytes()]
    assert bad==[],f'POSIX scripts contain CR characters: {bad}'
