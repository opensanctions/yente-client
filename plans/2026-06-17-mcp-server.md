---
description: Design for a standalone MCP server shipped in yente-client, exposing screening/search/entity tools to LLM agents over the yente SDK (AsyncClient).
date: 2026-06-17
tags: [yente, mcp, sdk, agents, design]
---

# yente-client MCP server

## Goal

Expose yente's matching and data-access capabilities to LLM agents through the
Model Context Protocol, so an agent can screen a name against sanctions lists,
search the entity graph, drill into a hit, and traverse relationships — without
a human assembling HTTP requests.

The MCP server is a third access form alongside the existing two, all thin over
one SDK:

- **`yente_client` SDK** — the library (`Client` / `AsyncClient`).
- **`yente-cli`** — human / skill-driven access (the `browse-opensanctions`
  skill rides this).
- **`yente-mcp`** — native tool-call access for agents. ← this doc.

## Why here, not mounted in yente

The earlier draft (in `../yente/plans/`, now removed) proposed mounting an MCP
transport inside yente's FastAPI app. We reversed that. Living in `yente-client`
as a **standalone server over `AsyncClient`** wins on every axis that matters:

- **Load balancing.** Every tool call is an ordinary stateless HTTP request to
  the yente base URL, distributed by the existing ingress/LB. The backend never
  sees "the MCP" — just requests it already balances.
- **Latency isolation.** The risky part of MCP traffic — long-lived, chatty,
  connection-holding agent sessions — lives entirely in the MCP process,
  decoupled from the latency-critical screening backend. No "separate replica
  pool" hack; the concern simply isn't in yente's process.
- **Zero blast radius on yente.** The FastMCP-in-FastAPI sharp edges (lifespan
  merge, sub-path SSE routing, app-wide CORS conflict) only existed because of
  mounting. Out here they don't apply. yente stays a lean screening backend with
  no MCP dependency.
- **No duplication.** Tools are thin adapters over `AsyncClient` methods that
  already own request construction, models, filters, and error handling. We also
  inherit the SDK's *product judgement* — see "search vs match" below.
- **Config falls out.** `AsyncClient(base_url=, api_key=)` already targets
  hosted (`https://api.opensanctions.org`, the default) or any self-hosted yente.
  "Configurable / both backends" is just how the SDK works.

Cost: one HTTP hop per tool call — negligible next to the agent's own inter-turn
LLM latency, and the in-process benefit was never about agent-perceived speed.

## Code layout & packaging

Mirror the existing `cli/` subpackage exactly. The CLI lives at
`src/yente_client/cli/` with entry point `yente-cli = yente_client.cli.main:main`
and a `[cli]` extra. The MCP server is its sibling:

```
python/src/yente_client/
  cli/            # existing
  mcp/            # new
    __init__.py
    main.py       # entry point: builds FastMCP server, picks transport, runs
    server.py     # FastMCP instance + tool/resource registration
    shaping.py    # Entity / ScoredEntity → trimmed dicts for the model
```

- **Dependency:** **FastMCP** (`fastmcp` package, gofastmcp.com) — the de facto
  standard for hand-built Python MCP servers; decorator API, stdio +
  streamable-HTTP. Built and verified against 3.4.2; pinned `>=3.4`. Not
  `fastapi-mcp` (auto-exposes routes; we want a curated surface). Added as a
  `[mcp]` optional-dependency extra, not a core dep, so plain
  `pip install yente-client` stays lean (today: `pydantic`, `httpx` only).
- **Entry point:** `[project.scripts]` gains
  `yente-mcp = "yente_client.mcp.main:main"`.
- **Python floor 3.11** (repo standard); FastMCP supports it.

> Note on "`yente-client/mcp`": the natural home is the **subpackage**
> `src/yente_client/mcp/` (sibling of `cli/`), *not* a new top-level dir parallel
> to `python/` and `typescript/`. The `cli/` precedent settles this. A TS MCP
> could follow later under `typescript/`, mirroring the dual-language SDK/CLI
> pattern — Python first.

## Transport

- **streamable-HTTP — required for v1.** A hosted, multi-tenant MCP. As a
  standalone service, this is just its own ASGI app (uvicorn serving FastMCP's
  `http_app()`) — no FastAPI-mounting complications, since we're not mounting
  into yente.
- **stdio** — optional local-dev convenience (`yente-mcp` against the hosted API
  with your own key). Not the deliverable.

`main.py` selects via flag/env (e.g. `--transport`, default `http`).

## Tool surface — curated, not a 1:1 wrap

yente has 19 endpoints; most are not things an LLM should call. The MCP surface
is **5 tools + 5 resources**: four tools are thin adapters over `AsyncClient`
methods, plus `describe_schema` over the bundled FtM model; two live `yente://`
resources and three static `ftm://` resources. Tool descriptions are part of the
contract (they steer the model) — drafts below.

**Naming.** Tool names track the SDK/API verbs (`match`, `search`, `fetch`,
`adjacent`), lightly adjusted to read clearly to a model — one vocabulary across
API, SDK, CLI, and MCP. Where it prevents a likely misuse, names are explicit:
`fetch_entity_by_id` / `fetch_entity_relations` carry the `by_id` signal so the
model doesn't reach for them with a name string (that's `match_entity` /
`search_entities`). The *intent* ("screen against sanctions lists") lives in the
tool description, where "screen" is fine as an English verb. The identifier
**`screen` is reserved** for a future API endpoint, so no tool is named it.

### `match_entity` → `AsyncClient.match(entity, ...)`

> Screen a single person, company, or organization against sanctions lists, PEP
> data and watchlists. Provide every structured detail you have — name plus any
> of date of birth, nationality, jurisdiction, registration number, address.
> Returns scored candidate matches with a per-feature explanation of *why* each
> scored as it did. Use this for **any** matching/screening task, even with only
> a partial record (just a name, or name + country). Prefer this over
> `search_entities` whenever the question is "is this person/company on a list?".

Inputs (model-friendly): `schema` (default `"Person"`), `properties`
(`{"name": [...], "birthDate": [...], ...}`), plus `threshold`, `algorithm`
(advertise `"best"` via `BEST_ALGORITHM`), `dataset`/`topics`/`include_dataset`
filters, `limit` (small, 3–5). The tool constructs the typed `EntityInput`
(`Person`, `Company`, …) from `schema` + `properties` and calls `match()`.

Inherited for free from the SDK: `_check_matchable_schema` raises
`ConfigurationError` *before* the round-trip for non-matchable schemas; the
"send every property you have, matchable or not" guidance. Returns
`MatchResponse` (list of `ScoredEntity`: `score`, `match`, `features`).

### `search_entities` → `AsyncClient.search(q, ...)`

> Free-text search over the OpenSanctions entity database for **end-user-style
> lookup** — browse/autocomplete where you have a keyword or name string and
> want to explore candidates. Returns plain entities (no score, no match flag).
> For a match/no-match judgement on a known individual — even with partial
> data — use `match_entity` instead.

This phrasing is lifted from the SDK's "search vs match" policy
(`CLAUDE.md`): never frame `search` as the fallback for `match` on incomplete
input. Inputs: `q`, `dataset`, `schema`, `countries`, `topics`, `limit`,
`offset`, `fuzzy`, `simple`. Returns `SearchResponse`.

### `fetch_entity_by_id` → `AsyncClient.fetch(entity_id, nested=True)`

> Fetch one entity by its OpenSanctions ID, including nested detail (sanctions,
> identifiers, addresses, relatives). Use to expand a candidate returned by
> `match_entity` or `search_entities` into full context.

The 308 entity-merge redirect is the SDK's concern, not the MCP's — another
benefit of building on the client. (Action item: confirm `AsyncClient.fetch`
follows merges to the canonical entity; if it surfaces a redirect/error, fix it
in the SDK so every consumer benefits.) Returns `Entity`.

### `fetch_entity_relations` → `AsyncClient.adjacent(entity_id, prop=, ...)` — Beta

> Traverse the relationship graph around an entity: owners, subsidiaries,
> associates, directorships, family. Paginated, for following ownership/control
> chains across multiple hops. Beta — shape may change.

Inputs: `entity_id`, optional `prop` (`"ownershipOwner"`,
`"directorshipDirector"`, …), `limit`, `offset`, `sort`. Returns
`AdjacentResponse` (all props) or `AdjacentPropertyResponse` (single prop).

### `describe_schema` → bundled FtM model (no server call)

> Look up the FollowTheMoney data model: which entity types (schemata) exist and
> what properties each has. Call with no argument for the index of matchable
> schemata; call with a name (e.g. `"Person"`, `"Company"`) for that schema's
> properties — their types, whether they're matchable, and for relationship
> properties what they point to. Use this *before* `match_entity` to fill
> `properties` with real field names (`birthDate`, `registrationNumber`,
> `nationality` — not `dob`/`reg_no`), and to learn valid `prop` values for
> `fetch_entity_relations`.

This is the FtM type system — the thing that makes the rest usable. Resolved
**locally** from the in-package snapshot (`schemas/model.json`); no yente call,
no latency, and version-pinned to the exact entity classes `AsyncClient.match`
accepts.

- No arg → index of matchable schemata: `name`, `label`, parents (inheritance,
  e.g. Person/Company → LegalEntity → Thing), `matchable`, one-line description.
- With `schema` → per-property detail: `name`, `label`, `type` (from the `types`
  registry — `name`, `date`, `country`, `topic`, `identifier`, `entity`, …),
  `matchable`, and for `entity`-typed properties the **range** (target schema)
  and reverse property.

Two nuances carried from the SDK's own guidance (`CLAUDE.md`): surface the
per-property `matchable` flag **but do not discourage non-matchable
properties** — `firstName`/`lastName`/`fatherName` feed name reconstruction and
`weakAlias`/`abbreviation` are cross-compared during scoring; the rule for the
model is "send every property you have." And per the repo's drift rule, output
is **derived from the bundled model at runtime** — never hardcode schema/topic
counts or enumerations.

### Resources

Two families, split by URI scheme:

**`ftm://*` — static type system**, resolved locally from `schemas/model.json`,
no server call. The read-only context mirror of `describe_schema` (same data;
exposed as resources too, for clients that pre-load context rather than calling
tools — see "resource vs tool" below):

- `ftm://schemata` — the matchable-schema index.
- `ftm://schema/{name}` — resource template; per-schema property detail (fetched
  one schema at a time, never the whole model as one blob).
- `ftm://topics`, `ftm://countries`, `ftm://genders` — the controlled
  vocabularies, from `model.types[...]` (topic values, country codes, genders).

**`yente://*` — live server state**, requires a call:

- `yente://catalog` → `AsyncClient.datasets()` (`DatasetsResponse`): which
  datasets exist, titles, freshness. Lets the agent pick `dataset` /
  `include_dataset` and know how current the data is.
- `yente://algorithms` → `AsyncClient.algorithms()` (`AlgorithmsResponse`):
  algorithm names + descriptions, so the scoring being invoked is legible.

Resources, not tools, for the slow-changing context a client reads once and
caches.

**Resource vs tool.** Resources have uneven client support and our consumers
skew autonomous-agent (the same logic that defers the prompt). So the FtM model
is **primarily a tool (`describe_schema`)** — reliably consumed, on-demand,
token-efficient — and *additionally* mirrored as `ftm://` resources, which is
cheap from the same bundled model and helps context-loading clients. The tool is
load-bearing; the `ftm://` resources are a bonus.

### Dropped

- `/reconcile/*` (6) — W3C/OpenRefine machine protocol, redundant with
  match/search for an LLM.
- `statements` — the SDK already documents this as OpenSanctions-API-only (yente
  returns 404; see `AsyncClient.statements`). Raw provenance, large, low LLM
  value. Out of v1; revisit only if a provenance use case appears.
- `healthz`/`readyz` — operational, not model-facing.

## Output shaping — the other half of the design

`Entity` / `ScoredEntity` are large (full nested provenance, every property,
referents, timestamps). Dumping raw JSON into an LLM context is expensive and
drowns the signal. `shaping.py` returns **trimmed** views by default:

- `id`, `caption`, `schema`, `datasets`, `topics`
- a small set of decision-relevant properties (name, birthDate, country,
  registrationNumber, sanction program…), not the full property bag
- `match_entity`: `score`, `match`, and a compact summary of the top
  contributing `features` (`FeatureResult`), not the full feature tree
- always the `id`, so the model can call `fetch_entity_by_id` for full detail on demand

`fetch_entity_by_id` is the one tool that returns rich nested data — and only when the
agent has decided a specific entity is worth the tokens. "List-cheap,
detail-on-demand" is the core ergonomic.

## Config & auth

Two distinct concerns the auth design has to satisfy:

- **A. Client → MCP** — who may use the MCP, and as whom.
- **B. MCP → yente** — the API key the server uses for the `AsyncClient` call
  (`base_url`, default `https://api.opensanctions.org`; `api_key`).

There are **two base auth modes**, selected by config (`YENTE_MCP_AUTH` /
`--auth`). A given *process* is in one mode; the hosted deployment composes both
on one URL via a dual-auth verifier (below). They differ only in how A→B is
resolved; everything downstream of "we have an API key" is shared.

### Mode `passthrough` (default) — covers no-auth *and* bearer-key

The bearer token **is** the OpenSanctions API key. The client sends
`Authorization: Bearer <key>`; the MCP reads it per-request and builds the
downstream `AsyncClient(api_key=<that key>)`. No FastMCP auth provider, no
validation in the MCP. This single path already covers two of the three modes we
care about, because an absent/blank token degrades to `api_key=None`:

- **No-auth (on-prem, open yente):** client sends no token → `None` → calls land
  unauthenticated against a gateway-less yente. Already implemented:
  `parse_bearer` returns `None` when absent (`auth.py`), `client_for(None, …)`
  builds an unauthenticated client.
- **Bearer-key (hosted-key / programmatic / CI / the `mcp-remote` bridge):**
  client sends its API key → forwarded downstream.

Properties: **no secret in the MCP** (it forwards identity, holds nothing);
**per-tenant by construction** (each key carries its own quota/billing);
**enforcement for free** (yente's existing SaaS gateway validates the key and
enforces quota exactly as for REST; the SDK raises `AuthenticationError` /
`RateLimitError`, which the MCP maps to MCP errors). No new identity system.

This is the **agent-native path** — headless/unattended workloads (CI, cron,
deployed services, scheduled agents) have no browser and no token-refresh
machinery, so they provision a long-lived API key and send it here. Keep
`passthrough` first-class; OAuth does not subsume it.

### Mode `oauth` — Ory login, for attended clients

For a public product where a human shouldn't copy-paste raw API keys.
**Ory.sh is already our Authorization Server** — the same OIDC provider behind
the website login — so the expensive part of OAuth (standing up an AS) is done.
The flow:

```
Claude Desktop ──login (auth-code + PKCE)──▶ Ory.sh (existing AS)
      │◀── Ory access token ───────────────┘
      ├── Bearer <ory-token> ──▶ yente-mcp (Resource Server)
      │                            │ validate JWT locally (Ory JWKS, iss, aud)
      │                            ├── ?access_token=<ory-token> ──▶ serviceapi /auth/credential  (NEW)
      │                            │◀────────── Credential.secret ──┘
      │                            └── AsyncClient(api_key=secret) ──▶ yente gateway (unchanged)
```

1. **Ory = AS.** Already does auth-code + PKCE, JWKS, `/.well-known` metadata.
2. **MCP = Resource Server.** FastMCP `JWTVerifier` against Ory's JWKS, checking
   `iss == OIDC_ISSUER_URL` and `aud == OIDC_AUDIENCE` — the same validation as
   serviceapi's `parse_access_token`, run inside the MCP. FastMCP auto-serves the
   protected-resource metadata + 401 challenge that triggers Desktop's login.
3. **Token → API key via a new serviceapi endpoint.** A handler guarded by the
   existing `request_token_user` dependency (which already accepts a token via
   `?access_token=` query param) returns the customer's API `Credential.secret`.
   If the customer has **no** API credential it returns an error telling the user
   to create one at `opensanctions.org/account` — it does **not** auto-mint (a
   deliberate departure from the website's `auth_callback`, which auto-provisions
   a trial). The MCP already sends the token as a query param, so the dependency
   needs **no change**; the only new code is the ~15-line endpoint body.
   (Tracked: opensanctions/operations#2620.)
4. **yente gateway stays key-based and untouched** — the MCP swaps the Ory token
   for the user's existing API key and calls downstream as today.

Chosen over "make the yente gateway validate Ory tokens directly" (bigger
gateway work, duplicates identity logic): the exchange endpoint reuses
`request_token_user` + `make_auto_api_credential` and keeps the gateway as-is.

**Attended only.** Interactive auth-code needs a browser, a human, and an MCP
client that manages refresh — fine for agents running *inside* Claude Desktop /
claude.ai / interactive Claude Code (they ride the human's session), a dead end
for truly headless agents. Those use `passthrough`. The split is
attended-vs-unattended, not human-vs-agent. (Client-credentials grant is the
"OAuth for machines" option but authenticates the client not a user, needs a
client→customer map, and is redundant with the API key we already issue — skip.)

### Hosted single endpoint (`mcp.opensanctions.org`) — dual-auth verifier

One advertised URL must serve both attended OAuth users and headless key
clients. A single-mode process can't, so the hosted deployment composes the two
modes in **one custom `TokenVerifier`** that branches on the *shape* of the
inbound bearer:

- **No token** → `401 + WWW-Authenticate` + protected-resource metadata →
  triggers OAuth login in attended clients.
- **JWT-shaped token** (`a.b.c`, Ory issuer) → validate against Ory JWKS →
  principal = user → token→key exchange (mode `oauth`).
- **Non-JWT token** (32-hex API key) → accept as an opaque key principal, forward
  downstream unchanged (mode `passthrough`). Never sees the 401, never triggers
  OAuth.

A key-bearing headless client sends its key on the first request and skips OAuth
entirely; a human connects with no credential, gets challenged, logs in. They
coexist on one URL.

**Fail closed on the JWT branch (the one rule that matters):** if a bearer *is*
JWT-shaped and claims the Ory issuer but fails signature/expiry validation,
**reject with 401** — never fall through to treating it as an API key. The
discriminator is purely structural (is it a JWT at all?); validation is
authoritative *within* each branch. The hex-vs-JWT shapes are distinct enough
that the sniff itself is low-risk; the fallback *ordering* is what must be
deliberate.

### State / caching (mode `oauth`, hosted)

`oauth` is the only mode that isn't credential-free: the MCP transiently caches
`token → secret`. Two module-level dicts in `auth.py` (same pattern as today's
`_CLIENTS`), both **in-process memory**:

- `_SECRETS: sha256(token) → (secret, exp)` — TTL-bounded by the token's own
  `exp` (lazy eviction); LRU-bounded for a busy server.
- `_CLIENTS: (secret, base_url) → AsyncClient` — today's cache, rekeyed on the
  *secret* so multiple tokens for the same customer share one httpx pool.

It's **per-process, not shared** — fine, because the cache is a latency/load
optimization, not a source of truth (serviceapi is). A miss costs one extra
exchange round-trip. **No Redis for v1**; that's the upgrade if cross-replica
sharing is ever wanted, not the starting point. Hash the token for the key (hold
the raw bearer no longer than needed; the `secret` is still held to use it, so
this isn't secret-free). JWT validation itself is stateless bar Ory's JWKS
keyset, cached one-time like serviceapi's `fetch_jwk`.

**Implementation notes.**
- `server.py`: make the FastMCP instance a `build_server(auth_mode)` factory so
  the `auth=` provider is wired (or omitted) per mode; `main.py` calls it.
- Confirm FastMCP's accessor for inbound headers / validated claims from a tool
  (`get_http_headers` is in use today for the bearer); thread the resolved key
  into the SDK call via a shared `resolve_api_key(request) -> str | None`.
- Per-request keys must not spin up a fresh `httpx` pool each call (the caches
  above); add an LRU bound + close-on-shutdown (FastMCP lifespan) — currently a
  TODO in `auth.py`.
- Optional: dedupe concurrent cache-miss exchanges for the same token
  (in-flight-future map) to avoid a dogpile. Not v1.
- `main.py` reads `YENTE_BASE_URL` (hosted vs self-hosted) and, for `oauth`, the
  Ory issuer / JWKS URL / audience and the serviceapi credential-exchange URL.

## Error mapping

The SDK raises a structured `YenteError` hierarchy: `AuthenticationError`,
`BadRequestError`, `NotFoundError`, `RateLimitError`, `ServerError`,
`TransportError`, `ConfigurationError`. Tools translate these into MCP tool
errors / structured payloads the model can read and act on (e.g. a
`ConfigurationError` for a non-matchable schema → a clear "this schema can't be
screened" message, not an opaque failure). Don't let raw exceptions become
transport-level errors.

## Open questions

1. ~~Transport~~ — **decided:** streamable-HTTP, required for v1; stdio optional.
2. **Auth** — **decided:** two base modes (`passthrough` = no-auth + bearer-key,
   already implemented; `oauth` = Ory login + serviceapi credential exchange),
   composed on the hosted `mcp.opensanctions.org` via a shape-sniffing dual-auth
   verifier (fail-closed on the JWT branch). Enforcement stays at yente's gateway.
   Still to confirm: **(a)** does our Ory project allow Dynamic Client
   Registration (RFC 7591)? If not, use FastMCP's `OAuthProxy` with the existing
   `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`. **(b)** the `aud` Ory issues to MCP
   clients matches `OIDC_AUDIENCE` (or add a dedicated MCP audience/scope).
   **(c)** which credential the exchange endpoint returns when a customer has
   several API credentials (lean: the primary/default API credential); if none
   exists it errors to `opensanctions.org/account` rather than auto-minting
   (opensanctions/operations#2620).
3. **`match_entity`** — single-entity only (lean), or expose the SDK's batch
   capability? Batching is awkward for an agent; the REST/SDK batch path stays
   available for programmatic callers.
4. **MCP prompts** — ship a counterparty-screening workflow prompt in v1, or
   tools/resources only? (Lean: tools/resources first; steering goes in tool
   descriptions, which are always in effect and client-universal, vs. a prompt
   that many clients surface poorly and only a human can invoke. If ever added:
   name it around `counterparty` — not `customer` (we screen vendors, owners,
   intermediaries too) and not `screen` (reserved); keep it procedural, never
   decisional.)
5. **TS MCP** — defer to a later milestone under `typescript/`? (Lean: yes.)
6. **`fetch_entity_by_id` merge-redirect** — verify/!fix in `AsyncClient.fetch` (SDK
   work, prerequisite for clean `fetch_entity_by_id`).

## Implementation phases (once surface is agreed)

1. Scaffold `src/yente_client/mcp/`, `[mcp]` extra (`fastmcp`), `yente-mcp`
   entry point serving FastMCP `http_app()` via uvicorn. One trivial tool;
   confirm an MCP client connects over streamable-HTTP.
2. `describe_schema` + `ftm://` resources over the bundled `schemas/model.json`
   (no server dependency — implementable/testable first, in isolation).
3. Implement the 4 `AsyncClient` tools with `shaping.py` and error mapping.
4. Add the 2 live `yente://` resources.
5. Auth, mode `passthrough` (covers no-auth + bearer-key): read the inbound
   `Authorization` header, thread the key into a per-key-cached `AsyncClient`;
   map `AuthenticationError` / `RateLimitError` to MCP errors. (+ `YENTE_BASE_URL`
   config wiring.) *Already implemented in `auth.py` / `server.py`.*
6. Auth, mode `oauth` (attended clients): new serviceapi `/auth/credential`
   exchange endpoint (serviceapi-side work); `build_server(auth_mode)` factory +
   FastMCP `JWTVerifier` against Ory; `token → secret` TTL cache; Ory issuer /
   JWKS / audience + exchange-URL config. Prereq: confirm Ory DCR (else
   `OAuthProxy`).
7. Hosted composition: dual-auth `TokenVerifier` (shape-sniff JWT vs API key,
   fail-closed on the JWT branch) so `mcp.opensanctions.org` serves both modes on
   one URL.
8. Tests (mock `AsyncClient` transport, shaping, error mapping, both auth modes
   + the dual-auth shape-sniff/fail-closed path) mirroring the SDK's
   `httpx.MockTransport` convention; docs page; `CHANGELOG.md` entry.
