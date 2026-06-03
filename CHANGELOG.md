# Changelog

All notable changes to **yente-client** (the Python SDK) and **yente-cli**
(the command-line tool) are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Python SDK over the yente / OpenSanctions API, sync and async surfaces:
  `Client.match()`, `search()`, `fetch()`, `adjacent()`, `catalog()`,
  `algorithms()`, `healthz()`, `readyz()`, plus the `AsyncClient`
  equivalents. v2-flat response shape over the v1 wire (one HTTP call
  per `match()`).
- Per-schema entity input classes generated from a bundled FtM model
  snapshot (`Person`, `Company`, `Vessel`, …), with camelCase fields
  matching the wire format.
- `MatchFilters` / `SearchFilters` for dataset / topic / schema /
  country narrowing.
- `YenteError` exception tree: `ConfigurationError`, `APIError`
  (and subtypes `Authentication`, `BadRequest`, `NotFound`, `RateLimit`,
  `Server`), `TransportError`.
- `yente-cli` command-line tool (ships with the `yente-client[cli]`
  install extra): `match`, `search`, `fetch`, `catalog`, `algorithms`,
  `status`, `healthz`, `ref schemas`, `ref schema NAME`, `ref topics`,
  `ref countries`. Designed for LLM-agent automation: workflow blocks,
  per-command worked examples, output-shape documentation, fuzzy
  schema/property suggestions on typos.
- Schema-level matchable enforcement: `Client.match()` raises
  `ConfigurationError` client-side when the target schema isn't
  matchable, preempting the server-side 4xx.
- `ref schema NAME` shows a `directly_scored` column with a legend
  explaining the three indirect-impact mechanisms (name reconstruction,
  weakAlias/abbreviation cross-comparison, gender qualifier).
- Documentation under `python/docs/` (mkdocs + Material theme +
  mkdocstrings): tutorial, CLI overview, auto-extracted API reference.
- Tutorial and `yente-cli match --help` now explain schema inheritance
  (e.g. `LegalEntity` covers `Person` + `Organization` + `Company` +
  `PublicBody` in a single call) and when to prefer the parent schema.

[Unreleased]: https://github.com/opensanctions/yente-client/compare/HEAD
