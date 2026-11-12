"""Small helpers shared across the package's projections."""

from typing import Any


def compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is empty (None / "" / [] / {}); bools and zeros are kept."""
    return {k: v for k, v in record.items() if v is not None and v != "" and v != [] and v != {}}
