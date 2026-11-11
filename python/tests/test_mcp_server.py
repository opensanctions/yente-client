"""Tests for the MCP tool adapters, driven through FastMCP against a mocked API."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from yente_client.async_client import AsyncClient
from yente_client.mcp import server
from yente_client.mcp._deps import ToolError


@pytest.fixture
def use_api(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    """Route every tool call to a mocked API serving the shared fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/match/"):
            return httpx.Response(200, json=load_fixture("match_zero_results"))
        if request.url.path == "/catalog":
            return httpx.Response(200, json=load_fixture("catalog"))
        return httpx.Response(404, json={"detail": "not mocked"})

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)
    monkeypatch.setattr(server, "_catalog_info_cache", None)


async def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server.mcp.call_tool(name, args)
    content = result[0] if isinstance(result, tuple) else result.content
    return json.loads("".join(getattr(block, "text", "") for block in content))


async def test_empty_screen_reports_as_of_without_a_legend(
    use_api: None, load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    out = await _call("match_entity", {"schema": "Person", "properties": {"name": ["Nobody"]}})
    assert out["results"] == []
    assert "near_misses" not in out
    assert "legend" not in out
    updated = next(d for d in load_fixture("catalog")["datasets"] if d["name"] == "default")
    assert out["checked"]["as_of"].startswith(updated["updated_at"][:10])


async def test_unknown_dataset_suggests_close_names(use_api: None) -> None:
    with pytest.raises(ToolError, match="Unknown dataset 'ofac'. Closest: "):
        await server.mcp.call_tool("describe_dataset", {"name": "ofac"})
