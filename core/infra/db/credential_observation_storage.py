# core/infra/db/credential_observation_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only, secret-free credential inventory and HTTP observations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CREDENTIAL_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS credential_inventory_snapshots (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  recorded_at TEXT NOT NULL CHECK(length(recorded_at)>0 AND instr(recorded_at,char(0))=0),
  entry_count INTEGER NOT NULL CHECK(entry_count>=0),
  entries_sha256 TEXT NOT NULL CHECK(
    length(entries_sha256)=64 AND entries_sha256 NOT GLOB '*[^0-9a-f]*'
  )
);
CREATE TABLE IF NOT EXISTS credential_inventory_entries (
  env_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  present_at_start INTEGER NOT NULL CHECK(present_at_start IN(0,1)),
  snapshot_id INTEGER NOT NULL DEFAULT 1 REFERENCES credential_inventory_snapshots(singleton),
  CHECK(snapshot_id=1),
  PRIMARY KEY(provider,env_name)
);
CREATE TABLE IF NOT EXISTS credential_transport_observations (
  observation_id TEXT PRIMARY KEY CHECK(
    length(observation_id)>0 AND instr(observation_id,char(0))=0
  ),
  provider TEXT NOT NULL,
  env_name TEXT NOT NULL,
  channel TEXT NOT NULL CHECK(channel IN('resolve','fetch','search')),
  outcome TEXT NOT NULL CHECK(outcome IN('response','http_error','network_error')),
  http_status INTEGER,
  created_at TEXT NOT NULL CHECK(length(created_at)>0 AND instr(created_at,char(0))=0),
  FOREIGN KEY(provider,env_name) REFERENCES credential_inventory_entries(provider,env_name),
  CHECK(
    (outcome IN('response','http_error') AND http_status BETWEEN 100 AND 599)
    OR (outcome='network_error' AND http_status IS NULL)
  )
);
"""

CREDENTIAL_OBSERVATION_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS credential_inventory_snapshots_no_update
BEFORE UPDATE ON credential_inventory_snapshots
BEGIN SELECT RAISE(ABORT,'credential inventory is write-once'); END;
CREATE TRIGGER IF NOT EXISTS credential_inventory_snapshots_no_delete
BEFORE DELETE ON credential_inventory_snapshots
BEGIN SELECT RAISE(ABORT,'credential inventory is write-once'); END;
CREATE TRIGGER IF NOT EXISTS credential_inventory_entries_no_update
BEFORE UPDATE ON credential_inventory_entries
BEGIN SELECT RAISE(ABORT,'credential inventory is write-once'); END;
CREATE TRIGGER IF NOT EXISTS credential_inventory_entries_no_delete
BEFORE DELETE ON credential_inventory_entries
BEGIN SELECT RAISE(ABORT,'credential inventory is write-once'); END;
CREATE TRIGGER IF NOT EXISTS credential_inventory_entries_capacity
BEFORE INSERT ON credential_inventory_entries
WHEN (
  SELECT COUNT(*) FROM credential_inventory_entries
) >= COALESCE((
  SELECT entry_count FROM credential_inventory_snapshots
  WHERE singleton=NEW.snapshot_id
),0)
BEGIN SELECT RAISE(ABORT,'credential inventory is sealed'); END;
CREATE TRIGGER IF NOT EXISTS credential_transport_observations_no_update
BEFORE UPDATE ON credential_transport_observations
BEGIN SELECT RAISE(ABORT,'credential observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS credential_transport_observations_no_delete
BEFORE DELETE ON credential_transport_observations
BEGIN SELECT RAISE(ABORT,'credential observations are append-only'); END;
"""

_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _text(name: str, value: Any) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError(f"credential {name} must be nonempty text")
    return value


def _mapping(provider: Any, env_name: Any) -> tuple[str, str]:
    provider = _text("provider", provider)
    env_name = _text("environment name", env_name)
    if not _PROVIDER.fullmatch(provider) or not _ENV_NAME.fullmatch(env_name):
        raise ValueError("credential mapping is invalid")
    return provider, env_name


def _inventory_rows(inventory: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(inventory, (list, tuple)):
        raise ValueError("credential inventory must be a list")
    rows = tuple(inventory)
    if any(
        type(row) is not dict
        or set(row) != {"provider", "env_name", "present_at_start"}
        for row in rows
    ):
        raise ValueError("credential inventory entry is invalid")
    actual = {_mapping(row["provider"], row["env_name"]) for row in rows}
    if len(rows) != len(actual):
        raise ValueError("credential inventory has duplicate entries")
    if any(type(row["present_at_start"]) is not bool for row in rows):
        raise ValueError("credential inventory presence is invalid")
    return rows


def _inventory_fingerprint(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    pairs = sorted((row["provider"], row["env_name"]) for row in rows)
    payload = json.dumps(pairs, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_inventory(conn, inventory: Any, *, recorded_at: str) -> None:
    _text("inventory timestamp", recorded_at)
    if conn.execute(
        "SELECT 1 FROM credential_inventory_snapshots "
        "UNION ALL SELECT 1 FROM credential_inventory_entries LIMIT 1"
    ).fetchone():
        raise ValueError("credential inventory is already recorded")
    rows = _inventory_rows(inventory)
    # Discover the runtime catalogue lazily: schema construction remains free
    # of provider imports, while every sealed run still records a complete
    # snapshot of the provider set that actually produced it.
    from core.infra.credential_catalog import credential_catalog

    expected = {
        (item["provider"], item["env_name"])
        for item in credential_catalog()
    }
    actual = {(row["provider"], row["env_name"]) for row in rows}
    if actual != expected:
        raise ValueError("credential inventory is incomplete")
    conn.execute(
        "INSERT INTO credential_inventory_snapshots VALUES(1,?,?,?)",
        (recorded_at, len(rows), _inventory_fingerprint(rows)),
    )
    conn.executemany(
        "INSERT INTO credential_inventory_entries"
        "(env_name,provider,present_at_start,snapshot_id) VALUES(?,?,?,1)",
        (
            (row["env_name"], row["provider"], int(row["present_at_start"]))
            for row in rows
        ),
    )


def read_inventory(conn) -> dict[str, Any] | None:
    header = conn.execute(
        "SELECT recorded_at,entry_count,entries_sha256 "
        "FROM credential_inventory_snapshots WHERE singleton=1"
    ).fetchone()
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT provider,env_name,present_at_start "
            "FROM credential_inventory_entries ORDER BY provider,env_name"
        )
    ]
    if header is None:
        if rows:
            raise ValueError("credential inventory has entries without a snapshot")
        return None
    if len(rows) != header[1]:
        raise ValueError("credential inventory is incomplete")
    if _inventory_fingerprint(rows) != header[2]:
        raise ValueError("credential inventory is invalid")
    for row in rows:
        row["present_at_start"] = bool(row["present_at_start"])
    return {"recorded_at": header[0], "entries": rows}


def transport_observation_payload(
    credential: Any,
    *,
    observation_id: str,
    channel: str,
    outcome: str,
    http_status: int | None,
) -> dict[str, Any]:
    if type(credential) is not dict or set(credential) != {"provider", "env_name"}:
        raise ValueError("credential transport binding is invalid")
    provider, env_name = _mapping(
        credential["provider"], credential["env_name"]
    )
    return {
        "observation_id": observation_id,
        "provider": provider,
        "env_name": env_name,
        "channel": channel,
        "outcome": outcome,
        "http_status": http_status,
    }


def append_observation(conn, payload: Any, *, created_at: str) -> None:
    _text("observation timestamp", created_at)
    required = {
        "observation_id", "provider", "env_name", "channel", "outcome",
        "http_status",
    }
    if type(payload) is not dict or set(payload) != required:
        raise ValueError("credential observation is invalid")
    observation_id = _text("observation id", payload["observation_id"])
    provider, env_name = _mapping(payload["provider"], payload["env_name"])
    channel, outcome = payload["channel"], payload["outcome"]
    if channel not in {"resolve", "fetch", "search"} or outcome not in {
        "response", "http_error", "network_error",
    }:
        raise ValueError("credential observation is invalid")
    status = payload["http_status"]
    if outcome == "network_error":
        if status is not None:
            raise ValueError("credential network observation has status")
    elif type(status) is not int or not 100 <= status <= 599:
        raise ValueError("credential HTTP status is invalid")
    if conn.execute(
        "SELECT 1 FROM credential_inventory_entries "
        "WHERE provider=? AND env_name=?",
        (provider, env_name),
    ).fetchone() is None:
        raise ValueError("credential mapping is not in the run inventory")
    conn.execute(
        "INSERT INTO credential_transport_observations VALUES(?,?,?,?,?,?,?)",
        (observation_id, provider, env_name, channel, outcome, status, created_at),
    )


def read_observations(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM credential_transport_observations "
            "ORDER BY created_at,observation_id"
        )
    ]
