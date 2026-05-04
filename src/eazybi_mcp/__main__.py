"""Entry point for `python -m eazybi_mcp` and the `eazybi-mcp` script."""

from __future__ import annotations

from .server import mcp


def main() -> None:
    """Run the MCP server over stdio (default transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
