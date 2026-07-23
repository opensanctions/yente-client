# Changelog

All notable changes to **yente-client** (the Python SDK) and **yente-cli**
(the command-line tool) are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-23

### Added

- **`Client.match_iter()` / `AsyncClient.match_iter()`** — stream `/match`
  calls over any iterable of `(key, entity)` pairs with bounded concurrency
  (`workers`, default 4). Results yield in completion order; input is pulled
  lazily so memory stays flat on large runs. `on_error="collect"` returns
  per-item failures in-band as the new **`MatchError`** dataclass instead of
  aborting the stream.

- **`cutoff` parameter** on `match()` / `match_iter()` — drop candidates
  scoring below the given value from the response entirely (server default
  0.50), complementing `threshold`, which only sets the `match` flag.

- **`yente-cli screen`** — batch-screen a CSV of entities against `/match`.
  Maps input columns to FtM properties (`-i col=prop`, `--schema-column`),
  runs concurrent queries (`--workers`, default 20), and writes one output
  row per candidate with all input columns passed through and `match_`-prefixed
  result columns (`--url`, `--explanation`, `-o prop=column` for candidate
  properties). Failed rows surface in a `match_error` column and exit code 5
  rather than aborting the run.

- **`schemas.describe_schema()` / `schema_index()` / `schema_properties()` /
  `property_matchable()` / `describe_type()` / `type_values()`** — a shared,
  condensed FtM model projection (schemata, properties, and value types) used by
  both `yente-cli ref` and the MCP `describe_schema`. Keeps the high-signal
  fields (descriptions, `extends`, `range`/`reverse`) and drops structural cruft
  (`label`/`plural`/flattened ancestor closure on schemata;
  `maxLength`/`qname`/`deprecated`/`format` on properties), and omits empty
  values. Boolean flags (`matchable`, `edge`) appear only when true, and
  `matchable` resolves the FtM type default. `describe_schema` splits its fields
  into `properties` (settable scalar attributes — the `match_entity` inputs) and
  `relations` (entity-typed edges as compact `{name, range, reverse}` — the
  `fetch_entity_relations` targets).

- **`Statement.origin`** — how a claim was produced (`"inferred"`, `"patch"`,
  …; unset for plain crawled data), previously dropped when parsing the
  `/statements` wire format.

- **`Client.programs()` / `AsyncClient.programs()`** — fetch the
  sanctions-program catalog (`Program` / `ProgramIssuer` / `ProgramsResponse`
  models), resolving the `programId` codes sanctioned entities carry into
  program title, issuer, policy summary, and measures. Served from the public
  artifact at `PROGRAMS_URL` (data.opensanctions.org), independent of
  `base_url`.

- **ETag revalidation** for `programs()` and `datasets()`: clients keep a
  per-URL `(etag, body)` pair and send `If-None-Match`, serving the held body
  on `304 Not Modified`. Transparent — servers without `ETag` support (yente's
  `/catalog` today, see opensanctions/yente#1202) get plain-fetch behavior.

- **`yente-cli programs [KEY]`** — list sanctions programs, or show one
  program's full metadata; `KEY` also resolves via program aliases.

- **Documentation site** published at
  [yenteclient.followthemoney.tech](https://yenteclient.followthemoney.tech/)
  (GitHub Pages, deployed from `main`), including a new MCP server page. The
  repository and package now carry the MIT license text (the metadata already
  declared MIT).

### Changed

- **Entity properties accept `None` as "unset"**: `Person(name="X",
  birthDate=None)` now validates, coerces to an empty list, and omits the
  property from the wire payload — so optional source fields (a CSV cell, a
  nullable database column) pass through without guards. Previously an
  explicit `None` raised a `ValidationError`.

- `yente-cli ref schemas` / `ref schema NAME` output is leaner (the projection
  above): the `abstract`/`required`/`deprecated` columns/fields are gone.

- **MCP server** (`yente-mcp`, `pip install 'yente-client[mcp]'`): exposes the
  matching surface to LLM agents over the Model Context Protocol as five
  entity tools (`match_entity`, `search_entities`, `fetch_entity_by_id`,
  `fetch_entity_relations`, `fetch_entity_statements`) plus `describe_*`
  lookup tools (`describe_schema`, `describe_topics`, `describe_countries`,
  `describe_dataset`, `describe_program`) and `ftm://` resources, built on
  `AsyncClient`. Every code in entity-bearing results resolves inline: topic
  and country labels from the bundled model, dataset and program titles as
  attached `dataset_titles` / `program_titles` legends (cached live lookups,
  failure-soft); matching algorithms are deliberately not exposed (server
  default applies). Runs over streamable-HTTP; auth is bearer-token pass-through (the
  token is the caller's OpenSanctions API key, forwarded downstream).
  Skeleton — the schema/model surface and response shaping are covered by
  tests; the network tools are not yet exercised end-to-end.

### Fixed

- **`yente-cli` help epilogs render as written**: Typer's rich help collapses
  single newlines in epilogs, which mangled every command's EXAMPLES /
  OUTPUT / EXIT CODES block into run-on paragraphs. The blocks now print
  verbatim, preserving indentation and alignment.

## [0.1.0] - 2026-06-07

First public release of the `yente-client` Python SDK and the `yente-cli`
command-line tool.

### Added

- **Python SDK** over the yente / OpenSanctions matching API, with parallel
  sync (`Client`) and async (`AsyncClient`) surfaces:
  `match()`, `search()`, `fetch()`, `adjacent()`, `datasets()`,
  `algorithms()`, `statements()`, `healthz()`, `readyz()`. A v2-flat
  response shape over the v1 wire (one HTTP call per `match()`).
  `statements()` is OpenSanctions-API only; yente instances raise a
  pointed `NotFoundError`.
- **Per-schema entity input classes** (`Person`, `Company`, `Vessel`, …),
  generated from a bundled followthemoney model snapshot, with camelCase
  fields matching the wire format. Bundled model: followthemoney v4.9.0.
- **`MatchFilters` / `SearchFilters`** for dataset / topic / schema /
  country narrowing.
- **Schema-level matchable enforcement**: `Client.match()` raises
  `ConfigurationError` client-side when the target schema isn't matchable,
  preempting the server-side 4xx.
- **Expanded `Dataset` metadata**: `summary`, `url`, `category`, `tags`,
  `entity_count`, `thing_count`, `updated_at`, `last_export`,
  `deprecated`/`deprecation`, plus nested `coverage` (`DataCoverage`) and
  `publisher` (`DataPublisher`) objects.
- **`YenteError` exception tree**: `ConfigurationError`, `APIError`
  (subtypes `Authentication`, `BadRequest`, `NotFound`, `RateLimit`,
  `Server`), and `TransportError`.
- **`yente-cli` command-line tool** (ships with the `yente-client[cli]`
  extra): `match`, `search`, `fetch`, `datasets [NAME]`, `statements`,
  `algorithms`, `status`, and the `ref` subcommands (`schemas`,
  `schema NAME`, `topics`, `countries`). Built for LLM-agent automation:
  workflow blocks, per-command worked examples, output-shape documentation,
  and fuzzy schema/property suggestions on typos. `ref schema NAME` surfaces
  each property's `matchable` flag (table column and `-f json` field) with a
  legend clarifying it's a matcher routing detail, not a "useful for
  matching" indicator — non-matchable properties (`firstName`, `weakAlias`,
  `gender`, …) are real scoring inputs. `datasets NAME` renders one
  dataset's full metadata, including only the fields a given dataset carries.
- **Documentation** under `python/docs/` (mkdocs + Material theme +
  mkdocstrings): tutorial, CLI overview, and auto-extracted API reference.
  Covers schema inheritance (e.g. `LegalEntity` spanning `Person`,
  `Organization`, `Company`, and `PublicBody` in a single call) and
  entity-reference graph traversal via statements.

[Unreleased]: https://github.com/opensanctions/yente-client/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/opensanctions/yente-client/releases/tag/v0.1.0
