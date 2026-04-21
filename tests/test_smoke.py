# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Smoke tests — pure-function coverage, no network."""

from attdown.config import AppConfig
from attdown.downloader import _render_path, _sanitize


def test_sanitize():
    assert _sanitize("ACME / Co.") == "ACME - Co"
    assert _sanitize("path\\sep") == "path-sep"


def test_render_path():
    rec = {"Vendor": {"value": "ACME"}, "ReferenceNbr": {"value": "INV-001"}}
    p = _render_path(
        "ap/{Vendor}/{ReferenceNbr}/{filename}",
        rec,
        filename="scan.pdf",
        entity="Bill",
    )
    assert p == "ap/ACME/INV-001/scan.pdf"


def test_config_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("ACU_URL", "https://example.com")
    monkeypatch.setenv("ACU_CLIENT_ID", "cid")
    monkeypatch.setenv("ACU_CLIENT_SECRET", "sec")
    monkeypatch.setenv("OUTPUT_URI", "file:///tmp/x")
    p = tmp_path / "c.yaml"
    p.write_text(
        """
acumatica:
  base_url: ${ACU_URL}
  endpoint: Default/24.200.001
  auth:
    type: oauth_client_credentials
    client_id: ${ACU_CLIENT_ID}
    client_secret: ${ACU_CLIENT_SECRET}
output: ${OUTPUT_URI}
jobs:
  - entity: Bill
"""
    )
    cfg = AppConfig.load(p)
    assert cfg.acumatica.base_url == "https://example.com"
    assert cfg.output == "file:///tmp/x"
    assert cfg.jobs[0].entity == "Bill"
