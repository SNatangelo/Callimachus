# tools/fetch_fingerprint_store.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-current SQLite store for deterministic corpus Fetch fingerprints."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_TABLES = {"sweep_meta", "sweep_papers", "sweep_references"}
_SCHEMA = """
CREATE TABLE sweep_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  schema_version INTEGER NOT NULL CHECK(schema_version=2), generated_on TEXT NOT NULL,
  accuracy TEXT NOT NULL CHECK(length(trim(accuracy)) > 0),
  paper_count INTEGER NOT NULL CHECK(typeof(paper_count)='integer' AND paper_count >= 0)
);
CREATE TABLE sweep_papers (
  paper_name TEXT PRIMARY KEY CHECK(length(trim(paper_name)) > 0),
  reference_count INTEGER NOT NULL CHECK(typeof(reference_count)='integer' AND reference_count >= 0),
  fulltext_count INTEGER NOT NULL CHECK(typeof(fulltext_count)='integer' AND fulltext_count >= 0),
  abstract_only_count INTEGER NOT NULL CHECK(typeof(abstract_only_count)='integer' AND abstract_only_count >= 0),
  neither_count INTEGER NOT NULL CHECK(typeof(neither_count)='integer' AND neither_count >= 0),
  parse_fingerprint TEXT NOT NULL CHECK(length(parse_fingerprint)=64),
  snapshot_complete INTEGER NOT NULL CHECK(snapshot_complete IN(0,1)),
  elapsed_seconds REAL CHECK(elapsed_seconds IS NULL OR (typeof(elapsed_seconds) IN ('integer','real') AND elapsed_seconds >= 0)),
  CHECK(reference_count=fulltext_count+abstract_only_count+neither_count)
);
CREATE TABLE sweep_references (
  paper_name TEXT NOT NULL REFERENCES sweep_papers(paper_name), ref_id TEXT NOT NULL CHECK(length(trim(ref_id)) > 0),
  ref_number INTEGER, tier TEXT NOT NULL CHECK(tier IN ('fulltext','abstract','niente')),
  resolve_status TEXT, reference_tag TEXT, resolve_via TEXT, resolution_basis TEXT,
  fabrication_risk TEXT, title_flag TEXT,
  fulltext_exists TEXT NOT NULL CHECK(fulltext_exists IN ('true','false','unknown','absent')),
  source_facts TEXT NOT NULL, unreadable_facts TEXT NOT NULL, task_facts TEXT NOT NULL,
  candidate_facts TEXT NOT NULL, PRIMARY KEY(paper_name, ref_id)
);
"""


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or (required and not value.strip()) or "\0" in value:
        raise ValueError(f"{name} must be {'nonempty ' if required else ''}text or null")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _json(value: Any, name: str) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be canonical JSON facts") from exc
    if json.loads(encoded) != value:
        raise ValueError(f"{name} must be canonical JSON facts")
    return encoded


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    exists = path.exists() and path.stat().st_size > 0
    if not exists and not create:
        raise RuntimeError(f"fetch fingerprint database does not exist: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not exists:
        conn.executescript(_SCHEMA)
        return conn
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != _TABLES:
        conn.close()
        raise RuntimeError("fetch fingerprint database is not exact current schema")
    row = conn.execute("SELECT schema_version FROM sweep_meta WHERE singleton=1").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("fetch fingerprint database version is unsupported")
    return conn


def write_fingerprint(path: str | Path, *, accuracy: str, papers: dict[str, dict]) -> None:
    path = Path(path)
    accuracy = _text(accuracy, "accuracy", required=True)
    if type(papers) is not dict:
        raise ValueError("papers must be a mapping")
    conn = _connect(path, create=True)
    try:
        with conn:
            conn.execute("DELETE FROM sweep_references")
            conn.execute("DELETE FROM sweep_papers")
            conn.execute("DELETE FROM sweep_meta")
            conn.execute("INSERT INTO sweep_meta VALUES(1,?,?,?,?)",
                         (SCHEMA_VERSION, date.today().isoformat(), accuracy, len(papers)))
            for paper_name, paper in sorted(papers.items()):
                paper_name = _text(paper_name, "paper_name", required=True)
                if type(paper) is not dict:
                    raise ValueError("paper fingerprint must be an object")
                refs = _count(paper.get("refs"), "refs")
                fulltext = _count(paper.get("fulltext"), "fulltext")
                abstract = _count(paper.get("abstract_only"), "abstract_only")
                neither = _count(paper.get("neither"), "neither")
                parse_fingerprint = _text(paper.get("parse_fingerprint"), "parse_fingerprint", required=True)
                if len(parse_fingerprint) != 64 or any(
                    char not in "0123456789abcdef" for char in parse_fingerprint
                ):
                    raise ValueError("parse_fingerprint must be a SHA-256 digest")
                snapshot_complete = paper.get("snapshot_complete", True)
                if type(snapshot_complete) is not bool:
                    raise ValueError("snapshot_complete must be boolean")
                seconds = paper.get("seconds")
                if seconds is not None and (type(seconds) not in (int, float)
                                            or not math.isfinite(seconds) or seconds < 0):
                    raise ValueError("seconds must be finite and nonnegative")
                per_ref = paper.get("ref_fingerprint")
                if type(per_ref) is not dict or len(per_ref) != refs:
                    raise ValueError("reference fingerprint count is inconsistent")
                conn.execute("INSERT INTO sweep_papers VALUES(?,?,?,?,?,?,?,?)",
                             (paper_name, refs, fulltext, abstract, neither, parse_fingerprint,
                              int(snapshot_complete), seconds))
                for ref_id, row in sorted(per_ref.items()):
                    ref_id = _text(ref_id, "ref_id", required=True)
                    if type(row) is not dict:
                        raise ValueError("reference fingerprint must be an object")
                    number = row.get("ref_number")
                    if number is not None and (type(number) is not int or isinstance(number, bool) or number < 1):
                        raise ValueError("ref_number must be a positive integer or null")
                    tier = row.get("tier")
                    if tier not in {"fulltext", "abstract", "niente"}:
                        raise ValueError("reference tier is invalid")
                    fulltext_exists = row.get("fulltext_exists")
                    if fulltext_exists not in {"true", "false", "unknown", "absent"}:
                        raise ValueError("fulltext_exists is invalid")
                    fact_values = [row.get(key, []) for key in
                                   ("sources", "unreadable", "tasks", "candidates")]
                    if any(type(value) is not list for value in fact_values):
                        raise ValueError("reference fact fields must be lists")
                    facts = [_json(value, key) for value, key in zip(
                        fact_values, ("sources", "unreadable", "tasks", "candidates"), strict=True)]
                    conn.execute("INSERT INTO sweep_references VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        paper_name, ref_id, number, tier, _text(row.get("status"), "status"),
                        _text(row.get("tag"), "tag"), _text(row.get("via"), "via"),
                        _text(row.get("basis"), "basis"), _text(row.get("risk"), "risk"),
                        _text(row.get("title_flag"), "title_flag"), fulltext_exists, *facts))
    finally:
        conn.close()


def read_fingerprint(path: str | Path) -> dict[str, dict]:
    conn = _connect(Path(path), create=False)
    try:
        papers: dict[str, dict] = {}
        for paper in conn.execute("SELECT * FROM sweep_papers ORDER BY paper_name"):
            refs = {}
            for row in conn.execute("SELECT * FROM sweep_references WHERE paper_name=? ORDER BY ref_number,ref_id", (paper["paper_name"],)):
                refs[row["ref_id"]] = {
                    "ref_number": row["ref_number"], "tier": row["tier"],
                    "status": row["resolve_status"], "tag": row["reference_tag"],
                    "via": row["resolve_via"], "basis": row["resolution_basis"],
                    "risk": row["fabrication_risk"], "title_flag": row["title_flag"],
                    "fulltext_exists": row["fulltext_exists"],
                    "sources": json.loads(row["source_facts"]),
                    "unreadable": json.loads(row["unreadable_facts"]),
                    "tasks": json.loads(row["task_facts"]),
                    "candidates": json.loads(row["candidate_facts"]),
                }
            if len(refs) != paper["reference_count"]:
                raise RuntimeError("fetch fingerprint reference count is inconsistent")
            papers[paper["paper_name"]] = {
                "refs": paper["reference_count"], "fulltext": paper["fulltext_count"],
                "abstract_only": paper["abstract_only_count"], "neither": paper["neither_count"],
                "parse_fingerprint": paper["parse_fingerprint"],
                "snapshot_complete": bool(paper["snapshot_complete"]),
                "seconds": paper["elapsed_seconds"],
                "ref_fingerprint": refs,
            }
        meta = conn.execute("SELECT paper_count FROM sweep_meta WHERE singleton=1").fetchone()
        if meta is None or meta[0] != len(papers):
            raise RuntimeError("fetch fingerprint paper count is inconsistent")
        return papers
    finally:
        conn.close()
