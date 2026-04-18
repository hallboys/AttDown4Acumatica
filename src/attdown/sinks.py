# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Destination writer. Wraps fsspec for local/s3/az/gs/sharepoint.

Usage:
    sink = Sink.from_uri("s3://bucket/prefix")
    await sink.write("2025/bills/VENDOR/INV-1.pdf", data)
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import fsspec


class Sink:
    def __init__(self, fs: fsspec.AbstractFileSystem, root: str):
        self.fs = fs
        self.root = root.rstrip("/")

    @classmethod
    def from_uri(cls, uri: str) -> "Sink":
        parsed = urlparse(uri)
        scheme = parsed.scheme or "file"

        if scheme == "file":
            root = parsed.path or "/data"
            fs = fsspec.filesystem("file")
            fs.makedirs(root, exist_ok=True)
            return cls(fs, root)

        if scheme == "s3":
            fs = fsspec.filesystem("s3")
            root = f"{parsed.netloc}{parsed.path}"
            return cls(fs, root)

        if scheme in ("az", "abfs"):
            fs = fsspec.filesystem("az")
            root = f"{parsed.netloc}{parsed.path}"
            return cls(fs, root)

        if scheme == "gs":
            fs = fsspec.filesystem("gs")
            root = f"{parsed.netloc}{parsed.path}"
            return cls(fs, root)

        if scheme == "sharepoint":
            # Handled by a dedicated Graph-API writer, see sinks_sharepoint.py (future).
            raise NotImplementedError(
                "sharepoint:// requires the 'sharepoint' extra and Graph credentials; "
                "see docs/sharepoint.md"
            )

        raise ValueError(f"Unsupported destination scheme: {scheme}")

    async def write(self, rel_path: str, data: bytes) -> str:
        full = f"{self.root}/{rel_path.lstrip('/')}"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_sync, full, data)
        return full

    def _write_sync(self, full: str, data: bytes) -> None:
        parent = full.rsplit("/", 1)[0]
        try:
            self.fs.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with self.fs.open(full, "wb") as f:
            f.write(data)

    async def exists(self, rel_path: str) -> bool:
        full = f"{self.root}/{rel_path.lstrip('/')}"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.fs.exists, full)
