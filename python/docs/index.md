# yente-client documentation

The yente-client Python SDK, `yente-cli` command-line tool, and
`yente-mcp` MCP server for matching entities against the
[OpenSanctions API](https://www.opensanctions.org) and on-premise
[yente](https://github.com/opensanctions/yente) instances.

## Start here

- **[Tutorial](tutorial.md)** — a linear walk through the SDK: install,
  first match, search, fetch, async, errors, and the
  [FollowTheMoney](https://followthemoney.tech/) (FtM) data model.
- **[Screening a file](screening.md)** — batch-screen a CSV of
  entities with `yente-cli screen` (or `match_iter` in Python):
  column mapping, triage, error handling.
- **[CLI overview](cli.md)** — the `yente-cli` command, when to reach
  for it, the command list, output formats, exit codes.
- **[MCP server](mcp.md)** — expose screening to LLM agents: install,
  run, connect a client, the tool surface.
- **[API reference](api/index.md)** — auto-generated from docstrings;
  every public symbol re-exported from `yente_client`.

## Scope

These pages cover the matching workflow (the SDK's primary use case)
and the search and fetch endpoints that surround it. For broader
context (sanctions screening, available datasets, getting an API key),
see the [OpenSanctions docs](https://www.opensanctions.org/docs/).

## Regenerating the API reference

The `api/` tree is generated from docstrings; do not hand-edit. Run
`make docs` to regenerate after changing public docstrings; CI runs
`make docs-check` to catch drift.
