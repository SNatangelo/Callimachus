#!/usr/bin/env python3
# tools/fetch_fingerprint_diff.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Report and fail-close gate for two exact-current Fetch fingerprints."""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fetch_fingerprint_store import read_fingerprint

TIER_RANK = {"niente": 0, "abstract": 1, "fulltext": 2}


def _by_number(paper: dict) -> dict:
    return {(row.get("ref_number") if row.get("ref_number") is not None else rid): row
            for rid, row in paper.get("ref_fingerprint", {}).items()}


def _source_keys(row: dict) -> Counter[tuple]:
    return Counter((item.get("sha256"), item.get("char_count")) for item in row.get("sources", []))


def _errors(items: list) -> Counter[tuple]:
    errors: Counter[tuple] = Counter()
    benign_attempt_outcomes = {"stored", "landing_no_store", "abstract_fallback", "skipped"}
    for item in items:
        if item.get("kind") == "attempt":
            outcome = item.get("outcome")
            if outcome not in benign_attempt_outcomes:
                errors[(outcome, item.get("status"), item.get("reason"))] += 1
            continue
        if (
            item.get("error")
            or item.get("error_type")
            or item.get("error_stage")
            or item.get("reason_code")
        ):
            errors[(
                item.get("error_type") or item.get("event"),
                item.get("reason_code"),
                item.get("error"),
            )] += 1
    return errors


def _manual_task(items: list) -> bool:
    return any(item.get("status") == "pending" or item.get("producer_class") in {"operator", "agent"}
               or item.get("actor") in {"user", "agent"} for item in items)


def _identity_rank(status: object) -> str:
    value = str(status or "").strip().lower()
    if value in {"exact_identifier", "corroborated_bibliography", "externally_corroborated_text"} or value.endswith("_confirmed"):
        return "confirmed"
    if any(token in value for token in ("reject", "conflict", "mismatch", "fail")):
        return "rejected"
    return "uncertain"


def _has_readable_source(row: dict) -> bool:
    return bool(row.get("sources"))


def _classify(old: dict, new: dict, *, review_papers: set[str]) -> tuple[list[str], list[str]]:
    """Return hard failures and review-only facts; never silently suppress drift."""
    hard, review = [], []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is None or after is None:
            hard.append(f"{name}: paper {'added' if before is None else 'missing'}")
            continue
        if before["refs"] != after["refs"]:
            hard.append(f"{name}: reference count {before['refs']} -> {after['refs']}")
        if not before.get("snapshot_complete", False) or not after.get("snapshot_complete", False):
            hard.append(f"{name}: snapshot incomplete")
        if before["parse_fingerprint"] != after["parse_fingerprint"]:
            hard.append(f"{name}: Parse fingerprint drift")
        brefs, arefs = _by_number(before), _by_number(after)
        for key in sorted(set(brefs) | set(arefs), key=lambda value: (isinstance(value, str), value)):
            b, a = brefs.get(key), arefs.get(key)
            label = f"{name}[{key}]"
            if b is None or a is None:
                hard.append(f"{label}: reference {'added' if b is None else 'missing'}")
                continue
            if TIER_RANK.get(a["tier"], -1) < TIER_RANK.get(b["tier"], -1):
                (review if name in review_papers else hard).append(
                    f"{label}: tier downgrade {b['tier']} -> {a['tier']}")
            elif a["tier"] != b["tier"]:
                review.append(f"{label}: tier upgrade {b['tier']} -> {a['tier']}")
            if b.get("fulltext_exists") == "true" and a.get("fulltext_exists") in {"unknown", "false", "absent"}:
                target = hard if not _has_readable_source(a) else review
                target.append(f"{label}: confirmed fulltext availability regressed")
            if any(_identity_rank(item.get("identity_status")) == "confirmed" for item in b.get("sources", [])) and not any(
                _identity_rank(item.get("identity_status")) == "confirmed" for item in a.get("sources", [])
            ):
                hard.append(f"{label}: confirmed identity regressed")
            bkeys, akeys = _source_keys(b), _source_keys(a)
            if not bkeys <= akeys:
                (review if name in review_papers else hard).append(f"{label}: source hash/count lost")
            elif b.get("sources") != a.get("sources"):
                review.append(f"{label}: source facts changed")
            if not b.get("unreadable") and a.get("unreadable") and not _has_readable_source(a):
                hard.append(f"{label}: readable -> unreadable/OCR")
            elif b.get("unreadable") != a.get("unreadable"):
                review.append(f"{label}: OCR facts changed")
            if not _manual_task(b.get("tasks", [])) and _manual_task(a.get("tasks", [])):
                hard.append(f"{label}: automatic -> manual task")
            if not _errors(b.get("tasks", [])) >= _errors(a.get("tasks", [])):
                hard.append(f"{label}: new/worse task error")
            elif b.get("tasks") != a.get("tasks"):
                review.append(f"{label}: task facts changed")
            if not _errors(b.get("candidates", [])) >= _errors(a.get("candidates", [])):
                hard.append(f"{label}: new/worse candidate/attempt error")
            elif b.get("candidates") != a.get("candidates"):
                review.append(f"{label}: candidate lifecycle changed")
    return hard, review


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--gate", action="store_true", help="exit nonzero on Fetch regression")
    ap.add_argument("--review-paper", action="append", default=[], metavar="NAME",
                    help="report permitted source/tier loss for this paper, never structural drift")
    args = ap.parse_args()
    old, new = read_fingerprint(args.old), read_fingerprint(args.new)
    hard, review = _classify(old, new, review_papers=set(args.review_paper))
    print("HARD")
    for item in hard:
        print(f"  {item}")
    print("REVIEW")
    for item in review:
        print(f"  {item}")
    if not hard and not review:
        print("  no Fetch facts changed")
    return 1 if args.gate and hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
