# Changelog

All notable changes to AttDown4Acumatica are tracked here. Format is loosely
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project follows
semver.

## [0.2.1] — 2026-04-22

Usability release for non-technical users on the prebuilt binary path.

### Added

- **`attdown gen-secret` subcommand.** Prints a single
  `SESSION_SECRET=<token>` line suitable for pasting into `.env`. Lets
  binary-only users generate a session secret without having Python
  installed.
- **Binary-first Quick Start** in the README. Per-OS step-by-step using
  the real release filenames (`attdown-macos-arm64`,
  `attdown-linux-x64`, `attdown-windows-x64.exe`). Source/Docker/headless
  paths are now clearly marked as "advanced".

### Fixed

- `docs/install.md` no longer lists `attdown-macos-x64` (dropped from
  the release matrix in v0.1.0's cycle when `macos-13` runners began
  being decommissioned). Intel Macs are pointed at Rosetta + the arm64
  binary.

### CI

- Bumped workflow actions to Node 24–compatible majors: `checkout@v6`,
  `setup-python@v6`, `upload-artifact@v7`, `download-artifact@v8`,
  `docker/build-push-action@v7`, `softprops/action-gh-release@v3`.

## [0.2.0] — 2026-04-22

Security release. The web UI is hardened for its intended local-first
deployment and documented as such in the README.

### Breaking

- **`SESSION_SECRET` is now required.** The web server refuses to start if
  it isn't set. Previously a silent random fallback rotated the signing key
  on every restart, which defeated the idle-timeout story and quietly
  invalidated live sessions. Generate one with
  `python -c 'import secrets; print(secrets.token_urlsafe(48))'` and pin it
  in `.env`.
- **`GET /api/fs/list?mkdir=...` is removed.** Folder creation moved to
  `POST /api/fs/mkdir` with CSRF. External scripts (not recommended, but
  known to exist) that poked `mkdir` on the GET need to be updated.

### Added

- **30-minute sliding idle timeout** on UI sessions. Enforced both by
  `SessionMiddleware(max_age=…)` (rolling cookie) and by a server-side
  `last_activity` check in `require_auth`. The `/ws/run/{id}` WebSocket
  mirrors the check so long-lived connections can't outlive the HTTP
  session.
- **Run ownership enforcement.** Dashboard runs list is scoped to the
  current user. `GET /run/{id}` returns 404 for non-owners; `/ws/run/{id}`
  closes with code 4404. Previously any logged-in user could read any
  other user's OData filter, filenames, and Acumatica error bodies.
- **CSRF double-submit token** on all state-changing POSTs: `/job/run`,
  `/api/match/preview`, `/api/entities/refresh`, `/api/fs/mkdir`. Token is
  minted per session, validated in constant time. Delivered via the
  `hx-headers` attribute on `<body>` for HTMX and a hidden `_csrf` input
  for regular form submits.
- **README "This is a local web server"** section at the top explaining
  the data-sensitivity, OAuth redirect, CORS, and concurrent-session-cap
  reasons behind the local-first design.
- **`tests/test_web_security.py`** — 9 tests covering SESSION_SECRET
  requirement, CSRF rejection/acceptance, idle timeout, cross-user run
  isolation, and the mkdir-move-to-POST.

### Fixed

- `SessionMiddleware` no longer falls back to a transient key; sessions
  survive process restarts when `SESSION_SECRET` is pinned.

## [0.1.0] — 2026-04-20

Initial release.

- Web UI (`attdown serve`) with OAuth Authorization Code + PKCE against
  Acumatica itself.
- Headless CLI (`attdown run --config ...`) with OAuth Client Credentials
  for cron / ACA Job / ECS.
- Entity auto-discovery via swagger `$expand=Files` walk.
- Filter via OData `$filter`, match-list CSV, or both (AND).
- Path templates with `|lower` / `|upper` / `|title` / `|slug` case filters.
- Sinks: local (`file://`), S3 (`s3://`), Azure Blob (`az://`), GCS
  (`gs://`), SharePoint stub (`sharepoint://`).
- SQLite checkpointing for resume and dedupe; auto-revalidation for local
  outputs.
- PyInstaller single-file binaries (linux-x64, macos-arm64, windows-x64)
  published to GitHub Releases.
- Docker image published to GHCR.

[0.2.1]: https://github.com/hallboys/AttDown4Acumatica/releases/tag/v0.2.1
[0.2.0]: https://github.com/hallboys/AttDown4Acumatica/releases/tag/v0.2.0
[0.1.0]: https://github.com/hallboys/AttDown4Acumatica/releases/tag/v0.1.0
