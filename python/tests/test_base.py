"""Tests for the hand-written entity base machinery.

Uses a stub subclass that mirrors what the codegen will eventually emit, so we
can verify the shared validator + ``to_payload`` behaviour without yet having
the generated per-schema classes.
"""

from typing import ClassVar, Literal

import pytest
from pydantic import Field, ValidationError, field_validator

from yente_client.entities._base import _coerce_property, _ensure_list, _EntityBase


class _StubPerson(_EntityBase):
    """A minimal stand-in for the codegen-emitted ``Person`` class."""

    schema_: ClassVar[Literal["Person"]] = "Person"

    name: list[str] = Field(default_factory=list)
    firstName: list[str] = Field(default_factory=list)
    lastName: list[str] = Field(default_factory=list)

    _coerce = field_validator("*", mode="before")(_coerce_property)


def test_str_coerces_to_singleton_list() -> None:
    p = _StubPerson(firstName="Aleksandr")
    assert p.firstName == ["Aleksandr"]


def test_list_passes_through() -> None:
    p = _StubPerson(firstName=["A", "B"])
    assert p.firstName == ["A", "B"]


def test_defaults_to_empty_list() -> None:
    p = _StubPerson()
    assert p.firstName == []
    assert p.lastName == []


def test_unknown_property_raises() -> None:
    with pytest.raises(ValidationError):
        _StubPerson(unknown="X")


def test_snake_case_alias_rejected() -> None:
    # We use camelCase canonically; snake_case kwargs should fail (no aliases).
    with pytest.raises(ValidationError):
        _StubPerson(first_name="X")


def test_non_string_non_list_raises() -> None:
    with pytest.raises(ValidationError):
        _StubPerson(firstName=42)
    with pytest.raises(ValidationError):
        _StubPerson(firstName={"key": "value"})


def test_to_payload_basic() -> None:
    p = _StubPerson(firstName="Aleksandr", lastName="Zacharov")
    payload = p.to_payload()
    assert payload == {
        "schema": "Person",
        "properties": {
            "firstName": ["Aleksandr"],
            "lastName": ["Zacharov"],
        },
    }


def test_to_payload_drops_empty_property_lists() -> None:
    p = _StubPerson(firstName="X")
    payload = p.to_payload()
    assert payload["properties"] == {"firstName": ["X"]}
    assert "lastName" not in payload["properties"]
    assert "name" not in payload["properties"]


def test_to_payload_with_id() -> None:
    p = _StubPerson(id="Q123", firstName="X")
    payload = p.to_payload()
    assert payload["id"] == "Q123"


def test_to_payload_omits_unset_id() -> None:
    p = _StubPerson(firstName="X")
    payload = p.to_payload()
    assert "id" not in payload


def test_schema_classvar() -> None:
    assert _StubPerson.schema_ == "Person"
    # Confirm it's a ClassVar (not an instance field): mutating on instance has no effect on class.
    p = _StubPerson(firstName="X")
    assert p.schema_ == "Person"


def test_ensure_list_directly() -> None:
    assert _ensure_list("a") == ["a"]
    assert _ensure_list(["a", "b"]) == ["a", "b"]
    assert _ensure_list(None) == []  # None means "unset"
    with pytest.raises(ValueError):
        _ensure_list(42)
