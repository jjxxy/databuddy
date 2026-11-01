"""A new code version is not adopted by a background worker."""
from agent_service.execution import _automatic_adoption_allowed


def test_changed_business_plan_requires_confirmation_but_refresh_is_allowed():
    p={'active_commit':'old','commit':'new','release_mode':'auto','business_plan_confirmation_required':True}
    assert not _automatic_adoption_allowed(p,'new')
    assert _automatic_adoption_allowed(p,'old')


def test_first_new_version_cannot_auto_publish():
    p={'active_commit':None,'commit':'new','release_mode':'auto','business_plan_confirmation_required':True}
    assert not _automatic_adoption_allowed(p,'new')


def test_existing_legacy_auto_policy_is_not_retroactively_rewritten():
    assert _automatic_adoption_allowed({'active_commit':'old','release_mode':'auto'},'new')
    assert not _automatic_adoption_allowed({'active_commit':None,'release_mode':'manual'},'new')
