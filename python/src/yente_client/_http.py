"""Internal HTTP helpers: User-Agent assembly, error-response mapping.

Lives below the ``Client`` so the request layer stays small and the helpers
are independently testable.
"""

import contextlib
import re
import sys
import warnings
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from yente_client.env import HOSTED_HOSTS
from yente_client.exceptions import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    ServerError,
)

# Reject characters that would break the User-Agent grammar:
# whitespace, parentheses (used to delimit comment blocks), semicolons
# (used to separate comment items).
_INVALID_APP_NAME = re.compile(r"[\s();]")


def validate_app_name(name: str) -> str:
    """Validate a caller-supplied ``app_name`` for inclusion in the User-Agent.

    Empty strings are rejected because they'd produce a stray ``;`` in the
    parenthesized comment. Whitespace / parens / semicolons would break the
    UA grammar and are rejected. Returns the name unchanged on success.
    """
    if not name:
        raise ConfigurationError("app_name must be a non-empty string")
    if _INVALID_APP_NAME.search(name):
        raise ConfigurationError(
            f"app_name must not contain whitespace, parens, or semicolons; got: {name!r}"
        )
    return name


def _installed_version() -> str:
    try:
        return version("yente-client")
    except PackageNotFoundError:
        return "0.0.0+unknown"


VERSION = _installed_version()
"""Installed ``yente-client`` version; ``0.0.0+unknown`` in an unbuilt checkout."""


def build_user_agent(app_name: str | None = None, override: str | None = None) -> str:
    """Build the User-Agent string.

    Format (RFC 7231 product + comment):
        yente-client/<ver> (<app_name>; python/<ver>; httpx/<ver>)

    ``app_name`` is omitted from the comment if ``None``. ``override`` short-
    circuits the whole assembly — caller takes responsibility for the result.
    """
    if override is not None:
        return override
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    parts: list[str] = []
    if app_name:
        parts.append(app_name)
    parts.append(f"python/{py}")
    parts.append(f"httpx/{httpx.__version__}")
    return f"yente-client/{VERSION} ({'; '.join(parts)})"


def is_opensanctions_url(base_url: str) -> bool:
    """Return True when ``base_url`` points at the OpenSanctions API."""
    return any(host in base_url for host in HOSTED_HOSTS)


def prepare_http_kwargs(
    *,
    api_key: str | None,
    base_url: str,
    app_name: str | None,
    user_agent: str | None,
    timeout: float | httpx.Timeout | None,
    verify: bool | str,
    proxy: str | None,
    headers: dict[str, str] | None,
) -> dict[str, Any]:
    """Assemble the kwargs shared between ``httpx.Client`` and ``httpx.AsyncClient``.

    Caller is responsible for setting ``transport=`` since sync and async use
    different transport base classes (``httpx.HTTPTransport`` vs
    ``httpx.AsyncHTTPTransport``).

    Side effects:
      - Raises ``ConfigurationError`` on an invalid ``app_name``.
      - Emits a one-shot ``UserWarning`` when no ``api_key`` is set and
        ``base_url`` points at the OpenSanctions API.
    """
    if app_name is not None:
        validate_app_name(app_name)

    if api_key is None and is_opensanctions_url(base_url):
        warnings.warn(
            "Client constructed against the OpenSanctions API without an "
            "api_key. Set api_key= or pass OPENSANCTIONS_API_KEY via env.",
            stacklevel=3,
        )

    # Caller's headers go in first; the Authorization / User-Agent set here win.
    merged_headers: dict[str, str] = dict(headers or {})
    if api_key:
        merged_headers["Authorization"] = f"ApiKey {api_key}"
    merged_headers["User-Agent"] = build_user_agent(app_name=app_name, override=user_agent)

    kwargs: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "headers": merged_headers,
        "timeout": timeout or httpx.Timeout(30.0, connect=10.0),
        "follow_redirects": True,
        "verify": verify,
    }
    if proxy is not None:
        kwargs["proxy"] = proxy
    return kwargs


def read_json(response: httpx.Response) -> Any:
    """Decode a response's JSON, raising the matching ``APIError`` on non-2xx."""
    if not response.is_success:
        raise_for_response(response)
    return response.json()


class ETagCache:
    """Hold one ``(etag, body)`` pair per URL for ``If-None-Match`` revalidation.

    Bodies are raw JSON, not validated models: callers re-validate, so a served
    ``304`` can't leak mutable state between calls. A server that emits no
    ``ETag`` leaves the cache empty, and every call stays a plain GET.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, Any]] = {}

    def headers(self, url: str) -> dict[str, str]:
        """Return the revalidation header for ``url``, if a body is held."""
        entry = self._entries.get(url)
        return {"If-None-Match": entry[0]} if entry is not None else {}

    def read(self, url: str, response: httpx.Response) -> Any:
        """Serve the held body on ``304 Not Modified``; otherwise decode and hold."""
        entry = self._entries.get(url)
        if response.status_code == 304 and entry is not None:
            return entry[1]
        body = read_json(response)
        etag = response.headers.get("ETag")
        if etag:
            self._entries[url] = (etag, body)
        return body


def raise_for_response(response: httpx.Response) -> None:
    """Map a non-2xx ``httpx.Response`` to the right ``APIError`` subclass and raise.

    The ``detail`` field from the JSON body is preferred, falling back to the
    response body text, then to a generic ``HTTP <status>`` placeholder. A
    Retry-After header on 429 is parsed as a float (delta-seconds); HTTP-date
    formats are ignored for now.
    """
    status = response.status_code
    detail = _extract_detail(response)

    if status == 400:
        raise BadRequestError(status, detail, response)
    if status in (401, 403):
        raise AuthenticationError(status, detail, response)
    if status == 404:
        raise NotFoundError(status, detail, response)
    if status == 429:
        retry_after: float | None = None
        ra_header = response.headers.get("Retry-After")
        if ra_header:
            with contextlib.suppress(ValueError):
                retry_after = float(ra_header)
        raise RateLimitError(status, detail, retry_after=retry_after, response=response)
    if 500 <= status < 600:
        raise ServerError(status, detail, response)
    raise APIError(status, detail, response)


def _extract_detail(response: httpx.Response) -> str:
    """Try to extract a human-readable error message from a non-2xx response."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"

    if isinstance(body, dict):
        detail = body.get("detail")
        if detail is None:
            return response.text or f"HTTP {response.status_code}"
        if isinstance(detail, list):
            # Pydantic validation error format: list of {msg, loc, type, ...}.
            return "; ".join(_format_detail_item(item) for item in detail)
        # yente puts a generic title in `detail` and the specifics in `errors`:
        # a list in pydantic's format, or a dict for FtM data errors.
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return f"{detail}: " + "; ".join(_format_detail_item(item) for item in errors)
        if isinstance(errors, dict) and errors:
            return f"{detail}: {errors}"
        return str(detail)
    return str(body)


def _format_detail_item(item: Any) -> str:
    if isinstance(item, dict):
        msg = item.get("msg") or item.get("message") or ""
        loc = item.get("loc")
        if loc:
            return f"{'.'.join(str(p) for p in loc)}: {msg}"
        return str(msg)
    return str(item)
