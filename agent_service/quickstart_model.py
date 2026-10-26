"""Read a bounded owner-supplied model connection without importing runtime state."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from urllib.parse import urlsplit, urlunsplit

MAX_CONFIGURATION_BYTES = 64 * 1024
_FIELDS = {'engine', 'base_url', 'model', 'api_key', 'share_data', 'tool_protocol', 'upstream_base_url'}
_METADATA_HOSTS = {'169.254.169.254', 'metadata.google.internal', 'metadata.azure.internal'}


def _decode(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:raise ValueError('模型配置不能包含重复字段')
            result[key] = value
        return result

    def invalid_constant(value):raise ValueError('模型配置不能包含非有限数值')

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError):
        raise ValueError('模型配置必须是有效且无重复字段的UTF-8 JSON对象') from None
    if not isinstance(value, dict):raise ValueError('模型配置必须是JSON对象')
    return value


def _hostname(parsed):
    host = (parsed.hostname or '').lower()
    if not host or host.rstrip('.') in _METADATA_HOSTS:
        raise ValueError('模型地址不能使用云元数据或空主机地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        address = getattr(address, 'ipv4_mapped', None) or address
        if address.is_link_local:
            raise ValueError('模型地址不能使用链路本地地址')
    return host


def normalize_base_url(value, *, container=False):
    if not isinstance(value, str) or not 8 <= len(value.strip()) <= 500:
        raise ValueError('模型base_url必须为8至500字符的HTTP(S)地址')
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
        host = _hostname(parsed)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('模型地址须为有效且不含凭证、查询参数、片段或链路本地地址的HTTP(S)地址') from None
    path = parsed.path.rstrip('/')
    suffix = '/chat/completions'
    if path.endswith(suffix):path = path[:-len(suffix)].rstrip('/')
    netloc = parsed.netloc
    if container and host in ('localhost', '127.0.0.1', '::1'):
        netloc = 'host.docker.internal' + (':' + str(port) if port is not None else '')
    return urlunsplit((parsed.scheme, netloc, path, '', ''))


def allow_model_host(base_url):
    """An explicitly supplied endpoint grants its host, never metadata access."""
    host = _hostname(urlsplit(base_url))
    allowed = [entry.strip() for entry in os.getenv('API_ALLOWED_HOSTS', '').split(',') if entry.strip()]
    if host not in {entry.lower() for entry in allowed}:allowed.append(host)
    os.environ['API_ALLOWED_HOSTS'] = ','.join(allowed)


def validate_connection(value, *, container=False):
    """Validate one owner connection without importing application state."""
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError('模型配置包含不支持的字段')
    for name in ('base_url', 'model', 'api_key'):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError('模型配置必须填写base_url、model和api_key三个字符串')
    for name in ('engine', 'tool_protocol', 'upstream_base_url'):
        if name in value and not isinstance(value[name], str):raise ValueError('模型配置字段类型不正确')
    if 'share_data' in value and type(value['share_data']) is not bool:
        raise ValueError('模型share_data必须为JSON布尔值')
    result = {'engine': 'atlas', 'share_data': False, 'tool_protocol': 'json', 'upstream_base_url': '', **value}
    if result['engine'] not in ('atlas', 'opencode') or result['tool_protocol'] not in ('json', 'native'):
        raise ValueError('模型引擎或工具协议无效')
    if not 1 <= len(result['model']) <= 120 or len(result['upstream_base_url']) > 500:
        raise ValueError('模型名称或上游地址超过长度限制')
    result['base_url'] = normalize_base_url(result['base_url'], container=container)
    return result


def validate_configuration(value, *, container=False):
    if 'models' not in value:
        return validate_connection(value, container=container)
    if set(value) - {'models', 'default_model_id'}:
        raise ValueError('模型目录包含不支持的字段')
    entries = value['models']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
        raise ValueError('模型目录须提供1至20个模型')
    models = []; identifiers = set()
    for entry in entries:
        if not isinstance(entry, dict):raise ValueError('模型目录条目须为对象')
        identifier = entry.get('id'); name = entry.get('name')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', identifier) or identifier in identifiers:
            raise ValueError('模型id须唯一并只含字母、数字、下划线或连字符')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError('模型展示名称须为1至80个字符')
        connection = validate_connection({k:v for k,v in entry.items() if k not in ('id','name')}, container=container)
        models.append({'id':identifier,'name':name.strip(),**connection});identifiers.add(identifier)
    default = value.get('default_model_id', models[0]['id'])
    if not isinstance(default, str) or default not in identifiers:
        raise ValueError('默认模型必须存在于模型目录')
    return {'default_model_id':default,'models':models}


def load_configuration(path, *, container=False):
    """Read only a regular <=64KB file; errors never include paths or contents."""
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIGURATION_BYTES:
            raise ValueError('模型配置必须是体积不超过64KB的普通文件')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
        if len(raw) > MAX_CONFIGURATION_BYTES:raise ValueError('模型配置超过64KB限制')
    except (OSError, TypeError):
        raise ValueError('无法读取模型配置，请检查文件路径与读取权限') from None
    finally:
        if descriptor is not None:os.close(descriptor)
    return validate_configuration(_decode(raw), container=container)
