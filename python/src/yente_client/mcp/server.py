"""Expose OpenSanctions screening and investigation to MCP clients.

Lets an assistant screen people, companies, and other entities against
sanctions, PEP, and watchlist data and walk their relationships — the same
matching surface as the yente SDK, framed for an analyst rather than a caller.

Adapters over :class:`yente_client.AsyncClient` (``match_entity``,
``search_entities``, ``fetch_entity_by_id`` — an entity profile assembled from
the record and its relations — ``fetch_entity_descriptions``,
``fetch_entity_relations``, ``fetch_entity_statements``) plus ``describe``, a
lookup of schemata, topics and countries from the bundled FtM model, or of one
dataset or program record by name — the catalogs are too large to list whole.
Every entity-bearing result resolves its own codes — topic labels inline,
everything else through one ``legend`` per response (see
:mod:`~yente_client.mcp.legend`) — so an agent never has to gloss a raw
identifier. The server instructions
(:data:`yente_client.env.DEFAULT_MCP_INSTRUCTIONS`) carry the data model and
the rules shared by every tool; tool docstrings cover only their own tool. The
server exposes no resources: anything the model must read mid-conversation is a
tool, since clients don't reliably read resources. Gender values need no
lookup; they explain themselves. Matching algorithms are deliberately not exposed:
algorithm choice is a tuning detail below this server's altitude — the server
default applies, and only its feature descriptions reach the legend; use the
SDK or CLI to experiment. Each request carries the caller's API key, or with
OAuth2 on its access token, as a bearer token, forwarded downstream (see
:mod:`yente_client.mcp.auth`).

Keep this module thin: the real logic lives in :mod:`yente_client.schemas`,
:mod:`~yente_client.mcp.shaping` and :mod:`~yente_client.mcp.legend`. Anything
beyond plumbing belongs there so it stays testable without FastMCP.
"""

import asyncio
import contextlib
import functools
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Generic, Literal, TypeVar

from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter, ValidationError

from yente_client import __version__, env, schemas
from yente_client._http import is_opensanctions_url
from yente_client.async_client import AsyncClient
from yente_client.entities import EntityInput, entity_class
from yente_client.exceptions import YenteError
from yente_client.mcp import legend, shaping
from yente_client.mcp._deps import (
    CallNext,
    CallToolRequestParams,
    FastMCP,
    Icon,
    JWTVerifier,
    Middleware,
    MiddlewareContext,
    RemoteAuthProvider,
    ToolAnnotations,
    ToolError,
    ToolResult,
    get_http_headers,
)
from yente_client.mcp.auth import parse_bearer
from yente_client.mcp.errors import describe_error
from yente_client.models import (
    AdjacentPropertyResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    ProgramsResponse,
    Statement,
    StatementsResponse,
)

T = TypeVar("T")

# OpenSanctions brand mark, served as square PNGs from the public asset CDN.
_ICON_BASE = "https://assets.opensanctions.org/images/nura"

# Every tool here only reads (screens, searches, fetches, describes) — nothing
# mutates server state — so they all share this read-only annotation. They query
# one closed database, not the open web, hence open_world_hint=False.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Page sizes a caller sees. Every list a tool pages through (search results,
# one relation type, statements) defaults to _PAGE_SIZE and is capped at
# _MAX_PAGE_SIZE: a page of 100 ownership edges (~105k characters) overflows MCP
# clients' tool-output caps, while paging is free. A profile previews a few
# records per section and relationship type so hub entities stay readable (and
# fit those caps), the overview of every type a few more. Tool docstrings state
# the default and the cap; a test keeps them in step with these values.
_PAGE_SIZE = 10
_MAX_PAGE_SIZE = 25
_PROFILE_EDGES = 3
_OVERVIEW_EDGES = 5
# Candidates returned below cutoff when a screen comes back empty.
_NEAR_MISS_LIMIT = 3

# Read sizes no caller sees. The descriptions tool reads designation records in
# one go, and statements per prose property likewise (Gazprom, a heavy case, has
# ~65 notes), then pages the texts by a character budget.
_SECTION_READ = 50
_STATEMENT_READ = 1000
_TEXT_PAGE_CHARS = 15_000

# Every code in an entity-bearing result resolves in the response itself:
# schema, property and value labels come from the bundled model (see shaping),
# dataset, program and scoring-feature records from live lookups cached below.
# Labels ship with the data rather than as an instruction to resolve codes:
# agents use a label next to its code and skip a separate lookup step.
#
# 5 minutes bounds how long a newly added dataset or sanctions program stays
# unresolvable, and how stale a response's reported `as_of` can be; the catalogs
# change on slower cadences than that. Refreshes
# are cheap: programs.json revalidates via ETag (a 304 round-trip), and the
# catalog is a small response (see opensanctions/yente#1202 for giving it an
# ETag too).
_GLOSSARY_TTL_SECONDS = 300.0


class _TTLCache(Generic[T]):
    """Hold one value fetched from yente, refetched once older than ``ttl`` seconds.

    A failed refetch serves the stale value when one is held.
    """

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self.clear()

    def clear(self) -> None:
        self._value: T | None = None
        self._fetched_at = 0.0

    async def get(self, fetch: Callable[[], Awaitable[T]]) -> T:
        """Return the held value, refetching it when stale.

        Raises ``YenteError`` only when the fetch fails and nothing is held.
        """
        now = time.monotonic()
        if self._value is not None and now - self._fetched_at < self.ttl:
            return self._value
        try:
            self._value = await fetch()
        except YenteError:
            if self._value is None:
                raise
            return self._value
        self._fetched_at = now
        return self._value


# Process-wide, not per client: the catalogs are the same for every caller.
_catalog: _TTLCache[DatasetsResponse] = _TTLCache(_GLOSSARY_TTL_SECONDS)
_programs: _TTLCache[ProgramsResponse] = _TTLCache(_GLOSSARY_TTL_SECONDS)
_algorithms: _TTLCache[AlgorithmsResponse] = _TTLCache(_GLOSSARY_TTL_SECONDS)


def _reset_caches() -> None:
    for cache in (_catalog, _programs, _algorithms):
        cache.clear()


BASE_URL = env.base_url()
DATASET = env.mcp_dataset()
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

# One client, and so one connection pool, for every tool call. It carries no
# key: _resolve_client() picks one per request.
_base_client = AsyncClient(base_url=BASE_URL, app_name="yente-mcp")


def _auth() -> RemoteAuthProvider | None:
    """Make the server an OAuth2 resource server, if ``$YENTE_MCP_OAUTH_ISSUER`` is set.

    The provider serves the protected resource metadata (RFC 9728) and answers
    requests without a valid access token from the issuer with ``401`` and a
    ``WWW-Authenticate`` header that names the metadata. A bare
    ``JWTVerifier`` serves no metadata, so MCP clients would not find the
    issuer. The audience is the MCP endpoint: MCP clients request it as the
    RFC 8707 ``resource``, and the issuer puts it into the token's ``aud``.
    """
    issuer = env.mcp_oauth_issuer()
    public_url = env.mcp_public_url()
    if issuer is None:
        return None
    if public_url is None:
        raise ValueError(f"{env.MCP_OAUTH_ISSUER_VAR} needs {env.MCP_PUBLIC_URL_VAR} too")
    return RemoteAuthProvider(
        token_verifier=JWTVerifier(
            jwks_uri=f"{issuer}/.well-known/jwks.json",
            issuer=issuer,
            audience=f"{public_url}/mcp",
        ),
        # RFC 9728 clients compare the listed authorization server with the
        # issuer in its own metadata character by character. Pydantic adds a
        # "/" path to a bare host URL by default, which breaks that match for
        # an issuer like "https://auth.example.org".
        authorization_servers=[
            TypeAdapter(
                AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True)
            ).validate_python(issuer)
        ],
        base_url=public_url,
        # offline_access gets the client a refresh token.
        scopes_supported=["openid", "offline_access"],
    )


mcp: FastMCP = FastMCP(
    # A self-hosted deployment rebrands via $YENTE_MCP_NAME, $YENTE_MCP_TITLE,
    # $YENTE_MCP_DESCRIPTION and $YENTE_MCP_INSTRUCTIONS.
    name=env.mcp_name(),
    version=__version__,
    website_url="https://www.opensanctions.org",
    icons=[
        # Full-colour brand mark first (renders on any background), favicons as
        # smaller fallbacks. Clients pick by `sizes`.
        Icon(src=f"{_ICON_BASE}/logo-icon-color.png", mime_type="image/png", sizes=["290x310"]),
        Icon(src=f"{_ICON_BASE}/favicon-32.png", mime_type="image/png", sizes=["32x32"]),
        Icon(src=f"{_ICON_BASE}/favicon-16.png", mime_type="image/png", sizes=["16x16"]),
    ],
    instructions=env.mcp_instructions(),
    # MCP clients start the OAuth2 login, and refresh an expired token, only
    # when the MCP endpoint answers 401. So with an issuer set, the server
    # rejects requests without a valid access token before any tool runs. This
    # also means that _resolve_client() never falls back to the server's key.
    auth=_auth(),
)
# FastMCP takes no title or description, but the low-level server sends both
# in the initialize response's serverInfo.
mcp._mcp_server.title = env.mcp_title()
mcp._mcp_server.description = env.mcp_description()


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
    """Return the API client for the current MCP request.

    Uses the request's bearer token. A request without one uses the server's
    ``OPENSANCTIONS_API_KEY``, so ``yente-mcp`` runs on localhost against a
    real API with no auth set up in the MCP client.

    With OAuth2, the bearer token is the caller's access token, and the API
    gets that same token. The MCP specification forbids this pass-through
    ("MCP servers MUST NOT pass through the token it received from the MCP
    client"). The rule stops an API from accepting a token that the issuer
    made for a different service. That risk does not apply here: the server
    and the API are one resource with one issuer, and the API operator
    explicitly accepts the MCP endpoint as a token audience. Token exchange
    (RFC 8693) obeys the rule, but it needs support from the issuer, a second
    request for each call, and a client secret on the server.
    """
    # FastMCP leaves credential headers out unless they are asked for by name.
    headers = get_http_headers(include={"authorization"})
    # The caller's own key wins. The server key applies only to requests with
    # no bearer token, so a caller never screens on the server's account.
    api_key = parse_bearer(headers.get("authorization")) or API_KEY
    if api_key is None:
        return _base_client
    return _base_client.with_api_key(api_key)


async def _soft(cache: _TTLCache[T], fetch: Callable[[], Awaitable[T]]) -> T | None:
    """Read a cached lookup that only enriches a response: a failure yields ``None``."""
    try:
        return await cache.get(fetch)
    except YenteError:
        return None


@contextlib.contextmanager
def _tool_errors() -> Iterator[None]:
    """Turn an SDK error into a tool error saying what failed and whether to retry.

    A context manager rather than middleware: FastMCP wraps a tool's exception
    in its own ``ToolError`` before middleware sees it.
    """
    try:
        yield
    except YenteError as exc:
        raise ToolError(describe_error(exc)) from exc


async def _with_legend(
    out: dict[str, Any],
    client: AsyncClient,
    codes: legend.LegendCodes | None = None,
    *,
    as_of: bool = True,
) -> dict[str, Any]:
    """Attach the response's ``legend`` for every code it mentions, and return it.

    The legend's ``as_of`` dates the data an entity response came from;
    describe records pass ``as_of=False``.

    ``codes`` replaces the walk of ``out`` when the caller knows codes the walk
    can't see (relation keys, a describe record's countries). Program and
    feature lookups are live (cached), failure-soft, and only run when the
    response carries a code they resolve. A response with nothing to resolve
    gets no legend at all, not one holding only link templates.
    """
    if codes is None:
        codes = legend.collect_codes(out)
    catalog = await _soft(_catalog, client.datasets)
    programs = algorithms = None
    if codes.programs:
        programs = await _soft(_programs, client.programs)
    if codes.features:
        algorithms = await _soft(_algorithms, client.algorithms)
    resolved = legend.build_legend(
        codes,
        datasets=legend.dataset_records(catalog) if catalog else {},
        programs=legend.program_records(programs.data) if programs else {},
        features=legend.feature_descriptions(algorithms) if algorithms else {},
        links=_LINKS,
        as_of=legend.as_of(catalog, DATASET) if catalog and as_of else None,
    )
    if resolved.keys() - {"links"}:
        out["legend"] = resolved
    return out


def _cap(limit: int) -> int:
    """Clamp a caller's page size to between 1 and :data:`_MAX_PAGE_SIZE`."""
    return max(1, min(limit, _MAX_PAGE_SIZE))


def _build_entity(schema: str, properties: dict[str, list[str]]) -> EntityInput:
    """Construct a typed input entity from a schema name + property bag."""
    try:
        schema_cls = entity_class(schema)
    except KeyError:
        raise ToolError(shaping.unknown_schema(schema)) from None
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
    dataset: str = DATASET,
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
    candidate values each compared, each with its signed `contribution`; the
    contributions add up to the score. A fuzzy name with nothing else agreeing is
    a lead, not an identification; identifiers and dates the query lacked are
    what would settle it. Candidates come in score order, which reflects match
    quality, not risk; `match_topics` gathers the topics of every candidate
    with `match` true. `query_suggested_properties` names key properties the
    query lacked. Candidates are abridged to their identity, topics
    and counts; profile the ones worth a closer look. When no candidate clears the
    cutoff, `near_misses` shows the closest ones and why they fell short. `checked`
    echoes the scope screened.

    Args:
        dataset: Collection to screen against; the default covers every source
            the server loads.
        threshold: Score at which `match` turns true; leave to the server.
        cutoff: Score below which candidates are dropped; leave to the server.
        limit: Candidates to return, at most 25.
    """
    entity = _build_entity(schema, properties)
    client = _resolve_client()
    screen = functools.partial(
        client.match, entity, threshold=threshold, datasets=[dataset], topics=topics
    )
    with _tool_errors():
        resp = await screen(cutoff=cutoff, limit=None if limit is None else _cap(limit))
        near_misses = None
        if not resp.results and cutoff is None:
            near_misses = await screen(cutoff=0.0, limit=_NEAR_MISS_LIMIT)
    checked = {
        "dataset": dataset,
        "topics": topics,
        "threshold": threshold,
        "cutoff": cutoff,
    }
    algorithms = await _soft(_algorithms, client.algorithms)
    coefficients = legend.feature_coefficients(algorithms) if algorithms else {}
    results = [shaping.shape_scored(r, coefficients) for r in resp.results]
    query_properties = sorted(name for name, values in properties.items() if values)
    suggested = [p for p in shaping.featured_names(schema) if p not in query_properties]
    match_topics: dict[str, str] = {}
    for result in results:
        if result["match"]:
            match_topics.update(result.get("topics", {}))
    out: dict[str, Any] = {
        "query_schema": schema,
        "query_properties": query_properties,
        "query_suggested_properties": suggested,
        "checked": {k: v for k, v in checked.items() if v is not None},
        "total": resp.total.value,
        "returned": len(results),
        "match_topics": dict(sorted(match_topics.items())),
        "results": results,
    }
    for key in ("query_suggested_properties", "match_topics"):
        if not out[key]:
            del out[key]
    if near_misses is not None and near_misses.results:
        out["near_misses"] = [shaping.shape_scored(r, coefficients) for r in near_misses.results]
    # `checked` echoes the caller's own scope, which needs no glossing.
    codes = legend.collect_codes({k: v for k, v in out.items() if k != "checked"})
    codes.add_properties(schema, [*query_properties, *suggested])
    return await _with_legend(out, client, codes)


@mcp.tool(title="Search entities", annotations=_READ_ONLY, output_schema=None)
async def search_entities(
    q: str,
    *,
    dataset: str = DATASET,
    schema: str | None = None,
    countries: list[str] | None = None,
    topics: list[str] | None = None,
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    fuzzy: bool = False,
    simple: bool = False,
) -> dict[str, Any]:
    """Browse the database by keyword; return unscored entities.

    For exploring: what a term turns up, narrowed by schema, country or topic
    (vessels flagged to a country, companies with a word in their name).
    Never use it to decide whether a person or company is listed — results
    carry no score or explanation, so neither a hit nor its absence proves
    anything. Use match_entity for that, even with just a name. Results are
    abridged to their identity, topics and counts.

    Args:
        limit: Results per page (default 10), at most 25.
        fuzzy: Tolerate spelling variation in `q`.
        simple: Read `q` as plain words, as typed into a search box, rather
            than query syntax (AND / OR, quotes, field:value).
    """
    client = _resolve_client()
    with _tool_errors():
        resp = await client.search(
            q,
            limit=_cap(limit),
            offset=offset,
            fuzzy=fuzzy,
            simple=simple,
            datasets=[dataset],
            schema=schema,
            countries=countries,
            topics=topics,
        )
    out = shaping.page(
        [shaping.shape(r, "ref") for r in resp.results],
        resp.total,
        offset,
        "search_entities",
        q=q,
        dataset=None if dataset == DATASET else dataset,
        schema=schema,
        countries=countries,
        topics=topics,
        limit=None if limit == _PAGE_SIZE else limit,
        fuzzy=fuzzy or None,
        simple=simple or None,
    )
    return await _with_legend(out, client)


@mcp.tool(title="Profile an entity", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_by_id(entity_id: str, *, full: bool = False) -> dict[str, Any]:
    """Profile one entity by ID: the call for "tell me about X".

    Returns the entity's record (names, dates, identifiers, addresses) and
    the first few records of its risk basis:

    - `sanctions`: designations that impose a restriction (program,
      authority, dates).
    - `risks`: listings that name the entity without a restriction (watch
      lists, advisories, persons-of-interest registers).
    - `positionOccupancies`: positions held, with country, dates and status.

    `relations` previews every other relationship type (ownership,
    directorships, family, associates, …) the same way. Each section and type
    carries its `total`, and `next` names the call for the rest. Each edge
    keeps its own details (role, share, relationship, dates); the entity at its
    far end is abridged to its identity, topics and counts. Prose is left out:
    `narrative_text` names the call for the notes and designation reasons. The
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
    with _tool_errors():
        entity = await client.fetch(entity_id, nested=False)
    if full:
        return await _with_legend(shaping.shape_raw(entity), client)
    try:
        adjacency = await client.adjacent(entity.id, limit=_PROFILE_EDGES)
    except YenteError as exc:
        profile = shaping.shape(entity)
        profile["relations_unavailable"] = describe_error(exc)
        return await _with_legend(profile, client)
    return await _with_legend(shaping.shape_profile(entity, adjacency), client)


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

    While `next` is set, it names the call for the next page. `credits_unavailable` means
    the texts carry no per-source credits (a self-hosted yente has no
    statement store); `relations_unavailable` means designation reasons could
    not be read.
    """
    client = _resolve_client()
    with _tool_errors():
        entity = await client.fetch(entity_id, nested=False)
    text_props = shaping.prose_names(entity.schema_)
    edge_props = shaping.designation_props(entity.schema_)
    statement_calls = [
        client.statements(canonical_id=entity.id, prop=prop, limit=_STATEMENT_READ)
        for prop in text_props
    ]
    edge_calls = [client.adjacent(entity.id, prop=p, limit=_SECTION_READ) for p in edge_props]
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
    catalog = await _soft(_catalog, client.datasets)
    records = legend.dataset_records(catalog) if catalog else {}
    unofficial = {name for name, rec in records.items() if rec.get("official") is False}
    entries = shaping.text_entries(entity, statements, designations, unofficial)
    out.update(
        shaping.page_entries(
            entries, offset, _TEXT_PAGE_CHARS, "fetch_entity_descriptions", entity_id=entity.id
        )
    )
    return await _with_legend(out, client)


@mcp.tool(title="Fetch entity relations", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_relations(
    entity_id: str,
    *,
    prop: str | None = None,
    # None, not _PAGE_SIZE: without `prop` the limit is per type, defaulting
    # to the overview's _OVERVIEW_EDGES.
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through one relationship type of an entity in full.

    A relationship is itself an entity (Ownership, Directorship, Family, …)
    linking two others, and `prop` names the side this entity is on — the keys
    of a profile's `relations`, or `sanctions` / `risks` /
    `positionOccupancies`. For a company, "ownershipOwner" lists what it owns
    and "ownershipAsset" who owns it. Each edge carries its own properties
    (role, share, dates); the entity at its far end is abridged to its
    identity, topics and counts. Pages hold `limit` edges (default 10,
    at most 25); `next` names the call for the next page and is null on the
    last.
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
    with _tool_errors():
        if prop is None:
            overview = await client.adjacent(
                entity_id, limit=_cap(_OVERVIEW_EDGES if limit is None else limit), offset=offset
            )
            out = shaping.shape_adjacency(overview)
            codes = legend.collect_codes(out)
            codes.add_properties(overview.entity.schema_, out)
        else:
            block = await client.adjacent(
                entity_id,
                prop=prop,
                limit=_cap(_PAGE_SIZE if limit is None else limit),
                offset=offset,
            )
            out = shaping.shape_relation_page(block, entity_id, prop, limit=limit)
            codes = legend.collect_codes(out)
            codes.add_properties(None, [prop])
    return await _with_legend(out, client, codes)


@mcp.tool(title="Audit entity provenance", annotations=_READ_ONLY, output_schema=None)
async def fetch_entity_statements(
    entity_id: str,
    *,
    prop: str | None = None,
    dataset: str | None = None,
    sort: list[str] | None = None,
    limit: int = _PAGE_SIZE,
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
        limit: Statements per page (default 10), at most 25.
    """
    client = _resolve_client()
    with _tool_errors():
        resp = await client.statements(
            canonical_id=entity_id,
            prop=prop,
            dataset=dataset,
            sort=sort,
            limit=_cap(limit),
            offset=offset,
        )
    out = shaping.shape_statements(
        resp,
        entity_id=entity_id,
        prop=prop,
        dataset=dataset,
        sort=sort,
        limit=None if limit == _PAGE_SIZE else limit,
    )
    return await _with_legend(out, client)


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
            return {"schemata": schemas.schema_index(matchable_only=True)}
        if not schemas.has_schema(name):
            raise ToolError(shaping.unknown_schema(name))
        return schemas.describe_schema(name)
    if kind == "topics":
        return {"about": _TOPICS_ABOUT, "topics": schemas.type_values("topic")}
    return schemas.type_values("country")


async def _describe_dataset(name: str) -> dict[str, Any]:
    """One dataset record from the live catalog, with its legend."""
    client = _resolve_client()
    with _tool_errors():
        resp = await _catalog.get(client.datasets)
    for dataset in resp.datasets:
        if dataset.name == name:
            out = dataset.model_dump(by_alias=True, mode="json", exclude_none=True)
            return await _with_legend(out, client, legend.dataset_codes(dataset), as_of=False)
    titles = {d.name: d.title for d in resp.datasets}
    raise ToolError(shaping.unknown_name("dataset", name, titles))


async def _describe_program(key: str) -> dict[str, Any]:
    """One sanctions program by key or alias, with its legend."""
    client = _resolve_client()
    with _tool_errors():
        catalog = (await _programs.get(client.programs)).data
    for program in catalog:
        if program.key == key or key in program.aliases:
            out = program.model_dump(mode="json", exclude_none=True)
            return await _with_legend(out, client, legend.program_codes(program), as_of=False)
    titles = {p.key: p.title for p in catalog}
    raise ToolError(shaping.unknown_name("program", key, titles))
