"""Expose OpenSanctions screening and investigation to MCP clients.

Lets an assistant screen people, companies, and other entities against
sanctions, PEP, and watchlist data and walk their relationships — the same
matching surface as the yente SDK, framed for an analyst rather than a caller.

Four thin adapters over :class:`yente_client.AsyncClient` (``match_entity``,
``search_entities``, ``fetch_entity_by_id``, ``fetch_entity_relations``) plus a
set of ``describe_*`` lookup tools: ``describe_schema``, ``describe_topics`` and
``describe_countries`` over the bundled FtM model, ``describe_dataset`` over the
live catalog. The ``ftm://schema`` resources mirror ``describe_schema`` for
resource-capable clients; anything the model must dereference mid-conversation is
a tool, not a resource, since clients don't reliably read resources. Each request
carries the caller's API key as a bearer token, forwarded downstream (see
:mod:`yente_client.mcp.auth`).

Keep this module thin: the real logic lives in :mod:`~yente_client.mcp.introspect`
and :mod:`~yente_client.mcp.shaping`. Anything beyond plumbing belongs there so
it stays testable without FastMCP.
"""

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

# OpenSanctions brand mark, served as square PNGs from the public asset CDN.
_ICON_BASE = "https://assets.opensanctions.org/images/nura"

# Every tool here only reads (screens, searches, fetches, describes) — nothing
# mutates server state — so they all share this read-only annotation.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# Emitted alongside entity-bearing results so an agent *reading the output* — not
# just one that happened to read describe_topics' description — is pushed to
# resolve codes. Guards the observed failure mode: glossing a legible-looking code
# (e.g. reading `crime.war` as generic crime) instead of resolving the vocabulary.
_RESOLVE_CODES_NOTE = (
    "Codes in these results are raw: resolve topic tags with describe_topics, "
    "country codes with describe_countries, and dataset names with describe_dataset "
    "before reporting them — even ones that look self-explanatory."
)

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
    algorithm: str | None = None,
    topics: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Screen one entity against sanctions/PEP/watchlist data; return scored matches.

    Use for ANY matching task, even with partial data. `schema` is an FtM type
    (Person, Company, ...) and `properties` maps FtM property names to value
    lists ({"name": ["Jane Doe"], "birthDate": ["1975"]}). Call describe_schema
    first if unsure of property names. Send every property you have.

    Results carry raw codes — topic tags, country codes, dataset names. Resolve
    them with describe_topics / describe_countries / describe_dataset before
    reporting, even ones that look self-explanatory (`crime.war` is "War crimes",
    not generic crime).
    """
    entity = _build_entity(schema, properties)
    client = _resolve_client()
    try:
        resp = await client.match(
            entity,
            threshold=threshold,
            algorithm=algorithm,
            limit=limit,
            datasets=[dataset],
            topics=topics,
        )
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    return {
        "query_schema": schema,
        "total": resp.total.value,
        "results": [shaping.shape_scored(r) for r in resp.results],
        "note": _RESOLVE_CODES_NOTE,
    }


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
    """Free-text search for human-style lookup; returns plain (unscored) entities.

    Not a fallback for match_entity: for a match/no-match decision on a known
    person or company, use match_entity even with partial input.

    Results carry raw codes (topics, countries, datasets); resolve them with the
    describe_* tools before reporting, even ones that look self-explanatory.
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
    return {
        "total": resp.total.value,
        "results": [shaping.shape_entity(r) for r in resp.results],
        "note": _RESOLVE_CODES_NOTE,
    }


@mcp.tool(title="Fetch entity by ID", annotations=_READ_ONLY)
async def fetch_entity_by_id(entity_id: str) -> dict[str, Any]:
    """Fetch one entity by its OpenSanctions ID — its full own record.

    Returns the entity and all its intrinsic properties (names, dates,
    identifiers, addresses, …). Relationships (sanctions, ownership, family) are
    NOT here — traverse those with fetch_entity_relations. Use to expand a
    candidate from match_entity / search_entities, or a counterparty id returned
    by fetch_entity_relations. Needs a real entity ID, not a name.
    """
    client = _resolve_client()
    try:
        entity = await client.fetch(entity_id, nested=False)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc
    # The detail tool: return the full node, not a trimmed view.
    return entity.model_dump(by_alias=True, mode="json", exclude_none=True)


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
    """Resolve topic tags to labels — the whole `topic` vocabulary (value → label).

    Returns the full map in one call so several tags seen together (`role.pep`,
    `role.pol`, `sanction`, …) resolve at once. Use to dereference topic codes in
    match_entity / search_entities results.
    """
    return introspect.topic_values()


@mcp.tool(title="Describe country codes", annotations=_READ_ONLY)
def describe_countries() -> dict[str, str]:
    """Resolve country / jurisdiction codes to names — the whole `country` vocabulary.

    Returns the full code → name map in one call. Use to dereference country codes
    seen in results (e.g. `gb` → United Kingdom).
    """
    return introspect.country_values()


@mcp.tool(title="Describe a dataset", annotations=_READ_ONLY)
async def describe_dataset(name: str | None = None) -> dict[str, Any]:
    """Resolve dataset names to their metadata (live catalog).

    No arg → a compact index of every indexed dataset (name, title, tags like
    `list.sanction` / `list.pep`, entity_count) for discovery. A name (e.g.
    "us_ofac_sdn") → that dataset's full record: title, summary, publisher,
    coverage, counts, freshness. Use to dereference dataset names seen in
    match_entity / search_entities results.
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
# above, not resources: the model needs them to dereference codes mid-conversation
# and clients don't reliably read resources. Gender values ("male", "female", …)
# are self-explanatory, so there's no lookup for them at all.


# ----- resources: yente:// (live server state) -----
# Dataset lookup lives in the describe_dataset *tool*, not a resource: clients
# don't reliably let the model read resources, so a code → metadata lookup it
# needs mid-conversation has to be a tool. algorithms stays a resource for now
# (rarely needed for dereferencing); revisit if it hits the same wall.


@mcp.resource("yente://algorithms")
async def yente_algorithms() -> dict[str, Any]:
    """Available scoring algorithms and their descriptions (live)."""
    resp = await _resolve_client().algorithms()
    return resp.model_dump(by_alias=True, mode="json", exclude_none=True)
