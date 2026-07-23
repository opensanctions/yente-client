"""Shared base for every per-schema entity input class.

Per-schema subclasses (``Person``, ``Company``, …) live in ``_generated.py`` and
declare their FtM properties as ``list[str]`` fields. The ``schema_`` ``ClassVar``
is the discriminator; the wire-format JSON uses the key ``"schema"``.
"""

from typing import Any, ClassVar, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationInfo

PropertyValue: TypeAlias = str | list[str]


def _ensure_list(value: Any) -> Any:
    """Coerce a property value to a list.

    Single string → one-element list; list → unchanged; ``None`` → empty
    list, meaning "unset": callers mapping optional source fields (a CSV
    cell, a nullable database column) can pass them through without guards,
    and ``to_payload()`` omits the empty list from the wire payload.
    Anything else raises; ints, dicts, and other types are not accepted as
    property values.

    The field annotations stay ``list[str]`` — the canonical shape — on
    purpose; this validator widens what is accepted, not what is declared.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    raise ValueError(f"expected str or list[str], got {type(value).__name__}")


def _coerce_property(value: Any, info: ValidationInfo) -> Any:
    """Pydantic ``field_validator(*, mode="before")`` body used by subclasses.

    Applies ``_ensure_list`` to every field *except* the inherited ``id`` field.
    Subclasses attach this validator with ``field_validator("*", mode="before")``
    so it runs for every property without enumerating each name.
    """
    if info.field_name == "id":
        return value
    return _ensure_list(value)


class _EntityBase(BaseModel):
    """Shared base for every per-schema input class.

    Subclasses declare ``schema_`` as a ``ClassVar[Literal["..."]]`` and add
    one ``list[str]`` field per FtM property (own + inherited, flattened).
    They also attach a wildcard ``field_validator`` that runs ``_coerce_property``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str | None = None
    schema_: ClassVar[str]

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the ``/match`` wire format.

        Returns ``{"schema": ..., "properties": {...}}`` with optional ``id``.
        Empty property lists are omitted from ``properties`` so the wire
        payload only carries fields the caller actually set.
        """
        props = {
            name: value
            for name, value in self.model_dump(exclude={"id"}, by_alias=True).items()
            if value
        }
        payload: dict[str, Any] = {"schema": self.schema_, "properties": props}
        if self.id is not None:
            payload["id"] = self.id
        return payload


EntityInput: TypeAlias = _EntityBase
"""Public type alias for any input entity. Any per-schema class (Person,
Company, ...) is an ``EntityInput``."""
