# Desktop delivery layer

This layer addresses H1/H2 in `DOUBAO-ACCEPTANCE-v0.md`: a self-contained Windows
desktop entry and local startup that does not need a package manager or network.
The existing Atlas business workbench remains the primary UI. React Flow is an
additional read-only provenance view, opened from **工作设置与工具 → 数据来源与处理链**.
Local processing is part of the web workbench's **工作 → 本地电脑** context. It
uses the current project and an actual bound workspace; the old local-data URL
returns to the workbench. Cloud is visibly unavailable for this local-only stage.
The native menu bar is removed from both windows. Editing, zoom and full-screen
shortcuts remain window-local. Plain web clients hide the desktop-only lineage
action. Service restart is not a business capability: recovery retry is exposed
only on the portable startup failure page and rejects workbench-origin IPC.

## Source debugging

From an installed Python environment with the project dependencies and Git:

```bash
python run_debug.py --state-dir .atlas-debug --port 8000
```

This source entry detects Git with `shutil.which("git")` and sets an absolute
`ATLAS_GIT_BINARY` before starting the desktop backend. An explicit existing
`ATLAS_GIT_BINARY` takes precedence. Packaged desktop builds still require their
bundled Git; their binary lookup does not fall back to the system PATH.

The server binds to `127.0.0.1`. Credentials are saved in the selected state
directory's `credentials.json` and reused on restart; an explicit `APP_TOKEN`
environment value takes precedence and is not printed. The default state directory
is `.atlas-debug`; without `--state-dir`, existing `DATA_DIR` / `DATABASE_URL`
settings are preserved. Optional `--model-config /path/to/llm.json` selects the
model configuration. Project data is never cleared by this entry.

API polling allows 3000 requests per IP per 60 seconds. After a 429, the client
honors `Retry-After`, including focus / online events, and retries initial device
registration automatically. If the activity API reports a missing device record,
the client registers the same device ID again and retains the current draft and
pending message ID. Do not delete the database to repair this condition. The
updated frontend has a new cache version; restart the service and reload the page
after updating the source.

## Maintainer build

Only the build machine needs Node and npm:

```powershell
npm ci
npm run desktop:runtime
npm run desktop:test
npm run desktop:build
```

Before packaging, stage the verified Windows runtimes under `runtime/python`,
`runtime/git`, and (when supported) `runtime/opencode`. The Python host entry is
`portable_runtime.py`; its readiness/stop protocol is described below. The build
fails rather than emitting a bundle with a missing Python or Git runtime.
The builder refuses to overwrite an existing output. Use a new child directory
of this checkout's `dist` via `ATLAS_DESKTOP_OUTPUT` for later comparison builds.

For the optional offline image OCR capability, stage a separate `runtime/ocr`
from a build machine using the pinned, hash-verified local wheelhouse and the
verified CPython 3.12.10 embeddable archive:

```powershell
python packaging/prepare-ocr-runtime.py --wheelhouse <local-wheelhouse> --embedded-python-zip <verified-cpython-zip> --build-python <build-python-exe>
python packaging/refresh-runtime-manifest.py
python packaging/hash_ocr_inventory.py
```

The OCR lock and upstream wheel inventory are `requirements-ocr-windows.lock`
and `vendor/metadata/ocr-wheel-inventory.json`; delivered-file hashes are recorded
separately in `vendor/metadata/ocr-python-packages.json`. The assembly script
does not run at startup, invokes pip with `--no-index` and verifies three model
hashes. If the OCR worker is not included, OCR reports that it is unavailable;
the other local capabilities remain usable. A build and synthetic OCR test do
not establish accuracy for real financial documents or scanned PDF support.

The output folder is `dist/Atlas-DataBuddy-win32-x64`. Copy or zip the **whole
folder**. The only user entry is `Atlas DataBuddy.exe`; there is no first-run npm,
pip, WSL, Docker, download, global PATH configuration, or compiler step.

## Host contract

The desktop starts the absolute bundled executable:

```text
runtime/python/python.exe portable_runtime.py serve --state-dir USER_DATA --port 0
```

Host stdout emits one JSON readiness line:

```json
{"event":"ready","url":"http://127.0.0.1:PORT","token":"OWNER_WORKSPACE_TOKEN"}
```

This is an authenticated local process pipe, not a log or an external API. The
desktop never prints the readiness payload. The host must persist the selected
available port so the browser's origin-scoped drafts remain available on restart.
The desktop sends `stop` plus a newline on stdin for graceful shutdown. The host
owns child-process cleanup, including cleanup when the desktop process dies.
After the user selects a native directory and confirms the workspace switch,
the host also receives `--workspace-dir ABSOLUTE_SELECTED_DIRECTORY`. The desktop
persists this actual selection in its user-data directory, never in the package.
The backend scopes state and credentials to that directory; no projects are
silently reparented. Failed switches retain the previous selection and provide
retry/reselection from the startup screen. All restart/selection transitions
share one exclusive gate; lineage windows are destroyed before a scope changes.

## Boundaries

- Business data lives in Electron's Windows user-data directory, outside the
  executable folder. A separate plaintext diagnostic log contains no readiness
  token. Runtime-specific diagnostic logs are managed by the host.
- Browser windows use context isolation, no Node integration and renderer
  sandboxing. Foreign navigation, redirects and popups are blocked.
- The owner token is set only for the exact loopback origin. No operator token
  is exposed to the renderer and no credential enters a URL.
- The provenance window gets only read-only pipeline/lineage IPC, not arbitrary
  URLs, filesystem paths, credentials or backend commands.
- Packaging uses a source allowlist and never copies `llm.json`, `.env`, live
  data, source-control state, caches or application test fixtures. The output has
  a SHA-256 inventory; the synthetic offline acceptance script is included.
- Dependency resources are preserved even when their directories are named
  `tests` (for example dbt's production test-materialization macros). Before a
  build reports success, every runtime path, byte count and SHA-256 must match
  `vendor/metadata/runtime-manifest.json`; unlisted runtime files also fail.
- Passing the small desktop unit suite does not demonstrate Windows clean-machine,
  offline data-processing, model quality or complete business acceptance.
