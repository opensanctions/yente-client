"""Tests for response models — parsing, recursion, and convenience accessors."""

from datetime import datetime

from yente_client.models import (
    Algorithm,
    AlgorithmsResponse,
    DataCoverage,
    DataPublisher,
    DatasetsResponse,
    Entity,
    MatchResponse,
    ScoredEntity,
    SearchFacet,
    SearchResponse,
    StatusResponse,
    TotalSpec,
)


def test_entity_basic() -> None:
    e = Entity.model_validate(
        {
            "id": "Q42",
            "caption": "Douglas Adams",
            "schema": "Person",
            "properties": {"firstName": ["Douglas"], "lastName": ["Adams"]},
            "datasets": ["wd_curated"],
            "target": False,
        }
    )
    assert e.id == "Q42"
    assert e.schema_ == "Person"
    assert e.properties["firstName"] == ["Douglas"]


def test_entity_nested() -> None:
    """A Person's properties.sanctions list may contain nested Entity objects
    when the server traverses adjacents."""
    raw = {
        "id": "Q1",
        "caption": "Sanctioned Person",
        "schema": "Person",
        "properties": {
            "firstName": ["X"],
            "sanctions": [
                {
                    "id": "sanction-1",
                    "caption": "EU sanction 2022",
                    "schema": "Sanction",
                    "properties": {"authority": ["EU"]},
                }
            ],
        },
    }
    e = Entity.model_validate(raw)
    assert isinstance(e.properties["sanctions"][0], Entity)
    nested = e.properties["sanctions"][0]
    assert isinstance(nested, Entity)
    assert nested.schema_ == "Sanction"
    assert nested.properties["authority"] == ["EU"]


def test_entity_datetime_parsing() -> None:
    e = Entity.model_validate(
        {
            "id": "Q1",
            "caption": "Test",
            "schema": "Person",
            "properties": {},
            "first_seen": "2023-01-15T10:00:00",
            "last_change": "2023-06-20T15:30:45",
        }
    )
    assert isinstance(e.first_seen, datetime)
    assert e.first_seen.year == 2023
    assert e.last_change is not None


def test_entity_ignores_unknown_fields() -> None:
    """Forward-compat: a future yente field shouldn't break parsing."""
    e = Entity.model_validate(
        {
            "id": "Q1",
            "caption": "Test",
            "schema": "Person",
            "properties": {},
            "future_field_we_havent_seen": "whatever",
        }
    )
    assert e.id == "Q1"


def test_scored_entity() -> None:
    se = ScoredEntity.model_validate(
        {
            "id": "Q1",
            "caption": "Match",
            "schema": "Person",
            "properties": {},
            "score": 0.85,
            "match": True,
            "explanations": {"name_match": {"score": 0.9, "detail": "exact match", "weight": 1.0}},
        }
    )
    assert se.score == 0.85
    assert se.match is True
    assert se.explanations["name_match"].score == 0.9


def test_contributing_explanations_drops_zero_keeps_negative() -> None:
    se = ScoredEntity.model_validate(
        {
            "id": "Q1",
            "caption": "Match",
            "schema": "Person",
            "score": 0.4,
            "match": False,
            "explanations": {
                "name_match": {"score": 0.6},
                "dob_disjoint": {"score": -0.95},
                "not_applicable": {"score": 0.0},
            },
        }
    )
    contributing = se.contributing_explanations
    assert set(contributing) == {"name_match", "dob_disjoint"}
    # raw explanations is left intact
    assert "not_applicable" in se.explanations


def test_match_response_top_with_results() -> None:
    mr = MatchResponse.model_validate(
        {
            "query": {"schema": "Person", "properties": {}},
            "results": [
                {
                    "id": "Q1",
                    "caption": "A",
                    "schema": "Person",
                    "properties": {},
                    "score": 0.95,
                    "match": True,
                },
                {
                    "id": "Q2",
                    "caption": "B",
                    "schema": "Person",
                    "properties": {},
                    "score": 0.4,
                    "match": False,
                },
            ],
            "total": {"value": 2, "relation": "eq"},
            "limit": 5,
        }
    )
    assert mr.top is not None
    assert mr.top.id == "Q1"
    assert mr.top.score == 0.95


def test_match_response_top_when_empty() -> None:
    mr = MatchResponse.model_validate(
        {
            "query": {},
            "results": [],
            "total": {"value": 0, "relation": "eq"},
            "limit": 5,
        }
    )
    assert mr.top is None
    assert mr.matches == []


def test_match_response_matches_filters_by_match_flag() -> None:
    mr = MatchResponse.model_validate(
        {
            "query": {},
            "results": [
                {
                    "id": "Q1",
                    "caption": "A",
                    "schema": "Person",
                    "properties": {},
                    "score": 0.95,
                    "match": True,
                },
                {
                    "id": "Q2",
                    "caption": "B",
                    "schema": "Person",
                    "properties": {},
                    "score": 0.65,
                    "match": False,
                },
                {
                    "id": "Q3",
                    "caption": "C",
                    "schema": "Person",
                    "properties": {},
                    "score": 0.85,
                    "match": True,
                },
            ],
            "total": {"value": 3, "relation": "eq"},
            "limit": 5,
        }
    )
    matched = mr.matches
    assert len(matched) == 2
    assert {r.id for r in matched} == {"Q1", "Q3"}


def test_search_response_with_facets() -> None:
    sr = SearchResponse.model_validate(
        {
            "results": [{"id": "Q1", "caption": "A", "schema": "Company", "properties": {}}],
            "facets": {
                "countries": {
                    "label": "Countries",
                    "values": [
                        {"name": "us", "label": "United States", "count": 42},
                        {"name": "ru", "label": "Russia", "count": 17},
                    ],
                }
            },
            "total": {"value": 1, "relation": "eq"},
            "limit": 10,
            "offset": 0,
        }
    )
    assert isinstance(sr.facets["countries"], SearchFacet)
    assert sr.facets["countries"].values[0].count == 42


def test_status_response() -> None:
    sr = StatusResponse.model_validate({"status": "ok"})
    assert sr.status == "ok"


def test_algorithms_response() -> None:
    ar = AlgorithmsResponse.model_validate(
        {
            "algorithms": [
                {"name": "logic-v2", "description": "..."},
                {"name": "name-matcher"},
            ],
            "default": "best",
            "best": "logic-v2",
        }
    )
    assert ar.best == "logic-v2"
    assert isinstance(ar.algorithms[0], Algorithm)
    assert ar.algorithms[1].description is None


def test_datasets_response() -> None:
    cr = DatasetsResponse.model_validate(
        {
            "datasets": [
                {"name": "default", "title": "Default Collection"},
                {"name": "us_ofac_sdn", "title": "US OFAC SDN", "version": "2024-01-15"},
            ],
            "current": ["default", "us_ofac_sdn"],
            "outdated": [],
            "index_stale": False,
        }
    )
    assert len(cr.datasets) == 2
    assert cr.datasets[1].version == "2024-01-15"


def test_dataset_expanded_metadata() -> None:
    """Descriptive scalars plus the coverage/publisher nested models parse."""
    cr = DatasetsResponse.model_validate(
        {
            "datasets": [
                {
                    "name": "default",
                    "title": "Default Collection",
                    "summary": "Combined.",
                    "category": "collection",
                    "tags": ["sanctions", "peps"],
                    "entity_count": 1234567,
                    "thing_count": 987654,
                    "updated_at": "2026-05-30T08:15:00Z",
                    "last_export": "2026-05-30T09:00:00Z",
                    "coverage": {
                        "start": "2015",
                        "end": "2026",
                        "countries": ["us", "gb"],
                        "frequency": "daily",
                    },
                    "publisher": {
                        "name": "OpenSanctions",
                        "country": "zz",
                        "country_label": "Global",
                        "official": False,
                    },
                    "deprecated": False,
                }
            ],
            "current": ["default"],
        }
    )
    ds = cr.datasets[0]
    assert ds.entity_count == 1234567
    assert ds.tags == ["sanctions", "peps"]
    assert isinstance(ds.updated_at, datetime)
    assert isinstance(ds.coverage, DataCoverage)
    assert ds.coverage.countries == ["us", "gb"]
    assert isinstance(ds.publisher, DataPublisher)
    assert ds.publisher.country_label == "Global"


def test_dataset_metadata_defaults_when_absent() -> None:
    """A bare source entry leaves the descriptive fields at their defaults."""
    cr = DatasetsResponse.model_validate(
        {"datasets": [{"name": "us_ofac_sdn", "title": "OFAC SDN"}]}
    )
    ds = cr.datasets[0]
    assert ds.summary is None
    assert ds.entity_count is None
    assert ds.coverage is None
    assert ds.publisher is None
    assert ds.tags == []
    assert ds.deprecated is False


def test_total_spec_relation_literal() -> None:
    ts = TotalSpec.model_validate({"value": 100, "relation": "gte"})
    assert ts.relation == "gte"
