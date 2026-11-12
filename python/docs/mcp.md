# MCP server

`yente-mcp` exposes the OpenSanctions matching surface to LLM agents over
the [Model Context Protocol](https://modelcontextprotocol.io/). An assistant
connected to it can screen people, companies, and other entities against
sanctions, PEP, and watchlist data, walk relationship graphs, and audit
where a claim came from — with the same match-first semantics as the SDK
and CLI.

!!! note "Status"
    Tool response shapes may still change between releases.

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
| — | `YENTE_MCP_DATASET` | `default` | Collection the tools query unless a call names another, and whose update time responses report as `as_of`. Set it to the scope your yente loads. |
| — | `YENTE_MCP_OAUTH_ISSUER` | unset | OAuth2 issuer whose access tokens the server accepts. See [OAuth2 login](#oauth2-login). |
| — | `YENTE_MCP_PUBLIC_URL` | unset | Public root URL of the server, e.g. `https://mcp.example.com`. Needed with `YENTE_MCP_OAUTH_ISSUER`. |

The HTTP transport is stateless, so any instance behind a load balancer can
answer any request.

## Authentication

The server needs no secret of its own. Each MCP request carries the caller's
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
- answers any other request, an API key included, with `401` and a
  `WWW-Authenticate` header that points at the metadata. The client then logs
  in or refreshes its token.

The tools forward the access token to the API. The server never uses
`OPENSANCTIONS_API_KEY` in this mode. The issuer must put
`$YENTE_MCP_PUBLIC_URL/mcp` into the access token's `aud`, and the API must
accept the access tokens. To use an API key instead, run `yente-mcp` yourself
without OAuth2.

This token pass-through is a deliberate difference from the MCP specification.
It is safe only when the MCP server and the API have the same operator and the
same issuer.

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
| `match_entity` | Screen one entity, query-by-example; returns scored candidates with the evidence behind each score. Use for any matching task, even with partial input. |
| `search_entities` | Keyword browsing; returns unscored entities. Never a way to decide whether someone is listed. |
| `fetch_entity_by_id` | Profile one entity by OpenSanctions ID: its record, and a preview of its sanctions, risk listings, positions held and other relationships. |
| `fetch_entity_descriptions` | What sources write about an entity, verbatim: sanction and risk reasons, then descriptions and biography notes, each credited to its source dataset. |
| `fetch_entity_relations` | Page through one relationship type (ownership, directorships, family, …) in full. |
| `fetch_entity_statements` | Statement-level provenance: which source asserted a value, and when (hosted API only). |

`describe(kind, name)` looks up vocabulary and source metadata:

| `kind` | Purpose |
| --- | --- |
| `schema` | FtM schemata, or one schema's properties and relations by `name` (offline, bundled model). |
| `topics` | What topic tags mean and how they filter, plus every code with its label (offline). |
| `countries` | The country vocabulary (offline). |
| `dataset` | One dataset's metadata from the live catalog, by `name`. |
| `program` | One sanctions program's metadata, by its `programId` key as `name`. |

Results resolve their own codes, so agents don't have to guess what an
identifier like `crime.war` or `us_ofac_sdn` means. Topic labels sit inline
on each entity. Every other code in a response resolves through a single
`legend` attached to it. The legend covers only the codes that response
contains, with the same sections in every tool:

- `schemata`: each schema's label, plural and ancestors, so a `Company`
  reads as a kind of `LegalEntity`.
- `properties`: label, value type and, for relationships, the schema they
  point at.
- One section per coded value type, such as `countries`, `languages` and
  `genders`.
- `datasets`: title, publisher and `official`. `official: false` marks a source whose publisher
  isn't an official body (aggregators, NGOs, Wikidata), like the "Unofficial"
  badge on the website.
- `programs`: the sanctions programs behind `programId` codes, with title,
  issuer and territory.
- `features`: what each scoring feature in a match explanation compares.

The legend also carries `links`, templates for citing an entity, dataset or
program on the OpenSanctions website. Links only appear when the server runs
against the hosted OpenSanctions API.

Every response follows the same shape:

- **Match candidates** carry an `explanation` with the scoring features and
  the query and candidate values each one compared, such as two birth dates
  that disagree. When nothing clears the cutoff, the closest candidates come
  back as `near_misses`, so a "no match" can still be explained. `checked`
  echoes the scope that was screened: the dataset, and any topics, threshold
  or cutoff the call set.
- **Every entity response is dated.** The `legend` carries `as_of`, when the
  data was last updated.
- **Lists** carry `total`, `returned` and `next`, the exact call for the
  next page, e.g. `fetch_entity_relations(entity_id="Q7747",
  prop="sanctions", offset=3)`. A `next` of `null` means the list is
  complete. Search results, one relation type and statements page 10 at a
  time by default, at most 25; a profile previews 3 records per section and
  relationship type.
- **Entities to look up next are abridged.** Match candidates, search
  results and the entity at the far end of a relationship carry their ID,
  caption, schema and topic labels, plus `property_count`, `value_count` and
  `dataset_count` for what the full record holds. `abridged` names the call
  that returns that record, e.g. `fetch_entity_by_id(entity_id="Q4398633")`. A match
  candidate's explanation still shows the values the matcher compared.
- **Relationship edges keep their own details** (role, share, relationship,
  dates). A position's post is part of the position held, so it keeps
  key properties such as its country. Only top-level records carry
  `first_seen` and `last_change`.
- **Prose has its own tool.** Biography notes and designation reasons are
  left out of every other response. A profile's `narrative_text` names the
  `fetch_entity_descriptions` call that returns them with per-source
  credits, official sources first.
- **Sanctions and risk listings stay separate.** A sanction imposes a
  restriction. A risk listing (a watch list, an advisory, a persons-of-interest
  register) names the entity without one.

Matching algorithms are deliberately not exposed. Algorithm choice is a
tuning decision below the assistant's altitude; the server default applies.
Use the [SDK](tutorial.md) or [CLI](cli.md) to experiment with algorithms.

## Branding a self-hosted deployment

These variables override what the server advertises to MCP clients, so a
self-hosted yente deployment can present its own identity without forking the
server:

| Env var | Default | Notes |
| --- | --- | --- |
| `YENTE_MCP_NAME` | `yente` | Server identifier. |
| `YENTE_MCP_TITLE` | `Yente` | Display name; clients show it in place of the identifier. |
| `YENTE_MCP_DESCRIPTION` | stock one-liner | Short description of the server. |
| `YENTE_MCP_INSTRUCTIONS` | stock copy | Instructions the assistant reads before using the tools. |
