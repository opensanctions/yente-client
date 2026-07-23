"""Synchronous yente / OpenSanctions client."""

from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Final, Literal, Self, overload
from urllib.parse import quote

import httpx

from yente_client._http import prepare_http_kwargs, raise_for_response
from yente_client._translation import (
    merge_filters,
    serialise_match_filters,
    serialise_search_filters,
    unwrap_match_response,
)
from yente_client.entities import EntityInput
from yente_client.env import DEFAULT_BASE_URL
from yente_client.exceptions import ConfigurationError, NotFoundError, TransportError
from yente_client.filters import MatchFilters, SearchFilters
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    MatchError,
    MatchResponse,
    ProgramsResponse,
    SearchResponse,
    StatementsResponse,
    StatusResponse,
)
from yente_client.schemas import is_matchable_schema, matchable_schemata

BEST_ALGORITHM: Final[str] = "best"
"""Canonical algorithm name resolving to whichever matching algorithm the
server currently recommends. Stable across algorithm version bumps — pass
``algorithm=BEST_ALGORITHM`` for forward-compatibility."""

PROGRAMS_URL: Final[str] = "https://data.opensanctions.org/meta/programs.json"
"""Public artifact carrying the sanctions-program catalog. Not part of the
yente API: it lives on the OpenSanctions data bucket and describes the
published data regardless of which server ``base_url`` points at."""


def _check_matchable_schema(entity: EntityInput) -> None:
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


def _prepare_match_query(
    filters: MatchFilters | None,
    filter_kwargs: dict[str, Any],
    *,
    threshold: float | None,
    cutoff: float | None,
    algorithm: str | None,
    limit: int | None,
) -> tuple[str, dict[str, Any]]:
    """Resolve filters and scoring params into the /match dataset path and query params."""
    f = merge_filters(MatchFilters, filters, filter_kwargs)
    dataset, params = serialise_match_filters(f)
    if threshold is not None:
        params["threshold"] = threshold
    if cutoff is not None:
        params["cutoff"] = cutoff
    if algorithm is not None:
        params["algorithm"] = algorithm
    if limit is not None:
        params["limit"] = limit
    return dataset, params


def _match_body(
    entity: EntityInput,
    weights: dict[str, float] | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the single-query POST body for /match."""
    return {
        "queries": {"q": entity.to_payload()},
        "weights": weights or {},
        "config": config or {},
    }


class Client:
    """Synchronous client for the yente / OpenSanctions API.

    Use as a context manager for deterministic cleanup of the underlying
    ``httpx.Client``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        app_name: str | None = None,
        user_agent: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        verify: bool | str = True,
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        kwargs = prepare_http_kwargs(
            api_key=api_key,
            base_url=base_url,
            app_name=app_name,
            user_agent=user_agent,
            timeout=timeout,
            verify=verify,
            proxy=proxy,
            headers=headers,
        )
        # Caller-supplied transport (typically `httpx.MockTransport` for tests)
        # bypasses the default. Otherwise stack httpx's connection-level retries
        # so DNS / connection-refused failures get a free retry.
        kwargs["transport"] = transport or httpx.HTTPTransport(retries=2)
        self._http = httpx.Client(**kwargs)
        self._base_url = base_url
        # Per-URL (etag, body) pairs for _request_revalidated. Raw JSON, not
        # validated models — callers re-validate, so a served 304 can't leak
        # mutable state between calls.
        self._etag_cache: dict[str, tuple[str, Any]] = {}

    @property
    def user_agent(self) -> str:
        """Return the User-Agent header this client sends on every request."""
        return self._http.headers["User-Agent"]

    def close(self) -> None:
        """Close the underlying ``httpx.Client``."""
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one HTTP request; map errors and decode JSON.

        Caller passes ``params=`` / ``json=`` / etc. as for ``httpx.Client.request``.
        Returns parsed JSON on 2xx; raises an ``APIError`` subclass on non-2xx
        and ``TransportError`` on connection-level failure.
        """
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise TransportError(str(exc)) from exc

        if not response.is_success:
            raise_for_response(response)

        return response.json()

    def _request_revalidated(self, url: str) -> Any:
        """GET with ETag revalidation — serve the held body on ``304 Not Modified``.

        Keeps one ``(etag, body)`` pair per URL on this client instance and sends
        ``If-None-Match`` once it has one. A server that emits no ``ETag`` gets
        exactly :meth:`_request` behavior, so this is safe to route any GET
        endpoint through — it lights up when the server grows validator support.
        """
        headers: dict[str, str] = {}
        cached = self._etag_cache.get(url)
        if cached is not None:
            headers["If-None-Match"] = cached[0]
        try:
            response = self._http.request("GET", url, headers=headers)
        except httpx.TransportError as exc:
            raise TransportError(str(exc)) from exc

        if response.status_code == 304 and cached is not None:
            return cached[1]
        if not response.is_success:
            raise_for_response(response)

        body = response.json()
        etag = response.headers.get("ETag")
        if etag:
            self._etag_cache[url] = (etag, body)
        return body

    # ----- system / health endpoints -----

    def healthz(self) -> StatusResponse:
        """Probe server liveness.

        Returns ``{"status": "ok"}`` whenever the server process is up. Useful
        for Kubernetes liveness probes. See :meth:`readyz` for index readiness,
        which can fail independently.
        """
        return StatusResponse.model_validate(self._request("GET", "/healthz"))

    def readyz(self) -> StatusResponse:
        """Probe whether the search index is ready to serve queries.

        Returns the same shape as :meth:`healthz`, but the server returns 503
        (mapped to :class:`ServerError`) until the index is loaded.
        """
        return StatusResponse.model_validate(self._request("GET", "/readyz"))

    # ----- datasets / introspection -----

    def datasets(self) -> DatasetsResponse:
        """Fetch the indexed datasets and their freshness state.

        Calls the wire endpoint ``GET /catalog``; the SDK surface uses
        the ``datasets`` name because that's what the response carries.
        Revalidates via ETag when the server provides one (yente doesn't yet;
        see opensanctions/yente#1202).
        """
        return DatasetsResponse.model_validate(self._request_revalidated("/catalog"))

    def algorithms(self) -> AlgorithmsResponse:
        """Fetch the list of enabled matching algorithms and the server's defaults."""
        return AlgorithmsResponse.model_validate(self._request("GET", "/algorithms"))

    def programs(self) -> ProgramsResponse:
        """Fetch the sanctions-program catalog — resolves ``programId`` codes.

        Sanctioned entities carry a ``programId`` property naming the program
        under which they were designated (e.g. ``"EU-UKR"``); this catalog maps
        those codes to the program's title, issuer, policy summary, and
        measures. Fetched from :data:`PROGRAMS_URL` on the public OpenSanctions
        data bucket — an absolute URL, so it works regardless of ``base_url``
        (including against a self-hosted yente). Long-lived clients revalidate
        via ETag: repeat calls cost a bodiless 304 round-trip, not the full
        artifact.
        """
        return ProgramsResponse.model_validate(self._request_revalidated(PROGRAMS_URL))

    # ----- statements (OpenSanctions API only) -----

    def statements(
        self,
        *,
        dataset: str | None = None,
        entity_id: str | None = None,
        canonical_id: str | None = None,
        prop: str | None = None,
        value: str | None = None,
        schema: str | None = None,
        sort: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> StatementsResponse:
        """Read raw entity data as statements — for lineage and diagnostics.

        Use this for tracking where a value came from, when it was first
        observed, and which dataset asserted it. See the
        `statement-based data model <https://www.opensanctions.org/docs/statements/>`_
        for context.

        **Typically you want ``canonical_id=``**, passing the ID returned
        by :meth:`match` / :meth:`search` / :meth:`fetch`. It returns every
        source fragment that was deduplicated into the canonical entity.
        ``entity_id=`` returns only one source's pre-deduplication fragment —
        useful for source-level audits, but the same person across five
        sanctions lists has five distinct ``entity_id`` values and only
        one ``canonical_id``.

        Only the OpenSanctions API serves this endpoint — it is backed
        by a Postgres instance that yente doesn't ship. A yente instance
        returns ``404``, which surfaces here as :class:`NotFoundError`
        with a pointed message.

        Args:
            dataset: Restrict to statements from this dataset.
            entity_id: Filter by the source entity ID
                (e.g. ``"ofac-1234"``). Pre-deduplication; usually
                not what you want — prefer ``canonical_id``.
            canonical_id: Filter by the post-deduplication entity ID
                (e.g. ``"NK-1234"``). The typical choice.
            prop: Filter by property name (e.g. ``"alias"``).
            value: Filter by exact property value.
            schema: Filter by entity schema (e.g. ``"LegalEntity"``).
            sort: Sort keys; defaults to ``["canonical_id", "prop"]``.
            limit: Page size; server-capped (typically at 5000).
            offset: Pagination offset.
        """
        params: dict[str, Any] = {"offset": offset}
        if dataset is not None:
            params["dataset"] = dataset
        if entity_id is not None:
            params["entity_id"] = entity_id
        if canonical_id is not None:
            params["canonical_id"] = canonical_id
        if prop is not None:
            params["prop"] = prop
        if value is not None:
            params["value"] = value
        if schema is not None:
            params["schema"] = schema
        if sort:
            params["sort"] = sort
        if limit is not None:
            params["limit"] = limit
        try:
            raw = self._request("GET", "/statements", params=params)
        except NotFoundError as exc:
            raise NotFoundError(
                status_code=exc.status_code,
                detail=(
                    "The /statements endpoint is not available on this server. "
                    "Statement-level access is provided only by the OpenSanctions "
                    "API; yente does not ship the backing Postgres instance and "
                    "returns 404 here."
                ),
                response=exc.response,
            ) from exc
        return StatementsResponse.model_validate(raw)

    # ----- entity fetch -----

    def fetch(self, entity_id: str, *, nested: bool = True) -> Entity:
        """Fetch a single entity by ID.

        Follows ``308`` redirects transparently when ``entity_id`` is a
        referent of a canonical entity. Pass ``nested=False`` for a lighter
        response that omits adjacent entities like sanctions and ownership
        links.
        """
        params = {"nested": "true" if nested else "false"}
        path = f"/entities/{quote(entity_id, safe='')}"
        return Entity.model_validate(self._request("GET", path, params=params))

    @overload
    def adjacent(
        self,
        entity_id: str,
        *,
        prop: None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentResponse: ...

    @overload
    def adjacent(
        self,
        entity_id: str,
        *,
        prop: str,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentPropertyResponse: ...

    def adjacent(
        self,
        entity_id: str,
        *,
        prop: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentResponse | AdjacentPropertyResponse:
        """Fetch the adjacency map for an entity, optionally restricted to one property.

        Without ``prop``: returns the full adjacency map keyed by property
        name. With ``prop``: returns paginated results for that one property.
        """
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        if sort:
            params["sort"] = sort

        eid = quote(entity_id, safe="")
        if prop is None:
            raw = self._request("GET", f"/entities/{eid}/adjacent", params=params)
            return AdjacentResponse.model_validate(raw)
        path = f"/entities/{eid}/adjacent/{quote(prop, safe='')}"
        return AdjacentPropertyResponse.model_validate(self._request("GET", path, params=params))

    # ----- search -----

    def search(
        self,
        q: str,
        *,
        filters: SearchFilters | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
        fuzzy: bool = False,
        simple: bool = False,
        facets: list[str] | None = None,
        **filter_kwargs: Any,
    ) -> SearchResponse:
        """Run a full-text search across one or more datasets.

        Pass filter fields either via ``filters=SearchFilters(...)`` or as
        kwargs (``datasets=[...]``, ``schema=``, ``countries=[...]``, …);
        kwargs win on any field they specify. The ``datasets`` filter is
        translated to the v1 wire as ``/search/<first-dataset>`` with the
        rest passed as repeated ``include_dataset`` query params.
        """
        f = merge_filters(SearchFilters, filters, filter_kwargs)
        dataset, params = serialise_search_filters(f)

        params["q"] = q
        params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        if sort:
            params["sort"] = sort
        if fuzzy:
            params["fuzzy"] = "true"
        if simple:
            params["simple"] = "true"
        if facets:
            params["facets"] = facets

        return SearchResponse.model_validate(
            self._request("GET", f"/search/{quote(dataset, safe='')}", params=params)
        )

    # ----- match -----

    def match(
        self,
        entity: EntityInput,
        *,
        filters: MatchFilters | None = None,
        threshold: float | None = None,
        cutoff: float | None = None,
        algorithm: str | None = None,
        weights: dict[str, float] | None = None,
        config: dict[str, Any] | None = None,
        limit: int | None = None,
        **filter_kwargs: Any,
    ) -> MatchResponse:
        """Match an entity against a dataset by example.

        Builds a single-query payload and unwraps the response into a flat
        :class:`MatchResponse`.

        Pass filter fields either via ``filters=MatchFilters(...)`` or as
        kwargs (``datasets=[...]``, ``topics=[...]``, ``exclude_entities=[...]``);
        kwargs win on any field they specify.

        Args:
            threshold: Score at or above which a result's ``match`` flag is
                set (server default 0.70). Does not remove results.
            cutoff: Score below which candidates are dropped from the
                response entirely (server default 0.50).
            algorithm: Server-side algorithm name. Common values: ``"best"``
                (use ``BEST_ALGORITHM``), ``"logic-v2"``, ``"name-matcher"``.
                The full set is dynamic via :meth:`algorithms`.

        Raises:
            ConfigurationError: ``entity``'s schema is not a matchable
                target (e.g. ``Document``, ``Article``). Yente would reject
                the query with a 4xx; the client refuses it before the
                round-trip to give a clearer error.
        """
        _check_matchable_schema(entity)
        dataset, params = _prepare_match_query(
            filters,
            filter_kwargs,
            threshold=threshold,
            cutoff=cutoff,
            algorithm=algorithm,
            limit=limit,
        )
        body = _match_body(entity, weights, config)

        raw = self._request("POST", f"/match/{quote(dataset, safe='')}", params=params, json=body)

        return MatchResponse.model_validate(unwrap_match_response(raw))

    def match_iter(
        self,
        entities: Iterable[tuple[str, EntityInput]],
        *,
        workers: int = 4,
        filters: MatchFilters | None = None,
        threshold: float | None = None,
        cutoff: float | None = None,
        algorithm: str | None = None,
        weights: dict[str, float] | None = None,
        config: dict[str, Any] | None = None,
        limit: int | None = None,
        on_error: Literal["raise", "collect"] = "raise",
        **filter_kwargs: Any,
    ) -> Iterator[tuple[str, MatchResponse | MatchError]]:
        """Stream /match calls over many entities with bounded concurrency.

        The batch-screening primitive: feed any iterable of ``(key, entity)``
        pairs — a lazy generator over a file works — and results stream back
        as ``(key, response)`` pairs with at most ``workers`` requests in
        flight. New work is only pulled from the iterable as results are
        consumed, so memory stays flat regardless of input size. One entity
        per request by design; there is no wire-level batching.

        Args:
            entities: ``(key, entity)`` pairs. Results arrive in completion
                order, not input order — the key ties a result back to its
                input row.
            workers: Maximum concurrent requests.
            on_error: ``"raise"`` (default) aborts the stream on the first
                failed item. ``"collect"`` yields ``(key, MatchError)`` for
                the failed item and keeps going — use for long runs that
                must survive individual failures.

        Yields:
            ``(key, MatchResponse)`` per input pair; with
            ``on_error="collect"``, failed pairs surface as
            ``(key, MatchError)`` instead.
        """
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        dataset, params = _prepare_match_query(
            filters,
            filter_kwargs,
            threshold=threshold,
            cutoff=cutoff,
            algorithm=algorithm,
            limit=limit,
        )
        path = f"/match/{quote(dataset, safe='')}"

        def run_one(entity: EntityInput) -> MatchResponse:
            _check_matchable_schema(entity)
            raw = self._request(
                "POST", path, params=params, json=_match_body(entity, weights, config)
            )
            return MatchResponse.model_validate(unwrap_match_response(raw))

        pairs = iter(entities)
        pending: dict[Future[MatchResponse], str] = {}
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            exhausted = False
            while True:
                while not exhausted and len(pending) < workers:
                    try:
                        key, entity = next(pairs)
                    except StopIteration:
                        exhausted = True
                        break
                    pending[executor.submit(run_one, entity)] = key
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    key = pending.pop(future)
                    result: MatchResponse | MatchError
                    try:
                        result = future.result()
                    # The "collect" contract is total: any per-item failure
                    # (transport, API, validation) must come back in-band.
                    except Exception as exc:
                        if on_error == "raise":
                            raise
                        result = MatchError(key=key, exception=exc)
                    yield key, result
        finally:
            # Block until in-flight requests finish so a caller's
            # `with Client(...)` exit can't close the transport under them.
            executor.shutdown(wait=True, cancel_futures=True)
