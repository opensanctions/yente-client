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
| `match_entity` | Screen one entity, query-by-example; returns scored candidates with the evidence behind each score. Use for any matching task, even with partial input. |
| `search_entities` | Keyword browsing; returns unscored entities. Never a way to decide whether someone is listed. |
| `fetch_entity_by_id` | Profile one entity by OpenSanctions ID: its record, its sanctions, risk listings and positions held in full, and a summary of its other relationships. |
| `fetch_entity_descriptions` | What sources write about an entity, verbatim: sanction and risk reasons, then descriptions and biography notes, each credited to its source dataset. |
| `fetch_entity_relations` | Page through one relationship type (ownership, directorships, family, …) in full. |
| `fetch_entity_statements` | Statement-level provenance: which source asserted a value, and when (hosted API only). |

Vocabulary and metadata lookups:

| Tool | Purpose |
| --- | --- |
| `describe_schema` | FtM schemata and their properties (offline, bundled model). |
| `describe_topics` | What topic tags mean and how they filter, plus every code with its label (offline). |
| `describe_countries` | The country vocabulary (offline). |
| `describe_dataset` | One dataset's metadata from the live catalog, by name. |
| `describe_program` | One sanctions program's metadata, by its `programId` key. |

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
  echoes the dataset and topics that were screened, and `as_of`, when the
  data was last updated.
- **Lists** carry `total`, `returned` and `next_offset`. A `next_offset` of
  `null` means the list is complete.
- **Relationship edges keep their own details** (role, share, relationship,
  dates). The entity at the far end is a reference: `{id, caption, schema,
  topics}` marked `abridged: true`, enough to spot a sanctioned neighbour. Profiling that `id` gives
  its record. A position's post is part of the position held, so it keeps
  key properties such as its country. Only top-level records carry
  `first_seen` and `last_change`.
- **Prose has its own tool.** Biography notes and designation reasons are
  left out of every other response; the profile counts them under
  `descriptions`, and `fetch_entity_descriptions` returns them with
  per-source credits, official sources first.
- **Sanctions and risk listings stay separate.** A sanction imposes a
  restriction. A risk listing (a watch list, an advisory, a persons-of-interest
  register) names the entity without one.

Matching algorithms are deliberately not exposed. Algorithm choice is a
tuning decision below the assistant's altitude; the server default applies.
Use the [SDK](tutorial.md) or [CLI](cli.md) to experiment with algorithms.

## Branding a self-hosted deployment

`YENTE_MCP_NAME` and `YENTE_MCP_INSTRUCTIONS` override the name and
instructions the server advertises to MCP clients, so a self-hosted yente
deployment can present its own identity without forking the server.
