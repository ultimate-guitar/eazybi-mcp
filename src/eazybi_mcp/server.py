"""FastMCP server exposing eazyBI as a set of read-only tools.

Tool reliability legend:

* **[stable]** — backed by the documented Report Results Export API.
* **[experimental]** — uses internal Rails JSON routes (e.g. ``/accounts.json``)
  that the eazyBI UI consumes. Useful but not part of the public contract;
  the eazyBI team may change them without notice.

Configuration is read from environment variables (see ``.env.example``):

* ``EAZYBI_BASE_URL``           — defaults to ``https://aod.eazybi.com``
                                  (eazyBI for Jira Cloud).
* ``EAZYBI_USER``               — Atlassian email / Jira username.
* ``EAZYBI_API_TOKEN``          — Atlassian API token / Jira password.
* ``EAZYBI_DEFAULT_ACCOUNT_ID`` — optional fallback for tools that take
                                  ``account_id``.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import EazyBIClient, EazyBIConfig, EazyBIError, collect_keys

logger = logging.getLogger("eazybi_mcp")


_NOT_SUPPORTED_HINT = (
    "This endpoint requires Atlassian Connect JWT (used inside the eazyBI "
    "iframe in Jira). On Atlassian Cloud, Basic auth with an API token only "
    "works for the documented export endpoint — use export_report / "
    "get_export_url instead. On Data Center / Private eazyBI it should work."
)


async def _safe_experimental(coro_factory):
    """Call an experimental endpoint, returning a structured 'unsupported'
    payload on 401/403/404 instead of bubbling up an error.

    ``coro_factory`` is a zero-arg callable that returns the coroutine to await,
    so we can build it lazily without paying for a coroutine that won't run.
    """
    try:
        return await coro_factory()
    except EazyBIError as exc:
        if exc.status in (401, 403, 404):
            return {
                "supported": False,
                "status": exc.status,
                "reason": _NOT_SUPPORTED_HINT,
                "raw_error": str(exc),
            }
        raise

# ---------------------------------------------------------------------------
# Server + lazily-initialized client
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="eazybi",
    instructions=(
        "Read-only access to eazyBI (Reports & Charts for Jira). "
        "Use `export_report` to run a saved report and get its data as JSON/CSV/XLS/PDF/PNG — "
        "this is the only officially documented endpoint and is the preferred way "
        "to fetch report results. The other `list_*` / `get_*` tools hit internal "
        "JSON routes used by the eazyBI UI; they work but are experimental."
    ),
)

_client: EazyBIClient | None = None


def _get_client() -> EazyBIClient:
    global _client
    if _client is None:
        cfg = EazyBIConfig.from_env()
        if not (cfg.user and cfg.api_token):
            raise EazyBIError(
                "Missing credentials: set EAZYBI_USER and EAZYBI_API_TOKEN "
                "(Atlassian email + API token for eazyBI for Jira Cloud)."
            )
        _client = EazyBIClient(cfg)
    return _client


def _resolve_account_id(account_id: str | int | None) -> str:
    if account_id not in (None, ""):
        return str(account_id)
    cfg = _get_client().config
    if cfg.default_account_id:
        return cfg.default_account_id
    raise EazyBIError(
        "account_id was not provided and EAZYBI_DEFAULT_ACCOUNT_ID is not set."
    )


# ---------------------------------------------------------------------------
# 1. Reports — documented "export" endpoint
# ---------------------------------------------------------------------------


@mcp.tool()
async def export_report(
    report_id: str | int,
    account_id: str | int | None = None,
    format: str = "json",
    selected_pages: list[str] | None = None,
    embed_token: str | None = None,
    save_to: str | None = None,
    max_chars: int = 50_000,
) -> dict[str, Any]:
    """[stable] Run a saved eazyBI report and return its results.

    Wraps the documented endpoint
    ``GET /accounts/{account_id}/export/report/{report_id}.{format}``.

    Args:
        report_id:    ID of the report (visible in the eazyBI URL after
                      ``/reports/`` and before ``-``).
        account_id:   Account ID (visible after ``/accounts/`` in the URL).
                      Falls back to ``EAZYBI_DEFAULT_ACCOUNT_ID``.
        format:       One of ``json`` (default), ``csv``, ``xls``, ``pdf``, ``png``.
        selected_pages: Optional list of full MDX member names used to set
                      page-filter values, e.g.
                      ``["[Time].[2026]", "[Project].[ACME]"]``.
        embed_token:  If the report is shared with a public access token, pass
                      it here to authenticate without Basic auth.
        save_to:      Absolute path to write binary output (xls/pdf/png) to disk.
                      Recommended for non-text formats — they will not be
                      embedded in the response.
        max_chars:    For json/csv responses, cap the size of inline text returned.
                      The full payload is always available via ``save_to``.

    Returns:
        For ``json``: ``{ "format": "json", "data": <parsed JSON> }``
        For ``csv``:  ``{ "format": "csv", "text": "..."}``
        For binary:   ``{ "format": ..., "saved_to": "...", "size_bytes": N }``
                      (or ``{ "base64": "..." }`` if ``save_to`` is omitted).
    """
    client = _get_client()
    aid = _resolve_account_id(account_id)
    fmt = format.lower().strip()
    payload, content_type = await client.export_report(
        aid,
        report_id,
        fmt,
        selected_pages=selected_pages,
        embed_token=embed_token,
    )

    result: dict[str, Any] = {
        "account_id": aid,
        "report_id": str(report_id),
        "format": fmt,
        "content_type": content_type,
        "size_bytes": len(payload),
    }

    if save_to:
        path = Path(save_to).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        result["saved_to"] = str(path)

    if fmt == "json":
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EazyBIError(f"eazyBI returned non-JSON body: {exc}") from exc
        # Inline parsed JSON. If huge, truncate the text representation.
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > max_chars:
            result["data"] = None
            result["truncated"] = True
            result["preview"] = text[:max_chars]
            result["hint"] = (
                f"Result is {len(text)} chars; pass save_to=... to get the full payload "
                "or call export_report with format='csv' for a smaller representation."
            )
        else:
            result["data"] = data
    elif fmt == "csv":
        text = payload.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            result["truncated"] = True
            result["text"] = text[:max_chars]
            result["hint"] = "Pass save_to=... to download the full CSV."
        else:
            result["text"] = text
    else:
        # Binary formats — never inline by default unless very small.
        if not save_to:
            if len(payload) <= 64 * 1024:
                result["base64"] = base64.b64encode(payload).decode("ascii")
            else:
                result["hint"] = (
                    "Binary result is larger than 64KB; re-run with save_to=/path/to/file "
                    "to write it to disk."
                )
    return result


@mcp.tool()
async def get_export_url(
    report_id: str | int,
    account_id: str | int | None = None,
    format: str = "json",
    selected_pages: list[str] | None = None,
) -> dict[str, str]:
    """[stable] Build the canonical export URL for a report (no request made).

    Useful when you need a link to share or to embed in an external system.
    """
    client = _get_client()
    aid = _resolve_account_id(account_id)
    base = client.config.base_url
    path = client.export_report_path(aid, report_id, format)
    qs = ""
    if selected_pages:
        from urllib.parse import quote

        qs = "?selected_pages=" + quote(",".join(selected_pages), safe="[].,")
    return {"url": f"{base}{path}{qs}"}


# ---------------------------------------------------------------------------
# 2. Listings — experimental, internal eazyBI routes
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_accounts() -> dict[str, Any]:
    """[experimental] List eazyBI accounts visible to the authenticated user.

    Hits ``GET /accounts.json``. **Not supported on eazyBI for Jira Cloud** —
    Basic auth there only authorizes the documented export endpoint. Returns
    a structured ``supported: false`` payload on 401/403/404. Works on Data
    Center / Private eazyBI deployments.
    """
    raw = await _safe_experimental(lambda: _get_client().list_accounts())
    if isinstance(raw, dict) and raw.get("supported") is False:
        return raw
    items = raw if isinstance(raw, list) else (raw or {}).get("accounts", [])
    summary = collect_keys(items, ["id", "name", "data_source", "currency", "time_zone"])
    return {"count": len(summary), "accounts": summary, "raw_sample": items[:1]}


@mcp.tool()
async def list_reports(
    account_id: str | int | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """[experimental] List reports in an account.

    Args:
        account_id: account ID; falls back to EAZYBI_DEFAULT_ACCOUNT_ID.
        folder:     case-insensitive substring match on folder name.
    """
    aid = _resolve_account_id(account_id)
    raw = await _safe_experimental(lambda: _get_client().list_reports(aid))
    if isinstance(raw, dict) and raw.get("supported") is False:
        return raw
    items = raw if isinstance(raw, list) else (raw or {}).get("reports", [])
    if folder:
        needle = folder.lower()
        items = [
            r for r in items
            if isinstance(r, dict) and needle in str(r.get("folder_name") or r.get("folder") or "").lower()
        ]
    summary = collect_keys(
        items, ["id", "name", "folder_id", "folder_name", "cube_name", "report_type", "updated_at"]
    )
    return {"account_id": aid, "count": len(summary), "reports": summary}


@mcp.tool()
async def get_report_definition(
    report_id: str | int,
    account_id: str | int | None = None,
) -> Any:
    """[experimental] Return the full JSON definition of a report.

    Includes the MDX-equivalent rows/columns/pages, calculated members,
    and chart options. Use this to inspect *how* a report is constructed
    (instead of running it via :func:`export_report`).
    """
    aid = _resolve_account_id(account_id)
    return await _safe_experimental(lambda: _get_client().get_report(aid, report_id))


# ---------------------------------------------------------------------------
# 3. Dashboards — experimental
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_dashboards(account_id: str | int | None = None) -> dict[str, Any]:
    """[experimental] List dashboards in an account."""
    aid = _resolve_account_id(account_id)
    raw = await _safe_experimental(lambda: _get_client().list_dashboards(aid))
    if isinstance(raw, dict) and raw.get("supported") is False:
        return raw
    items = raw if isinstance(raw, list) else (raw or {}).get("dashboards", [])
    summary = collect_keys(items, ["id", "name", "folder_id", "folder_name", "updated_at"])
    return {"account_id": aid, "count": len(summary), "dashboards": summary}


@mcp.tool()
async def get_dashboard(
    dashboard_id: str | int,
    account_id: str | int | None = None,
) -> Any:
    """[experimental] Return a dashboard's layout and the IDs of the reports it uses.

    Note: eazyBI does not provide a public API to export *dashboard* data as
    a single JSON payload — the export endpoint only supports individual
    reports. Use this tool to discover the constituent ``report_id``s and
    then call :func:`export_report` for each one.
    """
    aid = _resolve_account_id(account_id)
    return await _safe_experimental(lambda: _get_client().get_dashboard(aid, dashboard_id))


# ---------------------------------------------------------------------------
# 4. Cubes & metadata — experimental
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_cubes(account_id: str | int | None = None) -> dict[str, Any]:
    """[experimental] List OLAP cubes available in an account."""
    aid = _resolve_account_id(account_id)
    raw = await _safe_experimental(lambda: _get_client().list_cubes(aid))
    if isinstance(raw, dict) and raw.get("supported") is False:
        return raw
    items = raw if isinstance(raw, list) else (raw or {}).get("cubes", [])
    summary = collect_keys(items, ["name", "caption", "description"])
    return {"account_id": aid, "count": len(summary), "cubes": summary}


@mcp.tool()
async def get_cube(
    cube_name: str,
    account_id: str | int | None = None,
) -> Any:
    """[experimental] Full cube definition: dimensions, hierarchies, measures."""
    aid = _resolve_account_id(account_id)
    return await _safe_experimental(lambda: _get_client().get_cube(aid, cube_name))


@mcp.tool()
async def list_dimensions(
    cube_name: str,
    account_id: str | int | None = None,
) -> dict[str, Any]:
    """[experimental] Dimensions of a cube (extracted from get_cube)."""
    aid = _resolve_account_id(account_id)
    cube = await _safe_experimental(lambda: _get_client().get_cube(aid, cube_name))
    if isinstance(cube, dict) and cube.get("supported") is False:
        return cube
    cube = cube or {}
    dims = cube.get("dimensions") or []
    summary = collect_keys(dims, ["name", "caption", "description", "type"])
    return {"account_id": aid, "cube": cube_name, "count": len(summary), "dimensions": summary}


@mcp.tool()
async def list_measures(
    cube_name: str,
    account_id: str | int | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """[experimental] Measures (and calculated measures) of a cube.

    Args:
        folder: optional case-insensitive substring filter on measure folder.
    """
    aid = _resolve_account_id(account_id)
    cube = await _safe_experimental(lambda: _get_client().get_cube(aid, cube_name))
    if isinstance(cube, dict) and cube.get("supported") is False:
        return cube
    cube = cube or {}
    measures = cube.get("measures") or []
    if folder:
        needle = folder.lower()
        measures = [
            m for m in measures
            if isinstance(m, dict) and needle in str(m.get("folder") or "").lower()
        ]
    summary = collect_keys(measures, ["name", "caption", "description", "folder", "calculated", "format"])
    return {"account_id": aid, "cube": cube_name, "count": len(summary), "measures": summary}


@mcp.tool()
async def list_hierarchies(
    cube_name: str,
    dimension_name: str,
    account_id: str | int | None = None,
) -> dict[str, Any]:
    """[experimental] Hierarchies of a single dimension."""
    aid = _resolve_account_id(account_id)
    cube = await _safe_experimental(lambda: _get_client().get_cube(aid, cube_name))
    if isinstance(cube, dict) and cube.get("supported") is False:
        return cube
    cube = cube or {}
    dims = cube.get("dimensions") or []
    target = next(
        (d for d in dims if isinstance(d, dict) and d.get("name") == dimension_name),
        None,
    )
    if target is None:
        raise EazyBIError(
            f"Dimension {dimension_name!r} not found in cube {cube_name!r}. "
            f"Available: {[d.get('name') for d in dims if isinstance(d, dict)]}"
        )
    hierarchies = target.get("hierarchies") or []
    summary = collect_keys(hierarchies, ["name", "caption", "all_member_name", "default_member"])
    return {
        "account_id": aid,
        "cube": cube_name,
        "dimension": dimension_name,
        "count": len(summary),
        "hierarchies": summary,
    }
