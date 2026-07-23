"""``yente-cli screen`` — batch-screen a CSV of entities against ``/match``.

Reads one entity per input row, fans out over :meth:`Client.match_iter`, and
writes one output row per match candidate. All input columns pass through to
the output; result columns carry a ``match_`` prefix so the file joins back
to its source without bookkeeping.
"""

import csv
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from yente_client import entities
from yente_client.cli._deps import Console, typer
from yente_client.cli.commands import (
    _resolve_matchable_schema,
    _suggest_property,
    _suggest_schema,
    _with_client,
)
from yente_client.client import BEST_ALGORITHM
from yente_client.entities import EntityInput
from yente_client.models import MatchError, MatchResponse, ScoredEntity
from yente_client.schemas import is_matchable_schema

ENTITY_URL_TEMPLATE = "https://www.opensanctions.org/entities/{id}/"

# Result columns the command itself emits, in output order. `match` (the
# threshold flag) deliberately has no prefix — `match_match` would stutter.
_FIXED_COLUMNS = ["match_id", "match_caption", "match_score", "match", "match_topics"]


def _usage_error(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=2)


def _parse_mapping(spec: str, flag: str, shape: str) -> tuple[str, str]:
    """Split a ``LEFT=RIGHT`` mapping flag value; exit 2 on malformed input."""
    left, sep, right = spec.partition("=")
    if not sep or not left or not right:
        raise _usage_error(f"{flag} must be {shape}; got {spec!r}")
    return left, right


def _resolve_row_schema(name: str) -> type[EntityInput] | str:
    """Resolve a per-row schema name; return an error message instead of exiting."""
    schema_cls = getattr(entities, name, None)
    if schema_cls is None or not isinstance(schema_cls, type):
        suggestion = _suggest_schema(name)
        hint = f" (did you mean {suggestion!r}?)" if suggestion else ""
        return f"unknown schema {name!r}{hint}"
    if not is_matchable_schema(name):
        return f"schema {name!r} is not matchable"
    return schema_cls


def _build_row_entity(
    row: dict[str, Any],
    mappings: list[tuple[str, str]],
    schema_cls: type[EntityInput],
) -> EntityInput | str:
    """Build the query entity for one row; return an error message on bad input."""
    props: dict[str, list[str]] = {}
    for column, prop in mappings:
        value = (row.get(column) or "").strip()
        if value:
            props.setdefault(prop, []).append(value)
    if not props:
        return "no input values"
    try:
        return schema_cls.model_validate(props)
    except ValidationError as exc:
        schema = schema_cls.schema_
        problems: list[str] = []
        for err in exc.errors():
            loc = str(err["loc"][0]) if err.get("loc") else "?"
            if err.get("type") == "extra_forbidden":
                close = _suggest_property(schema, loc)
                hint = f" (did you mean {close!r}?)" if close else ""
                problems.append(f"unknown property {loc!r}{hint}")
            else:
                problems.append(f"{loc}: {err.get('msg', 'invalid value')}")
        return f"invalid {schema} entity: " + "; ".join(problems)


def _count_data_rows(path: Path) -> int:
    """Approximate data-row count for the progress bar (header excluded)."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        lines = sum(1 for _ in fh)
    return max(lines - 1, 0)


def screen_command(
    ctx: typer.Context,
    input_file: str = typer.Argument(
        ...,
        help="Input CSV file; `-` reads from stdin.",
    ),
    output_file: str | None = typer.Argument(
        None,
        help="Output CSV file; `-` writes to stdout. Default: <input>.out.csv next to the input.",
    ),
    schema: str | None = typer.Option(
        None,
        "--schema",
        "-s",
        help="FtM schema for every row (Person, Company, ...). Run `ref schemas --matchable`.",
    ),
    schema_column: str | None = typer.Option(
        None,
        "--schema-column",
        help="Input column holding each row's schema name. Rows with an empty "
        "cell fall back to `-s` when both are given.",
    ),
    inputs: list[str] | None = typer.Option(
        None,
        "--input",
        "-i",
        help="Map an input column to an FtM property: COLUMN=prop, e.g. "
        "`-i person_name=name -i dob=birthDate`. Repeatable; several columns "
        "may feed the same property.",
    ),
    outputs: list[str] | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Add an output column with a candidate property: prop=COLUMN, e.g. "
        "`-o name=candidate_name`. Repeatable. Multi-values are joined with --join.",
    ),
    datasets: list[str] | None = typer.Option(
        None, "--datasets", "-d", help="Restrict to dataset(s). Repeatable."
    ),
    topics: list[str] | None = typer.Option(
        None, "--topics", "-t", help="Topic filter. Repeatable."
    ),
    threshold: float | None = typer.Option(
        None, "--threshold", help="Score threshold for the match flag (server default 0.70)."
    ),
    cutoff: float | None = typer.Option(
        None, "--cutoff", help="Drop candidates scoring below this (server default 0.50)."
    ),
    algorithm: str = typer.Option(
        BEST_ALGORITHM,
        "--algorithm",
        "-a",
        help='Matching algorithm. "best" is stable across versions; see `algorithms`.',
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-l", help="Max candidates per input row (server default 5)."
    ),
    changed_since: str | None = typer.Option(
        None, "--changed-since", help="Only match entities updated since this ISO 8601 date."
    ),
    exclude_entities: list[str] | None = typer.Option(
        None, "--exclude-entities", help="Exclude these entity IDs from results. Repeatable."
    ),
    exclude_schemata: list[str] | None = typer.Option(
        None, "--exclude-schemata", help="Exclude these schemas from results. Repeatable."
    ),
    match_only: bool = typer.Option(
        False, "--match", help="Keep only candidates with match=true (score crossed the threshold)."
    ),
    include_empty: bool = typer.Option(
        False,
        "--include-empty",
        help="Emit one row with blank result columns for inputs that yielded no "
        "candidates, so the output is a complete screening record.",
    ),
    url: bool = typer.Option(
        False, "--url", help="Add a match_url column linking to opensanctions.org."
    ),
    explanation: bool = typer.Option(
        False,
        "--explanation",
        help="Add a match_explanation column: contributing features as "
        "feature=score pairs, highest first.",
    ),
    join: str = typer.Option(
        ";", "--join", help="Separator for multi-valued cells (topics, -o properties)."
    ),
    workers: int = typer.Option(20, "--workers", min=1, help="Concurrent match requests."),
) -> None:
    """Batch-screen a CSV of entities and write match candidates to a CSV.

    Map input columns to FtM properties with `-i`, and every row is matched
    concurrently. Output rows carry all input columns plus `match_`-prefixed
    candidate columns — one row per candidate. Rows that fail (bad schema,
    invalid values, request errors) get a `match_error` row instead of
    aborting the run.

    This is the bulk counterpart of `match`: use it for any matching task
    over a file, even with partial input (just a name column is fine).
    """
    # ---- resolve schema flags ----
    if schema is None and schema_column is None:
        raise _usage_error("pass --schema/-s or --schema-column (or both)")
    fixed_cls: type[EntityInput] | None = None
    if schema is not None:
        fixed_cls = _resolve_matchable_schema(schema, command="screen")

    # ---- parse mappings ----
    in_mappings = [_parse_mapping(s, "--input/-i", "COLUMN=prop") for s in inputs or []]
    if not in_mappings:
        raise _usage_error("pass at least one --input/-i COLUMN=prop mapping")
    out_mappings = [_parse_mapping(s, "--output/-o", "prop=COLUMN") for s in outputs or []]

    if fixed_cls is not None and schema_column is None:
        valid_props = set(fixed_cls.model_fields) - {"id"}
        for _, prop in in_mappings:
            if prop not in valid_props:
                close = _suggest_property(fixed_cls.schema_, prop)
                hint = f" Did you mean {close!r}?" if close else ""
                raise _usage_error(
                    f"{fixed_cls.schema_} has no property {prop!r}.{hint} "
                    f"Run `yente-cli ref schema {fixed_cls.schema_}`."
                )

    # ---- resolve input / output streams ----
    input_path: Path | None = None
    if input_file != "-":
        input_path = Path(input_file)
        if not input_path.is_file():
            raise _usage_error(f"input file not found: {input_file}")

    output_path: Path | None = None
    to_stdout = output_file == "-"
    if not to_stdout:
        if output_file is not None:
            output_path = Path(output_file)
        elif input_path is not None:
            output_path = input_path.with_suffix(".out.csv")
        else:
            raise _usage_error("reading from stdin requires an explicit output file (or `-`)")
        if input_path is not None and output_path.resolve() == input_path.resolve():
            raise _usage_error(f"output would overwrite the input file: {output_path}")

    in_stream: TextIO = sys.stdin if input_path is None else input_path.open(newline="")
    try:
        reader = csv.DictReader(in_stream)
        header = reader.fieldnames
        if not header:
            raise _usage_error("input has no header row")

        # ---- validate mappings against the header ----
        for column, _ in in_mappings:
            if column not in header:
                raise _usage_error(f"input column {column!r} not found in {list(header)}")
        if schema_column is not None and schema_column not in header:
            raise _usage_error(f"--schema-column {schema_column!r} not found in {list(header)}")

        # ---- assemble output columns ----
        fixed_columns = list(_FIXED_COLUMNS)
        if url:
            fixed_columns.append("match_url")
        if explanation:
            fixed_columns.append("match_explanation")
        clashes = sorted(set(fixed_columns + ["match_error"]) & set(header))
        if clashes:
            raise _usage_error(
                f"input column(s) {clashes} collide with result columns; "
                "rename them in the input file"
            )
        taken = set(header) | set(fixed_columns) | {"match_error"}
        for _, column in out_mappings:
            if column in taken:
                raise _usage_error(f"-o column {column!r} collides with an existing column")
            taken.add(column)
        fieldnames = list(header) + fixed_columns + [c for _, c in out_mappings] + ["match_error"]

        total_rows = _count_data_rows(input_path) if input_path is not None else None

        out_stream: TextIO = (
            sys.stdout if output_path is None else output_path.open("w", newline="")
        )
        try:
            _run_screen(
                ctx,
                reader,
                out_stream,
                fieldnames=fieldnames,
                fixed_cls=fixed_cls,
                schema_column=schema_column,
                in_mappings=in_mappings,
                out_mappings=out_mappings,
                match_kwargs=_collect_match_kwargs(
                    datasets=datasets,
                    topics=topics,
                    changed_since=changed_since,
                    exclude_entities=exclude_entities,
                    exclude_schemata=exclude_schemata,
                ),
                threshold=threshold,
                cutoff=cutoff,
                algorithm=algorithm,
                limit=limit,
                match_only=match_only,
                include_empty=include_empty,
                url=url,
                explanation=explanation,
                join=join,
                workers=workers,
                total_rows=total_rows,
            )
        finally:
            if output_path is not None:
                out_stream.close()
    finally:
        if input_path is not None:
            in_stream.close()


def _collect_match_kwargs(**maybe: Any) -> dict[str, Any]:
    """Drop unset filter flags so SDK/server defaults apply."""
    return {key: value for key, value in maybe.items() if value}


def _run_screen(
    ctx: typer.Context,
    reader: "csv.DictReader[str]",
    out_stream: TextIO,
    *,
    fieldnames: list[str],
    fixed_cls: type[EntityInput] | None,
    schema_column: str | None,
    in_mappings: list[tuple[str, str]],
    out_mappings: list[tuple[str, str]],
    match_kwargs: dict[str, Any],
    threshold: float | None,
    cutoff: float | None,
    algorithm: str,
    limit: int | None,
    match_only: bool,
    include_empty: bool,
    url: bool,
    explanation: bool,
    join: str,
    workers: int,
    total_rows: int | None,
) -> None:
    """Stream rows through match_iter and write results in input order."""
    from rich.progress import Progress

    dict_writer = csv.DictWriter(
        out_stream, fieldnames=fieldnames, extrasaction="ignore", restval=""
    )

    candidates = 0
    hit_rows = 0
    error_rows = 0
    rows_read = 0

    # Row dicts and per-row outcomes by 1-based index. Bounded: match_iter
    # pulls at most `workers` rows ahead of what has been flushed.
    pending_rows: dict[int, dict[str, Any]] = {}
    ready: dict[int, MatchResponse | str] = {}
    next_flush = 1

    def candidate_cells(result: ScoredEntity) -> dict[str, str]:
        def prop_values(prop: str) -> str:
            return join.join(v for v in result.properties.get(prop, []) if isinstance(v, str))

        cells = {
            "match_id": result.id,
            "match_caption": result.caption,
            "match_score": f"{result.score:.3f}",
            "match": "true" if result.match else "false",
            "match_topics": prop_values("topics"),
        }
        if url:
            cells["match_url"] = ENTITY_URL_TEMPLATE.format(id=result.id)
        if explanation:
            features = sorted(result.contributing_explanations.items(), key=lambda kv: -kv[1].score)
            cells["match_explanation"] = join.join(f"{n}={f.score:.2f}" for n, f in features)
        for prop, column in out_mappings:
            cells[column] = prop_values(prop)
        return cells

    def emit(row: dict[str, Any], outcome: MatchResponse | str) -> None:
        nonlocal candidates, hit_rows, error_rows
        base = {k: v for k, v in row.items() if isinstance(k, str)}
        if isinstance(outcome, str):
            error_rows += 1
            dict_writer.writerow({**base, "match_error": outcome})
            return
        results = outcome.matches if match_only else outcome.results
        if not results:
            if include_empty:
                dict_writer.writerow(base)
            return
        hit_rows += 1
        candidates += len(results)
        for result in results:
            dict_writer.writerow({**base, **candidate_cells(result)})

    def pairs() -> Iterator[tuple[str, EntityInput]]:
        nonlocal rows_read
        for idx, row in enumerate(reader, start=1):
            rows_read = idx
            pending_rows[idx] = row
            row_cls = fixed_cls
            if schema_column is not None:
                name = (row.get(schema_column) or "").strip()
                if name:
                    resolved = _resolve_row_schema(name)
                    if isinstance(resolved, str):
                        ready[idx] = resolved
                        continue
                    row_cls = resolved
                elif row_cls is None:
                    ready[idx] = f"empty {schema_column!r} cell and no --schema fallback"
                    continue
            assert row_cls is not None  # guaranteed by flag validation
            entity = _build_row_entity(row, in_mappings, row_cls)
            if isinstance(entity, str):
                ready[idx] = entity
                continue
            yield str(idx), entity

    stderr_console = Console(stderr=True)
    with (
        Progress(console=stderr_console, disable=not stderr_console.is_terminal) as progress,
        _with_client(ctx) as client,
    ):
        task = progress.add_task("screening", total=total_rows)

        def flush() -> None:
            nonlocal next_flush
            while next_flush in ready:
                emit(pending_rows.pop(next_flush), ready.pop(next_flush))
                progress.advance(task)
                next_flush += 1

        dict_writer.writeheader()
        stream = client.match_iter(
            pairs(),
            workers=workers,
            on_error="collect",
            threshold=threshold,
            cutoff=cutoff,
            algorithm=algorithm,
            limit=limit,
            **match_kwargs,
        )
        for key, outcome in stream:
            if isinstance(outcome, MatchError):
                exc = outcome.exception
                ready[int(key)] = f"{type(exc).__name__}: {exc}"
            else:
                ready[int(key)] = outcome
            flush()
        flush()  # drain rows that short-circuited after the last API result

    typer.echo(
        f"screened {rows_read} rows: {candidates} candidates on {hit_rows} rows, "
        f"{error_rows} errors",
        err=True,
    )
    if error_rows:
        raise typer.Exit(code=5)
    if not candidates:
        raise typer.Exit(code=1)


_SCREEN_EPILOG = """\
EXAMPLES:
  # customers.csv has columns: cust_id, full_name, born
  yente-cli screen customers.csv -s Person -i full_name=name -i born=birthDate
  yente-cli screen customers.csv hits.csv -s Person -i full_name=name --match --url
  yente-cli screen parties.csv -s LegalEntity -i party=name -d sanctions --explanation
  yente-cli screen mixed.csv --schema-column type -s LegalEntity -i name=name
  cat rows.csv | yente-cli screen - results.csv -s Company -i company=name

OUTPUT:
  One row per match candidate; all input columns pass through unchanged, so
  the file joins back to its source. Result columns:
    match_id, match_caption, match_score, match (true/false), match_topics,
    match_url (--url), match_explanation (--explanation), match_error.
  Inputs with no candidates are dropped unless --include-empty. Rows that
  fail (bad schema, invalid value, request error) get a match_error row and
  the run continues. Add candidate properties as columns with -o, e.g.
  `-o name=candidate_name -o country=candidate_country`.

PROPERTY NAMES:
  -i maps COLUMN=prop with FtM camelCase property names (`birthDate`, not
  `birth_date`). Run `yente-cli ref schema Person` to list them. Map every
  column you have — more properties mean better scoring.

EXIT CODES:
  0  completed, ≥1 candidate written
  1  completed, zero candidates
  2  usage error (bad mapping, unknown schema, column collision)
  3  API error before streaming started
  4  network/transport error before streaming started
  5  completed, but some rows failed (see match_error column)

Screen is the bulk counterpart of `match` — use it for any file-shaped
matching task, even with partial input. `search` is only for interactive,
user-facing search UIs.
"""
