# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Export a verified human HTML report as a public redacted derivative."""

from __future__ import annotations

import json
import argparse
import sqlite3
from pathlib import Path

from core.infra.db.repository import RunRepository
from core.infra.integrity import signing
from core.invocation import run_command

from .redaction import redact_html
from .sealing import verify_html_report


def export_public_html(run_dir: str, output: str) -> str:
    """Write a redacted derivative of a complete Verify run's sealed report.

    Returns the output path. Both the HTML and its manifest are created
    exclusively; existing files are never overwritten.
    """
    repo = RunRepository.open_readonly(run_dir)
    try:
        run = repo.get_run()
        if run.status != "completed" or run.phase != "done":
            raise ValueError("public export requires a completed run at phase done")
        if repo.get_run_setting("references_only", None) is not False:
            raise ValueError("public export requires semantic Verify, not references-only mode")
    finally:
        repo.close()

    source = Path(run_dir) / "report.html"
    original = source.read_bytes()
    gate = verify_html_report(run_dir, require_signature=signing.key_present())
    if not gate["ok"]:
        raise ValueError("public export refused: " + "; ".join(gate["failures"]))
    if source.read_bytes() != original:
        raise ValueError("report.html changed while its seal was being verified")

    destination = Path(output)
    if destination.resolve() == source.resolve():
        raise ValueError("output must differ from the original report")
    if destination.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError("output must be outside the run directory")
    public, manifest = redact_html(original)
    sidecar = destination.with_suffix(destination.suffix + ".redaction.json")
    if destination.exists() or sidecar.exists():
        raise ValueError("output or manifest already exists")
    with destination.open("xb") as stream:
        stream.write(public)
    try:
        with sidecar.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except OSError:
        destination.unlink()
        raise
    return str(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=run_command("report-export"),
        description="Export a public redacted copy of a completed Verify report.",
    )
    parser.add_argument("--run", required=True, help="completed Verify run directory")
    parser.add_argument("--output", required=True, help="new public HTML path")
    args = parser.parse_args(argv)
    try:
        path = export_public_html(args.run, args.output)
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"report export refused: {exc}\n")
    print(f"Wrote {path} and {path}.redaction.json")
    return 0
