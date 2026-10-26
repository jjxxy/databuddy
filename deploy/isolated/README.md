# Atlas v0.20: isolated owner-workspace deployment

This is **one independent Atlas backend container per owner workspace**, not a
shared-process multi-tenant SaaS implementation. Each backend has its own named
`/data` volume containing metadata SQLite, `connector.key`, uploads, runs, Git
projects, `/data/home`, and `opencode-runtime`. One exact OIDC subject per workspace
is the recommended independent-user configuration. Multiple subjects on one
workspace deliberately share **all** that owner's business content; they are
trusted collaborators, not isolated tenants.

The gateway serves the local browser shell and routes browser `/api` requests
only after verifying a signed access token and resolving its exact subject to a
configured backend. Each backend independently verifies the original token's
signature, issuer, audience and its own exact subject list. The gateway has no
business DATA volume, database, encryption key, model secret or APP_TOKEN.
Machine A2A/MCP, agent administration and operator APIs are not exposed by this
browser gateway. Header-selected workspaces are not supported.

## What has and has not been verified

The renderer has offline rejection/topology tests. A separate integration test
starts two real Atlas API processes and a real gateway process, exercising HTTP
routing, projects, upload/download, artifact catalogs, persisted jobs and foreign
ID denial with independent real JWT verification. Its test-only JWKS retrieval
uses a local public key fixture; it does not contact an IdP, run workers, call a
model or prove Docker networking. Test results must be reported separately.

These files are an operator-reviewed deployment starting point, **not a claim
of production verification**. Docker build/start, host firewall enforcement,
public TLS/IdP, model operation, backup restoration, resource/load capacity and
operational acceptance must pass in the target environment before release.
Containers share a host kernel: this is not protection against a hostile host
administrator, kernel compromise or arbitrary adversarial code execution.

## Render without provisioning

From the repository root:

```sh
cp deploy/isolated/manifest.example.json deploy/isolated/manifest.json
# Edit the non-secret issuer, audience, exact subjects, origin and secret filenames.
python -m agent_service.isolated_deployment deploy/isolated/manifest.json --output-dir deploy/isolated
```

The output is `compose.json` (valid Compose JSON/YAML) and `gateway.json`. Keep
these in `deploy/isolated`: the fixed build context `../..` points to the repo.
Generation neither reads secret files nor contacts any service. Run it on trusted
local paths; it replaces those two output files. It rejects unknown fields,
duplicate JSON fields/workspace IDs/subjects, interpolation in inputs, wildcard
subjects, path traversal and unsafe secret filenames. Backend, volume, database,
network and token-variable names are derived; arbitrary shared storage, network,
backend URLs and database overrides are not accepted. Do not hand-edit generated
topology to share resources. Use distinct Compose project names for separate
installations and never attach foreign containers to workspace networks.

Each backend attaches to exactly its own Docker bridge; only the gateway attaches
to all workspace bridges. Backend ports are **not host-published**. Bridges allow
outbound traffic because independent JWKS verification and model connectors need
it; they are not outbound firewalls. Operators must enforce host network policy,
block access to host/private services where inappropriate and validate that one
workspace cannot reach another. There is no host Docker socket, privileged mode,
host networking or shared network namespace. The gateway is a privileged routing
trust boundary: a compromised gateway can reach every backend, though each still
requires valid owner credentials.

## Operator prerequisites and secret handling

1. Configure an IdP to issue short-lived **access tokens**, with exact configured
   `iss`, `aud`, `sub`, `iat`, `exp`, and a supported RS256/ES256 signing key with a
   unique `kid`. Export only its public JWKS to
   `deploy/isolated/secrets/idp-public-jwks.json`. The backend HTTPS `jwks_url`
   must publish the matching public keys and be reachable under Atlas's URL
   safety policy. This release has manual token acquisition/paste, **not a full
   authorization-code/PKCE SSO login, callback, refresh or logout flow**. Obtain
   tokens via your approved IdP client/tool. Never paste an IdP client secret or
   signing private key into Atlas.
2. Supply a distinct model provisioning JSON per workspace when using the
   example's `model_config_file` fields; use the existing model configuration
   format. The manifest requires that field: a fresh OpenCode-enabled instance
   cannot start without a configured model catalog. Do not omit provisioning. These files mount read-only as Compose
   secrets. On local Compose, file secrets are bind-mounted, not an encrypted
   secret-management service. Ensure UID 10001 can read the necessary files,
   while unrelated host users cannot; use filesystem ACLs or a properly managed
   secret service. Do not commit them. Only the relevant backend receives each
   model file. Model secret updates do not overwrite existing provisioned model
   state on restart; follow the existing operator model-management procedure.
3. Set **different random** `ATLAS_ALICE_APP_TOKEN` and `ATLAS_BOB_APP_TOKEN`
   variables (or corresponding generated names) in the operator's protected
   deployment environment. Each must be at least 16 characters because cloud
   startup still requires APP_TOKEN even in OIDC mode. The renderer writes only
   `${ATLAS_<ID>_APP_TOKEN:?...}` placeholders and never generates credentials.
   Never reuse them, distribute them to browser users, print resolved Compose
   configuration into logs, or use token auth as a fallback. Docker administrators
   can inspect container environment values. Compose ensures presence, not
   entropy/uniqueness; operator secret management must enforce these properties.
   Do not turn on `ATLAS_CLOUD_VERIFY`, whose legacy checker assumes token auth.
4. Backups must include each complete DATA volume and its unique encryption key;
   keep workspace backup destinations and retention/access controls independent.
   Test restoration without pointing two live backends at one volume. Deleting a
   volume destroys its data/key; do not use `down -v` as a routine restart.

Only after separately authorized infrastructure preparation, review/build/start
with the target host's Compose tooling:

```sh
docker compose -f deploy/isolated/compose.json config --quiet
docker compose -f deploy/isolated/compose.json build
docker compose -f deploy/isolated/compose.json up -d
```

Only the gateway binds `127.0.0.1:8080`. A host TLS reverse proxy must expose the
configured HTTPS origin, preserve the original Host and Authorization header,
and forward to that loopback listener. Do not change it to `0.0.0.0` as a TLS
substitute. Uvicorn does not trust proxy headers. Set proxy body/timeout limits
consistently with the gateway; do not log bearer tokens or request bodies. Do not
expose backend, OpenCode or worker ports. Health requests to gateway must carry
the configured Host; browser HTTPS-origin policy is enforced.

Gateway JWKS is a read-only startup snapshot. Stage and validate public keys,
atomically replace the host file, then **recreate/restart the gateway and verify
new and retired keys** (a file bind mount may retain the old inode until
recreation). Backends independently fetch HTTPS JWKS and cache it for up to five
minutes. Plan overlap and emergency revocation accordingly; do not claim instant
revocation. Test short token expiry and IdP key rotation before launch.

Each workspace defaults to a 2 GiB memory limit, 2 CPU quota and 256 PIDs;
gateway defaults to 512 MiB, 1 CPU and 64 PIDs. Its generated concurrency limit
is two in-flight requests (including downloads), sized so two bounded 26 MiB
upload spools fit its 64 MiB `/tmp` budget. Excess concurrency returns 503; tune
concurrency, temporary disk and memory together, not independently. Two users therefore permit about
4.5 GiB aggregate container memory before host/proxy overhead. Ten workspaces
permit about 20.5 GiB, plus disk growth. These are limits, not measured capacity
or reservations. Set target-host capacity, disk quotas/alerts and load tests;
per-user worker/OpenCode processes intentionally cost more than shared execution.
Root filesystems are read-only, backend processes run as UID 10001, capabilities
are dropped, `/tmp` is bounded tmpfs, and DATA is the only writable persistent
mount. Named volumes must be initialized with the image's UID 10001 ownership;
verify this on the actual storage driver rather than enabling root execution.

## Validation and acceptance gates

```sh
python -m pytest tests/test_isolated_deployment.py tests/test_isolated_workspace_processes.py -q
python -m pytest tests/test_workspace_gateway.py -q
```

The first suite never deploys containers. The process test binds loopback sockets
and uses temporary data and ephemeral test signing keys, destroyed after the
run; it uses no production credentials and must not substitute for these gates:

- **Two users, simultaneously:** obtain Alice/Bob access tokens from the real
  IdP. Create distinguishable projects and upload distinguishable files through
  the public HTTPS gateway; queue and run jobs; generate/download artifacts.
  Verify lists, conversations, source downloads, artifact preview/download,
  jobs and history only contain that user's workspace. Reuse idempotency keys
  across owners and confirm they cannot cross-read/mutate/cancel each other's
  project/source/job/artifact IDs. Also test intentional identical project IDs
  across independent databases remain private by content, not just ID.
- **Adversarial routing:** missing/expired/wrong-issuer/wrong-audience/unknown-
  subject/wrong-signature tokens fail; a valid Bob token sent directly to Alice
  fails independently. Forged workspace, forwarded-user, access-token, Host and
  Origin headers cannot select another backend. Confirm A2A/MCP/operator routes
  are unavailable through the browser gateway.
- **Physical controls:** inspect target-host networks/mounts/ports; only loopback
  gateway is published; distinct DATA volumes and key fingerprints; no cross-
  workspace mount, Docker socket or host namespace. Attempt cross-network
  connection from Alice's container, and verify Bob cannot be reached. Verify
  permitted IdP/model egress and denied sensitive internal destinations under
  the host policy. Inspect logs for absence of tokens/model secrets/private rows.
- **Lifecycle:** restart one backend during active work; prove recovery and
  durable job/artifact state, while the other user continues. Restart gateway;
  verify routing and no business data stored there. Exercise expiry/key rotation,
  one-workspace backup/restore, disk-full and memory/queue limits. Measure real
  concurrency/load and document operator alert/incident procedures.
- **Sign-off:** record exact code/image digest, manifest hash, IdP config,
  deployment host, timestamps, test users, redacted results and remaining risks.
  Do not mark deployment production-ready until these gates and the existing
  Atlas release checks are independently satisfied.
