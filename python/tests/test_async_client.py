"""Tests for ``AsyncClient`` — parity with the sync ``Client`` at a smaller scale.

We don't re-test every error-mapping / UA-assembly case; those are exercised
by the sync suite and the shared ``prepare_http_kwargs`` / ``raise_for_response``
helpers do the actual work. What we DO verify here:
  - ``AsyncClient`` builds with the same kwargs as ``Client``
  - The async context manager closes the underlying httpx.AsyncClient
  - Each endpoint is genuinely awaitable and parses responses correctly
  - Error mapping fires identically through the async path
"""

import json
from typing import Any

import httpx
import pytest

from yente_client.async_client import AsyncClient
from yente_client.client import BEST_ALGORITHM
from yente_client.entities import Person
from yente_client.exceptions import AuthenticationError, BadRequestError, TransportError
from yente_client.models import (
    DatasetsResponse,
    Entity,
    MatchResponse,
    ProgramsResponse,
    SearchResponse,
    StatusResponse,
)


def _fixed(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {})

    return handler


# ---------- Construction parity ----------


async def test_async_client_user_agent_matches_sync_shape() -> None:
    async with AsyncClient(api_key="test", app_name="MyApp") as c:
        ua = c.user_agent
    assert ua.startswith("yente-client/")
    assert "MyApp" in ua
    assert "python/" in ua
    assert "httpx/" in ua


async def test_async_client_aclose_closes_underlying_http() -> None:
    c = AsyncClient(api_key="test")
    assert not c._http.is_closed
    async with c:
        pass
    assert c._http.is_closed


async def test_async_client_invalid_app_name_raises_at_construction() -> None:
    from yente_client.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        AsyncClient(api_key="test", app_name="bad name")


# ---------- Endpoints (each runs through the await path) ----------


async def test_async_healthz(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("status_ok"))) as c:
        r = await c.healthz()
    assert isinstance(r, StatusResponse)
    assert r.status == "ok"


async def test_async_datasets(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("catalog"))) as c:
        r = await c.datasets()
    assert isinstance(r, DatasetsResponse)
    assert r.datasets[0].name == "default"


async def test_async_programs(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("programs"))) as c:
        r = await c.programs()
    assert isinstance(r, ProgramsResponse)
    assert [p.key for p in r.data] == ["US-RUSHAR", "EU-UKR"]


async def test_async_programs_revalidates_with_etag(make_async_client, load_fixture) -> None:
    payload = load_fixture("programs")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=payload, headers={"ETag": '"v1"'})

    async with make_async_client(handler=handler) as c:
        first = await c.programs()
        second = await c.programs()  # served from the held body via 304
    assert [p.key for p in second.data] == [p.key for p in first.data]


async def test_async_fetch(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("entity_person"))) as c:
        e = await c.fetch("NK-aU5ybkbRFJucf8YMwsJvDw")
    assert isinstance(e, Entity)
    assert e.schema_ == "Person"


async def test_async_search(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("search_basic"))) as c:
        r = await c.search("acme", datasets=["default"])
    assert isinstance(r, SearchResponse)
    assert r.total.value == 2


async def test_async_match_unwraps_v1_envelope(make_async_client, load_fixture) -> None:
    async with make_async_client(handler=_fixed(load_fixture("match_high_score"))) as c:
        r = await c.match(
            Person(firstName="Aleksandr", lastName="Zacharov"),
            datasets=["sanctions"],
            algorithm=BEST_ALGORITHM,
        )
    assert isinstance(r, MatchResponse)
    assert r.top is not None
    assert r.top.score == 0.92


async def test_async_match_posts_correct_body(make_async_client, load_fixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("match_zero_results"))

    async with make_async_client(handler=handler) as c:
        await c.match(Person(firstName="X"), datasets=["sanctions"])

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/match/sanctions"
    body = json.loads(seen[0].content)
    assert body["queries"]["q"]["schema"] == "Person"


# ---------- Errors fire identically through the async path ----------


async def test_async_400_maps_to_bad_request(make_async_client) -> None:
    async with make_async_client(handler=_fixed({"detail": "bad schema"}, status=400)) as c:
        with pytest.raises(BadRequestError):
            await c._request("GET", "/x")


async def test_async_401_maps_to_authentication(make_async_client) -> None:
    async with make_async_client(handler=_fixed({"detail": "nope"}, status=401)) as c:
        with pytest.raises(AuthenticationError):
            await c._request("GET", "/x")


async def test_async_transport_error_wrapped(make_async_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with make_async_client(handler=handler) as c:
        with pytest.raises(TransportError):
            await c._request("GET", "/x")
