"""Centralized import of CLI-only dependencies.

The CLI extra (``pip install yente-client[cli]``) pulls in Typer and Rich.
Routing every CLI-side import through this module means users who installed
``yente-client`` without the extra see a single-line install hint on first
invocation instead of a raw :class:`ImportError` traceback.

CLI modules should import from here:

    from yente_client.cli._deps import typer, Console, Table
"""

import sys


def _bail(missing: str) -> None:
    sys.stderr.write(
        f"The yente-cli command requires the '{missing}' package.\n"
        "Install the CLI extra:\n"
        "  pip install 'yente-client[cli]'\n"
    )
    sys.exit(127)


try:
    import typer
    from typer.core import TyperCommand
except ImportError:
    _bail("typer")

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    _bail("rich")


__all__ = ["Console", "Table", "TyperCommand", "typer"]
