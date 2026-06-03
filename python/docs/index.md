# yente-client documentation

The yente-client Python SDK and `yente-cli` command-line tool for
matching entities against the [OpenSanctions API](https://www.opensanctions.org)
and self-deployed [yente](https://github.com/opensanctions/yente)
instances.

## Start here

- **[Tutorial](tutorial.md)** — a linear walk through the SDK: install,
  first match, search, fetch, async, errors, and the
  [FollowTheMoney](https://followthemoney.tech/) (FtM) data model.
- **[CLI overview](cli.md)** — the `yente-cli` command, when to reach
  for it, the command list, output formats, exit codes.
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
