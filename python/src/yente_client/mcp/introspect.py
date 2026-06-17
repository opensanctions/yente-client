"""FtM model lookups for the schema tool and ``ftm://`` resources.

The data model is the type system behind every query: it tells an agent which
entity types exist and what properties (with which names and types) each
carries. Getting this in front of the model is what stops it from guessing
``dob`` instead of ``birthDate`` or picking a non-matchable schema.

Everything here resolves from the bundled snapshot (:data:`yente_client.schemas.model`)
— no network, no yente call, and version-pinned to the same model the SDK's
entity classes were generated from. Pure dict-returning functions so they're
unit-testable without FastMCP installed.

Per the repo drift rule, nothing is hardcoded: schema/topic/country sets are
derived from the model at call time.
"""

from typing import Any

from yente_client.schemas import (
    has_schema,
    is_matchable_schema,
    matchable_schemata,
    model,
)


def schema_index() -> list[dict[str, Any]]:
    """Return the matchable-schema index — the map an agent reads first.

    One compact entry per matchable schema (the only ones usable as a
    ``match_entity`` target): name, label, plural, parent schemata, and
    description. Property detail is intentionally omitted; fetch it per-schema
    via :func:`describe_schema` so the whole model is never dumped at once.
    """
    out: list[dict[str, Any]] = []
    for name in matchable_schemata():
        defn = model["schemata"][name]
        out.append(
            {
                "name": name,
                "label": defn.get("label"),
                "plural": defn.get("plural"),
                "parents": defn.get("extends", []),
                "description": defn.get("description"),
            }
        )
    return out


def describe_schema(name: str) -> dict[str, Any]:
    """Return full property detail for one schema.

    The workhorse behind the ``describe_schema`` tool: for every property an
    agent could send (own + inherited), what it's called, its value type, and
    whether it routes through the matcher's candidate filter. Entity-typed
    properties also carry ``range`` (what they point to) and ``reverse`` (the
    property name to use with ``fetch_entity_relations``).

    Surfaces the per-property ``matchable`` flag but does not editorialize on
    it: non-matchable properties (``firstName``, ``weakAlias``, …) still feed
    scoring, so the rule for callers is "send every property you have."

    Raises:
        ValueError: if ``name`` is not a schema in the bundled model.
    """
    if not has_schema(name):
        raise ValueError(f"Unknown FtM schema {name!r}. Call describe_schema() for the index.")

    defn = model["schemata"][name]
    properties = [_property_view(pname, pdef) for pname, pdef in _flatten_properties(name).items()]
    properties.sort(key=lambda p: p["name"])
    return {
        "name": name,
        "label": defn.get("label"),
        "plural": defn.get("plural"),
        "matchable": is_matchable_schema(name),
        "parents": defn.get("extends", []),
        "description": defn.get("description"),
        "properties": properties,
    }


def topic_values() -> dict[str, str]:
    """Return the ``topic`` vocabulary (value → label), e.g. for ``topics=`` filters."""
    return _type_values("topic")


def country_values() -> dict[str, str]:
    """Return the ``country`` vocabulary (code → name) for nationality / jurisdiction props."""
    return _type_values("country")


def gender_values() -> dict[str, str]:
    """Return the ``gender`` vocabulary (value → label)."""
    return _type_values("gender")


def _flatten_properties(schema: str) -> dict[str, dict[str, Any]]:
    """Collect ``{property_name: definition}`` for a schema, own + inherited.

    ``model.json`` stores own-properties per schema; this walks the
    pre-flattened ancestor list (first definition wins) to mirror
    :func:`yente_client.schemas.iter_properties` while keeping the defs.
    """
    seen: dict[str, dict[str, Any]] = {}
    for ancestor in model["schemata"][schema]["schemata"]:
        ancestor_props = model["schemata"].get(ancestor, {}).get("properties", {})
        for pname, pdef in ancestor_props.items():
            if pname not in seen:
                seen[pname] = pdef
    return seen


def _property_view(name: str, defn: dict[str, Any]) -> dict[str, Any]:
    """Project one property definition into the compact view the tool returns."""
    view: dict[str, Any] = {
        "name": name,
        "label": defn.get("label"),
        "type": defn.get("type"),
        "matchable": bool(defn.get("matchable", False)),
    }
    if defn.get("description"):
        view["description"] = defn["description"]
    if defn.get("type") == "entity":
        view["range"] = defn.get("range")
        view["reverse"] = defn.get("reverse")
    return view


def _type_values(type_name: str) -> dict[str, str]:
    return dict(model["types"][type_name].get("values", {}))
