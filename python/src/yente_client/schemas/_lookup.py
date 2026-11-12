"""Runtime access to the bundled FtM model snapshot.

Loads ``schemas/model.json`` at import time and exposes it as a plain dict
(:data:`model`, which the codegen also reads), plus typed, cached views of its
schemata, properties and types (:class:`SchemaSpec`, :class:`PropertySpec`,
:class:`TypeSpec`) that every lookup and projection here is built on.

The on-disk file is the followthemoney release artifact verbatim:
``{schemata, types, version}`` at the top level.
"""

import functools
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yente_client._util import compact

model: dict[str, Any] = json.loads((Path(__file__).parent / "model.json").read_text())


class PropertySpec(BaseModel):
    """One property definition from the bundled model."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str | None = None
    type: str = "string"
    range: str | None = Field(default=None, description="Target schema of an entity property.")
    reverse: str | None = Field(
        default=None, description="The property naming this edge from the far entity."
    )
    matchable: bool | None = Field(
        default=None, description="Unset means: inherit the flag from the property's type."
    )
    hidden: bool = False
    deprecated: bool = False
    description: str | None = None
    examples: list[str] = Field(default_factory=list)


class SchemaSpec(BaseModel):
    """One schema definition from the bundled model, with its own properties only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str | None = None
    plural: str | None = None
    description: str | None = None
    matchable: bool = False
    edge: bool = False
    extends: list[str] = Field(default_factory=list)
    featured: list[str] = Field(default_factory=list)
    schemata: list[str] = Field(
        default_factory=list, description="The schema and all its ancestors."
    )
    properties: dict[str, PropertySpec] = Field(default_factory=dict)

    @field_validator("edge", mode="before")
    @classmethod
    def _is_edge(cls, value: Any) -> bool:
        # The model carries an edge's rendering spec; only its presence matters here.
        return value is not None and value is not False


class TypeSpec(BaseModel):
    """One property type definition from the bundled model."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str | None = None
    plural: str | None = None
    description: str | None = None
    matchable: bool = False
    values: dict[str, str] = Field(default_factory=dict)


def has_schema(name: str) -> bool:
    """Return True if ``name`` is a valid schema in the bundled model."""
    return name in model["schemata"]


@functools.cache
def schema_spec(name: str) -> SchemaSpec:
    """Return the typed definition of one schema.

    Raises:
        KeyError: if ``name`` is not a schema in the bundled model.
    """
    return SchemaSpec.model_validate(model["schemata"][name])


@functools.cache
def type_spec(name: str) -> TypeSpec:
    """Return the typed definition of one property type (``country``, ``date``, …).

    Raises:
        KeyError: if ``name`` is not a type in the bundled model.
    """
    return TypeSpec.model_validate(model["types"][name])


@functools.cache
def property_specs(schema: str) -> Mapping[str, PropertySpec]:
    """Return every property available on ``schema``, own and inherited.

    Where several ancestors define the same name, the first in the model's
    ``schemata`` list wins.

    Raises:
        KeyError: if ``schema`` is not a schema in the bundled model.
    """
    specs: dict[str, PropertySpec] = {}
    for ancestor in schema_spec(schema).schemata:
        for name, prop in schema_spec(ancestor).properties.items():
            specs.setdefault(name, prop)
    return MappingProxyType(specs)


@functools.cache
def all_property_specs() -> Mapping[str, PropertySpec]:
    """Return one definition per property name across every schema.

    For names met without a schema (a statement row's ``prop``). A few names
    differ between schemata — ``authority`` is a string on Sanction but an
    entity on Contract — so prefer :func:`property_specs` when the schema is
    known. Schemata are scanned alphabetically; the first definition wins.
    """
    specs: dict[str, PropertySpec] = {}
    for schema in sorted(model["schemata"]):
        for name, prop in schema_spec(schema).properties.items():
            specs.setdefault(name, prop)
    return MappingProxyType(specs)


def iter_properties(schema: str) -> Iterator[str]:
    """Yield every property name available on ``schema``, including inherited ones."""
    return iter(property_specs(schema))


def is_matchable_schema(schema: str) -> bool:
    """Return True if ``schema`` can be used as a `/match` query target.

    Non-matchable schemata (e.g. ``Document``, ``Article``, abstract
    parents like ``Thing``) cause yente to raise ``TypeError`` at query
    construction; the SDK refuses such queries client-side rather than
    let the server reject them. See ``yente/data/entity.py:42``.
    """
    return schema_spec(schema).matchable


def matchable_schemata() -> list[str]:
    """Return every schema name with ``matchable: true`` in the model.

    Sorted alphabetically for stable error messages.
    """
    return sorted(name for name in model["schemata"] if schema_spec(name).matchable)


def is_a(schema: str, ancestor: str) -> bool:
    """Return True if ``schema`` extends ``ancestor`` transitively.

    Reflexive on ``schema`` itself.
    """
    return ancestor in schema_spec(schema).schemata


def is_deprecated(schema: str, prop: str) -> bool:
    """Return True if ``prop`` is marked ``deprecated`` on ``schema`` or any ancestor."""
    return property_specs(schema)[prop].deprecated


# --- canonical model projection (shared by the MCP and the CLI) ---
#
# The bundled model carries a lot of structural metadata (maxLength, qname,
# plural, the flattened ancestor closure, …) that's noise for a human reading
# `ref` or an agent reading `describe(kind="schema")`. These helpers project schemata
# and properties down to the high-signal fields, keeping the descriptions (the
# part worth reading) and dropping the cruft. One projection so the CLI and the
# MCP can't drift.


def property_matchable(prop: PropertySpec) -> bool:
    """Resolve a property's matchable flag, falling back to its type's default.

    A property may not set ``matchable`` itself; when it doesn't, the value comes
    from its FtM type (e.g. the ``country`` type is matchable, so ``citizenship``
    inherits it). This is the FtM model's own resolution rule.
    """
    if prop.matchable is not None:
        return prop.matchable
    return prop.type in model["types"] and type_spec(prop.type).matchable


def _project_property(name: str, prop: PropertySpec) -> dict[str, Any]:
    return compact(
        {
            "name": name,
            "label": prop.label,
            "type": prop.type,
            # flag only emitted when true (false reads as absent)
            "matchable": property_matchable(prop) or None,
            "description": (prop.description or "").strip(),
            "examples": prop.examples,
        }
    )


def schema_properties(schema: str) -> list[dict[str, Any]]:
    """Project a schema's settable attributes (non-``entity``-typed), sorted by name.

    These are the fields you fill in a ``match_entity`` query (names, dates,
    countries, identifiers, …). Relationship edges (entity-typed properties)
    live in :func:`schema_relations`, not here.
    """
    specs = property_specs(schema)
    return [
        _project_property(name, specs[name])
        for name in sorted(specs)
        if specs[name].type != "entity"
    ]


def schema_relations(schema: str) -> list[dict[str, Any]]:
    """Project a schema's relationship edges (entity-typed properties).

    These are the edges ``fetch_entity_relations`` traverses (``ownershipOwner``,
    ``sanctions``, ``familyPerson``, …): pass the ``name`` as its ``prop``,
    ``range`` is the schema the edge points at, and ``reverse`` names the role
    the source plays on the far entity — which disambiguates same-``range`` edges
    (``familyPerson``/``person`` vs ``familyRelative``/``relative``, both →
    ``Family``). Kept compact — no full property projection.
    """
    specs = property_specs(schema)
    return [
        compact({"name": name, "range": specs[name].range, "reverse": specs[name].reverse})
        for name in sorted(specs)
        if specs[name].type == "entity"
    ]


def schema_summary(schema: str) -> dict[str, Any]:
    """Project a schema header (no properties): name, description, matchable, lineage."""
    spec = schema_spec(schema)
    return compact(
        {
            "name": schema,
            "description": (spec.description or "").strip(),
            # flags only emitted when true (false reads as absent)
            "matchable": spec.matchable or None,
            "edge": spec.edge or None,
            "extends": spec.extends,
            "featured": spec.featured,
        }
    )


def describe_schema(schema: str) -> dict[str, Any]:
    """Full projected schema view: summary + settable ``properties`` + ``relations``.

    ``properties`` are the attributes you set in a match query; ``relations`` are
    the entity-typed edges you traverse with ``fetch_entity_relations`` (omitted
    when the schema has none).
    """
    summary = schema_summary(schema)
    summary["properties"] = schema_properties(schema)
    relations = schema_relations(schema)
    if relations:
        summary["relations"] = relations
    return summary


def schema_index(matchable_only: bool = False) -> list[dict[str, Any]]:
    """Project every schema (or only matchable ones) as summaries, name-sorted."""
    names = matchable_schemata() if matchable_only else sorted(model["schemata"])
    return [schema_summary(name) for name in names]


def type_values(type_name: str) -> dict[str, str]:
    """Return an enum type's ``value → label`` map (``topic``, ``country``, …).

    Empty for non-enum types (``date``, ``name``, …). The single source for the
    controlled vocabularies the CLI and MCP expose.

    Raises:
        KeyError: if ``type_name`` is not a type in the bundled model.
    """
    return dict(type_spec(type_name).values)


def describe_type(type_name: str) -> dict[str, Any]:
    """Project an FtM value type, trimmed like schemata/properties.

    Keeps ``name``, ``label``, ``description``, ``matchable`` (true-only), and
    ``values`` (enum types only); drops structural cruft
    (``maxLength``/``group``/``plural``/``pivot``) and omits empties.

    Raises:
        KeyError: if ``type_name`` is not a type in the bundled model.
    """
    spec = type_spec(type_name)
    return compact(
        {
            "name": type_name,
            "label": spec.label,
            "description": (spec.description or "").strip(),
            "matchable": spec.matchable or None,
            "values": dict(spec.values),
        }
    )
