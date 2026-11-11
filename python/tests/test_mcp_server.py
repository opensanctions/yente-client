"""Tests for the MCP tool adapters, driven through FastMCP against a mocked API."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from yente_client.async_client import AsyncClient
from yente_client.mcp import server
from yente_client.mcp._deps import ToolError


@pytest.fixture(autouse=True)
def fresh_caches() -> None:
    """Start every test with empty catalog, program and algorithm caches."""
    server._reset_caches()


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


async def test_page_size_is_capped(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog":
            return httpx.Response(200, json=load_fixture("catalog"))
        seen.append(request.url.params.get("limit"))
        return httpx.Response(200, json=load_fixture("statements"))

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)
    await _call("fetch_entity_statements", {"entity_id": "Q7747", "limit": 500})
    assert seen == ["25"]


async def test_describe_topics_explains_the_codes() -> None:
    out = await _call("describe", {"kind": "topics"})
    assert "sanction.linked" in out["about"]
    assert out["topics"]["role.pep"]


async def test_describe_dataset_needs_a_name() -> None:
    with pytest.raises(ToolError, match="needs a name"):
        await server.mcp.call_tool("describe", {"kind": "dataset"})


async def test_describe_topics_takes_no_name() -> None:
    with pytest.raises(ToolError, match="takes no name"):
        await server.mcp.call_tool("describe", {"kind": "topics", "name": "sanction"})


async def test_results_are_sent_as_text_only() -> None:
    tools = await server.mcp.list_tools()
    assert all(tool.output_schema is None for tool in tools)
    result = await server.mcp.call_tool("describe", {"kind": "topics"})
    assert result.structured_content is None
    assert len(result.content) == 1


async def test_unknown_dataset_suggests_close_names(use_api: None) -> None:
    with pytest.raises(ToolError, match="Unknown dataset 'ofac'. Closest: "):
        await server.mcp.call_tool("describe", {"kind": "dataset", "name": "ofac"})


async def test_describe_unknown_schema_suggests_close_names() -> None:
    with pytest.raises(ToolError, match="Unknown schema 'Persn'. Closest: Person"):
        await _call("describe", {"kind": "schema", "name": "Persn"})


async def test_match_unknown_schema_suggests_close_names() -> None:
    with pytest.raises(ToolError, match="Unknown schema 'Compnay'. Closest: Company"):
        await _call("match_entity", {"schema": "Compnay", "properties": {"name": ["Acme"]}})


async def test_describe_schema_index_is_matchable_only() -> None:
    out = await _call("describe", {"kind": "schema"})
    names = {s["name"] for s in out["schemata"]}
    assert {"Person", "Company"} <= names
    assert "Document" not in names


async def test_describe_countries_lists_codes() -> None:
    out = await _call("describe", {"kind": "countries"})
    assert out["us"]


async def test_search_results_are_abridged(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search/"):
            return httpx.Response(200, json=load_fixture("search_basic"))
        return httpx.Response(404, json={"detail": "not mocked"})

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)
    out = await _call("search_entities", {"q": "Zacharov"})
    first = out["results"][0]
    assert first["abridged"] is True
    assert first["not_shown"] == ["firstName", "lastName"]
    assert "firstName" not in first.get("properties", {})


async def test_search_and_relation_pages_default_to_ten(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    limits: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/catalog":
            return httpx.Response(200, json=load_fixture("catalog"))
        limits[path.split("/")[1]] = request.url.params.get("limit")
        if path.startswith("/search/"):
            return httpx.Response(200, json=load_fixture("search_basic"))
        page = {"results": [], "total": {"value": 0, "relation": "eq"}, "limit": 10, "offset": 0}
        return httpx.Response(200, json=page)

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)
    await _call("search_entities", {"q": "Zacharov"})
    await _call("fetch_entity_relations", {"entity_id": "Q1", "prop": "familyPerson"})
    assert limits == {"search": "10", "entities": "10"}
