"""Tests for the FtM model lookups behind describe_schema and ftm:// resources.

Pure functions over the bundled model — no network, no FastMCP. Per the repo
drift rule these assert membership of stable anchors (schema/property/topic
names) plus sanity lower bounds, never exact counts that move with the model.
"""

import pytest

from yente_client.mcp import introspect
from yente_client.schemas import is_matchable_schema


def test_schema_index_includes_known_matchable() -> None:
    names = {s["name"] for s in introspect.schema_index()}
    assert "Person" in names
    assert "Company" in names


def test_schema_index_is_only_matchable() -> None:
    for entry in introspect.schema_index():
        assert is_matchable_schema(entry["name"]) is True


def test_schema_index_entry_is_compact() -> None:
    person = next(s for s in introspect.schema_index() if s["name"] == "Person")
    assert "LegalEntity" in person["extends"]
    # The index stays a map: per-property detail is describe_schema's job.
    assert "properties" not in person
    # Structural cruft is dropped.
    assert "label" not in person
    assert "plural" not in person


def test_schema_index_has_a_sane_lower_bound() -> None:
    assert len(introspect.schema_index()) >= 5


def test_describe_schema_person_flattens_inherited_properties() -> None:
    person = introspect.describe_schema("Person")
    assert person["name"] == "Person"
    assert person["matchable"] is True
    props = {p["name"]: p for p in person["properties"]}
    assert props["birthDate"]["type"] == "date"
    # inherited from LegalEntity / Thing
    assert "name" in props
    assert "topics" in props


def test_describe_schema_properties_sorted_by_name() -> None:
    names = [p["name"] for p in introspect.describe_schema("Person")["properties"]]
    assert names == sorted(names)


def test_describe_schema_entity_property_carries_range_and_reverse() -> None:
    owner = next(
        p for p in introspect.describe_schema("Ownership")["properties"] if p["name"] == "owner"
    )
    assert owner["type"] == "entity"
    assert owner["range"] == "LegalEntity"
    assert owner["reverse"] == "ownershipOwner"


def test_describe_schema_non_entity_property_omits_range() -> None:
    birth = next(
        p for p in introspect.describe_schema("Person")["properties"] if p["name"] == "birthDate"
    )
    assert "range" not in birth
    assert "reverse" not in birth


def test_describe_schema_surfaces_matchable_flag_both_ways() -> None:
    props = {p["name"]: p for p in introspect.describe_schema("Person")["properties"]}
    assert props["birthDate"]["matchable"] is True
    # firstName feeds name reconstruction but is non-matchable — still returned,
    # since the rule for callers is "send every property you have".
    assert props["firstName"]["matchable"] is False
    assert "firstName" in props


def test_describe_schema_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError):
        introspect.describe_schema("NotARealSchema")


def test_topic_values_anchors() -> None:
    topics = introspect.topic_values()
    assert "sanction" in topics
    assert "role.pep" in topics
    assert len(topics) >= 10


def test_country_values_anchors() -> None:
    countries = introspect.country_values()
    assert "us" in countries
    assert len(countries) >= 100


def test_gender_values() -> None:
    assert set(introspect.gender_values()) >= {"male", "female"}
