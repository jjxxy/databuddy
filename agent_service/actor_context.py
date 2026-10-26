"""Server-derived identity for personal extensions and preferences.

Projects retain the existing common-owner ACL. A shared access token identifies
one owner; signed OIDC identities can have distinct personal resources.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
import re

_actor=ContextVar('atlas_personal_actor',default='owner')


def current_actor():
    return _actor.get()


def actor_from_identity(identity):
    if not isinstance(identity,dict) or identity.get('authenticated') is not True:
        raise ValueError('缺少已验证的使用者身份')
    if identity.get('mode')=='token':return 'owner'
    if identity.get('mode')=='oidc' and isinstance(identity.get('subject'),str) and identity['subject']:
        issuer=identity.get('issuer') or os.getenv('OIDC_ISSUER','')
        return 'u_'+hashlib.sha256((issuer+'\x00'+identity['subject']).encode()).hexdigest()
    raise ValueError('无法确定个人资源的使用者身份')


@contextmanager
def actor_scope(actor_id):
    if not isinstance(actor_id,str) or not re.fullmatch(r'owner|u_[a-f0-9]{64}',actor_id):
        raise ValueError('使用者身份记录无效，未调用个人资源')
    token=_actor.set(actor_id)
    try:yield
    finally:_actor.reset(token)


use_actor=actor_scope
