"""Live integration tests against a real yente instance.

Gated on ``OPENSANCTIONS_API_KEY`` being present (see ``live_client`` fixture).
Run locally with ``pytest -m live``; CI runs them in a separate job that's
gated on the secret being available (which excludes PRs from forks).

Kept deliberately small — these are smoke tests, not a comprehensive suite.
They double as a drift detector for the OpenSanctions API's response shapes.
"""

import json
import os

import pytest
from typer.testing import CliRunner

from yente_client import env
from yente_client.entities import Person
from yente_client.models import (
    AlgorithmsResponse,
    DatasetsResponse,
    MatchResponse,
    ProgramsResponse,
    SearchResponse,
    StatusResponse,
)

pytestmark = pytest.mark.live


def test_healthz_returns_ok(live_client) -> None:
    r = live_client.healthz()
    assert isinstance(r, StatusResponse)
    assert r.status == "ok"


def test_datasets_returns_listing(live_client) -> None:
    r = live_client.datasets()
    assert isinstance(r, DatasetsResponse)
    assert len(r.datasets) > 0


def test_algorithms_includes_best_resolver(live_client) -> None:
    r = live_client.algorithms()
    assert isinstance(r, AlgorithmsResponse)
    # `best` is set by the server; ensure it's a non-empty string we can pass back.
    assert r.best
    assert isinstance(r.best, str)


def test_programs_catalog_parses(live_client) -> None:
    """Drift detector for the programs.json artifact — it's unversioned, so the
    nightly run is what tells us when its shape moves under the Program model."""
    r = live_client.programs()
    assert isinstance(r, ProgramsResponse)
    # Known anchors plus a sanity lower bound, never an exact count.
    assert len(r.data) > 50
    keys = {p.key for p in r.data}
    assert any(k.startswith("US-") for k in keys)
    assert any(p.issuer is not None and p.issuer.territory for p in r.data)


def test_match_known_sanctioned_person(live_client) -> None:
    """Aleksandr Zacharov is a long-standing OFAC SDN entry; a high-confidence
    match here is the integration check that match() actually works."""
    hits = live_client.match(
        Person(firstName="Aleksandr", lastName="Zacharov", birthDate="1965"),
        datasets=["sanctions"],
    )
    assert isinstance(hits, MatchResponse)
    assert hits.top is not None
    assert hits.top.score > 0.7
    # Should be flagged as a screening target.
    assert hits.top.target is True


def test_search_returns_results(live_client) -> None:
    r = live_client.search("acme", datasets=["default"], limit=5)
    assert isinstance(r, SearchResponse)
    assert r.limit == 5


async def test_async_match_known_sanctioned_person(live_async_client) -> None:
    """Mirror of the sync match test, run through the async path."""
    hits = await live_async_client.match(
        Person(firstName="Aleksandr", lastName="Zacharov", birthDate="1965"),
        datasets=["sanctions"],
    )
    assert isinstance(hits, MatchResponse)
    assert hits.top is not None
    assert hits.top.score > 0.7
    assert hits.top.target is True


async def test_async_healthz_returns_ok(live_async_client) -> None:
    r = await live_async_client.healthz()
    assert isinstance(r, StatusResponse)
    assert r.status == "ok"


def test_cli_status_against_live_api() -> None:
    """End-to-end CLI smoke: `yente-cli status` against the real API."""
    from yente_client.cli.main import app

    key = env.api_key()
    if not key:
        pytest.skip("OPENSANCTIONS_API_KEY not set")
    base_url = os.environ.get(env.BASE_URL_VAR, "https://api.test.opensanctions.org")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--api-key", key, "--base-url", base_url, "status", "-f", "json"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["api"]["liveness"]["status"] == "ok"
    assert parsed["api"]["url"] == base_url


def test_match_iter_live(live_client) -> None:
    """Two-entity fan-out against the real API."""
    pairs = [
        ("hit", Person(name="Arkadii Romanovich Rotenberg", birthDate="1951-12-15")),
        ("miss", Person(name="Zzyzx Qwomply Nonexistent")),
    ]
    results = dict(live_client.match_iter(pairs, workers=2, on_error="collect"))
    assert set(results) == {"hit", "miss"}
    hit = results["hit"]
    assert isinstance(hit, MatchResponse)
    assert hit.top is not None
    assert hit.top.score > 0.5


def test_cli_screen_against_live_api(tmp_path) -> None:
    """End-to-end CLI smoke: `yente-cli screen` over a tiny CSV."""
    from yente_client.cli.main import app

    key = env.api_key()
    if not key:
        pytest.skip("OPENSANCTIONS_API_KEY not set")
    base_url = env.base_url()
    src = tmp_path / "in.csv"
    src.write_text("row_id,full_name,born\n1,Arkadii Romanovich Rotenberg,1951-12-15\n")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--api-key",
            key,
            "--base-url",
            base_url,
            "screen",
            str(src),
            "-",
            "-s",
            "Person",
            "-i",
            "full_name=name",
            "-i",
            "born=birthDate",
            "--url",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("row_id,full_name,born,match_id,")
    assert len(lines) >= 2
    assert ",https://www.opensanctions.org/entities/" in lines[1]
