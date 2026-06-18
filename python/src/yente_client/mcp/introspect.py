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

from yente_client import schemas
from yente_client.schemas import has_schema


def schema_index() -> list[dict[str, Any]]:
    """Return the matchable-schema index — the map an agent reads first.

    One compact summary per matchable schema (the only ones usable as a
    ``match_entity`` target). Property detail is intentionally omitted; fetch it
    per-schema via :func:`describe_schema` so the whole model is never dumped at
    once. Projection (which fields, descriptions kept, cruft dropped) is the
    SDK's :func:`yente_client.schemas.schema_index`, shared with the CLI.
    """
    return schemas.schema_index(matchable_only=True)


def describe_schema(name: str) -> dict[str, Any]:
    """Return the projected detail for one schema (header + usable properties).

    Each property carries its name, type, and whether it routes through the
    matcher's candidate filter (`matchable`), plus `range`/`reverse` for
    entity-typed properties. Surfaces the `matchable` flag without
    editorializing — non-matchable properties (`firstName`, `weakAlias`, …) still
    feed scoring, so the rule for callers is "send every property you have."

    Delegates the projection to :func:`yente_client.schemas.describe_schema`
    (shared with the CLI); only the friendly error is MCP-specific.

    Raises:
        ValueError: if ``name`` is not a schema in the bundled model.
    """
    if not has_schema(name):
        raise ValueError(f"Unknown FtM schema {name!r}. Call describe_schema() for the index.")
    return schemas.describe_schema(name)


def topic_values() -> dict[str, str]:
    """Return the ``topic`` vocabulary (value → label), e.g. for ``topics=`` filters."""
    return schemas.type_values("topic")


def country_values() -> dict[str, str]:
    """Return the ``country`` vocabulary (code → name) for nationality / jurisdiction props."""
    return schemas.type_values("country")


def gender_values() -> dict[str, str]:
    """Return the ``gender`` vocabulary (value → label)."""
    return schemas.type_values("gender")
