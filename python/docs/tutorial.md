# Get started with the yente-client Python SDK

A linear walk through the Python SDK: install, first match, matching
in depth, fetch, search, async, errors, and entities.

If you want shell access rather than Python, see the
[CLI overview](cli.md) instead. The
[FollowTheMoney](https://followthemoney.tech/) (FtM) data model
underlies every entity returned by these endpoints; on first contact
with FtM, the [tutorial on the FtM model](#8-entities-and-the-ftm-model)
in section 8 is a useful entry point.

## 1. Install and authenticate

```bash
pip install yente-client
```

Python 3.11+. Runtime dependencies are `pydantic` and `httpx`.

The [OpenSanctions API](https://www.opensanctions.org/account/) needs
an API key. Get one, then export it:

```bash
export OPENSANCTIONS_API_KEY=sk_live_…
```

A bare `Client()` constructor picks up `OPENSANCTIONS_API_KEY` and
`YENTE_BASE_URL` from the environment. To target a yente instance, set
`YENTE_BASE_URL=http://localhost:8000` (or pass `base_url=` directly).

```python
from yente_client import Client

with Client() as client:
    print(client.healthz())
    # StatusResponse(status='ok')
```

`Client` is a context manager. Use `with Client() as client:` for
deterministic cleanup of the underlying `httpx.Client`.

## 2. Your first match

The SDK's primary use case is **matching**: given a partial or complete
entity description, find candidate matches in OpenSanctions data.

### Construct an entity

Every FollowTheMoney (FtM) schema has a Python class. Construct one
with the property fields you know:

```python
from yente_client import Client, Person

query = Person(
    firstName="Vladimir",
    lastName="Putin",
    birthDate="1952-10-07",
)
```

Property names use FtM **camelCase** (`firstName`, `birthDate`,
`nationality`, `passportNumber`) and match the wire format exactly.
Use [`yente-cli ref schema Person`](cli.md) (or the
[`Person` API reference](api/entities.md)) to discover what properties
each schema accepts.

### Run the match

```python
with Client() as client:
    response = client.match(query, datasets=["sanctions"])
```

The SDK issues **one HTTP request per `match()` call**. Bulk workflows
(matching N entities concurrently) get their own surface in a later
release; for now wrap `match()` in a loop or in `asyncio.gather` (see
section 6).

### Read the response

`match()` returns a flat `MatchResponse`:

```python
print(response.total.value)        # candidate count
print(response.top)                # highest-scoring result, or None
for hit in response.matches:        # results that crossed the threshold
    print(hit.score, hit.caption, hit.datasets)
```

Each result is a `ScoredEntity` carrying:

- `score` — float in `[0.0, 1.0]`.
- `match` — `True` when `score >= threshold` (defaults to 0.70).
- `caption` — display name from the server.
- `explanations` — per-feature score breakdown (which features fired,
  what their weights were).
- All the usual FtM properties under `entity.properties`.

See [`MatchResponse`](api/models.md) and [`ScoredEntity`](api/models.md)
for the full shape.

## 3. Matching in depth

### The threshold

By default the server returns candidates above `score=0.7` in `matches`,
but the full result set comes back regardless — `results` carries every
candidate the server considered, scored. Lower the threshold to inspect
near-misses:

```python
response = client.match(query, threshold=0.5)
```

### Choosing an algorithm

The server exposes several scoring algorithms. `BEST_ALGORITHM` resolves
to whichever the server currently recommends. Pass it for
forward-compatibility:

```python
from yente_client import BEST_ALGORITHM

response = client.match(query, algorithm=BEST_ALGORITHM)
```

Use `client.algorithms()` to see what's enabled on the target server.

### Narrowing with MatchFilters

Filters constrain which candidates the server considers. Pass them as
kwargs or as a `MatchFilters` object:

```python
from yente_client import MatchFilters

response = client.match(query, datasets=["sanctions"], topics=["sanction"])
# or
filters = MatchFilters(datasets=["sanctions"], topics=["sanction"])
response = client.match(query, filters=filters)
```

When both are supplied, the kwargs win on any field they specify.
[`MatchFilters`](api/filters.md) lists every available field.

### Querying a parent schema matches descendants too

FtM schemas form an inheritance tree. Querying a parent schema matches
every matchable descendant in a single call. The most useful parent for
screening is `LegalEntity`, which covers `Person`, `Organization`,
`Company`, and `PublicBody` — reach for it when the input could be
either an individual or an organization (raw payee strings, list
entries, freeform investigative notes):

```python
from yente_client import LegalEntity

# Matches Person, Organization, Company, PublicBody candidates.
response = client.match(
    LegalEntity(name="Acme Industries"),
    datasets=["sanctions"],
)
```

### Notes on the matchable flag

Schemas with `matchable: false` (e.g. `Document`) can't be queried;
`client.match()` raises `ConfigurationError` before the call. Use
`yente-cli ref schemas --matchable` to find valid targets.

The per-property `matchable` flag (shown by `yente-cli ref schema NAME`)
is a routing detail inside the matcher, not a usefulness indicator.
Send every property you have.

## 4. Fetch and adjacency

Given an entity ID (typically from a match result, search hit, or an
external system), fetch the full record:

```python
entity = client.fetch("NK-aU5ybkbRFJucf8YMwsJvDw")
print(entity.caption, entity.schema_)
print(entity.properties.get("topics"))
```

### Nested vs flat

`fetch()` returns nested entities by default (when properties reference
other entities, those are expanded inline). For the flat shape, pass
`nested=False`. This is useful in data pipelines where you don't want
to recurse:

```python
flat = client.fetch(entity_id, nested=False)
```

### Adjacent entities

The adjacency endpoint exposes paged neighbors by property name:

```python
# All adjacencies, grouped by property:
adj = client.adjacent(entity_id)
for prop, page in adj.adjacent.items():
    print(prop, page.total.value)

# One property at a time, with paging:
page = client.adjacent(entity_id, prop="familyRelative", limit=10, offset=0)
for ent in page.results:
    print(ent.caption)
```

See [`AdjacentResponse`](api/models.md) and
[`AdjacentPropertyResponse`](api/models.md).

## 5. Search (for user-facing search UIs)

`search()` is a **different use case** from matching. Reach for it when
you're building an end-user search experience: autocomplete fields,
browse pages, search-this-database forms where a human is typing into
the input.

```python
results = client.search("acme", datasets=["default"], schema="Company")
for entity in results.results:
    print(entity.caption, entity.id)
```

`search()` returns plain `Entity` objects (no score, no match flag). Use
[`SearchFilters`](api/filters.md) for the full filter shape (countries,
schema, free-text `filter:` clauses, facets).

## 6. Async

`AsyncClient` mirrors `Client` method-for-method. Use it when running
many requests concurrently: the network is the bottleneck, and async
lets one event loop juggle hundreds of in-flight requests.

```python
import asyncio
from yente_client import AsyncClient, Person

async def screen_all(queries: list[Person]) -> list:
    async with AsyncClient() as client:
        return await asyncio.gather(
            *(client.match(q, datasets=["sanctions"]) for q in queries)
        )

responses = asyncio.run(screen_all([
    Person(firstName="Alex", lastName="Smith"),
    Person(firstName="Maria", lastName="Garcia"),
]))
```

`async with` handles cleanup; `await client.aclose()` is the manual form.
See [`AsyncClient`](api/async_client.md).

## 7. Errors

Every error raised by this client inherits from `YenteError`. The tree:

```
YenteError
├── ConfigurationError      # bad client config, non-matchable schema, …
├── TransportError          # network, timeout, TLS
└── APIError                # non-2xx response
    ├── AuthenticationError # 401, 403
    ├── BadRequestError     # 400
    ├── NotFoundError       # 404
    ├── RateLimitError      # 429 (carries .retry_after)
    └── ServerError         # 5xx
```

Catch by category when you want to handle a class of failure:

```python
from yente_client.exceptions import APIError, TransportError

try:
    response = client.match(query)
except RateLimitError as exc:
    sleep(exc.retry_after or 5)
except APIError as exc:
    log.error("server said %s: %s", exc.status_code, exc.detail)
except TransportError:
    # The request never reached the server.
    raise
```

Input-shape errors (typo in a property name, wrong value type) surface
as `pydantic.ValidationError`. The SDK does not wrap or alias it.

Retries are **not** built in. The client raises; callers handle backoff.
See [`exceptions`](api/exceptions.md) for full per-class details.

## 8. Entities and the FtM model

The per-schema input classes (`Person`, `Company`, `Vessel`, …) are
generated from a bundled snapshot of the FtM model. Field names match
the wire format exactly (camelCase).

### Discovering schemas at runtime

```python
from yente_client.schemas import has_schema, iter_properties, matchable_schemata

has_schema("Person")             # True
list(iter_properties("Person"))  # ['abbreviation', 'address', …, 'weight']
matchable_schemata()             # ['Address', 'Airplane', 'Company', …]
```

The same data is available via the CLI:

```bash
yente-cli ref schemas --matchable
yente-cli ref schema Person
```

See [`schemas`](api/schemas.md) for the full lookup helper set.

### Updating the bundled model

The model is a *snapshot*, pinned at SDK-release time. Properties added
upstream don't appear until the next SDK release. Maintainers run
`make regen-model` to refresh.

## 9. Statements — lineage and diagnostics (OpenSanctions API only)

The `/statements` endpoint exposes the atomic claims that compose into
entities: each row is one `(entity_id, prop, value)` triple plus the
dataset that asserted it, the language, the pre-cleaning original value,
and the first/last-seen timestamps. Reach for it for diagnostics:
tracking where a value came from, finding deduplication issues,
auditing data quality. See the
[statement-based data model](https://www.opensanctions.org/docs/statements/)
for background.

```python
for stmt in client.statements(canonical_id="NK-aU5ybkbRFJucf8YMwsJvDw").results:
    print(stmt.dataset, stmt.prop, stmt.value, stmt.first_seen)
```

**Use `canonical_id=` for almost every call.** Pass the ID returned by
`match` / `search` / `fetch`. It returns every source fragment that was
deduplicated into the canonical entity — usually what you want when
investigating a record. `entity_id=` returns only one source's pre-dedup
fragment, which is useful for source-level audits but is *not* a
substitute for `canonical_id`: the same person across five sanctions
lists has five distinct `entity_id` values and only one `canonical_id`.

Only the OpenSanctions API serves this endpoint — it is backed by a
Postgres instance that yente does not ship. Calls against a yente
instance surface as `NotFoundError`.

## Where to go next

- [CLI overview](cli.md) — `yente-cli`, agent automations, shell pipelines.
- [API reference](api/index.md) — full signatures of every public symbol.
- [OpenSanctions docs](https://www.opensanctions.org/docs/) — domain
  context: sanctions screening, the FtM data model, the API
  quickstart, getting an API key.
