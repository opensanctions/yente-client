"""``yente-cli`` subcommand implementations.

Each command is a thin wrapper around the SDK; entity construction, filter
translation, and HTTP details live in :mod:`yente_client.client`. This
module's job is argument-parsing + output formatting.
"""

import contextlib
import difflib
import json
import time
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from yente_client import entities
from yente_client.cli._deps import typer
from yente_client.cli.config import CliConfig
from yente_client.cli.output import (
    Format,
    print_json,
    print_jsonl,
    print_table,
    resolve_format,
)
from yente_client.client import Client
from yente_client.entities import EntityInput
from yente_client.exceptions import (
    APIError,
    ConfigurationError,
    TransportError,
    YenteError,
)
from yente_client.models import (
    DataCoverage,
    DataPublisher,
    Dataset,
    Entity,
    ProgramIssuer,
)
from yente_client.schemas import (
    describe_schema,
    has_schema,
    is_matchable_schema,
    iter_properties,
    matchable_schemata,
    model,
    schema_index,
    type_values,
)

_FORMAT_HELP = "Output format. `auto` (default) renders a table on a TTY and JSON when piped."


# ----- error handling + suggestions -----


def _exit_code_for(exc: YenteError) -> int:
    """Map a ``YenteError`` to the CLI exit code."""
    if isinstance(exc, TransportError):
        return 4
    if isinstance(exc, (APIError, ConfigurationError)):
        return 3
    return 3


def _emit_yente_error(ctx: typer.Context, exc: YenteError) -> None:
    """Render a YenteError as a clean one-line message to stderr.

    ``-v`` / ``--verbose`` (read from ``ctx.obj.verbose``) shows the full
    chain via the active Python traceback handler instead.
    """
    config: CliConfig | None = ctx.obj if isinstance(ctx.obj, CliConfig) else None
    if config and config.verbose:
        raise exc
    if isinstance(exc, APIError):
        typer.echo(f"error: {type(exc).__name__} ({exc.status_code}): {exc.detail}", err=True)
    else:
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)


@contextlib.contextmanager
def _with_client(ctx: typer.Context) -> Iterator[Client]:
    """Context manager that builds a Client and converts SDK errors to clean exits.

    Wraps each endpoint command. ``YenteError`` subclasses are rendered as a
    one-line stderr message and re-raised as ``typer.Exit`` with the right
    exit code. ``-v`` / ``--verbose`` short-circuits to the original
    traceback for debugging.
    """
    config: CliConfig = ctx.obj
    try:
        with config.make_client() as client:
            yield client
    except YenteError as exc:
        _emit_yente_error(ctx, exc)
        raise typer.Exit(code=_exit_code_for(exc)) from exc


def _suggest_schema(name: str) -> str | None:
    """Return the closest valid FtM schema name, or ``None`` if no close match."""
    valid = list(model["schemata"].keys())
    matches = difflib.get_close_matches(name, valid, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _suggest_property(schema: str, prop_name: str) -> str | None:
    """Return the closest valid property name for ``schema``, or ``None``."""
    try:
        valid = list(iter_properties(schema))
    except KeyError:
        return None
    matches = difflib.get_close_matches(prop_name, valid, n=1, cutoff=0.6)
    return matches[0] if matches else None


# ----- shared helpers -----


def _read_model_version() -> str:
    """Return the bundled ``model.json``'s upstream ``version`` (e.g. ``"4.8.4"``)."""
    model_path = Path(__file__).resolve().parent.parent / "schemas" / "model.json"
    try:
        raw: dict[str, Any] = json.loads(model_path.read_text())
    except (OSError, ValueError):
        return "unknown"
    version = raw.get("version")
    return str(version) if version else "unknown"


def _client_version() -> str:
    """Resolve the installed ``yente-client`` version, with a fallback."""
    try:
        return version("yente-client")
    except PackageNotFoundError:
        return "0.0.0+unknown"


# ----- status (the catch-all "is everything wired up?" view) -----


def status_command(
    ctx: typer.Context,
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Show the client setup and live server state at a glance.

    Reports the installed CLI version (drawn from the ``yente-client``
    package), the bundled FtM model snapshot, the API URL, the masked API
    key, both liveness and readiness probes (with timing), and the datasets
    the server has actually loaded
    (``load: true`` entries in the dataset listing — typically one or two
    top-level datasets / collections; their members ride along in the
    index without independent freshness).

    Use this as the canonical "is everything set up correctly?" command. With
    ``-f json`` the output is parser-friendly for LLM agents.
    """
    config: CliConfig = ctx.obj
    summary = _gather_status(config)

    fmt = resolve_format(format_)
    if fmt in (Format.JSON, Format.JSONL):
        print_json(summary)
    else:
        _render_status_table(summary)


def _gather_status(config: CliConfig) -> dict[str, Any]:
    """Probe the server, fetch the dataset listing, and assemble the summary dict.

    Probe failures don't raise — they're reported as ``status="error"`` in
    the relevant field so ``status`` still produces a useful diagnostic when
    the server is partially up.
    """
    suffix = config.api_key[-4:] if config.api_key and len(config.api_key) >= 4 else None

    liveness: dict[str, Any] = {"status": "error", "detail": "client not built"}
    readiness: dict[str, Any] = {"status": "error", "detail": "client not built"}
    loaded: list[Dataset] = []
    datasets_error: str | None = None

    try:
        with config.make_client() as client:
            liveness = _timed_probe(client, "/healthz")
            readiness = _timed_probe(client, "/readyz")
            try:
                listing = client.datasets()
                loaded = [d for d in listing.datasets if d.load]
            except YenteError as exc:
                datasets_error = _format_error(exc)
    except YenteError as exc:
        # The Client itself couldn't even be used (transport error before any
        # request reaches the server). Fall through with the empty defaults.
        datasets_error = _format_error(exc)
        liveness = _err_dict(exc)
        readiness = _err_dict(exc)

    current = sum(1 for d in loaded if d.index_current)
    stale = len(loaded) - current

    summary: dict[str, Any] = {
        "client": {
            "version": _client_version(),
            "model_version": _read_model_version(),
        },
        "api": {
            "url": config.base_url,
            "auth": {"present": bool(suffix), "key_suffix": suffix},
            "liveness": liveness,
            "readiness": readiness,
        },
        "loaded": [
            {
                "name": d.name,
                "title": d.title,
                "version": d.version,
                "index_version": d.index_version,
                "current": bool(d.index_current),
                "is_collection": bool(d.children),
            }
            for d in loaded
        ],
        "summary": {"total": len(loaded), "current": current, "stale": stale},
    }
    if datasets_error is not None:
        summary["datasets_error"] = datasets_error
    return summary


def _timed_probe(client: Client, path: str) -> dict[str, Any]:
    """Time a GET to ``path``; return ``{status, elapsed_ms}`` or an error dict."""
    start = time.monotonic()
    try:
        result = client._request("GET", path)
        elapsed_ms = round((time.monotonic() - start) * 1000)
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        return {"status": status, "elapsed_ms": elapsed_ms}
    except YenteError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        d = _err_dict(exc)
        d["elapsed_ms"] = elapsed_ms
        return d


def _err_dict(exc: YenteError) -> dict[str, Any]:
    if isinstance(exc, APIError):
        return {"status": "error", "code": exc.status_code, "detail": exc.detail}
    return {"status": "error", "error": type(exc).__name__, "detail": str(exc)}


def _format_error(exc: YenteError) -> str:
    if isinstance(exc, APIError):
        return f"{type(exc).__name__} ({exc.status_code}): {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


def _render_status_table(summary: dict[str, Any]) -> None:
    """Render the status summary in the TTY-friendly form."""
    client_info = summary["client"]
    api = summary["api"]
    typer.echo(f"yente-cli {client_info['version']}")
    typer.echo(f"Bundled FtM model: v{client_info['model_version']}")
    typer.echo("")
    typer.echo(f"API:        {api['url']}")
    auth = api["auth"]
    if auth["present"]:
        typer.echo(f"Auth:       ApiKey ···· {auth['key_suffix']}")
    else:
        typer.echo("Auth:       (no API key set — export OPENSANCTIONS_API_KEY)")
    typer.echo(f"Liveness:   {_format_probe(api['liveness'])}")
    typer.echo(f"Readiness:  {_format_probe(api['readiness'])}")
    typer.echo("")

    loaded = summary["loaded"]
    if not loaded:
        if summary.get("datasets_error"):
            typer.echo(f"Loaded: (datasets unavailable — {summary['datasets_error']})")
        else:
            typer.echo("Loaded: (none — server has no datasets with load=true)")
        return

    rows = [
        [
            d["name"],
            (d["title"] or "")[:50],
            f"v={d['version'] or '-'}",
            "current" if d["current"] else "STALE",
        ]
        for d in loaded
    ]
    print_table(rows, headers=["name", "title", "version", "status"], title="Loaded datasets")
    s = summary["summary"]
    typer.echo(f"\n{s['total']} loaded, {s['current']} current, {s['stale']} stale")


def _format_probe(probe: dict[str, Any]) -> str:
    """Format a probe result for the TTY view."""
    status = probe.get("status", "?")
    elapsed = probe.get("elapsed_ms")
    if status == "ok":
        return f"ok    ({elapsed} ms)" if elapsed is not None else "ok"
    if status == "error":
        code = probe.get("code")
        detail = probe.get("detail", probe.get("error", "error"))
        head = f"ERROR {code}" if code else "ERROR"
        return f"{head} — {detail}"
    return f"{status}    ({elapsed} ms)" if elapsed is not None else status


# ----- datasets / algorithms -----


def _format_coverage(c: DataCoverage) -> str:
    """Summarise a dataset's coverage as one line for the metadata table."""
    parts: list[str] = []
    if c.start or c.end:
        parts.append(f"{c.start or '?'} – {c.end or 'now'}")
    if c.countries:
        n = len(c.countries)
        parts.append(f"{n} country" if n == 1 else f"{n} countries")
    if c.frequency and c.frequency != "unknown":
        parts.append(f"updated {c.frequency}")
    return ", ".join(parts) or "(none)"


def _format_publisher(p: DataPublisher) -> str:
    """Summarise a dataset's publisher as one line for the metadata table."""
    label = p.name
    location = p.country_label or p.country
    if location:
        label = f"{label} ({location})"
    if p.official:
        label = f"{label} — official source"
    return label


def datasets_command(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None,
        help="Optional dataset name. When given, show the full metadata for that dataset.",
    ),
    current_only: bool = typer.Option(
        False, "--current-only", help="Only show datasets whose index is current."
    ),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List the indexed datasets and their freshness state.

    Without an argument, lists every indexed dataset (use ``-d`` / ``--datasets``
    on ``search`` / ``match`` with these names). With a dataset name, fetches
    the full catalog and emits just that one entry's metadata — convenient
    for inspecting versions, freshness, and children of a specific dataset
    without piping through ``jq``.
    """
    with _with_client(ctx) as client:
        listing = client.datasets()

    if name is not None:
        match = next((d for d in listing.datasets if d.name == name), None)
        if match is None:
            suggestion = difflib.get_close_matches(
                name, [d.name for d in listing.datasets], n=1, cutoff=0.6
            )
            hint = f" Did you mean: {suggestion[0]}?" if suggestion else ""
            typer.echo(
                f"error: Unknown dataset {name!r}.{hint} Run `yente-cli datasets` for the list.",
                err=True,
            )
            raise typer.Exit(code=2)
        fmt = resolve_format(format_)
        if fmt in (Format.JSON, Format.JSONL):
            print_json(match)
        else:
            current_set = set(listing.current)
            # Core identity/freshness fields are always shown; the richer
            # descriptive fields are appended only when the entry carries them,
            # so sparse source datasets stay compact while the indexed
            # collection renders its full metadata.
            rows = [
                ["name", match.name],
                ["title", match.title or ""],
                ["version", match.version or ""],
                ["index_version", match.index_version or ""],
                ["current", "yes" if match.name in current_set else "no"],
                ["load", "yes" if match.load else "no"],
            ]
            if match.summary:
                rows.append(["summary", match.summary.strip()])
            if match.category:
                rows.append(["category", match.category])
            if match.entity_count is not None:
                rows.append(["entity_count", f"{match.entity_count:,}"])
            if match.thing_count is not None:
                rows.append(["thing_count", f"{match.thing_count:,}"])
            if match.coverage is not None:
                rows.append(["coverage", _format_coverage(match.coverage)])
            if match.publisher is not None:
                rows.append(["publisher", _format_publisher(match.publisher)])
            if match.updated_at is not None:
                rows.append(["updated_at", match.updated_at.date().isoformat()])
            if match.last_export is not None:
                rows.append(["last_export", match.last_export.date().isoformat()])
            if match.url:
                rows.append(["url", match.url])
            if match.tags:
                rows.append(["tags", ", ".join(match.tags)])
            if match.children:
                rows.append(["children", ", ".join(match.children)])
            if match.deprecated:
                rows.append(["deprecated", match.deprecation or "yes"])
            print_table(rows, headers=["field", "value"], title=match.name)
        return

    datasets = listing.datasets
    if current_only:
        current = set(listing.current)
        datasets = [d for d in datasets if d.name in current]

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(listing if not current_only else {"datasets": datasets})
    elif fmt == Format.JSONL:
        print_jsonl(datasets)
    else:
        current_set = set(listing.current)
        rows = [
            [
                d.name,
                d.title or "",
                d.version or "",
                "yes" if d.name in current_set else "no",
            ]
            for d in datasets
        ]
        print_table(rows, headers=["name", "title", "version", "current"])


def _format_issuer(issuer: ProgramIssuer) -> str:
    """Summarise a program's issuer as one line for the metadata table."""
    label = issuer.name or issuer.acronym or "(unknown)"
    if issuer.name and issuer.acronym:
        label = f"{label} ({issuer.acronym})"
    if issuer.territory:
        label = f"{label} — {issuer.territory}"
    return label


def programs_command(
    ctx: typer.Context,
    key: str | None = typer.Argument(
        None,
        help="Optional program key (a `programId` value, e.g. `US-RUSHAR`). "
        "When given, show that program's full metadata.",
    ),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List sanctions programs, or show one program's metadata.

    Programs are the policy regimes sanctions designations are made under;
    sanctioned entities name theirs in the `programId` property. Use this
    to resolve those codes into a title, issuer, and policy summary. The
    catalog is a public OpenSanctions data artifact — the fetch works
    regardless of ``--base-url`` and needs no API key.
    """
    with _with_client(ctx) as client:
        listing = client.programs()

    if key is not None:
        match = next((p for p in listing.data if p.key == key or key in p.aliases), None)
        if match is None:
            suggestion = difflib.get_close_matches(
                key, [p.key for p in listing.data], n=1, cutoff=0.6
            )
            hint = f" Did you mean: {suggestion[0]}?" if suggestion else ""
            typer.echo(
                f"error: Unknown program {key!r}.{hint} Run `yente-cli programs` for the list.",
                err=True,
            )
            raise typer.Exit(code=2)
        fmt = resolve_format(format_)
        if fmt in (Format.JSON, Format.JSONL):
            print_json(match)
        else:
            rows: list[list[Any]] = [["key", match.key], ["title", match.title or ""]]
            if match.issuer is not None:
                rows.append(["issuer", _format_issuer(match.issuer)])
            if match.summary:
                rows.append(["summary", match.summary.strip()])
            if match.dataset:
                rows.append(["dataset", match.dataset])
            if match.aliases:
                rows.append(["aliases", ", ".join(match.aliases)])
            if match.target_territories:
                rows.append(["target_territories", ", ".join(match.target_territories)])
            if match.measures:
                rows.append(["measures", ", ".join(match.measures)])
            if match.url:
                rows.append(["url", match.url])
            print_table(rows, headers=["field", "value"], title=match.key)
        return

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(listing)
    elif fmt == Format.JSONL:
        print_jsonl(listing.data)
    else:
        table_rows = [
            [
                p.key,
                _truncate(p.title or "", 60),
                p.issuer.territory if p.issuer else "",
            ]
            for p in listing.data
        ]
        print_table(
            table_rows,
            headers=["key", "title", "territory"],
            title=f"{len(listing.data)} program(s)",
        )


def statements_command(
    ctx: typer.Context,
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Restrict to one dataset."),
    canonical_id: str | None = typer.Option(
        None,
        "--canonical-id",
        "-c",
        help="Post-deduplication entity ID (e.g. `NK-...`). The typical choice.",
    ),
    entity_id: str | None = typer.Option(
        None,
        "--entity-id",
        help=(
            "Source entity ID (e.g. `ofac-1234`). Returns only the fragment from "
            "that one source; usually not what you want — see --canonical-id."
        ),
    ),
    prop: str | None = typer.Option(None, "--prop", help="Property name (e.g. `alias`)."),
    value: str | None = typer.Option(None, "--value", help="Exact property value."),
    schema: str | None = typer.Option(None, "--schema", "-s", help="Entity schema name."),
    sort: list[str] | None = typer.Option(
        None, "--sort", help="Sort key(s). Repeatable. Default: canonical_id, prop."
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Page size (server-capped)."),
    offset: int = typer.Option(0, "--offset", help="Pagination offset."),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Read raw entity data as statements (OpenSanctions API only).

    Statements track lineage: each row records a single
    ``(entity_id, prop, value)`` claim plus the dataset that asserted it
    and when. Useful for diagnostics — finding deduplication issues,
    investigating where a value came from, auditing data quality.

    Use ``-c`` / ``--canonical-id`` to fetch all statements for an entity
    (the ID returned by ``match`` / ``search`` / ``fetch``). This is the
    typical choice: it returns every source fragment that was
    deduplicated into the canonical entity.

    Use ``--entity-id`` only when you want statements from one specific
    source: it returns the pre-deduplication fragment as that source
    asserted it. The same person on five sanctions lists has five distinct
    ``entity_id`` values but one ``canonical_id``.

    Available only on the OpenSanctions API. yente does not ship the
    backing data store and returns 404 here.
    """
    with _with_client(ctx) as client:
        response = client.statements(
            dataset=dataset,
            entity_id=entity_id,
            canonical_id=canonical_id,
            prop=prop,
            value=value,
            schema=schema,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(response)
    elif fmt == Format.JSONL:
        print_jsonl(response.results)
    else:
        rows = [
            [
                s.canonical_id,
                s.prop,
                _truncate(s.value, 50),
                s.dataset,
                s.first_seen.date().isoformat() if s.first_seen else "",
            ]
            for s in response.results
        ]
        print_table(
            rows,
            headers=["canonical_id", "prop", "value", "dataset", "first_seen"],
            title=f"{len(response.results)} of {response.total.value} statement(s)",
        )

    if not response.results:
        raise typer.Exit(code=1)


def algorithms_command(
    ctx: typer.Context,
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List enabled matching algorithms and the server's "best" pick.

    Use the ``name`` from this list with ``-a`` / ``--algorithm`` on ``match``.
    ``best`` is the server's canonical default — passing ``-a best`` is stable
    across algorithm version bumps.
    """
    with _with_client(ctx) as client:
        algorithms = client.algorithms()

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(algorithms)
    elif fmt == Format.JSONL:
        print_jsonl(algorithms.algorithms)
    else:
        rows = [
            [a.name, "★" if a.name == algorithms.best else "", a.description or ""]
            for a in algorithms.algorithms
        ]
        print_table(
            rows,
            headers=["name", "best", "description"],
            title=f"default={algorithms.default!r}  best={algorithms.best!r}",
        )


# ----- fetch -----


def fetch_command(
    ctx: typer.Context,
    entity_id: str = typer.Argument(..., help="Entity ID returned by `match` or `search`."),
    no_nested: bool = typer.Option(
        False,
        "--no-nested",
        help="Skip inline adjacent entities (sanctions, ownership, family, ...).",
    ),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Fetch a single entity by ID.

    Follows ``308`` redirects when the supplied ID is a referent of a canonical
    entity — if the returned ``id`` differs from the one you passed, the entity
    was merged; update stored references. The default (with ``nested=true``)
    returns related entities inline; pass ``--no-nested`` for a lighter
    response.
    """
    with _with_client(ctx) as client:
        entity = client.fetch(entity_id, nested=not no_nested)

    fmt = resolve_format(format_)
    if fmt in (Format.JSON, Format.JSONL):
        print_json(entity)
    else:
        _print_entity_summary(entity)


def search_command(
    ctx: typer.Context,
    q: str = typer.Argument(..., help="Free-text query (name fragment, identifier, ...)."),
    datasets: list[str] | None = typer.Option(
        None,
        "--datasets",
        "-d",
        help="Restrict to dataset(s). Repeatable. Default: `default` (combined dataset).",
    ),
    schema: str | None = typer.Option(
        None, "--schema", "-s", help="Restrict to one schema, e.g. `Person`, `Company`."
    ),
    topics: list[str] | None = typer.Option(
        None,
        "--topics",
        "-t",
        help="Filter by risk topic(s), e.g. `sanction`, `role.pep`. Repeatable.",
    ),
    countries: list[str] | None = typer.Option(
        None, "--countries", help="Filter by country code(s) (see `ref countries`). Repeatable."
    ),
    filter_: list[str] | None = typer.Option(
        None,
        "--filter",
        help="Property filter `field:value` (e.g. `properties.birthDate:1965`). Repeatable.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-l", help="Results per page (server default 10)."
    ),
    offset: int = typer.Option(0, "--offset", help="Pagination offset."),
    sort: list[str] | None = typer.Option(None, "--sort", help="Sort key(s). Repeatable."),
    fuzzy: bool = typer.Option(False, "--fuzzy", help="Allow fuzzy query syntax."),
    simple: bool = typer.Option(False, "--simple", help="Use the simple-query parser."),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Free-text search across one or more datasets — for building user-facing search UIs.

    Use this when you need to back a search box, autocomplete, or browse
    interface where a human is typing into the input. Results are ranked by
    text relevance and returned as plain entities (no score).

    For ANY matching task — identifying whether some input corresponds to
    a known entity — use `match`, even with partial input. `search` is not
    a substitute for `match` on incomplete data.

    Exits 1 (no results) when the query returns zero hits, so shell scripts
    can gate on `yente-cli search … && …`.
    """
    search_kwargs: dict[str, Any] = {}
    if datasets:
        search_kwargs["datasets"] = datasets
    if schema:
        search_kwargs["schema"] = schema
    if topics:
        search_kwargs["topics"] = topics
    if countries:
        search_kwargs["countries"] = countries
    if filter_:
        search_kwargs["filter"] = filter_

    with _with_client(ctx) as client:
        response = client.search(
            q,
            limit=limit,
            offset=offset,
            sort=sort or None,
            fuzzy=fuzzy,
            simple=simple,
            **search_kwargs,
        )

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(response)
    elif fmt == Format.JSONL:
        print_jsonl(response.results)
    else:
        rows = [
            [
                r.id,
                r.caption,
                r.schema_,
                ", ".join(r.datasets[:3]),
                ", ".join(t for t in r.properties.get("topics", []) if isinstance(t, str)),
            ]
            for r in response.results
        ]
        print_table(
            rows,
            headers=["id", "caption", "schema", "datasets", "topics"],
            title=f"total={response.total.value}{'+' if response.total.relation == 'gte' else ''}",
        )

    if not response.results:
        raise typer.Exit(code=1)


def _print_entity_summary(entity: Entity) -> None:
    """Render an entity as a key-value summary table for TTY output.

    Full property detail is too wide for a table; users wanting it should use
    ``-f json``. This summary is the at-a-glance view.
    """
    topics = [t for t in entity.properties.get("topics", []) if isinstance(t, str)]
    rows: list[list[Any]] = [
        ["id", entity.id],
        ["caption", entity.caption],
        ["schema", entity.schema_],
        ["target", "yes" if entity.target else "no"],
        ["datasets", ", ".join(entity.datasets)],
        ["topics", ", ".join(topics)],
    ]
    print_table(rows, headers=["field", "value"], title=entity.caption)


# ----- match -----


def match_command(
    ctx: typer.Context,
    schema: str = typer.Option(
        ...,
        "--schema",
        "-s",
        help="FtM schema name (Person, Company, Vessel, ...). Run `ref schemas --matchable`.",
    ),
    properties: list[str] | None = typer.Option(
        None,
        "--property",
        "-p",
        help=(
            "Set a property, repeatable: `-p firstName=Aleksandr -p lastName=Zacharov`. "
            "Same key passed twice produces a multi-value property. "
            "Names are FtM camelCase (e.g. `birthDate`, not `birth_date`)."
        ),
    ),
    from_file: Path | None = typer.Option(
        None,
        "--from-file",
        "-i",
        help='JSON file with shape {"schema": "...", "properties": {...}}. '
        "`-s` and `-p` flags override values from the file.",
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
    algorithm: str | None = typer.Option(
        None,
        "--algorithm",
        "-a",
        help='Matching algorithm. "best" is stable across versions; see `algorithms`.',
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-l", help="Max results per query (server default 5)."
    ),
    changed_since: str | None = typer.Option(
        None,
        "--changed-since",
        help="Only match entities updated since this ISO 8601 date.",
    ),
    exclude_entities: list[str] | None = typer.Option(
        None, "--exclude-entities", help="Exclude these entity IDs from results. Repeatable."
    ),
    exclude_schemata: list[str] | None = typer.Option(
        None, "--exclude-schemata", help="Exclude these schemas from results. Repeatable."
    ),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Match a single entity (built from `-p` flags or `--from-file`) against a dataset.

    Query-by-example: describe the entity in as much detail as you can and
    the API returns ranked, scored candidates with explanations. This is the
    canonical command for ANY matching / record-linkage task, including when
    you only have partial input (just a name, name + country, etc.). Don't
    reach for `search` when the input is sparse — `search` is for human-typed
    search UIs, not for matching.

    Exits 1 if no results returned, so shell scripts can gate on
    `yente-cli match … && …`.
    """
    entity = _build_entity_input(schema, properties or [], from_file)

    match_kwargs: dict[str, Any] = {}
    if datasets:
        match_kwargs["datasets"] = datasets
    if topics:
        match_kwargs["topics"] = topics
    if changed_since:
        match_kwargs["changed_since"] = changed_since
    if exclude_entities:
        match_kwargs["exclude_entities"] = exclude_entities
    if exclude_schemata:
        match_kwargs["exclude_schemata"] = exclude_schemata

    with _with_client(ctx) as client:
        response = client.match(
            entity,
            threshold=threshold,
            algorithm=algorithm,
            limit=limit,
            **match_kwargs,
        )

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(response)
    elif fmt == Format.JSONL:
        print_jsonl(response.results)
    else:
        rows = [
            [
                f"{r.score:.2f}",
                "✓" if r.match else "",
                r.id,
                r.caption,
                r.schema_,
                ", ".join(r.datasets[:3]),
                ", ".join(t for t in r.properties.get("topics", []) if isinstance(t, str)),
            ]
            for r in response.results
        ]
        print_table(
            rows,
            headers=["score", "match", "id", "caption", "schema", "datasets", "topics"],
            title=(
                f"total={response.total.value}"
                f"{'+' if response.total.relation == 'gte' else ''} "
                f"threshold-passing={len(response.matches)}"
            ),
        )

    if not response.results:
        raise typer.Exit(code=1)


def _build_entity_input(schema: str, properties: list[str], from_file: Path | None) -> EntityInput:
    """Construct a per-schema entity from CLI inputs.

    Properties from ``--from-file`` are loaded first; ``-p KEY=VALUE`` flags
    are then layered on top (later wins on first set, same-key repeats append).
    The resulting dict is passed to the per-schema class — Pydantic enforces
    property-name validity via ``extra="forbid"``.
    """
    schema_cls = getattr(entities, schema, None)
    if schema_cls is None or not isinstance(schema_cls, type):
        suggestion = _suggest_schema(schema)
        hint = f" Did you mean: {suggestion}?" if suggestion else ""
        typer.echo(
            f"error: Unknown schema {schema!r}.{hint} Run `yente-cli ref schemas` for the list.",
            err=True,
        )
        raise typer.Exit(code=2)

    if not is_matchable_schema(schema):
        options = ", ".join(matchable_schemata()[:6])
        typer.echo(
            f"error: Schema {schema!r} is not a matchable target for `match`. "
            f"Try a matchable schema like {options}, … "
            f"(run `yente-cli ref schemas --matchable` for the full list).",
            err=True,
        )
        raise typer.Exit(code=2)

    props: dict[str, list[str]] = {}
    if from_file is not None:
        try:
            raw: dict[str, Any] = json.loads(from_file.read_text())
        except (OSError, ValueError) as exc:
            typer.echo(f"error: could not read {from_file}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        file_props = raw.get("properties") or {}
        for key, value in file_props.items():
            props[key] = value if isinstance(value, list) else [value]

    for spec in properties:
        if "=" not in spec:
            typer.echo(
                f"error: --property must be KEY=VALUE; got {spec!r}",
                err=True,
            )
            raise typer.Exit(code=2)
        key, value = spec.split("=", 1)
        props.setdefault(key, []).append(value)

    try:
        return cast(EntityInput, schema_cls(**props))
    except ValidationError as exc:
        # If any error is a known-extra-fields-forbidden case, try to suggest
        # the closest valid property name for the agent reading the message.
        suggestions: list[str] = []
        for err in exc.errors():
            if err.get("type") == "extra_forbidden" and err.get("loc"):
                bad_prop = str(err["loc"][0])
                close = _suggest_property(schema, bad_prop)
                if close:
                    suggestions.append(f"{bad_prop!r} → did you mean {close!r}?")
        tail = " " + "; ".join(suggestions) if suggestions else ""
        typer.echo(f"error: invalid {schema} entity: {exc}.{tail}", err=True)
        raise typer.Exit(code=2) from exc


# ----- ref (offline FtM model introspection) -----


def ref_schemas_command(
    matchable_only: bool = typer.Option(
        False,
        "--matchable",
        help="Filter to schemas that can be used as `match` query targets.",
    ),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List every FtM schema in the bundled model.

    Offline — no API call, no API key needed. Use this to discover what `-s`
    values you can pass to `match` or `search`. For details on one schema,
    run ``ref schema NAME``.
    """
    entries = schema_index(matchable_only=matchable_only)

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(entries)
    elif fmt == Format.JSONL:
        print_jsonl(entries)
    else:
        rows = [
            [
                e["name"],
                "✓" if e.get("matchable") else "",
                "edge" if e.get("edge") else "",
                ", ".join(e.get("extends", [])),
                _truncate(e.get("description", ""), 60),
            ]
            for e in entries
        ]
        print_table(
            rows,
            headers=["schema", "matchable", "flags", "extends", "description"],
            title=f"{len(entries)} schema(s)",
        )


def ref_schema_command(
    name: str = typer.Argument(..., help="Schema name, e.g. `Person`, `Company`, `Vessel`."),
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """Show one schema's properties, types, and inheritance.

    Includes inherited properties (walks the ancestor chain), so what you
    see is the complete set of fields you can pass via `-p KEY=VALUE` to
    `match`. With ``-f json`` the output is LLM-friendly and includes per-
    property type and `deprecated` flags.
    """
    if not has_schema(name):
        suggestion = _suggest_schema(name)
        hint = f" Did you mean: {suggestion}?" if suggestion else ""
        typer.echo(
            f"error: Unknown schema {name!r}.{hint} Run `yente-cli ref schemas` for the list.",
            err=True,
        )
        raise typer.Exit(code=2)

    summary = describe_schema(name)
    properties = summary["properties"]

    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(summary)
    elif fmt == Format.JSONL:
        # One property per line — useful for agents iterating over the prop list.
        print_jsonl(properties)
    else:
        typer.echo(name)
        if summary.get("description"):
            typer.echo(summary["description"])
        typer.echo("")
        typer.echo(f"  matchable:  {'yes' if summary.get('matchable') else 'no'}")
        typer.echo(f"  extends:    {', '.join(summary.get('extends', [])) or '(none)'}")
        typer.echo(f"  featured:   {', '.join(summary.get('featured', [])) or '(none)'}")
        if summary.get("edge"):
            typer.echo("  edge:       yes")
        typer.echo("")
        rows = [
            [
                p["name"],
                p["type"],
                "✓" if p.get("matchable") else "",
                _truncate(p.get("description", ""), 50),
            ]
            for p in properties
        ]
        print_table(
            rows,
            headers=["property", "type", "matchable", "description"],
            title=f"{len(properties)} settable property/properties (own + inherited)",
        )
        relations = summary.get("relations", [])
        if relations:
            typer.echo("")
            rel_rows = [[r["name"], r.get("range", ""), r.get("reverse", "")] for r in relations]
            print_table(
                rel_rows,
                headers=["relation", "points to", "reverse"],
                title=f"{len(relations)} relationship edge(s) — traverse via the adjacency API",
            )
        typer.echo("")
        typer.echo(
            "`matchable` is the FtM model's per-property flag. It is NOT a 'useful for matching'"
        )
        typer.echo("indicator — send every property you have. Non-matchable properties (firstName,")
        typer.echo(
            "lastName, weakAlias, gender, ...) feed dedicated matcher features and score just"
        )
        typer.echo("as directly as matchable ones — through different code paths.")


def ref_topics_command(
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List the Topic enum (the canonical risk tags an entity can carry).

    Use these names with `-t` / `--topics` on `match` and `search`. Sourced
    from ``model.types["topic"].values`` in the bundled snapshot.
    """
    entries = [
        {"name": name, "label": label} for name, label in sorted(type_values("topic").items())
    ]
    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(entries)
    elif fmt == Format.JSONL:
        print_jsonl(entries)
    else:
        rows = [[e["name"], e["label"]] for e in entries]
        print_table(rows, headers=["topic", "label"], title=f"{len(entries)} topic(s)")


def ref_countries_command(
    format_: Format = typer.Option(Format.AUTO, "--format", "-f", help=_FORMAT_HELP),
) -> None:
    """List valid country codes for the ``country`` property type.

    Use these with ``--countries`` on ``search`` or as values on
    country-typed properties (``country``, ``nationality``, ``birthCountry``,
    ``jurisdiction``, …). Sourced from ``model.types["country"].values``.
    """
    entries = [
        {"code": code, "name": name} for code, name in sorted(type_values("country").items())
    ]
    fmt = resolve_format(format_)
    if fmt == Format.JSON:
        print_json(entries)
    elif fmt == Format.JSONL:
        print_jsonl(entries)
    else:
        rows = [[e["code"], e["name"]] for e in entries]
        print_table(rows, headers=["code", "name"], title=f"{len(entries)} country code(s)")


def _truncate(text: str, max_len: int) -> str:
    """Truncate a string with an ellipsis if it exceeds `max_len`."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ----- registration -----


def register(app: typer.Typer) -> None:
    """Attach all subcommands to ``app``."""
    app.command("status", epilog=_STATUS_EPILOG)(status_command)
    app.command("datasets", epilog=_DATASETS_EPILOG)(datasets_command)
    app.command("programs", epilog=_PROGRAMS_EPILOG)(programs_command)
    app.command("statements", epilog=_STATEMENTS_EPILOG)(statements_command)
    app.command("algorithms", epilog=_ALGORITHMS_EPILOG)(algorithms_command)
    app.command("fetch", epilog=_FETCH_EPILOG)(fetch_command)
    app.command("search", epilog=_SEARCH_EPILOG)(search_command)
    app.command("match", epilog=_MATCH_EPILOG)(match_command)

    ref_app = typer.Typer(
        name="ref",
        help="Inspect the bundled FtM model (offline; no API key required).",
        no_args_is_help=True,
    )
    ref_app.command("schemas", epilog=_REF_SCHEMAS_EPILOG)(ref_schemas_command)
    ref_app.command("schema", epilog=_REF_SCHEMA_EPILOG)(ref_schema_command)
    ref_app.command("topics")(ref_topics_command)
    ref_app.command("countries")(ref_countries_command)
    app.add_typer(ref_app, name="ref")


# ----- epilogs (worked examples + output shape notes for agent use) -----

_STATUS_EPILOG = """\
EXAMPLES:
  yente-cli status                          # TTY-friendly summary
  yente-cli status -f json                  # parseable summary for agents
  yente-cli status --base-url http://...    # check a yente instance

OUTPUT (with -f json):
  {
    "client": {"version": ..., "model_version": ...},
    "api": {
      "url": ...,
      "auth": {"present": bool, "key_suffix": "..." | null},
      "liveness":  {"status": "ok" | "error", "elapsed_ms": int},
      "readiness": {"status": "ok" | "error", "elapsed_ms": int}
    },
    "loaded": [
      {"name", "title", "version", "index_version",
       "current": bool, "is_collection": bool}, ...
    ],
    "summary": {"total": int, "current": int, "stale": int}
  }

Use this as the first command when wiring up a new environment: it
verifies the API key, base URL, liveness, readiness, and what
datasets the server is actually indexing. Replaces the older
`version` and `readyz` subcommands.

The full dataset listing (every dataset visible to the server, loaded
or not) is available via `yente-cli datasets`.
"""

_DATASETS_EPILOG = """\
EXAMPLES:
  yente-cli datasets                       # all datasets, table view
  yente-cli datasets -f json               # full DatasetsResponse as JSON
  yente-cli datasets --current-only        # skip stale-index datasets
  yente-cli datasets us_ofac_sdn           # full metadata for one dataset
  yente-cli datasets sanctions -f json     # one dataset, JSON for piping

OUTPUT (no argument, with -f json):
  {datasets: [{name, title, version, index_current}, ...],
   current: [str], outdated: [str], index_stale: bool}

OUTPUT (with a dataset name + -f json):
  Dataset object: {name, title, description, version, index_version,
                   index_current, load, children, entities_url}

The `name` field is what you pass to `-d` / `--datasets` on match/search.
"""

_PROGRAMS_EPILOG = """\
EXAMPLES:
  yente-cli programs                       # every program, table view
  yente-cli programs -f jsonl              # one program per line
  yente-cli programs US-RUSHAR             # full metadata for one program
  yente-cli programs EU-UKR -f json        # one program, JSON for piping

OUTPUT (no argument, with -f json):
  {data: [{key, title, url, summary, dataset, issuer: {name, acronym,
   organisation, territory}, aliases, target_territories, measures}, ...]}

The `key` is what sanctioned entities carry in their `programId` property —
use this command to resolve codes seen in match/search/fetch results into
the program's title, issuer, and policy summary.

Fetched from https://data.opensanctions.org/meta/programs.json (a public
artifact, no API key needed), independent of --base-url.
"""

_STATEMENTS_EPILOG = """\
EXAMPLES:
  yente-cli statements -c NK-aU5y... -f json              # all lineage for a canonical entity
  yente-cli statements -c NK-aU5y... --prop alias         # narrow to one property
  yente-cli statements --entity-id ofac-1234              # source fragment (pre-deduplication)
  yente-cli statements --value "Acme LLC" -f jsonl        # find every claim of a value

CANONICAL VS SOURCE ID:
  -c / --canonical-id is the typical choice — it returns every source
  fragment that was deduplicated into one canonical entity. Pass the ID
  you got from `match` / `search` / `fetch`. --entity-id returns only
  what one specific source asserted; use it for source-level audits.

GRAPH TRAVERSAL:
  Entity-typed properties carry the referenced entity's canonical_id as
  `value` and the source entity_id as `original_value`. Filter on these
  to traverse the graph in reverse. To find every Sanction on Putin
  (Q7747):
    yente-cli statements --schema Sanction --prop entity --value Q7747

OUTPUT (with -f json):
  StatementsResponse: {results: [Statement, ...], total: {value, relation},
                       limit, offset}
  Each Statement: {id, entity_id, canonical_id, prop, prop_type, schema,
                   value, original_value, dataset, lang, first_seen, last_seen}

EXIT CODES:
  0  ≥1 row returned
  1  zero rows
  3  API error (incl. 404 on yente — the endpoint is OpenSanctions-only)
  4  network/transport error

For background on the statement-based data model see
https://www.opensanctions.org/docs/statements/
"""

_ALGORITHMS_EPILOG = """\
EXAMPLES:
  yente-cli algorithms
  yente-cli algorithms -f json

OUTPUT (with -f json):
  {algorithms: [{name, description, docs}], default: str, best: str}

Pass `best` to `match -a best` for the server's recommended algorithm —
stable across version bumps.
"""

_FETCH_EPILOG = """\
EXAMPLES:
  yente-cli fetch NK-aU5ybkbRFJucf8YMwsJvDw                # summary table
  yente-cli fetch <id> -f json                              # full Entity as JSON
  yente-cli fetch <id> --no-nested                          # skip adjacent entities

OUTPUT (with -f json):
  Entity object: {id, caption, schema, properties: {<name>: [...]}, datasets,
                  referents, target, first_seen, last_seen, last_change}

Property values are always lists. With nested=true (default), entity-valued
properties (sanctions, ownerships, family, ...) inline as nested Entity objects.

MERGED IDS:
  308 redirects are followed when the ID you pass was merged into another
  entity during deduplication. If the returned `id` differs from the one
  you requested, update your stored reference — the old ID stays resolvable
  only while it remains in the entity's `referents` (the source-record and
  superseded IDs that map to the canonical entity).
"""

_SEARCH_EPILOG = """\
EXAMPLES:
  yente-cli search "acme"                                            # default dataset
  yente-cli search "acme" -d default -s Company                      # type filter
  yente-cli search "vladimir putin" -d sanctions -t sanction -l 5
  yente-cli search "x" -d default --filter properties.birthDate:1965 -f json

OUTPUT (with -f json):
  SearchResponse: {results: [Entity, ...], facets: {...}, total: {value, relation},
                   limit, offset}

EXIT CODES:
  0  ≥1 result
  1  zero results
  3  API error (4xx, 5xx)
  4  network/transport error

For ANY matching / record-linkage task, use `match` instead — even when
your input is partial (a name only, name + country, etc.). `search` is
purely for user-facing search UIs.
"""

_MATCH_EPILOG = """\
EXAMPLES:
  yente-cli match -s Person -p firstName=Aleksandr -p lastName=Zacharov -d sanctions
  yente-cli match -s Company -p name="Acme LLC" -p jurisdiction=us -d default
  yente-cli match -s Person -p firstName=X -p firstName=Alexander -d sanctions   # multi-value
  yente-cli match -s Person -i query.json -d sanctions -a best                    # from JSON
  yente-cli match -s Person -p name=Putin -d sanctions -f jsonl     # LLM-friendly

PROPERTY NAMES:
  Run `yente-cli ref schema Person` (or Company, Vessel, ...) to see what
  properties a schema accepts. Names are FtM camelCase: `firstName`, `birthDate`,
  `lastName`, `country`, `nationality` — not snake_case.
  Country-typed values accept free text ("Russia") and are normalized
  server-side. Matching works by value type, not field name — don't agonize
  over `country` vs `jurisdiction`.

SCHEMA CHOICE:
  Pick the most specific schema you can confidently set. A parent schema
  matches all its descendants in one call (`-s LegalEntity` covers
  `Person`, `Organization`, `Company`, `PublicBody`), but a parent query
  disables schema-specific scoring — e.g. `birthDate` / `firstName` only
  score for `Person` — and returns more near-misses. Use `LegalEntity`
  only for genuine person-or-organization ambiguity (e.g. raw payee
  strings), not as a default.

OUTPUT (with -f json):
  MatchResponse: {query: {...}, results: [ScoredEntity, ...], total, limit}
  Each ScoredEntity: {id, caption, schema, score (0-1), match (bool),
                      properties: {...}, datasets, target, explanations: {...}}

EXIT CODES:
  0  ≥1 result returned (may not have crossed threshold; check .match)
  1  zero results
  2  usage error (unknown schema, bad property, malformed -p)
  3  API error
  4  network/transport error

Use `search` only when building a user-facing search UI (search box,
autocomplete, browse). For matching tasks with partial input, stay on
`match`.
"""

_REF_SCHEMAS_EPILOG = """\
EXAMPLES:
  yente-cli ref schemas                       # all schemas
  yente-cli ref schemas --matchable           # only what you can `match` against
  yente-cli ref schemas -f json               # for LLM consumption

For details on one schema (properties, types, deprecation flags):
  yente-cli ref schema Person
"""

_REF_SCHEMA_EPILOG = """\
EXAMPLES:
  yente-cli ref schema Person
  yente-cli ref schema Company -f json        # full LLM-friendly summary
  yente-cli ref schema Sanction -f jsonl      # one property per line

OUTPUT (with -f json):
  {name, label, description, matchable, abstract, extends, schemata,
   featured, required, properties: [{name, type, label, description,
   deprecated, matchable, from_schema}, ...]}

The property list is flat (own + inherited), excluding stub
(reverse-edge) properties that aren't user-settable.

The per-property `matchable` flag is the FtM model's flag for that
property — NOT a "useful for matching" indicator. Non-matchable
properties (firstName, lastName, weakAlias, gender, ...) feed
dedicated matcher features (name reconstruction, alias
cross-comparison, mismatch qualifiers) and score just as directly as
matchable ones, through different code paths. Send every property
you have.
"""
