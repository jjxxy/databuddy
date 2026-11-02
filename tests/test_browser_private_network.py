"""Private browser destinations need an exact host and an explicit CIDR."""
import socket
import pytest
from browser_runner.proxy import destination


def resolved(*addresses):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]


def test_private_network_is_default_denied_and_requires_all_policy_layers(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: resolved('10.4.3.2'))
    monkeypatch.delenv('NEXUS_BROWSER_PRIVATE_ENABLED', raising=False)
    with pytest.raises(ValueError): destination('erp.example', 443, {'erp.example'})
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_ENABLED', '1')
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_HOSTS', 'erp.example')
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_HOSTS_JSON', '{"erp.example":["10.4.0.0/16"]}')
    assert destination('erp.example', 443, {'erp.example'}) == '10.4.3.2'
    with pytest.raises(ValueError): destination('erp.example', 443, {'*'})
    with pytest.raises(ValueError): destination('other.example', 443, {'other.example'})
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_HOSTS_JSON', '{"erp.example":["0.0.0.0/0"]}')
    with pytest.raises(ValueError): destination('erp.example', 443, {'erp.example'})


def test_private_network_rejects_rebinding_and_protected_addresses(monkeypatch):
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_ENABLED', '1')
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_HOSTS', 'erp.example')
    monkeypatch.setenv('NEXUS_BROWSER_PRIVATE_HOSTS_JSON', '{"erp.example":["10.4.0.0/16"]}')
    for answers in (resolved('10.4.3.2', '10.5.0.1'), resolved('10.4.3.2','93.184.216.34'),
                    resolved('127.0.0.1'), resolved('169.254.169.254')):
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, answers=answers, **kw: answers)
        with pytest.raises(ValueError): destination('erp.example',443,{'erp.example'})


def test_public_destinations_continue_to_work(monkeypatch):
    monkeypatch.delenv('NEXUS_BROWSER_PRIVATE_ENABLED', raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: resolved('93.184.216.34'))
    assert destination('www.example',443,{'www.example'})=='93.184.216.34'
