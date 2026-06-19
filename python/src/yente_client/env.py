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

API_KEY_VAR = "OPENSANCTIONS_API_KEY"
"""Name of the API-key env var. Also fed to Typer's ``envvar=`` in the CLI."""

BASE_URL_VAR = "YENTE_BASE_URL"
"""Name of the base-URL env var. Also fed to Typer's ``envvar=`` in the CLI."""

MCP_NAME_VAR = "YENTE_MCP_NAME"
"""Name of the env var that overrides the MCP server's advertised name."""

MCP_INSTRUCTIONS_VAR = "YENTE_MCP_INSTRUCTIONS"
"""Name of the env var that overrides the MCP server's description/instructions."""

DEFAULT_MCP_NAME = "yente"
"""MCP server name advertised when ``$YENTE_MCP_NAME`` is unset."""

DEFAULT_MCP_INSTRUCTIONS = (
    "Screen and research people, companies, and other entities against the "
    "OpenSanctions database (sanctions lists, PEPs, watchlists). For any "
    "match/no-match question — even from a partial record — use match_entity. "
    "Use search_entities only to back a human-style search box. Call "
    "describe_schema first to learn real FtM property names (birthDate, "
    "registrationNumber) before building a match. "
    "Present findings as an analyst's briefing, not raw API output: lead with the "
    "risk conclusion (match / no match; sanctioned, PEP, or unlisted), then "
    "organize the supporting detail into scannable summaries — tables for "
    "sanctions, positions, and identifiers, a timeline for dates, grouped by "
    "entity and relationship, with the source datasets named. This is an AML and "
    "investigations aid, so surface what a compliance analyst needs to decide and "
    "explain it in plain language."
)
"""MCP server instructions advertised when ``$YENTE_MCP_INSTRUCTIONS`` is unset."""

HOSTED_HOSTS = (
    "api.opensanctions.org",
    "api.opensanctions.net",
    "api.opensanctions.com",
    "api.test.opensanctions.org",
    "api.test.opensanctions.net",
    "api.test.opensanctions.com",
)
"""Hostnames of the hosted OpenSanctions API, across every TLD we might serve
from. Used to decide whether a client built without an ``api_key`` warrants a
warning; a self-hosted yente is not in this set and stays quiet."""


def api_key() -> str | None:
    """Return ``$OPENSANCTIONS_API_KEY``, or ``None`` if unset."""
    return os.environ.get(API_KEY_VAR)


def base_url() -> str:
    """Return ``$YENTE_BASE_URL``, or :data:`DEFAULT_BASE_URL` if unset."""
    return os.environ.get(BASE_URL_VAR, DEFAULT_BASE_URL)


def mcp_name() -> str:
    """Return ``$YENTE_MCP_NAME``, or :data:`DEFAULT_MCP_NAME` if unset.

    Lets a self-hosted deployment advertise its own brand name to MCP clients
    without forking the server.
    """
    return os.environ.get(MCP_NAME_VAR, DEFAULT_MCP_NAME)


def mcp_instructions() -> str:
    """Return ``$YENTE_MCP_INSTRUCTIONS``, or :data:`DEFAULT_MCP_INSTRUCTIONS` if unset.

    Companion to :func:`mcp_name` — overrides the server description/instructions
    shown to MCP clients for a branded deployment.
    """
    return os.environ.get(MCP_INSTRUCTIONS_VAR, DEFAULT_MCP_INSTRUCTIONS)
