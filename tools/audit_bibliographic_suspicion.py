# tools/audit_bibliographic_suspicion.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only replay of suspicion decisions against archived resolver evidence.

Usage: python tools/audit_bibliographic_suspicion.py RUN.sqlite [RUN.sqlite ...]
The operator gold fixture is deliberately never passed to the decision function.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.resolve.resolver_coverage import (
    bibliographic_review_labels,
    bibliographic_suspicion,
)


def _nonexistent_signal_buckets(result: dict, negatives: set[int]) -> dict:
    refuted_or_elevated = set(result["refuted"]) | set(result["elevated"])
    review_labelled = {
        ref_number
        for ref_numbers in result["review_labels"].values()
        for ref_number in ref_numbers
    }
    return {
        "nonexistent_refuted_or_elevated": sorted(refuted_or_elevated & negatives),
        "nonexistent_review_label_only": sorted(
            (review_labelled - refuted_or_elevated) & negatives
        ),
        "nonexistent_without_refutation_elevated_or_review_label": sorted(
            negatives - refuted_or_elevated - review_labelled
        ),
    }


def replay(path: Path, gold: dict | None = None) -> dict:
    # URI mode=ro prevents accidental creation of a missing archive.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        gold_rows = {r["ref_number"]: r for r in gold["references"]} if gold else {}
        for stored in conn.execute("SELECT e.*, r.status AS resolve_status FROM reference_entries e JOIN resolve_results r USING(ref_id) ORDER BY ref_number"):
            ref = dict(stored)
            if gold is not None and (
                ref["ref_number"] not in gold_rows
                or gold_rows[ref["ref_number"]]["raw_entry"] != ref["raw_entry"]
            ):
                raise ValueError("Gold citation identity differs from the archived run")
            rid = ref["ref_id"]
            ref["cited_coordinates"] = [dict(r) for r in conn.execute(
                "SELECT coordinate_kind AS kind, normalized_value, raw_value FROM cited_bibliographic_coordinates WHERE ref_id=?", (rid,))]
            adjudication_row = conn.execute("SELECT * FROM resolve_evidence_bibliographic_adjudications WHERE ref_id=?", (rid,)).fetchone()
            if adjudication_row is None:
                continue
            adjudication = dict(adjudication_row)
            adjudication["refutations"] = [dict(r) for r in conn.execute("SELECT * FROM resolve_evidence_bibliographic_refutations WHERE ref_id=? ORDER BY refutation_order", (rid,))]
            alias_row = conn.execute("SELECT * FROM resolve_evidence_journal_alias_assessments WHERE ref_id=?", (rid,)).fetchone()
            attempts = []
            for attempt in conn.execute("SELECT * FROM resolve_attempts WHERE ref_id=? ORDER BY attempt_order", (rid,)):
                value = dict(attempt)
                search = conn.execute("SELECT resolver,query_contract,completion,outcome FROM resolve_attempt_identity_searches WHERE ref_id=? AND attempt_order=?", (rid, attempt["attempt_order"])).fetchone()
                if search is not None:
                    value["identity_search"] = dict(search)
                attempts.append(value)
            observations = [dict(r) for r in conn.execute("SELECT *, coverage_status AS status FROM resolve_evidence_resolver_coverage_observations WHERE ref_id=?", (rid,))]
            coverage_row = conn.execute(
                "SELECT * FROM resolve_evidence_resolver_coverages WHERE ref_id=?",
                (rid,),
            ).fetchone()
            coverage = {}
            if coverage_row is not None:
                coverage = {
                    "authority": {
                        "record_id": coverage_row["authority_record_id"],
                        "snapshot_sha256": coverage_row["authority_snapshot_sha256"],
                        "canonical_title": coverage_row["canonical_title"],
                    },
                    "article_lookups": [dict(r) for r in conn.execute(
                        "SELECT * FROM resolve_evidence_resolver_coverage_article_lookups "
                        "WHERE ref_id=? ORDER BY lookup_order",
                        (rid,),
                    )],
                }
            before = conn.execute("SELECT suspicion_level FROM resolve_evidence_bibliographic_suspicions WHERE ref_id=?", (rid,)).fetchone()
            decision = bibliographic_suspicion(
                ref, status=ref["resolve_status"], attempts=attempts,
                alias_assessment=dict(alias_row) if alias_row else None,
                observations=observations,
                compatible_identity=adjudication["identity_status"] in {"identified", "identified_with_errors"},
                adjudication=adjudication,
            )
            review_labels = bibliographic_review_labels(
                reference=ref,
                status=ref["resolve_status"],
                attempts=attempts,
                adjudication=adjudication,
                coverage=coverage,
            )
            rows.append({"ref_number": ref["ref_number"], "title": ref["title"],
                         "adjudication": adjudication["outcome"],
                         "before": before[0] if before else "none",
                         "review_labels": review_labels, **decision})
        label_codes = (
            "not_found_after_completed_searches",
            "no_compatible_article_at_cited_coordinates",
            "incomplete_bibliographic_source",
        )
        result = {"database": str(path), "adjudicated": len(rows),
                "refuted": [r["ref_number"] for r in rows if r["adjudication"] == "refuted"],
                "elevated": [r["ref_number"] for r in rows if r["suspicion_level"] == "elevated"],
                "review_labels": {
                    code: [r["ref_number"] for r in rows if any(
                        label.get("code") == code for label in r["review_labels"]
                    )]
                    for code in label_codes
                },
                "changes": [r for r in rows if r["before"] != r["suspicion_level"]]}
        if gold is not None:
            if {r["ref_number"] for r in rows} != set(gold_rows):
                raise ValueError("Gold replay requires every reference to have an adjudication")
            flagged = set(result["refuted"]) | set(result["elevated"])
            negatives = {n for n, r in gold_rows.items() if r["gold_label"] == "nonexistent"}
            result["gold_evaluation"] = {
                **_nonexistent_signal_buckets(result, negatives),
                "real_references_flagged": sorted(flagged - negatives),
                "real_identity_errors": [r["ref_number"] for r in rows
                    if gold_rows[r["ref_number"]]["gold_label"] != "nonexistent"
                    and r["adjudication"] != (
                        "identified" if gold_rows[r["ref_number"]]["gold_label"] == "correct"
                        else "identified_with_errors")],
                "review_labels": {
                    code: {
                        "nonexistent": sorted(
                            set(result["review_labels"][code]) & negatives
                        ),
                        "real": sorted(
                            set(result["review_labels"][code]) - negatives
                        ),
                    }
                    for code in label_codes
                },
            }
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+", type=Path)
    parser.add_argument("--gold", type=Path, help="Optional operator gold JSON; exact citation text must match")
    args = parser.parse_args()
    gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else None
    print(json.dumps([replay(path, gold) for path in args.databases], ensure_ascii=False, indent=2))
