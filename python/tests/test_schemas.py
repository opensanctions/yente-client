import pytest

from yente_client.schemas import (
    describe_schema,
    has_schema,
    is_a,
    is_deprecated,
    iter_properties,
    model,
    schema_index,
    schema_properties,
)


def test_model_loaded() -> None:
    assert "schemata" in model
    assert "types" in model
    assert "Person" in model["schemata"]
    assert "topic" in model["types"]


def test_person_property_type() -> None:
    assert model["schemata"]["Person"]["properties"]["birthDate"]["type"] == "date"


def test_topic_enum_has_sanction() -> None:
    assert "sanction" in model["types"]["topic"]["values"]


def test_has_schema() -> None:
    assert has_schema("Person") is True
    assert has_schema("Company") is True
    assert has_schema("Thing") is True
    assert has_schema("NotARealSchema") is False


def test_is_a_inheritance() -> None:
    assert is_a("Person", "Thing") is True
    assert is_a("Person", "LegalEntity") is True
    assert is_a("LegalEntity", "Thing") is True
    assert is_a("Address", "Thing") is True


def test_is_a_reflexive() -> None:
    assert is_a("Person", "Person") is True


def test_is_a_negative() -> None:
    assert is_a("Address", "LegalEntity") is False
    assert is_a("Person", "Company") is False


def test_is_a_unknown_schema_raises() -> None:
    with pytest.raises(KeyError):
        is_a("NotARealSchema", "Thing")


def test_iter_properties_flattens_inheritance() -> None:
    props = set(iter_properties("Person"))
    # Person's own:
    assert "firstName" in props
    assert "lastName" in props
    assert "birthDate" in props
    # Inherited from LegalEntity:
    assert "name" in props
    assert "jurisdiction" in props
    # Inherited from Thing:
    assert "topics" in props


def test_iter_properties_unique() -> None:
    props = list(iter_properties("Person"))
    assert len(props) == len(set(props)), "iter_properties yielded duplicates"


def test_iter_properties_unknown_raises() -> None:
    with pytest.raises(KeyError):
        list(iter_properties("NotARealSchema"))


def test_is_deprecated_true_own_property() -> None:
    assert is_deprecated("Person", "secondName") is True


def test_is_deprecated_false_active_property() -> None:
    assert is_deprecated("Person", "firstName") is False
    assert is_deprecated("Person", "birthDate") is False


def test_is_deprecated_inherited_deprecation() -> None:
    # LegalEntity.parent is deprecated; Person inherits it transitively.
    assert is_deprecated("Person", "parent") is True


def test_is_deprecated_unknown_schema_raises() -> None:
    with pytest.raises(KeyError):
        is_deprecated("NotARealSchema", "anything")


def test_is_deprecated_unknown_property_raises() -> None:
    with pytest.raises(KeyError):
        is_deprecated("Person", "noSuchProperty")


# ---------- canonical model projection (shared by MCP + CLI) ----------


def test_describe_schema_keeps_signal_drops_cruft() -> None:
    person = describe_schema("Person")
    assert person["name"] == "Person"
    assert person["matchable"] is True
    assert "LegalEntity" in person["extends"]
    assert person["properties"]
    # structural cruft dropped
    assert "label" not in person
    assert "plural" not in person
    assert "schemata" not in person  # flattened closure dropped; extends covers it


def test_describe_schema_property_field_policy() -> None:
    props = {p["name"]: p for p in describe_schema("Person")["properties"]}
    bd = props["birthDate"]
    assert bd["type"] == "date"
    assert bd["matchable"] is True
    for dropped in ("maxLength", "qname", "deprecated", "stub", "hidden", "format"):
        assert dropped not in bd


def test_schema_properties_excludes_stubs() -> None:
    names = {p["name"] for p in schema_properties("Person")}
    assert "birthDate" in names
    assert "images" not in names  # stub (reverse edge)


def test_property_matchable_resolves_type_default() -> None:
    props = {p["name"]: p for p in schema_properties("Person")}
    assert props["birthDate"]["matchable"] is True
    assert props["citizenship"]["matchable"] is True  # via the country type
    assert props["firstName"]["matchable"] is False


def test_empty_values_are_omitted() -> None:
    props = {p["name"]: p for p in schema_properties("Person")}
    # birthDate has no description and isn't entity-typed -> those keys absent
    assert "range" not in props["birthDate"]
    assert "reverse" not in props["birthDate"]
    # an entity-typed property keeps range/reverse
    owner = {p["name"]: p for p in schema_properties("Ownership")}["owner"]
    assert owner["range"] == "LegalEntity"
    assert owner["reverse"] == "ownershipOwner"


def test_schema_index_matchable_only_is_summaries() -> None:
    idx = schema_index(matchable_only=True)
    names = {s["name"] for s in idx}
    assert "Person" in names
    assert all("properties" not in s for s in idx)  # summaries, no property list
    assert len(schema_index()) > len(idx)  # full index lists more
