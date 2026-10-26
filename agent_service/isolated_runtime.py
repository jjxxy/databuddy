"""Non-root isolated instance entrypoint; no cross-workspace state or secrets."""
import os
from pathlib import Path


def main():
    if os.geteuid() != 10001 or os.getenv('DATA_DIR') != '/data' or os.getenv('HOME') != '/data/home':
        raise ValueError('Isolated runtime requires UID 10001 and dedicated /data home')
    if os.getenv('ATLAS_AUTH_MODE') != 'oidc' or os.getenv('OIDC_ACCEPT_PROXY_HEADER') != '0':
        raise ValueError('Isolated runtime requires direct OIDC verification')
    from .identity import validate_config
    validate_config()
    Path('/data/home').mkdir(mode=0o700, exist_ok=True)
    from .cloud import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
