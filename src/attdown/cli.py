# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from attdown import auth as auth_module
from attdown.checkpoint import Checkpoint
from attdown.client import AcumaticaClient
from attdown.config import AppConfig
from attdown.downloader import Downloader
from attdown.sinks import Sink


import sys


def _load_dotenv() -> str | None:
    """Load .env from CWD (or ATTDOWN_ENV_FILE). Returns the path loaded, or None."""
    path = Path(os.environ.get("ATTDOWN_ENV_FILE", ".env"))
    if not path.is_file():
        return None
    try:
        from dotenv import load_dotenv  # python-dotenv
        load_dotenv(path, override=False)
    except ImportError:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    return str(path.resolve())


_dotenv_loaded = _load_dotenv()
if _dotenv_loaded:
    print(f"[attdown] loaded env from {_dotenv_loaded}", file=sys.stderr)
else:
    _tried = Path(os.environ.get("ATTDOWN_ENV_FILE", ".env")).resolve()
    print(
        f"[attdown] no .env found at {_tried} "
        "(set ATTDOWN_ENV_FILE=/path/to/.env, or run from the directory containing .env, "
        "or export the vars in your shell)",
        file=sys.stderr,
    )


app = typer.Typer(
    name="attdown",
    help="AttDown4Acumatica — bulk download attachments from any Acumatica entity.",
)
console = Console()


@app.command("gen-secret")
def gen_secret() -> None:
    """Print a random SESSION_SECRET suitable for pasting into .env.

    The web UI refuses to start without SESSION_SECRET set. Run this once,
    copy the line into your .env file, and keep it stable across restarts
    (rotating it logs every user out).
    """
    import secrets as _secrets
    typer.echo(f"SESSION_SECRET={_secrets.token_urlsafe(48)}")


@app.command()
def run(
    config: Path = typer.Option(..., "--config", "-c", exists=True),
    dry_run: bool = typer.Option(False, "--dry-run"),
    only: str | None = typer.Option(None, "--job", help="Run only the named entity"),
) -> None:
    """Run all jobs in the config file."""
    cfg = AppConfig.load(config)
    asyncio.run(_run(cfg, dry_run=dry_run, only=only))


@app.command()
def entities(
    config: Path = typer.Option(..., "--config", "-c", exists=True),
    with_files: bool = typer.Option(True, "--with-files/--all"),
) -> None:
    """List entities in the configured endpoint. By default only those with a Files sub-entity."""
    cfg = AppConfig.load(config)
    asyncio.run(_entities(cfg, with_files=with_files))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser"),
) -> None:
    """Launch the web UI."""
    import threading
    import time
    import uvicorn
    import webbrowser

    if open_browser:
        # Delay slightly so uvicorn has bound the socket before the browser hits it.
        # 'localhost' works regardless of whether host is 0.0.0.0 or 127.0.0.1.
        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://localhost:{port}/")
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run("attdown.web.app:app", host=host, port=port, reload=False)


async def _run(cfg: AppConfig, *, dry_run: bool, only: str | None) -> None:
    ck = Checkpoint(cfg.checkpoint)
    await ck.init()
    sink = Sink.from_uri(cfg.output)
    auther = auth_module.build(cfg.acumatica)

    jobs = [j for j in cfg.jobs if not only or j.entity == only]
    if not jobs:
        console.print("[red]No jobs to run.[/red]")
        raise typer.Exit(1)

    async with AcumaticaClient(
        cfg.acumatica.base_url, cfg.acumatica.endpoint, auther,
        verify=cfg.acumatica.verify_ssl, concurrency=cfg.concurrency,
    ) as client:
        run_id = await ck.start_run(config_text=str(cfg.model_dump()))
        dl = Downloader(client, sink, ck, run_id, concurrency=cfg.concurrency)
        try:
            for job in jobs:
                console.print(f"[bold cyan]→ {job.entity}[/bold cyan] filter={job.filter!r}")
                prog = await dl.run_job(job, dry_run=dry_run)
                console.print(
                    f"  records={prog.total_records} queued={prog.files_queued} "
                    f"done={prog.files_done} skipped={prog.files_skipped} "
                    f"failed={prog.files_failed} bytes={prog.bytes_done:,}"
                )
            await ck.finish_run(run_id, "ok")
        except Exception:
            await ck.finish_run(run_id, "error")
            raise


async def _entities(cfg: AppConfig, *, with_files: bool) -> None:
    auther = auth_module.build(cfg.acumatica)
    async with AcumaticaClient(
        cfg.acumatica.base_url, cfg.acumatica.endpoint, auther, verify=cfg.acumatica.verify_ssl,
    ) as client:
        if with_files:
            names = await client.entities_with_files()
        else:
            data = await client._request("GET", f"/entity/{cfg.acumatica.endpoint}")
            names = sorted(e.get("name") or e.get("Name") or "" for e in data.json().get("entities", []))
    t = Table(title=f"Entities in {cfg.acumatica.endpoint}")
    t.add_column("Entity")
    for n in names:
        t.add_row(n)
    console.print(t)
