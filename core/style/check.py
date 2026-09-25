#!/usr/bin/env python3
# core/style/check.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
check.py — Axis 2, style dispatcher.

The orchestrator detects the source_type of the reference and calls the chosen style
module (one per style, NOT a universal validator). Only routing lives here; rules
live in the vancouver/apa7/chicago/mla9 modules.

Usage (single entry):
  python run.py style-check --style vancouver --type article --entry "Smith AB. ..."
Usage (batch, from a DB-backed run):
  python run.py style-check --style vancouver --run runs/<ts>
"""
import argparse
import importlib
import json

try:
    from core.infra.db import RunRepository
except ImportError:  # direct execution
    from db import RunRepository

STYLES = {"vancouver", "apa7", "chicago", "chicago_nb", "mla9", "ama", "ieee"}


def run_one(style: str, source_type: str, entry: str) -> dict:
    if style not in STYLES:
        raise SystemExit(f"unknown style: {style} (valid: {sorted(STYLES)})")
    mod = importlib.import_module(f"core.style.{style}")
    res = mod.check(entry, source_type or "unknown")
    res["style"] = style
    res["source_type"] = source_type
    return res


def _load_refs(run: str) -> list[dict]:
    repo = RunRepository.open_readonly(run)
    try:
        return repo.effective_parse_payload().get("references", [])
    finally:
        repo.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", required=True)
    ap.add_argument("--type", default="unknown", help="source_type (single entry)")
    ap.add_argument("--entry", help="raw_entry (single entry)")
    ap.add_argument("--run", help="run directory (DB-backed batch input)")
    args = ap.parse_args()

    if args.run:
        refs = _load_refs(args.run)
        results = []
        for r in refs:
            res = run_one(args.style, r.get("source_type", "unknown"), r["raw_entry"])
            res["ref_id"] = r["id"]
            res["ref_number"] = r.get("ref_number")
            results.append(res)
        payload = {"style": args.style, "checks": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not args.entry:
        raise SystemExit("provide --entry (single) or --run (batch)")
    print(json.dumps(run_one(args.style, args.type, args.entry),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
