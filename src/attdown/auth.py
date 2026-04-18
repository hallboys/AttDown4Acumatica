# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Acumatica auth strategies: OAuth client credentials, auth code + PKCE, and session cookie.

All three implement a common async interface:

    async def attach(self, client: httpx.AsyncClient) -> None

which mutates the client so that subsequent requests are authenticated.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

import httpx

from attdown.config import (
    AcumaticaConfig,
    OAuthAuthCode,
    OAuthClientCredentials,
    SessionAuth,
)


class Authenticator(Protocol):
    async def attach(self, client: httpx.AsyncClient) -> None: ...
    async def close(self, client: httpx.AsyncClient) -> None: ...


@dataclass
class _TokenBag:
    access_token: str = ""
    refresh_token: str | None = None
    expires_at: float = 0.0

    def is_expiring(self, slack: int = 60) -> bool:
        return time.time() + slack >= self.expires_at


@dataclass
class ClientCredentialsAuth:
    base_url: str
    client_id: str
    client_secret: str
    scope: str = "api offline_access"
    tokens: _TokenBag = field(default_factory=_TokenBag)

    async def attach(self, client: httpx.AsyncClient) -> None:
        if not self.tokens.access_token or self.tokens.is_expiring():
            await self._fetch(client)
        client.headers["Authorization"] = f"Bearer {self.tokens.access_token}"

    async def _fetch(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            f"{self.base_url}/identity/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self.tokens = _TokenBag(
            access_token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 3600)),
        )

    async def close(self, client: httpx.AsyncClient) -> None:
        return None


@dataclass
class AuthorizationCodeAuth:
    """Authorization code + PKCE. For the web UI, which drives the browser redirect dance."""

    base_url: str
    client_id: str
    redirect_uri: str
    scope: str = "api offline_access"
    client_secret: str | None = None
    tokens: _TokenBag = field(default_factory=_TokenBag)

    def authorize_url(self, state: str, code_verifier: str) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.base_url}/identity/connect/authorize?{urlencode(params)}"

    @staticmethod
    def new_verifier() -> str:
        return secrets.token_urlsafe(64)

    async def exchange(self, code: str, code_verifier: str) -> None:
        async with httpx.AsyncClient() as client:
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": code_verifier,
            }
            if self.client_secret:
                data["client_secret"] = self.client_secret
            resp = await client.post(
                f"{self.base_url}/identity/connect/token", data=data
            )
            resp.raise_for_status()
            payload = resp.json()
            self.tokens = _TokenBag(
                access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token"),
                expires_at=time.time() + int(payload.get("expires_in", 3600)),
            )

    async def _refresh(self, client: httpx.AsyncClient) -> None:
        if not self.tokens.refresh_token:
            raise RuntimeError("No refresh token; user must re-authorize.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.tokens.refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = await client.post(f"{self.base_url}/identity/connect/token", data=data)
        resp.raise_for_status()
        payload = resp.json()
        self.tokens = _TokenBag(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", self.tokens.refresh_token),
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
        )

    async def attach(self, client: httpx.AsyncClient) -> None:
        if self.tokens.is_expiring():
            await self._refresh(client)
        client.headers["Authorization"] = f"Bearer {self.tokens.access_token}"

    async def close(self, client: httpx.AsyncClient) -> None:
        return None


@dataclass
class SessionCookieAuth:
    base_url: str
    username: str
    password: str
    company: str
    branch: str | None = None
    _logged_in: bool = False

    async def attach(self, client: httpx.AsyncClient) -> None:
        if self._logged_in:
            return
        body = {"name": self.username, "password": self.password, "company": self.company}
        if self.branch:
            body["branch"] = self.branch
        resp = await client.post(f"{self.base_url}/entity/auth/login", json=body)
        resp.raise_for_status()
        self._logged_in = True

    async def close(self, client: httpx.AsyncClient) -> None:
        if self._logged_in:
            await client.post(f"{self.base_url}/entity/auth/logout")
            self._logged_in = False


def build(cfg: AcumaticaConfig) -> Authenticator:
    match cfg.auth:
        case OAuthClientCredentials():
            return ClientCredentialsAuth(
                base_url=cfg.base_url,
                client_id=cfg.auth.client_id,
                client_secret=cfg.auth.client_secret,
                scope=cfg.auth.scope,
            )
        case OAuthAuthCode():
            return AuthorizationCodeAuth(
                base_url=cfg.base_url,
                client_id=cfg.auth.client_id,
                client_secret=cfg.auth.client_secret,
                redirect_uri=cfg.auth.redirect_uri,
                scope=cfg.auth.scope,
            )
        case SessionAuth():
            return SessionCookieAuth(
                base_url=cfg.base_url,
                username=cfg.auth.username,
                password=cfg.auth.password,
                company=cfg.auth.company,
                branch=cfg.auth.branch,
            )
