"""Tests for read-only CLI subcommands (Phase D).

Uses Typer's ``CliRunner`` for invocation and ``respx`` for HTTP mocking.
Each test sets ``--api-key`` and ``--base-url`` flags so the missing-key
warning doesn't fire against the default OpenSanctions API URL.
"""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from yente_client.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


_BASE_URL = "http://test.local"
_BASE_FLAGS = ["--api-key", "test", "--base-url", _BASE_URL]


# ---------- status ----------


def test_status_json(runner, load_fixture) -> None:
    """status hits /healthz, /readyz, /catalog and assembles a summary dict."""
    catalog_payload = load_fixture("catalog")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz" or path == "/readyz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/catalog":
            return httpx.Response(200, json=catalog_payload)
        return httpx.Response(404, json={"detail": "not found"})

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.route().mock(side_effect=handler)
        result = runner.invoke(app, [*_BASE_FLAGS, "status", "-f", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["client"]["version"]
    assert summary["api"]["url"] == _BASE_URL
    assert summary["api"]["auth"] == {"present": True, "key_suffix": "test"}
    assert summary["api"]["liveness"]["status"] == "ok"
    assert summary["api"]["readiness"]["status"] == "ok"
    # Only the load=true entry (`default`) shows up; the other catalog
    # entries are metadata-only.
    assert [d["name"] for d in summary["loaded"]] == ["default"]
    assert summary["loaded"][0]["is_collection"] is True
    assert summary["summary"] == {"total": 1, "current": 1, "stale": 0}


def test_status_table(runner, load_fixture) -> None:
    catalog_payload = load_fixture("catalog")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz" or path == "/readyz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/catalog":
            return httpx.Response(200, json=catalog_payload)
        return httpx.Response(404, json={"detail": "not found"})

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.route().mock(side_effect=handler)
        result = runner.invoke(app, [*_BASE_FLAGS, "status", "-f", "table"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "yente-cli" in result.stdout
    assert "Liveness" in result.stdout
    assert "Readiness" in result.stdout
    # Last 4 of "test" → "test" (the whole key, since len == 4)
    assert "test" in result.stdout
    assert "Loaded datasets" in result.stdout
    # us_ofac_sdn is in the catalog but has load=false → must NOT appear in
    # the loaded section.
    assert "default" in result.stdout


def test_status_masks_api_key(runner, load_fixture) -> None:
    catalog_payload = load_fixture("catalog")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/healthz", "/readyz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=catalog_payload)

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.route().mock(side_effect=handler)
        result = runner.invoke(
            app,
            ["--api-key", "sk-supersecret-9e95", "--base-url", _BASE_URL, "status", "-f", "json"],
        )
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    # Full key must never appear in the output.
    assert "sk-supersecret-9e95" not in result.stdout
    # Last 4 only.
    assert summary["api"]["auth"] == {"present": True, "key_suffix": "9e95"}


def test_status_handles_readyz_failure(runner, load_fixture) -> None:
    """A failing /readyz should not crash status — it just reports the error."""
    catalog_payload = load_fixture("catalog")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/readyz":
            return httpx.Response(503, json={"detail": "index not ready"})
        return httpx.Response(200, json=catalog_payload)

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.route().mock(side_effect=handler)
        result = runner.invoke(app, [*_BASE_FLAGS, "status", "-f", "json"])
    assert result.exit_code == 0  # status itself succeeds even if readyz is unhealthy
    summary = json.loads(result.stdout)
    assert summary["api"]["liveness"]["status"] == "ok"
    assert summary["api"]["readiness"]["status"] == "error"
    assert summary["api"]["readiness"]["code"] == 503


# ---------- datasets ----------


def test_datasets_table(runner, load_fixture) -> None:
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/catalog").mock(return_value=httpx.Response(200, json=load_fixture("catalog")))
        result = runner.invoke(app, [*_BASE_FLAGS, "datasets", "-f", "table"])
    assert result.exit_code == 0
    assert "default" in result.stdout
    assert "us_ofac_sdn" in result.stdout


def test_datasets_json(runner, load_fixture) -> None:
    payload = load_fixture("catalog")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/catalog").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "datasets", "-f", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert "datasets" in parsed
    assert {d["name"] for d in parsed["datasets"]} >= {"default", "us_ofac_sdn"}


# ---------- statements ----------


def test_statements_table(runner, load_fixture) -> None:
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/statements").mock(
            return_value=httpx.Response(200, json=load_fixture("statements"))
        )
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "statements",
                "--canonical-id",
                "NK-aU5ybkbRFJucf8YMwsJvDw",
                "-f",
                "table",
            ],
        )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "John Doe" in result.stdout
    assert "Johnny D" in result.stdout
    assert "us_ofac_sdn" in result.stdout


def test_statements_404_message_points_at_opensanctions_api(runner) -> None:
    """On a yente instance, /statements returns 404 — the CLI shows the rewrapped detail."""
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/statements").mock(
            return_value=httpx.Response(404, json={"detail": "Not Found"}),
        )
        result = runner.invoke(app, [*_BASE_FLAGS, "statements", "--entity-id", "x"])
    assert result.exit_code == 3  # API error
    combined = result.stdout + result.stderr
    assert "OpenSanctions API" in combined


# ---------- algorithms ----------


def test_algorithms_table(runner, load_fixture) -> None:
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/algorithms").mock(
            return_value=httpx.Response(200, json=load_fixture("algorithms"))
        )
        # CliRunner captures into a non-TTY stream — auto would resolve to
        # JSON, so we force table explicitly.
        result = runner.invoke(app, [*_BASE_FLAGS, "algorithms", "-f", "table"])
    assert result.exit_code == 0
    assert "logic-v2" in result.stdout
    assert "name-matcher" in result.stdout
    # The "best" algorithm gets a marker.
    assert "★" in result.stdout


# ---------- fetch ----------


def test_fetch_table_summary(runner, load_fixture) -> None:
    payload = load_fixture("entity_person")
    eid = payload["id"]
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(f"/entities/{eid}").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "fetch", eid, "-f", "table"])
    assert result.exit_code == 0
    assert eid in result.stdout
    assert "Person" in result.stdout
    assert "sanction" in result.stdout


def test_fetch_json(runner, load_fixture) -> None:
    payload = load_fixture("entity_person")
    eid = payload["id"]
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(f"/entities/{eid}").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "fetch", eid, "-f", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["id"] == eid
    assert parsed["schema"] == "Person"


def test_search_table(runner, load_fixture) -> None:
    payload = load_fixture("search_basic")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(url__regex=r".*/search/.*").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "search", "acme", "-f", "table"])
    assert result.exit_code == 0
    assert "Acme" in result.stdout
    assert "Person" in result.stdout or "Company" in result.stdout


def test_search_json(runner, load_fixture) -> None:
    payload = load_fixture("search_basic")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(url__regex=r".*/search/.*").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "search", "acme", "-f", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert "results" in parsed
    assert len(parsed["results"]) == 2


def test_search_empty_results_exits_one(runner, load_fixture) -> None:
    payload = load_fixture("search_no_results")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(url__regex=r".*/search/.*").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(app, [*_BASE_FLAGS, "search", "zzzz", "-f", "json"])
    assert result.exit_code == 1


def test_search_passes_dataset_and_schema_filters(runner, load_fixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("search_no_results"))

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(url__regex=r".*/search/.*").mock(side_effect=handler)
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "search",
                "acme",
                "-d",
                "sanctions",
                "-d",
                "us_ofac_sdn",
                "-s",
                "Company",
                "-t",
                "sanction",
                "-f",
                "json",
            ],
        )
    # Exit 1 because we mocked an empty payload, but the request should have
    # gone out with the right URL and params.
    assert result.exit_code == 1
    assert seen[0].url.path == "/search/sanctions"
    assert seen[0].url.params.get("schema") == "Company"
    assert seen[0].url.params.get("topics") == "sanction"
    assert seen[0].url.params.get_list("include_dataset") == ["us_ofac_sdn"]


# ---------- match ----------


def test_match_table(runner, load_fixture) -> None:
    payload = load_fixture("match_high_score")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(url__regex=r".*/match/.*").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "match",
                "-s",
                "Person",
                "-p",
                "firstName=Aleksandr",
                "-p",
                "lastName=Zacharov",
                "-d",
                "sanctions",
                "-f",
                "table",
            ],
        )
    assert result.exit_code == 0
    assert "0.92" in result.stdout
    assert "ZAKHAROV" in result.stdout


def test_match_json(runner, load_fixture) -> None:
    payload = load_fixture("match_high_score")
    with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(url__regex=r".*/match/.*").mock(return_value=httpx.Response(200, json=payload))
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "match",
                "-s",
                "Person",
                "-p",
                "firstName=X",
                "-d",
                "sanctions",
                "-f",
                "json",
            ],
        )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert "results" in parsed
    assert parsed["results"][0]["score"] == 0.92


def test_match_sends_correct_body(runner, load_fixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("match_zero_results"))

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(url__regex=r".*/match/.*").mock(side_effect=handler)
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "match",
                "-s",
                "Person",
                "-p",
                "firstName=Aleksandr",
                "-p",
                "firstName=Alexander",  # multi-value (same key twice)
                "-p",
                "lastName=Zacharov",
                "-d",
                "sanctions",
                "-a",
                "best",
                "-f",
                "json",
            ],
        )
    assert result.exit_code == 1  # empty results, but request shape verified
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/match/sanctions"
    assert seen[0].url.params.get("algorithm") == "best"
    body = json.loads(seen[0].content)
    assert body["queries"]["q"]["schema"] == "Person"
    assert body["queries"]["q"]["properties"]["firstName"] == ["Aleksandr", "Alexander"]
    assert body["queries"]["q"]["properties"]["lastName"] == ["Zacharov"]


def test_match_unknown_schema_exits_two(runner) -> None:
    result = runner.invoke(
        app,
        [*_BASE_FLAGS, "match", "-s", "NotARealSchema", "-p", "name=X"],
    )
    assert result.exit_code == 2
    # respx not engaged because the error fires before any HTTP call.
    assert "Unknown schema" in (result.stdout + result.stderr)


def test_match_non_matchable_schema_exits_two(runner) -> None:
    """Schemas with `matchable: false` (e.g. Document) are refused before any HTTP call."""
    result = runner.invoke(app, [*_BASE_FLAGS, "match", "-s", "Document", "-p", "fileName=foo"])
    assert result.exit_code == 2
    output = result.stdout + result.stderr
    assert "not a matchable target" in output
    assert "ref schemas --matchable" in output


def test_match_fuzzy_suggests_schema(runner) -> None:
    """Typos in --schema get a 'Did you mean?' hint pointing at the closest valid name."""
    result = runner.invoke(app, [*_BASE_FLAGS, "match", "-s", "Persn", "-p", "firstName=X"])
    assert result.exit_code == 2
    output = result.stdout + result.stderr
    assert "Unknown schema" in output
    assert "Person" in output  # suggested via difflib


def test_match_fuzzy_suggests_property(runner) -> None:
    """Typos in -p KEY get a 'did you mean?' hint on the closest property."""
    result = runner.invoke(app, [*_BASE_FLAGS, "match", "-s", "Person", "-p", "frstName=X"])
    assert result.exit_code == 2
    output = result.stdout + result.stderr
    assert "firstName" in output  # suggested by _suggest_property


def test_match_unknown_property_exits_two(runner) -> None:
    result = runner.invoke(
        app,
        [*_BASE_FLAGS, "match", "-s", "Person", "-p", "not_a_real_prop=X"],
    )
    assert result.exit_code == 2
    assert "invalid Person entity" in (result.stdout + result.stderr)


def test_match_malformed_property_flag(runner) -> None:
    result = runner.invoke(
        app,
        [*_BASE_FLAGS, "match", "-s", "Person", "-p", "no_equals_sign"],
    )
    assert result.exit_code == 2
    assert "KEY=VALUE" in (result.stdout + result.stderr)


def test_match_from_file_then_property_override(runner, load_fixture, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("match_zero_results"))

    query_file = tmp_path / "query.json"
    query_file.write_text(
        json.dumps(
            {
                "schema": "Person",
                "properties": {"firstName": ["FromFile"], "birthDate": ["1965"]},
            }
        )
    )

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(url__regex=r".*/match/.*").mock(side_effect=handler)
        result = runner.invoke(
            app,
            [
                *_BASE_FLAGS,
                "match",
                "-s",
                "Person",
                "-i",
                str(query_file),
                "-p",
                "lastName=Override",
                "-f",
                "json",
            ],
        )
    assert result.exit_code == 1
    body = json.loads(seen[0].content)
    props = body["queries"]["q"]["properties"]
    assert props["firstName"] == ["FromFile"]  # from file
    assert props["birthDate"] == ["1965"]  # from file
    assert props["lastName"] == ["Override"]  # from -p


def test_fetch_no_nested_flag(runner, load_fixture) -> None:
    captured: list[str] = []
    payload = load_fixture("entity_person")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("nested") or "")
        return httpx.Response(200, json=payload)

    with respx.mock(base_url=_BASE_URL) as mock:
        mock.get(f"/entities/{payload['id']}").mock(side_effect=handler)
        result = runner.invoke(
            app, [*_BASE_FLAGS, "fetch", payload["id"], "--no-nested", "-f", "json"]
        )
    assert result.exit_code == 0
    assert captured == ["false"]
