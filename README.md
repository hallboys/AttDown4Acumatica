# AttDown4Acumatica

**Bulk attachment downloader for Acumatica ERP — any entity, any filter, any destination.**

Acumatica's UI lets you view attachments one record at a time. Auditors, compliance teams, and anyone doing an annual document pull want them in bulk. AttDown4Acumatica walks any contract-based REST entity that exposes `Files`, pages through matching records, and streams the attachments to local disk, S3, Azure Blob, GCS, or (stubbed) SharePoint.

> Copyright 2026 Hall Boys Inc. Apache-2.0 licensed. Contributions welcome.

## Features

- **Works on any entity with a `Files` sub-entity** — Bills, Invoices, Sales Orders, Purchase Orders, Projects, Cases, Payments, Subcontracts, Compliance Documents (via endpoint extension), ...
- **Two interfaces, one codebase**
  - **Web UI** — point-and-click with OAuth-gated login, live progress, filter preview.
  - **CLI** — `attdown run --config config.yaml` for cron / Azure Container Apps Jobs / ECS.
- **Filter flexibly**
  - OData `$filter` (dates, status, substring, etc.).
  - Match list — paste or upload a CSV of IDs (e.g. VendorIDs).
  - Combine both with `AND` for narrow targeting.
- **Auth via Acumatica itself** — OAuth 2.0 Authorization Code + PKCE for the UI; Client Credentials for headless jobs. No separate app password.
- **Pluggable destinations** — one URI scheme per destination, backed by [fsspec](https://filesystem-spec.readthedocs.io/).
- **Resumable** — SQLite checkpoint dedupes across runs; auto-revalidates file existence for local outputs; "Force re-download" overrides on demand.
- **Path templates** — `{entity}/{Vendor}/{ReferenceNbr}/{filename}` with case filters: `|lower`, `|upper`, `|title`, `|slug`.
- **Concurrent** — parallel downloads with a configurable semaphore; throttles to your Acumatica concurrent-request license.

## Quick start

### Prereqs (on your tenant)

1. In Acumatica: **Integration → Connected Applications (SM303010)** →
   - Create a Connected Application.
   - **Flow: Authorization Code.**
   - **Redirect URI:** `http://localhost:8080/oauth/callback` (or whatever you plan to serve on).
   - Save the client ID (and secret if you set one).
2. **Extend the endpoint** if you need entities not in stock Default (e.g. `ComplianceDocument`). See [endpoint-extensions/ComplianceDocument.md](endpoint-extensions/ComplianceDocument.md). For AP/AR/Projects/Sales/etc., stock Default works.

### Run locally

```bash
git clone https://github.com/hallboys/attdown4acumatica.git
cd attdown4acumatica

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"

cp .env.example .env
# fill in:
#   ACU_URL=https://your-tenant.acumatica.com
#   ACU_CLIENT_ID=...
#   ACU_CLIENT_SECRET=...   (optional if your Connected App is a PKCE public client)
#   ACU_REDIRECT_URI=http://localhost:8080/oauth/callback
#   SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')

attdown serve
# open http://localhost:8080 → log in through Acumatica → done.
```

### Run via Docker (no Python on host)

```bash
cp .env.example .env                          # edit as above
docker compose up
# UI on http://localhost:8080; downloads land in ./data
```

### Run headless (cron / ACA Job / ECS)

Use OAuth **Client Credentials** in `config.yaml` — a proxy-user service identity, no human in the loop:

```yaml
acumatica:
  base_url: https://your-tenant.acumatica.com
  endpoint: Default/24.200.001
  auth:
    type: oauth_client_credentials
    client_id: ${ACU_CLIENT_ID}
    client_secret: ${ACU_CLIENT_SECRET}

output: ${OUTPUT_URI}       # file://, s3://, az://, gs://
concurrency: 4

jobs:
  - entity: Bill
    filter: "Date ge datetimeoffset'2026-01-01T00:00:00Z' and Status ne 'Draft'"
    path: "ap/{Vendor}/{ReferenceNbr}/{filename}"
```

```bash
attdown run --config config.yaml
attdown run --config config.yaml --dry-run
attdown run --config config.yaml --job Bill
```

## CLI

```
attdown serve [--host 0.0.0.0] [--port 8080]   # Web UI
attdown run --config config.yaml                # Run all jobs
attdown run --config config.yaml --dry-run      # Plan, don't download
attdown run --config config.yaml --job Bill     # Only one job
attdown entities --config config.yaml           # List entities with Files
```

## Destinations (output URI)

| Scheme | Example | Needs |
|---|---|---|
| Local | `file:///Users/you/Downloads/acu` | — |
| AWS S3 | `s3://bucket/prefix` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (or instance profile) |
| Azure Blob | `az://container/prefix` | `AZURE_STORAGE_CONNECTION_STRING` (or managed identity) |
| Google Cloud Storage | `gs://bucket/prefix` | `GOOGLE_APPLICATION_CREDENTIALS` |
| SharePoint | `sharepoint://site/library/folder` | Stubbed — Graph API sink planned |

> `file://` takes **three slashes**: `file://` + absolute path starting with `/`.
> For Docker, mount a host folder to `/data` and set `OUTPUT_URI=file:///data`.

## Filters

Two ways to select records, usable together:

### OData `$filter`
Standard Acumatica OData v3 syntax:
```
Status eq 'Open'
Date ge datetimeoffset'2026-01-01T00:00:00Z'
substringof('ACME', Vendor)
Project eq 'JOB123' and Status ne 'Void'
```
Do **not** use `in`, `contains`, `toupper`, `tolower` — those are v4 and return 400.

### Match list (CSV / paste)
Paste IDs or upload a CSV. Each chunk becomes `(Field eq 'a' or Field eq 'b' ...) and (your OData filter)`.

YAML form:
```yaml
- entity: Bill
  filter: "Date ge datetimeoffset'2026-01-01T00:00:00Z'"
  match:
    field: Vendor
    from_csv: vendors.csv     # relative to config file
    column: VendorID          # or integer index
    chunk_size: 50
  path: ap/{Vendor}/{ReferenceNbr}/{filename}
```

## Path templates

`{FieldName}` placeholders for any top-level field returned by Acumatica. Plus `{entity}` and `{filename}`.

Case filters:
- `{VendorName|lower}`
- `{VendorName|upper}`
- `{VendorName|title}`
- `{VendorName|slug}` — lowercase, spaces → `-`

The sanitizer preserves spaces, parens, `&`, `'`, `,` — only characters Windows or POSIX actually reject (`< > : " | ? *` and control chars) become `_`.

## Checkpointing

Every successful download is recorded in a SQLite DB:
- Co-located with local output: `<output>/.attdown-state.sqlite`
- Cloud output: `~/.attdown/state.sqlite`
- Override with `CHECKPOINT_URI=file:///abs/path.sqlite`

Re-runs skip files already marked `ok`. For local sinks, the skip is automatically revoked if the file has been deleted on disk. The run page shows the checkpoint path; delete it for a clean slate, or tick **Force re-download** on the job form to bypass for one run.

## Deployments

| Mode | Where it runs | Folder picker | Notes |
|---|---|---|---|
| Local Python | Your laptop | Yes | `attdown serve` — fastest dev loop |
| Local Docker | Your laptop | Yes (inside container) | `docker compose up` |
| Azure Container Apps Job | Azure | Set `ATTDOWN_FS_BROWSER=off` | Use a cloud URI for output |
| ECS / k8s / VM | Anywhere | Set `ATTDOWN_FS_BROWSER=off` | Use a cloud URI for output |

For remote deployments, disable the filesystem browser with `ATTDOWN_FS_BROWSER=off`; it's useless on ephemeral container storage and misleads users into picking a doomed path.

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `ACU_URL` | Tenant base URL | — (required) |
| `ACU_ENDPOINT` | Contract endpoint + version | `Default/24.200.001` |
| `ACU_CLIENT_ID` | Connected App ID | — (required for UI) |
| `ACU_CLIENT_SECRET` | Connected App secret | — |
| `ACU_REDIRECT_URI` | OAuth callback | `http://localhost:8080/oauth/callback` |
| `SESSION_SECRET` | Signs the UI session cookie | random (regenerated each start — set this!) |
| `OUTPUT_URI` | Default download destination | `file://~/Downloads/attdown` |
| `CHECKPOINT_URI` | SQLite path | auto |
| `ATTDOWN_FS_BROWSER` | `off` hides folder picker | `on` |
| `ATTDOWN_ENV_FILE` | Alternate `.env` path | `./.env` |
| `HTTPS_ONLY` | `Secure` flag on session cookie | `false` |

## Contributing

1. Fork, branch, PR.
2. New Python files get an SPDX Apache-2.0 header:
   ```python
   # Copyright 2026 Hall Boys Inc
   # SPDX-License-Identifier: Apache-2.0
   ```
3. Don't hit live Acumatica in tests — mock responses at the httpx boundary.
4. Keep commits small and imperative ("Add X", "Fix Y"). Bundle no refactors with features.
5. Run smoke tests: `pytest -q`.

See [CLAUDE.md](CLAUDE.md) for architecture notes, Acumatica quirks, and the list of things not to casually change.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. Copyright 2026 Hall Boys Inc.
