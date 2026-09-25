#!/usr/bin/env python3
# core/fetch/diagnostics/fetch_audit.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Human-readable and JSON summaries of persisted fetch attempts."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter

try:
    from core.infra.db import RunRepository
except ImportError:  # direct execution
    from db import RunRepository


def build_summary(run_dir: str) -> dict:
    repo = RunRepository.open_readonly(run_dir)
    try:
        refs = repo.effective_parse_payload().get("references", [])
        attempts_by_ref = {}
        outcome_counts = Counter()
        method_counts = Counter()
        kind_counts = Counter()
        challenge_refs = 0
        paywalled_refs = 0
        latest_outcomes = Counter()
        for ref in refs:
            ref_id = ref["id"]
            rows = repo.list_fetch_attempts(ref_id)
            if not rows:
                continue
            attempts_by_ref[ref_id] = rows
            challenge = any(row.get("challenge_blocked") for row in rows)
            paywalled = any(row.get("paywalled") for row in rows)
            if challenge:
                challenge_refs += 1
            if paywalled:
                paywalled_refs += 1
            latest = rows[-1]
            latest_outcomes[latest.get("outcome") or "unknown"] += 1
            for row in rows:
                outcome_counts[row.get("outcome") or "unknown"] += 1
                if row.get("method"):
                    method_counts[row["method"]] += 1
                if row.get("kind"):
                    kind_counts[row["kind"]] += 1
        ref_summaries = []
        for ref in refs:
            rows = attempts_by_ref.get(ref["id"]) or []
            if not rows:
                continue
            latest = rows[-1]
            ref_summaries.append(
                {
                    "ref_id": ref["id"],
                    "ref_number": ref["ref_number"],
                    "title": ref.get("title"),
                    "raw_entry": ref.get("raw_entry"),
                    "attempt_count": len(rows),
                    "challenge_blocked": any(row.get("challenge_blocked") for row in rows),
                    "paywalled": any(row.get("paywalled") for row in rows),
                    "latest_outcome": latest.get("outcome"),
                    "latest_reason": latest.get("reason"),
                    "latest_method": latest.get("method"),
                    "latest_kind": latest.get("kind"),
                    "latest_url": latest.get("final_url") or latest.get("url"),
                    "outcomes": dict(Counter(row.get("outcome") or "unknown" for row in rows)),
                }
            )
    finally:
        repo.close()

    return {
        "run_dir": os.path.abspath(run_dir),
        "references_total": len(refs),
        "references_with_attempts": len(attempts_by_ref),
        "attempt_rows_total": sum(len(rows) for rows in attempts_by_ref.values()),
        "challenge_blocked_references": challenge_refs,
        "paywalled_references": paywalled_refs,
        "outcomes": dict(outcome_counts),
        "latest_outcomes": dict(latest_outcomes),
        "methods": dict(method_counts),
        "kinds": dict(kind_counts),
        "references": ref_summaries,
    }


def _titleish(ref: dict) -> str:
    title = (ref.get("title") or "").strip()
    if title:
        return title
    raw = " ".join(str(ref.get("raw_entry") or "").split())
    return raw[:88] + ("..." if len(raw) > 88 else "")


def print_summary(summary: dict) -> None:
    print("FETCH ATTEMPT SUMMARY")
    print(f"run: {summary['run_dir']}")
    print(
        "references with attempts: "
        f"{summary['references_with_attempts']} / {summary['references_total']}"
    )
    print(f"attempt rows: {summary['attempt_rows_total']}")
    print(f"challenge-blocked references: {summary['challenge_blocked_references']}")
    print(f"paywalled references: {summary['paywalled_references']}")
    if summary.get("outcomes"):
        line = ", ".join(
            f"{name}={count}" for name, count in sorted(summary["outcomes"].items())
        )
        print(f"outcomes: {line}")
    if summary.get("latest_outcomes"):
        line = ", ".join(
            f"{name}={count}" for name, count in sorted(summary["latest_outcomes"].items())
        )
        print(f"latest outcomes: {line}")
    if summary.get("methods"):
        line = ", ".join(
            f"{name}={count}" for name, count in sorted(summary["methods"].items())
        )
        print(f"methods: {line}")
    refs = summary.get("references") or []
    if not refs:
        print("\nno persisted fetch attempts")
        return
    print("\nper reference:")
    for ref in refs:
        flags = []
        if ref.get("challenge_blocked"):
            flags.append("challenge")
        if ref.get("paywalled"):
            flags.append("paywall")
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        print(
            f"- [{ref['ref_number']}] {ref['attempt_count']} attempt(s)"
            f" latest={ref.get('latest_outcome')}"
            f" via {ref.get('latest_method') or 'n/a'}{flag_text}"
        )
        print(f"  {_titleish(ref)}")
        if ref.get("latest_reason"):
            print(f"  reason: {ref['latest_reason']}")
        if ref.get("latest_url"):
            print(f"  url: {ref['latest_url']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    summary = build_summary(args.run)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
