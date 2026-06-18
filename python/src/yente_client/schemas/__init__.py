"""Bundled FtM model snapshot and lookup helpers.

The model is loaded from ``model.json`` at import time and re-exported as a
plain dict. Use the helpers for the common membership / inheritance /
deprecation checks; navigate ``model`` directly for anything richer.
"""

from yente_client.schemas._lookup import (
    describe_schema,
    has_schema,
    is_a,
    is_deprecated,
    is_matchable_schema,
    iter_properties,
    matchable_schemata,
    model,
    property_matchable,
    schema_index,
    schema_properties,
    schema_summary,
)

__all__ = [
    "describe_schema",
    "has_schema",
    "is_a",
    "is_deprecated",
    "is_matchable_schema",
    "iter_properties",
    "matchable_schemata",
    "model",
    "property_matchable",
    "schema_index",
    "schema_properties",
    "schema_summary",
]
