"""Operator-managed, encrypted model catalog with immutable per-turn selection.

Business requests choose an opaque catalog ID. They never supply a provider,
credential, endpoint or execution engine. Revisions retain the exact connection
selected when a turn was submitted; replacing the catalog cannot silently
redirect an in-flight task to a different model.
"""
import hashlib
import json
from pydantic import Field, model_validator
from .models import StrictModel
from .provider import ProviderInput
from .store import get, put, seal, unseal, now, uid
from .locks import FileLock
from .config import DATA


class CatalogEntry(ProviderInput):
    id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
    name: str = Field(min_length=1, max_length=80)


class CatalogInput(StrictModel):
    default_model_id: str = Field(min_length=1, max_length=64)
    models: list[CatalogEntry] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def identities(self):
        ids = [entry.id for entry in self.models]
        if len(set(ids)) != len(ids) or self.default_model_id not in ids:
            raise ValueError('模型ID必须唯一，默认模型必须在列表中')
        if any(not entry.name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in entry.name) for entry in self.models):
            raise ValueError('模型显示名称不能为空')
        if any(entry.engine != 'atlas' for entry in self.models):
            raise ValueError('模型目录需填写实际模型服务地址，不接受旧版Agent传输配置')
        return self


def _active():
    return get('system', 'model-catalog')


def _revision(revision):
    return unseal(get('model_catalog', revision)['secret_config'])


def owner_configuration():
    """Trusted startup/runtime use only; never return through a business API."""
    try:
        pointer = _active()
    except KeyError:
        from .provider import legacy_settings
        value = legacy_settings()
        if not value.get('base_url') or not value.get('model'):
            return {'models': [], 'default_model_id': None, 'revision': None}
        return {'models': [dict(value, id='default', name='默认模型')],
                'default_model_id': 'default', 'revision': None}
    try:return dict(_revision(pointer['revision']), revision=pointer['revision'])
    except KeyError:raise ValueError('后台模型目录版本不可用，请联系管理员恢复配置') from None


def public_catalog():
    import os
    if os.getenv('ATLAS_DESKTOP_MODEL_DISABLED')=='1':
        return {'configured':False,'default_model_id':None,'models':[]}
    raw = owner_configuration()
    return {'configured': bool(raw['models']), 'default_model_id': raw['default_model_id'],
            'models': [{'id': entry['id'], 'name': entry['name']} for entry in raw['models']]}


def provision(value, *, verify=True):
    """Publish owner input only after all connections validate successfully.

    Call from trusted startup provisioning. There is deliberately no business
    HTTP endpoint accepting this schema. ``verify=False`` is for controlled
    tests/offline migration, never selected by a client request.
    """
    from .provider import validate_configuration
    try:
        normalized = value if 'models' in value else {
            'default_model_id': 'default',
            'models': [dict(value, id='default', name='默认模型')]}
        parsed = CatalogInput.model_validate(normalized, strict=True)
    except Exception:
        raise ValueError('后台模型目录格式不正确，请检查唯一ID、名称和连接字段') from None
    canonical = parsed.model_dump()
    for record, entry in zip(canonical['models'], parsed.models):
        record['api_key'] = entry.api_key.get_secret_value()
        record['name'] = entry.name.strip()
        if entry.engine == 'atlas':record['tool_protocol'] = 'native'
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        if _active().get('configuration_sha256') == digest:
            return public_catalog()
    except KeyError:
        pass
    entries = []
    for entry in parsed.models:
        provider_input = ProviderInput.model_validate(entry.model_dump(exclude={'id', 'name'}))
        # OpenCode's native agent loop requires real function/tool calling.
        # A successful plain-text connectivity probe is insufficient.
        if provider_input.engine == 'atlas':provider_input.tool_protocol = 'native'
        if verify:
            raw = validate_configuration(provider_input, old={})
        else:
            raw = provider_input.model_dump()
            raw['api_key'] = provider_input.api_key.get_secret_value()
        entries.append(dict(raw, id=entry.id, name=entry.name.strip()))
    configuration = {'default_model_id': parsed.default_model_id, 'models': entries}
    # Compare without returning a hash of credentials to business users.
    with FileLock(str(DATA / 'model-catalog.lock'), timeout=10):
        try:
            current = _active()
            if current.get('configuration_sha256') == digest:
                return public_catalog()
        except KeyError:
            pass
        revision = 'mc_' + uid()
        put('model_catalog', {'secret_config': seal(configuration), 'created_at': now()}, revision)
        default = next(entry for entry in entries if entry['id'] == parsed.default_model_id)
        put('system', {'secret_config': seal({k: v for k, v in default.items() if k not in ('id', 'name')}),
                       'verified_at': now() if verify else None}, 'model-provider')
        put('system', {'revision': revision, 'configuration_sha256': digest, 'updated_at': now()}, 'model-catalog')
    return public_catalog()


provision_if_changed = provision


def default_settings():
    configuration = owner_configuration()
    entry = next((entry for entry in configuration['models']
                  if entry['id'] == configuration['default_model_id']), None)
    return {key: value for key, value in entry.items() if key not in ('id', 'name')} if entry else None


def active_selection():
    """Trusted approval checks use the task's ID, never a mutable UI choice."""
    from .provider import current_turn_id
    turn_id = current_turn_id()
    if turn_id:
        turn = get('turn', turn_id)
        if turn.get('model_id'):
            return {'model_id': turn['model_id'], 'model_catalog_revision': turn.get('model_catalog_revision')}
    configuration = owner_configuration()
    return {'model_id': configuration['default_model_id'], 'model_catalog_revision': configuration['revision']}


def _legacy_snapshot(configuration):
    """Freeze a legacy connection without converting owner provisioning mode.

    Content addressing reuses a snapshot across turns with identical settings.
    The credential-derived revision is internal and is never a public model ID.
    """
    raw = {key: value for key, value in configuration.items() if key != 'revision'}
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    revision = 'ml_' + hashlib.sha256(encoded.encode()).hexdigest()[:48]
    with FileLock(str(DATA / 'model-catalog.lock'), timeout=10):
        try:
            if _revision(revision) != raw:
                raise ValueError('任务模型配置快照校验失败，请联系管理员')
        except KeyError:
            put('model_catalog', {'secret_config': seal(raw), 'created_at': now(),
                                  'origin': 'legacy_selection'}, revision)
    return revision


def select(model_id=None, *, session=None, resume_turn=None):
    """Return a nonsecret immutable selection to persist on a new turn."""
    if resume_turn and resume_turn.get('model_id'):
        if model_id and model_id != resume_turn['model_id']:
            raise ValueError('继续任务需使用原任务的模型；切换模型请发起新任务')
        if not resume_turn.get('model_catalog_revision'):
            raise ValueError('原任务缺少模型配置版本记录，无法保证沿用原模型，请发起新任务')
        return {key: resume_turn[key] for key in ('model_id', 'model_name', 'model_catalog_revision') if key in resume_turn}
    raw = owner_configuration()
    chosen = model_id or (session or {}).get('model_id') or raw['default_model_id']
    entry = next((entry for entry in raw['models'] if entry['id'] == chosen), None)
    if not entry:
        if not chosen and not raw['models']:
            return {}
        raise ValueError('所选模型当前不可用，请从提供的模型列表中重新选择')
    revision = raw['revision'] or _legacy_snapshot(raw)
    return {'model_id': entry['id'], 'model_name': entry['name'], 'model_catalog_revision': revision}


def settings_for_turn(turn):
    revision = turn.get('model_catalog_revision')
    if not revision:
        return None
    current = public_catalog()
    if turn.get('model_id') not in {entry['id'] for entry in current['models']}:
        raise ValueError('任务使用的模型已被后台停用，已有结果已保留，请联系管理员或发起新任务')
    try:
        raw = _revision(revision)
        entry = next(entry for entry in raw['models'] if entry['id'] == turn['model_id'])
    except (KeyError, StopIteration):
        raise ValueError('任务的模型配置版本不可用，未切换到其他模型') from None
    return {key: value for key, value in entry.items() if key not in ('id', 'name')}
