"""Tests for the MCP server's bearer-token parsing.

Pure functions — no network, no FastMCP, no HTTP request context.
"""

from yente_client.mcp.auth import parse_bearer


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
