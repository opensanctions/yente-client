# yente-client

Python SDK for the [OpenSanctions](https://www.opensanctions.org) API and
self-deployed [yente](https://github.com/opensanctions/yente) instances.

## Install

```bash
pip install yente-client            # SDK only
pip install yente-client[cli]       # SDK + `yente-cli` command-line tool
```

Python 3.11+; runtime deps are `pydantic` and `httpx`. The `[cli]` extra
adds `typer` and `rich`.

## Quickstart

```python
from yente_client import Client, Person

with Client(api_key="...", app_name="MyScreeningApp") as c:
    hits = c.match(
        Person(firstName="Aleksandr", lastName="Zacharov", birthDate="1965"),
        datasets=["sanctions"],
        threshold=0.7,
    )
    if hits.top is not None:
        print(hits.top.caption, hits.top.score)
    for match in hits.matches:
        print(match.id, match.properties.get("topics", []))
```

The API key can be generated at
[opensanctions.org/account](https://www.opensanctions.org/account/). It's
read in this example from the `OPENSANCTIONS_API_KEY` env var if you skip
passing `api_key=`. To target a yente instance, pass `base_url=` (no key needed).

## Other endpoints

```python
# Free-text search
res = c.search("acme", datasets=["default"], schema="Company")

# Fetch one entity by ID; nested=True (default) inlines adjacent entities
entity = c.fetch("NK-aU5ybkbRFJucf8YMwsJvDw")
for sanction in entity.properties.get("sanctions", []):
    print(sanction.properties["authority"])

# Operational endpoints
c.datasets()      # available datasets and freshness
c.algorithms()    # enabled matching algorithms
c.healthz()       # liveness
```

## Entity construction

The package ships generated classes for every FtM schema (`Person`, `Company`,
`Vessel`, `Organization`, …). All take typed `list[str]` properties; a single
string is coerced to a one-element list. Unknown properties raise
`pydantic.ValidationError` at construction.

```python
from yente_client import Person, Company

p = Person(firstName="Aleksandr", lastName="Zacharov", country="ru")
c = Company(name="Acme LLC", jurisdiction="us")

Person(birth_date="1965")    # ValidationError — snake_case isn't aliased
Person(notARealProp="X")     # ValidationError — extra="forbid"
```

## Configuration

`Client` accepts:

| Kwarg | Default | Notes |
| --- | --- | --- |
| `api_key` | `None` | Sent as `Authorization: ApiKey <key>`. |
| `base_url` | `https://api.opensanctions.org` | Override for a yente instance or staging. |
| `app_name` | `None` | Identifier added to the User-Agent comment. |
| `user_agent` | `None` | Full override; bypasses the assembled UA. |
| `timeout` | `30s read, 10s connect` | Pass an `httpx.Timeout(...)` for fine control. |
| `verify` | `True` | SSL verification; pass a CA bundle path or `False`. |
| `proxy` | `None` | Forwarded to `httpx.Client(proxy=...)`. |
| `headers` | `None` | Merged onto every request; `Authorization` and `User-Agent` win. |
| `transport` | `None` | Custom `httpx.BaseTransport` (e.g. `MockTransport` for tests). |

## CLI

`pip install yente-client[cli]` ships a `yente-cli` binary that mirrors the SDK:

```bash
export OPENSANCTIONS_API_KEY=sk_...        # or pass --api-key

# Screen a known entity (KYC / sanctions checks):
yente-cli match -s Person -p firstName=Aleksandr -p lastName=Zacharov -d sanctions

# Free-text discovery by name:
yente-cli search "acme" -d default -s Company

# Fetch one entity (id from match/search):
yente-cli fetch NK-aU5ybkbRFJucf8YMwsJvDw

# Discover the data model (offline, no API key):
yente-cli ref schemas                   # all schemas with matchable flags
yente-cli ref schema Person -f json     # full property list, types, deprecation
yente-cli ref topics                    # the Topic enum
yente-cli ref countries                 # ISO country codes the server speaks

# Discover server state:
yente-cli status                        # client + server + auth + loaded datasets
yente-cli catalog                       # full per-source dataset list
yente-cli algorithms                    # enabled algorithms, default + best
```

Output formats: `-f table` (default on TTY), `-f json` (pretty, default when piped),
`-f jsonl` (one item per line, ideal for `jq` and LLM pipelines).

**`search` vs `match`:** use `match` when you have a known entity to screen
(name + dob + country); use `search` when you have a name fragment to look up.

**Exit codes:**
- `0` ≥1 result
- `1` zero results (lets shell scripts gate on `&&`)
- `2` usage error (bad flag, unknown schema/property)
- `3` API error (4xx, 5xx)
- `4` network/transport error

Designed for LLM agents: every command's `--help` carries worked examples and
documented JSON output shapes; unknown schema/property names get fuzzy
suggestions ("Did you mean `birthDate`?"). Run `yente-cli --help` first.

## Errors

Every non-2xx response raises a subclass of `YenteError`:

- `BadRequestError` (400)
- `AuthenticationError` (401, 403)
- `NotFoundError` (404)
- `RateLimitError` (429, with `.retry_after` when set)
- `ServerError` (5xx)
- `APIError` (other; carries `.status_code` and `.detail`)
- `TransportError` (network failure before the request reached the server)

Retries are not built into M2 — failed requests raise; users handle.
