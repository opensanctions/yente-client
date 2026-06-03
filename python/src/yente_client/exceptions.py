"""Exception hierarchy for yente-client.

All client-raised errors inherit from ``YenteError`` so callers can catch
broadly when they don't care which sub-case fired. ``pydantic.ValidationError``
is raised separately for input-shape mistakes (unknown kwargs on a per-schema
entity class, etc.).
"""

from typing import Any


class YenteError(Exception):
    """Base for every error raised by this client."""


class ConfigurationError(YenteError):
    """Bad client configuration (invalid ``app_name``, malformed ``base_url``…)."""


class TransportError(YenteError):
    """Network-level failure: timeout, connection refused, DNS, TLS handshake."""


class APIError(YenteError):
    """Non-2xx response from the server.

    Attributes:
        status_code: HTTP status as returned.
        detail: server-supplied error message (``detail`` field of the JSON body
            if present; otherwise the response text or a generic message).
        response: the raw ``httpx.Response`` for advanced inspection.
    """

    def __init__(self, status_code: int, detail: str, response: Any | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.response = response
        super().__init__(f"{status_code}: {detail}")


class BadRequestError(APIError):
    """HTTP 400 — invalid request (unknown schema, malformed filter, …)."""


class AuthenticationError(APIError):
    """HTTP 401 or 403 — missing or invalid API key."""


class NotFoundError(APIError):
    """HTTP 404 — entity, dataset, or property not found."""


class RateLimitError(APIError):
    """HTTP 429 — rate limit exceeded.

    Carries ``retry_after`` (seconds) when the server provided the header.
    Automatic retries are not built into the client; callers handle backoff.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        retry_after: float | None = None,
        response: Any | None = None,
    ) -> None:
        super().__init__(status_code, detail, response)
        self.retry_after = retry_after


class ServerError(APIError):
    """HTTP 5xx — server-side error."""
