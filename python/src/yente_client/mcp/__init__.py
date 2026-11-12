"""yente-client MCP server.

Exposes the matching / search / entity-lookup / schema surface as Model Context
Protocol tools, so an LLM agent can screen and research entities
against the OpenSanctions database without hand-assembling HTTP requests.

Built on :class:`yente_client.AsyncClient` and run as a standalone
streamable-HTTP service (``yente-mcp``). Install the extra:
``pip install 'yente-client[mcp]'``.

Module map:

- :mod:`yente_client.mcp.shaping` — trims response entities to the compact,
  decision-relevant views the tools return.
- :mod:`yente_client.mcp.legend` — resolves the codes a response mentions into its
  ``legend``.
- :mod:`yente_client.mcp.errors` — turns SDK errors into tool-error messages
  that say whether a retry can help.
- :mod:`yente_client.mcp.auth` — reads the caller's bearer token.
- :mod:`yente_client.mcp.server` — the FastMCP instance and tool wiring.
- :mod:`yente_client.mcp.main` — the ``yente-mcp`` entry point.
"""
