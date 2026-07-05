"""Trim response entities to the compact views the tools return.

yente entities are large — full nested provenance, every property, referents,
timestamps. Handing that raw to a model is expensive and buries the signal, so
list-style results return a trimmed view (the decision-relevant fields plus the
``id``) and the agent calls ``fetch_entity_by_id`` for full detail only when a
specific entity earns the tokens. "List-cheap, detail-on-demand."

Topic and country codes are resolved to labels inline here (the vocabularies
ship with the bundled FtM model, so it's free). An instruction to "resolve
codes before reporting" is routinely ignored; a label sitting next to the code
is not. Only lookups that need live data (dataset metadata) stay tool calls.

Pure functions over the SDK response models — no FastMCP, unit-testable.
"""

import functools
from collections.abc import Iterable, Mapping
from typing import Any

from yente_client import schemas
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    DatasetsResponse,
    Entity,
    Program,
    ScoredEntity,
    Statement,
    StatementsResponse,
)

# Properties worth showing in a trimmed hit. Names that don't exist on a given
# schema are simply skipped. `topics` is absent because it's promoted to a
# resolved top-level field; see shape_entity.
KEY_PROPERTIES: tuple[str, ...] = (
    "name",
    "alias",
    "birthDate",
    "country",
    "nationality",
    "jurisdiction",
    "registrationNumber",
    "idNumber",
    "position",
    "programId",
    "address",
    "incorporationDate",
)

# How many scoring features to surface per match — enough to explain the score,
# not the whole feature tree.
_EXPLANATION_LIMIT = 3


def shape_entity(entity: Entity, *, properties: tuple[str, ...] = KEY_PROPERTIES) -> dict[str, Any]:
    """Return a trimmed dict for a search/lookup result.

    Keeps identity and dataset context plus a curated slice of string
    properties (nested-entity values are dropped here — follow them via
    ``fetch_entity_relations``). Always includes ``id`` for drill-down.
    Topic codes are promoted to a top-level ``topics`` code → label map, and
    country codes seen in country-typed properties get a ``countries``
    code → name glossary — resolved from the bundled model so the agent never
    has to gloss a code like ``crime.war`` from its looks.
    """
    props = _string_properties(entity, properties)
    shaped: dict[str, Any] = {
        "id": entity.id,
        "caption": entity.caption,
        "schema": entity.schema_,
        "datasets": entity.datasets,
        "properties": props,
    }
    topics = _topic_labels(entity)
    if topics:
        shaped["topics"] = topics
    countries = _country_glossary(entity.schema_, props)
    if countries:
        shaped["countries"] = countries
    return shaped


def shape_full_record(entity: Entity) -> dict[str, Any]:
    """Project a full entity record (the fetch_entity_by_id view) with glossaries.

    The detail tool keeps the whole model dump — no property trimming — but the
    same inline ``topics`` / ``countries`` code → label maps the list views
    carry ride along at the top level, built over *all* of the entity's
    country-typed properties.
    """
    record = entity.model_dump(by_alias=True, mode="json", exclude_none=True)
    topics = _topic_labels(entity)
    if topics:
        record["topics"] = topics
    all_strings = {
        name: strings
        for name, values in entity.properties.items()
        if (strings := [v for v in values if isinstance(v, str)])
    }
    countries = _country_glossary(entity.schema_, all_strings)
    if countries:
        record["countries"] = countries
    return record


def shape_scored(entity: ScoredEntity) -> dict[str, Any]:
    """Return a trimmed dict for a match candidate, with score and explanation.

    Adds ``score`` / ``match`` and a compact summary of the most influential
    contributing features (by absolute score; zero-score features dropped)
    rather than the full ``explanations`` tree.
    """
    shaped = shape_entity(entity)
    shaped["score"] = entity.score
    shaped["match"] = entity.match
    shaped["explanation"] = _top_explanations(entity)
    return shaped


def _string_properties(entity: Entity, names: tuple[str, ...]) -> dict[str, list[str]]:
    """Pick the named properties, keeping only plain string values."""
    out: dict[str, list[str]] = {}
    for name in names:
        values = [v for v in entity.properties.get(name, []) if isinstance(v, str)]
        if values:
            out[name] = values
    return out


def _topic_labels(entity: Entity) -> dict[str, str]:
    """Resolve the entity's topic codes to a ``code → label`` map.

    A code missing from the bundled vocabulary (model drift) falls back to
    itself rather than being dropped — a raw code is still signal.
    """
    vocab = _type_vocabulary("topic")
    return {
        code: vocab.get(code, code)
        for code in entity.properties.get("topics", [])
        if isinstance(code, str)
    }


def _country_glossary(schema: str, props: dict[str, list[str]]) -> dict[str, str]:
    """Collect a ``code → name`` glossary for country codes in the shaped properties.

    Only values of country-typed properties are considered, so a name or
    registration number that happens to look like a code stays out. The codes
    themselves are kept verbatim in ``properties`` (they're the filter inputs);
    this is the companion legend.
    """
    vocab = _type_vocabulary("country")
    country_props = _country_property_names(schema)
    return {
        code: vocab[code]
        for name in props
        if name in country_props
        for code in props[name]
        if code in vocab
    }


@functools.cache
def _type_vocabulary(type_name: str) -> dict[str, str]:
    """Cached ``value → label`` map for an FtM enum type."""
    return schemas.type_values(type_name)


@functools.cache
def _country_property_names(schema: str) -> frozenset[str]:
    """Names of ``schema``'s country-typed properties (empty for unknown schemata)."""
    if not schemas.has_schema(schema):
        return frozenset()
    return frozenset(
        p["name"] for p in schemas.schema_properties(schema) if p.get("type") == "country"
    )


def _top_explanations(entity: ScoredEntity) -> dict[str, float]:
    """Return the most influential contributing features, name → score.

    Drops non-contributing (zero-score) features via the SDK's
    ``contributing_explanations`` and ranks by *absolute* score, so a strong
    negative penalty surfaces alongside strong positive evidence rather than
    sinking to the bottom.
    """
    ranked = sorted(
        entity.contributing_explanations.items(),
        key=lambda kv: abs(kv[1].score),
        reverse=True,
    )
    return {name: feature.score for name, feature in ranked[:_EXPLANATION_LIMIT]}


def _stub(entity: Entity) -> dict[str, Any]:
    """Minimal reference to an entity — enough to identify it and chain off its id."""
    return {"id": entity.id, "caption": entity.caption, "schema": entity.schema_}


def shape_edge(edge: Entity, source_id: str) -> dict[str, Any]:
    """Project one adjacency edge (Sanction, Ownership, Family, …) for the relations view.

    Edge entities carry no useful caption (it's just the schema name) — the
    signal is in their properties. So instead of stubbing, this keeps the edge
    whole with two changes: the back-reference to the source entity is dropped
    (redundant — the caller already has it), and the *counterparty* (the other
    endpoint, e.g. an Ownership's `asset`) is resolved to an `{id, caption,
    schema}` stub. Everything else is kept verbatim — edge attributes,
    `datasets` provenance, and prose (`reason`, `notes`), which is the screening
    signal. The size lever is the caller's `limit`, not field-stripping.
    """
    rec: dict[str, Any] = {"id": edge.id, "schema": edge.schema_}
    if edge.datasets:
        rec["datasets"] = edge.datasets
    props: dict[str, Any] = {}
    for name, values in edge.properties.items():
        nested = [v for v in values if isinstance(v, Entity)]
        if nested:
            props[name] = [_stub(v) for v in nested]
            continue
        strings = [v for v in values if isinstance(v, str)]
        if not strings or strings == [source_id]:  # absent, or the back-ref to source
            continue
        props[name] = strings
    rec["properties"] = props
    return rec


def shape_adjacency_property(block: AdjacentPropertyResponse, source_id: str) -> dict[str, Any]:
    """Shape one edge type's paginated results: counts + projected edges."""
    return {
        "total": block.total.value,
        "limit": block.limit,
        "offset": block.offset,
        "results": [
            shape_edge(e, source_id) if isinstance(e, Entity) else {"id": e} for e in block.results
        ],
    }


# Relation props suppressed from the adjacency projection. `riskSource` is a
# `Thing` entity property with no reverse: the adjacency endpoint reports a count
# for it but resolves no counterparties — a phantom edge. An entity's risk basis
# is read from its own record (topics / properties), not traversed as a relation.
HIDDEN_RELATION_PROPS: frozenset[str] = frozenset({"riskSource"})


def shape_adjacency(resp: AdjacentResponse) -> dict[str, Any]:
    """Shape the all-edge-types overview: a map of edge type → counts + projected edges."""
    source_id = resp.entity.id
    return {
        prop: shape_adjacency_property(block, source_id)
        for prop, block in resp.adjacent.items()
        if prop not in HIDDEN_RELATION_PROPS
    }


def shape_statement(stmt: Statement) -> dict[str, Any]:
    """Project one statement row to its provenance essentials.

    ``entity_id`` is the pre-deduplication source record that asserted the
    claim; the canonical ID is dropped per row (it's the query), and so is
    ``last_seen`` (``first_seen`` dates the claim; recency is noise here).
    ``original_value`` only appears when cleaning changed it, ``origin`` when
    the claim wasn't plain crawled data (e.g. ``"inferred"``, ``"patch"``).
    """
    rec: dict[str, Any] = {
        "prop": stmt.prop,
        "value": stmt.value,
        "dataset": stmt.dataset,
        "entity_id": stmt.entity_id,
        "first_seen": stmt.first_seen.date().isoformat(),
    }
    if stmt.lang:
        rec["lang"] = stmt.lang
    if stmt.original_value and stmt.original_value != stmt.value:
        rec["original_value"] = stmt.original_value
    if stmt.origin:
        rec["origin"] = stmt.origin
    return rec


def shape_statements(resp: StatementsResponse) -> dict[str, Any]:
    """Shape a statements page: counts + projected rows."""
    return {
        "total": resp.total.value,
        "limit": resp.limit,
        "offset": resp.offset,
        "results": [shape_statement(s) for s in resp.results],
    }


def title_glossary(names: Iterable[str], titles: Mapping[str, str]) -> dict[str, str]:
    """Build a sorted ``code → title`` legend for the codes present in ``names``.

    Shared by the dataset and program legends the tools attach to results.
    Codes without a known title are skipped, so a failed (empty) ``titles``
    lookup degrades to an empty legend the caller omits — never an error.
    """
    return {name: titles[name] for name in sorted(set(names)) if name in titles}


def program_index(programs: list[Program]) -> list[dict[str, Any]]:
    """Project the program catalog to a compact index for discovery.

    Full per-program records (summary, issuer, measures) are fetched on demand
    by key, mirroring :func:`dataset_index`.
    """
    return [
        {
            "key": p.key,
            "title": p.title,
            "territory": p.issuer.territory if p.issuer else None,
        }
        for p in programs
    ]


def dataset_index(resp: DatasetsResponse) -> list[dict[str, Any]]:
    """Project the catalog to a compact name → title index for discovery.

    The full per-dataset record (summary, publisher, coverage, counts) is fetched
    on demand by name; this keeps the index cheap enough to scan in one call.
    """
    return [
        {
            "name": d.name,
            "title": d.title,
            "tags": d.tags,
            "entity_count": d.entity_count,
        }
        for d in resp.datasets
    ]
