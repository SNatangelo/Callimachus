# core/infra/integrity/execution_assurance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit execution-assurance policy for standalone and agent invocations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sys

from core.infra.db import ExecutionAssuranceRecord, RunRepository
from core.infra.db.execution_assurance_storage import validate_agent_identity

from .gate import IntegrityGateError, RunIntegrityGate


@dataclass(frozen=True)
class AssuranceResolution:
    assurance: ExecutionAssuranceRecord
    gate: RunIntegrityGate | None


def _require_valid_agent_identity(agent_identity: str | None) -> None:
    if agent_identity is None:
        return
    try:
        validate_agent_identity(agent_identity)
    except ValueError as exc:
        raise IntegrityGateError(str(exc)) from exc


def new_assurance(run_dir: str, agent_identity: str | None) -> AssuranceResolution:
    if agent_identity is None:
        return AssuranceResolution(
            ExecutionAssuranceRecord("standalone", "standalone_unattested"), None
        )
    _require_valid_agent_identity(agent_identity)
    try:
        gate = RunIntegrityGate.from_environment()
        gate.activate_worker(run_dir)
        return AssuranceResolution(
            ExecutionAssuranceRecord("agent", "agent_attested", agent_identity=agent_identity),
            gate,
        )
    except IntegrityGateError as exc:
        return AssuranceResolution(_acknowledged("agent", agent_identity, exc), None)


def resolve_existing(
    run_dir: str,
    agent_identity: str | None,
    *,
    debug_override: bool = False,
    override_reason: str | None = None,
    activation_only: bool = False,
    activate_worker: bool = True,
    mirror_audit_records: bool = True,
    allow_local_downgrade: bool | None = None,
) -> AssuranceResolution:
    _require_valid_agent_identity(agent_identity)
    if allow_local_downgrade is None:
        allow_local_downgrade = _is_run_owner(run_dir)
    repo = RunRepository.open_readonly(run_dir)
    try:
        assurance = repo.get_execution_assurance()
        if assurance.protection == "agent_unprotected_acknowledged":
            _require_identity_match(assurance, agent_identity)
            return AssuranceResolution(assurance, None)
        if assurance.protection == "standalone_unattested":
            if agent_identity is None:
                return AssuranceResolution(assurance, None)
            _require_local_downgrade_allowed(run_dir, allow_local_downgrade)
            target = _acknowledged(
                assurance.initial_origin,
                agent_identity,
                "an agent-identified invocation cannot attest prior standalone history",
            )
            _persist_downgrade(run_dir, target, allow_local_downgrade)
            return AssuranceResolution(target, None)
        _require_identity_match(assurance, agent_identity)
        try:
            gate = RunIntegrityGate.from_environment()
            if activate_worker:
                gate.activate_worker(run_dir)
            if activation_only:
                return AssuranceResolution(assurance, gate)
            preflight_options = {
                "debug_override": debug_override,
                "override_reason": override_reason,
            }
            if not mirror_audit_records:
                preflight_options["mirror_audit_records"] = False
            gate.preflight(run_dir, **preflight_options)
            return AssuranceResolution(assurance, gate)
        except IntegrityGateError as exc:
            _require_local_downgrade_allowed(run_dir, allow_local_downgrade)
            target = _acknowledged(
                assurance.initial_origin, assurance.agent_identity, exc
            )
            _persist_downgrade(run_dir, target, allow_local_downgrade)
            return AssuranceResolution(target, None)
    finally:
        repo.close()


def downgrade_after_authority_failure(
    run_dir: str, assurance: ExecutionAssuranceRecord, error: BaseException
) -> AssuranceResolution:
    if assurance.protection == "agent_unprotected_acknowledged":
        return AssuranceResolution(assurance, None)
    if assurance.agent_identity is None:
        raise IntegrityGateError("authority failure has no agent identity to acknowledge")
    _require_local_downgrade_allowed(run_dir, _is_run_owner(run_dir))
    target = _acknowledged(assurance.initial_origin, assurance.agent_identity, error)
    _persist_downgrade(run_dir, target, _is_run_owner(run_dir))
    return AssuranceResolution(target, None)


def derived_child_assurance(
    source_run_dir: str, agent_identity: str | None, *,
    debug_override: bool = False, override_reason: str | None = None,
    mirror_audit_records: bool = True,
) -> AssuranceResolution:
    """Inspect a source run without mutating it; choose conservative child posture."""
    if agent_identity is None:
        return AssuranceResolution(
            ExecutionAssuranceRecord("standalone", "standalone_unattested"), None
        )
    _require_valid_agent_identity(agent_identity)
    repo = RunRepository.open_readonly(source_run_dir)
    try:
        source = repo.get_execution_assurance()
    finally:
        repo.close()
    if source.protection != "agent_attested":
        if source.protection == "agent_unprotected_acknowledged":
            # A derived child is a new agent execution and needs its own
            # explicit human acknowledgement.
            return AssuranceResolution(
                _acknowledged(
                    "agent",
                    agent_identity,
                    "derived from an unprotected agent source run",
                ),
                None,
            )
        return AssuranceResolution(
            _acknowledged(
                "agent",
                agent_identity,
                "derived from unattested source run",
            ),
            None,
        )
    try:
        gate = RunIntegrityGate.from_environment()
        gate.activate_worker(source_run_dir)
        preflight_options = {
            "debug_override": debug_override,
            "override_reason": override_reason,
        }
        if not mirror_audit_records:
            preflight_options["mirror_audit_records"] = False
        gate.preflight(source_run_dir, **preflight_options)
        gate.preflight_content_store(
            source_run_dir, debug_override=debug_override, override_reason=override_reason
        )
        return AssuranceResolution(
            ExecutionAssuranceRecord("agent", "agent_attested", agent_identity=agent_identity), gate
        )
    except IntegrityGateError as exc:
        return AssuranceResolution(_acknowledged("agent", agent_identity, exc), None)


def _require_identity_match(assurance: ExecutionAssuranceRecord, agent_identity: str | None) -> None:
    if agent_identity is not None and agent_identity != assurance.agent_identity:
        raise IntegrityGateError("agent identity does not match persisted run identity")


def _is_run_owner(run_dir: str) -> bool:
    """Whether this process may mutate the protected run's local state."""
    if os.name != "posix":
        return True
    try:
        current_uid = os.getuid()
        return current_uid == 0 or (
            os.stat(os.path.join(run_dir, "run.sqlite")).st_uid == current_uid
        )
    except OSError as exc:
        raise IntegrityGateError("cannot determine run database ownership") from exc


def _persist_downgrade(
    run_dir: str, target: ExecutionAssuranceRecord, allowed: bool
) -> None:
    _require_local_downgrade_allowed(run_dir, allowed)
    repo = RunRepository.open(run_dir)
    try:
        repo.downgrade_execution_assurance(target)
    finally:
        repo.close()


def _require_local_downgrade_allowed(run_dir: str, allowed: bool) -> None:
    if not allowed or not _is_run_owner(run_dir):
        raise IntegrityGateError(
            "this task must be rerun through the official trusted runner; "
            "the submitting process cannot acknowledge or persist a run downgrade"
        )


def _acknowledged(initial_origin: str, agent_identity: str, error: BaseException | str) -> ExecutionAssuranceRecord:
    reason = _technical_reason(error)
    _confirm_human(reason)
    return ExecutionAssuranceRecord(
        initial_origin=initial_origin,
        protection="agent_unprotected_acknowledged",
        agent_identity=agent_identity,
        failure_reason=reason,
        acknowledged_at=datetime.now(timezone.utc).isoformat(),
    )


def _technical_reason(error: BaseException | str) -> str:
    text = str(error).replace("\x00", "?").strip()
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {text}"
    return text or "integrity authority unavailable"


def _confirm_human(reason: str) -> None:
    print(
        "\n!!! EXECUTION ASSURANCE ALERT !!!\n"
        "The integrity authority is unavailable or this run has prior unattested history.\n"
        "Only the human user may answer this prompt. Continuing permanently marks "
        "the run as unprotected and not reliable for agent-resistant verification.\n"
        f"Technical reason: {reason}\n",
        file=sys.stderr,
    )
    if not sys.stdin.isatty():
        raise IntegrityGateError(
            "human confirmation required on an interactive TTY; refusing non-interactive continuation"
        )
    try:
        answer = input("Human user: continue as unprotected? [yes/no] ").strip().lower()
    except EOFError as exc:
        raise IntegrityGateError("human confirmation was not provided") from exc
    if answer not in {"yes", "y"}:
        raise IntegrityGateError("human user declined unprotected continuation")
