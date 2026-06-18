"""Tests for the offline ``ref`` introspection subcommands.

These don't hit the network — they read the bundled ``model.json`` — so we
exercise them directly without ``respx`` mocking.
"""

import json

import pytest
from typer.testing import CliRunner

from yente_client.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------- ref schemas ----------


def test_ref_schemas_json_includes_person(runner) -> None:
    result = runner.invoke(app, ["ref", "schemas", "-f", "json"])
    assert result.exit_code == 0
    schemas = json.loads(result.stdout)
    names = {s["name"] for s in schemas}
    assert {"Person", "Company", "Email"} <= names


def test_ref_schemas_marks_matchable(runner) -> None:
    result = runner.invoke(app, ["ref", "schemas", "-f", "json"])
    assert result.exit_code == 0
    schemas = json.loads(result.stdout)
    person = next(s for s in schemas if s["name"] == "Person")
    assert person["matchable"] is True


def test_ref_schemas_matchable_filter(runner) -> None:
    """`--matchable` drops any schema with matchable=False."""
    full = json.loads(runner.invoke(app, ["ref", "schemas", "-f", "json"]).stdout)
    filtered = json.loads(
        runner.invoke(app, ["ref", "schemas", "--matchable", "-f", "json"]).stdout
    )
    assert len(filtered) < len(full)
    assert all(s["matchable"] for s in filtered)


def test_ref_schemas_table(runner) -> None:
    result = runner.invoke(app, ["ref", "schemas", "-f", "table"])
    assert result.exit_code == 0
    assert "Person" in result.stdout
    assert "Company" in result.stdout


# ---------- ref schema NAME ----------


def test_ref_schema_person_json(runner) -> None:
    result = runner.invoke(app, ["ref", "schema", "Person", "-f", "json"])
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["name"] == "Person"
    assert summary["matchable"] is True
    assert "LegalEntity" in summary["extends"]
    prop_names = {p["name"] for p in summary["properties"]}
    # Own properties (Person.*)
    assert "firstName" in prop_names
    assert "birthDate" in prop_names
    # Inherited from LegalEntity:
    assert "name" in prop_names


def test_ref_schema_includes_stub_edges_marked(runner) -> None:
    """Stub (reverse-edge) properties ARE shown — they're the relationship edges
    fetch_entity_relations traverses — but marked so they read as edges."""
    summary = json.loads(runner.invoke(app, ["ref", "schema", "Person", "-f", "json"]).stdout)
    by_name = {p["name"]: p for p in summary["properties"]}
    assert by_name["ownershipOwner"]["stub"] is True
    assert "stub" not in by_name["birthDate"]  # settable attribute


def test_ref_schema_omits_deprecated_field(runner) -> None:
    """The `deprecated` field is intentionally dropped from the projection; the
    (deprecated) property itself still appears, consistent with the codegen."""
    summary = json.loads(runner.invoke(app, ["ref", "schema", "Person", "-f", "json"]).stdout)
    second_name = next(p for p in summary["properties"] if p["name"] == "secondName")
    assert "deprecated" not in second_name


def test_ref_schema_property_matchable_resolves_type_defaults(runner) -> None:
    """birthDate is explicitly matchable; firstName falls through to type-default (False)."""
    summary = json.loads(runner.invoke(app, ["ref", "schema", "Person", "-f", "json"]).stdout)
    by_name = {p["name"]: p for p in summary["properties"]}
    assert by_name["birthDate"]["matchable"] is True
    assert by_name["citizenship"]["matchable"] is True
    # Non-matchable per the FtM model (omitted now that false flags are dropped),
    # but still actively used in scoring via dedicated matcher features.
    assert "matchable" not in by_name["firstName"]
    assert "matchable" not in by_name["lastName"]
    assert "matchable" not in by_name["gender"]


def test_ref_schema_table_includes_legend(runner) -> None:
    """The table view explains the FtM matchable flag's actual meaning."""
    result = runner.invoke(app, ["ref", "schema", "Person", "-f", "table"])
    assert result.exit_code == 0
    # Column header uses the FtM term as-is.
    assert "matchable" in result.stdout
    # Legend names some of the non-matchable properties that still score.
    assert "firstName" in result.stdout
    assert "weakAlias" in result.stdout
    # Legend states the flag is NOT a "useful for matching" indicator.
    assert "NOT a 'useful for matching'" in result.stdout


def test_ref_schema_unknown_exits_two(runner) -> None:
    result = runner.invoke(app, ["ref", "schema", "NotARealSchema"])
    assert result.exit_code == 2
    assert "Unknown schema" in (result.stdout + result.stderr)


def test_ref_schema_table(runner) -> None:
    result = runner.invoke(app, ["ref", "schema", "Person", "-f", "table"])
    assert result.exit_code == 0
    assert "Person" in result.stdout
    assert "firstName" in result.stdout


# ---------- ref topics ----------


def test_ref_topics_includes_sanction(runner) -> None:
    result = runner.invoke(app, ["ref", "topics", "-f", "json"])
    assert result.exit_code == 0
    topics = json.loads(result.stdout)
    names = {t["name"] for t in topics}
    assert "sanction" in names
    assert "role.pep" in names


# ---------- ref countries ----------


def test_ref_countries_includes_known_codes(runner) -> None:
    result = runner.invoke(app, ["ref", "countries", "-f", "json"])
    assert result.exit_code == 0
    countries = json.loads(result.stdout)
    codes = {c["code"] for c in countries}
    # Anchor on stable ISO codes; don't assert exact count.
    assert {"us", "ru", "gb", "de"} <= codes


def test_ref_countries_table(runner) -> None:
    result = runner.invoke(app, ["ref", "countries", "-f", "table"])
    assert result.exit_code == 0
    assert "us" in result.stdout
    assert "ru" in result.stdout
