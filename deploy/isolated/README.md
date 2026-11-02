# Nexus WorkStudio v0.20: isolated owner-workspace deployment

This is **one independent Nexus WorkStudio backend container per owner workspace**, not a
shared-process multi-tenant SaaS implementation. Each backend has its own named
`/data` volume containing metadata SQLite, `connector.key`, uploads, runs, Git
projects, `/data/home`, and `opencode-runtime`. One exact OIDC subject per workspace
is the recommended independent-user configuration. Multiple subjects on one
workspace deliberately share **all** that owner's business content; they are
trusted collaborators, not isolated tenants.

To prevent a personal workspace being accidentally configured for multiple
users, set `"sharing": "personal"` on that workspace in the manifest. Rendering
then requires exactly one explicit OIDC subject and enables
`NEXUS_REQUIRE_SINGLE_OIDC_SUBJECT=1` on its backend. The backend independently
validates this policy at startup and each authentication request: it requires
OIDC, exactly one subject, and direct token verification without the forwarded
access-token header. Invalid or widened configuration is rejected. Existing
manifest entries without `sharing` remain explicitly shared-owner workspaces.
This prevents one common identity misconfiguration; it does not prove the host,
network, storage, gateway, IdP or model deployment isolates users.

The gateway serves the local browser shell and routes browser `/api` requests
only after verifying a signed access token and resolving its exact subject to a
configured backend. Each backend independently verifies the original token's
signature, issuer, audience and its own exact subject list. The gateway has no
business DATA volume, database, encryption key, model secret or APP_TOKEN.
Machine A2A/MCP, agent administration and operator APIs are not exposed by this
browser gateway. Header-selected workspaces are not supported.

## What has and has not been verified

The renderer has offline rejection/topology tests. A separate integration test
starts two real Nexus WorkStudio API processes and a real gateway process, exercising HTTP
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
Generation neither reads secret files nor contacts any service. The generated
services build from the repository’s `Dockerfile.quickstart`; Docker excludes
`deploy/isolated/secrets/` and the local manifest from its build context. The
image prepares `/data` for UID 10001, but existing volumes retain their prior
ownership and need separate verification. Run generation on trusted local
paths; it replaces those two output files. It rejects unknown fields,
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
   must publish the matching public keys and be reachable under Nexus WorkStudio's URL
   safety policy. By default users obtain and paste an access token via an approved IdP client.
   An optional gateway authorization-code/PKCE login is described below; it is
   process-local and does not provide IdP-wide logout or distributed sessions.
   Never paste an IdP client secret or signing private key into Nexus WorkStudio.
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
3. Set **different random** `NEXUS_ALICE_APP_TOKEN` and `NEXUS_BOB_APP_TOKEN`
   variables (or corresponding generated names) in the operator's protected
   deployment environment. Each must be at least 16 characters because cloud
   startup still requires APP_TOKEN even in OIDC mode. The renderer writes only
   `${NEXUS_<ID>_APP_TOKEN:?...}` placeholders and never generates credentials.
   Never reuse them, distribute them to browser users, print resolved Compose
   configuration into logs, or use token auth as a fallback. Docker administrators
   can inspect container environment values. Compose ensures presence, not
   entropy/uniqueness; operator secret management must enforce these properties.
   Do not turn on `NEXUS_CLOUD_VERIFY`, whose legacy checker assumes token auth.
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
  Nexus WorkStudio release checks are independently satisfied.

## Optional enterprise sign-in (OIDC authorization code with PKCE)

The isolated gateway can optionally offer an enterprise login button instead of
requiring the user to copy an access token. This is disabled unless the gateway
is configured explicitly. The IdP must issue signed JWT access tokens for the
existing configured API audience, plus an ID token for the client ID, signed by
one of the gateway's configured issuer keys. Opaque access tokens are not
supported. Register the exact redirect URI `<public_origin>/auth/callback` and
allow PKCE S256. The authorization and token endpoints must use the issuer's
HTTPS origin; provision a confidential client secret separately.

Create a private `deploy/isolated/login.json` (ignored by Git) by replacing every
placeholder in this example:

```json
{
  "client_id": "REPLACE_WITH_IDP_CLIENT_ID",
  "client_secret_file": "/run/secrets/oidc-client-secret",
  "authorization_endpoint": "https://identity.example.com/REPLACE/authorize",
  "token_endpoint": "https://identity.example.com/REPLACE/token",
  "scope": "openid offline_access",
  "session_seconds": 28800
}
```

Create `deploy/isolated/secrets/oidc-client-secret.txt` with the confidential
client secret, owned/readable by the gateway's UID 10001 and inaccessible to
other users. Never commit the real login JSON, secret, access token or refresh
token. Add this private Compose override to the generated deployment:

```yaml
services:
  gateway:
    environment:
      NEXUS_WORKSPACE_OIDC_LOGIN_CONFIG: /run/nexus/login.json
    volumes:
      - ./login.json:/run/nexus/login.json:ro
    secrets:
      - source: oidc_client_secret
        target: oidc-client-secret
secrets:
  oidc_client_secret:
    file: ./secrets/oidc-client-secret.txt
```

Start Compose with both the generated file and the private override. Gateway
login sessions and refresh tokens live only in gateway process memory: a gateway
restart requires sign-in again; multiple gateway processes cannot share or
resume sessions. Use a single gateway instance for this mode until an external
secure shared session store and distributed refresh fencing are implemented.
When this SSO mode is enabled, API requests require the live HttpOnly gateway
cookie and a per-tab session generation; a standalone bearer token is not an
alternative. State-changing requests must be same-origin. Switching accounts or
signing out seals other tabs, clears their local Nexus WorkStudio state and aborts their
pending browser fetches; requests already forwarded to an external service
cannot be assumed cancelled. Sign-out revokes the gateway session but leaves the
now-invalid cookie in place so a delayed logout response cannot erase a newer
login; the next login replaces it. Sign-out does not perform IdP-wide logout or
revoke the IdP refresh token. Generated gateway commands disable Uvicorn access
logs; reverse proxies and observability systems must also suppress query strings
on `/auth/callback` so authorization codes never enter logs. Refresh support
requires a refresh token permitted by the IdP; without one, the user signs in
again after access token expiry. Configure per-site login throttling at ingress.

The included mock-IdP tests cover the authorization-code flow, PKCE, nonce/state
checks, user routing, cookie security, refresh and logout. They do not replace
end-to-end validation against your real IdP, proxy, TLS configuration or browser.
