"""Tests for the entity-trimming the MCP tools return.

Pure functions over the SDK response models — no network, no FastMCP.
"""

from yente_client.mcp.shaping import (
    dataset_index,
    shape_adjacency,
    shape_adjacency_property,
    shape_edge,
    shape_entity,
    shape_scored,
)
from yente_client.models import (
    AdjacentPropertyResponse,
    AdjacentResponse,
    Dataset,
    DatasetsResponse,
    Entity,
    FeatureResult,
    ScoredEntity,
    TotalSpec,
)


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
