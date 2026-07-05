"""Tests for the MCP server's API-key resolution.

Pure functions — no network, no FastMCP, no HTTP request context.
"""

from yente_client.mcp.auth import parse_bearer, resolve_api_key


def test_parse_bearer_extracts_token() -> None:
    assert parse_bearer("Bearer sk-123") == "sk-123"


def test_parse_bearer_is_scheme_case_insensitive() -> None:
    assert parse_bearer("bearer sk-123") == "sk-123"


def test_parse_bearer_trims_whitespace() -> None:
    assert parse_bearer("Bearer  sk-123  ") == "sk-123"


def test_parse_bearer_rejects_non_bearer_and_empty() -> None:
    assert parse_bearer(None) is None
    assert parse_bearer("") is None
    assert parse_bearer("Basic sk-123") is None
    assert parse_bearer("Bearer ") is None


def test_resolve_api_key_prefers_bearer_over_env() -> None:
    assert resolve_api_key("Bearer from-header", "from-env") == "from-header"


def test_resolve_api_key_falls_back_to_env() -> None:
    # No usable bearer token → the server's own key (localhost-against-prod case).
    assert resolve_api_key(None, "from-env") == "from-env"
    assert resolve_api_key("Basic nope", "from-env") == "from-env"


def test_resolve_api_key_none_when_neither_present() -> None:
    assert resolve_api_key(None, None) is None
