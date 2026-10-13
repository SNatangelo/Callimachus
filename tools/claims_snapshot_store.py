# tools/claims_snapshot_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-current relational store for corpus claim snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1
_TABLES = {
    "snapshot_meta", "snapshot_papers", "snapshot_references",
    "snapshot_claims", "snapshot_claim_markers", "snapshot_claim_resolutions",
}
_SCHEMA = """
CREATE TABLE snapshot_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 paper_count INTEGER NOT NULL CHECK(typeof(paper_count)='integer' AND paper_count>=0)
);
CREATE TABLE snapshot_papers (
 file_path TEXT PRIMARY KEY CHECK(length(trim(file_path))>0),
 error TEXT, source_format TEXT, citation_mode TEXT,
 claim_count INTEGER, reference_count INTEGER, orphan_count INTEGER,
 coverage_pct REAL,
 CHECK((error IS NOT NULL AND claim_count IS NULL AND reference_count IS NULL AND orphan_count IS NULL AND coverage_pct IS NULL)
    OR (error IS NULL AND claim_count>=0 AND reference_count>=0 AND orphan_count>=0 AND coverage_pct>=0 AND coverage_pct<=100))
);
CREATE TABLE snapshot_references (
 file_path TEXT NOT NULL REFERENCES snapshot_papers(file_path),
 reference_order INTEGER NOT NULL CHECK(reference_order>=0),
 ref_number INTEGER, raw_entry TEXT NOT NULL,
 PRIMARY KEY(file_path,reference_order)
);
CREATE TABLE snapshot_claims (
 file_path TEXT NOT NULL REFERENCES snapshot_papers(file_path),
 claim_order INTEGER NOT NULL CHECK(claim_order>=0),
 skeleton TEXT NOT NULL, sentence TEXT NOT NULL, marker_raw TEXT,
 orphaned INTEGER NOT NULL CHECK(orphaned>=0),
 PRIMARY KEY(file_path,claim_order)
);
CREATE TABLE snapshot_claim_markers (
 file_path TEXT NOT NULL, claim_order INTEGER NOT NULL, marker_order INTEGER NOT NULL CHECK(marker_order>=0),
 marker_number INTEGER NOT NULL,
 PRIMARY KEY(file_path,claim_order,marker_order),
 FOREIGN KEY(file_path,claim_order) REFERENCES snapshot_claims(file_path,claim_order)
);
CREATE TABLE snapshot_claim_resolutions (
 file_path TEXT NOT NULL, claim_order INTEGER NOT NULL, resolution_order INTEGER NOT NULL CHECK(resolution_order>=0),
 ref_number INTEGER NOT NULL,
 PRIMARY KEY(file_path,claim_order,resolution_order),
 FOREIGN KEY(file_path,claim_order) REFERENCES snapshot_claims(file_path,claim_order)
);
"""


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    exists = path.exists() and path.stat().st_size > 0
    if not exists and not create:
        raise RuntimeError(f"claims snapshot database does not exist: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not exists:
        conn.executescript(_SCHEMA)
        return conn
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    meta = conn.execute(
        "SELECT schema_version FROM snapshot_meta WHERE singleton=1"
    ).fetchone() if tables == _TABLES else None
    if tables != _TABLES or meta is None or meta[0] != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("claims snapshot database is not exact current schema")
    return conn


def _text(value, name, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or "\0" in value or (not optional and not value.strip()):
        raise ValueError(f"{name} has invalid text")
    return value


def write_snapshots(path: str | Path, snapshots: list[dict]) -> None:
    if type(snapshots) is not list:
        raise ValueError("snapshots must be a list")
    conn = _connect(Path(path), create=True)
    try:
        with conn:
            for table in (
                "snapshot_claim_markers", "snapshot_claim_resolutions", "snapshot_claims",
                "snapshot_references", "snapshot_papers", "snapshot_meta",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("INSERT INTO snapshot_meta VALUES(1,?,?)", (SCHEMA_VERSION, len(snapshots)))
            seen = set()
            for snapshot in snapshots:
                if type(snapshot) is not dict:
                    raise ValueError("snapshot must be an object")
                file_path = _text(snapshot.get("file"), "snapshot file")
                if file_path in seen:
                    raise ValueError("snapshot file is duplicated")
                seen.add(file_path)
                error = _text(snapshot.get("error"), "snapshot error", optional=True)
                if error is not None:
                    conn.execute(
                        "INSERT INTO snapshot_papers(file_path,error) VALUES(?,?)",
                        (file_path, error),
                    )
                    continue
                totals = snapshot.get("totals")
                references = snapshot.get("references")
                claims = snapshot.get("claims")
                if type(totals) is not dict or type(references) is not list or type(claims) is not list:
                    raise ValueError("snapshot typed sections are invalid")
                conn.execute(
                    "INSERT INTO snapshot_papers VALUES(?,?,?,?,?,?,?,?)",
                    (
                        file_path,
                        None,
                        _text(snapshot.get("format"), "format", optional=True),
                        _text(snapshot.get("citation_mode"), "citation_mode", optional=True),
                        totals.get("claims"), totals.get("references"), totals.get("orphans"),
                        totals.get("coverage_pct"),
                    ),
                )
                for order, reference in enumerate(references):
                    conn.execute(
                        "INSERT INTO snapshot_references VALUES(?,?,?,?)",
                        (file_path, order, reference.get("number"), str(reference.get("raw") or "")),
                    )
                for order, claim in enumerate(claims):
                    conn.execute(
                        "INSERT INTO snapshot_claims VALUES(?,?,?,?,?,?)",
                        (
                            file_path, order, str(claim.get("skeleton") or ""),
                            str(claim.get("sentence") or ""), claim.get("marker_raw"),
                            claim.get("orphaned"),
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO snapshot_claim_markers VALUES(?,?,?,?)",
                        (
                            (file_path, order, item_order, number)
                            for item_order, number in enumerate(claim.get("marker_numbers") or [])
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO snapshot_claim_resolutions VALUES(?,?,?,?)",
                        (
                            (file_path, order, item_order, number)
                            for item_order, number in enumerate(claim.get("resolved") or [])
                        ),
                    )
    finally:
        conn.close()


def read_snapshot(path: str | Path, file_path: str) -> dict | None:
    conn = _connect(Path(path), create=False)
    try:
        paper = conn.execute(
            "SELECT * FROM snapshot_papers WHERE file_path=?", (file_path,)
        ).fetchone()
        if paper is None:
            return None
        if paper["error"] is not None:
            return {"file": file_path, "error": paper["error"]}
        references = [
            {"number": row["ref_number"], "raw": row["raw_entry"]}
            for row in conn.execute(
                "SELECT * FROM snapshot_references WHERE file_path=? ORDER BY reference_order",
                (file_path,),
            )
        ]
        claims = []
        for row in conn.execute(
            "SELECT * FROM snapshot_claims WHERE file_path=? ORDER BY claim_order",
            (file_path,),
        ):
            markers = [item[0] for item in conn.execute(
                "SELECT marker_number FROM snapshot_claim_markers WHERE file_path=? AND claim_order=? ORDER BY marker_order",
                (file_path, row["claim_order"]),
            )]
            resolved = [item[0] for item in conn.execute(
                "SELECT ref_number FROM snapshot_claim_resolutions WHERE file_path=? AND claim_order=? ORDER BY resolution_order",
                (file_path, row["claim_order"]),
            )]
            claims.append({
                "skeleton": row["skeleton"], "sentence": row["sentence"],
                "marker_raw": row["marker_raw"], "marker_numbers": markers,
                "resolved": resolved, "orphaned": row["orphaned"],
            })
        if len(references) != paper["reference_count"] or len(claims) != paper["claim_count"]:
            raise RuntimeError("claims snapshot child count is inconsistent")
        return {
            "file": file_path,
            "format": paper["source_format"],
            "citation_mode": paper["citation_mode"],
            "totals": {
                "claims": paper["claim_count"], "references": paper["reference_count"],
                "orphans": paper["orphan_count"], "coverage_pct": paper["coverage_pct"],
            },
            "references": references,
            "claims": claims,
        }
    finally:
        conn.close()
