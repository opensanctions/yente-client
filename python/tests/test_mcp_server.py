"""Tests for the MCP tool adapters and for how their calls reach the API.

Tools run through FastMCP, or the streamable-HTTP app in-process, and their API
calls are answered by an ``httpx.MockTransport``, so no network is involved.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client

from yente_client import env
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


async def test_empty_screen_legend_glosses_only_the_query(
    use_api: None, load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    out = await _call("match_entity", {"schema": "Person", "properties": {"name": ["Nobody"]}})
    assert out["results"] == []
    assert "near_misses" not in out
    assert "match_topics" not in out
    assert "as_of" not in out["checked"]
    assert out["legend"].keys() - {"links"} == {"as_of", "properties"}
    updated = next(d for d in load_fixture("catalog")["datasets"] if d["name"] == "default")
    assert out["legend"]["as_of"].startswith(updated["updated_at"][:10])


def _use_match_response(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    body: dict[str, Any],
    catalog: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/match/"):
            return httpx.Response(200, json=body)
        if request.url.path == "/catalog":
            return httpx.Response(200, json=catalog)
        return httpx.Response(404, json={"detail": "not mocked"})

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)


async def test_match_topics_gather_only_matching_candidates(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    body = load_fixture("match_high_score")
    response = body["responses"]["q"]
    near = json.loads(json.dumps(response["results"][0]))
    near.update(id="NK-other", score=0.5, match=False)
    near["properties"]["topics"] = ["role.pep"]
    response["results"].append(near)
    _use_match_response(monkeypatch, make_async_client, body, load_fixture("catalog"))

    out = await _call("match_entity", {"schema": "Person", "properties": {"name": ["Zakharov"]}})
    assert [r["id"] for r in out["results"]] == ["NK-aU5ybkbRFJucf8YMwsJvDw", "NK-other"]
    assert out["match_topics"] == {"sanction": "Sanctioned"}


async def test_match_names_query_and_suggested_properties(
    use_api: None,
) -> None:
    out = await _call(
        "match_entity",
        {"schema": "Person", "properties": {"name": ["Jane Doe"], "birthDate": []}},
    )
    assert out["query_properties"] == ["name"]
    assert out["query_suggested_properties"] == ["nationality", "birthDate"]
    assert {"name", "nationality", "birthDate"} <= out["legend"]["properties"].keys()


async def test_match_with_every_featured_property_suggests_nothing(use_api: None) -> None:
    props = {"name": ["Jane Doe"], "nationality": ["gb"], "birthDate": ["1975"]}
    out = await _call("match_entity", {"schema": "Person", "properties": props})
    assert out["query_properties"] == ["birthDate", "name", "nationality"]
    assert "query_suggested_properties" not in out


async def test_search_legend_dates_the_server_dataset(
    monkeypatch: pytest.MonkeyPatch,
    make_async_client: Callable[..., AsyncClient],
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog":
            return httpx.Response(200, json=load_fixture("catalog"))
        paths.append(request.url.path)
        return httpx.Response(200, json=load_fixture("search_basic"))

    client = make_async_client(handler=handler)
    monkeypatch.setattr(server, "_resolve_client", lambda: client)
    out = await _call("search_entities", {"q": "Zacharov"})
    assert out["legend"]["as_of"]

    # A dataset the catalog doesn't list has no update time to report.
    monkeypatch.setattr(server, "DATASET", "other")
    out = await _call("search_entities", {"q": "Zacharov", "dataset": "other"})
    assert paths == ["/search/default", "/search/other"]
    assert "as_of" not in out["legend"]


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
    assert first["abridged"] == f'fetch_entity_by_id(entity_id="{first["id"]}")'
    assert first["property_count"] == 2  # firstName, lastName; topics sit inline
    assert "properties" not in first


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


async def test_tool_descriptions_state_the_page_sizes() -> None:
    # FastMCP moves a docstring's Args section into the parameter schema.
    tools = {
        tool.name: (tool.description or "") + json.dumps(tool.parameters)
        for tool in await server.mcp.list_tools()
    }
    for name in ("search_entities", "fetch_entity_relations", "fetch_entity_statements"):
        assert f"default {server._PAGE_SIZE}" in tools[name], name
        assert f"at most {server._MAX_PAGE_SIZE}" in tools[name], name
    assert f"at most {server._MAX_PAGE_SIZE}" in tools["match_entity"]


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


async def test_server_info_carries_title_and_description() -> None:
    async with Client(server.mcp) as client:
        info = client.server_info
    assert info.title == env.mcp_title()
    assert info.description == env.mcp_description()
