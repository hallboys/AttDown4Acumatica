# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Thin Acumatica REST client. Handles paging, retry, metadata, file download."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from attdown.auth import Authenticator


class AcumaticaError(Exception):
    """4xx/5xx response from Acumatica with the full body attached for diagnosis."""

    def __init__(self, status: int, method: str, url: str, body: str):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(self._format())

    def _format(self) -> str:
        short = (self.body or "").strip()
        if len(short) > 800:
            short = short[:800] + " …(truncated)"
        return f"Acumatica HTTP {self.status} on {self.method} {self.url}\n{short}"

    @classmethod
    def from_response(cls, resp: httpx.Response) -> "AcumaticaError":
        body = resp.text
        try:
            data = resp.json()
            if isinstance(data, dict):
                parts: list[str] = []
                for k in ("message", "exceptionMessage", "error_description", "error"):
                    v = data.get(k)
                    if v:
                        parts.append(f"{k}: {v}")
                if parts:
                    body = "\n".join(parts)
        except Exception:
            pass
        req = resp.request
        return cls(
            status=resp.status_code,
            method=req.method if req else "?",
            url=str(req.url) if req else "?",
            body=body,
        )


class AcumaticaClient:
    def __init__(
        self,
        base_url: str,
        endpoint: str,
        auth: Authenticator,
        verify: bool = True,
        concurrency: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint.strip("/")
        self.auth = auth
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )

    async def __aenter__(self) -> "AcumaticaClient":
        await self.auth.attach(self._http)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.auth.close(self._http)
        finally:
            await self._http.aclose()

    @property
    def _entity_root(self) -> str:
        return f"/entity/{self.endpoint}"

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt:
                await self.auth.attach(self._http)
                resp = await self._http.request(method, url, **kw)
                if resp.status_code in (401, 403):
                    if hasattr(self.auth, "tokens"):
                        self.auth.tokens.access_token = ""
                if resp.status_code >= 500:
                    resp.raise_for_status()  # triggers tenacity retry
                if resp.status_code >= 400:
                    raise AcumaticaError.from_response(resp)  # 4xx: fail fast with body
                return resp
        raise RuntimeError("unreachable")

    # ----- metadata / discovery -----

    async def list_endpoints(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/entity")
        resp.raise_for_status()
        return resp.json().get("endpoints", [])

    async def swagger(self) -> dict[str, Any]:
        """Fetch the OpenAPI/Swagger document for the active endpoint."""
        resp = await self._request("GET", f"{self._entity_root}/swagger.json")
        resp.raise_for_status()
        return resp.json()

    async def entities_with_files(self) -> list[str]:
        """Return entity names that declare a `Files` property in the endpoint's swagger.

        Walks allOf/$ref chains so entities that inherit `Files` from a base schema
        (the normal Acumatica pattern) are detected.
        """
        doc = await self.swagger()
        definitions: dict[str, Any] = (
            doc.get("definitions")
            or doc.get("components", {}).get("schemas", {})
            or {}
        )

        entity_names: set[str] = set()
        for path in doc.get("paths", {}):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 1 and not parts[0].startswith("{"):
                entity_names.add(parts[0])

        def resolve(schema: dict[str, Any], seen: set[str]) -> dict[str, Any]:
            if "$ref" in schema:
                ref = schema["$ref"].rsplit("/", 1)[-1]
                if ref in seen or ref not in definitions:
                    return {}
                seen.add(ref)
                return resolve(definitions[ref], seen)
            out: dict[str, Any] = {}
            for sub in schema.get("allOf", []) or []:
                out.update(resolve(sub, seen))
            out.update(schema.get("properties") or {})
            return out

        matched: list[str] = []
        for name in entity_names:
            props = resolve(definitions.get(name) or {}, set())
            if any(k.lower() == "files" for k in props):
                matched.append(name)

        return sorted(matched) if matched else sorted(entity_names)

    # ----- querying -----

    async def list_entity(
        self,
        entity: str,
        *,
        filter: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        top: int = 500,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"$top": str(top), "$skip": str(skip)}
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        resp = await self._request("GET", f"{self._entity_root}/{entity}", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("value", [])

    async def iter_entity(
        self,
        entity: str,
        *,
        filter: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        page_size: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        skip = 0
        while True:
            page = await self.list_entity(
                entity,
                filter=filter,
                select=select,
                expand=expand,
                top=page_size,
                skip=skip,
            )
            if not page:
                return
            for row in page:
                yield row
            if len(page) < page_size:
                return
            skip += page_size

    # ----- file download -----

    async def download(self, href: str) -> tuple[bytes, str | None]:
        """Fetch raw bytes from a file href. Returns (content, filename-from-header-if-any)."""
        resp = await self._request("GET", href)
        resp.raise_for_status()
        filename = None
        cd = resp.headers.get("content-disposition")
        if cd and "filename=" in cd:
            filename = cd.split("filename=", 1)[1].strip().strip('"')
        return resp.content, filename
