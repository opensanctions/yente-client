"""The FastMCP server: tool and resource wiring over the yente SDK.

Five tools — four thin adapters over :class:`yente_client.AsyncClient`
(``match_entity``, ``search_entities``, ``fetch_entity_by_id``,
``fetch_entity_relations``) plus ``describe_schema`` over the bundled FtM model —
and five resources (static ``ftm://`` model views, live ``yente://`` server
state). Each request carries the caller's API key as a bearer token, forwarded
downstream (see :mod:`yente_client.mcp.auth`).

Keep this module thin: the real logic lives in :mod:`~yente_client.mcp.introspect`
and :mod:`~yente_client.mcp.shaping`. Anything beyond plumbing belongs there so
it stays testable without FastMCP.
"""

from typing import Any

from pydantic import ValidationError

from yente_client import entities, env
from yente_client.async_client import AsyncClient
from yente_client.entities import EntityInput
from yente_client.exceptions import YenteError
from yente_client.mcp import introspect, shaping
from yente_client.mcp._deps import FastMCP, ToolError, get_http_headers
from yente_client.mcp.auth import client_for, resolve_api_key
from yente_client.mcp.errors import describe_error

BASE_URL = env.base_url()
# Fallback API key for the whole server, used when a request carries no bearer
# token — lets you run yente-mcp locally against a real API for testing.
API_KEY = env.api_key()

mcp: FastMCP = FastMCP(
    name="yente",
    instructions=(
        "Screen and research people, companies, and other entities against the "
        "OpenSanctions database (sanctions lists, PEPs, watchlists). For any "
        "match/no-match question — even from a partial record — use match_entity. "
        "Use search_entities only to back a human-style search box. Call "
        "describe_schema first to learn real FtM property names (birthDate, "
        "registrationNumber) before building a match."
    ),
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


@mcp.tool
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
    }


@mcp.tool
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
    }


@mcp.tool
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


@mcp.tool
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
    client = _resolve_client()
    try:
        if prop is None:
            overview = await client.adjacent(entity_id, limit=limit, offset=offset)
            return shaping.shape_adjacency(overview)
        block = await client.adjacent(entity_id, prop=prop, limit=limit, offset=offset)
        return shaping.shape_adjacency_property(block, entity_id)
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc


@mcp.tool
def describe_schema(schema: str | None = None) -> dict[str, Any]:
    """Look up the FtM data model (offline). No arg → index of matchable schemata;
    a name (e.g. "Person") → that schema's properties, their types, and which
    relationship properties point where. Use before match_entity.
    """
    if schema is None:
        return {"schemata": introspect.schema_index()}
    try:
        return introspect.describe_schema(schema)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ----- resources: ftm:// (static, bundled model, no server call) -----


@mcp.resource("ftm://schemata")
def ftm_schemata() -> list[dict[str, Any]]:
    """Index of matchable FtM schemata."""
    return introspect.schema_index()


@mcp.resource("ftm://schema/{name}")
def ftm_schema(name: str) -> dict[str, Any]:
    """Property detail for one FtM schema."""
    return introspect.describe_schema(name)


@mcp.resource("ftm://topics")
def ftm_topics() -> dict[str, str]:
    """The `topic` vocabulary (value → label)."""
    return introspect.topic_values()


@mcp.resource("ftm://countries")
def ftm_countries() -> dict[str, str]:
    """The `country` vocabulary (code → name)."""
    return introspect.country_values()


@mcp.resource("ftm://genders")
def ftm_genders() -> dict[str, str]:
    """The `gender` vocabulary (value → label)."""
    return introspect.gender_values()


# ----- resources: yente:// (live server state) -----
# TODO: confirm the inbound Authorization header is reachable from a resource
# read (it is from a tool call); if not, these may need to be tools instead.


@mcp.resource("yente://catalog")
async def yente_catalog() -> dict[str, Any]:
    """Indexed datasets and their freshness (live)."""
    resp = await _resolve_client().datasets()
    return resp.model_dump(by_alias=True, mode="json", exclude_none=True)


@mcp.resource("yente://algorithms")
async def yente_algorithms() -> dict[str, Any]:
    """Available scoring algorithms and their descriptions (live)."""
    resp = await _resolve_client().algorithms()
    return resp.model_dump(by_alias=True, mode="json", exclude_none=True)
