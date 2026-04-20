# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec for the `attdown` CLI + web UI one-file binary.
# Build with:
#     pip install pyinstaller
#     pyinstaller pyinstaller/attdown.spec

# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).resolve().parent

# fsspec + friends use entry-point plugins; pull the whole submodule tree so
# at least the built-in `file://` filesystem is available. Cloud backends
# (s3fs / adlfs / gcsfs) are not bundled — users who need them can
# `pip install attdown4acumatica[s3]` / `[azure]` / `[gcs]`.
hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("fsspec")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("attdown")
# websocket + httptools implementations that uvicorn picks at runtime
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
]

# Bundle Jinja templates and the static dir alongside the package.
datas = [
    (str(ROOT / "src" / "attdown" / "web" / "templates"), "attdown/web/templates"),
    (str(ROOT / "src" / "attdown" / "web" / "static"),    "attdown/web/static"),
]
datas += collect_data_files("fsspec")

a = Analysis(
    [str(ROOT / "src" / "attdown" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="attdown",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX + anti-virus heuristics are a bad combo; skip it
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # show a console window — users see URL + errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
