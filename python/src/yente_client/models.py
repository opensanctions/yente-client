"""Pydantic response models for the yente / OpenSanctions API.

These mirror the wire format, reshaped where a flatter surface reads better —
notably ``MatchResponse``, flattened from the ``responses[<key>]`` envelope.

``extra="ignore"`` on every response model means unknown server-side fields
are silently dropped, so the client doesn't break when yente adds a field. A
field the client doesn't model yet becomes available only after a client
release that adds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TotalSpec(BaseModel):
    """Total-count envelope returned alongside paginated results.

    ``relation`` indicates whether ``value`` is exact (``"eq"``) or a lower
    bound (``"gte"``); the server uses ``"gte"`` when result counts hit
    Elasticsearch's track_total_hits limit.
    """

    model_config = ConfigDict(extra="ignore")

    value: int
    relation: Literal["eq", "gte"]


class FeatureResult(BaseModel):
    """Per-feature breakdown of how a match score was computed.

    Lives under ``ScoredEntity.explanations`` keyed by feature name (e.g.
    ``"name_match"``, ``"birth_date_match"``). Useful for showing users why
    a candidate scored as it did.
    """

    model_config = ConfigDict(extra="ignore")

    score: float
    detail: str | None = None
    query: str | None = None
    candidate: str | None = None
    weight: float | None = None


class Entity(BaseModel):
    """A FtM entity as the server returns it.

    Different from the input entity classes (``Person``, ``Company``, …):
    response entities are dict-shaped (any schema) and carry server-only
    metadata (datasets, referents, target, timestamps). Property values can
    be nested entities when the server traverses adjacent edges.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    caption: str
    schema_: str = Field(alias="schema")
    properties: dict[str, list[str | Entity]] = Field(default_factory=dict)
    datasets: list[str] = Field(default_factory=list)
    referents: list[str] = Field(default_factory=list)
    target: bool = False
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    last_change: datetime | None = None


class ScoredEntity(Entity):
    """An ``Entity`` plus match-specific scoring fields.

    Returned only by ``/match``; ``score`` is in [0.0, 1.0] and ``match`` is
    true when ``score >= threshold`` per the request (defaults to 0.70).
    """

    score: float
    match: bool
    explanations: dict[str, FeatureResult] = Field(default_factory=dict)

    @property
    def contributing_explanations(self) -> dict[str, FeatureResult]:
        """The features that actually moved the score — the non-zero subset.

        Use when presenting *why* a candidate scored as it did: a match response
        enumerates every feature the algorithm considered, including ones that
        carried no signal (``0.0`` — a property the query didn't supply, or one
        that doesn't apply to the schema). Those are noise in an explanation.
        Negative features (penalties) are kept; only exact zeros are dropped.
        ``explanations`` is left intact for callers that want the full set.
        """
        return {name: f for name, f in self.explanations.items() if f.score}


class MatchResponse(BaseModel):
    """Flat response for ``/match``.

    The wire format wraps the result in ``responses[<key>]``; the call layer
    unwraps that envelope so callers always see a flat object. ``query``
    echoes the input as the server saw it (post-cleaning).
    """

    model_config = ConfigDict(extra="ignore")

    query: dict[str, Any]
    results: list[ScoredEntity]
    total: TotalSpec
    limit: int

    @property
    def top(self) -> ScoredEntity | None:
        """Highest-scoring result, or ``None`` if ``results`` is empty.

        Server returns results sorted by score descending, so the first
        element is the top hit.
        """
        return self.results[0] if self.results else None

    @property
    def matches(self) -> list[ScoredEntity]:
        """Results with ``match=True`` (score crossed the threshold)."""
        return [r for r in self.results if r.match]


@dataclass(frozen=True)
class MatchError:
    """A failed item in a ``match_iter`` stream, returned in-band.

    With ``on_error="collect"`` a long batch run keeps going past
    individual failures; each one surfaces as a ``(key, MatchError)``
    pair instead of raising, so the caller can log or retry that row
    without losing the rest of the stream. Never raised.
    """

    key: str
    exception: Exception


class SearchFacetItem(BaseModel):
    """One value within a search facet (e.g. one country in a countries facet)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    label: str
    count: int


class SearchFacet(BaseModel):
    """A facet aggregation under ``SearchResponse.facets``."""

    model_config = ConfigDict(extra="ignore")

    label: str
    values: list[SearchFacetItem] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Response shape for ``/search``."""

    model_config = ConfigDict(extra="ignore")

    results: list[Entity]
    facets: dict[str, SearchFacet] = Field(default_factory=dict)
    total: TotalSpec
    limit: int
    offset: int


class StatusResponse(BaseModel):
    """Body of ``/healthz`` and ``/readyz``: ``{"status": "ok"}``."""

    model_config = ConfigDict(extra="ignore")

    status: str


class Algorithm(BaseModel):
    """Description of one enabled matching algorithm.

    Returned in the ``algorithms`` list of ``AlgorithmsResponse``. The
    ``docs`` dict maps feature name → human-readable description.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    docs: dict[str, Any] | None = None


class AlgorithmsResponse(BaseModel):
    """Body of ``/algorithms``."""

    model_config = ConfigDict(extra="ignore")

    algorithms: list[Algorithm]
    default: str
    best: str


class DataPublisher(BaseModel):
    """Who publishes a dataset's source data.

    Lives under ``Dataset.publisher``. For OpenSanctions source datasets this
    is the upstream authority (a sanctions body, registry, court); ``official``
    distinguishes a government/primary source from an aggregator.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    url: str | None = None
    acronym: str | None = None
    description: str | None = None
    country: str | None = None
    country_label: str | None = None
    official: bool | None = None


class DataCoverage(BaseModel):
    """The temporal and geographic extent a dataset claims to cover.

    Lives under ``Dataset.coverage``. ``start`` / ``end`` are partial dates
    (``"2020"``, ``"2020-06"``, or a full ISO date) so they stay strings here
    rather than parsed datetimes. ``frequency`` is the update cadence and is
    ``"unknown"`` when the publisher hasn't declared one.
    """

    model_config = ConfigDict(extra="ignore")

    start: str | None = None
    end: str | None = None
    countries: list[str] = Field(default_factory=list)
    frequency: str | None = None
    schedule: str | None = None


class Dataset(BaseModel):
    """One dataset entry in ``DatasetsResponse.datasets``.

    Mirrors a subset of yente's ``YenteDatasetModel`` — the fields most
    callers actually use. Unknown fields are dropped via ``extra="ignore"``.

    ``load`` is ``True`` on the dataset(s) yente has actually indexed (1 to N
    per server). Every other catalog entry rides along as metadata —
    collection definitions, source-level descriptors for the indexed
    collection's children, etc. ``index_current`` / ``index_version`` only
    carry meaningful freshness when ``load`` is true; on metadata-only
    entries they reflect upstream state, not what's queryable here.

    ``children`` is non-empty when the dataset is a *collection* — a grouping
    that aggregates other datasets. The descriptive fields (``summary``,
    ``entity_count``, ``coverage``, ``publisher``, …) are populated for source
    datasets and collections alike, but a given server only fills in what its
    upstream metadata carries — expect most to be ``None`` on bare entries.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    url: str | None = None
    version: str | None = None
    entities_url: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    entity_count: int | None = None
    thing_count: int | None = None
    updated_at: datetime | None = None
    last_export: datetime | None = None
    coverage: DataCoverage | None = None
    publisher: DataPublisher | None = None
    load: bool = False
    index_current: bool | None = None
    index_version: str | None = None
    children: list[str] = Field(default_factory=list)
    deprecated: bool = False
    deprecation: str | None = None


class DatasetsResponse(BaseModel):
    """Body of ``/catalog``: the list of indexed datasets and their freshness.

    Named ``DatasetsResponse`` (not ``CatalogResponse``) because the
    payload is fundamentally a datasets listing; ``catalog`` remains the
    wire endpoint name for the HTTP call.
    """

    model_config = ConfigDict(extra="ignore")

    datasets: list[Dataset]
    current: list[str] = Field(default_factory=list)
    outdated: list[str] = Field(default_factory=list)
    index_stale: bool = False


class ProgramIssuer(BaseModel):
    """The authority behind a sanctions program (government, UN body, …)."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    acronym: str | None = None
    organisation: str | None = None
    territory: str | None = None


class Program(BaseModel):
    """One sanctions program from the OpenSanctions program catalog.

    ``key`` is the value entities carry in their ``programId`` property
    (e.g. ``"EU-UKR"``, ``"US-RUSHAR"``) — fetch the catalog to resolve
    those codes into the program's title, issuer, and policy summary.
    """

    model_config = ConfigDict(extra="ignore")

    key: str
    title: str | None = None
    url: str | None = None
    summary: str | None = None
    dataset: str | None = None
    issuer: ProgramIssuer | None = None
    aliases: list[str] = Field(default_factory=list)
    target_territories: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)


class ProgramsResponse(BaseModel):
    """Body of the program catalog artifact — a plain list under ``data``."""

    model_config = ConfigDict(extra="ignore")

    data: list[Program]


class AdjacentPropertyResponse(BaseModel):
    """Body of ``/entities/{id}/adjacent/{property}`` — paginated entity refs."""

    model_config = ConfigDict(extra="ignore")

    results: list[str | Entity]
    total: TotalSpec
    limit: int
    offset: int


class AdjacentResponse(BaseModel):
    """Body of ``/entities/{id}/adjacent`` — entity plus its adjacency map."""

    model_config = ConfigDict(extra="ignore")

    entity: Entity
    adjacent: dict[str, AdjacentPropertyResponse]


class Statement(BaseModel):
    """One row of the statement-level data lineage stream.

    Statements are the atomic claims that compose into entities — each
    statement records a single ``(entity_id, prop, value)`` triple plus
    provenance metadata (which dataset asserted it, when it was first
    and last seen, language, original pre-cleaning value, and ``origin``
    — how the claim was produced, e.g. ``"inferred"`` or ``"patch"``,
    unset for plain crawled data). See the
    `statement-based data model <https://www.opensanctions.org/docs/statements/>`_
    for background.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    entity_id: str
    canonical_id: str
    prop: str
    prop_type: str
    schema_: str = Field(alias="schema")
    value: str
    original_value: str | None = None
    dataset: str
    lang: str | None = None
    origin: str | None = None
    first_seen: datetime
    last_seen: datetime


class StatementsResponse(BaseModel):
    """Body of ``/statements`` — paginated list of statement rows."""

    model_config = ConfigDict(extra="ignore")

    results: list[Statement]
    total: TotalSpec
    limit: int
    offset: int


# Rebuild for the recursive Entity → Entity reference inside `properties`.
Entity.model_rebuild()
