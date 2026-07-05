"""Map SDK exceptions to clear, never-empty tool-error messages.

A failing tool must tell the agent *what* went wrong and *whether retrying could
help*. The default ``str(exc)`` is not enough: httpx timeout exceptions
stringify to ``""``, so a transient blip surfaces as an empty error body — and an
agent that can't tell a bad argument from a network hiccup burns retries or gives
up silently (both observed in field testing).

The message embeds a compact ``status`` / ``retryable`` hint the agent can act
on. Message-building is pure (no FastMCP) so it's unit-testable.
"""

from yente_client.exceptions import APIError, RateLimitError, TransportError, YenteError


def describe_error(exc: YenteError) -> str:
    """Return a non-empty, agent-readable description of an SDK error.

    Retryable means "the same call might succeed later": network failures, 5xx,
    and 429. A 4xx (bad schema, unknown entity, auth) is not — the agent should
    fix the call or give up rather than retry.
    """
    if isinstance(exc, TransportError):
        detail = str(exc) or "network timeout or connection failure"
        return f"yente request failed (retryable=true): {detail}"
    if isinstance(exc, APIError):
        retryable = exc.status_code >= 500 or exc.status_code == 429
        suffix = ""
        if isinstance(exc, RateLimitError) and exc.retry_after is not None:
            suffix = f" retry_after={exc.retry_after}s"
        detail = exc.detail or "(no detail provided)"
        return (
            f"yente API error (status={exc.status_code}, "
            f"retryable={str(retryable).lower()}): {detail}{suffix}"
        )
    # ConfigurationError or a bare YenteError: no status, not retryable.
    return f"yente error: {str(exc) or type(exc).__name__}"
