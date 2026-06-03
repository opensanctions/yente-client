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
