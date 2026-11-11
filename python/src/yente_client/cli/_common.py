"""Helpers shared by the ``yente-cli`` subcommands: client setup, errors, suggestions."""

import contextlib
import difflib
from collections.abc import Iterable, Iterator

from yente_client.cli._deps import typer
from yente_client.cli.config import CliConfig
from yente_client.client import Client
from yente_client.entities import EntityInput, entity_class
from yente_client.exceptions import APIError, TransportError, YenteError
from yente_client.schemas import has_schema, is_matchable_schema, matchable_schemata, model


def usage_error(message: str) -> typer.Exit:
    """Print ``message`` as a usage error and return the exit-2 for the caller to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=2)


def error_text(exc: YenteError) -> str:
    """Render an SDK error as one line: its type, HTTP status if any, and detail."""
    if isinstance(exc, APIError):
        return f"{type(exc).__name__} ({exc.status_code}): {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


@contextlib.contextmanager
def with_client(ctx: typer.Context) -> Iterator[Client]:
    """Build a Client for one command and turn SDK errors into clean exits.

    A ``YenteError`` prints as one stderr line and exits 4 for a network
    failure, 3 for anything else. ``-v`` / ``--verbose`` re-raises it instead,
    for the full traceback.
    """
    config: CliConfig = ctx.obj
    try:
        with config.make_client() as client:
            yield client
    except YenteError as exc:
        if config.verbose:
            raise
        typer.echo(f"error: {error_text(exc)}", err=True)
        raise typer.Exit(code=4 if isinstance(exc, TransportError) else 3) from exc


def did_you_mean(name: str, choices: Iterable[str]) -> str:
    """Return a `` (did you mean 'X'?)`` hint naming the closest choice, or ``""``."""
    close = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return f" (did you mean {close[0]!r}?)" if close else ""


def unknown_schema(name: str) -> str:
    """Describe an unknown schema name, with the closest real one."""
    return f"Unknown schema {name!r}{did_you_mean(name, model['schemata'])}"


def schema_problem(name: str) -> str | None:
    """Say why ``name`` can't be a match query's schema, or ``None`` if it can."""
    if not has_schema(name):
        return unknown_schema(name)
    if not is_matchable_schema(name):
        options = ", ".join(matchable_schemata()[:6])
        return f"Schema {name!r} is not a matchable target; try one like {options}, …"
    return None


def matchable_entity_class(name: str) -> type[EntityInput]:
    """Return the input class for a match query's schema; exit 2 if it can't be one."""
    problem = schema_problem(name)
    if problem is not None:
        raise usage_error(f"{problem} (run `yente-cli ref schemas --matchable` for the list).")
    return entity_class(name)
