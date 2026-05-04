# eazybi-mcp

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-1.x-purple.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A read-only **[Model Context Protocol](https://modelcontextprotocol.io/)** server that lets LLMs query [eazyBI — Reports & Charts for Jira](https://eazybi.com/products/eazybi-reports-and-charts-for-jira). Plug it into Claude Desktop, Claude Code, Cursor, or any other MCP-compatible client and ask things like:

> *"Pull the Cycle Time report for Q1 2026 from eazyBI and tell me what changed."*
> *"List the dashboards in account 250143 and summarise the velocity ones."*
> *"Export report 5259102 as CSV to my Downloads folder."*

The server is intentionally read-only — it cannot modify reports, dashboards, or data — so giving it an API token is low-risk.

## Why

eazyBI builds incredibly powerful Jira analytics, but its data lives behind an OLAP cube and a UI. If you want an LLM to reason over your team's velocity, cycle time, throughput, or any custom metric you've already modeled in eazyBI, you have two options: re-implement the analysis against the Jira API (slow, brittle, ignores your calculated members), or run the saved report you already have and feed the result into the LLM. This server does the second.

## What's stable vs experimental

eazyBI publishes exactly **one** REST endpoint as part of its supported public API: the [Report Results Export API](https://docs.eazybi.com/eazybi/set-up-and-administer/customization/report-results-export-api). That is what `export_report` and `get_export_url` use, and on **eazyBI for Jira Cloud it is the only thing that accepts Basic auth** with an Atlassian API token — every other route is served behind Atlassian Connect JWT inside the eazyBI iframe.

The remaining `list_*` / `get_*` tools call internal JSON routes that the eazyBI UI consumes (`/accounts.json`, `/accounts/{id}/cubes.json`, …). They work fine on Data Center / Private eazyBI but **return `{"supported": false}` on Atlassian Cloud** — that's expected, not a bug. Each experimental tool is marked `[experimental]` in its docstring.

## Tools

| Tool | Status | What it does |
|---|---|---|
| `export_report` | stable | Run a saved report by ID and return data as JSON / CSV / XLS / PDF / PNG. Supports `selected_pages` for page-filter overrides and `embed_token` for publicly shared reports. |
| `get_export_url` | stable | Build the canonical export URL for a report — no HTTP request made. |
| `list_accounts` | experimental | List eazyBI accounts visible to the authenticated user. |
| `list_reports` | experimental | List reports in an account, optional folder filter. |
| `get_report_definition` | experimental | Full JSON definition of a report (rows / columns / pages / calculated members). |
| `list_dashboards` | experimental | List dashboards in an account. |
| `get_dashboard` | experimental | Layout of a dashboard and the IDs of the reports it contains. |
| `list_cubes` | experimental | List OLAP cubes available in an account. |
| `get_cube` | experimental | Full cube metadata: dimensions, hierarchies, measures. |
| `list_dimensions` | experimental | Dimensions of a cube. |
| `list_measures` | experimental | Measures of a cube, optional folder filter. |
| `list_hierarchies` | experimental | Hierarchies of a single dimension. |

## Quickstart

Pick one of the install options below, then hand the binary to your MCP client.

### Install with `uv` (recommended)

```bash
brew install uv     # one-time
uv tool install --from git+https://github.com/ultimate-guitar/eazybi-mcp eazybi-mcp
```

### Install with `pipx`

```bash
pipx install git+https://github.com/ultimate-guitar/eazybi-mcp
```

### From source

```bash
git clone https://github.com/ultimate-guitar/eazybi-mcp
cd eazybi-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Configure

Copy `.env.example` to `.env` (or set the variables in your MCP client's `env` block) and fill in:

```env
EAZYBI_BASE_URL=https://aod.eazybi.com         # eazyBI for Jira Cloud
[email protected]
EAZYBI_API_TOKEN=your-atlassian-api-token      # https://id.atlassian.com/manage-profile/security/api-tokens
EAZYBI_DEFAULT_ACCOUNT_ID=12345                # optional, looked up from the URL after /accounts/
```

| Deployment | `EAZYBI_BASE_URL` | Auth |
|---|---|---|
| eazyBI for Jira Cloud | `https://aod.eazybi.com` | Atlassian email + [API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| eazyBI for Jira / Confluence Data Center | `https://your-jira.example.com/plugins/servlet/eazybi` | Jira/Confluence username + password (or PAT) |
| [eazybi.com](https://eazybi.com) (SaaS) | `https://eazybi.com` | eazyBI account email + password |

> **Cloud users:** the URL inside the eazyBI iframe in Jira looks like `https://aod.eazybi.com/eazy/accounts/.../...`. The `/eazy` segment is for UI routes only — set `EAZYBI_BASE_URL` **without** it. The export endpoint lives at the bare host.

> **Public reports:** if a report is shared via "Embed report → Public access token", pass `embed_token="..."` to `export_report` and skip Basic auth.

### Where to find `account_id` and `report_id`

Open a report in eazyBI; the URL looks like `…/accounts/250143/cube_reports/5259102` (or `…/reports/5259102-foo`). The first number is `account_id`, the second is `report_id`. The "Embed report" action in the eazyBI toolbar surfaces both as well.

## Wire it into a client

### Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "eazybi": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ultimate-guitar/eazybi-mcp", "eazybi-mcp"],
      "env": {
        "EAZYBI_BASE_URL": "https://aod.eazybi.com",
        "EAZYBI_USER": "[email protected]",
        "EAZYBI_API_TOKEN": "your-atlassian-api-token",
        "EAZYBI_DEFAULT_ACCOUNT_ID": "12345"
      }
    }
  }
}
```

For a globally-installed binary use `"command": "eazybi-mcp", "args": []` instead.

After editing, **fully quit Claude Desktop** (Cmd+Q on macOS) and reopen it — closing the window is not enough.

### Claude Code

```bash
claude mcp add eazybi -- eazybi-mcp \
  --env EAZYBI_BASE_URL=https://aod.eazybi.com \
  --env [email protected] \
  --env EAZYBI_API_TOKEN=your-atlassian-api-token
```

### Cursor / Continue / any MCP client

Point the client at the `eazybi-mcp` binary; pass the env vars in whatever way your client supports.

## Sample prompts

Once wired up:

- *"What's the P85 cycle time for Q1 2026? Pull report 5259102 from eazyBI."*
- *"List all reports in the 'Velocity' folder of my eazyBI account."*
- *"Export report 5259102 as CSV and save to ~/Downloads/cycle-time.csv."*
- *"Compare the last three quarters of cycle time and tell me if the trend is up or down."*

The LLM picks tools automatically. For Cloud users only `export_report` is reliable — that's enough for most reporting tasks.

## Driving the client directly (for tests / scripts)

```python
import asyncio
from eazybi_mcp.client import EazyBIClient, EazyBIConfig

async def main():
    client = EazyBIClient(EazyBIConfig.from_env())
    payload, ctype = await client.export_report(
        account_id=250143,
        report_id=5259102,
        fmt="json",
    )
    print(ctype, len(payload))
    await client.aclose()

asyncio.run(main())
```

## Limitations

- eazyBI does not expose a public API to fetch *dashboard* data as a single payload. Use `get_dashboard` (Data Center / Private only) to discover the constituent report IDs, then call `export_report` for each.
- Arbitrary MDX execution is intentionally not wired up — eazyBI does not document a public endpoint for it. If your private deployment exposes one, extend `EazyBIClient` accordingly.
- Large JSON exports are truncated by the `max_chars` parameter. Pass `save_to=/path/to/file.json` to write the full payload to disk.
- This is an unofficial integration. eazyBI may change the experimental routes at any time; the stable export endpoint is the long-term contract.

## Contributing

Issues and PRs welcome. Please don't commit your `.env` or any token — the project's `.gitignore` excludes `.env` precisely for that reason. To add a new tool:

1. Add the HTTP call to `EazyBIClient` in `src/eazybi_mcp/client.py`.
2. Wrap it as a `@mcp.tool()` in `src/eazybi_mcp/server.py`.
3. Mark it `[stable]` only if it hits a documented public endpoint; otherwise `[experimental]` and route it through `_safe_experimental` so 401/404 responses degrade gracefully.

## License

MIT
