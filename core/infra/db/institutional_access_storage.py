# core/infra/db/institutional_access_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed, credential-free state for bounded institutional Fetch attempts."""
from __future__ import annotations

import re


DDL = """
CREATE TABLE IF NOT EXISTS institutional_access_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  profile TEXT NOT NULL,
  token TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('checking','complete','unavailable')),
  checked_at TEXT NOT NULL,
  source_count INTEGER NOT NULL CHECK(source_count>=0)
);
CREATE TABLE IF NOT EXISTS institutional_access_source (
  source_order INTEGER PRIMARY KEY CHECK(source_order>=0),
  ref_id TEXT NOT NULL UNIQUE REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN (
    'login_required','challenge','access_unavailable','validated',
    'validation_required','unsupported'
  )),
  domain TEXT NOT NULL
);
"""
STATES = frozenset({
    "login_required", "challenge", "access_unavailable", "validated",
    "validation_required", "unsupported",
})


def normalize(value):
    if type(value) is not dict or set(value) != {"profile", "token", "status", "checked_at", "states"}:
        raise ValueError("institutional access state has invalid fields")
    if type(value["profile"]) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value["profile"]):
        raise ValueError("institutional access profile is invalid")
    if type(value["token"]) is not str or not re.fullmatch(r"initial|[a-f0-9]{32}", value["token"]):
        raise ValueError("institutional access attempt token is invalid")
    if value["status"] not in {"checking", "complete", "unavailable"}:
        raise ValueError("institutional access status is invalid")
    stamp = value["checked_at"]
    if type(stamp) is not str or not stamp or any(ord(char) < 32 for char in stamp):
        raise ValueError("institutional access timestamp is invalid")
    if type(value["states"]) is not list:
        raise ValueError("institutional source states must be a list")
    seen = set()
    for row in value["states"]:
        if type(row) is not dict or set(row) != {"ref_id", "state", "domain"}:
            raise ValueError("institutional source state has invalid fields")
        if type(row["ref_id"]) is not str or not row["ref_id"] or "\0" in row["ref_id"] or row["ref_id"] in seen:
            raise ValueError("institutional source reference is invalid or duplicated")
        if type(row["state"]) is not str or row["state"] not in STATES:
            raise ValueError("institutional source access state is invalid")
        if type(row["domain"]) is not str or not re.fullmatch(r"[A-Za-z0-9.-]{0,253}", row["domain"]):
            raise ValueError("institutional source domain is invalid")
        seen.add(row["ref_id"])
    return {**value, "states": [dict(row) for row in value["states"]]}


def replace(conn, value):
    value = normalize(value)
    conn.execute("DELETE FROM institutional_access_source")
    conn.execute("DELETE FROM institutional_access_setting")
    conn.execute(
        "INSERT INTO institutional_access_setting VALUES(1,?,?,?,?,?)",
        (value["profile"], value["token"], value["status"], value["checked_at"], len(value["states"])),
    )
    conn.executemany(
        "INSERT INTO institutional_access_source VALUES(?,?,?,?)",
        [(index, row["ref_id"], row["state"], row["domain"]) for index, row in enumerate(value["states"])],
    )


def read(conn):
    record = conn.execute("SELECT profile,token,status,checked_at,source_count FROM institutional_access_setting WHERE singleton=1").fetchone()
    rows = conn.execute("SELECT source_order,ref_id,state,domain FROM institutional_access_source ORDER BY source_order").fetchall()
    if record is None:
        if rows:
            raise RuntimeError("institutional access source state has no parent")
        return None
    if [row[0] for row in rows] != list(range(record[4])):
        raise RuntimeError("institutional access source count is inconsistent")
    return normalize({
        "profile": record[0], "token": record[1], "status": record[2], "checked_at": record[3],
        "states": [{"ref_id": row[1], "state": row[2], "domain": row[3]} for row in rows],
    })
