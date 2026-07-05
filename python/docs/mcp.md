# MCP server

`yente-mcp` exposes the OpenSanctions matching surface to LLM agents over
the [Model Context Protocol](https://modelcontextprotocol.io/). An assistant
connected to it can screen people, companies, and other entities against
sanctions, PEP, and watchlist data, walk relationship graphs, and audit
where a claim came from — with the same match-first semantics as the SDK
and CLI.

!!! note "Status"
    The MCP server is new; tool response shapes may still change between
    releases.

## Install and run

```bash
pip install 'yente-client[mcp]'
export OPENSANCTIONS_API_KEY=...   # fallback key, handy for local runs
yente-mcp
```

The server speaks streamable HTTP and binds `127.0.0.1:8000` by default; the
MCP endpoint is `http://127.0.0.1:8000/mcp`. Configuration, by flag or
environment variable:

| Flag | Env var | Default | Notes |
| --- | --- | --- | --- |
| `--transport` | `YENTE_MCP_TRANSPORT` | `http` | `http` or `stdio` (local development). |
| `--host` | `YENTE_MCP_HOST` | `127.0.0.1` | Bind host for the HTTP transport. |
| `--port` | `YENTE_MCP_PORT` | `8000` | Bind port for the HTTP transport. |
| — | `YENTE_BASE_URL` | `https://api.opensanctions.org` | The yente / OpenSanctions API the server forwards calls to. |
| — | `OPENSANCTIONS_API_KEY` | unset | Fallback key when a request carries no bearer token. |

## Authentication

The server holds no secret of its own. Each MCP request carries the caller's
OpenSanctions API key as `Authorization: Bearer <key>`, and the server
forwards it downstream — validation, quota, and billing happen at the API
gateway, and the caller screens as themselves. A request without a bearer
token falls back to the server's `OPENSANCTIONS_API_KEY`; against an open
self-hosted yente, no key is needed at all.

## Connecting a client

Claude Code:

```bash
claude mcp add --transport http yente http://127.0.0.1:8000/mcp \
    --header "Authorization: Bearer $OPENSANCTIONS_API_KEY"
```

Any MCP client that supports streamable HTTP connects the same way: point it
at the `/mcp` endpoint and set the bearer header.

## Tools

Screening and investigation:

| Tool | Purpose |
| --- | --- |
| `match_entity` | Screen one entity, query-by-example; returns scored candidates. Use for any matching task, even with partial input. |
| `search_entities` | Free-text search for end-user search UIs; returns unscored entities. |
| `fetch_entity_by_id` | One entity's full record, by OpenSanctions ID. |
| `fetch_entity_relations` | Walk sanctions, ownership, and family edges, paginated. |
| `fetch_entity_statements` | Statement-level provenance: which source asserted a value (hosted API only). |

Vocabulary and metadata lookups:

| Tool | Purpose |
| --- | --- |
| `describe_schema` | FtM schemata and their properties (offline, bundled model). |
| `describe_topics` | The risk-topic vocabulary (offline). |
| `describe_countries` | The country vocabulary (offline). |
| `describe_dataset` | Dataset metadata from the live catalog. |
| `describe_program` | Sanctions-program metadata behind `programId` codes. |

Results resolve their own codes: topic and country labels are attached
inline from the bundled FtM model, and dataset / program titles ride along
as legends. Agents don't have to guess what an identifier like `crime.war`
means — the label is next to the code.

Matching algorithms are deliberately not exposed. Algorithm choice is a
tuning decision below the assistant's altitude; the server default applies.
Use the [SDK](tutorial.md) or [CLI](cli.md) to experiment with algorithms.

## Branding a self-hosted deployment

`YENTE_MCP_NAME` and `YENTE_MCP_INSTRUCTIONS` override the name and
instructions the server advertises to MCP clients, so a self-hosted yente
deployment can present its own identity without forking the server.
