#!/usr/bin/env python3
# tools/run_provenance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Emit a deterministic, offline provenance record for a run directory.

This deliberately reads only local files.  It is useful when freezing a fetch
baseline before verification: the input PDF, SQLite database, code revision,
and persisted source manifest are recorded without modifying the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.infra.db.repository import RunRepository


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _source_manifest(repo: RunRepository) -> list[dict]:
    """Canonical, local-only source inventory that can be hashed for a freeze."""
    return [
        {
            "ref_id": row.ref_id, "tier": row.tier, "origin": row.origin,
            "stored_path": row.stored_path, "sha256": row.sha256,
            "char_count": row.char_count, "content_version": row.content_version,
            "provenance_relation": row.provenance_relation,
            "extraction_method": row.extraction_method,
            "extraction_flags": list(row.extraction_flags),
        }
        for row in sorted(repo.list_source_texts(), key=lambda row: (
            row.ref_id, row.tier, row.origin, row.stored_path))
    ]


def provenance(run: Path) -> dict:
    db = run / "run.sqlite"
    settings: dict = {}
    run_record: dict = {}
    manifest: list[dict] = []
    if db.is_file():
        repo = RunRepository.open_readonly(str(run))
        try:
            row = repo.get_run()
            run_record = {
                "input_path": row.input_path, "status": row.status,
                "phase": row.phase, "parent_run_id": row.parent_run_id,
                "run_origin": row.run_origin,
            }
            settings = repo.list_run_settings()
            manifest = _source_manifest(repo)
        finally:
            repo.close()
    input_raw = run_record.get("input_path") or settings.get("input")
    input_path = Path(input_raw) if input_raw else None
    runtime = settings.get("verify_runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    frozen = bool(run_record.get("parent_run_id")) or run_record.get("run_origin") == "frozen"
    return {
        "run": str(run),
        "run_revision": runtime.get("code_revision"),
        "run_code_dirty": runtime.get("code_dirty"),
        "run_code_diff_sha256": runtime.get("code_diff_sha256"),
        "run_code_snapshot_id": runtime.get("code_snapshot_id"),
        "run_code_snapshot_error": runtime.get("code_snapshot_error"),
        "workspace_revision": _workspace_revision(),
        "run_sqlite_sha256": _sha256(db),
        "input_path": str(input_path) if input_path is not None else None,
        "input_sha256": _sha256(input_path) if input_path is not None else None,
        "run_status": run_record.get("status"),
        "run_phase": run_record.get("phase"),
        "snapshot_kind": "frozen_fetch" if frozen else "live_run",
        "source_manifest_sha256": hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
        "source_manifest_entries": len(manifest),
        "debug_mode": bool(settings.get("debug_mode")),
        "debug_labels": settings.get("debug_labels") or [],
        "settings": {k: settings[k] for k in (
            "model", "backend", "reasoning", "isolated_level",
            "require_fulltext", "parse_coverage"
        ) if k in settings},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    payload = json.dumps(provenance(args.run), ensure_ascii=False, indent=2) + "\n"
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
