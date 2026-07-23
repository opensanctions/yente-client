"""Tests for the `screen` CLI command — batch CSV in, candidate CSV out."""

import csv
import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from yente_client.cli.main import app

_BASE_URL = "http://test.local"
_BASE_FLAGS = ["--api-key", "test", "--base-url", _BASE_URL]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _candidate(
    id: str = "NK-1",
    caption: str = "Alexander ZAKHAROV",
    score: float = 0.92,
    match: bool = True,
    topics: tuple[str, ...] = ("sanction",),
    schema: str = "Person",
    properties: dict | None = None,
    explanations: dict | None = None,
) -> dict:
    props: dict = {"topics": list(topics)}
    if properties:
        props.update(properties)
    return {
        "id": id,
        "caption": caption,
        "schema": schema,
        "properties": props,
        "datasets": ["us_ofac_sdn"],
        "target": True,
        "score": score,
        "match": match,
        "explanations": explanations or {},
    }


def _match_response(results: list[dict]) -> dict:
    return {
        "responses": {
            "q": {
                "status": 200,
                "results": results,
                "total": {"value": len(results), "relation": "eq"},
                "query": {},
            }
        },
        "limit": 5,
    }


def _by_name(mapping: dict[str, list[dict]], status_by_name: dict[str, int] | None = None):
    """Handler returning canned candidates keyed on the queried `name` value."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        props = body["queries"]["q"]["properties"]
        name = (props.get("name") or props.get("firstName") or ["?"])[0]
        if status_by_name and name in status_by_name:
            return httpx.Response(status_by_name[name], json={"detail": "boom"})
        return httpx.Response(200, json=_match_response(mapping.get(name, [])))

    return handler


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _invoke(runner: CliRunner, args: list[str], handler, input_text: str | None = None):
    # assert_all_called=False: usage-error tests exit before any HTTP call.
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=handler)
        return runner.invoke(app, [*_BASE_FLAGS, "screen", *args], input=input_text)


# ---------- happy path ----------


def test_screen_writes_derived_output_with_passthrough(runner, tmp_path) -> None:
    src = tmp_path / "customers.csv"
    _write_csv(src, ["cust_id", "full_name"], [["c1", "Zakharov"], ["c2", "Nobody"]])
    handler = _by_name({"Zakharov": [_candidate()]})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "full_name=name"], handler)
    assert result.exit_code == 0, result.stdout + result.stderr

    out = tmp_path / "customers.out.csv"
    rows = _read_csv(out)
    # One row per candidate; the no-hit row (c2) is dropped by default.
    assert len(rows) == 1
    row = rows[0]
    assert row["cust_id"] == "c1"
    assert row["full_name"] == "Zakharov"
    assert row["match_id"] == "NK-1"
    assert row["match_caption"] == "Alexander ZAKHAROV"
    assert row["match_score"] == "0.920"
    assert row["match"] == "true"
    assert row["match_topics"] == "sanction"
    assert row["match_error"] == ""


def test_screen_column_order(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["a", "b"], [["x", "y"]])
    handler = _by_name({"x": [_candidate()]})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "a=name"], handler)
    assert result.exit_code == 0
    with (tmp_path / "in.out.csv").open(newline="") as fh:
        header = next(csv.reader(fh))
    assert header == [
        "a",
        "b",
        "match_id",
        "match_caption",
        "match_score",
        "match",
        "match_topics",
        "match_error",
    ]


def test_screen_multiple_candidates_multiple_rows(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    handler = _by_name(
        {
            "Zakharov": [
                _candidate(id="NK-1", score=0.92),
                _candidate(id="NK-2", score=0.55, match=False),
            ]
        }
    )

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], handler)
    assert result.exit_code == 0
    rows = _read_csv(tmp_path / "in.out.csv")
    assert [r["match_id"] for r in rows] == ["NK-1", "NK-2"]
    assert [r["match"] for r in rows] == ["true", "false"]
    assert all(r["name"] == "Zakharov" for r in rows)


def test_screen_preserves_input_order(runner, tmp_path) -> None:
    names = [f"P{i}" for i in range(1, 9)]
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [[n] for n in names])
    handler = _by_name({n: [_candidate(id=f"NK-{n}")] for n in names})

    result = _invoke(
        runner, [str(src), "-s", "Person", "-i", "name=name", "--workers", "8"], handler
    )
    assert result.exit_code == 0
    rows = _read_csv(tmp_path / "in.out.csv")
    assert [r["name"] for r in rows] == names


def test_screen_summary_on_stderr(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"], ["Nobody"]])
    handler = _by_name({"Zakharov": [_candidate()]})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], handler)
    assert "screened 2 rows: 1 candidates on 1 rows, 0 errors" in result.stderr


# ---------- flags: --match, --include-empty, --limit passthrough ----------


def test_screen_match_only_filters(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    handler = _by_name(
        {"Zakharov": [_candidate(id="NK-1", match=True), _candidate(id="NK-2", match=False)]}
    )

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name", "--match"], handler)
    assert result.exit_code == 0
    rows = _read_csv(tmp_path / "in.out.csv")
    assert [r["match_id"] for r in rows] == ["NK-1"]


def test_screen_include_empty(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"], ["Nobody"]])
    handler = _by_name({"Zakharov": [_candidate()]})

    result = _invoke(
        runner, [str(src), "-s", "Person", "-i", "name=name", "--include-empty"], handler
    )
    assert result.exit_code == 0
    rows = _read_csv(tmp_path / "in.out.csv")
    assert len(rows) == 2
    assert rows[1]["name"] == "Nobody"
    assert rows[1]["match_id"] == ""
    assert rows[1]["match_error"] == ""


def test_screen_zero_candidates_exit_1(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Nobody"]])
    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 1


def test_screen_query_params_passed(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_match_response([]))

    result = _invoke(
        runner,
        [
            str(src),
            "-s",
            "Person",
            "-i",
            "name=name",
            "--threshold",
            "0.8",
            "--cutoff",
            "0.6",
            "-l",
            "3",
            "-d",
            "sanctions",
            "-t",
            "sanction",
        ],
        handler,
    )
    assert result.exit_code == 1  # zero candidates
    params = seen[0].url.params
    assert seen[0].url.path == "/match/sanctions"
    assert params.get("threshold") == "0.8"
    assert params.get("cutoff") == "0.6"
    assert params.get("limit") == "3"
    assert params.get("topics") == "sanction"
    assert params.get("algorithm") == "best"  # explicit default


# ---------- -o mappings, --join, --url, --explanation ----------


def test_screen_output_mapping_and_join(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    handler = _by_name(
        {
            "Zakharov": [
                _candidate(
                    properties={"name": ["Alexander Z", "Sasha Z"], "country": ["ru"]},
                    topics=("sanction", "role.pep"),
                )
            ]
        }
    )

    result = _invoke(
        runner,
        [
            str(src),
            "-s",
            "Person",
            "-i",
            "name=name",
            "-o",
            "name=candidate_name",
            "-o",
            "country=candidate_country",
            "--join",
            "|",
        ],
        handler,
    )
    assert result.exit_code == 0
    row = _read_csv(tmp_path / "in.out.csv")[0]
    assert row["candidate_name"] == "Alexander Z|Sasha Z"
    assert row["candidate_country"] == "ru"
    assert row["match_topics"] == "sanction|role.pep"


def test_screen_url_column(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    handler = _by_name({"Zakharov": [_candidate(id="NK-abc")]})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name", "--url"], handler)
    assert result.exit_code == 0
    row = _read_csv(tmp_path / "in.out.csv")[0]
    assert row["match_url"] == "https://www.opensanctions.org/entities/NK-abc/"


def test_screen_explanation_column(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    explanations = {
        "country_match": {"score": 1.0, "weight": 0.1},
        "name_match": {"score": 0.92, "weight": 1.0},
        "dob_match": {"score": 0.0, "weight": 0.5},
    }
    handler = _by_name({"Zakharov": [_candidate(explanations=explanations)]})

    result = _invoke(
        runner, [str(src), "-s", "Person", "-i", "name=name", "--explanation"], handler
    )
    assert result.exit_code == 0
    row = _read_csv(tmp_path / "in.out.csv")[0]
    # Descending score; zero-score features dropped.
    assert row["match_explanation"] == "country_match=1.00;name_match=0.92"


# ---------- error rows / exit 5 ----------


def test_screen_request_error_row_and_exit_5(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"], ["Boom"], ["Nobody"]])
    handler = _by_name({"Zakharov": [_candidate()]}, status_by_name={"Boom": 500})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], handler)
    assert result.exit_code == 5
    rows = _read_csv(tmp_path / "in.out.csv")
    assert len(rows) == 2  # hit row + error row; Nobody dropped
    assert rows[0]["match_id"] == "NK-1"
    assert rows[1]["name"] == "Boom"
    assert "ServerError" in rows[1]["match_error"]
    assert rows[1]["match_id"] == ""
    assert "1 errors" in result.stderr


def test_screen_empty_mapped_cells_error_row(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Zakharov"], [""]])
    handler = _by_name({"Zakharov": [_candidate()]})

    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], handler)
    assert result.exit_code == 5
    rows = _read_csv(tmp_path / "in.out.csv")
    assert rows[1]["match_error"] == "no input values"


# ---------- schema handling ----------


def test_screen_requires_schema_or_schema_column(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), "-i", "name=name"], _by_name({}))
    assert result.exit_code == 2
    assert "--schema" in result.stderr


def test_screen_non_matchable_fixed_schema_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), "-s", "Document", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 2
    assert "not a matchable target" in result.stderr


def test_screen_schema_column_per_row(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(
        src,
        ["type", "name"],
        [["Person", "Zakharov"], ["Company", "Acme"], ["Nonsense", "X"], ["", "Fallback"]],
    )
    handler = _by_name(
        {
            "Zakharov": [_candidate()],
            "Acme": [_candidate(id="NK-co", caption="ACME LLC", schema="Company")],
            "Fallback": [_candidate(id="NK-fb")],
        }
    )

    result = _invoke(
        runner,
        [str(src), "--schema-column", "type", "-s", "Person", "-i", "name=name"],
        handler,
    )
    assert result.exit_code == 5  # the Nonsense row errored
    rows = _read_csv(tmp_path / "in.out.csv")
    assert [r["match_id"] for r in rows] == ["NK-1", "NK-co", "", "NK-fb"]
    assert "unknown schema 'Nonsense'" in rows[2]["match_error"]


def test_screen_schema_column_empty_cell_without_fallback(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["type", "name"], [["", "X"]])
    result = _invoke(runner, [str(src), "--schema-column", "type", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 5
    rows = _read_csv(tmp_path / "in.out.csv")
    assert "no --schema fallback" in rows[0]["match_error"]


def test_screen_schema_column_non_matchable_row(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["type", "name"], [["Document", "X"]])
    result = _invoke(runner, [str(src), "--schema-column", "type", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 5
    rows = _read_csv(tmp_path / "in.out.csv")
    assert "not matchable" in rows[0]["match_error"]


# ---------- mapping validation / usage errors ----------


def test_screen_unknown_input_property_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["dob"], [["1980-01-01"]])
    result = _invoke(runner, [str(src), "-s", "Person", "-i", "dob=birthdate"], _by_name({}))
    assert result.exit_code == 2
    assert "birthDate" in result.stderr  # fuzzy suggestion


def test_screen_missing_input_column_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), "-s", "Person", "-i", "missing=name"], _by_name({}))
    assert result.exit_code == 2
    assert "'missing' not found" in result.stderr


def test_screen_malformed_mapping_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name"], _by_name({}))
    assert result.exit_code == 2
    assert "COLUMN=prop" in result.stderr


def test_screen_requires_input_mapping(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), "-s", "Person"], _by_name({}))
    assert result.exit_code == 2
    assert "--input" in result.stderr


def test_screen_result_column_collision_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name", "match_id"], [["X", "1"]])
    result = _invoke(runner, [str(src), "-s", "Person", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 2
    assert "match_id" in result.stderr


def test_screen_output_column_collision_exit_2(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(
        runner, [str(src), "-s", "Person", "-i", "name=name", "-o", "name=name"], _by_name({})
    )
    assert result.exit_code == 2
    assert "collides" in result.stderr


def test_screen_missing_input_file_exit_2(runner, tmp_path) -> None:
    result = _invoke(
        runner, [str(tmp_path / "nope.csv"), "-s", "Person", "-i", "name=name"], _by_name({})
    )
    assert result.exit_code == 2
    assert "not found" in result.stderr


# ---------- stdin / stdout ----------


def test_screen_stdin_requires_output(runner) -> None:
    result = _invoke(runner, ["-", "-s", "Person", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 2
    assert "stdin" in result.stderr


def test_screen_stdin_to_stdout(runner) -> None:
    handler = _by_name({"Zakharov": [_candidate()]})
    result = _invoke(
        runner,
        ["-", "-", "-s", "Person", "-i", "name=name"],
        handler,
        input_text="name\nZakharov\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    rows = list(csv.DictReader(result.stdout.splitlines()))
    assert rows[0]["match_id"] == "NK-1"
    assert "screened 1 rows" in result.stderr


def test_screen_explicit_output_path(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    dst = tmp_path / "results.csv"
    _write_csv(src, ["name"], [["Zakharov"]])
    handler = _by_name({"Zakharov": [_candidate()]})

    result = _invoke(runner, [str(src), str(dst), "-s", "Person", "-i", "name=name"], handler)
    assert result.exit_code == 0
    assert _read_csv(dst)[0]["match_id"] == "NK-1"


def test_screen_refuses_overwriting_input(runner, tmp_path) -> None:
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["X"]])
    result = _invoke(runner, [str(src), str(src), "-s", "Person", "-i", "name=name"], _by_name({}))
    assert result.exit_code == 2
    assert "overwrite" in result.stderr
