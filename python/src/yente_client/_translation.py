"""Filter ↔ wire-format translation.

Isolated in its own module so the wire-format mapping can change without
rippling through the endpoint methods.
"""

from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from yente_client.filters import MatchFilters, SearchFilters, _CommonFilters

FT = TypeVar("FT", bound=BaseModel)


def merge_filters(cls: type[FT], filters: FT | None, kwargs: dict[str, Any]) -> FT:
    """Merge endpoint ``**kwargs`` into an optional ``filters=`` object.

    Kwargs win on any field they explicitly specify. ``None`` in a kwarg means
    "not supplied" rather than "clear this field" — endpoint methods declare
    most filter kwargs as ``... = None`` defaults, so a caller passing only
    ``filters=`` doesn't have its values clobbered by missing kwargs.

    Alias resolution piggybacks on ``cls.model_validate`` — kwargs go through
    the model's own validator, which already handles ``schema=`` ↔ ``schema_=``
    and ``filter=`` ↔ ``filter_=`` via ``populate_by_name=True``. Unknown
    kwargs raise ``ValidationError`` via the model's ``extra="forbid"``.
    """
    explicit = {k: v for k, v in kwargs.items() if v is not None}
    overrides = cls.model_validate(explicit)
    if filters is None:
        return overrides
    base = filters.model_dump()
    base.update(overrides.model_dump(exclude_unset=True))
    return cls(**base)


def datasets_for_wire(datasets: list[str] | None) -> tuple[str, list[str]]:
    """Split a datasets filter into (path-param, include_dataset-extras).

    The v1 wire takes one dataset in the URL path and extras via repeated
    ``include_dataset`` query params. Defaults to ``"default"`` when the
    caller didn't specify any — matching the server-side default.
    """
    materialised = datasets or ["default"]
    return materialised[0], materialised[1:]


def _serialise_common(f: _CommonFilters) -> dict[str, Any]:
    """Translate the fields _CommonFilters carries into v1 wire query params.

    Renames: ``exclude_datasets`` → ``exclude_dataset``,
    ``exclude_schemata`` → ``exclude_schema``. ``changed_since`` is stringified
    if a ``datetime`` was passed.
    """
    params: dict[str, Any] = {}
    if f.exclude_datasets:
        params["exclude_dataset"] = f.exclude_datasets
    if f.exclude_schemata:
        params["exclude_schema"] = f.exclude_schemata
    if f.topics:
        params["topics"] = f.topics
    if f.changed_since:
        cs = f.changed_since
        params["changed_since"] = cs.isoformat() if isinstance(cs, datetime) else cs
    return params


def serialise_search_filters(f: SearchFilters) -> tuple[str, dict[str, Any]]:
    """Translate a ``SearchFilters`` into ``(dataset_for_path, query_params)``."""
    dataset, include = datasets_for_wire(f.datasets)
    params = _serialise_common(f)
    if include:
        params["include_dataset"] = include
    if f.countries:
        params["countries"] = f.countries
    if f.schema_:
        params["schema"] = f.schema_
    if f.filter_:
        params["filter"] = f.filter_
    return dataset, params


def serialise_match_filters(f: MatchFilters) -> tuple[str, dict[str, Any]]:
    """Translate a ``MatchFilters`` into ``(dataset_for_path, query_params)``.

    Renames specific to ``/match``: ``exclude_entities`` → ``exclude_entity_ids``.
    """
    dataset, include = datasets_for_wire(f.datasets)
    params = _serialise_common(f)
    if include:
        params["include_dataset"] = include
    if f.exclude_entities:
        params["exclude_entity_ids"] = f.exclude_entities
    return dataset, params


def unwrap_match_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate the ``{responses: {q: {...}}, limit}`` envelope into the flat
    ``{query, results, total, limit}`` shape that ``MatchResponse`` takes."""
    inner = raw["responses"]["q"]
    return {
        "query": inner["query"],
        "results": inner["results"],
        "total": inner["total"],
        "limit": raw["limit"],
    }
