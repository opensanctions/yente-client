#!/usr/bin/env python3
"""Regenerate the yente-client Python code from the FtM model snapshot.

Usage:
    python scripts/regen_model.py                  # fetch + regenerate
    python scripts/regen_model.py --skip-fetch     # use existing model/model.json
    python scripts/regen_model.py --check          # compare vs committed (CI use)
    python scripts/regen_model.py --check --skip-fetch  # CI check

Outputs (regenerated; commit after running without --check):
    model/model.json                                       (only on full fetch)
    python/src/yente_client/schemas/model.json             (always copied)
    python/src/yente_client/schemas/_literals.py           (templated)
    python/src/yente_client/entities/_generated.py         (templated)
    python/src/yente_client/entities/__init__.py           (templated)

The script uses stdlib `urllib` to fetch, Jinja2 for templating, and
shells out to `ruff format` as a final pass so the output matches the
repo's formatting. ruff runs through `prek exec`, so the generated code and
the `ruff-format` hook always use the same pinned ruff. Determinism:
schemas, properties, type values are all sorted alphabetically; JSON is
written with sort_keys=True.
"""

from __future__ import annotations

import argparse
import difflib
import json
import keyword
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import jinja2


def _safe_field_name(prop_name: str) -> tuple[str, str | None]:
    """Return ``(field_name, alias)`` for an FtM property name.

    If the property name is a Python keyword (e.g. ``from`` on ``Email``),
    return a trailing-underscore version as the field name plus the original
    as the Pydantic alias. Otherwise return ``(prop_name, None)``.
    """
    if keyword.iskeyword(prop_name) or prop_name in {"True", "False", "None"}:
        return prop_name + "_", prop_name
    return prop_name, None

# followthemoney publishes the canonical model.json as a release artifact.
# The `latest/download` URL 302-redirects to whichever release is currently
# tagged latest — pinning to a specific version would be safer but also
# slower-rotting; we deliberately follow upstream here because regen runs
# are explicit, version-controlled, and pre-release-gated.
DEFAULT_MODEL_URL = (
    "https://github.com/opensanctions/followthemoney/releases/latest/download/model.json"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_FILE = REPO_ROOT / "model" / "model.json"
PKG_MODEL_FILE = REPO_ROOT / "python" / "src" / "yente_client" / "schemas" / "model.json"
LITERALS_FILE = REPO_ROOT / "python" / "src" / "yente_client" / "schemas" / "_literals.py"
GENERATED_FILE = REPO_ROOT / "python" / "src" / "yente_client" / "entities" / "_generated.py"
INIT_FILE = REPO_ROOT / "python" / "src" / "yente_client" / "entities" / "__init__.py"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
PREK_BIN = REPO_ROOT / "python" / ".venv" / "bin" / "prek"


def fetch_model(url: str) -> dict[str, Any]:
    """Fetch the FtM model JSON from `url`."""
    print(f"fetching {url}...", file=sys.stderr)
    with urllib.request.urlopen(url) as response:
        data: dict[str, Any] = json.loads(response.read())
    return data


def encode_model(model: dict[str, Any]) -> str:
    """Encode `model` to the canonical on-disk JSON format."""
    return json.dumps(model, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"


def collect_properties(schemata: dict[str, dict[str, Any]], schema_name: str) -> list[dict[str, Any]]:
    """Return the flat property list for `schema_name`, walking inherited schemas.

    `model.json` stores own-properties per schema; we walk the pre-flattened
    `schemata` ancestor list and merge. If a property name appears on multiple
    ancestors, the first occurrence in the ancestor list wins (matches FtM
    Python's MRO behaviour). Output is sorted alphabetically by name for
    deterministic diffs.
    """
    schema = schemata[schema_name]
    seen: set[str] = set()
    properties: list[dict[str, Any]] = []
    for ancestor in schema["schemata"]:
        anc_props = schemata.get(ancestor, {}).get("properties", {})
        for prop_name, prop_def in anc_props.items():
            if prop_name in seen:
                continue
            # Stub properties are the reverse side of directed entity edges
            # (e.g. Address.things ← Thing.addressEntity). They never make
            # sense on a query payload, so we omit them from the input class.
            if prop_def.get("stub"):
                continue
            seen.add(prop_name)
            field_name, alias = _safe_field_name(prop_name)
            description = (prop_def.get("description") or "").strip()
            label = (prop_def.get("label") or "").strip()
            # The DEPRECATED comment uses the real description (may be empty);
            # the Field(description=...) falls back to label → identifier so
            # IDE hover always shows something useful, even on terse props.
            properties.append(
                {
                    "name": prop_name,
                    "field_name": field_name,
                    "alias": alias,
                    "description": description,
                    "field_description": description or label or prop_name,
                    "deprecated": bool(prop_def.get("deprecated", False)),
                    "from_schema": ancestor,
                }
            )
    properties.sort(key=lambda p: p["name"])
    return properties


def build_context(model_json: dict[str, Any]) -> dict[str, Any]:
    """Build the Jinja context from the raw FtM model snapshot.

    The payload published as a GitHub release artifact has the shape
    ``{schemata, types, version}`` — no wrapper, the model is at the top
    level. (The older ``data.opensanctions.org/meta/model.json`` payload
    wrapped it under ``["model"]``; that source is no longer used.)
    """
    schemata = model_json["schemata"]
    types = model_json["types"]

    schemas = []
    for name in sorted(schemata):
        schema = schemata[name]
        description = (schema.get("description") or "").strip().replace('"""', '"​""')
        schemas.append(
            {
                "name": name,
                "description": description,
                "properties": collect_properties(schemata, name),
            }
        )

    return {
        "schemas": schemas,
        "schema_names": sorted(schemata),
        "type_names": sorted(types),
        "topic_values": sorted(types.get("topic", {}).get("values", {})),
        "gender_values": sorted(types.get("gender", {}).get("values", {})),
    }


def render_artefacts(model_json: dict[str, Any]) -> dict[Path, str]:
    """Render all artefacts to `path -> formatted content`."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["py_str"] = lambda s: json.dumps(s, ensure_ascii=False)

    context = build_context(model_json)
    renderings: dict[Path, str] = {}
    for path, template_name in (
        (GENERATED_FILE, "python_entities.py.j2"),
        (LITERALS_FILE, "python_literals.py.j2"),
        (INIT_FILE, "python_entities_init.py.j2"),
    ):
        rendered = env.get_template(template_name).render(**context)
        renderings[path] = ruff_format(rendered, path)
    return renderings


def _find_prek() -> str:
    """Locate the ``prek`` binary: prefer the project venv (local dev), fall
    back to whatever is on ``PATH`` (CI, where there's no per-project venv)."""
    if PREK_BIN.exists():
        return str(PREK_BIN)
    found = shutil.which("prek")
    if found is None:
        raise RuntimeError(
            "prek not found. Install with `pip install prek` or run `make setup`."
        )
    return found


def ruff_format(text: str, path: Path) -> str:
    """Run ``ruff format`` on `text` with the per-file settings for ``path``.

    Uses ``--stdin-filename`` so ruff reads the project's ``pyproject.toml``
    and applies the right line-length and target-version for the file.
    """
    result = subprocess.run(
        [_find_prek(), "exec", "ruff-format", "--", "ruff", "format", "--stdin-filename", str(path), "-"],
        cwd=REPO_ROOT,
        input=text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def write_outputs(model_json: dict[str, Any], artefacts: dict[Path, str], *, write_model_file: bool) -> int:
    """Write artefacts to disk; report what changed."""
    if write_model_file:
        MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODEL_FILE.write_text(encode_model(model_json))
        print(f"wrote {MODEL_FILE.relative_to(REPO_ROOT)}", file=sys.stderr)
    PKG_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PKG_MODEL_FILE.write_text(encode_model(model_json))
    print(f"wrote {PKG_MODEL_FILE.relative_to(REPO_ROOT)}", file=sys.stderr)
    for path, content in artefacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


def check_outputs(model_json: dict[str, Any], artefacts: dict[Path, str], *, check_model_file: bool) -> int:
    """Compare rendered artefacts against committed files; exit nonzero on diff."""
    differences: list[str] = []
    model_text = encode_model(model_json)
    targets = []
    if check_model_file:
        targets.append((MODEL_FILE, model_text))
    targets.append((PKG_MODEL_FILE, model_text))
    targets.extend(artefacts.items())

    for path, expected in targets:
        rel = path.relative_to(REPO_ROOT)
        if not path.exists():
            differences.append(f"{rel}: missing")
            continue
        actual = path.read_text()
        if actual != expected:
            diff = "".join(
                difflib.unified_diff(
                    actual.splitlines(keepends=True),
                    expected.splitlines(keepends=True),
                    fromfile=f"{rel} (committed)",
                    tofile=f"{rel} (regenerated)",
                    n=2,
                )
            )
            differences.append(f"{rel}: differs\n{diff}")

    if differences:
        for d in differences:
            print(d, file=sys.stderr)
        print(
            "\n--check failed: artefacts out of date. Run `make regen-model` and commit.",
            file=sys.stderr,
        )
        return 1
    print("--check passed: all artefacts up to date", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare against committed files; exit 1 on diff")
    parser.add_argument("--skip-fetch", action="store_true", help="use existing model/model.json")
    parser.add_argument("--model-url", default=DEFAULT_MODEL_URL, help="override source URL")
    args = parser.parse_args()

    if args.skip_fetch:
        if not MODEL_FILE.exists():
            print(f"error: --skip-fetch given but {MODEL_FILE} does not exist", file=sys.stderr)
            return 1
        model_json: dict[str, Any] = json.loads(MODEL_FILE.read_text())
    else:
        model_json = fetch_model(args.model_url)

    artefacts = render_artefacts(model_json)

    # On --skip-fetch, the existing model/model.json IS the source of truth; we
    # don't re-encode it (and thus can't detect "is this file in canonical form?")
    # except by writing during full regen.
    if args.check:
        return check_outputs(model_json, artefacts, check_model_file=not args.skip_fetch)
    return write_outputs(model_json, artefacts, write_model_file=not args.skip_fetch)


if __name__ == "__main__":
    sys.exit(main())
