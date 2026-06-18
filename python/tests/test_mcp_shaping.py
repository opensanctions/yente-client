"""Tests for the entity-trimming the MCP tools return.

Pure functions over the SDK response models — no network, no FastMCP.
"""

from yente_client.mcp.shaping import shape_entity, shape_scored
from yente_client.models import Entity, FeatureResult, ScoredEntity


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
