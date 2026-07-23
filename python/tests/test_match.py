"""Tests for client.match() — v2-shape over v1 wire, single-entity payload, unwrap."""

import json
from collections.abc import Callable

import httpx

from yente_client.client import BEST_ALGORITHM
from yente_client.entities import Person
from yente_client.filters import MatchFilters
from yente_client.models import MatchResponse


def _record_request(
    payload: dict,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return handler, seen


# ---------- happy path ----------


def test_match_returns_unwrapped_match_response(make_client, load_fixture) -> None:
    """v1 wire shape `{responses: {q: {...}}, limit}` is unwrapped to flat MatchResponse."""
    handler, _ = _record_request(load_fixture("match_high_score"))
    with make_client(handler=handler) as c:
        r = c.match(Person(firstName="Aleksandr", lastName="Zacharov"))
    assert isinstance(r, MatchResponse)
    assert r.limit == 5
    assert r.total.value == 1
    assert r.top is not None
    assert r.top.score == 0.92
    assert r.top.match is True


def test_match_top_caption(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_high_score"))
    with make_client(handler=handler) as c:
        r = c.match(Person(firstName="Aleksandr"))
    assert r.top is not None
    assert "ZAKHAROV" in r.top.caption


def test_match_matches_filter_when_above_threshold(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_high_score"))
    with make_client(handler=handler) as c:
        r = c.match(Person(firstName="X"))
    assert len(r.matches) == 1


def test_match_matches_empty_when_below_threshold(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_below_threshold"))
    with make_client(handler=handler) as c:
        r = c.match(Person(firstName="X"))
    assert r.matches == []
    assert len(r.results) == 1
    assert r.results[0].match is False


def test_match_zero_results(make_client, load_fixture) -> None:
    handler, _ = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        r = c.match(Person(firstName="ZZZZZZ"))
    assert r.results == []
    assert r.top is None


# ---------- wire shape: body, URL, params ----------


def test_match_posts_to_correct_dataset_path(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), datasets=["sanctions"])
    assert seen[0].url.path == "/match/sanctions"
    assert seen[0].method == "POST"


def test_match_defaults_to_default_dataset(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"))
    assert seen[0].url.path == "/match/default"


def test_match_body_wraps_entity_under_q(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="Aleksandr", lastName="Zacharov"))
    body = json.loads(seen[0].content)
    assert "queries" in body
    assert list(body["queries"].keys()) == ["q"]
    q = body["queries"]["q"]
    assert q["schema"] == "Person"
    assert q["properties"]["firstName"] == ["Aleksandr"]
    assert q["properties"]["lastName"] == ["Zacharov"]


def test_match_body_includes_weights_and_config(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(
            Person(firstName="X"),
            weights={"name_literal": 0.8},
            config={"some_knob": 1},
        )
    body = json.loads(seen[0].content)
    assert body["weights"] == {"name_literal": 0.8}
    assert body["config"] == {"some_knob": 1}


def test_match_body_has_empty_weights_and_config_by_default(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"))
    body = json.loads(seen[0].content)
    assert body["weights"] == {}
    assert body["config"] == {}


# ---------- filters ----------


def test_match_threshold_passes_as_query_param(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), threshold=0.85)
    assert seen[0].url.params.get("threshold") == "0.85"


def test_match_cutoff_passes_as_query_param(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), cutoff=0.6)
    assert seen[0].url.params.get("cutoff") == "0.6"


def test_match_algorithm_passes_as_query_param(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), algorithm=BEST_ALGORITHM)
    assert seen[0].url.params.get("algorithm") == "best"


def test_match_limit_passes_as_query_param(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), limit=20)
    assert seen[0].url.params.get("limit") == "20"


def test_match_topics_filter(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), topics=["sanction", "role.pep"])
    assert seen[0].url.params.get_list("topics") == ["sanction", "role.pep"]


def test_match_exclude_entities_renamed_to_exclude_entity_ids(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), exclude_entities=["Q1", "Q2"])
    p = seen[0].url.params
    assert p.get_list("exclude_entity_ids") == ["Q1", "Q2"]
    assert "exclude_entities" not in p


def test_match_exclude_schemata_renamed_to_exclude_schema(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), exclude_schemata=["Address"])
    assert seen[0].url.params.get_list("exclude_schema") == ["Address"]


def test_match_multiple_datasets_via_include_dataset(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), datasets=["sanctions", "us_ofac_sdn"])
    assert seen[0].url.path == "/match/sanctions"
    assert seen[0].url.params.get_list("include_dataset") == ["us_ofac_sdn"]


def test_match_filters_object_and_kwarg_override(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    f = MatchFilters(datasets=["default"], topics=["sanction"])
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), filters=f, topics=["role.pep"])
    assert seen[0].url.params.get_list("topics") == ["role.pep"]


def test_match_changed_since_passed_through(make_client, load_fixture) -> None:
    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c:
        c.match(Person(firstName="X"), changed_since="2022-02-24")
    assert seen[0].url.params.get("changed_since") == "2022-02-24"


def test_best_algorithm_constant_is_best() -> None:
    assert BEST_ALGORITHM == "best"


# ---------- non-matchable schema check ----------


def test_match_non_matchable_schema_raises_before_http(make_client, load_fixture) -> None:
    """A non-matchable schema (e.g. Document) is refused client-side; no HTTP call goes out."""
    import pytest

    from yente_client.entities import Document
    from yente_client.exceptions import ConfigurationError

    handler, seen = _record_request(load_fixture("match_zero_results"))
    with make_client(handler=handler) as c, pytest.raises(ConfigurationError) as exc_info:
        c.match(Document(fileName="foo.pdf"))
    # No HTTP request was sent.
    assert seen == []
    # Error message points at the discovery path.
    assert "ref schemas --matchable" in str(exc_info.value)
    assert "Document" in str(exc_info.value)


async def test_async_match_non_matchable_schema_raises(make_async_client, load_fixture) -> None:
    import pytest

    from yente_client.entities import Document
    from yente_client.exceptions import ConfigurationError

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called for non-matchable schema")

    async with make_async_client(handler=handler) as c:
        with pytest.raises(ConfigurationError):
            await c.match(Document(fileName="foo.pdf"))
