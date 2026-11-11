#!/usr/bin/env python3
"""Run the MCP use cases against a fresh, headless Claude and record what it does.

Each case prompt from ``docs/mcp_use_cases.md`` goes verbatim to ``claude -p``.
The run is attached to ``yente-mcp`` alone, has no built-in tools, and starts
in an empty working directory, so no project context reaches the model. The
system prompt describes only the app the assistant lives in, never how to use
the MCP: the run measures what the server's own docstrings, instructions and
responses convey. Expected queries and acceptance criteria are never shown to
the model; they're copied into the summary for whoever judges the answers.

Usage (from the repo root)::

    python/.venv/bin/python scripts/run_mcp_cases.py            # every case, once
    python/.venv/bin/python scripts/run_mcp_cases.py 2 3a 6b -n 2

Needs ``OPENSANCTIONS_API_KEY`` (read from the environment or the repo's
``.env``) for live data. ``--bare`` additionally skips the user's global
CLAUDE.md and memory, but Claude Code then authenticates only via
``ANTHROPIC_API_KEY``.

Writes ``runs/<timestamp>/`` (gitignored): one ``<case>-<n>.jsonl`` stream per
run and a ``summary.md`` listing each run's tool calls and final answer next to
the case's criteria.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "docs" / "mcp_use_cases.md"
YENTE_MCP = ROOT / "python" / ".venv" / "bin" / "yente-mcp"

APP_PROMPT = (
    "You are the assistant inside a bank's customer due-diligence application. "
    "Compliance analysts use you while onboarding and reviewing customers and "
    "their counterparties. Answer their questions."
)

# Per-turn wall-clock limit for one `claude -p` call.
TURN_TIMEOUT_SECONDS = 600

_HEADING = re.compile(r"^#{3,4} (\d+[a-z]?)\. (.+)$")
_FIELD = re.compile(r"^\*\*(Prompt|Follow-up):\*\* ?(.*)$")
_PLACEHOLDER = re.compile(r"<[^>]+>")


@dataclass
class Case:
    """One runnable use case: its prompts plus the judging text for the summary."""

    key: str
    title: str
    prompt: str
    follow_up: str | None
    criteria: str


def parse_cases(text: str) -> list[Case]:
    """Read the cases with a prompt out of the use-case document.

    A case's body runs to the next case heading; parent headings without a
    prompt of their own (e.g. case 3, which only groups 3a / 3b) are skipped.
    """
    cases: list[Case] = []
    sections: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            sections.append((heading.group(1), heading.group(2), []))
        elif line.startswith("## ") and sections:
            sections.append(("", "", []))  # a level-2 heading ends the case list
        elif sections:
            sections[-1][2].append(line)
    for key, title, body in sections:
        fields = _fields(body)
        if key and "Prompt" in fields:
            cases.append(
                Case(
                    key=key,
                    title=title,
                    prompt=fields["Prompt"],
                    follow_up=fields.get("Follow-up"),
                    criteria="\n".join(body).strip(),
                )
            )
    return cases


def _fields(body: list[str]) -> dict[str, str]:
    """Collect ``**Prompt:**`` / ``**Follow-up:**`` paragraphs (wrapped lines joined)."""
    fields: dict[str, str] = {}
    name: str | None = None
    for line in body:
        match = _FIELD.match(line)
        if match:
            name = match.group(1)
            fields[name] = match.group(2).strip()
        elif name is not None and line.strip():
            fields[name] = f"{fields[name]} {line.strip()}".strip()
        else:
            name = None
    return {k: v for k, v in fields.items() if v}


def mcp_config(work_dir: Path) -> Path:
    """Write the MCP config attaching yente-mcp over stdio; return its path."""
    env = {"YENTE_MCP_TRANSPORT": "stdio"}
    for var in ("OPENSANCTIONS_API_KEY", "YENTE_BASE_URL"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    config = {
        "mcpServers": {
            "yente": {
                "command": str(YENTE_MCP),
                "args": ["--transport", "stdio"],
                "env": env,
            }
        }
    }
    path = work_dir / "mcp.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def run_turn(
    prompt: str, *, session: str, resume: bool, config: Path, args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Run one ``claude -p`` turn in an empty directory; return its stream-json events."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--system-prompt",
        APP_PROMPT,
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--allowedTools",
        "mcp__yente",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    cmd += ["--resume", session] if resume else ["--session-id", session]
    if args.model:
        cmd += ["--model", args.model]
    if args.bare:
        cmd.append("--bare")
    with tempfile.TemporaryDirectory(prefix="mcp-case-") as cwd:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=TURN_TIMEOUT_SECONDS
        )
    events = [
        json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")
    ]
    if proc.returncode != 0 and not events:
        events.append(
            {
                "type": "runner_error",
                "returncode": proc.returncode,
                "stderr": proc.stderr,
            }
        )
    return events


def run_case(
    case: Case, n: int, out_dir: Path, config: Path, args: argparse.Namespace
) -> Path:
    """Run a case (and its follow-up, in the same session); write the event stream."""
    session = str(uuid.uuid4())
    events = run_turn(
        case.prompt, session=session, resume=False, config=config, args=args
    )
    if case.follow_up:
        events += run_turn(
            case.follow_up, session=session, resume=True, config=config, args=args
        )
    path = out_dir / f"{case.key}-{n}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def summarize_run(events: list[dict[str, Any]]) -> str:
    """Render one run's tool calls, errors and answers as Markdown."""
    lines: list[str] = []
    for event in events:
        if event.get("type") == "runner_error":
            lines.append(
                f"**Runner error** (exit {event['returncode']}):\n\n```\n{event['stderr'].strip()}\n```"
            )
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    call = json.dumps(block.get("input", {}), ensure_ascii=False)
                    lines.append(
                        f"- `{block.get('name', '').removeprefix('mcp__yente__')}` `{call}`"
                    )
        elif event.get("type") == "user":
            for block in event.get("message", {}).get("content", []):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error")
                ):
                    lines.append(f"  - error: {_text(block.get('content'))[:300]}")
        elif event.get("type") == "result":
            lines.append(f"\n**Answer:**\n\n{event.get('result', '').strip()}\n")
    return "\n".join(lines)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "cases", nargs="*", help="Case keys to run (e.g. 2 3a); default: all."
    )
    parser.add_argument("-n", "--repeat", type=int, default=1, help="Runs per case.")
    parser.add_argument("--model", help="Model alias or ID passed to claude.")
    parser.add_argument(
        "--bare", action="store_true", help="Skip global CLAUDE.md and memory."
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("OPENSANCTIONS_API_KEY"):
        print(
            "warning: OPENSANCTIONS_API_KEY unset; tool calls will fail with 401",
            file=sys.stderr,
        )

    cases = parse_cases(CASES_PATH.read_text())
    if args.cases:
        known = {c.key for c in cases}
        unknown = [k for k in args.cases if k not in known]
        if unknown:
            parser.error(f"unknown case(s): {', '.join(unknown)}")
        cases = [c for c in cases if c.key in args.cases]

    out_dir = ROOT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    config = mcp_config(out_dir)
    summary = [f"# MCP case runs {out_dir.name}\n"]
    for case in cases:
        summary.append(f"## {case.key}. {case.title}\n")
        if _PLACEHOLDER.search(case.prompt):
            print(f"skip {case.key}: prompt has a placeholder", file=sys.stderr)
            summary.append("_Skipped: the prompt still has a placeholder._\n")
            continue
        summary.append(
            f"<details><summary>Case text</summary>\n\n{case.criteria}\n\n</details>\n"
        )
        for n in range(1, args.repeat + 1):
            print(f"run {case.key} #{n}", file=sys.stderr)
            path = run_case(case, n, out_dir, config, args)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            summary.append(f"### Run {n}\n\n{summarize_run(events)}\n")
    (out_dir / "summary.md").write_text("\n".join(summary))
    print(out_dir / "summary.md")


if __name__ == "__main__":
    main()
