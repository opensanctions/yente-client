"""Bundled FtM model snapshot and lookup helpers.

The model is loaded from ``model.json`` at import time and re-exported as a
plain dict. The typed views (:func:`schema_spec`, :func:`property_specs`,
:func:`type_spec`) are what to navigate for anything beyond the common
membership / inheritance / deprecation checks.
"""

from yente_client.schemas._lookup import (
    PropertySpec,
    SchemaSpec,
    TypeSpec,
    all_property_specs,
    describe_schema,
    describe_type,
    has_schema,
    is_a,
    is_deprecated,
    is_matchable_schema,
    iter_properties,
    matchable_schemata,
    model,
    property_matchable,
    property_specs,
    schema_index,
    schema_properties,
    schema_relations,
    schema_spec,
    schema_summary,
    type_spec,
    type_values,
)

__all__ = [
    "PropertySpec",
    "SchemaSpec",
    "TypeSpec",
    "all_property_specs",
    "describe_schema",
    "describe_type",
    "has_schema",
    "is_a",
    "is_deprecated",
    "is_matchable_schema",
    "iter_properties",
    "matchable_schemata",
    "model",
    "property_matchable",
    "property_specs",
    "schema_index",
    "schema_properties",
    "schema_relations",
    "schema_spec",
    "schema_summary",
    "type_spec",
    "type_values",
]
