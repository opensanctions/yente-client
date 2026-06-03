"""Asynchronous yente / OpenSanctions client.

Mirrors :class:`yente_client.client.Client` method-for-method; every endpoint
returns a coroutine. Refer to the sync client for per-method documentation —
the only structural differences are language-level (``await``, ``async for``).
"""

from types import TracebackType
from typing import Any, Self, overload
from urllib.parse import quote

import httpx

from yente_client._http import prepare_http_kwargs, raise_for_response
from yente_client._translation import (
    merge_filters,
    serialise_match_filters,
    serialise_search_filters,
    unwrap_match_response,
)
from yente_client.client import _check_matchable_schema
from yente_client.entities import EntityInput
from yente_client.exceptions import TransportError
from yente_client.filters import MatchFilters, SearchFilters
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    MatchResponse,
    SearchResponse,
    StatusResponse,
)


class AsyncClient:
    """Asynchronous client for the yente / OpenSanctions API.

    Use as an async context manager for deterministic cleanup of the underlying
    ``httpx.AsyncClient``. See :class:`yente_client.Client` for the sync
    counterpart and the design doc §4.6 for the full constructor contract.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.opensanctions.org",
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
        # Async transport is different from sync. Honour a caller-supplied
        # MockTransport for tests; otherwise stack httpx's connection-level
        # retries (DNS, connection-refused) for free.
        kwargs["transport"] = transport or httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(**kwargs)
        self._base_url = base_url

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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one HTTP request; map errors and decode JSON.

        Async mirror of :meth:`yente_client.client.Client._request`. Same
        error semantics: :class:`APIError` subclass on non-2xx,
        :class:`TransportError` on connection-level failure.
        """
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise TransportError(str(exc)) from exc

        if not response.is_success:
            raise_for_response(response)

        return response.json()

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
        return DatasetsResponse.model_validate(await self._request("GET", "/catalog"))

    async def algorithms(self) -> AlgorithmsResponse:
        """Async equivalent of :meth:`yente_client.client.Client.algorithms`."""
        return AlgorithmsResponse.model_validate(await self._request("GET", "/algorithms"))

    # ----- entity fetch -----

    async def fetch(self, entity_id: str, *, nested: bool = True) -> Entity:
        """Async equivalent of :meth:`yente_client.client.Client.fetch`."""
        params = {"nested": "true" if nested else "false"}
        path = f"/entities/{quote(entity_id, safe='')}"
        return Entity.model_validate(await self._request("GET", path, params=params))

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
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        if sort:
            params["sort"] = sort

        eid = quote(entity_id, safe="")
        if prop is None:
            raw = await self._request("GET", f"/entities/{eid}/adjacent", params=params)
            return AdjacentResponse.model_validate(raw)
        path = f"/entities/{eid}/adjacent/{quote(prop, safe='')}"
        return AdjacentPropertyResponse.model_validate(
            await self._request("GET", path, params=params)
        )

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
            await self._request("GET", f"/search/{quote(dataset, safe='')}", params=params)
        )

    # ----- match -----

    async def match(
        self,
        entity: EntityInput,
        *,
        filters: MatchFilters | None = None,
        threshold: float | None = None,
        algorithm: str | None = None,
        weights: dict[str, float] | None = None,
        config: dict[str, Any] | None = None,
        limit: int | None = None,
        **filter_kwargs: Any,
    ) -> MatchResponse:
        """Async equivalent of :meth:`yente_client.client.Client.match`."""
        _check_matchable_schema(entity)
        f = merge_filters(MatchFilters, filters, filter_kwargs)
        dataset, params = serialise_match_filters(f)

        if threshold is not None:
            params["threshold"] = threshold
        if algorithm is not None:
            params["algorithm"] = algorithm
        if limit is not None:
            params["limit"] = limit

        body: dict[str, Any] = {
            "queries": {"q": entity.to_payload()},
            "weights": weights or {},
            "config": config or {},
        }

        raw = await self._request(
            "POST", f"/match/{quote(dataset, safe='')}", params=params, json=body
        )
        return MatchResponse.model_validate(unwrap_match_response(raw))
