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
export OPENSANCTIONS_API_KEY=...   # fallback key, handy for local runs
uvx --from 'yente-client[mcp]' yente-mcp
```

Other ways to run it:

```bash
# Install it on your PATH as a uv tool:
uv tool install 'yente-client[mcp]'

# With pip, in a virtual environment:
pip install 'yente-client[mcp]'
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
| — | `OPENSANCTIONS_API_KEY` | unset | Fallback key when a request carries no bearer token. Not used with OAuth2. |
| — | `YENTE_MCP_OAUTH_ISSUER` | unset | OAuth2 issuer whose access tokens the server accepts. See [OAuth2 login](#oauth2-login). |
| — | `YENTE_MCP_PUBLIC_URL` | unset | Public root URL of the server, e.g. `https://mcp.example.com`. Needed with `YENTE_MCP_OAUTH_ISSUER`. |

The HTTP transport is stateless, so any instance behind a load balancer can
answer any request.

## Authentication

The server holds no secret of its own. Each MCP request carries the caller's
OpenSanctions API key as `Authorization: Bearer <key>`, and the server
forwards it downstream — validation, quota, and billing happen at the API
gateway, and the caller screens as themselves. A request without a bearer
token falls back to the server's `OPENSANCTIONS_API_KEY`; against an open
self-hosted yente, no key is needed at all.

### OAuth2 login

MCP clients that support OAuth2 can log in with an OAuth2 issuer, for example
Ory. Set `YENTE_MCP_OAUTH_ISSUER` and
`YENTE_MCP_PUBLIC_URL` to run the server as an OAuth2 resource server. The
server then:

- serves the protected resource metadata (RFC 9728) at
  `/.well-known/oauth-protected-resource/mcp`. It names the issuer, and its
  `resource` is `$YENTE_MCP_PUBLIC_URL/mcp`;
- checks access tokens (JWTs) itself: signature (from the issuer's
  `/.well-known/jwks.json`), issuer, expiry and audience. The audience must be
  `$YENTE_MCP_PUBLIC_URL/mcp`, which MCP clients request as the RFC 8707
  `resource`;
- answers any other request, an API key included, with `401` and a `WWW-Authenticate` header that points at the metadata. The
  client then logs in or refreshes its token.

The tools forward the access token to the API. The server never uses
`OPENSANCTIONS_API_KEY` in this mode. The issuer must put
`$YENTE_MCP_PUBLIC_URL/mcp` into the access token's `aud`, and the API must
accept the access tokens. To use an API key instead, run `yente-mcp` yourself
without OAuth2.

## Connecting a client

Claude Code:

```bash
claude mcp add --transport http yente http://127.0.0.1:8000/mcp \
    --header "Authorization: Bearer $OPENSANCTIONS_API_KEY"
```

Any MCP client that supports streamable HTTP connects the same way: point it
at the `/mcp` endpoint and set the bearer header. Against a server with
OAuth2 login, leave out the header, and the client asks you to log in.

For a local, single-user setup, the client can start the server itself over
stdio. In that mode no bearer header exists, so the server uses
`OPENSANCTIONS_API_KEY` from its environment:

```bash
claude mcp add yente --env OPENSANCTIONS_API_KEY=$OPENSANCTIONS_API_KEY \
    -- uvx --from 'yente-client[mcp]' yente-mcp --transport stdio
```

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
