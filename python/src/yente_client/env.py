"""Environment-variable configuration, resolved in one place.

The SDK clients (:class:`~yente_client.Client` / :class:`~yente_client.AsyncClient`)
take explicit arguments and never read the environment themselves — that keeps
the library predictable: constructing a client does only what its arguments say.

This module is the shared helper the *edges* use instead — the CLI, the MCP
server, and the test suite — so the variable names and the default API root live
in exactly one spot rather than being re-typed (and drifting) in each caller.
Functions read ``os.environ`` on each call so tests can monkeypatch freely.
"""

import os

DEFAULT_BASE_URL = "https://api.opensanctions.org"
"""API root used when ``$YENTE_BASE_URL`` is unset."""


def api_key() -> str | None:
    """Return ``$OPENSANCTIONS_API_KEY``, or ``None`` if unset."""
    return os.environ.get("OPENSANCTIONS_API_KEY")


def base_url() -> str:
    """Return ``$YENTE_BASE_URL``, or :data:`DEFAULT_BASE_URL` if unset."""
    return os.environ.get("YENTE_BASE_URL", DEFAULT_BASE_URL)
