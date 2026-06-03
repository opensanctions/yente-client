# yente-cli — command-line overview

The `yente-cli` command-line tool mirrors the Python SDK surface and
serves one-off lookups, shell pipelines, and LLM-agent automations.

It ships with the `yente-client[cli]` install extra. Per-command
detail (every option, every exit code, output JSON shape, worked
examples) lives in `yente-cli <command> --help`.

## When to reach for the CLI vs the SDK

Use the CLI for ad-hoc lookups, shell pipelines, and agent automations;
use the SDK for embedded screening and long-running services.

## Install

```bash
pip install yente-client[cli]
```

The `[cli]` extra pulls in `typer` and `rich`. Without it, `yente-cli`
emits a one-line install hint and exits.

Authenticate by exporting your API key (the same one the SDK uses):

```bash
export OPENSANCTIONS_API_KEY=sk_live_…
```

To point at a yente instance, set `YENTE_BASE_URL` or pass
`--base-url`.

## The command surface

| Command | One-line |
|---|---|
| `match` | Match a single entity (built from `-p key=value` flags or `--from-file`) against a dataset. The canonical command for any matching task. |
| `search` | Free-text search across one or more datasets, for backing user-facing search UIs. |
| `fetch` | Fetch one entity by ID. |
| `datasets` | List indexed datasets and their freshness. |
| `statements` | Statement-level data lineage (OpenSanctions API only). |
| `algorithms` | List enabled matching algorithms and the server defaults. |
| `status` | Client setup, server health, and loaded datasets, at a glance. |
| `ref schemas` | List every FollowTheMoney (FtM) schema (offline; uses bundled snapshot). |
| `ref schema NAME` | Detail view: properties, types, `directly_scored` flag, deprecation. |
| `ref topics` | The `Topic` enum (`sanction`, `role.pep`, `crime.fraud`, …). |
| `ref countries` | The country-code to label lookup. |

`yente-cli --help` carries the full workflow block; per-command
`--help` carries worked examples, OUTPUT-shape blocks, and EXIT-CODE
tables.

## Output formats

Every command takes `-f` / `--format`:

- **`-f table`**: rich table, the default on a TTY.
- **`-f json`**: pretty JSON, the default when piped.
- **`-f jsonl`**: one item per line. Suitable for `jq` pipelines and
  LLM consumption.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, at least one result (or any successful response for `fetch` / `datasets`). |
| `1` | Success, zero results. Lets shell scripts gate on `match … && action`. |
| `2` | Usage error: bad flag, unknown schema, malformed `-p key=value`. |
| `3` | API error: non-2xx response. |
| `4` | Transport error: network, timeout, TLS. |

## Worked examples

```bash
# Screen a person against the sanctions collection
yente-cli match -s Person \
  -p firstName=Vladimir -p lastName=Putin -p birthDate=1952-10-07 \
  -d sanctions

# Multi-value property (the same key twice appends):
yente-cli match -s Person \
  -p firstName=Alexander -p firstName=Alex -p lastName=Smith \
  -d sanctions

# From a JSON file (the wire-format match query):
yente-cli match -s Person -i query.json -d sanctions

# Free-text search for a company
yente-cli search "acme" -d default -s Company -l 10

# Fetch one entity, full record
yente-cli fetch NK-aU5ybkbRFJucf8YMwsJvDw

# Check the client setup and server state
yente-cli status

# Inspect what's matchable, machine-readable
yente-cli ref schemas --matchable -f json
yente-cli ref schema Person -f json
```

Every command has an EXAMPLES block in its `--help`.

## Agent-friendly help

Run `yente-cli --help` first. The top-level help carries a workflow
block (intent → command) and a dispatch table; per-command `--help`
carries worked examples, the output JSON shape, and exit codes. Error
messages on bad schema or property names include fuzzy suggestions
("Did you mean: `birthDate`?").

## Where to go next

- [SDK tutorial](tutorial.md): embedded Python usage and the matching
  workflow in depth.
- [API reference](api/index.md): full signatures of every public symbol.
- [OpenSanctions docs](https://www.opensanctions.org/docs/) for domain
  context (sanctions screening, the FtM data model, the API
  quickstart, the account / API-key page).
