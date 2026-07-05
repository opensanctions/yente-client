"""Tests for the shared environment-variable helper."""

import pytest

from yente_client import env


def test_var_names_are_stable() -> None:
    assert env.API_KEY_VAR == "OPENSANCTIONS_API_KEY"
    assert env.BASE_URL_VAR == "YENTE_BASE_URL"
    assert env.MCP_NAME_VAR == "YENTE_MCP_NAME"
    assert env.MCP_INSTRUCTIONS_VAR == "YENTE_MCP_INSTRUCTIONS"


def test_hosted_hosts_cover_prod_and_tlds() -> None:
    assert "api.opensanctions.org" in env.HOSTED_HOSTS
    assert "api.opensanctions.net" in env.HOSTED_HOSTS
    assert "api.opensanctions.com" in env.HOSTED_HOSTS
    assert "api.test.opensanctions.org" in env.HOSTED_HOSTS
    # the default base URL's host is one of them
    assert env.DEFAULT_BASE_URL.split("//", 1)[1] in env.HOSTED_HOSTS


def test_api_key_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "sk-123")
    assert env.api_key() == "sk-123"


def test_api_key_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSANCTIONS_API_KEY", raising=False)
    assert env.api_key() is None


def test_base_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_BASE_URL", "https://yente.example.org")
    assert env.base_url() == "https://yente.example.org"


def test_base_url_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_BASE_URL", raising=False)
    assert env.base_url() == env.DEFAULT_BASE_URL


def test_mcp_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_NAME", "ACME Screening")
    assert env.mcp_name() == "ACME Screening"


def test_mcp_name_defaults_to_yente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_NAME", raising=False)
    assert env.mcp_name() == "yente"


def test_mcp_instructions_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_INSTRUCTIONS", "Custom branded blurb.")
    assert env.mcp_instructions() == "Custom branded blurb."


def test_mcp_instructions_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_INSTRUCTIONS", raising=False)
    assert env.mcp_instructions() == env.DEFAULT_MCP_INSTRUCTIONS
