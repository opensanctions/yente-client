"""Tests for the shared environment-variable helper."""

import pytest

from yente_client import env


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
