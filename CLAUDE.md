# CLAUDE.md — AttDown4Acumatica

Project context for Claude Code sessions in this repository. Written for future-me.

## What this is

Generic bulk attachment downloader for Acumatica. Walks any entity that exposes a
`Files` sub-entity over the contract-based REST API and streams attachments to a
pluggable sink (local disk, S3, Azure Blob, GCS, SharePoint-stub).

Two runtimes over a shared core:

- **CLI** — `attdown run --config config.yaml` for headless / cron / ACA Jobs.
- **Web UI** — `attdown serve`, FastAPI + HTMX + Tailwind. OAuth-gated via
  Acumatica itself; no separate app password.

### Intended deployment: local-first

The web UI is designed to be run on a user's workstation, **not** on a shared
server. The README "This is a local web server" section is the product-facing
statement of this; concretely it means:

- OAuth `redirect_uri` is `http://localhost:8080/oauth/callback`, registered
  as a per-user Connected App in SM303010.
- Session/CSRF hardening (30-min idle, per-session token, run ownership)
  assume a single-operator box. Multi-user shared-host deployments would
  need SSO/WAF/mTLS in front, which is out of scope.
- Headless / multi-user automation uses the CLI with client-credentials, not
  the web UI.

## Architecture

```
src/attdown/
├── __init__.py
├── __main__.py          # `python -m attdown` entry
├── cli.py               # Typer CLI; loads .env; dispatches to serve/run/entities
├── config.py            # Pydantic models (AppConfig, JobConfig, MatchConfig, *Auth)
├── auth.py              # OAuth client-creds, auth-code+PKCE, session-cookie
├── client.py            # Acumatica REST client: paging, retry, swagger, downloads
├── match.py             # CSV reader + OData OR-chain builder with chunking
├── downloader.py        # Core loop: plan() → iter(tasks) → concurrent workers
├── sinks.py             # fsspec-backed writer (file/s3/az/gs + sharepoint stub)
├── checkpoint.py        # SQLite dedupe + resume + run history
└── web/
    ├── app.py           # FastAPI routes, OAuth PKCE flow, session middleware
    └── templates/*.html # Jinja2, HTMX-driven partials
```

### Data flow (a run)

```
/job/run
  → build JobConfig (entity, filter, match?, path)
  → snapshot the user's OAuth tokens into a background AuthorizationCodeAuth
  → spawn asyncio.Task: _go()
       Checkpoint.init()
       Sink.from_uri(output)
       AcumaticaClient(...)
       Downloader.run_job(job, force?, on_progress=emit)
           plan() yields FileTask per attachment (paged, filter chunks for match)
           per task → worker()
               _should_skip()? → emit skipped, return
               client.download(href) → bytes, server filename
               sink.write(rel_path, bytes)
               ck.mark_ok(...)
               emit done
  WebSocket /ws/run/{id} pushes Progress snapshots to the browser.
```

### Auth flows

| Flow | Used by | Notes |
|---|---|---|
| Client Credentials | CLI / cron | Service user; `config.yaml` carries `client_id`/`client_secret`. |
| Authorization Code + PKCE | Web UI | Each human logs into Acumatica; tokens stored in signed session cookie; per-job background task uses a snapshot with auto-refresh. |
| Session cookie | Legacy fallback | Only when Connected Apps aren't set up. |

Token endpoints: `{base}/identity/connect/{authorize,token,userinfo,revocation}`.

### Swagger / entity discovery

- `GET /entity/{endpoint}/swagger.json` → full OpenAPI doc.
- Parse top-level paths → entity names.
- Recursively walk `allOf` + `$ref` to flatten inherited properties (Acumatica's
  `Entity` base carries `Files`, `id`, `rowNumber`, `custom`, `note`).
- An entity "has files" iff the flattened property set contains a `Files` key.

### Match filter chunking

- Dedupe values, escape `'` → `''`, chunk at 50 per OData request.
- Emit `(Field eq 'A' or Field eq 'B' ...)` per chunk.
- If a base `filter` is set, each chunk becomes `(chunk) and (base)`.
- Downloader dedupes records across chunks via `seen_record_keys`.

### Web UI security posture (landed in v0.2.0)

These live in `src/attdown/web/app.py`. Don't regress them.

- **`SESSION_SECRET` is required.** No silent-random fallback — the server raises
  at import if it's unset. Rationale: a volatile secret rotates on every
  restart, invalidating live sessions and breaking the idle-timeout story.
- **30-minute sliding idle timeout.** Enforced two ways:
  1. `SessionMiddleware(..., max_age=IDLE_SECONDS)` — cookie re-issued on each
     response where the session is mutated.
  2. `require_auth` writes `last_activity` on every hit and clears the session
     if the delta exceeds `IDLE_SECONDS`. The WebSocket `/ws/run/{id}` mirrors
     this check so long-lived connections can't outlive the HTTP session.
- **Run ownership.** `RunState.user` is compared against `session["username"]`
  on `GET /run/{id}` (404 on mismatch), on `/ws/run/{id}` (close 4404), and
  when rendering the dashboard (runs list is filtered to the current user).
  Without this, any logged-in user could read any other user's filter, filenames,
  and Acumatica error bodies.
- **CSRF double-submit token.** Per-session token stored in `session["csrf"]`
  (minted by `_csrf_token()`), validated by the `require_csrf` dependency on:
  - `POST /job/run`
  - `POST /api/match/preview`
  - `POST /api/entities/refresh`
  - `POST /api/fs/mkdir` (split out of the old `GET /api/fs/list?mkdir=...`
    exactly so GET is never state-changing)

  The token is delivered two ways: the `<body hx-headers='{"X-CSRF-Token":...}'>`
  attribute in `base.html` makes every HTMX request carry it; regular form
  POSTs include a hidden `_csrf` input.
- **GET is safe.** Don't add filesystem mutation, Acumatica mutation, or any
  state change to a GET handler. If you need side-effects, use POST + CSRF.

## Acumatica quirks — already landed

Write them down when you hit them. These are the gotchas I keep forgetting.

- **`$expand=Files` (capital) query → response key is `files` (lowercase).** The
  iterator checks both. Breaking this would silently skip every attachment.
- **`allOf` + `$ref` inheritance in swagger** — direct `properties` on most
  entities is empty; the real fields live in a base schema. Recursive resolver
  required both for field discovery and for the Files heuristic.
- **OData v3, not v4.** `in`, `contains`, `toupper`, `tolower` all return 400.
  `substringof('needle', Field)` is the case-insensitive substring op.
- **File `href`** can be absolute or relative. httpx's base_url handles both.
- **Files are deduped in SQLite by `(entity, record_key, file_id)`.** Record key
  is the GUID `id` when Acumatica returns one, otherwise `ReferenceNbr`/`RefNbr`.
- **Response wrapping**: many fields arrive as `{"value": "X"}` rather than
  raw values. `_render_path` unwraps; the swagger schema's `StringValue`
  type label is the hint to the user that it's wrapped.
- **Acumatica login has a concurrent-session license cap.** Headless jobs
  with client-credentials bypass this via a dedicated proxy user; the UI's
  per-user tokens don't contend.

## Acumatica admin prerequisites

Users need to configure these *in Acumatica*:

1. **Integration → Connected Applications (SM303010)**
   - For the UI: one app, OAuth 2.0 Flow = **Authorization Code**, redirect URI
     matches `ACU_REDIRECT_URI` exactly.
   - For headless: a second app, Flow = **Client Credentials**, assign a
     proxy user with read access to every entity they'll export.
2. **Web Service Endpoints (SM207060)** — extend the `Default` endpoint to
   expose entities the stock endpoint omits (e.g. `ComplianceDocument`).
   See [endpoint-extensions/ComplianceDocument.md](endpoint-extensions/ComplianceDocument.md).
3. Grant the proxy user / signed-in user access to the screens behind those
   entities (e.g. CL301000 for compliance).

## Env vars

| Var | Purpose |
|---|---|
| `ACU_URL` | Tenant base URL, no trailing slash |
| `ACU_ENDPOINT` | Endpoint/version (default `Default/24.200.001`) |
| `ACU_CLIENT_ID` | Connected App ID |
| `ACU_CLIENT_SECRET` | Connected App secret (optional if PKCE public client) |
| `ACU_REDIRECT_URI` | OAuth callback, must match SM303010 |
| `SESSION_SECRET` | Signs the session cookie. **Required** — web server refuses to boot without it. Generate with `python -c 'import secrets; print(secrets.token_urlsafe(48))'` |
| `OUTPUT_URI` | Default download destination (overridden by job form) |
| `CHECKPOINT_URI` | SQLite path; defaults next to `file://` output or `~/.attdown/state.sqlite` |
| `ATTDOWN_FS_BROWSER` | `off` disables the folder picker (remote deploys) |
| `ATTDOWN_ENV_FILE` | Alternate path to `.env` |
| `HTTPS_ONLY` | `true` sets `Secure` on the session cookie |
| `UI_USER` / `UI_PASS` | Legacy Basic auth — unused since OAuth landed; do not re-introduce |

## Dev setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"

cp .env.example .env
# fill in ACU_URL, ACU_CLIENT_ID, ACU_REDIRECT_URI=http://localhost:8080/oauth/callback, SESSION_SECRET

attdown serve
# open http://localhost:8080 → redirects to Acumatica → sign in → dashboard
```

To iterate on templates without restarting:
```
uvicorn attdown.web.app:app --reload --host 127.0.0.1 --port 8080
```

### Testing

- Unit tests in `tests/` — pure functions only (sanitizer, path render, match
  builder, config env expansion).
- **Never hit live Acumatica in tests.** Anything that needs a response mocks
  the httpx client or constructs a dict that mimics the `$expand=Files` shape.
- Smoke tests preferred — the inline `python -c "..."` style in commits has
  been useful.

### Adding a new entity to the OSS default experience

1. Confirm it appears in `Default/{ver}/swagger.json` and has `Files` in its
   flattened schema.
2. If not, author an endpoint extension XML under `endpoint-extensions/`.
3. Add an example config in `examples/`.
4. Mention in README's "Entities" table.

### Adding a new sink

1. Implement in `sinks.py` under a new URL scheme.
2. If it needs creds, add them to `.env.example` under "Destination credentials".
3. For cloud sinks, prefer fsspec over a hand-rolled SDK if available.

## What NOT to change without thinking

- **Progress snapshot per emit** — the shared-reference race bit us. Keep
  `_emit()` constructing a new `Progress` per call.
- **Files casing** — `rec.get("files") or rec.get("Files")` — do not delete
  either half.
- **Checkpoint table schema** — changing it breaks resume for existing users.
- **OAuth session cookie key names** (`tokens`, `oauth_state`, `code_verifier`,
  `username`, `last_activity`, `csrf`) — any rename invalidates live sessions.
- **`SESSION_SECRET` required-at-import** — do not restore the
  `secrets.token_urlsafe(32)` silent-random fallback; it rotates on every
  restart and breaks the idle-timeout/CSRF stories.
- **Run ownership checks** in `run_page`, `run_ws`, and the dashboard's
  `my_runs` filter — removing any one of the three is a cross-user data leak.
- **CSRF dependency** on `/job/run`, `/api/match/preview`,
  `/api/entities/refresh`, `/api/fs/mkdir` — if you add a new state-changing
  POST, add `_csrf: None = Depends(require_csrf)` to it.

## Known limitations / future work

- SharePoint sink is stubbed. Graph API chunked upload needed.
- File System Access API browser-direct SPA mode considered and deferred —
  requires admin CORS config on Acumatica, and FS Access API isn't in Safari.
- No built-in run scheduling; users drive cron externally.
- Multi-column tuple matching not supported (OData v3 limitation).
- PyInstaller single-binary release not yet wired in CI.

## Commit style

Small, focused commits. Subject line imperative, under 70 chars. Reference the
issue number when applicable. Don't bundle refactors with feature work.

## License

Apache-2.0. Copyright 2026 Hall Boys Inc. New source files get the standard
Apache header (see existing files for the template).
