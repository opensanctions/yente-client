"""Per-request API key extraction.

v1 auth is bearer-token pass-through: the MCP holds no secret. Each request
carries the caller's OpenSanctions API key as ``Authorization: Bearer <key>``;
the server forwards it downstream so yente's existing gateway does the actual
validation, quota, and billing, and the caller is screening as themselves. When
a request carries no bearer token, the server falls back to its own
``OPENSANCTIONS_API_KEY`` env var — convenient for running locally against a
real API.

This module is the credential-free half (parse the header) so it stays
FastMCP-independent and testable; the header extraction lives in
:mod:`yente_client.mcp.server`.
"""


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
