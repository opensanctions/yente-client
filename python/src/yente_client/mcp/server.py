"""Expose OpenSanctions screening and investigation to MCP clients.

Lets an assistant screen people, companies, and other entities against
sanctions, PEP, and watchlist data and walk their relationships — the same
matching surface as the yente SDK, framed for an analyst rather than a caller.

Five adapters over :class:`yente_client.AsyncClient` (``match_entity``,
``search_entities``, ``fetch_entity_by_id`` — an entity profile assembled from
the record and its relations — ``fetch_entity_relations``,
``fetch_entity_statements``) plus a set of ``describe_*`` lookup tools:
``describe_schema``, ``describe_topics`` and ``describe_countries`` over the
bundled FtM model, ``describe_dataset`` over the live catalog, and
``describe_program`` over the public program-catalog artifact. Every
entity-bearing result resolves its own codes — topic labels inline, everything
else through one ``legend`` per response (see :mod:`~yente_client.mcp.shaping`)
— so an agent never has to gloss a raw identifier. The ``ftm://schema`` resources mirror
``describe_schema`` for resource-capable clients; anything the model must
dereference mid-conversation is a tool, not a resource, since clients don't
reliably read resources. Matching algorithms are deliberately not exposed:
algorithm choice is a tuning detail below this server's altitude — the server
default applies; use the SDK or CLI to experiment. Each request carries the
caller's API key as a bearer token, forwarded downstream (see
:mod:`yente_client.mcp.auth`).

Keep this module thin: the real logic lives in :mod:`~yente_client.mcp.introspect`
and :mod:`~yente_client.mcp.shaping`. Anything beyond plumbing belongs there so
it stays testable without FastMCP.
"""

import asyncio
import time
from typing import Any

from mcp.types import Icon, ToolAnnotations
from pydantic import ValidationError

from yente_client import entities, env
from yente_client._http import _client_version
from yente_client.async_client import AsyncClient
from yente_client.entities import EntityInput
from yente_client.exceptions import YenteError
from yente_client.mcp import introspect, shaping
from yente_client.mcp._deps import FastMCP, ToolError, get_http_headers
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
# mutates server state — so they all share this read-only annotation.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# Page sizes. A profile shows a few edges per relationship type so hub entities
# stay readable; the relations overview a few more; paging one type, a full page.
_PROFILE_EDGES = 3
_OVERVIEW_EDGES = 5
_PAGE_EDGES = 50
# Character budget for one page of fetch_entity_descriptions, and how many
# statements to read per prose property (Gazprom, a heavy case, has ~65 notes).
_TEXT_PAGE_CHARS = 15_000
_STATEMENT_LIMIT = 1000
# Candidates returned below cutoff when a screen comes back empty.
_NEAR_MISS_LIMIT = 3

# Every code in an entity-bearing result resolves in the response itself:
# country and property labels come from the bundled model (see shaping),
# dataset and program titles from live lookups cached below. An instruction to
# "resolve codes before reporting" was routinely ignored; labels shipped with
# the data are not — so there is no note nudging the agent, just the labels.
#
# 15 minutes bounds how long a newly added dataset or sanctions program stays
# unresolvable; both catalogs change on slower cadences than that. Refreshes
# are cheap: programs.json revalidates via ETag (a 304 round-trip), and the
# catalog is a small response (see opensanctions/yente#1202 for giving it an
# ETag too).
_GLOSSARY_TTL_SECONDS = 900.0
_dataset_info_cache: tuple[float, dict[str, tuple[str | None, bool | None]]] | None = None
_program_catalog_cache: tuple[float, list[Program]] | None = None

BASE_URL = env.base_url()
# Fallback API key for the whole server, used when a request carries no bearer
# token — lets you run yente-mcp locally against a real API for testing.
API_KEY = env.api_key()

mcp: FastMCP = FastMCP(
    # name and instructions default to "yente" / the stock copy, but a self-hosted
    # deployment can rebrand both via $YENTE_MCP_NAME / $YENTE_MCP_INSTRUCTIONS.
    name=env.mcp_name(),
    version=_client_version(),
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


def _resolve_client() -> AsyncClient:
    """Build (cached) an AsyncClient for this request.

    Uses the request's bearer token, falling back to the server's
    ``OPENSANCTIONS_API_KEY`` (see :func:`resolve_api_key`).
    """
    token = resolve_api_key(get_http_headers().get("authorization"), API_KEY)
    return client_for(token, BASE_URL)


async def _dataset_info(client: AsyncClient) -> dict[str, tuple[str | None, bool | None]]:
    """Dataset name → (title, publisher official?) from the live catalog; cached, failure-soft.

    On a fetch error returns the stale cache if there is one, else an empty
    map — a missing legend must never fail a screening call. ``official`` is
    ``None`` for collections, which have no publisher.
    """
    global _dataset_info_cache
    now = time.monotonic()
    if _dataset_info_cache is not None:
        fetched_at, info = _dataset_info_cache
        if now - fetched_at < _GLOSSARY_TTL_SECONDS:
            return info
    try:
        resp = await client.datasets()
    except YenteError:
        return _dataset_info_cache[1] if _dataset_info_cache is not None else {}
    info = {d.name: (d.title, d.publisher.official if d.publisher else None) for d in resp.datasets}
    _dataset_info_cache = (now, info)
    return info


def _unofficial(info: dict[str, tuple[str | None, bool | None]]) -> set[str]:
    """Names of datasets whose publisher isn't official (aggregators, NGOs, Wikidata)."""
    return {name for name, (_, official) in info.items() if official is False}


async def _program_catalog(client: AsyncClient) -> list[Program]:
    """The sanctions-program catalog, cached process-wide (it's a global artifact).

    Raises ``YenteError`` on a failed fetch — describe_program wants the error;
    the legend path wraps this in :func:`_program_titles` instead.
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


async def _program_titles(client: AsyncClient) -> dict[str, str]:
    """Program key → title; the failure-soft face of :func:`_program_catalog`."""
    try:
        catalog = await _program_catalog(client)
    except YenteError:
        return {}
    return {p.key: p.title for p in catalog if p.title}


async def _with_legend(out: dict[str, Any], client: AsyncClient) -> dict[str, Any]:
    """Attach the response's ``legend`` for every code it mentions, and return it.

    Title lookups are live (cached) and failure-soft, and only run when the
    response carries a code they resolve.
    """
    codes = shaping.collect_codes(out)
    info = await _dataset_info(client) if codes.datasets else {}
    dataset_titles = {name: title for name, (title, _) in info.items() if title}
    unofficial = _unofficial(info)
    program_titles = await _program_titles(client) if codes.programs else {}
    legend = shaping.build_legend(codes, dataset_titles, program_titles, unofficial)
    if legend:
        out["legend"] = legend
    return out


def _build_entity(schema: str, properties: dict[str, list[str]]) -> EntityInput:
    """Construct a typed input entity from a schema name + property bag."""
    schema_cls = getattr(entities, schema, None)
    if not isinstance(schema_cls, type):
        raise ToolError(
            f"Unknown FtM schema {schema!r}. Call describe_schema() for matchable schemata."
        )
    try:
        entity: EntityInput = schema_cls(**properties)
    except ValidationError as exc:
        raise ToolError(f"Invalid properties for schema {schema!r}: {exc}") from exc
    return entity


# ----- tools -----


@mcp.tool(title="Screen against watchlists", annotations=_READ_ONLY)
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
    """Screen one entity against sanctions/PEP/watchlist data; return scored candidates.

    Query-by-example: describe the entity in as much detail as you can and the
    API returns ranked candidates. Use for ANY matching task, even with partial
    data. `schema` is an FtM type (Person, Company, ...) and `properties` maps
    FtM property names to value lists ({"name": ["Jane Doe"], "birthDate":
    ["1975"]}) — call describe_schema first if unsure of the names. Send every
    property you have. Country values accept free text ("Russia"); the server
    normalizes them, and matching works by value type rather than field name,
    so don't agonize over e.g. `country` vs `jurisdiction`. For a sanctions
    screen, pass topics=["sanction", "sanction.linked", "sanction.control"].

    `score` is 0-1 confidence; `match` is true when score >= `threshold`
    (server default 0.7). Candidates below `cutoff` (server default 0.5) are
    dropped. Each candidate's `explanation` lists the scoring features with
    the query and candidate values they compared — read it before calling a
    hit confirmed: a high score on a fuzzy name alone is a lead, not an
    identification. When nothing clears the default cutoff, the closest
    candidates come back under `near_misses`: below cutoff, not hits, but
    their explanations show why they fell short. `checked` echoes the scope
    that was screened. Expand a candidate with fetch_entity_by_id.
    """
    entity = _build_entity(schema, properties)
    client = _resolve_client()
    try:
        resp = await client.match(
            entity,
            threshold=threshold,
            cutoff=cutoff,
            limit=limit,
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
    checked = {"dataset": dataset, "topics": topics, "threshold": threshold, "cutoff": cutoff}
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
    return await _with_legend(out, client)


@mcp.tool(title="Search entities", annotations=_READ_ONLY)
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
    """Free-text search over the database; returns plain (unscored) entities.

    For end-user search UIs — autocomplete, browse pages, boxes where a human
    is typing the input. Not a fallback for match_entity: any match/no-match
    decision on a known person or company uses match_entity, even with partial
    input.
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
            q, limit=limit, offset=offset, fuzzy=fuzzy, simple=simple, **kwargs
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    results = [shaping.shape(r) for r in resp.results]
    out = {**shaping.page_info(resp.total, offset, len(results)), "results": results}
    return await _with_legend(out, client)


@mcp.tool(title="Profile an entity", annotations=_READ_ONLY)
async def fetch_entity_by_id(entity_id: str, *, full: bool = False) -> dict[str, Any]:
    """Profile one entity by its OpenSanctions ID — the one call for "tell me about X".

    Returns the entity's own record (names, dates, identifiers, addresses)
    plus, in full where the entity has them:

    - `sanctions`: designations that impose a restriction (program,
      authority, dates).
    - `risks`: listings that name the entity without imposing a restriction —
      watch lists, advisories, persons-of-interest registers. Evidence for
      review; never report a risk listing as "sanctioned".
    - `positionOccupancies`: positions held (the post with its country,
      dates, status).

    `relations` summarizes every other relationship type (ownership,
    directorships, family, associates, …): the total count and the first few
    edges, each counterparty with its risk topics and key properties. Use to
    expand a candidate from match_entity / search_entities or a counterparty
    from a relation. Needs a real entity ID, not a name. Answer from this
    record rather than background knowledge, and say so where you go beyond
    it.

    Prose is not here: `descriptions` counts the biography notes and
    designation reasons that exist (e.g. {"notes": 16, "reason": 21}). Call
    fetch_entity_descriptions to read them — the place to answer "why is this
    entity listed?". A list whose `next_offset` is set has more edges than
    shown — page them with fetch_entity_relations. `first_seen` / `last_change` dates on the
    record and on each edge show when it entered the data and last changed.
    If `relations_unavailable` is set, sanctions and relations could not be
    read — say so rather than reporting the entity as clean. If the returned
    `id` differs from the one requested, the entity was merged during
    deduplication — update any stored reference.

    Args:
        full: Return the raw, untrimmed record instead (including `referents`,
            the source-record IDs merged into it), without relations.
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


@mcp.tool(title="Read source descriptions and designation reasons", annotations=_READ_ONLY)
async def fetch_entity_descriptions(entity_id: str, *, offset: int = 0) -> dict[str, Any]:
    """Read what sources write about an entity, verbatim and credited.

    Use for "why is this entity listed?", for background on a person or
    company, or to quote a source. Returns the reason text of each sanction
    and risk listing first (with the designating record: authority, program,
    dates), then the entity's own descriptions and biography notes. Each entry
    names its `sources` (dataset, first seen); identical texts from several
    sources appear once, translations separately with their `lang`. Official
    sources come first. Datasets in `legend.unofficial_datasets` are
    aggregators, NGOs or crowd-edited sources rather than authorities — say so
    when you rely on them.

    Long histories are paged: continue with `offset` while `next_offset` is
    set. `credits_unavailable` means the entity's own texts came from its
    merged record without per-source credits (self-hosted yente has no
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
    unofficial = _unofficial(await _dataset_info(client))
    entries = shaping.text_entries(entity, statements, designations, unofficial)
    out.update(shaping.page_entries(entries, offset, _TEXT_PAGE_CHARS))
    return await _with_legend(out, client)


@mcp.tool(title="Fetch entity relations", annotations=_READ_ONLY)
async def fetch_entity_relations(
    entity_id: str,
    *,
    prop: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through an entity's relationships beyond what its profile shows.

    fetch_entity_by_id already summarizes every relationship type; use this to
    walk one type in full. Pass a `prop` ("ownershipAsset",
    "directorshipDirector", …: the profile's `sanctions` / `risks` /
    `positionOccupancies` keys or the keys of its `relations`) and page
    with `limit` (default 50) / `offset` until `next_offset` is null. With no
    `prop`, returns every type with its total and a first page of 5. Each edge
    carries its own properties (role, share, dates) and the counterparty as
    {id, caption, schema, topics} plus its key `properties`. Prose such as a
    sanction's reason is in fetch_entity_descriptions. Beta; shape may change.
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
                entity_id, limit=limit or _OVERVIEW_EDGES, offset=offset
            )
            out = shaping.shape_adjacency(overview)
        else:
            block = await client.adjacent(
                entity_id, prop=prop, limit=limit or _PAGE_EDGES, offset=offset
            )
            out = shaping.shape_relation_page(block, entity_id)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return await _with_legend(out, client)


@mcp.tool(title="Audit entity provenance", annotations=_READ_ONLY)
async def fetch_entity_statements(
    entity_id: str,
    *,
    prop: str | None = None,
    dataset: str | None = None,
    sort: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Audit where an entity's data came from, statement by statement.

    Each statement is one property-value claim plus the dataset that asserted
    it and when it was first and last seen. Use after a hit to answer "which
    source says this?" — e.g. which list contributed an alias, birth date, or
    address — or, with sort=["first_seen:desc"], "what changed since a
    date". `entity_id` is the canonical ID from match/search/fetch results;
    each row's `entity_id` names the pre-deduplication source record that was
    merged into it. A sanction designation is its own entity: pass its ID
    (from the profile's `sanctions`) to audit it. Narrow with `prop` (e.g.
    "alias") or `dataset`. Available on the hosted OpenSanctions API only — a
    self-hosted yente has no statement store and errors here.
    """
    client = _resolve_client()
    try:
        resp = await client.statements(
            canonical_id=entity_id,
            prop=prop,
            dataset=dataset,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return await _with_legend(shaping.shape_statements(resp), client)


@mcp.tool(title="Describe FollowTheMoney (FtM) schema", annotations=_READ_ONLY)
def describe_schema(schema: str | None = None) -> dict[str, Any]:
    """Look up the FtM data model (offline). No arg → index of matchable schemata;
    a name (e.g. "Person") → its settable `properties` (the fields you fill in a
    match_entity query, with real names like `birthDate`) plus `relations` (the
    entity edges: pass a relation's `name` as `prop` to fetch_entity_relations,
    `range` is the schema it points at, `reverse` is the source's role on the far
    entity). Use before match_entity.
    """
    if schema is None:
        return {"schemata": introspect.schema_index()}
    try:
        return introspect.describe_schema(schema)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Describe topic tags", annotations=_READ_ONLY)
def describe_topics() -> dict[str, str]:
    """List the risk-topic vocabulary — the whole `topic` type (value → label).

    Use to pick values for the `topics` filter on match_entity /
    search_entities (`sanction`, `role.pep`, …). Topic codes in results arrive
    already labelled; this is the full menu.
    """
    return introspect.topic_values()


@mcp.tool(title="Describe country codes", annotations=_READ_ONLY)
def describe_countries() -> dict[str, str]:
    """List the country / jurisdiction vocabulary — the whole `country` type (code → name).

    Use to pick codes for the `countries` filter on search_entities. Country
    codes in results already resolve through the response's `legend`.
    """
    return introspect.country_values()


@mcp.tool(title="Describe a dataset", annotations=_READ_ONLY)
async def describe_dataset(name: str | None = None) -> dict[str, Any]:
    """Resolve dataset names to their metadata (live catalog).

    No arg → a compact index of every indexed dataset (name, title, tags like
    `list.sanction` / `list.pep`, entity_count) for discovery. A name (e.g.
    "us_ofac_sdn") → that dataset's full record: title, summary, publisher,
    coverage, counts, freshness. Dataset names in results already resolve through
    the response `legend`; call this when the analyst needs the dataset's
    substance — who publishes it, what it covers, how fresh it is.
    """
    client = _resolve_client()
    try:
        resp = await client.datasets()
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    if name is None:
        return {"datasets": shaping.dataset_index(resp)}
    for dataset in resp.datasets:
        if dataset.name == name:
            return dataset.model_dump(by_alias=True, mode="json", exclude_none=True)
    raise ToolError(f"Unknown dataset {name!r}. Call describe_dataset() for the catalog index.")


@mcp.tool(title="Describe a sanctions program", annotations=_READ_ONLY)
async def describe_program(key: str | None = None) -> dict[str, Any]:
    """Resolve `programId` codes to sanctions-program metadata.

    No arg → a compact index of every program (key, title, issuer territory).
    A key (e.g. "US-RUSHAR" — the `programId` values sanctioned entities
    carry) → that program's full record: title, issuer, policy summary,
    measures, and links. Program codes in results already resolve through
    the response `legend`; call this when the analyst needs the program's
    substance — who imposed it, why, and what it restricts.
    """
    client = _resolve_client()
    try:
        catalog = await _program_catalog(client)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    if key is None:
        return {"programs": shaping.program_index(catalog)}
    for program in catalog:
        if program.key == key or key in program.aliases:
            return program.model_dump(mode="json", exclude_none=True)
    raise ToolError(f"Unknown program {key!r}. Call describe_program() for the index.")


# ----- resources: ftm:// (static, bundled model, no server call) -----


@mcp.resource("ftm://schemata")
def ftm_schemata() -> list[dict[str, Any]]:
    """Index of matchable FtM schemata."""
    return introspect.schema_index()


@mcp.resource("ftm://schema/{name}")
def ftm_schema(name: str) -> dict[str, Any]:
    """Property detail for one FtM schema."""
    return introspect.describe_schema(name)


# Topic / country vocabularies are the describe_topics / describe_countries *tools*
# above, not resources: the model needs them to pick filter values mid-conversation
# and clients don't reliably read resources (for the same reason, dataset lookup is
# the describe_dataset tool). Gender values ("male", "female", …) are
# self-explanatory, so there's no lookup for them at all. There is no yente://
# resource for live server state: the one candidate, the algorithm catalog, is
# deliberately unexposed (see the module docstring).
