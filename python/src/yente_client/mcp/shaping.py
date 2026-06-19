"""Trim response entities to the compact views the tools return.

yente entities are large — full nested provenance, every property, referents,
timestamps. Handing that raw to a model is expensive and buries the signal, so
list-style results return a trimmed view (the decision-relevant fields plus the
``id``) and the agent calls ``fetch_entity_by_id`` for full detail only when a
specific entity earns the tokens. "List-cheap, detail-on-demand."

Pure functions over the SDK response models — no FastMCP, unit-testable.
"""

from typing import Any

from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    DatasetsResponse,
    Entity,
    ScoredEntity,
)

# Properties worth showing in a trimmed hit. Names that don't exist on a given
# schema are simply skipped. `name` and `topics` are almost always wanted.
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
    "topics",
    "address",
    "incorporationDate",
)

# How many scoring features to surface per match — enough to explain the score,
# not the whole feature tree.
_EXPLANATION_LIMIT = 3


def shape_entity(entity: Entity, *, properties: tuple[str, ...] = KEY_PROPERTIES) -> dict[str, Any]:
    """Return a trimmed dict for a search/lookup result.

    Keeps identity and dataset/topic context plus a curated slice of string
    properties (nested-entity values are dropped here — follow them via
    ``fetch_entity_relations``). Always includes ``id`` for drill-down.
    """
    return {
        "id": entity.id,
        "caption": entity.caption,
        "schema": entity.schema_,
        "datasets": entity.datasets,
        "properties": _string_properties(entity, properties),
    }


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
