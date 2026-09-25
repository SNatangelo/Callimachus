# core/infra/db/execution_assurance_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed typed storage for a run's execution-assurance posture."""

from __future__ import annotations

from datetime import datetime, timezone
import re

from .models import ExecutionAssuranceRecord


_ORIGINS = frozenset(("standalone", "agent"))
_PROTECTIONS = frozenset((
    "standalone_unattested",
    "agent_attested",
    "agent_unprotected_acknowledged",
))
_AGENT_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", re.ASCII)

EXECUTION_ASSURANCE_DDL = """
CREATE TABLE IF NOT EXISTS execution_assurance (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  initial_origin TEXT NOT NULL CHECK(initial_origin IN ('standalone','agent')),
  protection TEXT NOT NULL CHECK(protection IN ('standalone_unattested','agent_attested','agent_unprotected_acknowledged')),
  agent_identity TEXT CHECK(
    agent_identity IS NULL OR (
      typeof(agent_identity)='text'
      AND length(agent_identity) BETWEEN 1 AND 64
      AND substr(agent_identity, 1, 1) GLOB '[A-Za-z0-9]'
      AND agent_identity NOT GLOB '*[^A-Za-z0-9._:-]*'
      AND instr(agent_identity, char(0))=0
    )
  ),
  failure_reason TEXT,
  acknowledged_at TEXT,
  CHECK(
    (protection='standalone_unattested' AND initial_origin='standalone' AND agent_identity IS NULL AND failure_reason IS NULL AND acknowledged_at IS NULL)
    OR (protection='agent_attested' AND initial_origin='agent' AND agent_identity IS NOT NULL AND failure_reason IS NULL AND acknowledged_at IS NULL)
    OR (protection='agent_unprotected_acknowledged' AND agent_identity IS NOT NULL AND failure_reason IS NOT NULL AND length(trim(failure_reason))>0 AND instr(failure_reason,char(0))=0 AND acknowledged_at IS NOT NULL AND length(acknowledged_at)>0)
  )
);

CREATE TRIGGER IF NOT EXISTS execution_assurance_no_delete
BEFORE DELETE ON execution_assurance
BEGIN
  SELECT RAISE(ABORT, 'execution assurance cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS execution_assurance_transition_guard
BEFORE UPDATE ON execution_assurance
WHEN NOT (
  (
    NEW.initial_origin IS OLD.initial_origin
    AND NEW.protection IS OLD.protection
    AND NEW.agent_identity IS OLD.agent_identity
    AND NEW.failure_reason IS OLD.failure_reason
    AND NEW.acknowledged_at IS OLD.acknowledged_at
  )
  OR (
    OLD.protection='agent_attested'
    AND NEW.protection='agent_unprotected_acknowledged'
    AND NEW.initial_origin IS OLD.initial_origin
    AND NEW.agent_identity IS OLD.agent_identity
  )
  OR (
    OLD.protection='standalone_unattested'
    AND NEW.protection='agent_unprotected_acknowledged'
    AND NEW.initial_origin IS OLD.initial_origin
    AND OLD.agent_identity IS NULL
    AND NEW.agent_identity IS NOT NULL
  )
)
BEGIN
  SELECT RAISE(ABORT, 'execution assurance transition is not monotonic');
END;
"""


def standalone_unattested() -> ExecutionAssuranceRecord:
    return ExecutionAssuranceRecord(
        initial_origin="standalone", protection="standalone_unattested"
    )


def validate_agent_identity(value: object) -> str:
    """Reject non-canonical agent identities without rewriting caller input."""
    if not isinstance(value, str) or _AGENT_IDENTITY_RE.fullmatch(value) is None:
        raise ValueError("execution assurance agent identity is invalid")
    return value


def normalize(value: ExecutionAssuranceRecord) -> ExecutionAssuranceRecord:
    if not isinstance(value, ExecutionAssuranceRecord):
        raise ValueError("execution assurance must be an ExecutionAssuranceRecord")
    if value.initial_origin not in _ORIGINS:
        raise ValueError("execution assurance initial origin is invalid")
    if value.protection not in _PROTECTIONS:
        raise ValueError("execution assurance protection is invalid")
    if value.agent_identity is not None:
        validate_agent_identity(value.agent_identity)
    if value.protection == "standalone_unattested":
        if (
            value.initial_origin != "standalone"
            or value.agent_identity is not None
            or value.failure_reason is not None
            or value.acknowledged_at is not None
        ):
            raise ValueError("standalone assurance state is malformed")
    elif value.protection == "agent_attested":
        if value.agent_identity is None:
            raise ValueError("attested agent assurance requires an agent identity")
        if (
            value.initial_origin != "agent"
            or value.failure_reason is not None
            or value.acknowledged_at is not None
        ):
            raise ValueError("attested agent assurance state is malformed")
    else:
        if value.agent_identity is None:
            raise ValueError("unprotected agent assurance requires an agent identity")
        if not isinstance(value.failure_reason, str) or not value.failure_reason.strip():
            raise ValueError("unprotected agent assurance requires a failure reason")
        if "\x00" in value.failure_reason:
            raise ValueError("execution assurance failure reason contains NUL")
        if not isinstance(value.acknowledged_at, str):
            raise ValueError("unprotected agent assurance requires UTC acknowledgement")
        try:
            acknowledged = datetime.fromisoformat(value.acknowledged_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("execution assurance acknowledgement timestamp is invalid") from exc
        if acknowledged.tzinfo is None or acknowledged.utcoffset() != timezone.utc.utcoffset(acknowledged):
            raise ValueError("execution assurance acknowledgement must be UTC")
        return ExecutionAssuranceRecord(
            initial_origin=value.initial_origin,
            protection=value.protection,
            agent_identity=value.agent_identity,
            failure_reason=value.failure_reason,
            acknowledged_at=acknowledged.astimezone(timezone.utc).isoformat(),
        )
    return value


def insert(conn, value: ExecutionAssuranceRecord) -> None:
    assurance = normalize(value)
    conn.execute(
        """
        INSERT INTO execution_assurance(
          singleton, initial_origin, protection, agent_identity, failure_reason, acknowledged_at
        ) VALUES(1, ?, ?, ?, ?, ?)
        """,
        (
            assurance.initial_origin,
            assurance.protection,
            assurance.agent_identity,
            assurance.failure_reason,
            assurance.acknowledged_at,
        ),
    )


def read(conn) -> ExecutionAssuranceRecord:
    row = conn.execute("SELECT * FROM execution_assurance WHERE singleton=1").fetchone()
    if row is None:
        raise RuntimeError("execution assurance singleton missing")
    try:
        return normalize(ExecutionAssuranceRecord(
            initial_origin=row["initial_origin"],
            protection=row["protection"],
            agent_identity=row["agent_identity"],
            failure_reason=row["failure_reason"],
            acknowledged_at=row["acknowledged_at"],
        ))
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise RuntimeError("execution assurance storage is malformed") from exc


def downgrade(conn, value: ExecutionAssuranceRecord) -> None:
    current = read(conn)
    target = normalize(value)
    if target.initial_origin != current.initial_origin:
        raise ValueError("execution assurance initial origin is immutable")
    identity_assignment = (
        current.protection == "standalone_unattested"
        and current.agent_identity is None
        and target.protection == "agent_unprotected_acknowledged"
        and target.agent_identity is not None
    )
    if target.agent_identity != current.agent_identity and not identity_assignment:
        raise ValueError("execution assurance agent identity is immutable")
    if target == current:
        return
    if current.protection == "agent_unprotected_acknowledged":
        raise ValueError("unprotected execution assurance cannot be upgraded")
    if target.protection != "agent_unprotected_acknowledged":
        raise ValueError("execution assurance may only remain or downgrade")
    conn.execute(
        """
        UPDATE execution_assurance
        SET protection=?, agent_identity=?, failure_reason=?, acknowledged_at=?
        WHERE singleton=1
        """,
        (target.protection, target.agent_identity, target.failure_reason, target.acknowledged_at),
    )
