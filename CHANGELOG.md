# Changelog

All notable changes to **yente-client** (the Python SDK) and **yente-cli**
(the command-line tool) are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  `relations` (entity-typed edges as compact `{name, range}` — the
  `fetch_entity_relations` targets).

### Changed

- `yente-cli ref schemas` / `ref schema NAME` output is leaner (the projection
  above): the `abstract`/`required`/`deprecated` columns/fields are gone.

- **MCP server** (`yente-mcp`, `pip install 'yente-client[mcp]'`): exposes the
  matching surface to LLM agents over the Model Context Protocol as five tools
  (`match_entity`, `search_entities`, `fetch_entity_by_id`,
  `fetch_entity_relations`, `describe_schema`) and `ftm://` / `yente://`
  resources, built on `AsyncClient`. Runs over streamable-HTTP; auth is
  bearer-token pass-through (the token is the caller's OpenSanctions API key,
  forwarded downstream). Skeleton — the schema/model surface and response
  shaping are covered by tests; the network tools are not yet exercised
  end-to-end.

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
