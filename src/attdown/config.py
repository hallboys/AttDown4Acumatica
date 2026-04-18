# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(raw: str) -> str:
    def sub(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_PATTERN.sub(sub, raw)


class OAuthClientCredentials(BaseModel):
    type: Literal["oauth_client_credentials"]
    client_id: str
    client_secret: str
    scope: str = "api offline_access"


class OAuthAuthCode(BaseModel):
    type: Literal["oauth_auth_code"]
    client_id: str
    client_secret: str | None = None
    redirect_uri: str
    scope: str = "api offline_access"


class SessionAuth(BaseModel):
    type: Literal["session"]
    username: str
    password: str
    company: str
    branch: str | None = None


AuthConfig = OAuthClientCredentials | OAuthAuthCode | SessionAuth


class AcumaticaConfig(BaseModel):
    base_url: str
    endpoint: str = "Default/24.200.001"
    auth: AuthConfig = Field(discriminator="type")
    verify_ssl: bool = True


class MatchConfig(BaseModel):
    """Match records where a field equals any value in a list.

    Provide either `values` inline or point at a CSV via `from_csv` + `column`.
    Combine with `filter` on the parent JobConfig to AND the two criteria.
    """
    field: str
    values: list[str] | None = None
    from_csv: str | None = None
    column: str | int | None = None
    has_header: bool = True
    chunk_size: int = 50

    @model_validator(mode="after")
    def _one_source(self) -> "MatchConfig":
        has_values = bool(self.values)
        has_csv = bool(self.from_csv)
        if has_values == has_csv:
            raise ValueError(
                "MatchConfig: specify exactly one of `values` or `from_csv`."
            )
        if has_csv and self.column is None:
            raise ValueError(
                "MatchConfig.from_csv requires `column` (header name or 0-indexed int)."
            )
        return self


class JobConfig(BaseModel):
    entity: str
    filter: str | None = None
    match: MatchConfig | None = None
    select: list[str] | None = None
    expand: list[str] = Field(default_factory=lambda: ["Files"])
    path: str = "{entity}/{id}/{filename}"
    top_n: int | None = None


class AppConfig(BaseModel):
    acumatica: AcumaticaConfig
    output: str
    checkpoint: str = "file:///data/.state.sqlite"
    concurrency: int = 4
    jobs: list[JobConfig] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        cfg_path = Path(path).resolve()
        text = cfg_path.read_text()
        text = _expand_env(text)
        data = yaml.safe_load(text)
        cfg = cls.model_validate(data)
        # Resolve relative match.from_csv paths against the config file's directory.
        for job in cfg.jobs:
            if job.match and job.match.from_csv:
                p = Path(job.match.from_csv)
                if not p.is_absolute():
                    job.match.from_csv = str((cfg_path.parent / p).resolve())
        return cfg
