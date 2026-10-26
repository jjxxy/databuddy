"""Container health checks readiness of all three services, not just a TCP port."""
import json
import os
import socket
from urllib.request import Request, build_opener, ProxyHandler


def ready(value, instance=None):
    healthy = (isinstance(value, dict) and value.get('planner_mode') == 'llm'
            and value.get('worker', {}).get('healthy') is True
            and value.get('agent', {}).get('healthy') is True)
    if not healthy or instance is None:
        return healthy
    # Persistent metadata may contain fresh heartbeats from a prior container.
    # Never let those make a replacement container appear ready prematurely.
    return all(any(node.get('instance') == instance and node.get('healthy') is True
                   for node in value[role].get('nodes', [])) for role in ('worker', 'agent'))


def main():
    try:
        from . import opencode_runtime
        if opencode_runtime.is_enabled() and not opencode_runtime.ready():return 1
        port = int(os.getenv('PORT', '8000'))
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/ready', timeout=2) as response:
            if json.loads(response.read(4096)).get('ready') is not True:
                return 1
        request = Request(f'http://127.0.0.1:{port}/api/status',
                          headers={'Authorization': 'Bearer ' + os.environ['APP_TOKEN']})
        with opener.open(request, timeout=2) as response:
            value = json.loads(response.read(256 * 1024))
        instance = os.getenv('ATLAS_INSTANCE_ID') or os.getenv('RAILWAY_REPLICA_ID') or socket.gethostname()
        return 0 if ready(value, instance=instance) else 1
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
