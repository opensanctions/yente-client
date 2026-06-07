# yenteclient — project conventions

Project-specific rules for working in this repo. The user's global
`~/.claude/CLAUDE.md` still applies — these are extensions or explicit choices
where this project deviates from defaults.

## Names and identity

Three names diverge, easily conflated:

- **PyPI package:** `yente-client` (kebab). What `pip install` takes.
- **Python import:** `yente_client` (snake). What `import` takes.
- **CLI binary:** `yente-cli`. Console-script entry point, shipped by the
  `yente-client[cli]` install extra.
- **GitHub repo:** `opensanctions/yente-client`.
- **User-Agent product token:** `yente-client/<version>`. Stays
  package-shaped on the wire.

## Python target

- **Floor: Python 3.11.** Modern syntax is available natively; no compatibility
  shims for older versions.
- Develop on whatever's installed locally; CI matrix covers 3.11 / 3.12 / 3.13.

## Imports

- **Absolute imports only.** No `from .foo import bar` anywhere in the package.
  Use `from yente_client.foo import bar`.
- Standard `isort` grouping: stdlib → third-party → first-party. Ruff enforces.
- Star imports are forbidden except in generated `__init__.py` files that
  re-export per-schema entity classes — those have `# noqa: F401, F403`.

## Typing

- **Modern PEP 604 / 585 syntax**: `list[str]`, `dict[str, Any]`, `int | None`.
  No imports from `typing` for those — use the builtins. `typing.Optional`,
  `typing.Union`, `typing.List` etc. are not used.
- `Final`, `Literal`, `TypeAlias`, `ClassVar`, `Self`, `overload` — fine to use
  from `typing` when needed.
- `mypy --strict` must pass on `src/yente_client/`. New code that adds
  `# type: ignore` needs a code (`[attr-defined]` etc.) and a comment.
- **Do not add `from __future__ import annotations`** unless you have a
  concrete need (forward references, circular imports). Modern syntax works
  natively at 3.11; adding the future import everywhere causes subtle issues
  with Pydantic eval (we hit one in M2). If a file genuinely needs it, leave
  a comment saying why.

## API surface positioning

Two policies govern how the matching surface is described in docs, CLI
help, error messages, examples, and onboarding material.

### Search vs match

`/search` is for **end-user search UIs** — autocomplete fields, browse
pages, search-this-database boxes where a human is typing into the input.
It returns plain `Entity` objects (no score, no match flag).

**Any matching task uses `/match`, even with partial input** (just a name,
name + country, etc.). Never frame `search` as a fallback for `match` on
incomplete data; that sends users to the wrong endpoint and yields
unscored/unranked results. The CLI's `PICK A COMMAND` block and the
tutorial's section 5 are the canonical phrasings — match new wording to
them.

### Matchable flag

- **Schema-level `matchable`** is enforced by the server and preempted
  client-side: `Client.match()` raises `ConfigurationError` before the
  round-trip when the entity's schema isn't matchable. The CLI mirrors
  the check in `_build_entity_input`.
- **Property-level `matchable`** governs one specific routing decision
  inside the matcher (whether the property's value is used as a
  candidate-filter clause in the search query). It is **NOT** a
  "useful for matching" indicator. Non-matchable properties are real
  scoring inputs; they're routed through dedicated matcher features:
  - **Name reconstruction**: `firstName`, `middleName`, `lastName`,
    `fatherName`, … feed name-comparison features via name reconstruction.
  - **Cross-comparison**: `weakAlias`, `abbreviation` are compared
    against candidate names during scoring.
  - **Qualifier features**: `gender` is consumed by a mismatch feature.
- **Use the FtM term `matchable` as-is in docs / CLI / API output.**
  Don't invent parallel names like `directly_scored`. Don't editorialize
  about "indirect" impact — the routing distinction is real but
  non-matchable properties drive scoring just as directly. The actionable
  rule for users is "send every property you have".
- **Do not filter, warn on, or discourage non-matchable properties.** The
  codegen includes every non-stub property.

## Drift-prone facts

Never bake specific counts from the bundled FtM model into docstrings,
comments, plan docs, or `--help` output. **No** "69 schemas", "71 topics",
"20 property types", "3 genders" etc. — these all change when upstream
ships a new model snapshot and every reference becomes a small lie.

Acceptable forms:

- "every FtM schema" / "the full schema set"
- "the Topic enum (sourced from `model.types["topic"].values`)"
- "one class per FtM schema"
- Anchors that the model can't break: specific class names (`Person`,
  `Company`), specific topic strings (`"sanction"`, `"role.pep"`).

If a test needs to assert against the bundled model, check membership of
known anchors plus a sanity lower bound, never an exact count. The
`regen_model.py --check` CI step is the authoritative drift detector.

## Docstrings

Hybrid: Google-style structure, project-style content. The user's global rule
("lead with why, not how") wins on content; Google's sections give us a
consistent shape.

**Shape:**

1. **Imperative one-line summary.** `"Fetch a single entity by ID."` — not
   `"Fetches…"`.
2. **Why / when paragraph** (optional). One paragraph max. Hidden constraints,
   non-obvious motivation, "use this when…" guidance. Skip if the one-liner
   covers it.
3. **`Args:` / `Returns:` / `Raises:` sections** — *only when they add
   information beyond what the type annotations show*. Don't write `args: x
   (int): the x value` for a typed parameter; do write `Args:` when behaviour
   depends on a flag value, or when an argument has constraints that aren't
   in the type.

Private functions (leading underscore) get a one-line docstring or none —
spend effort on the public surface.

## Naming

- `snake_case` for functions, methods, variables, modules.
- `PascalCase` for classes (including generated entity classes).
- `SCREAMING_SNAKE_CASE` for module-level constants (`BEST_ALGORITHM`).
- `_leading_underscore` for module-private (anything not exported).
- Double-leading-underscore name-mangling: avoid.
- **camelCase exception:** the per-schema entity classes (`Person`, `Company`)
  carry their FtM properties as `camelCase` fields (`firstName`, `birthDate`)
  to match the wire format. This is intentional and only applies to entity
  input classes. All other naming is snake_case.

## Errors

- Every project-raised error inherits from `YenteError` (defined in
  `yente_client.exceptions`).
- `pydantic.ValidationError` is raised separately for input-shape mistakes; we
  don't wrap or alias it.
- Wrap external errors with `raise NewError(...) from exc` to preserve context.
- Don't catch `Exception` broadly. Catch what you know how to handle.

## Logging

- **Stdlib `logging`** when M4+ work introduces logging.
- Module-level logger: `log = logging.getLogger(__name__)` at the top of the
  file (after imports).
- The SDK should be quiet by default. Callers configure handlers / levels.
- Don't log inside hot paths or per-request unless gated on debug level.

## Custom exceptions

- Defined in `yente_client/exceptions.py`.
- `YenteError` is the only base class for client-raised errors.
- Subclasses carry structured attributes (status_code, retry_after, etc.) in
  addition to the message — caller can branch on type or read fields.

## File / module layout

- `src/yente_client/` package layout. Tests under `python/tests/` at the
  repo root (not inside the package).
- Public surface is re-exported from `yente_client/__init__.py`.
- Generated files: `_generated.py` and `_literals.py` carry `# ruff: noqa` at
  the top and are produced by `scripts/regen_model.py`. Don't hand-edit.
- Files can grow as long as they're cohesive. If `client.py` crosses 500
  lines, consider splitting endpoints into a submodule. We're under that
  ceiling today.

## Tests

- `pytest` with `asyncio_mode = "auto"` (configured in `pyproject.toml`).
  `async def test_*` works without per-test decorators.
- Fixtures live in `python/tests/conftest.py`. Shared: `load_fixture`,
  `make_client`, `make_async_client`, `live_client`, `live_async_client`.
- **`@pytest.mark.live`** for tests that hit a real yente; gated on
  `OPENSANCTIONS_API_KEY`. Run locally via `pytest -m live`; CI splits them
  into a separate job.
- Mock HTTP via `httpx.MockTransport(handler)` passed through the `transport=`
  kwarg on `Client` / `AsyncClient`. We don't use `respx` despite having it
  installed.
- Fixtures (JSON response bodies) live in **`testdata/` at the repo root**,
  not in `python/tests/fixtures/` — shared with the future TS SDK.
- Prefer separate test functions over `@pytest.mark.parametrize` when the
  cases are conceptually different. Parametrize when you're varying one input
  and the assertions are identical (e.g. `test_invalid_app_name_raises[bad]`).

## Codegen

- `scripts/regen_model.py` fetches the FtM model from
  `https://github.com/opensanctions/followthemoney/releases/latest/download/model.json`,
  writes `model/model.json`, copies the snapshot to the package, renders Jinja
  templates, and runs `ruff format` as a postprocess.
- The payload is the model itself at the top level: `{schemata, types, version}`.
  `version` is the followthemoney semver (e.g. `"4.8.4"`) and surfaces in
  `yente-cli status` as the bundled-model identifier.
- Determinism rules: schemas alphabetical, properties alphabetical, JSON
  written with `sort_keys=True`, compact separators, trailing newline.
- CI runs `regen_model.py --check --skip-fetch` to detect drift between the
  templates and the committed generated files.
- **Never hand-edit generated files.** Update the template, run regen, commit.

### Upgrading the bundled FtM model

`regen_model.py` (no flags) follows `latest/download`, so a plain regen pulls
whatever FtM release is current. To bump the bundled snapshot:

```bash
# 1. Fetch latest + regenerate (model.json, package snapshot, _literals.py,
#    _generated.py, entities/__init__.py). make regen-model wraps this.
make regen-model

# 2. Confirm the new bundled version, then verify nothing drifted / broke.
python scripts/regen_model.py --check --skip-fetch   # or: make regen-model-check
cd python && ruff check . && ruff format --check . && mypy && pytest -m "not live"

# 3. Eyeball the diff — model upgrades are usually additive (new schemas,
#    properties, topic/gender values). Removals are breaking; flag them.
git diff --stat
```

Note the FtM version surfaces in `yente-cli status`; a bump is user-visible and
warrants a `CHANGELOG.md` line.

## Documentation

- Lives under `python/docs/`, built with **mkdocs + mkdocs-material +
  mkdocstrings**.
- Three hand-written pages: `index.md`, `tutorial.md`, `cli.md`. They are
  the only prose pages — keep additions inside this small set unless the
  surface justifies a new file.
- The `api/` tree is a set of thin `:::`-directive stubs; mkdocstrings
  expands them at build time from public docstrings. Updating a docstring
  updates the API reference; no separate regeneration step.
- Prose follows the FollowTheMoney styleguide at
  `/home/pudo/code/followthemoney/docs/styleguide.md` — applies to both
  these pages and any future doc work.
- `make docs` builds, `make docs-serve` runs the dev server, `make docs-check`
  builds with `--strict` (mirrored in CI; a broken link or unresolved
  reference fails the build).
- Build output goes to `python/site/` and is gitignored.

## Releasing

- Version is bumped with **`bump2version`** (config at `.bumpversion.cfg`,
  pinned to `1.0.1`). Run from the repo root:

      bump2version --verbose minor   # or patch / major

  This edits `python/pyproject.toml`, commits, and tags `vX.Y.Z`.
- Pushing the tag triggers `.github/workflows/release.yml`: full matrix
  validation, tag-matches-version assertion, wheel + sdist build, install
  smoke against a clean venv, then PyPI publish via OIDC trusted publishing
  with PEP 740 attestations. A GitHub Release is created with the
  `[Unreleased]` section of `CHANGELOG.md` as the notes.
- `CHANGELOG.md` follows Keep-a-Changelog. PRs are nudged toward updating
  `[Unreleased]` via an advisory CI step (`scripts/check_changelog.py`);
  put `[skip changelog]` in the PR body for genuinely user-invisible
  changes.
- One-time PyPI setup (documented inline at the top of `release.yml`):
  configure a Trusted Publisher for the `release.yml` workflow with
  environment `pypi`.

## Working in this repo

- Project-local venv at `python/.venv/`. System Python 3.14 lacks
  `ensurepip`, so we create it via `uv venv` and install via
  `uv pip install --python python/.venv/bin/python -e python[dev]`.
- Make targets: `setup`, `regen-model`, `regen-model-check`, `test`, `lint`,
  `docs`, `docs-serve`, `docs-check`. CI runs the underlying commands directly.
- `.env` at repo root (gitignored) carries `OPENSANCTIONS_API_KEY` and
  `YENTE_BASE_URL`. Conftest loads it for local convenience.
- Don't commit secrets. Don't push without explicit user direction (per the
  global CLAUDE.md pacing rules).
