# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed checkpoint: dedupe files across runs, resume on failure."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    config      TEXT
);

CREATE TABLE IF NOT EXISTS files (
    run_id      INTEGER NOT NULL,
    entity      TEXT NOT NULL,
    record_key  TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    filename    TEXT,
    sink_path   TEXT,
    size        INTEGER,
    status      TEXT NOT NULL,
    error       TEXT,
    finished_at TEXT,
    PRIMARY KEY (entity, record_key, file_id)
);

CREATE INDEX IF NOT EXISTS idx_files_run ON files(run_id);
"""


def _path_from_uri(uri: str) -> str:
    if uri.startswith("file://"):
        return urlparse(uri).path
    return uri


class Checkpoint:
    def __init__(self, path: str):
        self.path = Path(_path_from_uri(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def start_run(self, config_text: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO runs(started_at, status, config) VALUES (datetime('now'), 'running', ?)",
                (config_text,),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def finish_run(self, run_id: int, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE runs SET finished_at=datetime('now'), status=? WHERE id=?",
                (status, run_id),
            )
            await db.commit()

    async def is_done(self, entity: str, record_key: str, file_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT status FROM files WHERE entity=? AND record_key=? AND file_id=?",
                (entity, record_key, file_id),
            )
            row = await cur.fetchone()
            return bool(row and row[0] == "ok")

    async def prior_ok_path(
        self, entity: str, record_key: str, file_id: str
    ) -> str | None:
        """Return the sink_path of a prior successful download, or None."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT sink_path FROM files WHERE entity=? AND record_key=? AND file_id=? AND status='ok'",
                (entity, record_key, file_id),
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

    async def mark_ok(
        self,
        run_id: int,
        entity: str,
        record_key: str,
        file_id: str,
        filename: str | None,
        sink_path: str,
        size: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO files
                   (run_id, entity, record_key, file_id, filename, sink_path, size, status, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', datetime('now'))""",
                (run_id, entity, record_key, file_id, filename, sink_path, size),
            )
            await db.commit()

    async def mark_err(
        self, run_id: int, entity: str, record_key: str, file_id: str, error: str
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO files
                   (run_id, entity, record_key, file_id, status, error, finished_at)
                   VALUES (?, ?, ?, ?, 'error', ?, datetime('now'))""",
                (run_id, entity, record_key, file_id, error),
            )
            await db.commit()

    async def run_stats(self, run_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? GROUP BY status",
                (run_id,),
            )
            rows = await cur.fetchall()
            out = {"ok": 0, "error": 0, "bytes": 0}
            for status, n, bs in rows:
                out[status] = n
                if status == "ok":
                    out["bytes"] = int(bs)
            return out
