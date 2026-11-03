import copy
import pytest
from agent_service import agent_handoff as handoff


def bundle(format_name):
    value = {"format": format_name, "version": 1, "complete": True, "items": []}
    value["manifest_sha256"] = handoff._digest(handoff._json(handoff._manifest(value)))
    return value


@pytest.mark.parametrize("name", ["atlas-agent-handoff", "nexus-agent-handoff"])
def test_old_and_new_handoff_names_preserve_manifest_checks(name):
    value = bundle(name)
    assert handoff.validate_bundle(value)["integrity_verified"] is True
    altered = copy.deepcopy(value)
    altered["format"] = "nexus-agent-handoff" if name.startswith("atlas") else "atlas-agent-handoff"
    with pytest.raises(handoff.HandoffError):
        handoff.validate_bundle(altered)


def test_unknown_handoff_format_still_rejected():
    with pytest.raises(handoff.HandoffError):
        handoff.validate_bundle(bundle("unknown-agent-handoff"))
