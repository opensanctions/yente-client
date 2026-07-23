"""Tests for match_iter — bounded fan-out over /match on both clients."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

from yente_client.entities import Document, EntityInput, Person
from yente_client.exceptions import ConfigurationError, ServerError
from yente_client.models import MatchError, MatchResponse


def _record_request(
    payload: dict,
) -> tuple:
    seen: list[httpx.Request] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(request)
        return httpx.Response(200, json=payload)

    return handler, seen


def _query_first_name(request: httpx.Request) -> str:
    body = json.loads(request.content)
    return body["queries"]["q"]["properties"]["firstName"][0]


def _pairs(n: int) -> list[tuple[str, EntityInput]]:
    return [(str(i), Person(firstName=f"P{i}")) for i in range(n)]


# ---------- happy path ----------


def test_match_iter_yields_every_key(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        results = dict(c.match_iter(_pairs(7), workers=3))
    assert set(results) == {str(i) for i in range(7)}
    assert all(isinstance(r, MatchResponse) for r in results.values())
    assert len(seen) == 7


def test_match_iter_empty_input(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        assert list(c.match_iter([])) == []
    assert seen == []


def test_match_iter_keys_map_to_their_entities(make_client, load_fixture) -> None:
    """Each yielded key belongs to the response for that key's entity."""
    fixture = load_fixture("match_zero_results")

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the queried name back through the response's query echo.
        payload = json.loads(json.dumps(fixture))
        payload["responses"]["q"]["query"] = {"echo": _query_first_name(request)}
        return httpx.Response(200, json=payload)

    with make_client(handler=handler) as c:
        for key, result in c.match_iter(_pairs(6), workers=4):
            assert isinstance(result, MatchResponse)
            assert result.query == {"echo": f"P{key}"}


# ---------- laziness / backpressure ----------


def test_match_iter_pulls_input_lazily(make_client, load_fixture) -> None:
    """No more than ~workers items are consumed from the input before the first yield."""
    handler, _ = _record_request(load_fixture("match_zero_results"))
    pulled = 0

    def generate() -> Iterator[tuple[str, EntityInput]]:
        nonlocal pulled
        for key, entity in _pairs(50):
            pulled += 1
            yield key, entity

    with make_client(handler=handler) as c:
        stream = c.match_iter(generate(), workers=3)
        next(stream)
        assert pulled <= 3
        assert len(dict(stream)) == 49  # the rest still arrives


def test_match_iter_runs_concurrently(make_client, load_fixture) -> None:
    payload = load_fixture("match_zero_results")
    lock = threading.Lock()
    inflight = 0
    max_inflight = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        time.sleep(0.03)
        with lock:
            inflight -= 1
        return httpx.Response(200, json=payload)

    with make_client(handler=handler) as c:
        assert len(dict(c.match_iter(_pairs(8), workers=4))) == 8
    assert max_inflight > 1


def test_match_iter_workers_must_be_positive(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c, pytest.raises(ValueError, match="workers"):
        next(c.match_iter(_pairs(1), workers=0))


# ---------- params ----------


def test_match_iter_passes_scoring_params_and_filters(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        dict(
            c.match_iter(
                _pairs(2),
                threshold=0.8,
                cutoff=0.6,
                algorithm="best",
                limit=10,
                datasets=["sanctions"],
                topics=["sanction"],
            )
        )
    for request in seen:
        assert request.url.path == "/match/sanctions"
        params = request.url.params
        assert params.get("threshold") == "0.8"
        assert params.get("cutoff") == "0.6"
        assert params.get("algorithm") == "best"
        assert params.get("limit") == "10"
        assert params.get("topics") == "sanction"


# ---------- errors ----------


def _fail_p2_handler(payload: dict):
    """Handler that 500s the entity with firstName P2 and records requests."""
    seen: list[httpx.Request] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(request)
        if _query_first_name(request) == "P2":
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json=payload)

    return handler, seen


def test_match_iter_raise_mode_aborts_on_failure(make_client, load_fixture) -> None:
    handler, _ = _fail_p2_handler(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c, pytest.raises(ServerError):
        dict(c.match_iter(_pairs(6), workers=2))


def test_match_iter_collect_mode_yields_match_error(make_client, load_fixture) -> None:
    handler, _ = _fail_p2_handler(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        results = dict(c.match_iter(_pairs(6), workers=2, on_error="collect"))
    assert set(results) == {str(i) for i in range(6)}
    failed = results["2"]
    assert isinstance(failed, MatchError)
    assert failed.key == "2"
    assert isinstance(failed.exception, ServerError)
    assert all(isinstance(r, MatchResponse) for k, r in results.items() if k != "2")


def test_match_iter_collect_non_matchable_schema_no_http(make_client, load_fixture) -> None:
    """A non-matchable schema fails in-band without a round-trip in collect mode."""
    handler, seen = _record_request(load_fixture("match_zero_results"))
    pairs: list[tuple[str, EntityInput]] = [
        ("ok", Person(firstName="X")),
        ("doc", Document(fileName="foo.pdf")),
    ]
    with make_client(handler=handler) as c:
        results = dict(c.match_iter(pairs, on_error="collect"))
    assert isinstance(results["ok"], MatchResponse)
    failed = results["doc"]
    assert isinstance(failed, MatchError)
    assert isinstance(failed.exception, ConfigurationError)
    assert len(seen) == 1  # only the Person went over the wire


def test_match_iter_raise_mode_non_matchable_schema(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_zero_results"))
    pairs: list[tuple[str, EntityInput]] = [("doc", Document(fileName="foo.pdf"))]
    with make_client(handler=handler) as c, pytest.raises(ConfigurationError):
        dict(c.match_iter(pairs))


# ---------- async ----------


async def test_async_match_iter_yields_every_key(make_async_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    results: dict[str, MatchResponse | MatchError] = {}
    async with make_async_client(handler=handler) as c:
        async for key, result in c.match_iter(_pairs(7), workers=3):
            results[key] = result
    assert set(results) == {str(i) for i in range(7)}
    assert all(isinstance(r, MatchResponse) for r in results.values())
    assert len(seen) == 7


async def test_async_match_iter_collect_mode(make_async_client, load_fixture) -> None:
    handler, _ = _fail_p2_handler(load_fixture("match_zero_results"))
    results: dict[str, MatchResponse | MatchError] = {}
    async with make_async_client(handler=handler) as c:
        async for key, result in c.match_iter(_pairs(6), workers=2, on_error="collect"):
            results[key] = result
    failed = results["2"]
    assert isinstance(failed, MatchError)
    assert isinstance(failed.exception, ServerError)
    assert len(results) == 6


async def test_async_match_iter_raise_mode_aborts(make_async_client, load_fixture) -> None:
    handler, _ = _fail_p2_handler(load_fixture("match_zero_results"))
    async with make_async_client(handler=handler) as c:
        with pytest.raises(ServerError):
            async for _ in c.match_iter(_pairs(6), workers=2):
                pass


async def test_async_match_iter_runs_concurrently(make_async_client, load_fixture) -> None:
    payload = load_fixture("match_zero_results")
    inflight = 0
    max_inflight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.03)
        inflight -= 1
        return httpx.Response(200, json=payload)

    count = 0
    async with make_async_client(handler=handler) as c:
        async for _ in c.match_iter(_pairs(8), workers=4):
            count += 1
    assert count == 8
    assert max_inflight > 1


async def test_async_match_iter_cutoff_param(make_async_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    async with make_async_client(handler=handler) as c:
        async for _ in c.match_iter(_pairs(1), cutoff=0.55):
            pass
    assert seen[0].url.params.get("cutoff") == "0.55"
