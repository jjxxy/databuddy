"""Opt-in deployment qualification, never a replacement for runtime preflight.

The gate reads operator-produced evidence from a separate read-only mount at
DATA/operator-evidence. It cannot issue evidence or certify its own deployment.
Evidence is trusted at that operating-system boundary, not externally attested.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
MAX_AGE = timedelta(days=7)
HEX = re.compile(r"[0-9a-f]{64}\Z")
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock", "requirements.lock",
                    "requirements-cloud.lock", "requirements-multimodal.txt")
# These expectations are release criteria, not operator-editable success flags.
# Changes invalidate case_set_sha256 and, because this is runtime code, the build.
CASES = {
    "model_finance": {
        "decimal_money": {"net_amount": "118.15", "record_count": 2},
        "reviewed_semantics": {"semantic_confirmation_recorded": True},
        "source_lineage": {"all_outputs_source_linked": True},
        "office_delivery": {"editable_docx_verified": True, "editable_pptx_verified": True},
        "task_recovery": {"interrupted_task_resumed": True, "duplicate_effects": 0},
    },
    "browser_reports": {
        "authorized_report": {"authorized_fetch": True, "download_parsed": True},
        "invalid_download": {"non_report_download_rejected": True},
        "failed_fetch": {"failed_download_not_completed": True},
    },
    "backup_restore": {
        "restore_roundtrip": {"restored_snapshot_matches": True,
                              "restored_encrypted_credentials_usable": True,
                              "audit_chain_verified": True},
    },
    "capacity": {"declared_load": {"resource_limits_held": True, "failed_requests": 0}},
    "sso": {"owner_boundary": {"valid_owner_status": 200,
                                "invalid_token_status": 401, "non_owner_status": 403}},
    "ha_recovery": {"disrupted_process": {"pending_task_recovered": True,
                                            "shared_storage_integrity": True,
                                            "duplicate_effects": 0}},
    "python_isolation": {"runner_boundary": {"network_denied": True,
                                              "host_files_denied": True,
                                              "resource_limits_held": True}},
}
BASE_SUITES = ("model_finance", "browser_reports", "backup_restore")
CASE_PROTOCOL = {
    "model_finance": "Use the deployed configured model and actual task API. Decimal fixture: "
    "A1 paid 100.25 refund 10.10; A2 unpaid 20 refund 0; A3 paid 30 refund 2. "
    "Aggregate only paid amounts minus refunds. Independently verify exact output, persisted "
    "human metric confirmation, source links, editable Word/PPT contents, and interrupted task "
    "recovery without duplicated effects. Retain task transcripts and output files.",
    "browser_reports": "Use an actual supported browser against operator-authorized reports. "
    "Verify authorized report download and parsing, rejection of a non-report download, and "
    "failure reporting when fetching fails. Retain the browser trace and downloaded bytes.",
    "backup_restore": "Create a real backup, restore into an isolated target, compare source "
    "and restored snapshot hashes, use restored encrypted credentials, verify the audit chain. "
    "Retain backup/restore execution logs. Never restore over the live workspace for this test.",
    "capacity": "Run the operator-declared concurrency and duration against the deployment; "
    "measure requests, failures and resource limits. Retain the load-runner output.",
    "sso": "Exercise the actual gateway and identity provider using an allowed owner, an "
    "invalid token, and a non-owner identity. Retain redacted HTTP results and gateway run ID.",
    "ha_recovery": "Disrupt a process holding a pending real task on the HA deployment and "
    "observe recovery, shared storage integrity, and absence of duplicate effects. Retain "
    "orchestrator events, task records and timing.",
    "python_isolation": "Run real bounded probes through the configured independent runner "
    "to check blocked networking and host-file access plus enforced resource limits. Retain "
    "the runner receipts. Configuration alone does not establish isolation.",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def case_manifest():
    """Public input contract for an independently run acceptance campaign."""
    value = {"schema_version": SCHEMA_VERSION, "suites": CASES, "protocol": CASE_PROTOCOL}
    return {**value, "case_set_sha256": _sha(_canonical(value))}


def build_fingerprint(root: Path | None = None):
    """Hash actual source bytes, dependency locks and installed version inventory.

    BUILD-INFO, source manifests, docs, test results and operator evidence are
    deliberately not inputs. This is a local measurement, not image attestation.
    """
    root = Path(root or ROOT).resolve()
    source = root / "agent_service"
    if not source.is_dir() or source.is_symlink():
        raise ValueError("runtime_source_missing")
    files = [p for p in source.rglob("*") if p.is_file()
             and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")]
    if not any(p.suffix == ".py" for p in files):
        raise ValueError("runtime_source_missing")
    dependency_files = []
    for name in DEPENDENCY_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
            dependency_files.append(name)
    if not any(name.endswith(".lock") for name in dependency_files):
        raise ValueError("dependency_manifest_missing")
    for pattern in ("Dockerfile*", "compose*.yaml", "start.*", "run-local.sh",
                    "bootstrap.py", "quickstart.py", "llm.example.json", "railway.toml",
                    "deploy/install_opencode.py"):
        files.extend(p for p in root.glob(pattern) if p.is_file())
    records = []
    for path in sorted(set(files), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any((root / Path(*relative.parts[:i])).is_symlink()
               for i in range(1, len(relative.parts) + 1)):
            raise ValueError("runtime_symlink_not_supported")
        records.append([relative.as_posix(), _sha(path.read_bytes())])
    inventory = sorted({(re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(), d.version)
                        for d in importlib.metadata.distributions() if d.metadata["Name"]})
    if not inventory:
        raise ValueError("installed_dependencies_unknown")
    value = {"algorithm": "atlas-runtime-v1", "files": records,
             "python": platform.python_version(), "installed_distributions": inventory}
    return {"algorithm": value["algorithm"], "sha256": _sha(_canonical(value)),
            "source_files": len(records), "installed_distributions": len(inventory),
            "dependency_files": dependency_files}


@lru_cache(maxsize=1)
def process_build_fingerprint():
    """Frozen on the first heartbeat; changed code needs restarted processes."""
    return build_fingerprint()["sha256"]


def configuration_fingerprint():
    from .provider import status
    model = status()
    fields = ("ATLAS_DEPLOYMENT_ID", "ATLAS_PUBLIC_ORIGIN", "ATLAS_DEPLOYMENT_PROFILE",
              "ATLAS_AUTH_MODE", "OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL",
              "OIDC_ALLOWED_SUBJECTS", "OIDC_ACCEPT_PROXY_HEADER", "ATLAS_STORAGE_MODE",
              "ATLAS_STORAGE_ID", "AGENT_ORCHESTRATION_MODE", "SCHEDULER_MODE",
              "ATLAS_RUNNER_URL", "ATLAS_RUNNER_ISOLATION")
    # Only the digest is returned; this snapshot is never emitted or persisted.
    snapshot = {"environment": {k: os.getenv(k, "") for k in fields}, "model": model}
    return {"sha256": _sha(_canonical(snapshot)), "model_configured": model.get("configured") is True,
            "model": model.get("model", "")}


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(label + "_schema")
    return value


def _string(value):
    return isinstance(value, str) and 0 < len(value) <= 500 and not any(ord(c) < 32 for c in value)


def _hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _date(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp_invalid") from None
    if result.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return result.astimezone(timezone.utc)


def _json(path, max_bytes=2_000_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("evidence_file_invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite_json")
    try:
        return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("evidence_json_invalid") from None


def _read_only_mount(path):
    """Require a real, separate Linux read-only mount, not a checkbox/chmod.

    A chmod-only child of app-writable DATA can be replaced by that app. Mount
    isolation must be provisioned by the operator; this function never mounts.
    Unsupported platforms fail closed for qualification, not for application use.
    """
    try:
        if not (os.statvfs(path).f_flag & os.ST_RDONLY):
            return False
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            if Path(point) == path and "ro" in fields[5].split(","):
                return True
    except (OSError, IndexError, ValueError):
        pass
    return False


def _evidence_boundary(folder, data):
    if folder != data / "operator-evidence" or folder.is_symlink() or not folder.is_dir():
        raise ValueError("operator_evidence_missing")
    if not _read_only_mount(folder):
        raise ValueError("operator_evidence_requires_separate_read_only_mount")
    # Reject aliases to project files even within an operator-provisioned mount.
    for path in folder.rglob("*"):
        if path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1):
            raise ValueError("operator_evidence_alias_rejected")


def _policy(folder):
    value = _object(_json(folder / "policy.json"), (
        "schema_version", "deployment_id", "origin", "identity_architecture", "profile",
        "require_capacity", "require_sso", "capacity_concurrency", "capacity_duration_seconds"), "policy")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("policy_version")
    if not _string(value["deployment_id"]) or value["deployment_id"] != os.getenv("ATLAS_DEPLOYMENT_ID"):
        raise ValueError("deployment_id_mismatch_or_missing")
    origin = urlsplit(value["origin"]) if isinstance(value["origin"], str) else None
    if (not origin or origin.scheme != "https" or not origin.hostname or origin.username
            or origin.password or origin.query or origin.fragment or origin.path not in ("", "/")
            or value["origin"].rstrip("/") != os.getenv("ATLAS_PUBLIC_ORIGIN", "").rstrip("/")):
        raise ValueError("deployment_origin_mismatch_or_invalid")
    if value["identity_architecture"] != "owner_workspace":
        raise ValueError("only_owner_workspace_supported")
    if value["profile"] not in ("single", "ha") or value["profile"] != os.getenv("ATLAS_DEPLOYMENT_PROFILE", "single"):
        raise ValueError("deployment_profile_mismatch")
    if any(type(value[k]) is not bool for k in ("require_capacity", "require_sso")):
        raise ValueError("policy_boolean_invalid")
    if any(type(value[k]) is not int or value[k] < 1 for k in ("capacity_concurrency", "capacity_duration_seconds")):
        raise ValueError("capacity_requirements_invalid")
    return value


def _identity(policy):
    from .identity import mode, validate_config
    from .config import TOKEN
    validate_config()
    if mode() == "token" and (len(TOKEN) < 32 or policy["require_sso"]):
        raise ValueError("owner_authentication_not_qualified")
    if mode() not in ("token", "oidc"):
        raise ValueError("identity_mode_unknown")
    return {"architecture": "owner_workspace", "authentication": mode(),
            "tenant_isolation": False, "note": "Allowed owner identities share the whole workspace."}


def runtime_health(build, at):
    from .runtime import nodes, readiness
    state = readiness()
    if state.get("ready") is not True or state.get("checks") != {"database": True, "storage": True}:
        raise ValueError("runtime_readiness_failed")
    required = 2 if os.getenv("ATLAS_DEPLOYMENT_PROFILE", "single") == "ha" else 1
    counts = {}
    for role in ("api", "agent", "worker"):
        fresh = []
        for node in nodes(role):
            age = (at - _date(node.get("heartbeat"))).total_seconds()
            if -5 <= age < 20:
                fresh.append(node)
        if len({n.get("instance") for n in fresh if _string(n.get("instance"))}) < required:
            raise ValueError(role + "_healthy_nodes_missing")
        if any(n.get("build_fingerprint") != build for n in fresh):
            raise ValueError(role + "_running_build_mismatch_or_unknown")
        counts[role] = len(fresh)
    return {"readiness": True, "healthy_nodes": counts, "profile": state.get("profile")}


def _required_suites(policy):
    from .identity import mode
    from .isolation import status
    required = list(BASE_SUITES)
    if policy["require_capacity"] or policy["profile"] == "ha":
        required.append("capacity")
    if policy["require_sso"] or mode() == "oidc":
        required.append("sso")
    if policy["profile"] == "ha":
        required.append("ha_recovery")
    if status().get("configured") is True:
        required.append("python_isolation")
    return required


def _measurements(kind, value, policy, config):
    fields = {
        "model_finance": ("model", "model_calls", "task_id"),
        "browser_reports": ("browser_engine", "browser_version", "navigation_count"),
        "backup_restore": ("backup_id", "restore_id", "source_snapshot_sha256", "restored_snapshot_sha256"),
        "capacity": ("observed_concurrency", "duration_seconds", "requests"),
        "sso": ("issuer", "audience", "gateway_run_id"),
        "ha_recovery": ("disruption_id", "recovered_task_id", "recovery_seconds"),
        "python_isolation": ("execution_id", "isolation", "network"),
    }
    _object(value, fields[kind], "measurements")
    def positive(name):
        return type(value[name]) is int and value[name] > 0
    if kind == "model_finance":
        ok = value["model"] == config["model"] and positive("model_calls") and _string(value["task_id"])
    elif kind == "browser_reports":
        ok = value["browser_engine"] in ("chromium", "firefox", "webkit") and _string(value["browser_version"]) and positive("navigation_count")
    elif kind == "backup_restore":
        ok = (_string(value["backup_id"]) and _string(value["restore_id"])
              and _hash(value["source_snapshot_sha256"])
              and value["source_snapshot_sha256"] == value["restored_snapshot_sha256"])
    elif kind == "capacity":
        ok = (positive("observed_concurrency") and value["observed_concurrency"] >= policy["capacity_concurrency"]
              and positive("duration_seconds") and value["duration_seconds"] >= policy["capacity_duration_seconds"]
              and positive("requests"))
    elif kind == "sso":
        ok = value["issuer"] == os.getenv("OIDC_ISSUER") and value["audience"] == os.getenv("OIDC_AUDIENCE") and _string(value["gateway_run_id"])
    elif kind == "ha_recovery":
        ok = (_string(value["disruption_id"]) and _string(value["recovered_task_id"])
              and type(value["recovery_seconds"]) in (int, float) and 0 < value["recovery_seconds"] <= 300)
    else:
        ok = (_string(value["execution_id"]) and value["isolation"] == os.getenv("ATLAS_RUNNER_ISOLATION")
              and value["isolation"] in ("microvm", "gvisor") and value["network"] == "none")
    if not ok:
        raise ValueError("measurements_failed_or_unknown")


def _verify_model_task(measurements, record):
    """Cross-check finance evidence against actual deployment-side receipts.

    A configured model and an operator's 'passed' field alone are insufficient.
    Provider receipts still do not attest which system a remote endpoint runs.
    """
    from .store import get, all_items
    task = get("turn", measurements["task_id"])
    if (task.get("state") != "done" or task.get("outcome") != "verified_complete"
            or type(task.get("model_usage", {}).get("calls")) is not int
            or task["model_usage"]["calls"] < 1
            or measurements["model_calls"] != task["model_usage"]["calls"]):
        raise ValueError("finance_task_not_verified_complete")
    start, end = _date(record["started_at"]), _date(record["completed_at"])
    events = [event for event in all_items("model_event")
              if event.get("turn_id") == measurements["task_id"]
              and event.get("status") == "succeeded"
              and event.get("model") == measurements["model"]
              and start - timedelta(seconds=5) <= _date(event.get("started_at")) <= end + timedelta(seconds=5)]
    if not events:
        raise ValueError("finance_successful_model_receipt_missing")


def _acceptance(folder, policy, build, config, at):
    manifest = _object(_json(folder / "acceptance.json"), (
        "schema_version", "deployment_id", "campaign_id", "build_fingerprint",
        "configuration_fingerprint", "case_set_sha256", "started_at", "completed_at",
        "expires_at", "execution_mode", "simulated", "operator", "runner", "cases"), "acceptance")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("acceptance_version")
    for key in ("campaign_id", "operator", "runner"):
        if not _string(manifest[key]):
            raise ValueError("acceptance_provenance_missing")
    expected = {"deployment_id": policy["deployment_id"], "build_fingerprint": build,
                "configuration_fingerprint": config["sha256"],
                "case_set_sha256": case_manifest()["case_set_sha256"]}
    if any(manifest[k] != v for k, v in expected.items()):
        raise ValueError("acceptance_build_configuration_or_cases_mismatch")
    if manifest["execution_mode"] != "real" or manifest["simulated"] is not False:
        raise ValueError("acceptance_not_real")
    started, completed, expires = map(_date, (manifest["started_at"], manifest["completed_at"], manifest["expires_at"]))
    if not (started <= completed <= at + timedelta(seconds=5) and completed < expires and at < expires
            and completed - started <= MAX_AGE and expires - completed <= MAX_AGE
            and at - completed <= MAX_AGE):
        raise ValueError("acceptance_expired_or_timestamps_invalid")
    required = _required_suites(policy)
    expected_ids = {kind + "/" + case for kind in required for case in CASES[kind]}
    entries = manifest["cases"]
    if not isinstance(entries, list) or len(entries) != len(expected_ids):
        raise ValueError("acceptance_case_set_incomplete")
    seen = set()
    for entry in entries:
        _object(entry, ("id", "artifact", "sha256"), "case_entry")
        case_id = entry["id"]
        if not isinstance(case_id, str) or case_id not in expected_ids or case_id in seen:
            raise ValueError("acceptance_unknown_or_duplicate_case")
        seen.add(case_id)
        relative = Path(entry["artifact"]) if isinstance(entry["artifact"], str) else Path("/")
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                or relative.parts[0] != "artifacts" or not _hash(entry["sha256"])):
            raise ValueError("acceptance_artifact_path_invalid")
        path = folder / relative
        record = _json(path)
        if _sha(path.read_bytes()) != entry["sha256"]:
            raise ValueError("acceptance_artifact_hash_mismatch")
        _object(record, ("schema_version", "id", "run_id", "deployment_id", "campaign_id",
                         "build_fingerprint", "configuration_fingerprint", "case_set_sha256",
                         "execution_mode", "simulated", "started_at", "completed_at",
                         "status", "observations", "measurements"), "case_record")
        if (type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION
                or record["id"] != case_id or not _string(record["run_id"])
                or record["campaign_id"] != manifest["campaign_id"]
                or any(record[k] != v for k, v in expected.items())):
            raise ValueError("case_provenance_or_case_set_mismatch")
        if record["execution_mode"] != "real" or record["simulated"] is not False or record["status"] != "passed":
            raise ValueError("case_failed_simulated_or_unknown")
        if not started <= _date(record["started_at"]) <= _date(record["completed_at"]) <= completed:
            raise ValueError("case_timestamps_invalid")
        kind, name = case_id.split("/")
        observations = _object(record["observations"], CASES[kind][name], "observations")
        if any(type(observations[k]) is not type(v) or observations[k] != v for k, v in CASES[kind][name].items()):
            raise ValueError("case_observation_failed")
        _measurements(kind, record["measurements"], policy, config)
        if kind == "model_finance":
            _verify_model_task(record["measurements"], record)
    return {"campaign_id": manifest["campaign_id"], "completed_at": manifest["completed_at"],
            "expires_at": manifest["expires_at"], "passed_cases": len(seen), "required_suites": required}


def evaluate_release(*, root=None, data_dir=None, at=None):
    """Read-only public report; never enables deployment or changes app modes."""
    from .config import DATA
    at = at or datetime.now(timezone.utc)
    data = Path(data_dir or DATA).resolve()
    folder = data / "operator-evidence"
    checks = []
    report = {"schema_version": SCHEMA_VERSION, "status": "blocked", "eligible": False,
              "evaluated_at": at.isoformat(), "checks": checks,
              "identity_architecture": "owner_workspace", "multi_tenant": False,
              "assurance": "Operator evidence behind an OS read-only mount; not external certification.",
              "scope": "deployment_qualification_only", "application_startup_affected": False}
    def check(name, operation):
        try:
            value = operation()
            checks.append({"check": name, "status": "passed"})
            return value
        except Exception as exc:
            code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "unavailable_or_invalid"
            checks.append({"check": name, "status": "blocked", "reason": code})
            return None
    build = check("actual_build", lambda: build_fingerprint(root))
    if build:
        report["build"] = build
    config = check("runtime_configuration", configuration_fingerprint)
    if config:
        report["configuration_fingerprint"] = config["sha256"]
        if not config["model_configured"]:
            checks.append({"check": "real_model_connection", "status": "blocked", "reason": "model_not_configured"})
    if build:
        health = check("runtime_health", lambda: runtime_health(build["sha256"], at))
        if health:
            report["runtime"] = health
    boundary = check("operator_evidence_boundary", lambda: (_evidence_boundary(folder, data), True)[1])
    if boundary:
        policy = check("deployment_policy", lambda: _policy(folder))
        if policy:
            identity = check("identity", lambda: _identity(policy))
            if identity:
                report["identity"] = identity
            if build and config:
                evidence = check("acceptance_evidence", lambda: _acceptance(folder, policy, build["sha256"], config, at))
                if evidence:
                    report["evidence"] = evidence
    if checks and all(c["status"] == "passed" for c in checks):
        report.update(status="eligible", eligible=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Exit 1 unless deployment qualification passes")
    actions.add_argument("--fingerprint", action="store_true", help="Measure the current actual build")
    actions.add_argument("--case-manifest", action="store_true", help="Print the fixed acceptance case contract")
    args = parser.parse_args(argv)
    try:
        result = build_fingerprint() if args.fingerprint else case_manifest() if args.case_manifest else evaluate_release()
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "qualification_unavailable"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if args.check and not result["eligible"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
