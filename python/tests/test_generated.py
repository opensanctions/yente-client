"""Tests for the codegen-emitted per-schema entity classes."""

import pytest
from pydantic import ValidationError

from yente_client.entities import Company, Email, Person
from yente_client.entities._base import _EntityBase
from yente_client.schemas._literals import Gender, PropertyType, Schema, Topic


def test_person_construction_and_payload() -> None:
    p = Person(firstName="Aleksandr", lastName="Zacharov", birthDate="1965")
    assert p.firstName == ["Aleksandr"]
    assert p.lastName == ["Zacharov"]
    assert p.birthDate == ["1965"]
    payload = p.to_payload()
    assert payload == {
        "schema": "Person",
        "properties": {
            "birthDate": ["1965"],
            "firstName": ["Aleksandr"],
            "lastName": ["Zacharov"],
        },
    }


def test_person_inherited_properties_present() -> None:
    # `name` and `country` come from LegalEntity; `topics` from Thing.
    p = Person(name=["Aleksandr Zacharov"], country="ru", topics=["sanction"])
    assert p.name == ["Aleksandr Zacharov"]
    assert p.country == ["ru"]
    assert p.topics == ["sanction"]


def test_person_str_to_list_coercion() -> None:
    p = Person(firstName="X")
    assert p.firstName == ["X"]


def test_person_none_means_unset() -> None:
    # Callers mapping optional sources (CSV cells, nullable columns) pass
    # None through without guards; the property is omitted from the payload.
    p = Person(name="X", birthDate=None)
    assert p.birthDate == []
    assert "birthDate" not in p.to_payload()["properties"]


def test_person_none_id_stays_none() -> None:
    # The id field is not a property; the list coercion must not touch it.
    p = Person(name="X", id=None)
    assert p.id is None
    assert "id" not in p.to_payload()


def test_person_unknown_property_rejected() -> None:
    with pytest.raises(ValidationError):
        Person(notAProperty="X")


def test_person_snake_case_rejected() -> None:
    # camelCase is canonical; snake_case is not aliased to anything.
    with pytest.raises(ValidationError):
        Person(first_name="X")


def test_person_classvar_schema() -> None:
    assert Person.schema_ == "Person"
    assert Person(firstName="X").schema_ == "Person"


def test_company_distinct_from_person() -> None:
    c = Company(name="Acme LLC", jurisdiction="us")
    assert c.schema_ == "Company"
    assert c.to_payload()["schema"] == "Company"
    # Property only on Company, not Person:
    assert "jurisdiction" in c.to_payload()["properties"]


def test_email_keyword_escape_via_alias() -> None:
    # `from` is a Python keyword so the field is `from_` with alias="from".
    e = Email(from_="alice@x.com", to="bob@y.com")
    assert e.from_ == ["alice@x.com"]
    payload = e.to_payload()
    # On the wire we send the alias.
    assert payload["properties"]["from"] == ["alice@x.com"]
    assert "from_" not in payload["properties"]


def test_email_keyword_escape_via_alias_dict() -> None:
    # Also accept the wire-format alias via model_validate.
    e = Email.model_validate({"from": ["alice@x.com"]})
    assert e.from_ == ["alice@x.com"]


def test_isinstance_entity_base() -> None:
    assert isinstance(Person(firstName="X"), _EntityBase)
    assert isinstance(Company(name="Acme"), _EntityBase)
    assert isinstance(Email(from_="a@b.com"), _EntityBase)


def test_to_payload_drops_empty_properties() -> None:
    # Only fields the caller set should appear; empty defaults are omitted.
    p = Person(firstName="X")
    payload = p.to_payload()
    assert payload["properties"] == {"firstName": ["X"]}


def test_to_payload_with_id() -> None:
    p = Person(id="Q123", firstName="X")
    assert p.to_payload()["id"] == "Q123"


def test_to_payload_omits_unset_id() -> None:
    p = Person(firstName="X")
    assert "id" not in p.to_payload()


def test_schema_literal_includes_core_schemas() -> None:
    # Don't lock in a specific count (FtM model evolves); check known anchors
    # plus a sanity lower bound. `regen_model.py --check` enforces actual drift.
    args = Schema.__args__  # type: ignore[attr-defined]
    assert "Person" in args
    assert "Company" in args
    assert "Email" in args
    assert len(args) > 30


def test_topic_literal_includes_sanction() -> None:
    args = Topic.__args__  # type: ignore[attr-defined]
    assert "sanction" in args
    assert "role.pep" in args


def test_property_type_literal() -> None:
    args = PropertyType.__args__  # type: ignore[attr-defined]
    assert "name" in args
    assert "date" in args
    assert "country" in args
    assert "entity" in args


def test_gender_literal() -> None:
    args = Gender.__args__  # type: ignore[attr-defined]
    assert set(args) == {"female", "male", "other"}


def test_deprecated_field_present_and_usable() -> None:
    """Deprecated fields are still emitted on the input class — they're real
    fields, just flagged. We had a regression where a template whitespace bug
    pulled the DEPRECATED comment onto the same line as the field, hiding the
    field inside a Python comment. This test guards against that."""
    # Person.secondName is deprecated own-property.
    assert "secondName" in Person.model_fields
    p = Person(secondName="Vyacheslavovich")
    assert p.secondName == ["Vyacheslavovich"]
    assert p.to_payload()["properties"]["secondName"] == ["Vyacheslavovich"]


def test_deprecated_inherited_field_present_and_usable() -> None:
    """Same regression check but on an inherited deprecated property:
    Person.parent comes from LegalEntity and is deprecated."""
    assert "parent" in Person.model_fields
    p = Person(parent="LegalEntityID-123")
    assert p.parent == ["LegalEntityID-123"]


def test_generated_source_has_deprecated_comment() -> None:
    """The DEPRECATED comment should be on its own line above the field —
    not merged into the field declaration."""
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parent
    source = tests_dir.parent / "src" / "yente_client" / "entities" / "_generated.py"
    text = source.read_text()
    # Find the secondName field and look at the preceding line.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "secondName: list[str] = Field(" in line:
            # The preceding non-empty line should be the DEPRECATED comment,
            # alone on its line (not concatenated with the field).
            prev = lines[i - 1].strip()
            assert prev.startswith("# DEPRECATED"), (
                f"expected DEPRECATED comment immediately above secondName, got: {prev!r}"
            )
            assert "secondName" not in prev, (
                f"DEPRECATED comment must be on its own line; got: {prev!r}"
            )
            break
    else:
        raise AssertionError("secondName field not found in generated source")


def test_stub_properties_excluded() -> None:
    """Reverse-side ("stub") entity properties are noise on a query payload;
    we exclude them from the generated input class so users only see fields
    they can meaningfully set."""
    # `images`, `associates`, `familyPerson`, `employers` are all stub=True
    # on Person in model.json — they should NOT be fields on the class.
    assert "images" not in Person.model_fields
    assert "associates" not in Person.model_fields
    assert "familyPerson" not in Person.model_fields
    assert "employers" not in Person.model_fields
    # Forward-direction entity properties (non-stub) remain — `parent` is
    # the one non-stub entity property on Person (inherited from LegalEntity).
    assert "parent" in Person.model_fields
    # Stubs hitting extra="forbid" gives a clear ValidationError.
    with pytest.raises(ValidationError):
        Person(images=["some-id"])
