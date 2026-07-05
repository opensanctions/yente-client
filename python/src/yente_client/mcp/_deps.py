"""Centralized import of MCP-only dependencies (FastMCP).

The MCP extra (``pip install yente-client[mcp]``) pulls in FastMCP. Routing the
import through one module turns a missing-extra into a single-line install hint
instead of a raw :class:`ImportError` traceback — mirrors
:mod:`yente_client.cli._deps`.

MCP modules import FastMCP names from here, not from ``fastmcp`` directly.
"""

import sys


def _bail(missing: str) -> None:
    sys.stderr.write(
        f"The yente-mcp server requires the '{missing}' package.\n"
        "Install the MCP extra:\n"
        "  pip install 'yente-client[mcp]'\n"
    )
    sys.exit(127)


try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_headers
except ImportError:
    _bail("fastmcp")


__all__ = ["FastMCP", "ToolError", "get_http_headers"]
