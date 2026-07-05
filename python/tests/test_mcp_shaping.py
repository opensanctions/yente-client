"""Tests for the entity-trimming the MCP tools return.

Pure functions over the SDK response models — no network, no FastMCP.
"""

from datetime import datetime

from yente_client.mcp.shaping import (
    dataset_index,
    program_index,
    shape_adjacency,
    shape_adjacency_property,
    shape_edge,
    shape_entity,
    shape_full_record,
    shape_scored,
    shape_statements,
    title_glossary,
)
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    Dataset,
    DatasetsResponse,
    Entity,
    FeatureResult,
    Program,
    ProgramIssuer,
    ScoredEntity,
    Statement,
    StatementsResponse,
    TotalSpec,
)
from yente_client.schemas import type_values


def test_shape_entity_keeps_core_identity_fields() -> None:
    entity = Entity(
        id="Q1",
        caption="Jane Doe",
        schema="Person",
        properties={"name": ["Jane Doe"]},
        datasets=["us_ofac_sdn"],
    )
    shaped = shape_entity(entity)
    assert shaped["id"] == "Q1"
    assert shaped["caption"] == "Jane Doe"
    assert shaped["schema"] == "Person"
    assert shaped["datasets"] == ["us_ofac_sdn"]
    assert shaped["properties"]["name"] == ["Jane Doe"]


def test_shape_entity_keeps_only_allowlisted_properties() -> None:
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"name": ["x"], "birthDate": ["1975"], "notes": ["junk"]},
    )
    props = shape_entity(entity)["properties"]
    assert "birthDate" in props
    assert "notes" not in props


def test_shape_entity_omits_absent_and_empty_properties() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"name": ["x"]})
    props = shape_entity(entity)["properties"]
    assert set(props) == {"name"}


def test_shape_entity_drops_nested_entity_values() -> None:
    nested = Entity(id="a1", caption="1 Main St", schema="Address")
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"address": ["1 Main St", nested]},
    )
    # Only the plain string survives; follow nested edges via fetch_entity_relations.
    assert shape_entity(entity)["properties"]["address"] == ["1 Main St"]


def test_shape_entity_resolves_topics_inline() -> None:
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"name": ["x"], "topics": ["sanction", "crime.war"]},
    )
    shaped = shape_entity(entity)
    # topics leave `properties` and become a top-level code → label map
    assert "topics" not in shaped["properties"]
    vocab = type_values("topic")
    assert shaped["topics"] == {"sanction": vocab["sanction"], "crime.war": vocab["crime.war"]}


def test_shape_entity_unknown_topic_falls_back_to_code() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"topics": ["not.a.topic"]})
    assert shape_entity(entity)["topics"] == {"not.a.topic": "not.a.topic"}


def test_shape_entity_no_topics_key_when_absent() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"name": ["x"]})
    assert "topics" not in shape_entity(entity)


def test_shape_entity_adds_country_glossary() -> None:
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"nationality": ["ru"], "country": ["de"]},
    )
    shaped = shape_entity(entity)
    vocab = type_values("country")
    assert shaped["countries"] == {"ru": vocab["ru"], "de": vocab["de"]}
    # the raw codes stay in properties — they're the filter inputs
    assert shaped["properties"]["nationality"] == ["ru"]


def test_shape_entity_country_glossary_only_from_country_typed_props() -> None:
    # "de" here is a registration number that happens to look like a code
    entity = Entity(
        id="Q1", caption="x", schema="Company", properties={"registrationNumber": ["de"]}
    )
    assert "countries" not in shape_entity(entity)


def test_shape_entity_keeps_program_id() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"programId": ["US-RUSHAR"]})
    assert shape_entity(entity)["properties"]["programId"] == ["US-RUSHAR"]


def test_shape_entity_honours_custom_property_selection() -> None:
    entity = Entity(
        id="Q1",
        caption="x",
        schema="Person",
        properties={"name": ["x"], "gender": ["male"]},
    )
    props = shape_entity(entity, properties=("gender",))["properties"]
    assert props == {"gender": ["male"]}


def test_shape_scored_adds_score_match_and_explanation() -> None:
    scored = ScoredEntity(
        id="Q1",
        caption="Jane",
        schema="Person",
        properties={"name": ["Jane"]},
        datasets=["x"],
        score=0.93,
        match=True,
        explanations={"name": FeatureResult(score=0.8)},
    )
    shaped = shape_scored(scored)
    assert shaped["score"] == 0.93
    assert shaped["match"] is True
    assert shaped["explanation"] == {"name": 0.8}
    # still carries the trimmed entity view
    assert shaped["id"] == "Q1"
    assert shaped["properties"]["name"] == ["Jane"]


def test_shape_scored_caps_and_ranks_explanations() -> None:
    scored = ScoredEntity(
        id="Q1",
        caption="x",
        schema="Person",
        score=0.5,
        match=False,
        explanations={
            "a": FeatureResult(score=0.1),
            "b": FeatureResult(score=0.9),
            "c": FeatureResult(score=0.5),
            "d": FeatureResult(score=0.7),
        },
    )
    explanation = shape_scored(scored)["explanation"]
    # top 3 by magnitude, highest first; the 0.1 feature is dropped
    assert list(explanation) == ["b", "d", "c"]
    assert "a" not in explanation


def test_shape_scored_drops_zero_and_ranks_by_magnitude() -> None:
    scored = ScoredEntity(
        id="Q1",
        caption="x",
        schema="Person",
        score=0.4,
        match=False,
        explanations={
            "name_match": FeatureResult(score=0.6),
            "dob_disjoint": FeatureResult(score=-0.95),  # strong penalty
            "not_applicable": FeatureResult(score=0.0),  # no signal
        },
    )
    explanation = shape_scored(scored)["explanation"]
    assert "not_applicable" not in explanation  # zero dropped
    assert list(explanation) == ["dob_disjoint", "name_match"]  # ranked by |score|
    assert explanation["dob_disjoint"] == -0.95  # negative preserved


def test_shape_edge_drops_backref_stubs_counterparty_keeps_attrs() -> None:
    asset = Entity(id="c1", caption="ACME Ltd", schema="Company")
    edge = Entity(
        id="own1",
        caption="Ownership",  # useless caption — why we don't stub edges
        schema="Ownership",
        datasets=["ext_ru_egrul"],
        properties={
            "owner": ["P1"],  # back-ref to the source
            "asset": [asset],  # counterparty
            "percentage": ["100"],
            "startDate": ["2019-01-01"],
        },
    )
    rec = shape_edge(edge, "P1")
    assert rec["id"] == "own1"
    assert rec["schema"] == "Ownership"
    assert rec["datasets"] == ["ext_ru_egrul"]  # provenance kept
    assert "owner" not in rec["properties"]  # back-ref dropped
    assert rec["properties"]["asset"] == [{"id": "c1", "caption": "ACME Ltd", "schema": "Company"}]
    assert rec["properties"]["percentage"] == ["100"]  # attrs kept as lists


def test_shape_edge_keeps_prose() -> None:
    edge = Entity(
        id="s1",
        caption="Sanction",
        schema="Sanction",
        properties={"entity": ["P1"], "reason": ["sanctioned because X"], "authority": ["OFAC"]},
    )
    rec = shape_edge(edge, "P1")
    assert "entity" not in rec["properties"]  # back-ref dropped
    assert rec["properties"]["reason"] == ["sanctioned because X"]  # prose kept
    assert rec["properties"]["authority"] == ["OFAC"]


def test_shape_adjacency_keys_by_edge_type_with_counts() -> None:
    src = Entity(id="P1", caption="Jane", schema="Person")
    asset = Entity(id="c1", caption="ACME", schema="Company")
    edge = Entity(
        id="own1",
        caption="Ownership",
        schema="Ownership",
        properties={"owner": ["P1"], "asset": [asset]},
    )
    resp = AdjacentResponse(
        entity=src,
        adjacent={
            "ownershipOwner": AdjacentPropertyResponse(
                results=[edge], total=TotalSpec(value=29, relation="eq"), limit=50, offset=0
            )
        },
    )
    out = shape_adjacency(resp)
    assert set(out) == {"ownershipOwner"}
    block = out["ownershipOwner"]
    assert (block["total"], block["limit"], block["offset"]) == (29, 50, 0)
    assert block["results"][0]["properties"]["asset"][0]["caption"] == "ACME"


def test_shape_adjacency_drops_riskSource_phantom_edge() -> None:
    src = Entity(id="P1", caption="Jane", schema="Person")
    resp = AdjacentResponse(
        entity=src,
        adjacent={
            "ownershipOwner": AdjacentPropertyResponse(
                results=[], total=TotalSpec(value=2, relation="eq"), limit=50, offset=0
            ),
            # Reports a count but resolves no entities — must be suppressed.
            "riskSource": AdjacentPropertyResponse(
                results=[], total=TotalSpec(value=33, relation="eq"), limit=50, offset=0
            ),
        },
    )
    out = shape_adjacency(resp)
    assert set(out) == {"ownershipOwner"}


def test_shape_full_record_keeps_dump_and_adds_glossaries() -> None:
    entity = Entity(
        id="Q1",
        caption="Jane Doe",
        schema="Person",
        properties={
            "name": ["Jane Doe"],
            "notes": ["kept — no trimming in the detail view"],
            "topics": ["sanction"],
            "birthCountry": ["ru"],  # country-typed but outside KEY_PROPERTIES
        },
        referents=["ofac-1234"],
    )
    record = shape_full_record(entity)
    # untrimmed dump: properties and referents survive
    assert record["properties"]["notes"] == ["kept — no trimming in the detail view"]
    assert record["properties"]["topics"] == ["sanction"]
    assert record["referents"] == ["ofac-1234"]
    # inline glossaries ride along at the top level
    assert record["topics"] == {"sanction": type_values("topic")["sanction"]}
    assert record["countries"] == {"ru": type_values("country")["ru"]}


def test_shape_full_record_omits_empty_glossaries() -> None:
    entity = Entity(id="Q1", caption="x", schema="Person", properties={"name": ["x"]})
    record = shape_full_record(entity)
    assert "topics" not in record
    assert "countries" not in record


def test_shape_statements_projects_provenance_rows() -> None:
    resp = StatementsResponse(
        results=[
            Statement(
                id="s1",
                entity_id="ofac-45937",
                canonical_id="NK-abc",
                prop="alias",
                prop_type="name",
                schema="Person",
                value="A. Zakharov",
                original_value="ZAKHAROV, A.",
                dataset="us_ofac_sdn",
                lang="eng",
                origin="patch",
                first_seen=datetime(2023, 7, 19, 18, 2, 43),
                last_seen=datetime(2026, 3, 25, 12, 53, 9),
            ),
            Statement(
                id="s2",
                entity_id="eu-fsf-12789",
                canonical_id="NK-abc",
                prop="birthDate",
                prop_type="date",
                schema="Person",
                value="1965-09-21",
                original_value="1965-09-21",  # unchanged by cleaning → omitted
                dataset="eu_fsf",
                first_seen=datetime(2024, 1, 1),
                last_seen=datetime(2026, 1, 1),
            ),
        ],
        total=TotalSpec(value=2, relation="eq"),
        limit=50,
        offset=0,
    )
    out = shape_statements(resp)
    assert (out["total"], out["limit"], out["offset"]) == (2, 50, 0)
    first = out["results"][0]
    assert first == {
        "prop": "alias",
        "value": "A. Zakharov",
        "dataset": "us_ofac_sdn",
        "entity_id": "ofac-45937",
        "first_seen": "2023-07-19",
        "lang": "eng",
        "original_value": "ZAKHAROV, A.",
        "origin": "patch",
    }
    second = out["results"][1]
    assert "canonical_id" not in second  # per-row canonical dropped (it's the query)
    assert "last_seen" not in second  # recency is noise for provenance
    assert "original_value" not in second  # same as value → omitted
    assert "lang" not in second
    assert "origin" not in second  # unset for plain crawled data


def test_title_glossary_keeps_known_sorted_dedupes() -> None:
    titles = {"us_ofac_sdn": "OFAC SDN", "eu_fsf": "EU Sanctions"}
    out = title_glossary(["us_ofac_sdn", "eu_fsf", "us_ofac_sdn", "no_such"], titles)
    assert out == {"eu_fsf": "EU Sanctions", "us_ofac_sdn": "OFAC SDN"}
    assert list(out) == ["eu_fsf", "us_ofac_sdn"]  # sorted


def test_title_glossary_empty_titles_yields_empty_legend() -> None:
    # failure-soft: a failed titles lookup degrades to an empty legend
    assert title_glossary(["us_ofac_sdn"], {}) == {}


def test_program_index_projects_compact_fields() -> None:
    programs = [
        Program(
            key="US-RUSHAR",
            title="Russian Harmful Foreign Activities Sanctions",
            summary="dropped from the index projection",
            issuer=ProgramIssuer(name="OFAC", territory="us"),
        ),
        Program(key="EU-UKR"),
    ]
    out = program_index(programs)
    assert out[0] == {
        "key": "US-RUSHAR",
        "title": "Russian Harmful Foreign Activities Sanctions",
        "territory": "us",
    }
    assert out[1] == {"key": "EU-UKR", "title": None, "territory": None}


def test_dataset_index_projects_compact_fields() -> None:
    resp = DatasetsResponse(
        datasets=[
            Dataset(
                name="us_ofac_sdn",
                title="OFAC Specially Designated Nationals",
                tags=["list.sanction"],
                entity_count=12000,
                summary="dropped from the index projection",
            ),
            Dataset(name="default", title="All datasets"),
        ]
    )
    out = dataset_index(resp)
    assert out[0] == {
        "name": "us_ofac_sdn",
        "title": "OFAC Specially Designated Nationals",
        "tags": ["list.sanction"],
        "entity_count": 12000,
    }
    assert out[1]["name"] == "default"
    assert out[1]["tags"] == []
    assert out[1]["entity_count"] is None


def test_shape_adjacency_property_shapes_results() -> None:
    edge = Entity(
        id="s1",
        caption="Sanction",
        schema="Sanction",
        properties={"entity": ["P1"], "authority": ["OFAC"]},
    )
    block = AdjacentPropertyResponse(
        results=[edge], total=TotalSpec(value=21, relation="eq"), limit=50, offset=0
    )
    out = shape_adjacency_property(block, "P1")
    assert out["total"] == 21
    assert out["results"][0]["properties"]["authority"] == ["OFAC"]
    assert "entity" not in out["results"][0]["properties"]
