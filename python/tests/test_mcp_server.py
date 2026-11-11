"""Tests for how the MCP server's tools reach the API.

Runs the streamable-HTTP app in-process and answers the tools' API calls with an
``httpx.MockTransport``, so no network is involved.
"""

from typing import Any

import httpx
import pytest

from yente_client.mcp.auth import ClientCache
from yente_client.mcp.server import create_server


@pytest.fixture
def downstream() -> list[httpx.Request]:
    """Collect the requests that reach the mock API."""
    return []


async def call_tool(
    downstream: list[httpx.Request],
    headers: dict[str, str],
    name: str,
    arguments: dict[str, Any],
    api_key: str | None = None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        downstream.append(request)
        return httpx.Response(404, json={"detail": "Not found"})

    mcp = create_server(
        base_url="https://yente.test",
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
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
        downstream,
        {"Authorization": "Bearer caller-key"},
        "fetch_entity_by_id",
        {"entity_id": "Q7747"},
    )
    assert downstream, "the tool made no API call"
    request = downstream[0]
    assert request.url.host == "yente.test"
    assert request.headers["authorization"] == "ApiKey caller-key"


async def test_tool_without_bearer_token_uses_server_key(
    downstream: list[httpx.Request],
) -> None:
    await call_tool(
        downstream, {}, "fetch_entity_by_id", {"entity_id": "Q7747"}, api_key="server-key"
    )
    assert downstream[0].headers["authorization"] == "ApiKey server-key"


def test_client_cache_is_bounded() -> None:
    cache = ClientCache("https://yente.test", max_size=2)
    first = cache.get("a")
    cache.get("b")
    # Using "a" again makes "b" the least recently used client.
    assert cache.get("a") is first
    cache.get("c")
    assert set(cache._clients) == {"a", "c"}
