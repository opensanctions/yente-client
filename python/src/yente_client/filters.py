"""Filter types for ``/match`` and ``/search``.

Two public types share a small private base so each endpoint method
accepts only the filter shape that makes sense for it. ``extra="forbid"``
turns cross-endpoint typos (passing ``countries=`` to ``match()``, etc.)
into immediate ``pydantic.ValidationError``.

Aliases for ``schema`` and ``filter`` exist because those names are
reserved (Python builtin / Pydantic-sensitive); ``populate_by_name=True``
means callers can write either ``schema=...`` (the alias) or
``schema_=...`` (the Python field name) and both work.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from yente_client.schemas._literals import Schema, Topic


class _CommonFilters(BaseModel):
    """Filter fields shared by ``MatchFilters`` and ``SearchFilters``.

    Not intended for direct use; instantiate one of the concrete classes.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    datasets: list[str] | None = None
    exclude_datasets: list[str] | None = None
    exclude_schemata: list[Schema] | None = None
    topics: list[Topic] | None = None
    changed_since: str | datetime | None = None


class MatchFilters(_CommonFilters):
    """Filters accepted by ``client.match()``."""

    exclude_entities: list[str] | None = None


class SearchFilters(_CommonFilters):
    """Filters accepted by ``client.search()``."""

    countries: list[str] | None = None
    schema_: Schema | None = Field(default=None, alias="schema")
    filter_: list[str] | None = Field(default=None, alias="filter")
