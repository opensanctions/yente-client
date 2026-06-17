"""Trim response entities to the compact views the tools return.

yente entities are large — full nested provenance, every property, referents,
timestamps. Handing that raw to a model is expensive and buries the signal, so
list-style results return a trimmed view (the decision-relevant fields plus the
``id``) and the agent calls ``fetch_entity_by_id`` for full detail only when a
specific entity earns the tokens. "List-cheap, detail-on-demand."

Pure functions over the SDK response models — no FastMCP, unit-testable.
"""

from typing import Any

from yente_client.models import Entity, ScoredEntity

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

    Adds ``score`` / ``match`` and a compact summary of the top contributing
    features (by score) rather than the full ``explanations`` tree.
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
    """Return the highest-scoring features, feature name → score."""
    ranked = sorted(entity.explanations.items(), key=lambda kv: kv[1].score, reverse=True)
    return {name: feature.score for name, feature in ranked[:_EXPLANATION_LIMIT]}
