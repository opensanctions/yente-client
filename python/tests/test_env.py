"""Tests for the shared environment-variable helper."""

import pytest

from yente_client import env


def test_var_names_are_stable() -> None:
    assert env.API_KEY_VAR == "OPENSANCTIONS_API_KEY"
    assert env.BASE_URL_VAR == "YENTE_BASE_URL"
    assert env.MCP_NAME_VAR == "YENTE_MCP_NAME"
    assert env.MCP_TITLE_VAR == "YENTE_MCP_TITLE"
    assert env.MCP_DESCRIPTION_VAR == "YENTE_MCP_DESCRIPTION"
    assert env.MCP_INSTRUCTIONS_VAR == "YENTE_MCP_INSTRUCTIONS"
    assert env.MCP_DATASET_VAR == "YENTE_MCP_DATASET"
    assert env.MCP_TRANSPORT_VAR == "YENTE_MCP_TRANSPORT"
    assert env.MCP_HOST_VAR == "YENTE_MCP_HOST"
    assert env.MCP_PORT_VAR == "YENTE_MCP_PORT"


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


def test_mcp_title_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_TITLE", "ACME Screening")
    assert env.mcp_title() == "ACME Screening"


def test_mcp_title_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_TITLE", raising=False)
    assert env.mcp_title() == "Yente"


def test_mcp_description_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_DESCRIPTION", "ACME's watchlist screening.")
    assert env.mcp_description() == "ACME's watchlist screening."


def test_mcp_description_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_DESCRIPTION", raising=False)
    assert env.mcp_description() == env.DEFAULT_MCP_DESCRIPTION


def test_mcp_dataset_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_DATASET", "sanctions")
    assert env.mcp_dataset() == "sanctions"


def test_mcp_dataset_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_DATASET", raising=False)
    assert env.mcp_dataset() == "default"


def test_mcp_instructions_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_INSTRUCTIONS", "Custom branded blurb.")
    assert env.mcp_instructions() == "Custom branded blurb."


def test_mcp_instructions_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_INSTRUCTIONS", raising=False)
    assert env.mcp_instructions() == env.DEFAULT_MCP_INSTRUCTIONS


def test_mcp_serving_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("YENTE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("YENTE_MCP_PORT", "9000")
    assert (env.mcp_transport(), env.mcp_host(), env.mcp_port()) == ("stdio", "0.0.0.0", 9000)


def test_mcp_serving_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("YENTE_MCP_TRANSPORT", "YENTE_MCP_HOST", "YENTE_MCP_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert (env.mcp_transport(), env.mcp_host(), env.mcp_port()) == ("http", "127.0.0.1", 8000)
