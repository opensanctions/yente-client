# Changelog

All notable changes to **yente-client** (the Python SDK) and **yente-cli**
(the command-line tool) are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Python SDK over the yente / OpenSanctions API, sync and async surfaces:
  `Client.match()`, `search()`, `fetch()`, `adjacent()`, `datasets()`,
  `algorithms()`, `statements()`, `healthz()`, `readyz()`, plus the
  `AsyncClient` equivalents. v2-flat response shape over the v1 wire
  (one HTTP call per `match()`). `statements()` is OpenSanctions-API
  only; yente instances return 404, which the SDK rewraps with a
  pointed `NotFoundError` message.
- Per-schema entity input classes generated from a bundled FtM model
  snapshot (`Person`, `Company`, `Vessel`, …), with camelCase fields
  matching the wire format.
- `MatchFilters` / `SearchFilters` for dataset / topic / schema /
  country narrowing.
- `YenteError` exception tree: `ConfigurationError`, `APIError`
  (and subtypes `Authentication`, `BadRequest`, `NotFound`, `RateLimit`,
  `Server`), `TransportError`.
- `yente-cli` command-line tool (ships with the `yente-client[cli]`
  install extra): `match`, `search`, `fetch`, `datasets`, `statements`,
  `algorithms`, `status`, `ref schemas`, `ref schema NAME`, `ref topics`,
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
- Per-property matchability surfaces in `ref schema NAME` (table column
  and `-f json` field) under its real name `matchable`, not the
  previously-invented `directly_scored`. Accompanying legend states the
  flag is a routing detail inside the matcher, not a "useful for
  matching" indicator — non-matchable properties (`firstName`,
  `weakAlias`, `gender`, …) are real scoring inputs.
- Tutorial and CLI overview trimmed: the matchable-flag subsections,
  CLI-vs-SDK comparison, and agent-help enumeration compress to
  one-liners (full nuance lives in `--help`, the `ref schema` legend,
  and the API reference).

[Unreleased]: https://github.com/opensanctions/yente-client/compare/HEAD
