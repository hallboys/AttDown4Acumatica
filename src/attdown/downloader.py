# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Core download loop. Generic across any entity with a Files sub-entity."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from attdown.checkpoint import Checkpoint
from attdown.client import AcumaticaClient
from attdown.config import JobConfig
from attdown.sinks import Sink


# Path placeholders: {FieldName} or {FieldName|filter}, where filter ∈ {lower, upper, title, slug}.
_FIELD_RE = re.compile(r"\{([A-Za-z0-9_]+)(?:\|([A-Za-z]+))?\}")

# Characters that are unsafe on any major OS (Windows is the strictest).
# We keep spaces, parens, ampersands, apostrophes, commas, hashes, etc. — they work on
# macOS/Linux/Windows and are common in real business data (e.g. "Acme & Co.").
_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]+')

# Windows reserved device names. Prefixing them with "_" makes them legal.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize(s: str) -> str:
    """Make a single path component safe across macOS/Linux/Windows.

    Preserves spaces and human punctuation; only strips characters Windows or POSIX
    actually reject. Also handles Windows reserved device names and trailing dots/spaces.
    """
    # Path separators become a hyphen so we don't accidentally create a subfolder.
    s = s.replace("/", "-").replace("\\", "-")
    s = _UNSAFE.sub("_", s)
    # Collapse runs of whitespace and trim.
    s = re.sub(r"\s+", " ", s).strip()
    # Windows strips trailing dots/spaces silently — normalize ahead of time.
    s = s.rstrip(". ")
    # Guard Windows reserved device names (case-insensitive).
    stem = s.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        s = "_" + s
    return s or "_"


def _apply_filter(val: str, flt: str | None) -> str:
    if not flt:
        return val
    f = flt.lower()
    if f == "lower":
        return val.lower()
    if f == "upper":
        return val.upper()
    if f == "title":
        return val.title()
    if f == "slug":
        return re.sub(r"\s+", "-", val.strip().lower())
    return val


def _render_path(tmpl: str, rec: dict[str, Any], *, filename: str, entity: str) -> str:
    """Render a path template. Missing values become '_'. Only top-level fields are supported.

    Placeholders:
        {FieldName}          → raw value, sanitized for the filesystem
        {FieldName|lower}    → lowercase
        {FieldName|upper}    → uppercase
        {FieldName|title}    → title case
        {FieldName|slug}     → lowercase with whitespace → "-"
        {entity}, {filename} → the current entity name / attachment filename
    """

    def lookup(m: re.Match[str]) -> str:
        key = m.group(1)
        flt = m.group(2)

        if key == "filename":
            return _sanitize(_apply_filter(filename, flt))
        if key == "entity":
            return _sanitize(_apply_filter(entity, flt))

        val = rec.get(key)
        if isinstance(val, dict):
            val = val.get("value") or val.get("Value")
        if val is None:
            return "_"
        return _sanitize(_apply_filter(str(val), flt))

    return _FIELD_RE.sub(lookup, tmpl)


def _record_key(rec: dict[str, Any]) -> str:
    """Compose a stable key for a record. Uses 'id' (GUID) when available."""
    rid = rec.get("id") or rec.get("ID")
    if rid:
        return str(rid)
    # fall back to the first "ReferenceNbr" / "RefNbr"-like field
    for k in ("ReferenceNbr", "RefNbr", "OrderNbr", "Nbr"):
        v = rec.get(k)
        if isinstance(v, dict):
            v = v.get("value")
        if v:
            return f"{k}:{v}"
    return json.dumps({k: rec.get(k) for k in list(rec.keys())[:3]}, sort_keys=True)


@dataclass
class FileTask:
    entity: str
    record_key: str
    file_id: str
    filename: str
    href: str
    sink_rel_path: str


@dataclass
class Progress:
    total_records: int = 0
    files_queued: int = 0
    files_done: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_done: int = 0
    current: str = ""


ProgressCb = Callable[[Progress], None]


class Downloader:
    def __init__(
        self,
        client: AcumaticaClient,
        sink: Sink,
        checkpoint: Checkpoint,
        run_id: int,
        concurrency: int = 4,
    ):
        self.client = client
        self.sink = sink
        self.ck = checkpoint
        self.run_id = run_id
        self.sem = asyncio.Semaphore(concurrency)
        self.progress = Progress()

    async def plan(self, job: JobConfig) -> AsyncIterator[FileTask]:
        """Walk the entity, yield one FileTask per attachment (no downloads)."""
        from attdown.match import build_match_filters, resolve_match_values

        select = job.select
        if select is not None and "Files" not in select:
            select = [*select, "Files"]

        # Assemble the list of filter strings to issue. Three cases:
        #   - match set  -> one chunked OR-filter per chunk, optionally AND'ed with job.filter
        #   - filter only -> single-element list with that filter
        #   - neither     -> single-element list with None (no filter)
        if job.match:
            values = resolve_match_values(job.match)
            filters: list[str | None] = list(
                build_match_filters(
                    job.match.field,
                    values,
                    chunk_size=job.match.chunk_size,
                    extra_filter=job.filter,
                )
            )
            if not filters:
                return  # no keys -> no work
        else:
            filters = [job.filter]

        seen_record_keys: set[str] = set()
        for flt in filters:
            async for rec in self.client.iter_entity(
                job.entity,
                filter=flt,
                select=select,
                expand=job.expand,
            ):
                rk = _record_key(rec)
                if rk in seen_record_keys:
                    continue  # record appeared in more than one match chunk; skip
                seen_record_keys.add(rk)
                self.progress.total_records += 1
                # Acumatica returns the sub-entity array under "files" (lowercase) even
                # though the query uses "$expand=Files". Check both to be robust.
                files = rec.get("files") or rec.get("Files") or []
                for f in files:
                    fid = str(f.get("id") or f.get("ID") or "")
                    name = f.get("filename") or f.get("Filename") or f.get("name") or "unnamed"
                    href = f.get("href") or f.get("Href")
                    if not (fid and href):
                        continue
                    rel = _render_path(job.path, rec, filename=name, entity=job.entity)
                    self.progress.files_queued += 1
                    yield FileTask(job.entity, rk, fid, name, href, rel)

    def _emit(self, cb: ProgressCb | None) -> None:
        """Invoke progress callback with a snapshot, not a shared reference."""
        if cb is None:
            return
        snap = Progress(
            total_records=self.progress.total_records,
            files_queued=self.progress.files_queued,
            files_done=self.progress.files_done,
            files_skipped=self.progress.files_skipped,
            files_failed=self.progress.files_failed,
            bytes_done=self.progress.bytes_done,
            current=self.progress.current,
        )
        cb(snap)

    async def _should_skip(self, task: "FileTask") -> bool:
        """True if this file was already downloaded AND still exists at its sink path.

        For local `file://` sinks we re-check the disk — if the user deleted the file
        we want to fetch it again. For cloud sinks we trust the checkpoint.
        """
        prior = await self.ck.prior_ok_path(task.entity, task.record_key, task.file_id)
        if not prior:
            return False
        if prior.startswith("/") or prior.startswith("file://"):
            from pathlib import Path
            from urllib.parse import urlparse
            p = Path(urlparse(prior).path if prior.startswith("file://") else prior)
            return p.is_file()
        # non-local sink: trust checkpoint
        return True

    async def run_job(
        self,
        job: JobConfig,
        *,
        dry_run: bool = False,
        force: bool = False,
        on_progress: ProgressCb | None = None,
    ) -> Progress:
        async def worker(task: FileTask) -> None:
            async with self.sem:
                if not force and await self._should_skip(task):
                    self.progress.files_skipped += 1
                    self._emit(on_progress)
                    return
                # Announce start-of-file so the "Current file" cell updates live.
                self.progress.current = f"{task.entity} / {task.filename}"
                self._emit(on_progress)
                if dry_run:
                    self.progress.files_done += 1
                    self._emit(on_progress)
                    return
                try:
                    data, server_name = await self.client.download(task.href)
                    name = server_name or task.filename
                    rel = task.sink_rel_path.replace(task.filename, name) if server_name else task.sink_rel_path
                    full = await self.sink.write(rel, data)
                    await self.ck.mark_ok(
                        self.run_id, task.entity, task.record_key, task.file_id,
                        name, full, len(data),
                    )
                    self.progress.files_done += 1
                    self.progress.bytes_done += len(data)
                except Exception as e:
                    await self.ck.mark_err(
                        self.run_id, task.entity, task.record_key, task.file_id, str(e)[:500]
                    )
                    self.progress.files_failed += 1
                finally:
                    self._emit(on_progress)

        tasks: list[asyncio.Task[None]] = []
        async for t in self.plan(job):
            self._emit(on_progress)  # emit as each record is discovered, so counts climb
            tasks.append(asyncio.create_task(worker(t)))
        if tasks:
            await asyncio.gather(*tasks)
        return self.progress
