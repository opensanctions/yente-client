"""Endpoint tests: healthz, readyz, datasets, algorithms, fetch, adjacent."""

from typing import Any

import httpx

from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    StatusResponse,
)


def _fixed_response(
    payload: dict[str, Any],
    status: int = 200,
    headers: dict[str, str] | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {})

    return handler


# ---------- healthz / readyz ----------


def test_healthz_returns_status_ok(make_client, load_fixture) -> None:
    payload = load_fixture("status_ok")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.healthz()
    assert isinstance(r, StatusResponse)
    assert r.status == "ok"


def test_healthz_hits_healthz_path(make_client) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler=handler) as c:
        c.healthz()
    assert seen == ["/healthz"]


def test_readyz_hits_readyz_path(make_client) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler=handler) as c:
        c.readyz()
    assert seen == ["/readyz"]


# ---------- datasets / algorithms ----------


def test_datasets_returns_datasets_response(make_client, load_fixture) -> None:
    payload = load_fixture("catalog")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.datasets()
    assert isinstance(r, DatasetsResponse)
    assert r.datasets[0].name == "default"
    assert r.datasets[0].load is True
    assert "default" in r.current
    assert r.index_stale is False


def test_algorithms_returns_algorithms_response(make_client, load_fixture) -> None:
    payload = load_fixture("algorithms")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.algorithms()
    assert isinstance(r, AlgorithmsResponse)
    assert r.best == "logic-v2"
    assert {a.name for a in r.algorithms} == {"logic-v2", "name-matcher"}


# ---------- fetch ----------


def test_fetch_returns_entity(make_client, load_fixture) -> None:
    payload = load_fixture("entity_person")
    with make_client(handler=_fixed_response(payload)) as c:
        e = c.fetch("NK-aU5ybkbRFJucf8YMwsJvDw")
    assert isinstance(e, Entity)
    assert e.id == "NK-aU5ybkbRFJucf8YMwsJvDw"
    assert e.schema_ == "Person"
    assert "sanction" in e.properties["topics"]
    assert e.target is True


def test_fetch_with_nested_entities(make_client, load_fixture) -> None:
    payload = load_fixture("entity_with_sanctions")
    with make_client(handler=_fixed_response(payload)) as c:
        e = c.fetch("NK-aU5ybkbRFJucf8YMwsJvDw")
    sanctions = e.properties["sanctions"]
    assert len(sanctions) == 1
    nested = sanctions[0]
    assert isinstance(nested, Entity)
    assert nested.schema_ == "Sanction"
    assert nested.properties["authority"] == ["European Union"]


def test_fetch_default_nested_param_true(make_client) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("nested"))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "caption": "x",
                "schema": "Person",
                "properties": {},
            },
        )

    with make_client(handler=handler) as c:
        c.fetch("x")
    assert seen == ["true"]


def test_fetch_nested_false_param(make_client) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("nested"))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "caption": "x",
                "schema": "Person",
                "properties": {},
            },
        )

    with make_client(handler=handler) as c:
        c.fetch("x", nested=False)
    assert seen == ["false"]


def test_fetch_url_encodes_id(make_client) -> None:
    """Special chars in the entity id must be percent-encoded on the wire."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # The raw, on-the-wire URL — httpx normalizes .path to decoded form.
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "caption": "x",
                "schema": "Person",
                "properties": {},
            },
        )

    with make_client(handler=handler) as c:
        c.fetch("weird/id with space")
    assert "weird%2Fid%20with%20space" in seen[0]


# ---------- adjacent ----------


def test_adjacent_full_returns_adjacent_response(make_client, load_fixture) -> None:
    payload = load_fixture("adjacent_full")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.adjacent("NK-aU5ybkbRFJucf8YMwsJvDw")
    assert isinstance(r, AdjacentResponse)
    assert r.entity.id == "NK-aU5ybkbRFJucf8YMwsJvDw"
    assert "sanctions" in r.adjacent
    assert r.adjacent["sanctions"].total.value == 1


def test_adjacent_property_returns_property_response(make_client, load_fixture) -> None:
    payload = load_fixture("adjacent_property")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.adjacent("NK-aU5ybkbRFJucf8YMwsJvDw", prop="sanctions")
    assert isinstance(r, AdjacentPropertyResponse)
    assert r.total.value == 2
    assert len(r.results) == 2


def test_adjacent_path_routing(make_client) -> None:
    """Without prop -> /adjacent. With prop -> /adjacent/<prop>."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        # Branch the response shape based on path so model_validate works.
        if request.url.path.endswith("/adjacent"):
            return httpx.Response(
                200,
                json={
                    "entity": {"id": "x", "caption": "x", "schema": "Person", "properties": {}},
                    "adjacent": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [],
                "total": {"value": 0, "relation": "eq"},
                "limit": 10,
                "offset": 0,
            },
        )

    with make_client(handler=handler) as c:
        c.adjacent("x")
        c.adjacent("x", prop="sanctions")

    assert seen == ["/entities/x/adjacent", "/entities/x/adjacent/sanctions"]


def test_adjacent_pagination_params(make_client) -> None:
    captured: list[set[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(set(request.url.params.multi_items()))
        return httpx.Response(
            200,
            json={
                "results": [],
                "total": {"value": 0, "relation": "eq"},
                "limit": 50,
                "offset": 25,
            },
        )

    with make_client(handler=handler) as c:
        c.adjacent("x", prop="sanctions", limit=50, offset=25)

    assert captured[0] == {("limit", "50"), ("offset", "25")}
