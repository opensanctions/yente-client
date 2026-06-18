"""Runtime access to the bundled FtM model snapshot.

Loads ``schemas/model.json`` at import time and exposes it directly plus a
handful of lookup helpers. No Pydantic-typed wrappers — the codegen reads
``model.json`` directly and callers who want introspection get dict access.

The on-disk file is the followthemoney release artifact verbatim:
``{schemata, types, version}`` at the top level.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

model: dict[str, Any] = json.loads((Path(__file__).parent / "model.json").read_text())


def has_schema(name: str) -> bool:
    """Return True if ``name`` is a valid schema in the bundled model."""
    return name in model["schemata"]


def iter_properties(schema: str) -> Iterator[str]:
    """Yield every property name available on ``schema``, including inherited ones.

    ``model.json`` stores own-properties only on each schema definition; this
    walks the pre-flattened ``schemata`` ancestor list and yields each property
    name at most once, even if multiple ancestors define a property of the same
    name.
    """
    if not has_schema(schema):
        raise KeyError(schema)
    seen: set[str] = set()
    for ancestor in model["schemata"][schema]["schemata"]:
        ancestor_props = model["schemata"].get(ancestor, {}).get("properties", {})
        for prop in ancestor_props:
            if prop not in seen:
                seen.add(prop)
                yield prop


def is_matchable_schema(schema: str) -> bool:
    """Return True if ``schema`` can be used as a `/match` query target.

    Non-matchable schemata (e.g. ``Document``, ``Article``, abstract
    parents like ``Thing``) cause yente to raise ``TypeError`` at query
    construction; the SDK refuses such queries client-side rather than
    let the server reject them. See ``yente/data/entity.py:42``.
    """
    if not has_schema(schema):
        raise KeyError(schema)
    return bool(model["schemata"][schema].get("matchable", False))


def matchable_schemata() -> list[str]:
    """Return every schema name with ``matchable: true`` in the model.

    Sorted alphabetically for stable error messages.
    """
    return sorted(n for n, d in model["schemata"].items() if d.get("matchable"))


def is_a(schema: str, ancestor: str) -> bool:
    """Return True if ``schema`` extends ``ancestor`` transitively.

    Reflexive on ``schema`` itself. O(1) lookup against the pre-flattened
    ``schemata`` list — no MRO walk needed.
    """
    if not has_schema(schema):
        raise KeyError(schema)
    return ancestor in model["schemata"][schema]["schemata"]


def is_deprecated(schema: str, prop: str) -> bool:
    """Return True if ``prop`` is marked ``deprecated`` on ``schema`` or any ancestor."""
    if not has_schema(schema):
        raise KeyError(schema)
    for ancestor in model["schemata"][schema]["schemata"]:
        props = model["schemata"].get(ancestor, {}).get("properties", {})
        if prop in props:
            return bool(props[prop].get("deprecated", False))
    raise KeyError(prop)


# --- canonical model projection (shared by the MCP and the CLI) ---
#
# The bundled model carries a lot of structural metadata (maxLength, qname,
# plural, the flattened ancestor closure, …) that's noise for a human reading
# `ref` or an agent reading `describe_schema`. These helpers project schemata
# and properties down to the high-signal fields, keeping the descriptions (the
# part worth reading) and dropping the cruft. One projection so the CLI and the
# MCP can't drift.


def _is_empty(value: Any) -> bool:
    """True for the empty values we omit from a projection: None, "", [], {}."""
    return value is None or value == "" or value == [] or value == {}


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is empty (None / "" / [] / {}); bools are kept."""
    return {k: v for k, v in record.items() if not _is_empty(v)}


def property_matchable(prop_def: dict[str, Any]) -> bool:
    """Resolve a property's matchable flag, falling back to its type's default.

    A property may not set ``matchable`` itself; when it doesn't, the value comes
    from its FtM type (e.g. the ``country`` type is matchable, so ``citizenship``
    inherits it). This is the FtM model's own resolution rule.
    """
    matchable = prop_def.get("matchable")
    if matchable is None:
        matchable = model["types"].get(prop_def.get("type", ""), {}).get("matchable")
    return bool(matchable)


def _project_property(name: str, prop_def: dict[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "name": name,
            "label": prop_def.get("label"),
            "type": prop_def.get("type"),
            "matchable": property_matchable(prop_def),
            "description": (prop_def.get("description") or "").strip(),
            "range": prop_def.get("range"),
            "reverse": prop_def.get("reverse"),
            "examples": prop_def.get("examples"),
        }
    )


def schema_properties(schema: str) -> list[dict[str, Any]]:
    """Project a schema's usable properties (own + inherited), sorted by name.

    Excludes ``stub`` (reverse-edge) properties — they're navigation-only, not
    something a caller sends, and the exclusion matches the entity codegen.
    """
    if not has_schema(schema):
        raise KeyError(schema)
    chosen: dict[str, dict[str, Any]] = {}
    for ancestor in model["schemata"][schema]["schemata"]:
        for pname, pdef in model["schemata"].get(ancestor, {}).get("properties", {}).items():
            if pname in chosen or pdef.get("stub"):
                continue
            chosen[pname] = pdef
    return [_project_property(name, chosen[name]) for name in sorted(chosen)]


def schema_summary(schema: str) -> dict[str, Any]:
    """Project a schema header (no properties): name, description, matchable, lineage."""
    if not has_schema(schema):
        raise KeyError(schema)
    defn = model["schemata"][schema]
    return _compact(
        {
            "name": schema,
            "description": (defn.get("description") or "").strip(),
            "matchable": bool(defn.get("matchable", False)),
            "edge": defn.get("edge"),  # only present (and only kept) for edge schemata
            "extends": list(defn.get("extends") or []),
            "featured": list(defn.get("featured") or []),
        }
    )


def describe_schema(schema: str) -> dict[str, Any]:
    """Full projected schema view: the summary plus its projected properties."""
    summary = schema_summary(schema)
    summary["properties"] = schema_properties(schema)
    return summary


def schema_index(matchable_only: bool = False) -> list[dict[str, Any]]:
    """Project every schema (or only matchable ones) as summaries, name-sorted."""
    names = matchable_schemata() if matchable_only else sorted(model["schemata"])
    return [schema_summary(name) for name in names]
