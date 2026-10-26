"""Qualification verifier tests; fixture records are never production evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_service import production_release as release

AT = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
BUILD = "a" * 64
CONFIG = {"sha256": "b" * 64, "model_configured": True, "model": "fixture-model"}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    folder = tmp_path / "operator-evidence"
    folder.mkdir()
    monkeypatch.setenv("ATLAS_DEPLOYMENT_ID", "fixture-deployment")
    monkeypatch.setenv("ATLAS_PUBLIC_ORIGIN", "https://fixture.test")
    monkeypatch.setenv("ATLAS_DEPLOYMENT_PROFILE", "single")
    monkeypatch.setenv("ATLAS_AUTH_MODE", "token")
    monkeypatch.delenv("ATLAS_RUNNER_URL", raising=False)
    monkeypatch.delenv("ATLAS_RUNNER_TOKEN", raising=False)
    monkeypatch.setattr("agent_service.config.TOKEN", "fixture-token-" + "x" * 40)
    # Positive tests simulate only the OS boundary in temporary storage. No test
    # mounts a filesystem, calls a model, or supplies deployable evidence.
    monkeypatch.setattr(release, "_read_only_mount", lambda path: True)
    monkeypatch.setattr(release, "build_fingerprint", lambda root=None: {"sha256": BUILD})
    monkeypatch.setattr(release, "configuration_fingerprint", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(release, "runtime_health", lambda build, at: {"readiness": True})
    monkeypatch.setattr(release, "_verify_model_task", lambda measurements, record: None)
    policy = {"schema_version": 1, "deployment_id": "fixture-deployment",
              "origin": "https://fixture.test", "identity_architecture": "owner_workspace",
              "profile": "single", "require_capacity": False, "require_sso": False,
              "capacity_concurrency": 2, "capacity_duration_seconds": 60}
    save(folder / "policy.json", policy)
    manifest = {"schema_version": 1, "deployment_id": policy["deployment_id"],
                "campaign_id": "unit-fixture-never-production", "build_fingerprint": BUILD,
                "configuration_fingerprint": CONFIG["sha256"],
                "case_set_sha256": release.case_manifest()["case_set_sha256"],
                "started_at": (AT - timedelta(hours=1)).isoformat(),
                "completed_at": (AT - timedelta(minutes=1)).isoformat(),
                "expires_at": (AT + timedelta(days=1)).isoformat(),
                "execution_mode": "real", "simulated": False,
                "operator": "unit-fixture", "runner": "unit-fixture", "cases": []}
    measures = {"model_finance": {"model": CONFIG["model"], "model_calls": 1, "task_id": "fixture-task"},
                "browser_reports": {"browser_engine": "chromium", "browser_version": "fixture", "navigation_count": 1},
                "backup_restore": {"backup_id": "fixture-backup", "restore_id": "fixture-restore",
                                   "source_snapshot_sha256": "c" * 64, "restored_snapshot_sha256": "c" * 64}}
    for kind in release.BASE_SUITES:
        for name, expected in release.CASES[kind].items():
            case_id = kind + "/" + name
            record = {k: manifest[k] for k in ("schema_version", "deployment_id", "campaign_id",
                      "build_fingerprint", "configuration_fingerprint", "case_set_sha256",
                      "execution_mode", "simulated", "started_at", "completed_at")}
            record.update(id=case_id, run_id="fixture-run", status="passed",
                          observations=deepcopy(expected), measurements=deepcopy(measures[kind]))
            relative = "artifacts/" + kind + "-" + name + ".json"
            save(folder / relative, record)
            manifest["cases"].append({"id": case_id, "artifact": relative,
                                      "sha256": hashlib.sha256((folder / relative).read_bytes()).hexdigest()})
    save(folder / "acceptance.json", manifest)
    return tmp_path, folder, policy, manifest


def result(campaign):
    return release.evaluate_release(data_dir=campaign[0], at=AT)


def reason(report, name):
    return next(c for c in report["checks"] if c["check"] == name).get("reason")


def update_record(campaign, transform):
    _, folder, _, manifest = campaign
    entry = manifest["cases"][0]
    record = json.loads((folder / entry["artifact"]).read_text())
    transform(record)
    save(folder / entry["artifact"], record)
    entry["sha256"] = hashlib.sha256((folder / entry["artifact"]).read_bytes()).hexdigest()
    save(folder / "acceptance.json", manifest)


def test_valid_contract_and_real_boundary_separate_from_business_evidence(campaign):
    report = result(campaign)
    assert report["eligible"] and report["status"] == "eligible"
    assert report["evidence"]["passed_cases"] == 9
    assert report["identity_architecture"] == "owner_workspace" and not report["multi_tenant"]
    assert not report["application_startup_affected"]
    assert "not external certification" in report["assurance"]


def test_writable_operator_directory_is_not_a_trust_boundary(campaign, monkeypatch):
    monkeypatch.setattr(release, "_read_only_mount", lambda path: False)
    report = result(campaign)
    assert not report["eligible"]
    assert reason(report, "operator_evidence_boundary") == "operator_evidence_requires_separate_read_only_mount"
    assert not any(c["check"] == "acceptance_evidence" for c in report["checks"])


@pytest.mark.parametrize("field,value", [
    ("build_fingerprint", "f" * 64), ("configuration_fingerprint", "f" * 64),
    ("case_set_sha256", "f" * 64), ("execution_mode", "simulated_protocol"),
    ("simulated", True), ("simulated", 0), ("schema_version", True),
    ("expires_at", AT.isoformat()),
    ("expires_at", (AT + timedelta(days=30)).isoformat()),
    ("completed_at", (AT + timedelta(hours=1)).isoformat()),
    ("started_at", "2026-10-21T01:00:00"),
])
def test_stale_simulated_expired_unknown_evidence_blocks(campaign, field, value):
    campaign[3][field] = value
    save(campaign[1] / "acceptance.json", campaign[3])
    assert not result(campaign)["eligible"]


@pytest.mark.parametrize("transform", [
    lambda m: m["cases"].pop(),
    lambda m: m["cases"].append(deepcopy(m["cases"][0])),
    lambda m: m["cases"][0].update(id="unknown/case"),
    lambda m: m["cases"][1].update(id=m["cases"][0]["id"]),
    lambda m: m["cases"][0].update(artifact="../projectfiles/evidence.json"),
    lambda m: m["cases"][0].update(artifact="/tmp/evidence.json"),
    lambda m: m["cases"][0].update(sha256="f" * 64),
    lambda m: m.update(verified=True),
])
def test_no_checkbox_missing_cases_aliases_or_unbound_artifacts(campaign, transform):
    transform(campaign[3])
    save(campaign[1] / "acceptance.json", campaign[3])
    assert not result(campaign)["eligible"]


@pytest.mark.parametrize("transform", [
    lambda r: r.update(status="failed"),
    lambda r: r.update(status="not_executed"),
    lambda r: r.update(simulated=True),
    lambda r: r.update(campaign_id="different-campaign"),
    lambda r: r.update(case_set_sha256="f" * 64),
    lambda r: r["observations"].update(net_amount="118.14"),
    lambda r: r["observations"].update(record_count=True),
    lambda r: r["measurements"].update(model_calls=0),
    lambda r: r["measurements"].update(model="another-model"),
    lambda r: r["measurements"].update(manually_verified=True),
])
def test_case_failure_simulation_observation_and_raw_measurements_fail_closed(campaign, transform):
    update_record(campaign, transform)
    assert not result(campaign)["eligible"]


def test_operator_cannot_silently_disable_deployment_requirements(campaign, monkeypatch):
    policy = campaign[2]
    policy.update(profile="ha", require_capacity=False, require_sso=False)
    monkeypatch.setenv("ATLAS_DEPLOYMENT_PROFILE", "ha")
    save(campaign[1] / "policy.json", policy)
    assert not result(campaign)["eligible"]  # capacity and HA cases now missing
    policy["identity_architecture"] = "multi_tenant"
    save(campaign[1] / "policy.json", policy)
    assert reason(result(campaign), "deployment_policy") == "only_owner_workspace_supported"


def test_model_not_configured_blocks_despite_all_uploaded_evidence(campaign, monkeypatch):
    monkeypatch.setattr(release, "configuration_fingerprint", lambda: dict(CONFIG, model_configured=False))
    assert reason(result(campaign), "real_model_connection") == "model_not_configured"


def test_no_symlinks_or_hardlinks_in_operator_evidence(campaign, tmp_path):
    path = campaign[1] / "artifacts" / "alias.json"
    target = tmp_path / "projectfiles" / "fake.json"
    save(target, {})
    path.symlink_to(target)
    assert reason(result(campaign), "operator_evidence_boundary") == "operator_evidence_alias_rejected"
    path.unlink()
    os.link(target, path)
    assert reason(result(campaign), "operator_evidence_boundary") == "operator_evidence_alias_rejected"


def test_unknown_or_duplicate_json_fields_rejected(campaign):
    (campaign[1] / "policy.json").write_text('{"schema_version":1,"schema_version":1}')
    assert reason(result(campaign), "deployment_policy") == "duplicate_json_key"


def test_health_checks_running_builds_three_roles_freshness_and_ha_counts(monkeypatch):
    from agent_service import runtime
    monkeypatch.setenv("ATLAS_DEPLOYMENT_PROFILE", "single")
    monkeypatch.setattr(runtime, "readiness", lambda: {"ready": True, "profile": "single",
                                                     "checks": {"database": True, "storage": True}})
    node = {"instance": "fixture-node", "heartbeat": AT.isoformat(), "build_fingerprint": BUILD}
    monkeypatch.setattr(runtime, "nodes", lambda role: [deepcopy(node)])
    assert set(release.runtime_health(BUILD, AT)["healthy_nodes"]) == {"api", "agent", "worker"}
    node["build_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="running_build_mismatch"):
        release.runtime_health(BUILD, AT)
    node.pop("build_fingerprint")
    with pytest.raises(ValueError, match="running_build_mismatch"):
        release.runtime_health(BUILD, AT)
    node["build_fingerprint"] = BUILD
    node["heartbeat"] = (AT - timedelta(seconds=20)).isoformat()
    with pytest.raises(ValueError, match="healthy_nodes_missing"):
        release.runtime_health(BUILD, AT)
    node["heartbeat"] = (AT + timedelta(seconds=30)).isoformat()
    with pytest.raises(ValueError, match="healthy_nodes_missing"):
        release.runtime_health(BUILD, AT)
    node["heartbeat"] = AT.isoformat()
    monkeypatch.setenv("ATLAS_DEPLOYMENT_PROFILE", "ha")
    with pytest.raises(ValueError, match="healthy_nodes_missing"):
        release.runtime_health(BUILD, AT)


def test_fingerprint_hashes_actual_source_dependencies_and_static_not_docs_or_results(tmp_path):
    source = tmp_path / "agent_service"
    source.mkdir()
    (source / "main.py").write_text("version = 1")
    for name in release.DEPENDENCY_FILES:
        (tmp_path / name).write_text("locked-1")
    first = release.build_fingerprint(tmp_path)["sha256"]
    save(tmp_path / "BUILD-INFO.json", {"build": "claimed"})
    save(tmp_path / "operator-evidence" / "acceptance.json", {"verified": True})
    (tmp_path / "README.md").write_text("updated documentation")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "main.pyc").write_bytes(b"cache")
    assert release.build_fingerprint(tmp_path)["sha256"] == first
    (source / "main.py").write_text("version = 2")
    second = release.build_fingerprint(tmp_path)["sha256"]
    assert second != first
    (tmp_path / "requirements.lock").write_text("locked-2")
    third = release.build_fingerprint(tmp_path)["sha256"]
    assert third != second
    (source / "static").mkdir()
    (source / "static" / "app.js").write_text("updated-runtime-ui")
    assert release.build_fingerprint(tmp_path)["sha256"] != third


def test_cli_check_nonzero_without_production_evidence_but_app_preflight_still_single(tmp_path):
    environment = dict(os.environ, DATA_DIR=str(tmp_path), DATABASE_URL="sqlite:///" + str(tmp_path / "metadata.sqlite"),
                       ATLAS_DEPLOYMENT_PROFILE="single", ATLAS_AUTH_MODE="token")
    run = subprocess.run([sys.executable, "-m", "agent_service.production_release", "--check"],
                         env=environment, capture_output=True, text=True, timeout=30)
    assert run.returncode == 1 and json.loads(run.stdout)["status"] == "blocked"
    assert "fixture-token" not in run.stdout
    preflight = subprocess.run([sys.executable, "-c", "from agent_service.runtime import preflight; print(preflight())"],
                               env=environment, capture_output=True, text=True, timeout=30)
    assert preflight.returncode == 0 and preflight.stdout.strip() == "single"


def test_finance_receipts_require_actual_finished_task_and_successful_provider_call(monkeypatch):
    from agent_service import store
    task = {"state": "done", "outcome": "verified_complete", "model_usage": {"calls": 1}}
    events = [{"turn_id": "fixture-task", "status": "succeeded", "model": "fixture-model",
               "started_at": AT.isoformat()}]
    monkeypatch.setattr(store, "get", lambda kind, item_id: deepcopy(task))
    monkeypatch.setattr(store, "all_items", lambda kind: deepcopy(events))
    record = {"started_at": (AT - timedelta(minutes=1)).isoformat(), "completed_at": AT.isoformat()}
    measures = {"task_id": "fixture-task", "model": "fixture-model", "model_calls": 1}
    release._verify_model_task(measures, record)
    measures["model_calls"] = 9
    with pytest.raises(ValueError, match="not_verified_complete"):
        release._verify_model_task(measures, record)
    measures["model_calls"] = 1
    events[0]["status"] = "failed"
    with pytest.raises(ValueError, match="successful_model_receipt_missing"):
        release._verify_model_task(measures, record)
    events[0]["status"] = "succeeded"
    events[0]["started_at"] = (AT - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="successful_model_receipt_missing"):
        release._verify_model_task(measures, record)
    task["outcome"] = "unverified"
    with pytest.raises(ValueError, match="not_verified_complete"):
        release._verify_model_task(measures, record)


def test_unmeasurable_build_does_not_interrupt_regular_heartbeats(monkeypatch):
    from agent_service import runtime
    def unavailable():
        raise ValueError("no_lock_in_development_checkout")
    values = []
    monkeypatch.setattr(release, "process_build_fingerprint", unavailable)
    monkeypatch.setattr(runtime, "put", lambda kind, value, item_id: values.append((kind, value)))
    runtime.heartbeat("worker")
    assert len(values) == 2
    assert values[0][1]["build_fingerprint"] is None
