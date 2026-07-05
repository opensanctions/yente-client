"""``yente-mcp`` entry point.

Runs the FastMCP server. Defaults to streamable-HTTP (the v1 deliverable);
``--transport stdio`` is a local-dev convenience. ``main()`` is what
``pyproject.toml``'s ``[project.scripts]`` calls.

Config (all overridable by flag): ``YENTE_MCP_TRANSPORT`` (http|stdio),
``YENTE_MCP_HOST``, ``YENTE_MCP_PORT``, ``YENTE_BASE_URL`` (the yente the server
forwards calls to), and ``OPENSANCTIONS_API_KEY`` (fallback key used when a
request carries no bearer token) — the latter two read in
:mod:`yente_client.mcp.server`.

Branding for self-hosted deployments: ``YENTE_MCP_NAME`` and
``YENTE_MCP_INSTRUCTIONS`` override the name and description advertised to MCP
clients (default ``yente`` / the stock copy); resolved in :mod:`yente_client.env`.
"""

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="yente-mcp",
        description="MCP server for the yente / OpenSanctions matching API.",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("YENTE_MCP_TRANSPORT", "http"),
        help="Transport to serve (default: http / streamable-HTTP).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("YENTE_MCP_HOST", "127.0.0.1"),
        help="Bind host for the http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("YENTE_MCP_PORT", "8000")),
        help="Bind port for the http transport.",
    )
    args = parser.parse_args()

    # Imported lazily so `--help` works without FastMCP installed.
    from yente_client.mcp.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
