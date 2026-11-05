"""Tests for how the MCP server's tools reach the API.

Runs the streamable-HTTP app in-process and answers the tools' API calls with an
``httpx.MockTransport``, so no network is involved.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from yente_client.mcp import auth, server


@pytest.fixture
def downstream() -> Iterator[list[httpx.Request]]:
    """Point the tools at a mock API and collect the requests it receives."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"detail": "Not found"})

    previous = (server.BASE_URL, server._transport)
    server.configure(base_url="https://yente.test", transport=httpx.MockTransport(handler))
    auth._CLIENTS.clear()
    yield requests
    server.configure(base_url=previous[0], transport=previous[1])
    auth._CLIENTS.clear()


async def call_tool(headers: dict[str, str], name: str, arguments: dict[str, Any]) -> None:
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", **headers},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text


async def test_tool_forwards_bearer_token(downstream: list[httpx.Request]) -> None:
    await call_tool(
        {"Authorization": "Bearer caller-key"}, "fetch_entity_by_id", {"entity_id": "Q7747"}
    )
    assert downstream, "the tool made no API call"
    request = downstream[0]
    assert request.url.host == "yente.test"
    assert request.headers["authorization"] == "ApiKey caller-key"


async def test_tool_without_bearer_token_uses_server_key(
    downstream: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "API_KEY", "server-key")
    await call_tool({}, "fetch_entity_by_id", {"entity_id": "Q7747"})
    assert downstream[0].headers["authorization"] == "ApiKey server-key"


def test_client_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_MAX_CLIENTS", 2)
    auth._CLIENTS.clear()
    first = auth.client_for("a", "https://yente.test")
    auth.client_for("b", "https://yente.test")
    # Using "a" again makes "b" the least recently used client.
    assert auth.client_for("a", "https://yente.test") is first
    auth.client_for("c", "https://yente.test")
    assert set(auth._CLIENTS) == {("a", "https://yente.test"), ("c", "https://yente.test")}
    auth._CLIENTS.clear()
