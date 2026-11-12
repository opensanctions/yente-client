"""Filter ↔ wire-format translation.

Isolated in its own module so the wire-format mapping can change without
rippling through the endpoint methods.
"""

from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import quote

from pydantic import BaseModel

from yente_client.entities import EntityInput
from yente_client.exceptions import ConfigurationError, NotFoundError
from yente_client.filters import MatchFilters, SearchFilters, _CommonFilters
from yente_client.schemas import is_matchable_schema, matchable_schemata

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


def drop_none(**params: Any) -> dict[str, Any]:
    """Keep only the query params the caller set, so server defaults apply."""
    return {key: value for key, value in params.items() if value is not None}


def entity_path(entity_id: str, *parts: str) -> str:
    """Build ``/entities/<id>[/<part>…]`` with every segment percent-encoded."""
    return "/entities/" + "/".join(quote(part, safe="") for part in (entity_id, *parts))


def search_request(
    q: str,
    filters: SearchFilters | None,
    filter_kwargs: dict[str, Any],
    *,
    limit: int | None,
    offset: int,
    sort: list[str] | None,
    fuzzy: bool,
    simple: bool,
    facets: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    """Resolve a search into its ``/search`` path and query params."""
    f = merge_filters(SearchFilters, filters, filter_kwargs)
    dataset, params = serialise_search_filters(f)
    params.update(
        drop_none(
            q=q,
            offset=offset,
            limit=limit,
            sort=sort or None,
            fuzzy="true" if fuzzy else None,
            simple="true" if simple else None,
            facets=facets or None,
        )
    )
    return f"/search/{quote(dataset, safe='')}", params


def match_request(
    filters: MatchFilters | None,
    filter_kwargs: dict[str, Any],
    *,
    threshold: float | None,
    cutoff: float | None,
    algorithm: str | None,
    limit: int | None,
) -> tuple[str, dict[str, Any]]:
    """Resolve filters and scoring params into the ``/match`` path and query params."""
    f = merge_filters(MatchFilters, filters, filter_kwargs)
    dataset, params = serialise_match_filters(f)
    params.update(drop_none(threshold=threshold, cutoff=cutoff, algorithm=algorithm, limit=limit))
    return f"/match/{quote(dataset, safe='')}", params


def match_body(
    entity: EntityInput,
    weights: dict[str, float] | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the single-query POST body for ``/match``."""
    return {
        "queries": {"q": entity.to_payload()},
        "weights": weights or {},
        "config": config or {},
    }


def check_matchable_schema(entity: EntityInput) -> None:
    """Raise :class:`ConfigurationError` if ``entity``'s schema can't be matched."""
    schema_name = entity.schema_
    if is_matchable_schema(schema_name):
        return
    options = ", ".join(matchable_schemata()[:6]) + ", …"
    raise ConfigurationError(
        f"Schema {schema_name!r} is not a matchable target for /match. "
        f"Use a matchable schema like {options} "
        f"(run `yente-cli ref schemas --matchable` for the full list)."
    )


def statements_unavailable(exc: NotFoundError) -> NotFoundError:
    """Restate a ``/statements`` 404: the endpoint, not a record, is missing."""
    return NotFoundError(
        status_code=exc.status_code,
        detail=(
            "The /statements endpoint is not available on this server. "
            "Statement-level access is provided only by the OpenSanctions "
            "API; yente does not ship the backing Postgres instance and "
            "returns 404 here."
        ),
        response=exc.response,
    )


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
