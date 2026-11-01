"""Visual OCR must not export source bytes without a destination-bound grant."""
import pytest

from agent_service import connectors, model_catalog, provider, store

_real_complete = provider.complete


@pytest.fixture
def setup_vision(monkeypatch):
    calls = []
    config = {'base_url': 'https://vision.example.invalid/v1', 'model': 'fixture', 'share_data': True}
    project = {'id': 'project-fixture'}
    monkeypatch.setattr(model_catalog, 'vision_settings', lambda: config)
    monkeypatch.setattr(store, 'get', lambda kind, identity: project if kind == 'project' and identity == project['id'] else (_ for _ in ()).throw(KeyError(identity)))
    def fake_complete(messages, **kwargs):
        calls.append((messages, kwargs, provider._project_scope.get()))
        return '识别候选'
    monkeypatch.setattr(provider, 'complete', fake_complete)
    return config, project, calls


@pytest.mark.parametrize('consent', [None, False, 'true', 1])
def test_vision_requires_explicit_boolean_share_data(setup_vision, consent):
    config, _, calls = setup_vision
    if consent is None:
        config.pop('share_data')
    else:
        config['share_data'] = consent
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED'):
        connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png')
    assert calls == []


def test_vision_without_configured_model_never_calls_provider(setup_vision, monkeypatch):
    _, _, calls = setup_vision
    monkeypatch.setattr(model_catalog, 'vision_settings', lambda: None)
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED'):
        connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png')
    assert calls == []


@pytest.mark.parametrize('policy', ['company_private', 'work_private', 'work_model_policies', 'mesh_model_policies'])
def test_vision_rejects_private_project_without_exact_destination_grant(setup_vision, policy):
    _, project, calls = setup_vision
    project[policy] = {'fixture': True} if policy.endswith('policies') else True
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED'):
        connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png')
    assert calls == []


@pytest.mark.parametrize('source', [{}, {'project_id': ''}, {'project_id': 'unknown-project'}])
def test_vision_requires_verified_project_context(setup_vision, source):
    _, _, calls = setup_vision
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED'):
        connectors._vision_image_text(source, b'image-fixture', '.png')
    assert calls == []


def test_vision_with_explicit_consent_preserves_project_scope(setup_vision):
    config, _, calls = setup_vision
    assert connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png') == '识别候选'
    assert len(calls) == 1
    _, kwargs, scoped_project = calls[0]
    assert kwargs['configuration'] is config
    assert kwargs['purpose'] == 'vision_ocr'
    assert scoped_project == 'project-fixture'
    assert provider._project_scope.get() is None


def test_vision_failure_does_not_expose_provider_error(setup_vision, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError('sensitive fixture detail')
    monkeypatch.setattr(provider, 'complete', failure)
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED') as error:
        connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png')
    assert 'sensitive fixture detail' not in str(error.value)


def test_vision_real_provider_runs_project_authority_checks_before_transport(setup_vision, monkeypatch):
    from agent_service import agent_mesh, company_search, network_policy
    visited = []
    monkeypatch.setattr(provider, 'complete', _real_complete)
    monkeypatch.setattr(network_policy, 'check_url', lambda url: None)
    monkeypatch.setattr(company_search, 'assert_model_allowed', lambda pid: visited.append(('company', pid)))
    def deny_mesh(pid):
        visited.append(('mesh', pid))
        raise ValueError('not approved')
    monkeypatch.setattr(agent_mesh, 'assert_project_model_allowed', deny_mesh)
    monkeypatch.setattr(provider.httpx, 'Client', lambda *args, **kwargs: pytest.fail('network transport reached'))
    with pytest.raises(connectors.SourceError, match='OCR_REQUIRED'):
        connectors._vision_image_text({'project_id': 'project-fixture'}, b'image-fixture', '.png')
    assert visited == [('company', 'project-fixture'), ('mesh', 'project-fixture')]
