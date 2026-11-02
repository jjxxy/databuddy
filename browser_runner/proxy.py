"""Restricted HTTP/HTTPS egress; every new connection pins a validated IP.

Run on an internal Docker network shared only with the browser service. CONNECT
does not decrypt TLS. DNS rebinding cannot change the already selected address.
"""
import asyncio
import ipaddress
import json
import os
import socket
from urllib.parse import urlsplit


_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                          ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def private_networks(host, allowed):
    """Administrator-approved hostname and private network, never wildcarded.

    The proxy remains a separate enforcement boundary: a hostname resolving to
    both an approved and an unapproved address must be rejected entirely.
    """
    if os.getenv('NEXUS_BROWSER_PRIVATE_ENABLED') != '1':
        return ()
    approved = {x.strip().lower().rstrip('.') for x in
                os.getenv('NEXUS_BROWSER_PRIVATE_HOSTS', '').split(',') if x.strip()}
    if host not in approved:
        return ()
    if host not in allowed or host == '*':
        raise ValueError('Private destination requires an exact host approval')
    try:
        config = json.loads(os.getenv('NEXUS_BROWSER_PRIVATE_HOSTS_JSON', '{}'))
        if not isinstance(config, dict): raise ValueError()
        raw = config.get(host, [])
        if not isinstance(raw, list) or not raw or len(raw) > 32: raise ValueError()
        networks = tuple(ipaddress.ip_network(value, strict=True) for value in raw)
        if not all(any(net.version == base.version and net.subnet_of(base)
                       for base in _PRIVATE_NETWORKS) for net in networks):
            raise ValueError()
        return networks
    except (ValueError, TypeError):
        raise ValueError('Invalid private destination policy') from None


def destination(host, port, allowed):
    host = host.lower().rstrip('.')
    if port not in (80, 443) or ('*' not in allowed and host not in allowed):
        raise ValueError('Destination is not approved')
    private = private_networks(host, allowed)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        public = ip.is_global and not ip.is_multicast and not ip.is_reserved
        private_allowed = any(ip.version == net.version and ip in net for net in private)
        if (not (private_allowed if private else public) or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped):
            raise ValueError('Non-public destination')
    if not addresses: raise ValueError('DNS returned no address')
    return addresses[0][4][0]


async def relay(reader, writer):
    while data := await asyncio.wait_for(reader.read(65536), 60):
        writer.write(data)
        await writer.drain()


async def handle(reader, writer):
    upstream = None
    try:
        raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        if len(raw) > 16384: raise ValueError('Headers too large')
        lines = raw.decode('latin1').split('\r\n')
        method, target, version = lines[0].split(' ')
        if version not in ('HTTP/1.0', 'HTTP/1.1'): raise ValueError('Invalid protocol')
        allowed = {s.strip().lower().rstrip('.') for s in os.getenv('NEXUS_BROWSER_ALLOWED_HOSTS', '').split(',') if s.strip()}
        if method == 'CONNECT':
            parsed = urlsplit('https://' + target)
            if parsed.path or parsed.query or parsed.fragment: raise ValueError('Invalid CONNECT')
        else:
            parsed = urlsplit(target)
            if parsed.scheme != 'http' or method not in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                raise ValueError('Protocol denied')
        if not parsed.hostname or parsed.username or parsed.password: raise ValueError('Invalid destination')
        port = parsed.port or (443 if method == 'CONNECT' else 80)
        ip = await asyncio.to_thread(destination, parsed.hostname, port, allowed)
        remote, upstream = await asyncio.wait_for(asyncio.open_connection(ip, port), 10)
        if method == 'CONNECT':
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); await writer.drain()
        else:
            # One HTTP connection per request; discard proxy authentication.
            kept = [line for line in lines[1:] if line and not line.lower().startswith(
                ('proxy-', 'connection:', 'host:', 'keep-alive:', 'upgrade:'))]
            authority = parsed.hostname + (':' + str(port) if port != 80 else '')
            path = parsed.path or '/'
            if parsed.query: path += '?' + parsed.query
            upstream.write((f'{method} {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n'
                            + '\r\n'.join(kept) + '\r\n\r\n').encode('latin1'))
            await upstream.drain()
        tasks = [asyncio.create_task(relay(reader, upstream)), asyncio.create_task(relay(remote, writer))]
        try:
            await asyncio.wait(tasks, timeout=300, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        try: writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'); await writer.drain()
        except Exception: pass
    finally:
        if upstream: upstream.close()
        writer.close()


async def main():
    # Bounded simultaneous tunnels, bounded headers and connection lifetimes.
    active = asyncio.Semaphore(64)
    async def limited(reader, writer):
        if active.locked(): writer.close(); return
        async with active: await handle(reader, writer)
    server = await asyncio.start_server(limited, '0.0.0.0', 8092, limit=16384)
    async with server: await server.serve_forever()


if __name__ == '__main__': asyncio.run(main())
