"""Tests for the MCP server as an OAuth2 resource server.

Runs the streamable-HTTP app in-process with the OAuth2 provider, signs access
tokens with a local RSA key, and answers the tools' API calls with an
``httpx.MockTransport``, so no network is involved.
"""

from typing import Any

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from yente_client.async_client import AsyncClient
from yente_client.mcp import server

ISSUER = "https://issuer.test"
PUBLIC_URL = "https://mcp.test"
RESOURCE = f"{PUBLIC_URL}/mcp"
_FETCH = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "fetch_entity_by_id", "arguments": {"entity_id": "Q7747"}},
}


@pytest.fixture
def key_pair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def downstream(monkeypatch: pytest.MonkeyPatch, key_pair: RSAKeyPair) -> list[httpx.Request]:
    """Put the server in OAuth2 mode; collect the API requests its tools make."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"detail": "Not found"})

    def local_jwt_verifier(**kwargs: Any) -> JWTVerifier:
        # Check against the local key in place of the issuer's JWKS.
        kwargs.pop("jwks_uri")
        return JWTVerifier(public_key=key_pair.public_key, **kwargs)

    monkeypatch.setattr(server, "JWTVerifier", local_jwt_verifier)
    monkeypatch.setenv("YENTE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("YENTE_MCP_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setattr(server, "API_KEY", "server-key")
    monkeypatch.setattr(
        server,
        "_base_client",
        AsyncClient(base_url="https://yente.test", transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(server.mcp, "auth", server._auth())
    return requests


async def post(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL) as client,
    ):
        if path != "/mcp":
            return await client.get(path)
        return await client.post(
            path,
            headers={"Accept": "application/json, text/event-stream", **(headers or {})},
            json=_FETCH,
        )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_metadata_names_issuer_and_resource(downstream: list[httpx.Request]) -> None:
    response = await post("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == RESOURCE
    assert metadata["authorization_servers"] == [f"{ISSUER}/"]


async def test_request_without_credential_gets_401(downstream: list[httpx.Request]) -> None:
    response = await post("/mcp")
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert downstream == []


async def test_access_token_is_forwarded(
    downstream: list[httpx.Request], key_pair: RSAKeyPair
) -> None:
    token = key_pair.create_token(issuer=ISSUER, audience=RESOURCE)
    response = await post("/mcp", bearer(token))
    assert response.status_code == 200, response.text
    assert downstream[0].headers["authorization"] == f"ApiKey {token}"


async def test_access_token_for_other_resource_gets_401(
    downstream: list[httpx.Request], key_pair: RSAKeyPair
) -> None:
    token = key_pair.create_token(issuer=ISSUER, audience="https://other.test/mcp")
    response = await post("/mcp", bearer(token))
    assert response.status_code == 401
    assert downstream == []


async def test_access_token_without_audience_gets_401(
    downstream: list[httpx.Request], key_pair: RSAKeyPair
) -> None:
    token = key_pair.create_token(issuer=ISSUER)
    response = await post("/mcp", bearer(token))
    assert response.status_code == 401


async def test_access_token_from_other_issuer_gets_401(
    downstream: list[httpx.Request], key_pair: RSAKeyPair
) -> None:
    token = key_pair.create_token(issuer="https://other-issuer.test", audience=RESOURCE)
    response = await post("/mcp", bearer(token))
    assert response.status_code == 401


async def test_expired_access_token_gets_401(
    downstream: list[httpx.Request], key_pair: RSAKeyPair
) -> None:
    token = key_pair.create_token(issuer=ISSUER, audience=RESOURCE, expires_in_seconds=-60)
    response = await post("/mcp", bearer(token))
    assert response.status_code == 401


async def test_access_token_with_bad_signature_gets_401(
    downstream: list[httpx.Request],
) -> None:
    token = RSAKeyPair.generate().create_token(issuer=ISSUER, audience=RESOURCE)
    response = await post("/mcp", bearer(token))
    assert response.status_code == 401


async def test_api_key_gets_401(downstream: list[httpx.Request]) -> None:
    response = await post("/mcp", bearer("caller-key"))
    assert response.status_code == 401
    assert downstream == []


def test_no_issuer_means_no_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YENTE_MCP_OAUTH_ISSUER", raising=False)
    assert server._auth() is None


def test_issuer_without_public_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YENTE_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.delenv("YENTE_MCP_PUBLIC_URL", raising=False)
    with pytest.raises(ValueError, match="YENTE_MCP_PUBLIC_URL"):
        server._auth()
