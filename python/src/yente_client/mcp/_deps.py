"""Centralized import of MCP-only dependencies (FastMCP).

The MCP extra (``pip install yente-client[mcp]``) pulls in FastMCP. Routing the
import through one module turns a missing-extra into a single-line install hint
instead of a raw :class:`ImportError` traceback — mirrors
:mod:`yente_client.cli._deps`.

MCP modules import FastMCP and MCP SDK names from here, not from ``fastmcp``
or ``mcp`` directly.
"""

import sys


def _bail(missing: str) -> None:
    sys.stderr.write(
        f"The yente-mcp server requires the '{missing}' package.\n"
        "Run it with the MCP extra:\n"
        "  uvx --from 'yente-client[mcp]' yente-mcp\n"
        "or install the extra:\n"
        "  pip install 'yente-client[mcp]'\n"
    )
    sys.exit(127)


try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier
    from fastmcp.server.dependencies import get_http_headers
except ImportError:
    _bail("fastmcp")

# FastMCP depends on the MCP SDK, so this only fails on a broken install.
try:
    from mcp.types import Icon, ToolAnnotations
except ImportError:
    _bail("mcp")


__all__ = [
    "FastMCP",
    "Icon",
    "JWTVerifier",
    "RemoteAuthProvider",
    "ToolAnnotations",
    "ToolError",
    "get_http_headers",
]
