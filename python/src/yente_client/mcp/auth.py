"""Per-request API key → ``AsyncClient`` plumbing.

v1 auth is bearer-token pass-through: the MCP holds no secret. Each request
carries the caller's OpenSanctions API key as ``Authorization: Bearer <key>``;
the server forwards it downstream so yente's existing gateway does the actual
validation, quota, and billing, and the caller is screening as themselves. When
a request carries no bearer token, the server falls back to its own
``OPENSANCTIONS_API_KEY`` env var — convenient for running locally against a
real API (see :func:`resolve_api_key`).

This module is the credential-free half (build/cache clients by key) so it stays
FastMCP-independent and testable; the header extraction lives in
:mod:`yente_client.mcp.server`.
"""

from collections import OrderedDict

import httpx

from yente_client.async_client import AsyncClient

# One AsyncClient per (key, base_url) so we don't spin up a fresh httpx pool on
# every tool call. Bounded because access tokens expire and are replaced, so a
# long-running multi-tenant server sees an endless stream of new keys.
_MAX_CLIENTS = 1024
_CLIENTS: OrderedDict[tuple[str | None, str], AsyncClient] = OrderedDict()


def client_for(
    api_key: str | None,
    base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncClient:
    """Return a cached :class:`AsyncClient` for this key + base URL.

    ``api_key`` may be ``None`` when targeting an open self-hosted yente (no
    gateway); the key is simply unused downstream in that case. ``transport``
    only applies to a newly built client.
    """
    cache_key = (api_key, base_url)
    client = _CLIENTS.get(cache_key)
    if client is not None:
        _CLIENTS.move_to_end(cache_key)
        return client
    client = AsyncClient(
        api_key=api_key, base_url=base_url, app_name="yente-mcp", transport=transport
    )
    _CLIENTS[cache_key] = client
    if len(_CLIENTS) > _MAX_CLIENTS:
        # Not closed: a tool call may still be using it. Garbage collection
        # releases its connections once nothing refers to it.
        _CLIENTS.popitem(last=False)
    return client


def parse_bearer(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header value.

    Returns ``None`` when the header is absent or not a bearer credential, so a
    missing key degrades to an unauthenticated downstream call (which the
    gateway rejects) rather than an error here.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def resolve_api_key(authorization: str | None, env_key: str | None) -> str | None:
    """Pick the downstream API key: per-request bearer token, else the env key.

    The bearer token is the production, per-tenant path. Falling back to a
    server-wide ``OPENSANCTIONS_API_KEY`` lets you run ``yente-mcp`` on
    localhost against a real API without configuring auth in the MCP client —
    handy for testing. ``None`` from both means an unauthenticated downstream
    call.
    """
    return parse_bearer(authorization) or env_key
