# core/infra/db/integrity_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed append-only mirror for externally authorised artifact checkpoints."""

from __future__ import annotations

import re
import sqlite3
from typing import Any


INTEGRITY_DDL = r"""
CREATE TABLE IF NOT EXISTS artifact_checkpoints (
  checkpoint_id TEXT PRIMARY KEY CHECK(length(trim(checkpoint_id)) > 0 AND instr(checkpoint_id, char(0)) = 0),
  sequence INTEGER NOT NULL UNIQUE CHECK(typeof(sequence) = 'integer' AND sequence >= 0),
  phase TEXT NOT NULL CHECK(phase IN ('parse','resolve','resolve_repair','fetch','gaps','style','verify','web_research','report','done')),
  checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind IN ('enrolled','phase_boundary','pause','external_input','integrity_override','report','unit')),
  created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0 AND instr(created_at, char(0)) = 0),
  previous_checkpoint_id TEXT REFERENCES artifact_checkpoints(checkpoint_id),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  database_projection_sha256 TEXT NOT NULL CHECK(length(database_projection_sha256) = 64 AND database_projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  file_count INTEGER NOT NULL CHECK(typeof(file_count) = 'integer' AND file_count >= 0),
  integrity_state TEXT NOT NULL CHECK(integrity_state IN ('clean','debug_overridden')),
  authority_id TEXT NOT NULL CHECK(length(trim(authority_id)) > 0 AND instr(authority_id, char(0)) = 0),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm = 'hmac-sha256'),
  signature TEXT NOT NULL CHECK(length(signature) = 64 AND signature NOT GLOB '*[^0-9a-f]*'),
  CHECK((sequence = 0 AND previous_checkpoint_id IS NULL) OR (sequence > 0 AND previous_checkpoint_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS artifact_checkpoint_entries (
  checkpoint_id TEXT NOT NULL REFERENCES artifact_checkpoints(checkpoint_id),
  entry_order INTEGER NOT NULL CHECK(typeof(entry_order) = 'integer' AND entry_order >= 0),
  logical_path TEXT NOT NULL CHECK(length(trim(logical_path)) > 0 AND instr(logical_path, char(0)) = 0),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  byte_count INTEGER NOT NULL CHECK(typeof(byte_count) = 'integer' AND byte_count >= 0),
  PRIMARY KEY(checkpoint_id, entry_order),
  UNIQUE(checkpoint_id, logical_path)
);

CREATE TABLE IF NOT EXISTS artifact_integrity_violations (
  violation_id TEXT PRIMARY KEY CHECK(length(trim(violation_id)) > 0 AND instr(violation_id, char(0)) = 0),
  observed_at TEXT NOT NULL CHECK(length(trim(observed_at)) > 0 AND instr(observed_at, char(0)) = 0),
  phase TEXT NOT NULL CHECK(phase IN ('parse','resolve','resolve_repair','fetch','gaps','style','verify','web_research','report','done')),
  session_id TEXT CHECK(session_id IS NULL OR (length(trim(session_id)) > 0 AND instr(session_id, char(0)) = 0)),
  authenticated_caller TEXT NOT NULL CHECK(length(trim(authenticated_caller)) > 0 AND instr(authenticated_caller, char(0)) = 0),
  expected_checkpoint_id TEXT NOT NULL REFERENCES artifact_checkpoints(checkpoint_id),
  expected_manifest_sha256 TEXT NOT NULL CHECK(length(expected_manifest_sha256) = 64 AND expected_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  observed_manifest_sha256 TEXT NOT NULL CHECK(length(observed_manifest_sha256) = 64 AND observed_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  database_changed INTEGER NOT NULL CHECK(database_changed IN (0,1)),
  difference_count INTEGER NOT NULL CHECK(typeof(difference_count) = 'integer' AND difference_count >= 0),
  authority_id TEXT NOT NULL CHECK(length(trim(authority_id)) > 0 AND instr(authority_id, char(0)) = 0),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm = 'hmac-sha256'),
  signature TEXT NOT NULL CHECK(length(signature) = 64 AND signature NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE IF NOT EXISTS artifact_integrity_violation_entries (
  violation_id TEXT NOT NULL REFERENCES artifact_integrity_violations(violation_id),
  entry_order INTEGER NOT NULL CHECK(typeof(entry_order) = 'integer' AND entry_order >= 0),
  difference_kind TEXT NOT NULL CHECK(difference_kind IN ('changed','missing','unexpected')),
  logical_path TEXT NOT NULL CHECK(length(trim(logical_path)) > 0 AND instr(logical_path, char(0)) = 0),
  PRIMARY KEY(violation_id, entry_order),
  UNIQUE(violation_id, difference_kind, logical_path)
);

CREATE TABLE IF NOT EXISTS artifact_integrity_overrides (
  override_id TEXT PRIMARY KEY CHECK(length(trim(override_id)) > 0 AND instr(override_id, char(0)) = 0),
  violation_id TEXT NOT NULL UNIQUE REFERENCES artifact_integrity_violations(violation_id),
  resulting_checkpoint_id TEXT NOT NULL UNIQUE REFERENCES artifact_checkpoints(checkpoint_id),
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0 AND instr(reason, char(0)) = 0),
  authenticated_caller TEXT NOT NULL CHECK(length(trim(authenticated_caller)) > 0 AND instr(authenticated_caller, char(0)) = 0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0 AND instr(created_at, char(0)) = 0),
  authority_id TEXT NOT NULL CHECK(length(trim(authority_id)) > 0 AND instr(authority_id, char(0)) = 0),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm = 'hmac-sha256'),
  signature TEXT NOT NULL CHECK(length(signature) = 64 AND signature NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE IF NOT EXISTS artifact_crash_recoveries (
  recovery_id TEXT PRIMARY KEY CHECK(length(trim(recovery_id)) > 0 AND instr(recovery_id, char(0)) = 0),
  subject_scope TEXT NOT NULL CHECK(subject_scope IN ('run','content_store')),
  lease_id TEXT NOT NULL UNIQUE CHECK(length(trim(lease_id)) > 0 AND instr(lease_id, char(0)) = 0),
  transition_id TEXT NOT NULL CHECK(length(trim(transition_id)) > 0 AND instr(transition_id, char(0)) = 0),
  expected_checkpoint_id TEXT NOT NULL CHECK(length(trim(expected_checkpoint_id)) > 0 AND instr(expected_checkpoint_id, char(0)) = 0),
  authenticated_caller TEXT NOT NULL CHECK(length(trim(authenticated_caller)) > 0 AND instr(authenticated_caller, char(0)) = 0),
  previous_owner_process TEXT NOT NULL CHECK(length(trim(previous_owner_process)) > 0 AND instr(previous_owner_process, char(0)) = 0),
  recovered_by_process TEXT NOT NULL CHECK(length(trim(recovered_by_process)) > 0 AND instr(recovered_by_process, char(0)) = 0),
  last_heartbeat_at TEXT NOT NULL CHECK(length(trim(last_heartbeat_at)) > 0 AND instr(last_heartbeat_at, char(0)) = 0),
  observed_manifest_sha256 TEXT NOT NULL CHECK(length(observed_manifest_sha256) = 64 AND observed_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  restored_manifest_sha256 TEXT NOT NULL CHECK(length(restored_manifest_sha256) = 64 AND restored_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  database_changed INTEGER NOT NULL CHECK(database_changed IN (0,1)),
  difference_count INTEGER NOT NULL CHECK(typeof(difference_count) = 'integer' AND difference_count >= 0),
  action_count INTEGER NOT NULL CHECK(typeof(action_count) = 'integer' AND action_count >= 0),
  observed_recovery_snapshot_sha256 TEXT NOT NULL CHECK(length(observed_recovery_snapshot_sha256) = 64 AND observed_recovery_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0 AND instr(created_at, char(0)) = 0),
  authority_id TEXT NOT NULL CHECK(length(trim(authority_id)) > 0 AND instr(authority_id, char(0)) = 0),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm = 'hmac-sha256'),
  signature TEXT NOT NULL CHECK(length(signature) = 64 AND signature NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE IF NOT EXISTS artifact_crash_recovery_entries (
  recovery_id TEXT NOT NULL REFERENCES artifact_crash_recoveries(recovery_id),
  entry_kind TEXT NOT NULL CHECK(entry_kind IN ('difference','action')),
  entry_order INTEGER NOT NULL CHECK(typeof(entry_order) = 'integer' AND entry_order >= 0),
  detail_kind TEXT NOT NULL CHECK(length(trim(detail_kind)) > 0 AND instr(detail_kind, char(0)) = 0),
  logical_path TEXT NOT NULL CHECK(length(trim(logical_path)) > 0 AND instr(logical_path, char(0)) = 0),
  PRIMARY KEY(recovery_id, entry_kind, entry_order),
  UNIQUE(recovery_id, entry_kind, detail_kind, logical_path)
);
"""


INTEGRITY_TRIGGERS_SQL = r"""
CREATE TRIGGER IF NOT EXISTS artifact_checkpoints_chain_guard
BEFORE INSERT ON artifact_checkpoints BEGIN
  SELECT CASE WHEN
    (NOT EXISTS(SELECT 1 FROM artifact_checkpoints) AND
      (NEW.sequence != 0 OR NEW.previous_checkpoint_id IS NOT NULL))
    OR
    (EXISTS(SELECT 1 FROM artifact_checkpoints) AND
      (NEW.sequence != (SELECT MAX(sequence) + 1 FROM artifact_checkpoints)
       OR NEW.previous_checkpoint_id != (
         SELECT checkpoint_id FROM artifact_checkpoints ORDER BY sequence DESC LIMIT 1
       )))
  THEN RAISE(ABORT, 'artifact checkpoint chain is not contiguous') END;
END;
CREATE TRIGGER IF NOT EXISTS artifact_checkpoints_debug_taint_guard
BEFORE INSERT ON artifact_checkpoints
WHEN NEW.integrity_state = 'clean' AND EXISTS(
  SELECT 1 FROM artifact_checkpoints WHERE integrity_state = 'debug_overridden'
) BEGIN
  SELECT RAISE(ABORT, 'artifact integrity debug override is irreversible');
END;
CREATE TRIGGER IF NOT EXISTS artifact_checkpoint_entries_count_guard
BEFORE INSERT ON artifact_checkpoint_entries
WHEN NEW.entry_order >= (
  SELECT file_count FROM artifact_checkpoints WHERE checkpoint_id = NEW.checkpoint_id
) BEGIN
  SELECT RAISE(ABORT, 'artifact checkpoint entry exceeds declared file count');
END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_violation_entries_count_guard
BEFORE INSERT ON artifact_integrity_violation_entries
WHEN NEW.entry_order >= (
  SELECT difference_count FROM artifact_integrity_violations WHERE violation_id = NEW.violation_id
) BEGIN
  SELECT RAISE(ABORT, 'artifact violation entry exceeds declared difference count');
END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_overrides_state_guard
BEFORE INSERT ON artifact_integrity_overrides
WHEN (SELECT integrity_state FROM artifact_checkpoints
      WHERE checkpoint_id = NEW.resulting_checkpoint_id) != 'debug_overridden' BEGIN
  SELECT RAISE(ABORT, 'artifact override requires a debug-overridden checkpoint');
END;
CREATE TRIGGER IF NOT EXISTS artifact_crash_recovery_entries_count_guard
BEFORE INSERT ON artifact_crash_recovery_entries
WHEN (NEW.entry_kind = 'difference' AND NEW.entry_order >= (
        SELECT difference_count FROM artifact_crash_recoveries
        WHERE recovery_id = NEW.recovery_id
      ))
  OR (NEW.entry_kind = 'action' AND NEW.entry_order >= (
        SELECT action_count FROM artifact_crash_recoveries
        WHERE recovery_id = NEW.recovery_id
      ))
BEGIN
  SELECT RAISE(ABORT, 'artifact crash recovery entry exceeds declared count');
END;

CREATE TRIGGER IF NOT EXISTS artifact_checkpoints_no_update BEFORE UPDATE ON artifact_checkpoints BEGIN SELECT RAISE(ABORT, 'artifact_checkpoints is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_checkpoints_no_delete BEFORE DELETE ON artifact_checkpoints BEGIN SELECT RAISE(ABORT, 'artifact_checkpoints is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_checkpoint_entries_no_update BEFORE UPDATE ON artifact_checkpoint_entries BEGIN SELECT RAISE(ABORT, 'artifact_checkpoint_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_checkpoint_entries_no_delete BEFORE DELETE ON artifact_checkpoint_entries BEGIN SELECT RAISE(ABORT, 'artifact_checkpoint_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_violations_no_update BEFORE UPDATE ON artifact_integrity_violations BEGIN SELECT RAISE(ABORT, 'artifact_integrity_violations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_violations_no_delete BEFORE DELETE ON artifact_integrity_violations BEGIN SELECT RAISE(ABORT, 'artifact_integrity_violations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_violation_entries_no_update BEFORE UPDATE ON artifact_integrity_violation_entries BEGIN SELECT RAISE(ABORT, 'artifact_integrity_violation_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_violation_entries_no_delete BEFORE DELETE ON artifact_integrity_violation_entries BEGIN SELECT RAISE(ABORT, 'artifact_integrity_violation_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_overrides_no_update BEFORE UPDATE ON artifact_integrity_overrides BEGIN SELECT RAISE(ABORT, 'artifact_integrity_overrides is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_integrity_overrides_no_delete BEFORE DELETE ON artifact_integrity_overrides BEGIN SELECT RAISE(ABORT, 'artifact_integrity_overrides is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_crash_recoveries_no_update BEFORE UPDATE ON artifact_crash_recoveries BEGIN SELECT RAISE(ABORT, 'artifact_crash_recoveries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_crash_recoveries_no_delete BEFORE DELETE ON artifact_crash_recoveries BEGIN SELECT RAISE(ABORT, 'artifact_crash_recoveries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_crash_recovery_entries_no_update BEFORE UPDATE ON artifact_crash_recovery_entries BEGIN SELECT RAISE(ABORT, 'artifact_crash_recovery_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_crash_recovery_entries_no_delete BEFORE DELETE ON artifact_crash_recovery_entries BEGIN SELECT RAISE(ABORT, 'artifact_crash_recovery_entries is append-only'); END;
"""


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset({
    "parse", "resolve", "resolve_repair", "fetch", "gaps", "style",
    "verify", "web_research", "report", "done",
})
_CHECKPOINT_KINDS = frozenset({
    "enrolled", "phase_boundary", "pause", "external_input",
    "integrity_override", "report", "unit",
})
_INTEGRITY_STATES = frozenset({"clean", "debug_overridden"})
_DIFFERENCE_KINDS = frozenset({"changed", "missing", "unexpected"})
_CHECKPOINT_FIELDS = (
    "checkpoint_id", "sequence", "phase", "checkpoint_kind", "created_at",
    "previous_checkpoint_id", "manifest_sha256", "database_projection_sha256",
    "file_count", "integrity_state", "authority_id", "signature_algorithm",
    "signature",
)
_VIOLATION_FIELDS = (
    "violation_id", "observed_at", "phase", "session_id",
    "authenticated_caller",
    "expected_checkpoint_id", "expected_manifest_sha256",
    "observed_manifest_sha256", "database_changed", "difference_count",
    "authority_id", "signature_algorithm", "signature",
)
_OVERRIDE_FIELDS = (
    "override_id", "violation_id", "resulting_checkpoint_id", "reason",
    "authenticated_caller", "created_at", "authority_id",
    "signature_algorithm", "signature",
)
_RECOVERY_FIELDS = (
    "recovery_id", "subject_scope", "lease_id", "transition_id",
    "expected_checkpoint_id", "authenticated_caller",
    "previous_owner_process", "recovered_by_process", "last_heartbeat_at",
    "observed_manifest_sha256", "restored_manifest_sha256",
    "database_changed", "difference_count", "action_count",
    "observed_recovery_snapshot_sha256", "created_at", "authority_id",
    "signature_algorithm", "signature",
)
_RECOVERY_ACTIONS = frozenset({
    "removed_unexpected", "restored_database", "restored_file",
})


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty NUL-free text")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _signature_fields(record: dict[str, Any]) -> None:
    _text(record.get("authority_id"), "authority_id")
    if record.get("signature_algorithm") != "hmac-sha256":
        raise ValueError("signature_algorithm must be hmac-sha256")
    _hash(record.get("signature"), "signature")


def _checkpoint_projection(record: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(record[name] for name in _CHECKPOINT_FIELDS)


def _validate_checkpoint(record: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    expected = set(_CHECKPOINT_FIELDS)
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("artifact checkpoint has unsupported fields")
    _text(record["checkpoint_id"], "checkpoint_id")
    sequence = _nonnegative_int(record["sequence"], "sequence")
    if record["phase"] not in _PHASES:
        raise ValueError("artifact checkpoint phase is invalid")
    if record["checkpoint_kind"] not in _CHECKPOINT_KINDS:
        raise ValueError("artifact checkpoint kind is invalid")
    _text(record["created_at"], "created_at")
    previous = record["previous_checkpoint_id"]
    if (sequence == 0) != (previous is None):
        raise ValueError("artifact checkpoint previous identity is invalid")
    if previous is not None:
        _text(previous, "previous_checkpoint_id")
    _hash(record["manifest_sha256"], "manifest_sha256")
    _hash(record["database_projection_sha256"], "database_projection_sha256")
    if record["integrity_state"] not in _INTEGRITY_STATES:
        raise ValueError("artifact checkpoint integrity state is invalid")
    _signature_fields(record)
    if _nonnegative_int(record["file_count"], "file_count") != len(entries):
        raise ValueError("artifact checkpoint file count is invalid")
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "logical_path", "sha256", "byte_count"
        }:
            raise ValueError("artifact checkpoint entry has unsupported fields")
        paths.append(_text(entry["logical_path"], "logical_path"))
        _hash(entry["sha256"], "artifact sha256")
        _nonnegative_int(entry["byte_count"], "artifact byte_count")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("artifact checkpoint entries must have unique sorted paths")


def _checkpoint_entries(conn: sqlite3.Connection, checkpoint_id: str) -> list[dict[str, Any]]:
    return [
        {
            "logical_path": row["logical_path"],
            "sha256": row["sha256"],
            "byte_count": row["byte_count"],
        }
        for row in conn.execute(
            "SELECT * FROM artifact_checkpoint_entries "
            "WHERE checkpoint_id=? ORDER BY entry_order",
            (checkpoint_id,),
        )
    ]


def append_checkpoint(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    """Append a checkpoint mirror, accepting only an exact replay."""
    _validate_checkpoint(record, entries)
    existing = conn.execute(
        "SELECT * FROM artifact_checkpoints WHERE checkpoint_id=?",
        (record["checkpoint_id"],),
    ).fetchone()
    if existing is not None:
        if tuple(existing[name] for name in _CHECKPOINT_FIELDS) != _checkpoint_projection(record):
            raise ValueError("artifact checkpoint replay differs from immutable record")
        if _checkpoint_entries(conn, record["checkpoint_id"]) != entries:
            raise ValueError("artifact checkpoint entry replay differs from immutable record")
        return
    latest = conn.execute(
        "SELECT checkpoint_id, sequence, integrity_state FROM artifact_checkpoints "
        "ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        if record["sequence"] != 0 or record["previous_checkpoint_id"] is not None:
            raise ValueError("first artifact checkpoint must start the chain")
    elif (
        record["sequence"] != latest["sequence"] + 1
        or record["previous_checkpoint_id"] != latest["checkpoint_id"]
    ):
        raise ValueError("artifact checkpoint does not extend the current chain")
    if latest is not None and latest["integrity_state"] == "debug_overridden":
        if record["integrity_state"] != "debug_overridden":
            raise ValueError("artifact integrity debug override is irreversible")
    conn.execute(
        """
        INSERT INTO artifact_checkpoints(
          checkpoint_id, sequence, phase, checkpoint_kind, created_at,
          previous_checkpoint_id, manifest_sha256, database_projection_sha256,
          file_count, integrity_state, authority_id, signature_algorithm, signature
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        _checkpoint_projection(record),
    )
    for order, entry in enumerate(entries):
        conn.execute(
            "INSERT INTO artifact_checkpoint_entries VALUES(?,?,?,?,?)",
            (
                record["checkpoint_id"], order, entry["logical_path"],
                entry["sha256"], entry["byte_count"],
            ),
        )


def list_checkpoints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    output = []
    for row in conn.execute("SELECT * FROM artifact_checkpoints ORDER BY sequence"):
        record = dict(row)
        record["files"] = _checkpoint_entries(conn, record["checkpoint_id"])
        if record["file_count"] != len(record["files"]):
            raise RuntimeError("artifact checkpoint mirror has an incomplete file inventory")
        output.append(record)
    return output


def _validate_violation(record: dict[str, Any], differences: list[dict[str, Any]]) -> None:
    expected = set(_VIOLATION_FIELDS)
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("artifact violation has unsupported fields")
    _text(record["violation_id"], "violation_id")
    _text(record["observed_at"], "observed_at")
    if record["phase"] not in _PHASES:
        raise ValueError("artifact violation phase is invalid")
    if record["session_id"] is not None:
        _text(record["session_id"], "session_id")
    _text(record["authenticated_caller"], "authenticated_caller")
    _text(record["expected_checkpoint_id"], "expected_checkpoint_id")
    _hash(record["expected_manifest_sha256"], "expected_manifest_sha256")
    _hash(record["observed_manifest_sha256"], "observed_manifest_sha256")
    if record["expected_manifest_sha256"] == record["observed_manifest_sha256"]:
        raise ValueError("artifact violation must compare different manifests")
    if type(record["database_changed"]) is not bool:
        raise ValueError("database_changed must be boolean")
    if _nonnegative_int(record["difference_count"], "difference_count") != len(differences):
        raise ValueError("artifact violation difference count is invalid")
    if not record["database_changed"] and not differences:
        raise ValueError("artifact violation must contain an observed difference")
    _signature_fields(record)
    facts = []
    for difference in differences:
        if not isinstance(difference, dict) or set(difference) != {
            "difference_kind", "logical_path"
        }:
            raise ValueError("artifact violation difference has unsupported fields")
        if difference["difference_kind"] not in _DIFFERENCE_KINDS:
            raise ValueError("artifact violation difference kind is invalid")
        facts.append((difference["difference_kind"], _text(
            difference["logical_path"], "logical_path"
        )))
    if facts != sorted(facts) or len(facts) != len(set(facts)):
        raise ValueError("artifact violation differences must be unique and sorted")


def _violation_differences(conn: sqlite3.Connection, violation_id: str) -> list[dict[str, Any]]:
    return [
        {
            "difference_kind": row["difference_kind"],
            "logical_path": row["logical_path"],
        }
        for row in conn.execute(
            "SELECT * FROM artifact_integrity_violation_entries "
            "WHERE violation_id=? ORDER BY entry_order",
            (violation_id,),
        )
    ]


def append_violation(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    differences: list[dict[str, Any]],
) -> None:
    _validate_violation(record, differences)
    expected = tuple(record[name] for name in _VIOLATION_FIELDS)
    existing = conn.execute(
        "SELECT * FROM artifact_integrity_violations WHERE violation_id=?",
        (record["violation_id"],),
    ).fetchone()
    if existing is not None:
        if tuple(existing[name] for name in _VIOLATION_FIELDS) != expected:
            raise ValueError("artifact violation replay differs from immutable record")
        if _violation_differences(conn, record["violation_id"]) != differences:
            raise ValueError("artifact violation difference replay differs from immutable record")
        return
    checkpoint = conn.execute(
        "SELECT manifest_sha256 FROM artifact_checkpoints WHERE checkpoint_id=?",
        (record["expected_checkpoint_id"],),
    ).fetchone()
    if checkpoint is None or checkpoint["manifest_sha256"] != record["expected_manifest_sha256"]:
        raise ValueError("artifact violation expected checkpoint is inconsistent")
    conn.execute(
        """
        INSERT INTO artifact_integrity_violations(
          violation_id, observed_at, phase, session_id, authenticated_caller,
          expected_checkpoint_id,
          expected_manifest_sha256, observed_manifest_sha256, database_changed,
          difference_count, authority_id, signature_algorithm, signature
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        expected,
    )
    for order, difference in enumerate(differences):
        conn.execute(
            "INSERT INTO artifact_integrity_violation_entries VALUES(?,?,?,?)",
            (
                record["violation_id"], order, difference["difference_kind"],
                difference["logical_path"],
            ),
        )


def append_override(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    expected_fields = set(_OVERRIDE_FIELDS)
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise ValueError("artifact override has unsupported fields")
    for name in (
        "override_id", "violation_id", "resulting_checkpoint_id", "reason",
        "authenticated_caller", "created_at",
    ):
        _text(record[name], name)
    _signature_fields(record)
    expected = tuple(record[name] for name in _OVERRIDE_FIELDS)
    existing = conn.execute(
        "SELECT * FROM artifact_integrity_overrides WHERE override_id=?",
        (record["override_id"],),
    ).fetchone()
    if existing is not None:
        if tuple(existing[name] for name in _OVERRIDE_FIELDS) != expected:
            raise ValueError("artifact override replay differs from immutable record")
        return
    violation = conn.execute(
        "SELECT 1 FROM artifact_integrity_violations WHERE violation_id=?",
        (record["violation_id"],),
    ).fetchone()
    checkpoint = conn.execute(
        "SELECT integrity_state FROM artifact_checkpoints WHERE checkpoint_id=?",
        (record["resulting_checkpoint_id"],),
    ).fetchone()
    if violation is None or checkpoint is None:
        raise ValueError("artifact override references unavailable records")
    if checkpoint["integrity_state"] != "debug_overridden":
        raise ValueError("artifact override checkpoint is not debug-overridden")
    conn.execute(
        """
        INSERT INTO artifact_integrity_overrides(
          override_id, violation_id, resulting_checkpoint_id, reason,
          authenticated_caller, created_at, authority_id,
          signature_algorithm, signature
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        expected,
    )


def _recovery_entries(
    conn: sqlite3.Connection, recovery_id: str
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM artifact_crash_recovery_entries "
        "WHERE recovery_id=? ORDER BY entry_kind DESC, entry_order",
        (recovery_id,),
    ):
        if row["entry_kind"] == "difference":
            output.append({
                "entry_kind": "difference",
                "difference_kind": row["detail_kind"],
                "logical_path": row["logical_path"],
            })
        else:
            output.append({
                "entry_kind": "action",
                "action": row["detail_kind"],
                "logical_path": row["logical_path"],
            })
    return output


def _validate_recovery(
    record: dict[str, Any], entries: list[dict[str, Any]]
) -> None:
    if not isinstance(record, dict) or set(record) != set(_RECOVERY_FIELDS):
        raise ValueError("artifact crash recovery has unsupported fields")
    for name in (
        "recovery_id", "lease_id", "transition_id", "expected_checkpoint_id",
        "authenticated_caller", "previous_owner_process",
        "recovered_by_process", "last_heartbeat_at", "created_at",
    ):
        _text(record[name], name)
    if record["subject_scope"] not in {"run", "content_store"}:
        raise ValueError("artifact crash recovery subject scope is invalid")
    for name in (
        "observed_manifest_sha256", "restored_manifest_sha256",
        "observed_recovery_snapshot_sha256",
    ):
        _hash(record[name], name)
    if type(record["database_changed"]) is not bool:
        raise ValueError("database_changed must be a boolean")
    differences = [row for row in entries if row.get("entry_kind") == "difference"]
    actions = [row for row in entries if row.get("entry_kind") == "action"]
    if _nonnegative_int(record["difference_count"], "difference_count") != len(
        differences
    ):
        raise ValueError("artifact crash recovery difference count is invalid")
    if _nonnegative_int(record["action_count"], "action_count") != len(actions):
        raise ValueError("artifact crash recovery action count is invalid")
    facts: set[tuple[str, str, str]] = set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("artifact crash recovery entry is malformed")
        entry_kind = row.get("entry_kind")
        if entry_kind == "difference" and set(row) == {
            "entry_kind", "difference_kind", "logical_path",
        }:
            detail_kind = row["difference_kind"]
            if detail_kind not in _DIFFERENCE_KINDS:
                raise ValueError("artifact crash recovery difference kind is invalid")
        elif entry_kind == "action" and set(row) == {
            "entry_kind", "action", "logical_path",
        }:
            detail_kind = row["action"]
            if detail_kind not in _RECOVERY_ACTIONS:
                raise ValueError("artifact crash recovery action is invalid")
        else:
            raise ValueError("artifact crash recovery entry has unsupported fields")
        logical_path = _text(row["logical_path"], "logical_path")
        fact = (entry_kind, detail_kind, logical_path)
        if fact in facts:
            raise ValueError("artifact crash recovery entries are duplicated")
        facts.add(fact)
    _signature_fields(record)


def append_recovery(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    _validate_recovery(record, entries)
    expected = tuple(record[name] for name in _RECOVERY_FIELDS)
    existing = conn.execute(
        "SELECT * FROM artifact_crash_recoveries WHERE recovery_id=?",
        (record["recovery_id"],),
    ).fetchone()
    if existing is not None:
        if tuple(existing[name] for name in _RECOVERY_FIELDS) != expected:
            raise ValueError(
                "artifact crash recovery replay differs from immutable record"
            )
        if _recovery_entries(conn, record["recovery_id"]) != entries:
            raise ValueError(
                "artifact crash recovery entry replay differs from immutable record"
            )
        return
    conn.execute(
        """
        INSERT INTO artifact_crash_recoveries(
          recovery_id, subject_scope, lease_id, transition_id,
          expected_checkpoint_id, authenticated_caller,
          previous_owner_process, recovered_by_process, last_heartbeat_at,
          observed_manifest_sha256, restored_manifest_sha256,
          database_changed, difference_count, action_count,
          observed_recovery_snapshot_sha256, created_at, authority_id,
          signature_algorithm, signature
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        expected,
    )
    counters = {"difference": 0, "action": 0}
    for row in entries:
        entry_kind = row["entry_kind"]
        detail_kind = (
            row["difference_kind"]
            if entry_kind == "difference"
            else row["action"]
        )
        conn.execute(
            "INSERT INTO artifact_crash_recovery_entries VALUES(?,?,?,?,?)",
            (
                record["recovery_id"],
                entry_kind,
                counters[entry_kind],
                detail_kind,
                row["logical_path"],
            ),
        )
        counters[entry_kind] += 1


def list_recoveries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    output = []
    for row in conn.execute(
        "SELECT * FROM artifact_crash_recoveries ORDER BY created_at, recovery_id"
    ):
        record = dict(row)
        record["database_changed"] = bool(record["database_changed"])
        record["entries"] = _recovery_entries(conn, record["recovery_id"])
        if record["difference_count"] != sum(
            entry["entry_kind"] == "difference" for entry in record["entries"]
        ) or record["action_count"] != sum(
            entry["entry_kind"] == "action" for entry in record["entries"]
        ):
            raise RuntimeError("artifact crash recovery mirror has incomplete entries")
        output.append(record)
    return output


def integrity_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    checkpoints = list_checkpoints(conn)
    violations = [dict(row) for row in conn.execute(
        "SELECT * FROM artifact_integrity_violations ORDER BY observed_at, violation_id"
    )]
    overrides = [dict(row) for row in conn.execute(
        "SELECT * FROM artifact_integrity_overrides ORDER BY created_at, override_id"
    )]
    recoveries = list_recoveries(conn)
    for violation in violations:
        violation["differences"] = _violation_differences(
            conn, violation["violation_id"]
        )
        if violation["difference_count"] != len(violation["differences"]):
            raise RuntimeError("artifact violation mirror has incomplete differences")
    overridden_ids = {row["violation_id"] for row in overrides}
    unresolved = [
        row["violation_id"] for row in violations
        if row["violation_id"] not in overridden_ids
    ]
    if not checkpoints:
        state = "unverifiable"
    elif unresolved:
        state = "violated"
    elif overrides or checkpoints[-1]["integrity_state"] == "debug_overridden":
        state = "debug_overridden"
    else:
        state = "clean"
    return {
        "state": state,
        "audit_ready": state == "clean",
        "latest_checkpoint": checkpoints[-1] if checkpoints else None,
        "checkpoint_count": len(checkpoints),
        "violation_count": len(violations),
        "override_count": len(overrides),
        "crash_recovery_count": len(recoveries),
        "unresolved_violation_ids": unresolved,
        "violations": violations,
        "overrides": overrides,
        "crash_recoveries": recoveries,
    }
