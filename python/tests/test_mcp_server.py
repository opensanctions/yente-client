"""Tests for how the MCP server's tools reach the API.

Runs the streamable-HTTP app in-process and answers the tools' API calls with an
``httpx.MockTransport``, so no network is involved.
"""

from typing import Any

import httpx
import pytest

from yente_client.async_client import AsyncClient
from yente_client.mcp import server

_FETCH = ("fetch_entity_by_id", {"entity_id": "Q7747"})


async def call_tools(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[dict[str, str], str, dict[str, Any]]],
    api_key: str | None = None,
) -> list[httpx.Request]:
    """Make each ``(headers, tool name, arguments)`` call; return the API requests."""
    downstream: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        downstream.append(request)
        return httpx.Response(404, json={"detail": "Not found"})

    monkeypatch.setattr(server, "API_KEY", api_key)
    monkeypatch.setattr(
        server,
        "_base_client",
        AsyncClient(
            base_url="https://yente.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
        ) as client,
    ):
        for headers, name, arguments in calls:
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
    return downstream


async def test_tool_forwards_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    downstream = await call_tools(monkeypatch, [({"Authorization": "Bearer caller-key"}, *_FETCH)])
    assert downstream, "the tool made no API call"
    assert downstream[0].url.host == "yente.test"
    assert downstream[0].headers["authorization"] == "ApiKey caller-key"


async def test_tool_without_bearer_token_uses_server_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downstream = await call_tools(monkeypatch, [({}, *_FETCH)], api_key="server-key")
    assert downstream[0].headers["authorization"] == "ApiKey server-key"


async def test_each_call_forwards_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    downstream = await call_tools(
        monkeypatch,
        [
            ({"Authorization": "Bearer key-a"}, *_FETCH),
            ({"Authorization": "Bearer key-b"}, *_FETCH),
            ({}, *_FETCH),
        ],
        api_key="server-key",
    )
    assert [r.headers["authorization"] for r in downstream] == [
        "ApiKey key-a",
        "ApiKey key-b",
        "ApiKey server-key",
    ]


async def test_tool_without_any_key_sends_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downstream = await call_tools(monkeypatch, [({}, *_FETCH)])
    assert "authorization" not in downstream[0].headers
