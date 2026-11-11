"""yente-client MCP server.

Exposes the matching / search / entity-lookup / schema surface as Model Context
Protocol tools and resources, so an LLM agent can screen and research entities
against the OpenSanctions database without hand-assembling HTTP requests.

Built on :class:`yente_client.AsyncClient` and run as a standalone
streamable-HTTP service (``yente-mcp``). Install the extra:
``pip install 'yente-client[mcp]'``.

Module map:

- :mod:`yente_client.mcp.shaping` — trims response entities to the compact,
  decision-relevant views the tools return.
- :mod:`yente_client.mcp.legend` — resolves the codes a response mentions into its
  ``legend``.
- :mod:`yente_client.mcp.auth` — per-request API-key → ``AsyncClient`` plumbing.
- :mod:`yente_client.mcp.server` — the FastMCP instance and tool/resource wiring.
- :mod:`yente_client.mcp.main` — the ``yente-mcp`` entry point.
"""
