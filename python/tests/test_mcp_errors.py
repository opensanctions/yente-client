"""Tests for SDK-exception -> tool-error message mapping."""

from yente_client.exceptions import (
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    RateLimitError,
    ServerError,
    TransportError,
)
from yente_client.mcp.errors import describe_error


def test_transport_error_is_never_empty_and_retryable() -> None:
    # httpx timeouts stringify to "" — the message must still be useful.
    msg = describe_error(TransportError(""))
    assert msg
    assert "retryable=true" in msg


def test_4xx_is_not_retryable_and_keeps_detail() -> None:
    msg = describe_error(BadRequestError(400, "unknown schema"))
    assert "status=400" in msg
    assert "retryable=false" in msg
    assert "unknown schema" in msg


def test_auth_error_not_retryable() -> None:
    msg = describe_error(AuthenticationError(401, "No API key provided"))
    assert "status=401" in msg
    assert "retryable=false" in msg


def test_5xx_is_retryable_with_default_detail() -> None:
    msg = describe_error(ServerError(502, ""))
    assert "status=502" in msg
    assert "retryable=true" in msg
    assert "no detail" in msg


def test_rate_limit_is_retryable_with_retry_after() -> None:
    msg = describe_error(RateLimitError(429, "slow down", retry_after=5.0))
    assert "status=429" in msg
    assert "retryable=true" in msg
    assert "retry_after=5.0s" in msg


def test_configuration_error_has_message() -> None:
    msg = describe_error(ConfigurationError("bad app_name"))
    assert "bad app_name" in msg
