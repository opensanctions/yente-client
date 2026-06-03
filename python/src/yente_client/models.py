"""Pydantic response models for the yente / OpenSanctions API.

These mirror the v1 wire format but are reshaped where v2's planned shape
differs (notably ``MatchResponse``, which we flatten from v1's
``responses[<key>]`` envelope). See the design doc §4.3 and §4.8.

``extra="ignore"`` on every response model means unknown server-side fields
are silently dropped — the client doesn't break when yente adds a field, but
users who want it must wait for a SDK update. This is intentional for the MVP.
"""

from __future__ import annotations

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


class MatchResponse(BaseModel):
    """v2-shaped flat response for ``/match``.

    The v1 wire wraps the result in ``responses[<key>]``; the call layer
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
    that aggregates other datasets.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    title: str | None = None
    description: str | None = None
    version: str | None = None
    entities_url: str | None = None
    load: bool = False
    index_current: bool | None = None
    index_version: str | None = None
    children: list[str] = Field(default_factory=list)


class DatasetsResponse(BaseModel):
    """Body of ``/catalog``: the list of indexed datasets and their freshness.

    Named ``DatasetsResponse`` (not ``CatalogResponse``) because the
    payload is fundamentally a datasets listing; "catalog" is the wire
    endpoint name we keep using for the HTTP call.
    """

    model_config = ConfigDict(extra="ignore")

    datasets: list[Dataset]
    current: list[str] = Field(default_factory=list)
    outdated: list[str] = Field(default_factory=list)
    index_stale: bool = False


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
    and last seen, language, original pre-cleaning value). See the
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
