"""Optional application destination policy; not a third-party process firewall."""
import ipaddress
import os
import socket
from urllib.parse import urlsplit


def mode():
    value=os.getenv('ATLAS_NETWORK_MODE','connected')
    if value not in ('connected','local_only'):raise ValueError('网络模式无效')
    return value


def check_host(host,port=None,*,addresses=None):
    if mode()!='local_only':return
    if not isinstance(host,str) or not host:raise ValueError('仅本地连接模式需要明确的主机地址')
    normalized=host.lower().rstrip('.')
    allowed={entry.strip().lower().rstrip('.') for entry in os.getenv('ATLAS_LOCAL_ONLY_HOSTS','').split(',') if entry.strip()}
    try:literal=ipaddress.ip_address(normalized)
    except ValueError:literal=None
    if normalized!='localhost' and not (literal and literal.is_loopback) and normalized not in allowed:
        raise ValueError('仅本地连接模式禁止访问此地址；请使用本地服务或由管理员明确允许私网主机')
    try:
        ips=addresses if addresses is not None else [item[4][0] for item in socket.getaddrinfo(host,port or 80,type=socket.SOCK_STREAM)]
        parsed=[ipaddress.ip_address(item) for item in ips]
    except (OSError,ValueError):raise ValueError('仅本地连接目标无法安全解析') from None
    if not parsed or any(item.is_global or item.is_link_local or item.is_multicast or item.is_unspecified for item in parsed):
        raise ValueError('仅本地连接模式拒绝公网、链路本地或未知目标')
    if normalized=='localhost' and any(not item.is_loopback for item in parsed):raise ValueError('本地主机解析结果无效')


def check_url(url):
    if mode()!='local_only':return
    parsed=urlsplit(str(url))
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:raise ValueError('仅本地连接地址无效')
    check_host(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80))


def status():
    return {'mode':mode(),'application_connections_restricted':mode()=='local_only','os_network_isolation':False}
