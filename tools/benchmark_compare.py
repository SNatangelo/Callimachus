#!/usr/bin/env python3
# tools/benchmark_compare.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Compare a completed current run with a certified V3 benchmark manifest."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.claim_keys import manifest_pair_universe
from tools.stable_pair_keys import stable_pair_rows


def _actual_pairs(conn: sqlite3.Connection) -> dict[str, str]:
    states = {}
    for row in stable_pair_rows(conn)[0]:
        status, outcome = row["status"], row["outcome"]
        if status == "accepted" and outcome in {"supports", "partial", "related", "contradicts", "off_topic"}:
            states[row["key"]] = outcome
        else:
            states[row["key"]] = "exhausted"
    return states


def _validate_manifest(benchmark: dict) -> None:
    if benchmark.get("pair_key_version") != 3:
        raise ValueError("benchmark must declare pair_key_version 3")
    forbidden_sections = {"expected_improvement", "adjudication"} & set(benchmark)
    if forbidden_sections:
        raise ValueError("benchmark contains removed top-level sections: " + ", ".join(sorted(forbidden_sections)))


def compare_connection(conn: sqlite3.Connection, benchmark: dict):
    _validate_manifest(benchmark)
    expected = benchmark.get("expected") or {}
    placements: dict[str, set[str]] = {}
    for category, pairs in expected.items():
        for pair in pairs or []:
            placements.setdefault(pair, set()).add(f"expected.{category}")
    for section in ("either",):
        for pair in benchmark.get(section) or {}:
            placements.setdefault(pair, set()).add(section)
    conflicts = {pair: sorted(locations) for pair, locations in placements.items() if len(locations) > 1}
    if conflicts:
        rendered = "; ".join(f"{pair}: {', '.join(locations)}" for pair, locations in sorted(conflicts.items()))
        raise ValueError("benchmark pair appears in multiple verdict sections: " + rendered)
    expected_by_pair = {pair: category for category in (
        "supports", "partial", "related", "contradicts", "off_topic", "adjudication"
    ) for pair in expected.get(category, [])}
    either = benchmark.get("either") or {}
    forbidden = benchmark.get("forbidden") or {}
    actual = _actual_pairs(conn)
    expected_universe = manifest_pair_universe(benchmark)
    differences, warnings = [], []
    if set(actual) != expected_universe:
        differences.append(("PAIR-UNIVERSE-DRIFT", "__pair_universe__", f"actual={len(actual)}", f"expected={len(expected_universe)}"))
    for pair in sorted(expected_universe | set(actual)):
        found = actual.get(pair, "missing")
        if found in set(forbidden.get(pair, ())):
            differences.append(("FAIL", pair, found, "forbidden: " + ", ".join(sorted(forbidden[pair]))))
        elif pair in either:
            allowed = set(either[pair])
            (warnings if found in allowed else differences).append(
                (("EITHER-OK" if found in allowed else "REGRESSION"), pair, found, ", ".join(sorted(allowed)))
            )
        elif expected_by_pair.get(pair) == "adjudication":
            if found == "partial":
                differences.append(("UPGRADE (verify by hand)", pair, found, "exhausted"))
            elif found == "supports":
                differences.append(("FAIL", pair, found, "exhausted"))
            elif found != "exhausted":
                differences.append(("REGRESSION", pair, found, "exhausted/partial"))
        elif (expected_class := expected_by_pair.get(pair)) is None:
            differences.append(("REGRESSION", pair, found, "not in benchmark"))
        elif found != expected_class:
            differences.append(("REGRESSION", pair, found, expected_class))
    return differences, warnings, actual


def compare(run_db, benchmark_path):
    with sqlite3.connect(run_db) as conn:
        benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
        differences, warnings, actual = compare_connection(conn, benchmark)
    for kind, pair, found, expected in differences + warnings:
        print(f"{kind}: {pair}: found={found}, expected={expected}")
    print(f"summary: {len(actual)} pairs, {len(differences)} blocking differences, {len(warnings)} warnings")
    return int(any(kind in {"REGRESSION", "FAIL", "PAIR-UNIVERSE-DRIFT"} for kind, *_ in differences))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python tools/benchmark_compare.py <run.sqlite> <benchmark.json>", file=sys.stderr)
        return 2
    return compare(Path(argv[0]), Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
