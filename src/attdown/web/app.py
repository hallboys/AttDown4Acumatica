# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""FastAPI web UI for AttDown4Acumatica.

Authentication: OAuth 2.0 Authorization Code + PKCE against Acumatica.
Logging in to Acumatica *is* logging in to the tool. No separate app password.

Env vars required:
    ACU_URL              https://tenant.acumatica.com
    ACU_CLIENT_ID        from SM303010 (Connected Applications)
    ACU_CLIENT_SECRET    optional (PKCE works without it for public clients)
    ACU_REDIRECT_URI     e.g. http://localhost:8080/oauth/callback
    ACU_ENDPOINT         default "Default/24.200.001"
    SESSION_SECRET       any long random string; signs the session cookie

For headless/cron runs, use the CLI (attdown run) with client-credentials
auth configured in config.yaml — that path doesn't touch this module.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from attdown.auth import AuthorizationCodeAuth, _TokenBag
from attdown.checkpoint import Checkpoint
from attdown.client import AcumaticaClient
from attdown.config import AcumaticaConfig, JobConfig, MatchConfig, OAuthAuthCode
from attdown.match import build_match_filters, dedupe_keep_order
from attdown.downloader import Downloader, Progress
from attdown.sinks import Sink


BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Idle timeout for authenticated sessions. Cookie re-issued each request so the
# window is sliding, and require_auth also enforces it server-side against the
# stored last_activity timestamp.
IDLE_SECONDS = 30 * 60

_SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not _SESSION_SECRET:
    raise RuntimeError(
        "SESSION_SECRET must be set. Generate one with: "
        "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
    )

app = FastAPI(title="AttDown4Acumatica")
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    same_site="lax",
    https_only=os.environ.get("HTTPS_ONLY", "false").lower() == "true",
    max_age=IDLE_SECONDS,
)


# ---- configuration from env ----

def _cfg() -> AcumaticaConfig:
    url = os.environ.get("ACU_URL")
    cid = os.environ.get("ACU_CLIENT_ID")
    redirect = os.environ.get("ACU_REDIRECT_URI", "http://localhost:8080/oauth/callback")
    missing = [n for n, v in [("ACU_URL", url), ("ACU_CLIENT_ID", cid)] if not v]
    if missing:
        raise RuntimeError(
            f"Missing env var(s): {', '.join(missing)}. "
            f"Put them in a .env file in the working directory, or export them in your shell, "
            f"or set ATTDOWN_ENV_FILE=/path/to/.env. "
            f"Configure the Connected Application in Acumatica → SM303010."
        )
    return AcumaticaConfig(
        base_url=url,
        endpoint=os.environ.get("ACU_ENDPOINT", "Default/24.200.001"),
        auth=OAuthAuthCode(
            type="oauth_auth_code",
            client_id=cid,
            client_secret=os.environ.get("ACU_CLIENT_SECRET"),
            redirect_uri=redirect,
        ),
    )


# ---- auth plumbing ----

class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def _not_authenticated_handler(request: Request, _exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse("/oauth/login", status_code=303)


def _auther_from_session(request: Request) -> AuthorizationCodeAuth:
    """Reconstruct an Authenticator pre-loaded with this user's tokens."""
    tok = request.session.get("tokens")
    if not tok:
        raise NotAuthenticated()
    cfg = _cfg().auth  # type: ignore[assignment]
    base = _cfg().base_url
    auther = AuthorizationCodeAuth(
        base_url=base,
        client_id=cfg.client_id,  # type: ignore[attr-defined]
        client_secret=cfg.client_secret,  # type: ignore[attr-defined]
        redirect_uri=cfg.redirect_uri,  # type: ignore[attr-defined]
        scope=cfg.scope,  # type: ignore[attr-defined]
    )
    auther.tokens = _TokenBag(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token"),
        expires_at=float(tok.get("expires_at", 0)),
    )
    return auther


def _csrf_token(request: Request) -> str:
    """Fetch (or mint) the per-session CSRF token. Stable for the session's life."""
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def require_csrf(request: Request) -> None:
    """Validate a double-submit CSRF token on state-changing requests.
    Accepts the token via the `X-CSRF-Token` header (HTMX/fetch) or `_csrf`
    form field. Uses a constant-time compare against the session-bound token.
    """
    expected = request.session.get("csrf")
    header = request.headers.get("x-csrf-token", "")
    form_token = ""
    if not header:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
            form = await request.form()
            form_token = str(form.get("_csrf", ""))
    presented = header or form_token
    if not expected or not presented or not secrets.compare_digest(expected, presented):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")


async def require_auth(request: Request) -> AuthorizationCodeAuth:
    # Enforce the 30-minute sliding idle window. The SessionMiddleware max_age
    # re-issues the cookie on each response where the session is mutated, and
    # touching last_activity here guarantees that mutation on every auth'd hit.
    now = time.time()
    last = request.session.get("last_activity")
    if last is not None and now - float(last) > IDLE_SECONDS:
        request.session.clear()
        raise NotAuthenticated()
    request.session["last_activity"] = now

    auther = _auther_from_session(request)
    # best-effort refresh if needed, so dependent handlers always have a fresh token
    if auther.tokens.is_expiring(slack=30):
        try:
            async with httpx.AsyncClient() as http:
                await auther._refresh(http)
        except Exception:
            # couldn't refresh; force re-auth
            request.session.pop("tokens", None)
            raise NotAuthenticated() from None
        request.session["tokens"] = {
            "access_token": auther.tokens.access_token,
            "refresh_token": auther.tokens.refresh_token,
            "expires_at": auther.tokens.expires_at,
        }
    return auther


# ---- in-memory state ----

@dataclass
class RunState:
    id: str
    job: JobConfig
    user: str
    output: str = ""
    checkpoint: str = ""
    progress: Progress = field(default_factory=Progress)
    status: str = "running"
    error: str | None = None
    started: float = field(default_factory=time.time)
    subscribers: list[asyncio.Queue] = field(default_factory=list)


STATE: dict[str, Any] = {
    "entities": [],           # list[str], shared across users; refreshed on demand
    "swagger": None,          # cached OpenAPI doc for the endpoint
    "runs": {},               # dict[run_id, RunState]
}


def _default_checkpoint_uri(output_uri: str) -> str:
    """Pick a sane SQLite checkpoint location. Follows CHECKPOINT_URI if set,
    otherwise co-locates with a local output or falls back to $HOME/.attdown."""
    override = os.environ.get("CHECKPOINT_URI")
    if override:
        return override
    if output_uri.startswith("file://"):
        from urllib.parse import urlparse
        path = urlparse(output_uri).path or "/data"
        return f"file://{path.rstrip('/')}/.attdown-state.sqlite"
    home = Path.home() / ".attdown"
    return f"file://{home}/state.sqlite"


async def _ensure_swagger(auther: AuthorizationCodeAuth) -> dict[str, Any]:
    """Fetch and cache the OpenAPI doc for the configured endpoint."""
    if STATE["swagger"]:
        return STATE["swagger"]
    cfg = _cfg()
    async with AcumaticaClient(cfg.base_url, cfg.endpoint, auther) as client:
        STATE["swagger"] = await client.swagger()
    return STATE["swagger"]


def _resolve_properties(
    defs: dict[str, Any], schema: dict[str, Any], seen: set[str] | None = None
) -> dict[str, Any]:
    """Flatten properties across allOf / $ref chains (Acumatica swagger inherits fields)."""
    seen = seen if seen is not None else set()
    out: dict[str, Any] = {}
    if "$ref" in schema:
        ref = schema["$ref"].rsplit("/", 1)[-1]
        if ref not in seen and ref in defs:
            seen.add(ref)
            out.update(_resolve_properties(defs, defs[ref], seen))
        return out
    for sub in schema.get("allOf", []) or []:
        out.update(_resolve_properties(defs, sub, seen))
    out.update(schema.get("properties") or {})
    return out


def _field_rows(swagger: dict[str, Any], entity: str) -> list[dict[str, str]]:
    """Return [{name, type}, ...] for the entity's fields, sorted."""
    defs: dict[str, Any] = (
        swagger.get("definitions")
        or swagger.get("components", {}).get("schemas", {})
        or {}
    )
    schema = defs.get(entity) or {}
    props = _resolve_properties(defs, schema)
    return sorted(
        ({"name": n, "type": _pretty_type(p)} for n, p in props.items()),
        key=lambda r: r["name"].lower(),
    )


def _pretty_type(prop: dict[str, Any]) -> str:
    """Render a human-friendly type string from an OpenAPI property schema."""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    if "allOf" in prop and prop["allOf"]:
        ref = prop["allOf"][0].get("$ref", "")
        return ref.rsplit("/", 1)[-1] or "obj"
    t = prop.get("type") or "any"
    fmt = prop.get("format")
    if t == "array":
        inner = prop.get("items") or {}
        return f"[{_pretty_type(inner)}]"
    if fmt:
        return f"{t}<{fmt}>"
    return t


# ---- OAuth flow ----

@app.get("/oauth/login")
async def oauth_login(request: Request) -> RedirectResponse:
    cfg = _cfg()
    auther = AuthorizationCodeAuth(
        base_url=cfg.base_url,
        client_id=cfg.auth.client_id,  # type: ignore[union-attr]
        client_secret=cfg.auth.client_secret,  # type: ignore[union-attr]
        redirect_uri=cfg.auth.redirect_uri,  # type: ignore[union-attr]
        scope=cfg.auth.scope,  # type: ignore[union-attr]
    )
    state = secrets.token_urlsafe(24)
    verifier = auther.new_verifier()
    request.session["oauth_state"] = state
    request.session["code_verifier"] = verifier
    return RedirectResponse(auther.authorize_url(state=state, code_verifier=verifier))


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "") -> Any:
    expected_state = request.session.pop("oauth_state", None)
    verifier = request.session.pop("code_verifier", None)
    if not code or not state or state != expected_state or not verifier:
        raise HTTPException(400, detail="Invalid OAuth callback.")

    cfg = _cfg()
    auther = AuthorizationCodeAuth(
        base_url=cfg.base_url,
        client_id=cfg.auth.client_id,  # type: ignore[union-attr]
        client_secret=cfg.auth.client_secret,  # type: ignore[union-attr]
        redirect_uri=cfg.auth.redirect_uri,  # type: ignore[union-attr]
        scope=cfg.auth.scope,  # type: ignore[union-attr]
    )
    try:
        await auther.exchange(code=code, code_verifier=verifier)
    except Exception as e:
        raise HTTPException(400, detail=f"OAuth token exchange failed: {e}") from e

    request.session["tokens"] = {
        "access_token": auther.tokens.access_token,
        "refresh_token": auther.tokens.refresh_token,
        "expires_at": auther.tokens.expires_at,
    }

    # Best-effort: fetch the human name for display.
    username = "user"
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"{cfg.base_url}/identity/connect/userinfo",
                headers={"Authorization": f"Bearer {auther.tokens.access_token}"},
                timeout=10.0,
            )
            if r.status_code == 200:
                info = r.json()
                username = info.get("name") or info.get("preferred_username") or info.get("sub") or username
    except Exception:
        pass
    request.session["username"] = username

    return RedirectResponse("/", status_code=303)


@app.get("/oauth/logout")
async def oauth_logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---- app routes ----

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, auther: AuthorizationCodeAuth = Depends(require_auth)) -> HTMLResponse:
    discovery_error: str | None = None
    if not STATE["entities"]:
        try:
            cfg = _cfg()
            async with AcumaticaClient(cfg.base_url, cfg.endpoint, auther) as client:
                STATE["entities"] = await client.entities_with_files()
        except Exception as e:
            discovery_error = f"{type(e).__name__}: {e}"
    username = request.session.get("username", "user")
    my_runs = {rid: r for rid, r in STATE["runs"].items() if r.user == username}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "entities": STATE["entities"],
            "runs": my_runs,
            "username": username,
            "discovery_error": discovery_error,
            "endpoint": _cfg().endpoint,
            "csrf_token": _csrf_token(request),
        },
    )


@app.get("/job/new", response_class=HTMLResponse)
async def job_form(request: Request, _: Any = Depends(require_auth)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "job.html",
        {
            "entities": STATE["entities"],
            "default_output": os.environ.get(
                "OUTPUT_URI",
                f"file://{_default_download_dir()}",
            ),
            "username": request.session.get("username", "user"),
            "fs_browser": _fs_browser_enabled(),
            "csrf_token": _csrf_token(request),
        },
    )


def _parse_values_blob(blob: str) -> list[str]:
    """Split a free-form textarea into keys. Accepts newline, comma, tab, semicolon separators."""
    if not blob:
        return []
    import re as _re
    parts = _re.split(r"[\n,;\t]+", blob)
    return [p.strip() for p in parts if p and p.strip()]


@app.post("/job/run")
async def job_run(
    request: Request,
    entity: str = Form(...),
    filter: str = Form(""),
    path: str = Form("{entity}/{id}/{filename}"),
    output: str = Form("file:///data"),
    concurrency: int = Form(4),
    dry_run: bool = Form(False),
    match_field: str = Form(""),
    match_values: str = Form(""),
    force: bool = Form(False),
    auther: AuthorizationCodeAuth = Depends(require_auth),
    _csrf: None = Depends(require_csrf),
) -> Any:
    cfg = _cfg()

    # OData filter and Match list are independent. Either or both may be set.
    # If both are set, the downloader AND's them together.
    match_cfg: MatchConfig | None = None
    values = _parse_values_blob(match_values)
    if match_field and values:
        match_cfg = MatchConfig(field=match_field, values=values)
    elif match_field and not values:
        raise HTTPException(400, detail="Match field set without any values.")
    elif values and not match_field:
        raise HTTPException(400, detail="Match values provided without a field.")

    if not match_cfg and not filter:
        raise HTTPException(400, detail="Provide an OData filter, a match list, or both.")

    job = JobConfig(
        entity=entity,
        filter=filter or None,
        match=match_cfg,
        path=path,
    )
    run_id = uuid.uuid4().hex[:12]
    state = RunState(
        id=run_id, job=job, user=request.session.get("username", "user"),
        output=output, checkpoint=_default_checkpoint_uri(output),
    )
    STATE["runs"][run_id] = state

    # Capture this user's tokens for the background task; AuthorizationCodeAuth will refresh as needed.
    snap = AuthorizationCodeAuth(
        base_url=auther.base_url,
        client_id=auther.client_id,
        client_secret=auther.client_secret,
        redirect_uri=auther.redirect_uri,
        scope=auther.scope,
    )
    snap.tokens = _TokenBag(
        access_token=auther.tokens.access_token,
        refresh_token=auther.tokens.refresh_token,
        expires_at=auther.tokens.expires_at,
    )

    async def _go() -> None:
        try:
            ck = Checkpoint(_default_checkpoint_uri(output))
            await ck.init()
            sink = Sink.from_uri(output)
            async with AcumaticaClient(
                cfg.base_url, cfg.endpoint, snap, concurrency=concurrency
            ) as client:
                ck_run = await ck.start_run(config_text=str(job.model_dump()))
                dl = Downloader(client, sink, ck, ck_run, concurrency=concurrency)

                def emit(p: Progress) -> None:
                    state.progress = p
                    for q in list(state.subscribers):
                        try:
                            q.put_nowait(p)
                        except Exception:
                            pass

                await dl.run_job(job, dry_run=dry_run, force=force, on_progress=emit)
                await ck.finish_run(ck_run, "ok")
                state.status = "ok"
        except Exception as e:
            state.status = "error"
            state.error = str(e)
        finally:
            for q in state.subscribers:
                q.put_nowait(None)

    asyncio.create_task(_go())
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str, _: Any = Depends(require_auth)) -> HTMLResponse:
    state = STATE["runs"].get(run_id)
    username = request.session.get("username", "user")
    # Hide existence from users who don't own the run — same 404 as a missing run.
    if not state or state.user != username:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run_id": run_id,
            "state": state,
            "username": username,
        },
    )


@app.websocket("/ws/run/{run_id}")
async def run_ws(ws: WebSocket, run_id: str) -> None:
    # Gate on a logged-in session AND on ownership of this specific run. The
    # HTTP idle check runs in require_auth; mirror the age check here so a long-
    # lived WS can't outlive the user's session.
    sess = ws.session
    if not sess.get("tokens"):
        await ws.close(code=4401)
        return
    last = sess.get("last_activity")
    if last is not None and time.time() - float(last) > IDLE_SECONDS:
        await ws.close(code=4401)
        return
    username = sess.get("username", "user")
    state = STATE["runs"].get(run_id)
    if not state or state.user != username:
        await ws.close(code=4404)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    state.subscribers.append(q)
    try:
        await ws.send_json({
            "progress": state.progress.__dict__,
            "status": state.status,
            "error": state.error,
        })
        while True:
            item = await q.get()
            if item is None:
                await ws.send_json({
                    "progress": state.progress.__dict__,
                    "status": state.status,
                    "error": state.error,
                })
                break
            await ws.send_json({
                "progress": item.__dict__,
                "status": state.status,
                "error": state.error,
            })
    except WebSocketDisconnect:
        pass
    finally:
        if q in state.subscribers:
            state.subscribers.remove(q)


@app.post("/api/match/preview", response_class=HTMLResponse)
async def api_match_preview(
    request: Request,
    match_field: str = Form(""),
    match_values: str = Form(""),
    filter: str = Form(""),
    _: Any = Depends(require_auth),
    _csrf: None = Depends(require_csrf),
) -> HTMLResponse:
    """Render a preview of what a match-list filter would look like."""
    raw = _parse_values_blob(match_values)
    unique = dedupe_keep_order(raw)
    chunk_size = 50
    chunks = build_match_filters(
        match_field, unique, chunk_size=chunk_size, extra_filter=(filter or None)
    ) if match_field else []
    sample = unique[:5]
    sample_filter = chunks[0] if chunks else ""
    if len(sample_filter) > 400:
        sample_filter = sample_filter[:400] + " …"
    return templates.TemplateResponse(
        request,
        "_match_preview.html",
        {
            "total": len(raw),
            "unique_count": len(unique),
            "chunk_count": len(chunks),
            "chunk_size": chunk_size,
            "sample": sample,
            "sample_filter": sample_filter,
            "field": match_field,
        },
    )


@app.post("/api/entities/refresh")
async def api_entities_refresh(
    auther: AuthorizationCodeAuth = Depends(require_auth),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    cfg = _cfg()
    async with AcumaticaClient(cfg.base_url, cfg.endpoint, auther) as client:
        STATE["swagger"] = await client.swagger()
        STATE["entities"] = await client.entities_with_files()
    return {"entities": STATE["entities"]}


# ---- local filesystem browser (for the folder picker) ----

def _default_download_dir() -> Path:
    """Pick a sensible default output folder on the server host.
    Order: ~/Downloads/attdown → ~/attdown-downloads → $CWD/downloads."""
    home = Path.home()
    candidates = [home / "Downloads" / "attdown", home / "attdown-downloads"]
    for c in candidates:
        if c.parent.is_dir():  # e.g. ~/Downloads exists on macOS/Windows
            return c
    return Path.cwd() / "downloads"


def _fallback_browse_dir() -> Path:
    """Where the folder picker should open when the requested path is missing."""
    for c in [Path.home() / "Downloads", Path.home()]:
        if c.is_dir():
            return c
    return Path("/")


def _fs_browser_enabled() -> bool:
    """Folder picker makes sense only where the container FS is persistent and
    matches something the user cares about (local dev, volume-mounted Docker).
    Disabled in cloud deployments: set ATTDOWN_FS_BROWSER=off."""
    return os.environ.get("ATTDOWN_FS_BROWSER", "on").lower() not in {"off", "false", "0", "no"}


def _render_fs_list(request: Request, target: Path, error: str | None = None) -> HTMLResponse:
    if not target.is_dir():
        return HTMLResponse(
            f'<div class="text-red-700 text-xs">Not a directory: {target}</div>'
        )
    entries: list[Path] = []
    try:
        for e in target.iterdir():
            if e.is_dir() and not e.name.startswith("."):
                entries.append(e)
    except PermissionError:
        return HTMLResponse(
            f'<div class="text-amber-700 text-xs">Permission denied: {target}</div>'
        )
    entries.sort(key=lambda e: e.name.lower())
    parent = str(target.parent) if target.parent != target else None
    return templates.TemplateResponse(
        request, "_fs_list.html",
        {"path": str(target), "entries": entries, "parent": parent, "error": error,
         "csrf_token": _csrf_token(request)},
    )


def _resolve_browse_target(path: str) -> Path:
    target = Path(path).expanduser() if path else _fallback_browse_dir()
    try:
        target = target.resolve()
    except Exception:
        target = _fallback_browse_dir()
    if not target.is_dir():
        target = _fallback_browse_dir()
    return target


@app.get("/api/fs/list", response_class=HTMLResponse)
async def api_fs_list(
    request: Request,
    path: str = "",
    _: Any = Depends(require_auth),
) -> HTMLResponse:
    """Return a partial that lists subdirectories of `path` for the folder picker."""
    if not _fs_browser_enabled():
        return HTMLResponse(
            '<div class="text-amber-700 text-xs">'
            'Folder browser disabled on this deployment. Use a cloud URI '
            '(<code>s3://</code>, <code>az://</code>, <code>gs://</code>, '
            '<code>sharepoint://</code>) instead.'
            '</div>',
            status_code=403,
        )
    return _render_fs_list(request, _resolve_browse_target(path))


@app.post("/api/fs/mkdir", response_class=HTMLResponse)
async def api_fs_mkdir(
    request: Request,
    path: str = Form(""),
    mkdir: str = Form(""),
    _: Any = Depends(require_auth),
    _csrf: None = Depends(require_csrf),
) -> HTMLResponse:
    """Create a subfolder under `path` and re-render the folder list.
    State-changing — requires CSRF. Kept separate from /api/fs/list so listing
    stays a safe GET."""
    if not _fs_browser_enabled():
        raise HTTPException(status_code=403, detail="Folder browser disabled.")
    target = _resolve_browse_target(path)
    safe = mkdir.strip().replace("/", "").replace("\\", "")
    if safe and not safe.startswith("."):
        new_path = target / safe
        try:
            new_path.mkdir(parents=False, exist_ok=True)
            target = new_path.resolve()
        except Exception as e:
            return _render_fs_list(request, target, error=f"Could not create folder: {e}")
    return _render_fs_list(request, target)


@app.get("/api/fields", response_class=HTMLResponse)
async def api_fields(
    request: Request,
    entity: str,
    auther: AuthorizationCodeAuth = Depends(require_auth),
) -> HTMLResponse:
    """Return the field list for an entity as an HTMX partial."""
    try:
        doc = await _ensure_swagger(auther)
    except Exception as e:
        return HTMLResponse(
            f'<div class="text-red-700 text-xs">Could not load fields: {e}</div>',
            status_code=200,
        )
    rows = _field_rows(doc, entity)
    return templates.TemplateResponse(
        request, "_fields.html", {"entity": entity, "fields": rows}
    )
