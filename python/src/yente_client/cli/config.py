"""Resolved CLI configuration.

Each invocation builds a :class:`CliConfig` from CLI flags and env vars (Typer
handles the merge via ``envvar=``). Subcommands then call ``.make_client()``
or ``.make_async_client()`` to get an SDK client wired with the right auth /
base URL / app identifier.

Client construction is lazy on purpose: commands that don't make HTTP calls
(``version``, ``ref *``) avoid building a Client (and the "missing api_key"
warning it would emit against the OpenSanctions API URL).
"""

from dataclasses import dataclass

from yente_client.async_client import AsyncClient
from yente_client.client import Client


@dataclass(frozen=True)
class CliConfig:
    """Resolved CLI invocation configuration.

    Built once in the Typer ``@app.callback()`` and stashed on
    ``ctx.obj``. Subcommands read it to construct SDK clients on demand.
    """

    api_key: str | None
    base_url: str
    app_name: str | None
    user_agent: str | None
    verbose: bool

    def make_client(self) -> Client:
        """Build a sync :class:`Client` from this config."""
        return Client(
            api_key=self.api_key,
            base_url=self.base_url,
            app_name=self.app_name,
            user_agent=self.user_agent,
        )

    def make_async_client(self) -> AsyncClient:
        """Build an :class:`AsyncClient` from this config."""
        return AsyncClient(
            api_key=self.api_key,
            base_url=self.base_url,
            app_name=self.app_name,
            user_agent=self.user_agent,
        )
