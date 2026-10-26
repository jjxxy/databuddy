#!/usr/bin/env bash
set -euo pipefail
# Explicit target and prepared inputs only. Never selects a guessed cluster.
config_file=${1:?Pass enterprise config JSON}
target_context=${2:?Pass the intended kubectl context}
manifest_path=${3:-/tmp/atlas-enterprise.yaml}
python scripts/render_enterprise.py "$config_file" "$manifest_path"
namespace=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"])' "$config_file")
secret_name=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["secretName"])' "$config_file")
claim_name=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["rwxClaim"])' "$config_file")
kubectl --context "$target_context" get namespace "$namespace" >/dev/null
kubectl --context "$target_context" -n "$namespace" get secret "$secret_name" >/dev/null
kubectl --context "$target_context" -n "$namespace" get pvc "$claim_name" -o json | python -c 'import json,sys; v=json.load(sys.stdin); assert v["status"]["phase"]=="Bound" and "ReadWriteMany" in v["spec"]["accessModes"], "Requires a bound, HA-backed RWX claim"'
# Server-side validation catches missing CRDs and unsupported Kubernetes fields before applying.
kubectl --context "$target_context" apply --dry-run=server -f "$manifest_path" >/dev/null
# Apply dependencies first. The Secret/PVC remain owner-managed and are never overwritten.
python - "$manifest_path" <<'PY'
import sys,yaml
from pathlib import Path
values=list(yaml.safe_load_all(Path(sys.argv[1]).read_text()))
for suffix,kinds in [('bootstrap',{'Namespace','ServiceAccount','Cluster'}),('init',{'Job'}),('runtime',{'Deployment','PodDisruptionBudget','Service','Ingress','NetworkPolicy','CronJob'})]:
 Path(sys.argv[1]+'.'+suffix).write_text(yaml.safe_dump_all([v for v in values if v['kind'] in kinds],sort_keys=False))
PY
kubectl --context "$target_context" apply -f "$manifest_path.bootstrap"
if kubectl --context "$target_context" -n "$namespace" get cluster atlas-db >/dev/null 2>&1; then kubectl --context "$target_context" -n "$namespace" wait --for=condition=Ready cluster/atlas-db --timeout=15m; fi
kubectl --context "$target_context" apply -f "$manifest_path.init"
kubectl --context "$target_context" -n "$namespace" wait --for=condition=complete job/atlas-init --timeout=10m
kubectl --context "$target_context" apply -f "$manifest_path.runtime"
for role in api agent worker gateway; do kubectl --context "$target_context" -n "$namespace" rollout status "deployment/atlas-$role" --timeout=10m; done
printf '%s\n' 'Rollout complete. Run fault acceptance before asserting an availability SLO.'
