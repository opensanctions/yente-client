"""Asynchronous yente / OpenSanctions client.

Mirrors :class:`yente_client.client.Client` method-for-method; every endpoint
returns a coroutine. Refer to the sync client for per-method documentation —
the only structural differences are language-level (``await``, ``async for``).
"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from types import TracebackType
from typing import Any, Literal, Self, overload

import httpx

from yente_client._http import ETagCache, prepare_http_kwargs, read_json
from yente_client._translation import (
    check_matchable_schema,
    drop_none,
    entity_path,
    match_body,
    match_request,
    search_request,
    statements_unavailable,
    unwrap_match_response,
)
from yente_client.client import PROGRAMS_URL
from yente_client.entities import EntityInput
from yente_client.env import DEFAULT_BASE_URL
from yente_client.exceptions import NotFoundError, TransportError
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


class AsyncClient:
    """Asynchronous client for the yente / OpenSanctions API.

    Use as an async context manager for deterministic cleanup of the underlying
    ``httpx.AsyncClient``. See :class:`yente_client.Client` for the sync
    counterpart.
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
        transport: httpx.AsyncBaseTransport | None = None,
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
        # Async transport is different from sync. Honor a caller-supplied
        # MockTransport for tests; otherwise stack httpx's connection-level
        # retries (DNS, connection-refused) for free.
        kwargs["transport"] = transport or httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(**kwargs)
        self._etags = ETagCache()

    @property
    def user_agent(self) -> str:
        """Return the User-Agent header this client sends on every request."""
        return self._http.headers["User-Agent"]

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Async mirror of :meth:`yente_client.client.Client._send`."""
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise TransportError(str(exc)) from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Async mirror of :meth:`yente_client.client.Client._request`."""
        return read_json(await self._send(method, path, **kwargs))

    async def _request_revalidated(self, url: str) -> Any:
        """Async mirror of :meth:`yente_client.client.Client._request_revalidated`."""
        response = await self._send("GET", url, headers=self._etags.headers(url))
        return self._etags.read(url, response)

    # ----- system / health endpoints -----

    async def healthz(self) -> StatusResponse:
        """Async equivalent of :meth:`yente_client.client.Client.healthz`."""
        return StatusResponse.model_validate(await self._request("GET", "/healthz"))

    async def readyz(self) -> StatusResponse:
        """Async equivalent of :meth:`yente_client.client.Client.readyz`."""
        return StatusResponse.model_validate(await self._request("GET", "/readyz"))

    # ----- datasets / introspection -----

    async def datasets(self) -> DatasetsResponse:
        """Async equivalent of :meth:`yente_client.client.Client.datasets`."""
        return DatasetsResponse.model_validate(await self._request_revalidated("/catalog"))

    async def algorithms(self) -> AlgorithmsResponse:
        """Async equivalent of :meth:`yente_client.client.Client.algorithms`."""
        return AlgorithmsResponse.model_validate(await self._request("GET", "/algorithms"))

    async def programs(self) -> ProgramsResponse:
        """Async equivalent of :meth:`yente_client.client.Client.programs`."""
        return ProgramsResponse.model_validate(await self._request_revalidated(PROGRAMS_URL))

    # ----- statements (OpenSanctions API only) -----

    async def statements(
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
        """Async equivalent of :meth:`yente_client.client.Client.statements`."""
        params = drop_none(
            offset=offset,
            dataset=dataset,
            entity_id=entity_id,
            canonical_id=canonical_id,
            prop=prop,
            value=value,
            schema=schema,
            sort=sort or None,
            limit=limit,
        )
        try:
            raw = await self._request("GET", "/statements", params=params)
        except NotFoundError as exc:
            raise statements_unavailable(exc) from exc
        return StatementsResponse.model_validate(raw)

    # ----- entity fetch -----

    async def fetch(self, entity_id: str, *, nested: bool = True) -> Entity:
        """Async equivalent of :meth:`yente_client.client.Client.fetch`."""
        params = {"nested": "true" if nested else "false"}
        raw = await self._request("GET", entity_path(entity_id), params=params)
        return Entity.model_validate(raw)

    @overload
    async def adjacent(
        self,
        entity_id: str,
        *,
        prop: None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentResponse: ...

    @overload
    async def adjacent(
        self,
        entity_id: str,
        *,
        prop: str,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentPropertyResponse: ...

    async def adjacent(
        self,
        entity_id: str,
        *,
        prop: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: list[str] | None = None,
    ) -> AdjacentResponse | AdjacentPropertyResponse:
        """Async equivalent of :meth:`yente_client.client.Client.adjacent`."""
        params = drop_none(offset=offset, limit=limit, sort=sort or None)
        if prop is None:
            raw = await self._request("GET", entity_path(entity_id, "adjacent"), params=params)
            return AdjacentResponse.model_validate(raw)
        raw = await self._request("GET", entity_path(entity_id, "adjacent", prop), params=params)
        return AdjacentPropertyResponse.model_validate(raw)

    # ----- search -----

    async def search(
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
        """Async equivalent of :meth:`yente_client.client.Client.search`."""
        path, params = search_request(
            q,
            filters,
            filter_kwargs,
            limit=limit,
            offset=offset,
            sort=sort,
            fuzzy=fuzzy,
            simple=simple,
            facets=facets,
        )
        return SearchResponse.model_validate(await self._request("GET", path, params=params))

    # ----- match -----

    async def match(
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
        """Async equivalent of :meth:`yente_client.client.Client.match`."""
        check_matchable_schema(entity)
        path, params = match_request(
            filters,
            filter_kwargs,
            threshold=threshold,
            cutoff=cutoff,
            algorithm=algorithm,
            limit=limit,
        )
        body = match_body(entity, weights, config)
        raw = await self._request("POST", path, params=params, json=body)
        return MatchResponse.model_validate(unwrap_match_response(raw))

    async def match_iter(
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
    ) -> AsyncIterator[tuple[str, MatchResponse | MatchError]]:
        """Async equivalent of :meth:`yente_client.client.Client.match_iter`.

        An async generator — iterate with ``async for``, no ``await`` on the
        call itself.
        """
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        path, params = match_request(
            filters,
            filter_kwargs,
            threshold=threshold,
            cutoff=cutoff,
            algorithm=algorithm,
            limit=limit,
        )

        async def run_one(entity: EntityInput) -> MatchResponse:
            check_matchable_schema(entity)
            raw = await self._request(
                "POST", path, params=params, json=match_body(entity, weights, config)
            )
            return MatchResponse.model_validate(unwrap_match_response(raw))

        pairs = iter(entities)
        pending: dict[asyncio.Task[MatchResponse], str] = {}
        try:
            exhausted = False
            while True:
                while not exhausted and len(pending) < workers:
                    try:
                        key, entity = next(pairs)
                    except StopIteration:
                        exhausted = True
                        break
                    pending[asyncio.ensure_future(run_one(entity))] = key
                if not pending:
                    break
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    key = pending.pop(task)
                    result: MatchResponse | MatchError
                    try:
                        result = task.result()
                    # The "collect" contract is total: any per-item failure
                    # (transport, API, validation) must come back in-band.
                    except Exception as exc:
                        if on_error == "raise":
                            raise
                        result = MatchError(key=key, exception=exc)
                    yield key, result
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
