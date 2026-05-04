"""Thin async HTTP client for eazyBI.

The only **publicly documented** REST endpoint is the *report results export*:

    GET {BASE}/accounts/{account_id}/export/report/{report_id}.{format}

For eazyBI for Jira Cloud the BASE is fixed to ``https://aod.eazybi.com`` and
authentication is HTTP Basic with an Atlassian account email + API token.
See: https://docs.eazybi.com/eazybi/set-up-and-administer/customization/report-results-export-api

In addition, the eazyBI Rails app exposes a number of internal JSON routes
that the UI uses (``/accounts.json``, ``/accounts/{id}/reports.json`` etc.).
They are not part of the documented public API but are stable enough to be
useful for read-only metadata. Tools that hit them are clearly marked as
*experimental* in their docstrings.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import httpx


class EazyBIError(Exception):
    """Raised on HTTP / protocol errors when talking to eazyBI."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class EazyBIConfig:
    base_url: str
    user: str | None
    api_token: str | None
    timeout: float = 30.0
    default_account_id: str | None = None

    @classmethod
    def from_env(cls) -> "EazyBIConfig":
        base_url = os.environ.get("EAZYBI_BASE_URL", "https://aod.eazybi.com").rstrip("/")
        user = os.environ.get("EAZYBI_USER") or None
        api_token = os.environ.get("EAZYBI_API_TOKEN") or None
        try:
            timeout = float(os.environ.get("EAZYBI_HTTP_TIMEOUT", "30"))
        except ValueError:
            timeout = 30.0
        default_account = os.environ.get("EAZYBI_DEFAULT_ACCOUNT_ID") or None
        return cls(
            base_url=base_url,
            user=user,
            api_token=api_token,
            timeout=timeout,
            default_account_id=default_account,
        )


class EazyBIClient:
    """Async client that handles auth, retries, and JSON parsing."""

    def __init__(self, config: EazyBIConfig) -> None:
        self._config = config
        auth: httpx.BasicAuth | None = None
        if config.user and config.api_token:
            auth = httpx.BasicAuth(config.user, config.api_token)
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            auth=auth,
            timeout=config.timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "eazybi-mcp/0.1 (+https://github.com/ultimate-guitar/eazybi-mcp)",
            },
            follow_redirects=True,
        )

    @property
    def config(self) -> EazyBIConfig:
        return self._config

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        accept: str | None = None,
        embed_token: str | None = None,
    ) -> httpx.Response:
        """One request with simple 429/503 backoff."""
        if not path.startswith("/"):
            path = "/" + path

        merged_params: dict[str, Any] = {}
        if params:
            merged_params.update({k: v for k, v in params.items() if v is not None})
        if embed_token:
            merged_params["embed_token"] = embed_token

        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept

        # If we're using embed_token, drop Basic auth for this single call.
        auth: httpx._types.AuthTypes | None = httpx.USE_CLIENT_DEFAULT  # type: ignore[assignment]
        if embed_token:
            auth = None

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=merged_params or None,
                    json=json_body,
                    headers=headers or None,
                    auth=auth,
                )
            except httpx.TransportError as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code in (429, 502, 503, 504) and attempt < 2:
                # Honor Retry-After if present, otherwise exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                snippet = resp.text[:500] if resp.text else ""
                raise EazyBIError(
                    f"eazyBI {method} {path} failed with HTTP {resp.status_code}",
                    status=resp.status_code,
                    body=snippet,
                )
            return resp

        # All retries exhausted on a transport error.
        raise EazyBIError(f"Network error talking to eazyBI: {last_exc!r}")

    # --------------------------------------------------------------- helpers

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        embed_token: str | None = None,
    ) -> Any:
        resp = await self._request("GET", path, params=params, embed_token=embed_token, accept="application/json")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise EazyBIError(
                f"Expected JSON from {path}, got {resp.headers.get('content-type', '?')}",
                status=resp.status_code,
                body=resp.text[:500],
            ) from exc

    async def get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        embed_token: str | None = None,
        accept: str | None = None,
    ) -> tuple[bytes, str]:
        resp = await self._request("GET", path, params=params, embed_token=embed_token, accept=accept)
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    # --------------------------------------------------------------- domain

    @staticmethod
    def _format_selected_pages(pages: Sequence[str] | None) -> str | None:
        """eazyBI expects ``selected_pages`` as comma-separated full member names.

        Each item must already be a full MDX member name like
        ``[Time].[2026]`` or ``[Project].[ACME]``. We just join them.
        """
        if not pages:
            return None
        return ",".join(pages)

    def export_report_path(self, account_id: str | int, report_id: str | int, fmt: str) -> str:
        return f"/accounts/{account_id}/export/report/{report_id}.{fmt}"

    async def export_report(
        self,
        account_id: str | int,
        report_id: str | int,
        fmt: str = "json",
        *,
        selected_pages: Sequence[str] | None = None,
        embed_token: str | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> tuple[bytes, str]:
        """Run the documented Report Results Export endpoint and return raw bytes + content type."""
        fmt = fmt.lower().strip()
        allowed = {"csv", "xls", "json", "pdf", "png"}
        if fmt not in allowed:
            raise ValueError(f"Unsupported format {fmt!r}; expected one of {sorted(allowed)}")

        params: dict[str, Any] = {}
        if extra_params:
            params.update(extra_params)
        sp = self._format_selected_pages(selected_pages)
        if sp:
            params["selected_pages"] = sp

        accept = {
            "json": "application/json",
            "csv": "text/csv",
            "xls": "application/vnd.ms-excel",
            "pdf": "application/pdf",
            "png": "image/png",
        }[fmt]
        return await self.get_bytes(
            self.export_report_path(account_id, report_id, fmt),
            params=params,
            embed_token=embed_token,
            accept=accept,
        )

    # ------------- experimental list/metadata helpers ------------------

    async def list_accounts(self) -> Any:
        """Experimental: ``GET /accounts.json`` (used by the eazyBI UI)."""
        return await self.get_json("/accounts.json")

    async def list_reports(self, account_id: str | int) -> Any:
        """Experimental: ``GET /accounts/{id}/reports.json``."""
        return await self.get_json(f"/accounts/{account_id}/reports.json")

    async def get_report(self, account_id: str | int, report_id: str | int) -> Any:
        """Experimental: report metadata (definition + folder)."""
        return await self.get_json(f"/accounts/{account_id}/reports/{report_id}.json")

    async def list_dashboards(self, account_id: str | int) -> Any:
        """Experimental: ``GET /accounts/{id}/dashboards.json``."""
        return await self.get_json(f"/accounts/{account_id}/dashboards.json")

    async def get_dashboard(self, account_id: str | int, dashboard_id: str | int) -> Any:
        """Experimental: ``GET /accounts/{id}/dashboards/{did}.json``."""
        return await self.get_json(f"/accounts/{account_id}/dashboards/{dashboard_id}.json")

    async def list_cubes(self, account_id: str | int) -> Any:
        """Experimental: ``GET /accounts/{id}/cubes.json``."""
        return await self.get_json(f"/accounts/{account_id}/cubes.json")

    async def get_cube(self, account_id: str | int, cube_name: str) -> Any:
        """Experimental: ``GET /accounts/{id}/cubes/{cube}.json``."""
        # Cube names can contain spaces — leave them to httpx for URL-encoding.
        return await self.get_json(f"/accounts/{account_id}/cubes/{cube_name}.json")


def collect_keys(items: Iterable[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Project an iterable of dicts down to a fixed set of keys (best-effort)."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        out.append({k: item.get(k) for k in keys if k in item})
    return out
