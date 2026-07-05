"""Expose OpenSanctions screening and investigation to MCP clients.

Lets an assistant screen people, companies, and other entities against
sanctions, PEP, and watchlist data and walk their relationships — the same
matching surface as the yente SDK, framed for an analyst rather than a caller.

Five thin adapters over :class:`yente_client.AsyncClient` (``match_entity``,
``search_entities``, ``fetch_entity_by_id``, ``fetch_entity_relations``,
``fetch_entity_statements``) plus a set of ``describe_*`` lookup tools:
``describe_schema``, ``describe_topics`` and ``describe_countries`` over the
bundled FtM model, ``describe_dataset`` over the live catalog, and
``describe_program`` over the public program-catalog artifact. Every code in
an entity-bearing result resolves inline — topic/country labels from the
bundled model, dataset/program titles as attached legends — so an agent never
has to gloss a raw identifier. The ``ftm://schema`` resources mirror
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
from yente_client.models import Program

# OpenSanctions brand mark, served as square PNGs from the public asset CDN.
_ICON_BASE = "https://assets.opensanctions.org/images/nura"

# Every tool here only reads (screens, searches, fetches, describes) — nothing
# mutates server state — so they all share this read-only annotation.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# Every code in an entity-bearing result resolves inline: topic and country
# labels come from the bundled model (see shaping), dataset and program titles
# from live lookups cached below. An instruction to "resolve codes before
# reporting" was routinely ignored; a label sitting next to the code is not —
# so there is no note nudging the agent, just the labels.
#
# 15 minutes bounds how long a newly added dataset or sanctions program stays
# unresolvable; both catalogs change on slower cadences than that. Refreshes
# are cheap: programs.json revalidates via ETag (a 304 round-trip), and the
# catalog is a small response (see opensanctions/yente#1202 for giving it an
# ETag too).
_GLOSSARY_TTL_SECONDS = 900.0
_dataset_titles_cache: tuple[float, dict[str, str]] | None = None
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


async def _dataset_titles(client: AsyncClient) -> dict[str, str]:
    """Dataset name → title from the live catalog; cached, failure-soft.

    On a fetch error returns the stale cache if there is one, else an empty
    map — a missing legend must never fail a screening call.
    """
    global _dataset_titles_cache
    now = time.monotonic()
    if _dataset_titles_cache is not None:
        fetched_at, titles = _dataset_titles_cache
        if now - fetched_at < _GLOSSARY_TTL_SECONDS:
            return titles
    try:
        resp = await client.datasets()
    except YenteError:
        return _dataset_titles_cache[1] if _dataset_titles_cache is not None else {}
    titles = {d.name: d.title for d in resp.datasets if d.title}
    _dataset_titles_cache = (now, titles)
    return titles


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


async def _attach_glossaries(
    out: dict[str, Any], shaped: list[dict[str, Any]], client: AsyncClient
) -> None:
    """Attach dataset / program title legends for the codes present in ``shaped``.

    Legends are looked up live (cached) and failure-soft: one that can't be
    built is omitted, never an error. The program catalog is only fetched when
    a result actually carries a ``programId``.
    """
    dataset_names = {n for r in shaped for n in r.get("datasets", ())}
    if dataset_names:
        legend = shaping.title_glossary(dataset_names, await _dataset_titles(client))
        if legend:
            out["dataset_titles"] = legend
    program_ids = {p for r in shaped for p in r.get("properties", {}).get("programId", ())}
    if program_ids:
        legend = shaping.title_glossary(program_ids, await _program_titles(client))
        if legend:
            out["program_titles"] = legend


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
    so don't agonize over e.g. `country` vs `jurisdiction`.

    `score` is 0-1 confidence; `match` is true when score >= `threshold`
    (server default 0.7). Results are trimmed views — expand a candidate with
    fetch_entity_by_id.
    """
    entity = _build_entity(schema, properties)
    client = _resolve_client()
    try:
        resp = await client.match(
            entity,
            threshold=threshold,
            limit=limit,
            datasets=[dataset],
            topics=topics,
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    shaped = [shaping.shape_scored(r) for r in resp.results]
    out: dict[str, Any] = {"query_schema": schema, "total": resp.total.value, "results": shaped}
    await _attach_glossaries(out, shaped, client)
    return out


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
    shaped = [shaping.shape_entity(r) for r in resp.results]
    out: dict[str, Any] = {"total": resp.total.value, "results": shaped}
    await _attach_glossaries(out, shaped, client)
    return out


@mcp.tool(title="Fetch entity by ID", annotations=_READ_ONLY)
async def fetch_entity_by_id(entity_id: str) -> dict[str, Any]:
    """Fetch one entity by its OpenSanctions ID — its full own record.

    Returns the entity and all its intrinsic properties (names, dates,
    identifiers, addresses, …). Relationships (sanctions, ownership, family) are
    NOT here — traverse those with fetch_entity_relations. Use to expand a
    candidate from match_entity / search_entities, or a counterparty id returned
    by fetch_entity_relations. Needs a real entity ID, not a name.

    Topic and country codes arrive resolved, as top-level `topics` /
    `countries` code → label maps. If the returned `id` differs from the one
    requested, the entity was merged during deduplication — update any stored
    reference to the new canonical ID. The `referents` field lists the
    source-record and superseded IDs that map to this entity.
    """
    client = _resolve_client()
    try:
        entity = await client.fetch(entity_id, nested=False)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    # The detail tool: the full node (untrimmed) plus the inline glossaries.
    record = shaping.shape_full_record(entity)
    await _attach_glossaries(record, [record], client)
    return record


@mcp.tool(title="Fetch entity relations", annotations=_READ_ONLY)
async def fetch_entity_relations(
    entity_id: str,
    *,
    prop: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Traverse an entity's relationship graph — sanctions, owners, directors, family.

    Call with no `prop` first for an overview: every edge type with its total
    count and a capped first page. Then pass a `prop` ("sanctions",
    "ownershipOwner", "directorshipDirector", …) to walk one type, paging large
    "hub" nodes with `limit` / `offset`. Each result is the edge with its
    counterparty resolved to {id, caption, schema}; call fetch_entity_by_id on
    that id for the counterparty's full record. Prop names come from this tool's
    overview keys or from describe_schema. Beta; shape may change.
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
            overview = await client.adjacent(entity_id, limit=limit, offset=offset)
            return shaping.shape_adjacency(overview)
        block = await client.adjacent(entity_id, prop=prop, limit=limit, offset=offset)
        return shaping.shape_adjacency_property(block, entity_id)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc


@mcp.tool(title="Audit entity provenance", annotations=_READ_ONLY)
async def fetch_entity_statements(
    entity_id: str,
    *,
    prop: str | None = None,
    dataset: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Audit where an entity's data came from, statement by statement.

    Each statement is one property-value claim plus the dataset that asserted
    it and when it was first and last seen. Use after a hit to answer "which
    source says this?" — e.g. which list contributed an alias, birth date, or
    address. `entity_id` is the canonical ID from match/search/fetch results;
    each row's `entity_id` names the pre-deduplication source record that was
    merged into it. Narrow with `prop` (e.g. "alias") or `dataset`. Available
    on the hosted OpenSanctions API only — a self-hosted yente has no
    statement store and errors here.
    """
    client = _resolve_client()
    try:
        resp = await client.statements(
            canonical_id=entity_id,
            prop=prop,
            dataset=dataset,
            limit=limit,
            offset=offset,
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return shaping.shape_statements(resp)


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

    Use to pick codes for the `countries` filter on search_entities, or to
    resolve a code met outside shaped results (e.g. in a full record from
    fetch_entity_by_id). Country codes in match/search results arrive with a
    `countries` glossary already attached.
    """
    return introspect.country_values()


@mcp.tool(title="Describe a dataset", annotations=_READ_ONLY)
async def describe_dataset(name: str | None = None) -> dict[str, Any]:
    """Resolve dataset names to their metadata (live catalog).

    No arg → a compact index of every indexed dataset (name, title, tags like
    `list.sanction` / `list.pep`, entity_count) for discovery. A name (e.g.
    "us_ofac_sdn") → that dataset's full record: title, summary, publisher,
    coverage, counts, freshness. Dataset names in results already arrive with
    a `dataset_titles` legend; call this when the analyst needs the dataset's
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
    measures, and links. Program codes in results already arrive with a
    `program_titles` legend; call this when the analyst needs the program's
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
