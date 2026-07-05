"""Endpoint tests: healthz, readyz, datasets, algorithms, fetch, adjacent."""

from typing import Any

import httpx

from yente_client.client import PROGRAMS_URL
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    AlgorithmsResponse,
    DatasetsResponse,
    Entity,
    ProgramsResponse,
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


# ---------- programs (public artifact, not the API host) ----------


def test_programs_returns_programs_response(make_client, load_fixture) -> None:
    payload = load_fixture("programs")
    with make_client(handler=_fixed_response(payload)) as c:
        r = c.programs()
    assert isinstance(r, ProgramsResponse)
    assert [p.key for p in r.data] == ["US-RUSHAR", "EU-UKR"]
    assert r.data[0].issuer is not None
    assert r.data[0].issuer.acronym == "OFAC"
    assert r.data[0].aliases == ["RUSSIA-EO14024"]
    assert r.data[1].summary is None


def test_programs_hits_public_artifact_url(make_client) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    with make_client(handler=handler) as c:
        c.programs()
    # Absolute URL: overrides the client's base_url entirely.
    assert seen == [PROGRAMS_URL]


# ---------- ETag revalidation (programs today, /catalog once yente#1202 lands) ----------


def _etagged(payload: dict[str, Any], etag: str):
    """Handler serving ``payload`` with an ETag, answering If-None-Match with 304."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, json=payload, headers={"ETag": etag})

    return handler


def test_programs_revalidates_with_etag(make_client, load_fixture) -> None:
    payload = load_fixture("programs")
    with make_client(handler=_etagged(payload, '"v1"')) as c:
        first = c.programs()
        second = c.programs()  # served from the held body via 304
    assert [p.key for p in second.data] == [p.key for p in first.data]


def test_programs_refetches_after_etag_change(make_client, load_fixture) -> None:
    payload = load_fixture("programs")
    etags: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        etags.append(request.headers.get("If-None-Match"))
        # The artifact changed: the old validator no longer matches.
        return httpx.Response(200, json=payload, headers={"ETag": f'"v{len(etags)}"'})

    with make_client(handler=handler) as c:
        c.programs()
        c.programs()
        c.programs()
    # Each call revalidates against the last-seen ETag and picks up the new one.
    assert etags == [None, '"v1"', '"v2"']


def test_datasets_without_etag_behaves_plainly(make_client, load_fixture) -> None:
    """yente serves no ETag today: no If-None-Match sent, full fetch each call."""
    payload = load_fixture("catalog")
    etags: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        etags.append(request.headers.get("If-None-Match"))
        return httpx.Response(200, json=payload)

    with make_client(handler=handler) as c:
        c.datasets()
        c.datasets()
    assert etags == [None, None]


def test_datasets_revalidates_once_server_sends_etag(make_client, load_fixture) -> None:
    """The same path lights up for /catalog when yente#1202 ships."""
    payload = load_fixture("catalog")
    with make_client(handler=_etagged(payload, '"idx-7"')) as c:
        first = c.datasets()
        second = c.datasets()
    assert [d.name for d in second.datasets] == [d.name for d in first.datasets]


# ---------- statements (OpenSanctions API only) ----------


def test_statements_returns_statements_response(make_client, load_fixture) -> None:
    import httpx as _httpx

    from yente_client.models import StatementsResponse

    seen: list[_httpx.Request] = []

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return _httpx.Response(200, json=load_fixture("statements"))

    with make_client(handler=handler) as c:
        r = c.statements(canonical_id="NK-aU5ybkbRFJucf8YMwsJvDw", limit=50)
    assert isinstance(r, StatementsResponse)
    assert seen[0].url.path == "/statements"
    assert seen[0].url.params["canonical_id"] == "NK-aU5ybkbRFJucf8YMwsJvDw"
    assert seen[0].url.params["limit"] == "50"
    assert len(r.results) == 2
    assert r.results[0].schema_ == "Person"
    assert r.results[0].prop == "name"
    assert r.results[0].original_value == "John Doe (Esq.)"
    assert r.results[1].lang is None


def test_statements_404_on_yente_rewraps_with_pointed_message(make_client) -> None:
    """A yente instance returns 404; the SDK rewraps with a pointed message."""
    import httpx as _httpx
    import pytest

    from yente_client.exceptions import NotFoundError

    def handler(request: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(404, json={"detail": "Not Found"})

    with make_client(handler=handler) as c, pytest.raises(NotFoundError) as exc_info:
        c.statements(entity_id="anything")
    msg = str(exc_info.value)
    assert "/statements endpoint is not available" in msg
    assert "OpenSanctions API" in msg
    # The original 404 is preserved.
    assert exc_info.value.status_code == 404


def test_statements_passes_all_filter_params(make_client, load_fixture) -> None:
    import httpx as _httpx

    seen: list[_httpx.Request] = []

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return _httpx.Response(200, json=load_fixture("statements"))

    with make_client(handler=handler) as c:
        c.statements(
            dataset="us_ofac_sdn",
            entity_id="ofac-1234",
            canonical_id="NK-aU5ybkbRFJucf8YMwsJvDw",
            prop="alias",
            value="Johnny D",
            schema="Person",
            sort=["first_seen", "prop"],
            limit=10,
            offset=20,
        )
    params = seen[0].url.params
    assert params["dataset"] == "us_ofac_sdn"
    assert params["entity_id"] == "ofac-1234"
    assert params["canonical_id"] == "NK-aU5ybkbRFJucf8YMwsJvDw"
    assert params["prop"] == "alias"
    assert params["value"] == "Johnny D"
    assert params["schema"] == "Person"
    assert params.get_list("sort") == ["first_seen", "prop"]
    assert params["limit"] == "10"
    assert params["offset"] == "20"


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
