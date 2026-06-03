"""Tests for the synchronous Client: constructor, User-Agent, error mapping."""

import re

import httpx
import pytest

from yente_client.client import Client
from yente_client.exceptions import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TransportError,
)

UA_RE = re.compile(r"^yente-client/[^\s]+ \(([^)]+)\)$")


# ---------- User-Agent ----------


def test_user_agent_without_app_name() -> None:
    with Client(api_key="test") as c:
        ua = c.user_agent
    m = UA_RE.match(ua)
    assert m is not None, ua
    parts = [p.strip() for p in m.group(1).split(";")]
    assert any(p.startswith("python/") for p in parts)
    assert any(p.startswith("httpx/") for p in parts)
    # No app name slot.
    assert all(p.startswith(("python/", "httpx/")) for p in parts)


def test_user_agent_with_app_name() -> None:
    with Client(api_key="test", app_name="MyScreeningApp") as c:
        ua = c.user_agent
    m = UA_RE.match(ua)
    assert m is not None, ua
    parts = [p.strip() for p in m.group(1).split(";")]
    assert parts[0] == "MyScreeningApp"


def test_user_agent_override_skips_assembly() -> None:
    with Client(api_key="test", user_agent="totally/custom (free-form)") as c:
        assert c.user_agent == "totally/custom (free-form)"


def test_user_agent_override_ignores_app_name() -> None:
    # When user_agent is set, app_name is silently ignored.
    with Client(api_key="test", user_agent="x/y", app_name="DoesNotAppear") as c:
        assert "DoesNotAppear" not in c.user_agent


# ---------- app_name validation ----------


@pytest.mark.parametrize(
    "bad",
    ["", "My App", "(MyApp)", "My;App", "App\n", "App\t"],
)
def test_invalid_app_name_raises(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        Client(app_name=bad)


@pytest.mark.parametrize(
    "good",
    ["MyApp", "my-app", "my.app", "my_app", "MyOrg/MyApp", "App2"],
)
def test_valid_app_name_accepted(good: str) -> None:
    with Client(api_key="test", app_name=good) as c:
        assert good in c.user_agent


# ---------- Auth header ----------


def test_api_key_sets_authorization_header() -> None:
    with Client(api_key="secret-key") as c:
        assert c._http.headers["Authorization"] == "ApiKey secret-key"


def test_missing_api_key_warns_on_opensanctions_url() -> None:
    with pytest.warns(UserWarning, match="api_key"):
        Client().close()


def test_missing_api_key_warns_on_opensanctions_test_url() -> None:
    with pytest.warns(UserWarning, match="api_key"):
        Client(base_url="https://api.test.opensanctions.org").close()


def test_missing_api_key_does_not_warn_on_yente_instance() -> None:
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        # No warning expected — `error` mode would raise if one fired.
        Client(base_url="http://yente.internal:8000").close()


def test_caller_headers_merged_but_auth_wins() -> None:
    with Client(
        api_key="secret",
        headers={"X-Custom": "value", "Authorization": "Bearer wrong"},
    ) as c:
        # Caller's X-Custom passes through; Authorization gets overwritten.
        assert c._http.headers["X-Custom"] == "value"
        assert c._http.headers["Authorization"] == "ApiKey secret"


def test_caller_cannot_override_user_agent_via_headers() -> None:
    with Client(api_key="test", headers={"User-Agent": "fake-ua"}) as c:
        assert c.user_agent.startswith("yente-client/")


# ---------- HTTP / error mapping ----------


def _client_with_handler(handler):
    transport = httpx.MockTransport(handler)
    return Client(api_key="test", transport=transport)


def test_200_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with _client_with_handler(handler) as c:
        assert c._request("GET", "/healthz") == {"status": "ok"}


def test_400_maps_to_bad_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad schema"})

    with _client_with_handler(handler) as c:
        with pytest.raises(BadRequestError) as exc:
            c._request("GET", "/x")
        assert exc.value.status_code == 400
        assert "bad schema" in exc.value.detail


def test_401_and_403_map_to_authentication() -> None:
    for status in (401, 403):

        def handler(request: httpx.Request, _s: int = status) -> httpx.Response:
            return httpx.Response(_s, json={"detail": "nope"})

        with _client_with_handler(handler) as c:
            with pytest.raises(AuthenticationError) as exc:
                c._request("GET", "/x")
            assert exc.value.status_code == status


def test_404_maps_to_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Entity not found"})

    with _client_with_handler(handler) as c, pytest.raises(NotFoundError):
        c._request("GET", "/x")


def test_429_maps_to_rate_limit_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "12"})

    with _client_with_handler(handler) as c:
        with pytest.raises(RateLimitError) as exc:
            c._request("GET", "/x")
        assert exc.value.retry_after == 12.0


def test_429_without_retry_after_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"})

    with _client_with_handler(handler) as c:
        with pytest.raises(RateLimitError) as exc:
            c._request("GET", "/x")
        assert exc.value.retry_after is None


def test_500_maps_to_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    with _client_with_handler(handler) as c:
        with pytest.raises(ServerError) as exc:
            c._request("GET", "/x")
        assert exc.value.status_code == 503


def test_other_status_maps_to_generic_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"detail": "teapot"})

    with _client_with_handler(handler) as c:
        with pytest.raises(APIError) as exc:
            c._request("GET", "/x")
        # Not a more-specific subclass:
        assert type(exc.value) is APIError
        assert exc.value.status_code == 418


def test_pydantic_style_detail_list_is_flattened() -> None:
    detail = [
        {"loc": ["body", "queries"], "msg": "field required", "type": "value_error"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": detail})

    with _client_with_handler(handler) as c:
        with pytest.raises(BadRequestError) as exc:
            c._request("GET", "/x")
        assert "body.queries" in exc.value.detail
        assert "field required" in exc.value.detail


def test_transport_error_wraps_httpx_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client_with_handler(handler) as c, pytest.raises(TransportError):
        c._request("GET", "/x")


def test_context_manager_closes_underlying_http() -> None:
    c = Client(api_key="x")
    assert not c._http.is_closed
    with c:
        pass
    assert c._http.is_closed


# ---------- base_url handling ----------


def test_base_url_trailing_slash_stripped() -> None:
    with Client(base_url="https://api.test.opensanctions.org/", api_key="x") as c:
        # httpx normalises but we strip on entry to keep equality predictable.
        assert str(c._http.base_url).rstrip("/") == "https://api.test.opensanctions.org"
