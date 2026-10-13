#!/usr/bin/env python3
# tools/run_pair_compare.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Compare canonical citation pairs across completed run databases.

The comparison is intentionally offline and fail-closed.  Pair identity uses
the content-addressed V3 key, and a comparison is rejected when the two runs do
not expose the same pair universe.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.report import terminal_failure_dimension
from tools.stable_pair_keys import manuscript_sha, stable_pair_rows


PAIR_KEY_VERSION = 3
_OLD_SPAN_CAUSES = {"jury_exhausted", "source_span_invalid"}
_NEW_SPAN_CAUSES = {"source_span_invalid"}
_TERMINAL_DIMENSIONS = ("mechanical", "semantic", "infrastructure", "unclassified")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _guard_rejection_causes_by_pair(
    conn: sqlite3.Connection,
) -> dict[tuple[object, object], dict[str, int]]:
    required = {
        "verification_candidates": {"candidate_id", "claim_id", "ref_id"},
        "verification_candidate_events": {
            "event_id", "candidate_id", "event_type", "created_at",
        },
        "verification_candidate_guard_rejections": {"event_id", "cause"},
    }
    for table, columns in required.items():
        if not columns <= _columns(conn, table):
            raise ValueError(f"{table} lacks required typed verification columns")
    counts: dict[tuple[object, object], Counter[str]] = {}
    for claim_id, ref_id, cause in conn.execute(
        "SELECT candidate.claim_id, candidate.ref_id, rejection.cause "
        "FROM verification_candidate_guard_rejections AS rejection "
        "JOIN verification_candidate_events AS event "
        "  ON event.event_id = rejection.event_id "
        " AND event.event_type = 'guard_rejected' "
        "JOIN verification_candidates AS candidate "
        "  ON candidate.candidate_id = event.candidate_id "
        "ORDER BY event.created_at, event.event_id"
    ):
        counts.setdefault((claim_id, ref_id), Counter())[str(cause)] += 1
    return {
        pair: dict(sorted(pair_counts.items()))
        for pair, pair_counts in counts.items()
    }


def _terminal_causes_by_state(
    conn: sqlite3.Connection,
) -> dict[tuple[object, object, object], str | None]:
    columns = _columns(conn, "verification_pair_state")
    required = {
        "claim_id", "ref_id", "scope", "status", "terminal_outcome",
        "terminal_cause",
    }
    if not required <= columns:
        raise ValueError("verification_pair_state lacks required lifecycle columns")
    result = {}
    for claim_id, ref_id, scope, status, cause in conn.execute(
        "SELECT claim_id, ref_id, scope, status, terminal_cause "
        "FROM verification_pair_state"
    ):
        normalized = str(cause) if cause else None
        result[(claim_id, ref_id, scope)] = normalized
    return result


def _snapshot(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict], str, str | None]:
    rows, key_mode = stable_pair_rows(conn)
    causes = _terminal_causes_by_state(conn)
    rejection_causes = _guard_rejection_causes_by_pair(conn)
    snapshots = {}
    for row in rows:
        pair = (row.get("claim_id"), row.get("ref_id"))
        snapshots[row["key"]] = {
            "key": row["key"],
            "display_label": (
                f"claim {row['claim_order']} / reference {row['ref_number']}"
            ),
            "claim_id": row.get("claim_id"),
            "ref_id": row.get("ref_id"),
            "claim_order": row.get("claim_order"),
            "ref_number": row.get("ref_number"),
            "sentence": row.get("sentence"),
            "status": row.get("status"),
            "outcome": row.get("outcome"),
            "terminal_cause": causes.get((*pair, row.get("scope"))),
            "guard_rejection_causes": rejection_causes.get(pair, {}),
        }
    return snapshots, key_mode, manuscript_sha(conn)


def _cause_label(pair: dict) -> str:
    if pair["status"] == "accepted":
        return "accepted"
    return str(pair.get("terminal_cause") or f"{pair['status']}:unknown")


def _verdict_label(pair: dict) -> str:
    outcome = pair.get("outcome") or "none"
    return f"{pair['status']}:{outcome}"


def _rejection_causes(causes: dict[str, int]) -> list[str]:
    result: list[str] = []
    for cause, count in causes.items():
        result.extend([cause] * int(count))
    return result


def _terminal_dimension_summary(pair_rows: list[dict], side: str) -> dict[str, int | float]:
    counts: Counter[str] = Counter()
    eligible_pair_count = len(pair_rows)
    nonaccepted_pair_count = 0
    for row in pair_rows:
        pair = row[side]
        if pair["status"] == "accepted":
            continue
        nonaccepted_pair_count += 1
        dimension = terminal_failure_dimension(
            pair.get("terminal_cause"),
            _rejection_causes(pair.get("guard_rejection_causes", {})),
        )
        counts[dimension] += 1
    mechanical = counts["mechanical"]
    return {
        "eligible_pair_count": eligible_pair_count,
        "nonaccepted_pair_count": nonaccepted_pair_count,
        "accepted_pair_count": eligible_pair_count - nonaccepted_pair_count,
        "mechanical": mechanical,
        "semantic": counts["semantic"],
        "infrastructure": counts["infrastructure"],
        "unclassified": counts["unclassified"],
        "mechanical_rate": round(
            mechanical / eligible_pair_count, 3
        ) if eligible_pair_count else 0.0,
    }


def _span_transition(old: dict, new: dict) -> str | None:
    old_span = old.get("terminal_cause") in _OLD_SPAN_CAUSES
    new_span = new.get("terminal_cause") in _NEW_SPAN_CAUSES
    if old_span and new_span:
        if old.get("terminal_cause") != new.get("terminal_cause"):
            return "relabeled"
        return "retained"
    if not old_span and new_span:
        return "new"
    if old_span and not new_span:
        return "recovered" if new.get("status") == "accepted" else "flipped"
    return None


def compare_connections(
    old_conn: sqlite3.Connection,
    new_conn: sqlite3.Connection,
    paper: str,
    *,
    old_path: str = "<old>",
    new_path: str = "<new>",
) -> dict:
    old_pairs, old_mode, old_sha = _snapshot(old_conn)
    new_pairs, new_mode, new_sha = _snapshot(new_conn)
    if old_sha and new_sha and old_sha != new_sha:
        raise ValueError(f"{paper}: manuscript SHA mismatch")
    old_keys = set(old_pairs)
    new_keys = set(new_pairs)
    old_only = sorted(old_keys - new_keys)
    new_only = sorted(new_keys - old_keys)
    if old_only or new_only:
        raise ValueError(
            f"{paper}: pair-universe drift "
            f"(old_only={len(old_only)}, new_only={len(new_only)})"
        )
    cause_transitions: Counter[tuple[str, str]] = Counter()
    verdict_transitions: Counter[tuple[str, str]] = Counter()
    span_counts: Counter[str] = Counter()
    pair_rows = []
    for key in sorted(old_keys):
        old = old_pairs[key]
        new = new_pairs[key]
        old_cause = _cause_label(old)
        new_cause = _cause_label(new)
        old_verdict = _verdict_label(old)
        new_verdict = _verdict_label(new)
        cause_transitions[(old_cause, new_cause)] += 1
        verdict_transitions[(old_verdict, new_verdict)] += 1
        span_transition = _span_transition(old, new)
        if span_transition:
            span_counts[span_transition] += 1
        pair_rows.append({
            "paper": paper,
            "key": key,
            "display_label": old["display_label"],
            "claim_order": old["claim_order"],
            "ref_number": old["ref_number"],
            "sentence": old["sentence"],
            "old": {
                "status": old["status"],
                "outcome": old["outcome"],
                "terminal_cause": old["terminal_cause"],
                "guard_rejection_causes": old["guard_rejection_causes"],
            },
            "new": {
                "status": new["status"],
                "outcome": new["outcome"],
                "terminal_cause": new["terminal_cause"],
                "guard_rejection_causes": new["guard_rejection_causes"],
            },
            "cause_transition": f"{old_cause} -> {new_cause}",
            "verdict_transition": f"{old_verdict} -> {new_verdict}",
            "span_transition": span_transition,
        })

    old_dimensions = _terminal_dimension_summary(pair_rows, "old")
    new_dimensions = _terminal_dimension_summary(pair_rows, "new")
    return {
        "paper": paper,
        "old_run": old_path,
        "new_run": new_path,
        "pair_key_version": PAIR_KEY_VERSION,
        "old_key_mode": old_mode,
        "new_key_mode": new_mode,
        "manuscript_sha256": old_sha or new_sha,
        "pair_count": len(pair_rows),
        "old_accepted": sum(row["old"]["status"] == "accepted" for row in pair_rows),
        "new_accepted": sum(row["new"]["status"] == "accepted" for row in pair_rows),
        "old_terminal_dimensions": old_dimensions,
        "new_terminal_dimensions": new_dimensions,
        "cause_transitions": [
            {"old": old, "new": new, "count": count}
            for (old, new), count in sorted(cause_transitions.items())
        ],
        "verdict_transitions": [
            {"old": old, "new": new, "count": count}
            for (old, new), count in sorted(verdict_transitions.items())
        ],
        "span_transition_counts": dict(sorted(span_counts.items())),
        "pairs": pair_rows,
    }


def compare_paths(paper: str, old_path: Path, new_path: Path) -> dict:
    with sqlite3.connect(old_path) as old_conn, sqlite3.connect(new_path) as new_conn:
        return compare_connections(
            old_conn,
            new_conn,
            paper,
            old_path=str(old_path),
            new_path=str(new_path),
        )


def aggregate(comparisons: list[dict]) -> dict:
    cause_transitions: Counter[tuple[str, str]] = Counter()
    verdict_transitions: Counter[tuple[str, str]] = Counter()
    span_counts: Counter[str] = Counter()
    old_dimensions: Counter[str] = Counter()
    new_dimensions: Counter[str] = Counter()
    old_eligible_pair_count = 0
    new_eligible_pair_count = 0
    old_nonaccepted_pair_count = 0
    new_nonaccepted_pair_count = 0
    for comparison in comparisons:
        for row in comparison["cause_transitions"]:
            cause_transitions[(row["old"], row["new"])] += row["count"]
        for row in comparison["verdict_transitions"]:
            verdict_transitions[(row["old"], row["new"])] += row["count"]
        span_counts.update(comparison["span_transition_counts"])
        old_summary = comparison["old_terminal_dimensions"]
        new_summary = comparison["new_terminal_dimensions"]
        old_eligible_pair_count += int(old_summary["eligible_pair_count"])
        new_eligible_pair_count += int(new_summary["eligible_pair_count"])
        old_nonaccepted_pair_count += int(old_summary["nonaccepted_pair_count"])
        new_nonaccepted_pair_count += int(new_summary["nonaccepted_pair_count"])
        for dimension in _TERMINAL_DIMENSIONS:
            old_dimensions[dimension] += int(old_summary[dimension])
            new_dimensions[dimension] += int(new_summary[dimension])
    return {
        "pair_count": sum(item["pair_count"] for item in comparisons),
        "old_accepted": sum(item["old_accepted"] for item in comparisons),
        "new_accepted": sum(item["new_accepted"] for item in comparisons),
        "old_terminal_dimensions": {
            "eligible_pair_count": old_eligible_pair_count,
            "nonaccepted_pair_count": old_nonaccepted_pair_count,
            "accepted_pair_count": (
                old_eligible_pair_count - old_nonaccepted_pair_count
            ),
            "mechanical": old_dimensions["mechanical"],
            "semantic": old_dimensions["semantic"],
            "infrastructure": old_dimensions["infrastructure"],
            "unclassified": old_dimensions["unclassified"],
            "mechanical_rate": round(
                old_dimensions["mechanical"] / old_eligible_pair_count, 3
            ) if old_eligible_pair_count else 0.0,
        },
        "new_terminal_dimensions": {
            "eligible_pair_count": new_eligible_pair_count,
            "nonaccepted_pair_count": new_nonaccepted_pair_count,
            "accepted_pair_count": (
                new_eligible_pair_count - new_nonaccepted_pair_count
            ),
            "mechanical": new_dimensions["mechanical"],
            "semantic": new_dimensions["semantic"],
            "infrastructure": new_dimensions["infrastructure"],
            "unclassified": new_dimensions["unclassified"],
            "mechanical_rate": round(
                new_dimensions["mechanical"] / new_eligible_pair_count, 3
            ) if new_eligible_pair_count else 0.0,
        },
        "cause_transitions": [
            {"old": old, "new": new, "count": count}
            for (old, new), count in sorted(cause_transitions.items())
        ],
        "verdict_transitions": [
            {"old": old, "new": new, "count": count}
            for (old, new), count in sorted(verdict_transitions.items())
        ],
        "span_transition_counts": dict(sorted(span_counts.items())),
    }


def build_report(
    comparisons: list[dict],
    *,
    old_label: str = "old",
    new_label: str = "new",
) -> dict:
    aggregate_row = aggregate(comparisons)
    return {
        "schema_version": 2,
        "pair_key_version": PAIR_KEY_VERSION,
        "comparison": f"{old_label}-to-{new_label}",
        "old_label": old_label,
        "new_label": new_label,
        "aggregate": aggregate_row,
        "papers": comparisons,
        "comparison_note": (
            "This artifact records persisted pair membership, state transitions, "
            "and terminal failure dimensions. It does not infer causality."
        ),
    }


def render_markdown(report: dict) -> str:
    aggregate_row = report["aggregate"]
    old_label = report["old_label"]
    new_label = report["new_label"]
    lines = [
        f"# Pair transition: {old_label} → {new_label}",
        "",
        f"- Stable pair universe: **{aggregate_row['pair_count']}** "
        f"(key schema V{report['pair_key_version']})",
        f"- Accepted: **{old_label} {aggregate_row['old_accepted']} → "
        f"{new_label} {aggregate_row['new_accepted']}**",
        f"- Comparison labels: **{old_label} → {new_label}**",
        "",
        "## Cause transition matrix",
        "",
        "| Old cause/state | New cause/state | Count |",
        "|---|---|---:|",
    ]
    for row in aggregate_row["cause_transitions"]:
        lines.append(f"| `{row['old']}` | `{row['new']}` | {row['count']} |")

    lines.extend([
        "",
        "## Span transition matrix",
        "",
        "| Span transition | Count |",
        "|---|---:|",
    ])
    for category in ("relabeled", "retained", "new", "recovered", "flipped"):
        lines.append(
            f"| `{category}` | {aggregate_row['span_transition_counts'].get(category, 0)} |"
        )

    lines.extend([
        "",
        f"## Terminal failure dimensions ({old_label} → {new_label})",
        "",
        "| Dimension | Old | New |",
        "|---|---:|---:|",
    ])
    old_dimensions = aggregate_row["old_terminal_dimensions"]
    new_dimensions = aggregate_row["new_terminal_dimensions"]
    for dimension in _TERMINAL_DIMENSIONS:
        if dimension == "mechanical":
            lines.append(
                f"| `{dimension}` | "
                f"{old_dimensions[dimension]}/{old_dimensions['eligible_pair_count']} "
                f"({old_dimensions['mechanical_rate']:.1%}) | "
                f"{new_dimensions[dimension]}/{new_dimensions['eligible_pair_count']} "
                f"({new_dimensions['mechanical_rate']:.1%}) |"
            )
        else:
            lines.append(
                f"| `{dimension}` | {old_dimensions[dimension]} | "
                f"{new_dimensions[dimension]} |"
            )

    for comparison in report["papers"]:
        lines.extend([
            "",
            f"## {comparison['paper']}",
            "",
            f"- Pairs: {comparison['pair_count']}",
            f"- Accepted: {old_label} {comparison['old_accepted']} → "
            f"{new_label} {comparison['new_accepted']}",
            f"- Mechanical failures: "
            f"{comparison['old_terminal_dimensions']['mechanical']}/"
            f"{comparison['old_terminal_dimensions']['eligible_pair_count']} "
            f"({comparison['old_terminal_dimensions']['mechanical_rate']:.1%}) → "
            f"{comparison['new_terminal_dimensions']['mechanical']}/"
            f"{comparison['new_terminal_dimensions']['eligible_pair_count']} "
            f"({comparison['new_terminal_dimensions']['mechanical_rate']:.1%})",
            "",
            f"### Terminal failure dimensions ({old_label} → {new_label})",
            "",
        ])
        for dimension in _TERMINAL_DIMENSIONS:
            lines.append(
                f"- `{dimension}`: "
                f"{comparison['old_terminal_dimensions'][dimension]} → "
                f"{comparison['new_terminal_dimensions'][dimension]}"
            )

        lines.extend([
            "",
            "### Span transitions",
            "",
        ])
        found = False
        for pair in comparison["pairs"]:
            category = pair.get("span_transition")
            if not category:
                continue
            found = True
            lines.append(
                f"- `{category}` · `{pair['key']}` ({pair['display_label']}): "
                f"`{pair['cause_transition']}`"
            )
        if not found:
            lines.append("- None.")

    lines.extend([
        "",
        "## Comparison note",
        "",
        f"- {report['comparison_note']}",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair",
        nargs=3,
        action="append",
        metavar=("PAPER", "OLD_DB", "NEW_DB"),
        required=True,
        help="paper label followed by the old and new run.sqlite paths",
    )
    parser.add_argument("--old-label", default="old")
    parser.add_argument("--new-label", default="new")
    parser.add_argument("--markdown-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        comparisons = [
            compare_paths(paper, Path(old_db), Path(new_db))
            for paper, old_db, new_db in args.pair
        ]
        report = build_report(
            comparisons,
            old_label=args.old_label,
            new_label=args.new_label,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"run-pair-compare: {exc}", file=sys.stderr)
        return 1

    json_text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    markdown_text = render_markdown(report)
    if args.markdown_out:
        args.markdown_out.write_text(markdown_text, encoding="utf-8")
    if not args.markdown_out:
        print(json_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
