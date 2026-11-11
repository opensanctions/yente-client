"""Expose OpenSanctions screening and investigation to MCP clients.

Lets an assistant screen people, companies, and other entities against
sanctions, PEP, and watchlist data and walk their relationships — the same
matching surface as the yente SDK, framed for an analyst rather than a caller.

Five adapters over :class:`yente_client.AsyncClient` (``match_entity``,
``search_entities``, ``fetch_entity_by_id`` — an entity profile assembled from
the record and its relations — ``fetch_entity_relations``,
``fetch_entity_statements``) plus ``describe``, a lookup of schemata, topics
and countries from the bundled FtM model, or of one dataset or program record
by name — the catalogs are too large to list whole. Every
entity-bearing result resolves its own codes — topic labels inline, everything
else through one ``legend`` per response (see :mod:`~yente_client.mcp.shaping`)
— so an agent never has to gloss a raw identifier. The server instructions
(:data:`yente_client.env.DEFAULT_MCP_INSTRUCTIONS`) carry the data model and
the rules shared by every tool; tool docstrings cover only their own tool.
The ``ftm://schema`` resources mirror
``describe(kind="schema")`` for resource-capable clients; anything the model must
dereference mid-conversation is a tool, not a resource, since clients don't
reliably read resources. Matching algorithms are deliberately not exposed:
algorithm choice is a tuning detail below this server's altitude — the server
default applies, and only its feature descriptions reach the legend; use the
SDK or CLI to experiment. Each request carries the
caller's API key as a bearer token, forwarded downstream (see
:mod:`yente_client.mcp.auth`).

Keep this module thin: the real logic lives in :mod:`~yente_client.mcp.introspect`
and :mod:`~yente_client.mcp.shaping`. Anything beyond plumbing belongs there so
it stays testable without FastMCP.
"""

import asyncio
import time
from typing import Any, Literal, NamedTuple

from mcp.types import CallToolRequestParams, Icon, ToolAnnotations
from pydantic import ValidationError

from yente_client import __version__, env
from yente_client._http import is_opensanctions_url
from yente_client.async_client import AsyncClient
from yente_client.entities import EntityInput, entity_class
from yente_client.exceptions import YenteError
from yente_client.mcp import introspect, shaping
from yente_client.mcp._deps import (
    CallNext,
    FastMCP,
    Middleware,
    MiddlewareContext,
    ToolError,
    ToolResult,
    get_http_headers,
)
from yente_client.mcp.auth import client_for, resolve_api_key
from yente_client.mcp.errors import describe_error
from yente_client.models import (
    AdjacentPropertyResponse,
    Entity,
    Program,
    Statement,
    StatementsResponse,
)

# OpenSanctions brand mark, served as square PNGs from the public asset CDN.
_ICON_BASE = "https://assets.opensanctions.org/images/nura"

# Every tool here only reads (screens, searches, fetches, describes) — nothing
# mutates server state — so they all share this read-only annotation. They query
# one closed database, not the open web, hence openWorldHint=False.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# Page sizes. A profile shows a few edges per relationship type so hub entities
# stay readable; the relations overview a few more; paging one type, a full page.
_PROFILE_EDGES = 3
_OVERVIEW_EDGES = 5
_PAGE_EDGES = 50
# Ceiling on any caller-chosen `limit`: a page of 100 ownership edges (~105k
# characters) overflows MCP clients' tool-output caps, while paging is free.
_MAX_LIMIT = 25
# Character budget for one page of fetch_entity_descriptions, and how many
# statements to read per prose property (Gazprom, a heavy case, has ~65 notes).
_TEXT_PAGE_CHARS = 15_000
_STATEMENT_LIMIT = 1000
# Candidates returned below cutoff when a screen comes back empty.
_NEAR_MISS_LIMIT = 3

# Every code in an entity-bearing result resolves in the response itself:
# schema, property and value labels come from the bundled model (see shaping),
# dataset, program and scoring-feature records from live lookups cached below.
# Labels ship with the data rather than as an instruction to resolve codes:
# agents use a label next to its code and skip a separate lookup step.
#
# 15 minutes bounds how long a newly added dataset or sanctions program stays
# unresolvable, and how stale a screen's reported `as_of` can be; the catalogs
# change on slower cadences than that. Refreshes
# are cheap: programs.json revalidates via ETag (a 304 round-trip), and the
# catalog is a small response (see opensanctions/yente#1202 for giving it an
# ETag too).
_GLOSSARY_TTL_SECONDS = 900.0
_program_catalog_cache: tuple[float, list[Program]] | None = None
_features_cache: tuple[float, dict[str, str]] | None = None


class _CatalogInfo(NamedTuple):
    datasets: dict[str, dict[str, Any]]
    freshness: dict[str, Any] | None


_catalog_info_cache: tuple[float, _CatalogInfo] | None = None

BASE_URL = env.base_url()
# Fallback API key for the whole server, used when a request carries no bearer
# token — lets you run yente-mcp locally against a real API for testing.
API_KEY = env.api_key()
# Website links only resolve for data the website knows: the hosted API's.
HOSTED = is_opensanctions_url(BASE_URL)
_LINKS = (
    {
        "entity": env.ENTITY_URL_TEMPLATE,
        "dataset": f"{env.WEBSITE_URL}/datasets/{{name}}/",
        "program": f"{env.WEBSITE_URL}/programs/{{key}}/",
    }
    if HOSTED
    else None
)

mcp: FastMCP = FastMCP(
    # name and instructions default to "yente" / the stock copy, but a self-hosted
    # deployment can rebrand both via $YENTE_MCP_NAME / $YENTE_MCP_INSTRUCTIONS.
    name=env.mcp_name(),
    version=__version__,
    website_url="https://www.opensanctions.org",
    icons=[
        # Full-colour brand mark first (renders on any background), favicons as
        # smaller fallbacks. Clients pick by `sizes`.
        Icon(src=f"{_ICON_BASE}/logo-icon-color.png", mimeType="image/png", sizes=["290x310"]),
        Icon(src=f"{_ICON_BASE}/favicon-32.png", mimeType="image/png", sizes=["32x32"]),
        Icon(src=f"{_ICON_BASE}/favicon-16.png", mimeType="image/png", sizes=["16x16"]),
    ],
    instructions=env.mcp_instructions(),
)


class _TextOnly(Middleware):
    """Send tool results as their JSON text block alone.

    FastMCP mirrors any dict result into ``structuredContent``, so clients see
    each result twice. Tools also pass ``output_schema=None``: results are
    dynamic JSON (FtM property bags, per-response legends) that a declared
    schema couldn't describe, and that clients would validate against it.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        return ToolResult(content=result.content, meta=result.meta, is_error=result.is_error)


mcp.add_middleware(_TextOnly())


def _resolve_client() -> AsyncClient:
    """Build (cached) an AsyncClient for this request.

    Uses the request's bearer token, falling back to the server's
    ``OPENSANCTIONS_API_KEY`` (see :func:`resolve_api_key`).
    """
    token = resolve_api_key(get_http_headers().get("authorization"), API_KEY)
    return client_for(token, BASE_URL)


async def _catalog_info(client: AsyncClient) -> _CatalogInfo:
    """Dataset records and freshness from the live catalog; cached, failure-soft.

    On a fetch error returns the stale cache if there is one, else an empty
    record — a missing legend must never fail a screening call.
    """
    global _catalog_info_cache
    now = time.monotonic()
    if _catalog_info_cache is not None:
        fetched_at, info = _catalog_info_cache
        if now - fetched_at < _GLOSSARY_TTL_SECONDS:
            return info
    try:
        resp = await client.datasets()
    except YenteError:
        return _catalog_info_cache[1] if _catalog_info_cache is not None else _CatalogInfo({}, None)
    info = _CatalogInfo(shaping.dataset_records(resp), shaping.freshness(resp))
    _catalog_info_cache = (now, info)
    return info


def _unofficial(info: _CatalogInfo) -> set[str]:
    """Names of datasets whose publisher isn't official (aggregators, NGOs, Wikidata)."""
    return {name for name, rec in info.datasets.items() if rec.get("official") is False}


async def _program_catalog(client: AsyncClient) -> list[Program]:
    """The sanctions-program catalog, cached process-wide (it's a global artifact).

    Raises ``YenteError`` on a failed fetch — describe wants the error;
    the legend path wraps this in :func:`_program_records` instead.
    """
    global _program_catalog_cache
    now = time.monotonic()
    if _program_catalog_cache is not None:
        fetched_at, catalog = _program_catalog_cache
        if now - fetched_at < _GLOSSARY_TTL_SECONDS:
            return catalog
    resp = await client.programs()
    _program_catalog_cache = (now, resp.data)
    return resp.data


async def _program_records(client: AsyncClient) -> dict[str, dict[str, Any]]:
    """Program key → legend record; the failure-soft face of :func:`_program_catalog`."""
    try:
        catalog = await _program_catalog(client)
    except YenteError:
        return {}
    return shaping.program_records(catalog)


async def _feature_descriptions(client: AsyncClient) -> dict[str, str]:
    """Scoring feature name → description for the default algorithm; cached, failure-soft."""
    global _features_cache
    now = time.monotonic()
    if _features_cache is not None:
        fetched_at, features = _features_cache
        if now - fetched_at < _GLOSSARY_TTL_SECONDS:
            return features
    try:
        resp = await client.algorithms()
    except YenteError:
        return _features_cache[1] if _features_cache is not None else {}
    features = shaping.feature_descriptions(resp)
    _features_cache = (now, features)
    return features


async def _with_legend(
    out: dict[str, Any], client: AsyncClient, codes: shaping.LegendCodes | None = None
) -> dict[str, Any]:
    """Attach the response's ``legend`` for every code it mentions, and return it.

    ``codes`` replaces the walk of ``out`` when the caller knows codes the walk
    can't see (relation keys, a describe record's countries). Program and
    feature lookups are live (cached), failure-soft, and only run when the
    response carries a code they resolve. A response with nothing to resolve
    gets no legend at all, not one holding only link templates.
    """
    if codes is None:
        codes = shaping.collect_codes(out)
    info = await _catalog_info(client)
    programs = await _program_records(client) if codes.programs else {}
    features = await _feature_descriptions(client) if codes.features else {}
    legend = shaping.build_legend(
        codes,
        datasets=info.datasets,
        programs=programs,
        features=features,
        links=_LINKS,
    )
    if legend.keys() - {"links"}:
        out["legend"] = legend
    return out


def _cap(limit: int | None) -> int | None:
    """Clamp a caller's page size to :data:`_MAX_LIMIT`; ``None`` keeps the default."""
    return None if limit is None else max(1, min(limit, _MAX_LIMIT))


def _build_entity(schema: str, properties: dict[str, list[str]]) -> EntityInput:
    """Construct a typed input entity from a schema name + property bag."""
    try:
        schema_cls = entity_class(schema)
    except KeyError:
        raise ToolError(
            f'Unknown FtM schema {schema!r}. Call describe(kind="schema") for the list.'
        ) from None
    try:
        return schema_cls.model_validate(properties)
    except ValidationError as exc:
        raise ToolError(f"Invalid properties for schema {schema!r}: {exc}") from exc


# ----- tools -----


@mcp.tool(title="Screen against watchlists", annotations=_READ_ONLY, output_schema=None)
async def match_entity(
    schema: str,
    properties: dict[str, list[str]],
    *,
    dataset: str = "default",
    threshold: float | None = None,
    cutoff: float | None = None,
    topics: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Screen one person, company, vessel or other entity; return scored candidates.

    The tool for every "is this party listed?" question, even from just a
    name. Describe the entity as fully as you can: `schema` is its FtM schema
    and `properties` maps property names to value lists, e.g. {"name": ["Jane
    Doe"], "birthDate": ["1975"], "nationality": ["Russia"]}. Send every
    property you have — identifiers (imoNumber, registrationNumber) and dates
    settle what names alone can't. Screen each party in its own call.

    `topics` sets what kind of listing to screen for. A candidate qualifies if
    it carries any of the given codes, matched exactly: for sanctions pass
    ["sanction", "sanction.linked", "sanction.control"], for politically
    exposed persons ["role.pep", "role.rca"]. Unset, the screen covers every
    listing. describe(kind="topics") lists every code.

    Each candidate has a `score` (0-1), `match` (score at or above the
    threshold) and an `explanation`: the scoring features with the query and
    candidate values each compared. A fuzzy name with nothing else agreeing is
    a lead, not an identification; identifiers and dates the query lacked are
    what would settle it. Candidates are abridged: identifying properties and
    the evidence, with `not_shown` naming what the full record adds;
    fetch_entity_by_id on the `id` returns it. When no candidate clears the
    cutoff, `near_misses` shows the closest ones and why they fell short. `checked`
    echoes the scope screened and the data's `as_of` time.

    Args:
        dataset: Collection to screen against; "default" covers all sources.
        threshold: Score at which `match` turns true; leave to the server.
        cutoff: Score below which candidates are dropped; leave to the server.
        limit: Candidates to return, at most 25.
    """
    entity = _build_entity(schema, properties)
    client = _resolve_client()
    try:
        resp = await client.match(
            entity,
            threshold=threshold,
            cutoff=cutoff,
            limit=_cap(limit),
            datasets=[dataset],
            topics=topics,
        )
        near_misses = None
        if not resp.results and cutoff is None:
            near_misses = await client.match(
                entity,
                threshold=threshold,
                cutoff=0.0,
                limit=_NEAR_MISS_LIMIT,
                datasets=[dataset],
                topics=topics,
            )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    freshness = (await _catalog_info(client)).freshness or {}
    checked = {
        "dataset": dataset,
        "topics": topics,
        "threshold": threshold,
        "cutoff": cutoff,
        "as_of": freshness.get("updated_at"),
    }
    results = [shaping.shape_scored(r) for r in resp.results]
    out: dict[str, Any] = {
        "query_schema": schema,
        "checked": {k: v for k, v in checked.items() if v is not None},
        "total": resp.total.value,
        "returned": len(results),
        "results": results,
    }
    if near_misses is not None and near_misses.results:
        out["near_misses"] = [shaping.shape_scored(r) for r in near_misses.results]
    # `checked` echoes the caller's own scope, which needs no glossing.
    codes = shaping.collect_codes({k: v for k, v in out.items() if k != "checked"})
    return await _with_legend(out, client, codes)


@mcp.tool(title="Search entities", annotations=_READ_ONLY, output_schema=None)
async def search_entities(
    q: str,
    *,
    dataset: str = "default",
    schema: str | None = None,
    countries: list[str] | None = None,
    topics: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    fuzzy: bool = False,
    simple: bool = False,
) -> dict[str, Any]:
    """Browse the database by keyword; return unscored entities.

    For exploring: what a term turns up, narrowed by schema, country or topic
    (vessels flagged to a country, companies with a word in their name).
    Never use it to decide whether a person or company is listed — results
    carry no score or explanation, so neither a hit nor its absence proves
    anything. Use match_entity for that, even with just a name.

    Args:
        limit: Results per page, at most 25.
        fuzzy: Tolerate spelling variation in `q`.
        simple: Read `q` as plain words, as typed into a search box, rather
            than query syntax (AND / OR, quotes, field:value).
    """
    client = _resolve_client()
    kwargs: dict[str, Any] = {"datasets": [dataset]}
    if schema is not None:
        kwargs["schema"] = schema
    if countries is not None:
        kwargs["countries"] = countries
    if topics is not None:
        kwargs["topics"] = topics
    try:
        resp = await client.search(
            q, limit=_cap(limit), offset=offset, fuzzy=fuzzy, simple=simple, **kwargs
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    results = [shaping.shape(r) for r in resp.results]
    out = {**shaping.page_info(resp.total, offset, len(results)), "results": results}
    return await _with_legend(out, client)


@mcp.tool(title="Profile an entity", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_by_id(entity_id: str, *, full: bool = False) -> dict[str, Any]:
    """Profile one entity by ID: the call for "tell me about X".

    Returns the entity's record (names, dates, identifiers, addresses) and, in
    full, its risk basis:

    - `sanctions`: designations that impose a restriction (program,
      authority, dates).
    - `risks`: listings that name the entity without a restriction (watch
      lists, advisories, persons-of-interest registers).
    - `positionOccupancies`: positions held, with country, dates and status.

    `relations` summarizes every other relationship type (ownership,
    directorships, family, associates, …): its total and first few edges. Each
    edge keeps its own details (role, share, relationship, dates); the entity
    at its far end is just {id, caption, schema, topics}, marked `abridged`:
    a missing field there is not shown, not unknown — call
    fetch_entity_by_id on that `id` for its record. Page one type in full
    with fetch_entity_relations. Prose is left out: `descriptions` counts the
    notes and designation reasons that fetch_entity_descriptions returns. The
    record's `first_seen` / `last_change` say when it entered the data and
    last changed.

    If `relations_unavailable` is set, sanctions and relations could not be
    read, so their absence says nothing about a listing. A returned `id`
    other than the one requested means the entity was merged into another,
    and the returned ID is the current one.

    Args:
        full: Return the raw record instead, including `referents` (the
            source records merged into it), without relations.
    """
    client = _resolve_client()
    try:
        entity = await client.fetch(entity_id, nested=False)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    if full:
        return await _with_legend(shaping.shape_raw(entity), client)
    try:
        adjacency = await client.adjacent(entity.id, limit=_PROFILE_EDGES)
        truncated = [
            prop
            for prop in shaping.PROFILE_SECTIONS
            if (page := adjacency.adjacent.get(prop)) is not None
            and page.total.value > len(page.results)
        ]
        pages = await asyncio.gather(
            *(client.adjacent(entity.id, prop=prop, limit=_PAGE_EDGES) for prop in truncated)
        )
    except YenteError as exc:
        profile = shaping.shape(entity)
        profile["relations_unavailable"] = describe_error(exc)
        return await _with_legend(profile, client)
    full_pages = dict(zip(truncated, pages, strict=True))
    return await _with_legend(shaping.shape_profile(entity, adjacency, full_pages), client)


@mcp.tool(
    title="Read source descriptions and designation reasons",
    annotations=_READ_ONLY,
    output_schema=None,
)
async def fetch_entity_descriptions(entity_id: str, *, offset: int = 0) -> dict[str, Any]:
    """Read what sources write about an entity, verbatim and credited.

    The place to answer "why is this entity listed?", to give background on a
    person or company, or to quote a source. The reasons given for each
    sanction and risk listing come first, each with its designating record
    (authority, program, dates), then the entity's own descriptions and
    biography notes. Every entry names its `sources` (dataset, first seen),
    official sources first; identical texts from several sources appear once,
    translations separately with their `lang`.

    Page with `offset` while `next_offset` is set. `credits_unavailable` means
    the texts carry no per-source credits (a self-hosted yente has no
    statement store); `relations_unavailable` means designation reasons could
    not be read.
    """
    client = _resolve_client()
    try:
        entity = await client.fetch(entity_id, nested=False)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    text_props = shaping.text_property_names(entity.schema_)
    edge_props = shaping.designation_props(entity.schema_)
    statement_calls = [
        client.statements(canonical_id=entity.id, prop=prop, limit=_STATEMENT_LIMIT)
        for prop in text_props
    ]
    edge_calls = [client.adjacent(entity.id, prop=p, limit=_PAGE_EDGES) for p in edge_props]
    results = await asyncio.gather(*statement_calls, *edge_calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, YenteError):
            raise result
    statement_results = results[: len(statement_calls)]
    edge_results = results[len(statement_calls) :]

    out: dict[str, Any] = {"id": entity.id, "caption": entity.caption, "schema": entity.schema_}
    statements: list[Statement] | None = []
    for resp in statement_results:
        if isinstance(resp, StatementsResponse) and statements is not None:
            statements.extend(resp.results)
        else:
            statements = None
    if statements is None:
        out["credits_unavailable"] = True
    designations: dict[str, list[Entity]] = {}
    for prop, resp in zip(edge_props, edge_results, strict=True):
        if isinstance(resp, AdjacentPropertyResponse):
            designations[prop] = [e for e in resp.results if isinstance(e, Entity)]
        elif isinstance(resp, YenteError):
            out["relations_unavailable"] = describe_error(resp)
    unofficial = _unofficial(await _catalog_info(client))
    entries = shaping.text_entries(entity, statements, designations, unofficial)
    out.update(shaping.page_entries(entries, offset, _TEXT_PAGE_CHARS))
    return await _with_legend(out, client)


@mcp.tool(title="Fetch entity relations", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_relations(
    entity_id: str,
    *,
    prop: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through one relationship type of an entity in full.

    A relationship is itself an entity (Ownership, Directorship, Family, …)
    linking two others, and `prop` names the side this entity is on — the keys
    of a profile's `relations`, or `sanctions` / `risks` /
    `positionOccupancies`. For a company, "ownershipOwner" lists what it owns
    and "ownershipAsset" who owns it. Each edge carries its own properties
    (role, share, dates); the entity at its far end is {id, caption, schema,
    topics}, marked `abridged` — call fetch_entity_by_id on its `id` for its
    record. Page with
    `limit` (default and maximum 25) / `offset` until `next_offset` is null;
    `total` counts every edge, read or not. With no `prop`, returns every
    type with its total and first few edges.
    """
    if prop in shaping.HIDDEN_RELATION_PROPS:
        raise ToolError(
            f"{prop!r} is not a traversable relation here: the adjacency endpoint "
            "reports a count but resolves no entities. Read the entity's own record "
            "(fetch_entity_by_id) for its risk basis."
        )
    client = _resolve_client()
    try:
        if prop is None:
            overview = await client.adjacent(
                entity_id, limit=_cap(limit) or _OVERVIEW_EDGES, offset=offset
            )
            out = shaping.shape_adjacency(overview)
            codes = shaping.collect_codes(out)
            codes.add_properties(overview.entity.schema_, out)
        else:
            block = await client.adjacent(
                entity_id, prop=prop, limit=_cap(limit) or _MAX_LIMIT, offset=offset
            )
            out = shaping.shape_relation_page(block, entity_id, prop)
            codes = shaping.collect_codes(out)
            codes.add_properties(None, [prop])
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return await _with_legend(out, client, codes)


@mcp.tool(title="Audit entity provenance", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_statements(
    entity_id: str,
    *,
    prop: str | None = None,
    dataset: str | None = None,
    sort: list[str] | None = None,
    limit: int = _MAX_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Trace which source asserted each value of an entity, and when.

    A statement is one property value plus the dataset that asserted it and
    when it was first and last seen. Use it for "which source says this?" (an
    alias, a birth date, an address) or, with sort=["first_seen:desc"], for
    "what changed since a date". Sanctions and positions are entities of their
    own with their own statements: pass their IDs from the profile to check
    them too. Narrow with `prop` (e.g. "alias") or `dataset`. Each row's
    `entity_id` is the source record that was merged into the entity. Hosted
    OpenSanctions API only; a self-hosted yente errors here.

    Args:
        limit: Statements per page, at most 25.
    """
    client = _resolve_client()
    try:
        resp = await client.statements(
            canonical_id=entity_id,
            prop=prop,
            dataset=dataset,
            sort=sort,
            limit=_cap(limit),
            offset=offset,
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return await _with_legend(shaping.shape_statements(resp), client)


_TOPICS_ABOUT = (
    "Topics are tags on an entity saying why it is in the data or what kind of "
    "entity it is. Codes form dotted families: `sanction*` (listed, or owned, "
    "controlled or otherwise linked to a listed entity), `role.pep*` and "
    "`role.rca` (politically exposed persons and their close associates), "
    "`crime.*`, `debarment`, `wanted`, `reg.*` (offences and official action), "
    "`export.*` and `mare.*` (trade and shipping restrictions), and descriptive "
    "families such as `gov.*`, `fin.*` and `corp.*`. A tag is not an "
    "accusation: being a PEP calls for closer scrutiny, not suspicion. As a "
    "filter, a topic matches its exact code only — `sanction` "
    "does not include `sanction.linked` — and an entity qualifies if it "
    "carries any of the codes given."
)

_NAME_EXAMPLES = {"dataset": "us_ofac_sdn", "program": "US-RUSHAR"}


@mcp.tool(title="Describe codes and sources", annotations=_READ_ONLY, output_schema=None)
async def describe(
    kind: Literal["schema", "topics", "countries", "dataset", "program"],
    name: str | None = None,
) -> dict[str, Any]:
    """Look up the data model's vocabulary or one source record.

    - `schema`: without `name`, the schemata match_entity accepts; with one
      ("Person", "Vessel"), its `properties` — the names match_entity takes,
      such as `birthDate` or `imoNumber` — and its `relations`, the keys
      fetch_entity_relations takes, with `range` the schema each points to.
    - `topics`: what the topic tags mean, and every code with its label, for
      the `topics` filter of match_entity and search_entities.
    - `countries`: every country and jurisdiction code with its name, for the
      `countries` filter of search_entities (match_entity takes names).
    - `dataset`: one source by `name` (e.g. "us_ofac_sdn", from an entity's
      `datasets` or the legend) — publisher, summary, coverage, update times.
    - `program`: one sanctions program by `name`, its key (e.g. "US-RUSHAR",
      an entity's `programId`) — issuer, policy summary, measures, links.

    Codes in results already resolve through the `legend`. An unknown dataset
    or program name returns the closest matches.
    """
    if kind in ("topics", "countries") and name is not None:
        raise ToolError(f'describe(kind="{kind}") takes no name; it lists every code.')
    if kind in ("dataset", "program"):
        if name is None:
            example = _NAME_EXAMPLES[kind]
            raise ToolError(f'describe(kind="{kind}") needs a name, e.g. "{example}".')
        lookup = _describe_dataset if kind == "dataset" else _describe_program
        return await lookup(name)
    if kind == "schema":
        if name is None:
            return {"schemata": introspect.schema_index()}
        try:
            return introspect.describe_schema(name)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    if kind == "topics":
        return {"about": _TOPICS_ABOUT, "topics": introspect.topic_values()}
    return introspect.country_values()


async def _describe_dataset(name: str) -> dict[str, Any]:
    """One dataset record from the live catalog, with its legend."""
    client = _resolve_client()
    try:
        resp = await client.datasets()
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    for dataset in resp.datasets:
        if dataset.name == name:
            out = dataset.model_dump(by_alias=True, mode="json", exclude_none=True)
            codes = shaping.collect_codes(out)
            if dataset.coverage is not None:
                codes.add_values("country", dataset.coverage.countries)
            if dataset.publisher is not None:
                codes.add_values("country", [dataset.publisher.country])
            return await _with_legend(out, client, codes)
    titles = {d.name: d.title for d in resp.datasets}
    raise ToolError(shaping.unknown_name("dataset", name, titles))


async def _describe_program(key: str) -> dict[str, Any]:
    """One sanctions program by key or alias, with its legend."""
    client = _resolve_client()
    try:
        catalog = await _program_catalog(client)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    for program in catalog:
        if program.key == key or key in program.aliases:
            out = program.model_dump(mode="json", exclude_none=True)
            codes = shaping.collect_codes(out)
            territory = program.issuer.territory if program.issuer else None
            codes.add_values("country", [territory, *program.target_territories])
            if program.dataset:
                codes.datasets.add(program.dataset)
            return await _with_legend(out, client, codes)
    titles = {p.key: p.title for p in catalog}
    raise ToolError(shaping.unknown_name("program", key, titles))


# ----- resources: ftm:// (static, bundled model, no server call) -----


@mcp.resource("ftm://schemata")
def ftm_schemata() -> list[dict[str, Any]]:
    """Index of matchable FtM schemata."""
    return introspect.schema_index()


@mcp.resource("ftm://schema/{name}")
def ftm_schema(name: str) -> dict[str, Any]:
    """Property detail for one FtM schema."""
    return introspect.describe_schema(name)


# Topic / country vocabularies are served by the describe *tool* above, not
# resources: the model needs them to pick filter values mid-conversation and
# clients don't reliably read resources (for the same reason, dataset lookup is
# there too). Gender values ("male", "female", …) are
# self-explanatory, so there's no lookup for them at all. There is no yente://
# resource for live server state: the one candidate, the algorithm catalog, is
# deliberately unexposed (see the module docstring).
