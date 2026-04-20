# Contributing to AttDown4Acumatica

Thanks for wanting to help. This is a small project built to solve a specific, annoying problem — keep that focus when proposing changes.

## TL;DR

1. Open an issue before starting non-trivial work, especially for new features. Avoids wasted time if the scope doesn't fit.
2. Fork → branch from `main` → PR.
3. New Python files get the SPDX header (see below).
4. Tests must pass (`pytest -q`) and not touch live Acumatica.
5. Keep commits small, imperative, single-concern.

## What's in scope

- Bug fixes.
- Support for more Acumatica entities, endpoint extensions, sink URI schemes.
- UX improvements on the web UI (keep it HTMX + server-rendered; no SPA pivot without discussion).
- Better docs, especially Acumatica-admin prerequisites.
- Platform-packaging improvements (notarization, Linux packages, Homebrew tap).

## Out of scope for v0.x

- Multi-tenant SaaS features.
- Replacing the core stack (FastAPI, HTMX, SQLite checkpoint).
- Server-side staging as the default privacy model — see the [architecture discussion in CLAUDE.md](CLAUDE.md#known-limitations--future-work).
- Features that require a specific cloud vendor (keep it generic).

If unsure, open an issue and ask.

## Developer setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]" pyinstaller pytest

cp .env.example .env
# fill in ACU_URL, ACU_CLIENT_ID, ACU_REDIRECT_URI, SESSION_SECRET

attdown serve
# browser opens on http://localhost:8080
```

### Running tests

```bash
pytest -q
```

All tests are pure functions — sanitizer, path render, match builder, config expansion. **Never hit live Acumatica from tests.** Mock at the httpx boundary (`httpx.Response(...)`, or construct dicts that mimic a `$expand=Files` response shape).

### Building the binary locally

```bash
pyinstaller --clean pyinstaller/attdown.spec
./dist/attdown --help
```

## Coding conventions

- **Python 3.11+** — use `from __future__ import annotations`, `|` unions, PEP 604.
- **Async everywhere in the download path** — mixing sync/async around the event loop has bitten us. If you must do sync I/O, wrap with `asyncio.to_thread`.
- **No global mutable state** except the `STATE` dict in `web/app.py`, and even that is being chipped away.
- **Pydantic for anything that crosses a process boundary** (config, form submissions, session cookies).
- **Type hints required on public functions.** Mypy is not currently enforced; don't fight it.

### License header

Every new Python file starts with:

```python
# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
```

Immediately after the shebang/encoding if any, before the module docstring.

### Acumatica quirks to keep in mind

See [CLAUDE.md](CLAUDE.md#acumatica-quirks-already-landed) for the running list. The main ones:

- OData **v3**, not v4. No `in`, `contains`, `toupper`.
- `$expand=Files` capital → response `files` lowercase. Check both.
- Swagger `allOf` inheritance — use the existing resolver.

## Pull requests

- Branch from `main`. Keep PRs scoped to one concern.
- Reference the issue in the PR description (`Fixes #123`).
- PRs should be green on CI before a reviewer is tagged.
- **Don't bundle refactors with feature work** — separate PRs are easier to review and revert.
- Squash merges are the default. Write the merge commit message yourself; don't accept GitHub's default "PR title + bullet list."

### PR checklist

- [ ] Tests pass locally (`pytest -q`)
- [ ] New files have the SPDX header
- [ ] README / CLAUDE.md / install.md updated if user-visible behavior changed
- [ ] No `.env`, secrets, or SQLite files committed
- [ ] No live-Acumatica calls in tests

## Reporting bugs

Open a GitHub Issue using the **Bug report** template. Include:

- Version (`attdown --version` if you're on a binary; commit SHA if on source).
- OS and Python version (if running from source).
- Acumatica version (seen on the About dialog).
- Exact steps to reproduce.
- What you expected vs. what happened.
- Relevant log lines — **scrub tenant URLs and user IDs** before pasting.

## Reporting security issues

**Do not** file public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible-disclosure process.

## Code of Conduct

By participating in this project you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree your contributions will be licensed under Apache-2.0 (see [LICENSE](LICENSE)). Don't submit code you don't have the right to contribute.
