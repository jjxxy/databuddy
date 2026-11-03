#!/bin/sh
# CPython may trust stale timestamp-based .pyc files after source replacement.
# Select a fresh cache namespace before importing the supervisor or its child
# processes. Do not delete user files, including root-owned __pycache__ files.
set -eu
nonce=$(cat /proc/sys/kernel/random/uuid)
case "$nonce" in
  ''|*[!0123456789abcdef-]*) echo 'Cannot initialize the Python cache namespace' >&2; exit 1 ;;
esac
export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/nexus-python-cache-$nonce"
export PYTHONDONTWRITEBYTECODE=1
exec "$@"
