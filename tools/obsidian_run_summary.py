#!/usr/bin/env python3
# tools/obsidian_run_summary.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Create a deterministic, generated Obsidian summary for a current run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.infra.db import RunRepository


class RunSummaryError(ValueError):
    """Raised when a run cannot be summarized without guessing."""


def _safe_id(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("._-") or "run"


def _default_output(run_id: str) -> Path:
    return _ROOT / "knowledge" / "40 Runs" / f"Generated Run - {_safe_id(run_id)}.md"


def _file_state(path: Path) -> tuple[bytes, int]:
    try:
        return hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns
    except OSError as exc:
        raise RunSummaryError(f"cannot inspect run database: {path}") from exc


def _require_report(run_dir: Path) -> Path:
    report = run_dir / "report.md"
    if not report.is_file():
        raise RunSummaryError(f"required report.md missing: {report}")
    try:
        if not report.read_text(encoding="utf-8").strip():
            raise RunSummaryError(f"report.md empty: {report}")
    except UnicodeDecodeError as exc:
        raise RunSummaryError(f"report.md not valid UTF-8: {report}") from exc
    return report


def _artifact_link(note_path: Path, artifact: Path) -> str:
    return Path(os.path.relpath(artifact, note_path.parent)).as_posix()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _markdown(*, run: Any, snapshot: dict[str, Any], settings: dict[str, Any], events: list[Any], pairs: list[dict[str, Any]], note_path: Path, run_dir: Path) -> str:
    statuses = Counter(str(pair.get("status") or "missing") for pair in pairs)
    outcomes = Counter(str(pair.get("terminal_outcome") or "none") for pair in pairs)
    report = _artifact_link(note_path, run_dir / "report.md")
    database = _artifact_link(note_path, run_dir / "run.sqlite")
    lines = [
        "---",
        "type: run",
        "status: generated",
        "source: generated",
        "generated: true",
        f"run_id: {json.dumps(run.run_id)}",
        f"run_status: {json.dumps(run.status)}",
        f"run_phase: {json.dumps(run.phase)}",
        f"created: {json.dumps(run.created_at)}",
        f"updated: {json.dumps(run.updated_at)}",
        "---",
        "",
        "# Generated Run Summary",
        "",
        "> [!warning] Generated summary",
        "> This note is projected from immutable run artifacts; do not edit it as an audit record.",
        "",
        "## Run identity and state",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Status: `{run.status}`; phase: `{run.phase}`",
        f"- Status schema: `{snapshot['schema']}`",
        f"- Pending tasks: **{snapshot['pending_tasks_total']}**; answered awaiting resume: **{snapshot['answered_tasks_waiting_resume_total']}**",
        "",
        "## Settings and provenance",
        "",
    ]
    if settings:
        lines.extend(f"- `{key}`: `{_compact_json(settings[key])}`" for key in sorted(settings))
    else:
        lines.append("- No typed run settings recorded.")
    lines.extend(["", "## Phase timeline", ""])
    if events:
        lines.extend(
            f"- `{event.created_at}` · `{event.phase}` · `{event.event_type}`"
            + (f" · `{_compact_json(event.payload)}`" if event.payload is not None else "")
            for event in events
        )
    else:
        lines.append("- No phase events recorded.")
    lines.extend(["", "## Verify outcomes", "", f"- Verification pairs: **{len(pairs)}**"])
    lines.extend(f"- Pair status `{status}`: **{count}**" for status, count in sorted(statuses.items()))
    lines.extend(f"- Terminal outcome `{outcome}`: **{count}**" for outcome, count in sorted(outcomes.items()))
    lines.extend([
        "",
        "## Artifacts",
        "",
        f"- [report.md]({report})",
        f"- [run.sqlite]({database})",
        "",
    ])
    return "\n".join(lines)


def generate_summary(run_dir: str | Path, output: str | Path | None = None) -> Path:
    """Generate a summary from the current schema through public read APIs only."""
    run_dir = Path(run_dir).resolve()
    _require_report(run_dir)
    database = run_dir / "run.sqlite"
    if not database.is_file():
        raise RunSummaryError(f"required run.sqlite missing: {database}")
    before = _file_state(database)
    try:
        repository = RunRepository.open_readonly(str(run_dir))
    except Exception as exc:
        raise RunSummaryError(f"current run database required: {database}") from exc
    try:
        run = repository.get_run()
        snapshot = repository.status_snapshot()
        settings = repository.list_run_settings()
        events = repository.list_phase_events()
        pairs = repository.verification_pair_state_payloads()
        repository.verification_raw_payloads()
    except Exception as exc:
        raise RunSummaryError(f"malformed current run database: {database}") from exc
    finally:
        repository.close()
    if _file_state(database) != before:
        raise RunSummaryError(f"read-only summary changed run database: {database}")
    note_path = Path(output).resolve() if output is not None else _default_output(run.run_id)
    content = _markdown(
        run=run,
        snapshot=snapshot,
        settings=settings,
        events=events,
        pairs=pairs,
        note_path=note_path,
        run_dir=run_dir,
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")
    return note_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, metavar="RUN_DIR")
    parser.add_argument("--output", metavar="PATH")
    args = parser.parse_args(argv)
    try:
        output = generate_summary(args.run, args.output)
    except RunSummaryError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
