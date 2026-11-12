"""``yente-mcp`` entry point.

Runs the FastMCP server. Defaults to streamable-HTTP; ``--transport stdio`` is
a local-dev convenience. ``main()`` is what ``pyproject.toml``'s
``[project.scripts]`` calls.

Config, all read via :mod:`yente_client.env`: ``YENTE_MCP_TRANSPORT``
(http|stdio), ``YENTE_MCP_HOST`` and ``YENTE_MCP_PORT``, each overridable by
flag; ``YENTE_BASE_URL`` (the yente the server forwards calls to),
``OPENSANCTIONS_API_KEY`` (fallback key used when a request carries no bearer
token), ``YENTE_MCP_DATASET`` (the collection tools query by default), and
``YENTE_MCP_OAUTH_ISSUER`` / ``YENTE_MCP_PUBLIC_URL`` (OAuth2 login).

Branding for self-hosted deployments: ``YENTE_MCP_NAME`` and
``YENTE_MCP_INSTRUCTIONS`` override the name and description advertised to MCP
clients (default ``yente`` / the stock copy).
"""

import argparse

from yente_client import env


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="yente-mcp",
        description="MCP server for the yente / OpenSanctions matching API.",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=env.mcp_transport(),
        help="Transport to serve (default: http / streamable-HTTP).",
    )
    parser.add_argument(
        "--host",
        default=env.mcp_host(),
        help="Bind host for the http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env.mcp_port(),
        help="Bind port for the http transport.",
    )
    args = parser.parse_args()

    # Imported lazily so `--help` works without FastMCP installed.
    from yente_client.mcp.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Stateless: any instance behind a load balancer can answer any
        # request. The tools need no session state.
        mcp.run(transport="http", host=args.host, port=args.port, stateless_http=True)


if __name__ == "__main__":
    main()
