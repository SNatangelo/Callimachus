# core/verify/claim_evidence/adapters/runtime_repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bridge authoritative run events to shared operational projections."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from core.infra.llm_runtime import (
    LLMRuntimeRepository,
    open_runtime,
)
from core.infra.db import llm_dispatches
from core.verify.claim_evidence.domain.cooldown import (
    CooldownState,
    success,
    transition,
)
from core.verify.claim_evidence.domain.pacing import PacingState
from core.verify.claim_evidence.domain.selection import SelectionState
from core.verify.claim_evidence.domain.types import ProviderPolicy, SchedulerState

from .scheduler_controls import configured_controls

_PROVIDER_EVENTS = frozenset({"started", "completed", "failed", "abandoned"})
_CONTROL_EVENTS = frozenset({"completed", "failed", "abandoned"})


class RuntimeRepository:
    """Project run facts without changing or reinterpreting the run ledger."""

    def __init__(
        self,
        run_conn: sqlite3.Connection | None = None,
        *,
        environ: dict[str, str] | None = None,
        runtime_conn: sqlite3.Connection | None = None,
        run_lock: Any | None = None,
    ):
        self._run_conn = run_conn
        if self._run_conn is not None:
            self._run_conn.row_factory = sqlite3.Row
        self._conn = runtime_conn if runtime_conn is not None else open_runtime(environ)
        self._lock, self._run_lock = threading.RLock(), run_lock or threading.RLock()
        self._repo = LLMRuntimeRepository(self._conn)

    def bind_run_lock(self, lock: Any) -> None: self._run_lock = lock

    def replay_dispatch_observations(self) -> int:
        """Replay missing immutable observations; never call a provider."""
        with self._lock, self._run_lock:
            if self._run_conn is None:
                raise ValueError("run connection is required for replay")
            rows = self._run_conn.execute(
            """
            SELECT e.*, r.candidate_id,
                   a.provider_id, a.model_id, a.credential_id,
                   a.credential_fingerprint, a.lane_id, (SELECT run_id FROM run LIMIT 1) AS run_id
            FROM llm_dispatch_events e
            JOIN llm_dispatch_attempts a
              ON a.dispatch_attempt_id = e.dispatch_attempt_id
            JOIN llm_logical_requests r
              ON r.logical_request_id = e.logical_request_id
            WHERE e.event_type IN ('started','completed','failed','abandoned')
            ORDER BY e.created_at, e.event_id
            """
            ).fetchall()
            return sum(self._replay_observation(dict(row)) for row in rows)

    @staticmethod
    def terminal_control(
        assignment: Any, result: Any, cooldown_policy: Any,
    ) -> dict[str, Any] | None:
        return _scheduler_control(assignment, result, cooldown_policy)

    def replay_scheduler_controls(self) -> int:
        """Replay audited control projections before any provider dispatch."""
        with self._lock, self._run_lock:
            if self._run_conn is None:
                raise ValueError("run connection is required for replay")
            rows = self._run_conn.execute(
            """
            SELECT * FROM llm_dispatch_events
            WHERE event_type IN ('completed','failed','abandoned')
            ORDER BY created_at, event_id
            """
            ).fetchall()
            controls = (
                llm_dispatches.decode_event(dict(row), conn=self._run_conn)["payload"].get("scheduler_control")
                for row in rows
            )
            return sum(self._apply_control(control) for control in controls
                       if control is not None)

    def _apply_control(self, payload: dict[str, Any]) -> int:
        event_id = payload["source_event_id"]
        applied = False
        if payload.get("cooldown") is not None:
            applied |= self.project_cooldown(
                run_event_id=event_id, **payload["cooldown"])
        if payload.get("lane") is not None:
            applied |= self.project_lane(
                run_event_id=event_id, **payload["lane"])
        if payload.get("profile") is not None:
            applied |= self.project_backoff_profile(
                run_event_id=event_id, **payload["profile"])
        return int(applied)

    def scheduler_controls(self) -> dict[str, Any]:
        """Return only non-secret shared controls for scheduler reconstruction."""
        snapshot = self._repo.debug_snapshot()
        disabled = frozenset(
            (_provider(row["credential_id"]), row["credential_id"])
            for row in snapshot["state"]
            if row["disabled"]
        )
        eligibility = tuple(
            (
                (_provider(row["credential_id"]), row["credential_id"]),
                int(row["next_eligible_at"]),
            )
            for row in snapshot["state"]
            if row["next_eligible_at"] is not None
        )
        lanes = frozenset(
            (
                _provider(row["credential_id"]),
                row["credential_id"],
                row["model_id"],
            )
            for row in snapshot["lanes"]
            if not row["available"]
        )
        rows = self._conn.execute(
            """
            SELECT e.credential_id, e.model_id, s.next_eligible_at,
                   s.last_applied_seconds, s.learned_seconds,
                   s.post_cooldown_successes, s.policy_version
            FROM cooldown_events e JOIN cooldown_states s ON s.event_id = e.event_id
            WHERE e.status IN ('cooldown','cleared')
            ORDER BY e.observed_at, e.event_id
            """
        ).fetchall()
        cooldowns = {
            (
                _provider(row["credential_id"]),
                row["credential_id"],
                row["model_id"],
            ): {key: row[key] for key in ("next_eligible_at", "last_applied_seconds", "learned_seconds", "post_cooldown_successes", "policy_version")}
            for row in rows
        }
        return {
            "disabled_credentials": disabled,
            "disabled_lanes": lanes,
            "key_eligibility_ms": eligibility,
            "cooldowns": cooldowns,
        }

    def scheduler_state(
        self,
        snapshot: dict[str, Any],
        providers: tuple[ProviderPolicy, ...],
    ) -> SchedulerState:
        """Combine run cursors/pacing with authoritative shared controls."""
        controls = self.scheduler_controls()
        controls = configured_controls(controls, providers)
        attempts = snapshot["dispatch_attempts"]
        last = max(
            attempts,
            key=lambda row: int(row["dispatch_attempt_id"].rsplit(":", 1)[1]),
        ) if attempts else {}
        started_ids = {
            row["dispatch_attempt_id"] for row in snapshot["dispatch_events"]
            if row["event_type"] == "started"
        }
        starts = [
            row for row in attempts
            if row["dispatch_attempt_id"] in started_ids
            and row.get("queued_at") is not None
        ]
        by_model: dict[str, int] = {}
        for row in starts:
            key, started = f"{row['provider_id']}:{row['model_id']}", int(row["queued_at"])
            by_model[key] = max(by_model.get(key, started), started)
        pacing = PacingState(
            max(int(row["queued_at"]) for row in starts) if starts else None,
            tuple(sorted(by_model.items())),
        )
        cooldowns = tuple(
            (key, CooldownState(**value))
            for key, value in controls["cooldowns"].items()
        )
        return SchedulerState(
            SelectionState(
                last.get("credential_cursor", 0),
                last.get("model_draw_index", 0),
            ),
            pacing,
            cooldowns,
            tuple(controls["key_eligibility_ms"]),
            controls["disabled_credentials"],
            controls["disabled_lanes"],
            int(last["dispatch_attempt_id"].rsplit(":", 1)[1]) if last else 0,
        )

    def close(self) -> None:
        self._conn.close()

    def project_lane(
        self,
        *,
        run_event_id: str,
        credential_id: str,
        model_id: str,
        lane_id: str,
        available: bool,
    ) -> bool:
        source = self._control_source(run_event_id)
        self._assert_source(
            source,
            credential_id=credential_id,
            model_id=model_id,
            lane_id=lane_id,
        )
        self._register_source(source)
        return self._repo.apply_lane(source_run_id=source["run_id"], source_event_id=run_event_id, credential_id=credential_id, model_id=model_id, lane_id=lane_id, available=available, updated_at=source["created_at"])

    def project_cooldown(
        self,
        *,
        run_event_id: str,
        credential_id: str,
        model_id: str | None,
        next_eligible_at: int | None,
        status: str,
        disabled: bool,
        reason: str | None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        source = self._control_source(run_event_id)
        self._assert_source(
            source,
            credential_id=credential_id,
            model_id=model_id,
        )
        self._register_source(source)
        return self._repo.apply_cooldown(source_run_id=source["run_id"], source_event_id=run_event_id, credential_id=credential_id, model_id=model_id, observed_at=source["created_at"], next_eligible_at=next_eligible_at, status=status, disabled=disabled, reason=reason, payload=payload)

    def project_backoff_profile(
        self,
        *,
        run_event_id: str,
        provider_id: str,
        credential_fingerprint: str,
        model_id: str | None,
        profile_version: str,
        baseline_seconds: int,
        multiplier: int,
        local_max_seconds: int,
        stable_successes: int,
    ) -> bool:
        source = self._control_source(run_event_id)
        self._assert_source(
            source,
            provider_id=provider_id,
            credential_fingerprint=credential_fingerprint,
            model_id=model_id,
        )
        self._register_source(source)
        return self._repo.apply_backoff_profile(source_run_id=source["run_id"], source_event_id=run_event_id, provider_id=provider_id, credential_fingerprint=credential_fingerprint, model_id=model_id, profile_version=profile_version, baseline_seconds=baseline_seconds, multiplier=multiplier, local_max_seconds=local_max_seconds, stable_successes=stable_successes, updated_at=source["created_at"])

    def _replay_observation(self, row: dict[str, Any]) -> int:
        if row["event_type"] not in _PROVIDER_EVENTS:
            raise ValueError("run event is not provider-visible")
        self._register_source(row)
        payload = llm_dispatches.decode_event(row, conn=self._run_conn)["payload"]
        runtime_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"protocol_error_code", "response_hash"}
        }
        _, applied = self._repo.record_model_observation_status(
            source_run_id=row["run_id"], source_event_id=row["event_id"],
            logical_request_id=row["logical_request_id"],
            candidate_id=row["candidate_id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            credential_id=row["credential_id"],
            event_type=row["event_type"],
            payload=runtime_payload,
            created_at=row["created_at"],
        )
        return int(applied)

    def _control_source(self, run_event_id: str) -> sqlite3.Row:
        with self._run_lock:
            if self._run_conn is None:
                raise ValueError("run connection is required for projection")
            row = self._run_conn.execute(
                """
                SELECT e.event_id, e.event_type, e.created_at,
                       a.provider_id, a.model_id, a.credential_id, a.credential_fingerprint,
                       a.lane_id, (SELECT run_id FROM run LIMIT 1) AS run_id
                FROM llm_dispatch_events e
                JOIN llm_dispatch_attempts a ON a.dispatch_attempt_id = e.dispatch_attempt_id
                WHERE e.event_id = ?
                """,
                (run_event_id,),
            ).fetchone()
        if row is None or row["event_type"] not in _CONTROL_EVENTS:
            raise ValueError("control projection lacks a terminal run event")
        return row

    def _register_source(self, source: sqlite3.Row) -> None:
        self._repo.register_credential(
            credential_id=source["credential_id"],
            provider_id=source["provider_id"],
            credential_alias=source["credential_id"],
            credential_fingerprint=source["credential_fingerprint"],
            created_at=source["created_at"],
        )

    @staticmethod
    def _assert_source(source: sqlite3.Row, **expected: object) -> None:
        for name, value in expected.items():
            if source[name] != value:
                raise ValueError(
                    "runtime projection differs from authoritative run event"
                )


def _provider(credential_id: str) -> str:
    return credential_id.split(":", 1)[0]


def _scheduler_control(
    assignment: Any, result: Any, policy: Any,
) -> dict[str, Any] | None:
    cause = getattr(result, "cause", None)
    prior = assignment.prior_cooldown if isinstance(
        assignment.prior_cooldown, CooldownState
    ) else CooldownState()
    current = None
    if cause == "rate_limited":
        if not isinstance(result.observed_at_ms, int):
            raise ValueError("rate-limit observation time is missing")
        current = transition(
            policy, prior, now=(result.observed_at_ms + 999) // 1000,
            status=429, retry_after_seconds=result.retry_after_seconds,
            configured_override_seconds=assignment.cooldown_override_seconds,
            post_cooldown=assignment.post_cooldown,
        )
    elif cause is None and assignment.post_cooldown:
        current = success(policy, prior, post_cooldown=True)
    payload: dict[str, Any] = {
        "source_event_id": f"{assignment.dispatch_attempt_id}:terminal",
        "cooldown": None, "lane": None, "profile": None}
    if cause == "credential_invalid":
        payload["cooldown"] = {
            "credential_id": assignment.credential_alias,
            "model_id": assignment.model, "next_eligible_at": None,
            "status": "quarantined", "disabled": True, "reason": cause}
    elif cause == "lane_unavailable":
        payload["lane"] = {
            "credential_id": assignment.credential_alias,
            "model_id": assignment.model, "lane_id": ":".join(
                assignment.lane_id), "available": False}
    elif cause == "rate_limited":
        payload["cooldown"] = {
            "credential_id": assignment.credential_alias,
            "model_id": assignment.model, "disabled": False,
            "next_eligible_at": current.next_eligible_at * 1000,
            "status": "cooldown", "reason": cause,
            "payload": _cooldown_payload(current)}
    elif cause is None and current is not None:
        payload["cooldown"] = {
            "credential_id": assignment.credential_alias,
            "model_id": assignment.model, "next_eligible_at": None,
            "status": "cleared", "disabled": False, "reason": None,
            "payload": _cooldown_payload(current)}
    if current is not None and assignment.credential_fingerprint is not None:
        payload["profile"] = {
            "provider_id": assignment.provider,
            "credential_fingerprint": assignment.credential_fingerprint,
            "model_id": assignment.model, "profile_version": current.policy_version,
            "baseline_seconds": current.learned_seconds or policy.global_seconds,
            "multiplier": policy.multiplier, "local_max_seconds": policy.local_max_seconds,
            "stable_successes": policy.stable_successes}
    names = ("cooldown", "lane", "profile")
    return payload if any(payload[name] is not None for name in names) else None


def _cooldown_payload(state: Any) -> dict[str, Any]:
    return {
        "next_eligible_at": state.next_eligible_at,
        "last_applied_seconds": state.last_applied_seconds,
        "learned_seconds": state.learned_seconds,
        "post_cooldown_successes": state.post_cooldown_successes,
        "policy_version": state.policy_version}
