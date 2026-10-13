#!/usr/bin/env python3
# tools/stable_pair_keys.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build V3 content-addressed verification-pair keys from a current run DB."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verify.pair_keys import assign_pair_keys, manuscript_identity


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    missing = required - _columns(conn, table)
    if missing:
        raise ValueError(f"{table} lacks required current columns: {', '.join(sorted(missing))}")


def manuscript_sha(conn: sqlite3.Connection) -> str:
    _require_columns(conn, "run", {"input_sha256"})
    row = conn.execute("SELECT input_sha256 FROM run LIMIT 1").fetchone()
    try:
        return manuscript_identity(row[0] if row else "")
    except ValueError as exc:
        raise ValueError(f"run has invalid manuscript SHA: {exc}") from exc


_SCOPE_RANK = {"fulltext_complete": 4, "fulltext_chunked_complete": 4,
               "fulltext_partial": 3, "rag": 3, "abstract_only": 2,
               "abstract_fallback": 2, "web_secondhand": 1, "preview_snippet": 0}
_STATUS_RANK = {"accepted": 6, "uncertain": 5, "exhausted": 4,
                "deadline_exceeded": 3, "infrastructure_error": 2,
                "cancelled": 1, "open": 0}


def canonical_pair_rows(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[object, object], list[dict]] = {}
    for position, raw in enumerate(rows):
        row = dict(raw, _canonical_position=position)
        groups.setdefault((row["claim_id"], row["ref_id"]), []).append(row)
    canonical = []
    for group in groups.values():
        chosen = max(group, key=lambda row: (
            _STATUS_RANK.get(str(row.get("status") or ""), -1),
            _SCOPE_RANK.get(str(row.get("scope") or ""), -1),
            str(row.get("scope") or ""), str(row.get("outcome") or ""),
            -int(row["_canonical_position"]),
        ))
        outcomes = {str(row.get("outcome") or "") for row in group
                    if row.get("status") == "accepted" and row.get("outcome")}
        chosen = dict(chosen)
        if len(outcomes) > 1:
            chosen.update(status="uncertain", outcome="scope_conflict")
        chosen.pop("_canonical_position", None)
        canonical.append(chosen)
    return sorted(canonical, key=lambda row: (row["claim_order"], row["claim_id"], row["ref_number"], row["ref_id"]))


def stable_pair_rows(conn: sqlite3.Connection) -> tuple[list[dict], str]:
    _require_columns(conn, "claims", {"claim_id", "claim_order", "sentence", "context_window", "marker_raw"})
    _require_columns(conn, "reference_entries", {"ref_id", "ref_number", "raw_entry", "title", "doi", "pmid", "isbn", "year", "ay_surname", "ay_year", "ay_suffix"})
    _require_columns(conn, "verification_pair_state", {"claim_id", "ref_id", "scope", "status", "terminal_outcome"})
    cursor = conn.execute(
        "SELECT c.claim_id, c.claim_order, c.sentence, c.context_window, c.marker_raw, "
        "r.ref_id, r.ref_number, r.raw_entry, r.title, r.doi, r.pmid, r.isbn, r.year, "
        "r.ay_surname, r.ay_year, r.ay_suffix, v.scope, v.status, "
        "v.terminal_outcome AS outcome FROM verification_pair_state v "
        "JOIN claims c ON c.claim_id = v.claim_id "
        "JOIN reference_entries r ON r.ref_id = v.ref_id "
        "ORDER BY c.claim_order, c.claim_id, r.ref_number, r.ref_id"
    )
    names = [str(column[0]) for column in cursor.description]
    rows = [dict(zip(names, values)) for values in cursor]
    return assign_pair_keys(canonical_pair_rows(rows), manuscript_sha(conn)), "content-addressed V3"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_db")
    args = parser.parse_args()
    with sqlite3.connect(args.run_db) as connection:
        rows, mode = stable_pair_rows(connection)
    print(f"key mode: {mode}")
    for row in rows:
        print(f"{row['key']} [{row['key_reason']}]")
