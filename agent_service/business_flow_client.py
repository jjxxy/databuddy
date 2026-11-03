"""Submit an explicitly versioned, published business flow to an Atlas service.

The caller supplies a stable request ID so an uncertain HTTP response can be
retried without inadvertently executing a financial operation twice.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_DIGEST = re.compile(r'^[a-f0-9]{64}$')
_MAX_INPUT_BYTES = 2 * 1024 * 1024


class FlowCallError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value):
    if not isinstance(value, str):
        raise FlowCallError('服务地址无效')
    try:
        u = urlsplit(value)
        port = u.port
        hostname = u.hostname
    except ValueError:
        raise FlowCallError('服务地址无效') from None
    if (u.scheme not in ('http', 'https') or not hostname or u.username is not None or
            u.password is not None or u.path not in ('', '/') or u.query or u.fragment or
            any(ord(c) < 32 for c in value)):
        raise FlowCallError('服务地址必须是明确的服务源地址')
    if u.scheme == 'http':
        try:
            local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = hostname.lower() == 'localhost'
        if not local:
            raise FlowCallError('远程服务必须使用 HTTPS')
    return u.scheme + '://' + u.netloc


def submit(base_url, token, project_id, flow_id, expected_digest, request_id, inputs, *, timeout=30):
    """Enqueue a fixed published version; result is a run receipt, not final output."""
    base = _origin(base_url)
    if not isinstance(token, str) or not token or len(token)>8192 or any(ord(c)<33 or ord(c)>126 for c in token):
        raise FlowCallError('服务访问凭证无效')
    if any(not isinstance(x, str) or not _IDENTIFIER.fullmatch(x)
           for x in (project_id, flow_id, request_id)):
        raise FlowCallError('项目、流程或请求标识无效')
    if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
        raise FlowCallError('流程版本摘要无效')
    if not isinstance(inputs, dict):
        raise FlowCallError('流程参数必须是 JSON 对象')
    try:
        body = json.dumps({'request_id': request_id, 'expected_digest': expected_digest,
                           'inputs': inputs, 'use_published': True}, ensure_ascii=False,
                          allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise FlowCallError('流程参数不是有效 JSON') from None
    if len(body) > _MAX_INPUT_BYTES:
        raise FlowCallError('流程参数过大，请使用受控文件引用')
    path = f'/api/projects/{project_id}/business-flows/{flow_id}/runs'
    request = urllib.request.Request(base + path, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise FlowCallError(f'服务未接受请求（HTTP {error.code}），请核对版本、权限和参数') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FlowCallError('未确认服务是否收到请求；重试时请使用相同请求标识和参数') from None
    if len(raw) > 1024 * 1024:
        raise FlowCallError('服务返回的运行回执过大')
    try:
        receipt = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise FlowCallError('服务返回的运行回执无效') from None
    if (not isinstance(receipt, dict) or not isinstance(receipt.get('run_id'), str) or
            receipt.get('plan_digest') != expected_digest):
        raise FlowCallError('服务返回的运行回执与请求版本不一致')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description='调用已采用的固定版本业务流程')
    parser.add_argument('--project', required=True)
    parser.add_argument('--flow', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--request-id', required=True, help='重试同一请求时必须保持不变')
    parser.add_argument('--inputs', required=True, help='包含参数 JSON 对象的 UTF-8 文件')
    args = parser.parse_args(argv)
    try:
        with open(args.inputs, 'rb') as file:
            raw = file.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise FlowCallError('流程参数文件过大')
        inputs = json.loads(raw)
        result = submit(os.getenv('NEXUS_URL', ''), os.getenv('NEXUS_TOKEN', ''),
                        args.project, args.flow, args.digest, args.request_id, inputs)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (FlowCallError, OSError, ValueError, UnicodeError) as error:
        print(str(error) if isinstance(error, FlowCallError) else '无法读取有效的参数文件', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
