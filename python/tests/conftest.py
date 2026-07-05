"""Shared test fixtures.

- ``load_fixture(name)`` reads ``testdata/<name>.json`` at the repo root
  (language-agnostic; the future TS SDK loads the same corpus).
- ``make_client(handler=)`` builds a ``Client`` wired up to a
  ``httpx.MockTransport`` so respx-style unit tests don't touch the network.
- ``live_client`` builds a ``Client`` against a real yente instance when
  ``OPENSANCTIONS_API_KEY`` is set; otherwise the test that requested it
  is skipped. Reads ``.env`` at the repo root for local-dev convenience —
  CI relies on env vars provided by repo secrets instead.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

from yente_client import env
from yente_client.async_client import AsyncClient
from yente_client.client import Client

# Top-level testdata/ at the repo root, shared with the future TS SDK.
TESTDATA_DIR = Path(__file__).resolve().parent.parent.parent / "testdata"

# Load .env if present (repo root, gitignored). CI uses secrets directly.
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


@pytest.fixture
def load_fixture() -> Callable[[str], dict]:
    """Return a callable that loads ``testdata/<name>.json`` and returns it as a dict."""

    def _load(name: str) -> dict:
        return json.loads((TESTDATA_DIR / f"{name}.json").read_text())

    return _load


@pytest.fixture
def make_client() -> Callable[..., Client]:
    """Return a factory for ``Client`` instances backed by an httpx.MockTransport.

    Pass ``handler=`` to control responses per test:

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={...})

        client = make_client(handler=handler)
    """

    def _factory(
        *,
        handler: Callable[[httpx.Request], httpx.Response],
        api_key: str = "test",
        base_url: str = "https://api.test.opensanctions.org",
    ) -> Client:
        return Client(api_key=api_key, base_url=base_url, transport=httpx.MockTransport(handler))

    return _factory


@pytest.fixture
def make_async_client() -> Callable[..., AsyncClient]:
    """Async counterpart to ``make_client``: factory backed by httpx.MockTransport."""

    def _factory(
        *,
        handler: Callable[[httpx.Request], httpx.Response],
        api_key: str = "test",
        base_url: str = "https://api.test.opensanctions.org",
    ) -> AsyncClient:
        return AsyncClient(
            api_key=api_key,
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )

    return _factory


@pytest.fixture
def live_client() -> Iterator[Client]:
    """A ``Client`` against the real yente API.

    Skips when ``OPENSANCTIONS_API_KEY`` is unset (CI without secrets,
    local without .env). ``YENTE_BASE_URL`` overrides the default
    (``api.opensanctions.org``). Local dev usually points at the test
    instance via ``.env``; CI sets ``YENTE_BASE_URL`` explicitly via secrets.
    """
    key = env.api_key()
    if not key:
        pytest.skip("OPENSANCTIONS_API_KEY not set; skipping live tests")
    with Client(api_key=key, base_url=env.base_url(), app_name="yenteclient-tests") as client:
        yield client


@pytest.fixture
async def live_async_client():
    """Async counterpart to ``live_client``. Skips on missing key."""
    key = env.api_key()
    if not key:
        pytest.skip("OPENSANCTIONS_API_KEY not set; skipping live tests")
    async with AsyncClient(
        api_key=key, base_url=env.base_url(), app_name="yenteclient-tests"
    ) as client:
        yield client
