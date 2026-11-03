"""Authenticated local-subject evidence for an untrusted Agent CSV handoff.

Byte equality says that the authenticated subject supplied the same bytes; it does not
validate the remote Agent's identity, ancestry or the truth of the data. The
caller must require a separate informed business review of the business schema.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from .actor_context import current_actor
from .connectors import verified_file_source
from .project_data import owned, source_requires_fact_alignment
from .store import get, unseal


class AnchorError(ValueError):
    pass


def _identity(source):
    """Capture frozen server-side source metadata without disclosing secrets."""
    return {key:source.get(key) for key in (
        'id','project_id','kind','name','status','archived','revision','revision_id',
        'content_hash','bytes','secret_config','profile','origin_source_id',
        'origin_source_revision','fact_review_id','visual_fact_review_id',
        'origin_agent_handoff','external_provenance','semantic_alignment_required',
        'browser_download_id','local_origin')}


def _version(source):
    revision=source.get('revision');rid=source.get('revision_id')
    if type(revision) is not int or revision<1 or not isinstance(rid,str) or not rid:
        raise AnchorError('资料缺少可验证的固定版本')
    try:record=get('source_version',rid)
    except KeyError:raise AnchorError('资料历史版本不可用') from None
    if (record.get('source_id')!=source['id'] or record.get('project_id')!=source['project_id']
            or record.get('revision')!=revision or record.get('content_hash')!=source.get('content_hash')
            or record.get('secret_config')!=source.get('secret_config')):
        raise AnchorError('资料与固定版本不一致')


def _csv(source):
    if (source.get('kind')!='file' or source.get('status')!='ready' or source.get('archived')
            or not isinstance(source.get('name'),str) or not source['name'].lower().endswith('.csv')
            or not isinstance(source.get('content_hash'),str)
            or not re.fullmatch(r'[a-f0-9]{64}',source['content_hash'])):
        raise AnchorError('仅支持已就绪且版本完整的 CSV 文件')
    profile=source.get('profile')
    evidence=profile.get('evidence') if isinstance(profile,dict) else None
    if not isinstance(evidence,dict) or evidence.get('parser')!='csv':
        raise AnchorError('资料尚无可验证的 CSV 解析记录')
    _version(source)


def check_anchor(project_id, external_source_id, anchor_source_id):
    """Validate the exact currently authorized pair and return a frozen binding.

    This is a precondition only; callers must separately authenticate the user,
    collect explicit per-field semantic decisions and preserve consent records.
    Authentication does not prove a natural person or an independent origin.
    """
    if not all(isinstance(v,str) and v for v in (project_id,external_source_id,anchor_source_id)):
        raise AnchorError('项目或资料标识无效')
    if external_source_id==anchor_source_id:raise AnchorError('需另选认证主体提交的文件')
    try:
        external=owned(project_id,'source',external_source_id)
        anchor=owned(project_id,'source',anchor_source_id)
    except (KeyError,ValueError):raise AnchorError('资料不存在或不属于当前项目') from None
    _csv(external);_csv(anchor)
    if (not isinstance(external.get('origin_agent_handoff'),str)
            or not re.fullmatch(r'[a-f0-9]{64}',external['origin_agent_handoff'])
            or not isinstance(external.get('external_provenance'),dict)):
        raise AnchorError('资料不是完整 Agent 交接来源')
    try:
        external_cfg=unseal(external['secret_config']);anchor_cfg=unseal(anchor['secret_config'])
    except Exception:raise AnchorError('资料来源记录不可验证') from None
    handoff=external_cfg.get('agent_handoff') if isinstance(external_cfg,dict) else None
    if (not isinstance(handoff,dict) or handoff.get('manifest_sha256')!=external['origin_agent_handoff']
            or handoff.get('source_version')!=external['external_provenance']
            or external['external_provenance'].get('content_sha256')!=external['content_hash']):
        raise AnchorError('Agent 交接与来源回执不一致')
    # A digest or a peer's own claims never establish authority. Require the
    # server-recorded grant from the authenticated gateway and check it live.
    grant=handoff.get('gateway_grant')
    if not isinstance(grant,dict) or set(grant)!={'client_id','revision'}:
        raise AnchorError('交接缺少已授权调用方的固定记录，请重新交接')
    try:
        from .agent_gateway import _fresh
        client=_fresh(grant['client_id'])
    except (KeyError,ValueError,TypeError):
        raise AnchorError('交接调用方已停用或不可用') from None
    if (client.get('revision')!=grant['revision'] or client.get('owner_id')!=current_actor()
            or project_id not in client.get('project_ids',[])
            or handoff.get('scope')!='gateway:'+client['id']):
        raise AnchorError('交接调用方授权或当前用户已变化')
    forbidden=('origin_source_id','origin_source_revision','fact_review_id','visual_fact_review_id',
               'origin_agent_handoff','external_provenance','browser_download_id','local_origin')
    if (any(key in anchor for key in forbidden) or anchor.get('semantic_alignment_required') is True
            or source_requires_fact_alignment(anchor,{})):
        raise AnchorError('提交的文件不能是 Agent 交接、网页下载或派生资料')
    proof=anchor_cfg.get('user_upload_proof') if isinstance(anchor_cfg,dict) else None
    expected={'version':1,'project_id':project_id,'source_id':anchor['id'],
              'actor_id':current_actor(),'content_sha256':anchor['content_hash']}
    if (not isinstance(proof,dict) or set(proof)!={*expected,'request_id'}
            or any(proof.get(k)!=v for k,v in expected.items())
            or not isinstance(proof.get('request_id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',proof['request_id'])):
        raise AnchorError('请由当前认证主体重新上传 CSV 文件')
    if external['content_hash']!=anchor['content_hash']:
        raise AnchorError('提交文件与交接文件内容不一致')
    before_external=_identity(external);before_anchor=_identity(anchor)
    try:
        _,external_bytes,_=verified_file_source(external)
        _,anchor_bytes,_=verified_file_source(anchor)
    except (ValueError,OSError) as exc:
        raise AnchorError('提交文件或交接资料内容无法核验') from exc
    if not hmac.compare_digest(external_bytes,anchor_bytes):
        raise AnchorError('提交文件与交接文件字节不一致')
    try:
        after_external=owned(project_id,'source',external_source_id)
        after_anchor=owned(project_id,'source',anchor_source_id)
        after_client=_fresh(grant['client_id'])
    except (KeyError,ValueError,TypeError):
        raise AnchorError('资料或授权在核验期间发生变化，请重新核对') from None
    if (_identity(after_external)!=before_external or _identity(after_anchor)!=before_anchor
            or after_client.get('revision')!=grant['revision']
            or after_client.get('owner_id')!=current_actor()
            or project_id not in after_client.get('project_ids',[])):
        raise AnchorError('资料或授权在核验期间发生变化，请重新核对')
    _version(after_external);_version(after_anchor)
    return {'version':1,'project_id':project_id,'actor_id':current_actor(),
            'gateway_client_id':client['id'],'gateway_client_revision':client['revision'],
            'external_source_id':external['id'],'external_revision':external['revision'],
            'external_revision_id':external['revision_id'],'external_content_sha256':external['content_hash'],
            'external_manifest_sha256':external['origin_agent_handoff'],
            'external_config_sha256':hashlib.sha256(external['secret_config'].encode()).hexdigest(),
            'anchor_source_id':anchor['id'],'anchor_revision':anchor['revision'],
            'anchor_revision_id':anchor['revision_id'],'anchor_content_sha256':anchor['content_hash'],
            'anchor_config_sha256':hashlib.sha256(anchor['secret_config'].encode()).hexdigest(),
            'proof_scope':'recipient_authenticated_subject_uploaded_identical_bytes',
            'remote_lineage_verified':False,'financial_verified':False}
