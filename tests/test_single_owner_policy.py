"""Strict identity mode prevents a personal workspace becoming shared."""
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from agent_service import identity
from agent_service.isolated_deployment import ManifestError, render


@pytest.fixture
def identity_config(monkeypatch):
    for name,value in {'NEXUS_AUTH_MODE':'oidc','NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT':'1',
        'OIDC_ISSUER':'https://identity.example.test','OIDC_AUDIENCE':'nexus',
        'OIDC_JWKS_URL':'https://identity.example.test/keys','OIDC_ALLOWED_SUBJECTS':'alice',
        'OIDC_ACCEPT_PROXY_HEADER':'0'}.items():monkeypatch.setenv(name,value)


def test_single_owner_checks_signature_and_exact_subject(identity_config,monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk['kid']='key1'
    monkeypatch.setattr(identity,'keys',lambda refresh=False:[jwk])
    def request(subject):
        token=jwt.encode({'iss':'https://identity.example.test','aud':'nexus','sub':subject,
                          'exp':int(time.time())+60},key,algorithm='RS256',headers={'kid':'key1'})
        return SimpleNamespace(headers={'Authorization':'Bearer '+token})
    assert identity.principal(request('alice'))['subject']=='alice'
    with pytest.raises(HTTPException) as exc:identity.principal(request('bob'))
    assert exc.value.status_code==403
    # Even if the backend's subject allowlist is accidentally widened after
    # provisioning, a request is denied before JWT verification/routing.
    monkeypatch.setenv('OIDC_ALLOWED_SUBJECTS','alice,bob')
    with pytest.raises(ValueError,match='只能配置一个'):identity.principal(request('bob'))


def test_strict_mode_rejects_token_auth_and_proxy_headers(identity_config,monkeypatch):
    monkeypatch.setenv('NEXUS_AUTH_MODE','token')
    with pytest.raises(ValueError,match='OIDC'):identity.validate_config()
    monkeypatch.setenv('NEXUS_AUTH_MODE','oidc')
    monkeypatch.setenv('OIDC_ACCEPT_PROXY_HEADER','1')
    with pytest.raises(ValueError,match='直接验证'):identity.validate_config()
    monkeypatch.setenv('NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT','yes')
    with pytest.raises(ValueError,match='策略配置'):identity.validate_config()


def test_personal_manifest_exactly_one_subject_and_default_remains_shared():
    manifest=json.loads((Path(__file__).parents[1]/'deploy/isolated/manifest.example.json').read_text())
    personal=copy.deepcopy(manifest);personal['workspaces'][0]['sharing']='personal'
    compose,_=render(personal)
    service=compose['services']['nexus-'+personal['workspaces'][0]['id']]
    assert service['environment']['NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT']=='1'
    personal['workspaces'][0]['subjects'].append('another-subject')
    with pytest.raises(ManifestError,match='exactly one'):render(personal)
    # Existing explicit shared workspace configurations retain their semantics.
    shared=copy.deepcopy(manifest);shared['workspaces'][0]['subjects'].append('trusted-collaborator')
    compose,_=render(shared)
    service=compose['services']['nexus-'+shared['workspaces'][0]['id']]
    assert 'NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT' not in service['environment']
