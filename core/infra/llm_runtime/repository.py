# core/infra/llm_runtime/repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Transactional, replay-safe operational projections."""

from __future__ import annotations

import re
import sqlite3
import math
from typing import Any

from .connection import RuntimeStateError
from .schema_bootstrap import ensure_schema
from .projections import runtime_payload_id, runtime_projection_id

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER = re.compile(r"^[A-Za-z0-9_.-]+$")
_SECRET_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "credential_value",
        "raw_key",
        "secret",
    }
)
_SECRET_PREFIXES = ("sk-", "AIza", "ghp_", "xoxb-")
_CONTROL_REASONS = frozenset(
    {None, "rate_limited", "credential_invalid"}
)
_FAILURE_TECHNICAL_RESULTS = frozenset(
    {
        "protocol_invalid",
        "rate_limited",
        "credential_invalid",
        "lane_unavailable",
        "timeout",
        "transport",
        "provider_failure",
    }
)
_RETRY_CAUSES = frozenset({"rate_limited", "credential_invalid", "lane_unavailable", "timeout", "transport", "provider_failure", "lease_expired"})


class LLMRuntimeRepository:
    """Persist non-secret cross-run state without becoming run authority."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self._conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeStateError("LLM runtime foreign keys are disabled")
        ensure_schema(self._conn)

    def register_credential(
        self,
        *,
        credential_id: str,
        provider_id: str,
        credential_alias: str,
        credential_fingerprint: str | None,
        created_at: str,
    ) -> None:
        """Register one stable non-secret identity, preserving first-seen time."""
        _credential_identity(
            credential_id,
            provider_id,
            credential_alias,
            credential_fingerprint,
        )
        _text("created_at", created_at)
        identity = (provider_id, credential_alias, credential_fingerprint)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO credentials VALUES(?,?,?,?,?)",
                (credential_id, *identity, created_at),
            )
            row = self._conn.execute(
                """
                SELECT provider_id, credential_alias, credential_fingerprint
                FROM credentials WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
        if row is None or tuple(row) != identity:
            if row is not None:
                raise ValueError(
                    "credential replay differs from immutable identity"
                )
            raise ValueError("credential alias conflicts with immutable identity")

    def apply_lane(
        self,
        *,
        source_run_id: str,
        source_event_id: str,
        credential_id: str,
        model_id: str,
        lane_id: str,
        available: bool,
        updated_at: str,
    ) -> bool:
        """Apply an already-computed lane state transition."""
        _text("credential_id", credential_id)
        _text("model_id", model_id)
        _text("lane_id", lane_id)
        _text("updated_at", updated_at)
        if not isinstance(available, bool):
            raise ValueError("available must be boolean")
        kind = "credential_lane"
        projection_id = runtime_projection_id(source_run_id, source_event_id, kind=kind)
        payload = {
            "credential_id": credential_id,
            "model_id": model_id,
            "lane_id": lane_id,
            "available": available,
        }
        payload_hash = runtime_payload_id(payload)
        with self._conn:
            if not self._claim(
                projection_id, source_run_id, source_event_id, kind, payload_hash, updated_at
            ):
                return False
            self._conn.execute(
                """
                INSERT INTO credential_models VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(credential_id, model_id, lane_id) DO UPDATE SET
                  available = excluded.available,
                  source_projection_id = excluded.source_projection_id,
                  payload_hash = excluded.payload_hash,
                  updated_at = excluded.updated_at
                """,
                (
                    credential_id,
                    model_id,
                    lane_id,
                    int(available),
                    projection_id,
                    payload_hash,
                    updated_at,
                ),
            )
        return True

    def apply_cooldown(
        self,
        *,
        source_run_id: str,
        source_event_id: str,
        credential_id: str,
        model_id: str | None,
        observed_at: str,
        next_eligible_at: int | None,
        status: str,
        disabled: bool,
        reason: str | None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically append a control event and update key-wide state."""
        _text("credential_id", credential_id)
        _optional_text("model_id", model_id)
        _text("observed_at", observed_at)
        _optional_int("next_eligible_at", next_eligible_at)
        _optional_text("reason", reason)
        if status not in {"cooldown", "quarantined", "cleared"}:
            raise ValueError("invalid cooldown status")
        if not isinstance(disabled, bool):
            raise ValueError("disabled must be boolean")
        if reason not in _CONTROL_REASONS:
            raise ValueError("invalid credential control reason")
        expected = {
            "cooldown": (
                False,
                "rate_limited",
                True,
            ),
            "quarantined": (True, "credential_invalid", False),
            "cleared": (False, None, False),
        }[status]
        if (disabled, reason, next_eligible_at is not None) != expected:
            raise ValueError("credential control state is inconsistent")
        state = _cooldown_state(payload)
        if status in {"cooldown", "cleared"} and state is None:
            raise ValueError("cooldown state is required")
        if status == "quarantined" and state is not None:
            raise ValueError("quarantined cooldown forbids state")
        _reject_secrets(reason)
        kind = "cooldown"
        event_id = runtime_projection_id(source_run_id, source_event_id, kind=kind)
        event = (
            event_id,
            credential_id,
            model_id,
            observed_at,
            next_eligible_at,
            status,
        )
        projection = {
            "event": event,
            "disabled": disabled,
            "reason": reason,
            "state": state,
        }
        payload_hash = runtime_payload_id(projection)
        with self._conn:
            if not self._claim(
                event_id, source_run_id, source_event_id, kind, payload_hash, observed_at
            ):
                return False
            self._conn.execute(
                "INSERT INTO cooldown_events VALUES(?,?,?,?,?,?)",
                event,
            )
            if state is not None:
                self._conn.execute("INSERT INTO cooldown_states VALUES(?,?,?,?,?,?)", (event_id, *state))
            self._conn.execute(
                """
                INSERT INTO credential_state VALUES(?,?,?,?,?,?)
                ON CONFLICT(credential_id) DO UPDATE SET
                  next_eligible_at_ms = excluded.next_eligible_at_ms,
                  disabled = excluded.disabled,
                  reason = excluded.reason,
                  source_projection_id = excluded.source_projection_id,
                  updated_at = excluded.updated_at
                """,
                (
                    credential_id,
                    next_eligible_at,
                    int(disabled),
                    reason,
                    event_id,
                    observed_at,
                ),
            )
        return True

    def apply_backoff_profile(
        self,
        *,
        source_run_id: str,
        source_event_id: str,
        provider_id: str,
        credential_fingerprint: str,
        model_id: str | None,
        profile_version: str,
        baseline_seconds: int,
        multiplier: int,
        local_max_seconds: int,
        stable_successes: int,
        updated_at: str,
    ) -> bool:
        """Apply a versioned learned profile without calculating policy."""
        _provider(provider_id)
        _fingerprint(credential_fingerprint)
        _optional_text("model_id", model_id)
        _text("profile_version", profile_version)
        _text("updated_at", updated_at)
        _nonnegative("baseline_seconds", baseline_seconds)
        _positive("multiplier", multiplier)
        _nonnegative("local_max_seconds", local_max_seconds)
        _nonnegative("stable_successes", stable_successes)
        if local_max_seconds < baseline_seconds:
            raise ValueError("local maximum is below baseline")
        known = self._conn.execute(
            """
            SELECT 1 FROM credentials
            WHERE provider_id = ? AND credential_fingerprint = ?
            """,
            (provider_id, credential_fingerprint),
        ).fetchone()
        if known is None:
            raise ValueError("backoff profile credential is not registered")
        model_key = model_id or ""
        kind = "backoff_profile"
        projection_id = runtime_projection_id(source_run_id, source_event_id, kind=kind)
        payload = {
            "provider_id": provider_id,
            "credential_fingerprint": credential_fingerprint,
            "model_id": model_key,
            "profile_version": profile_version,
            "baseline_seconds": baseline_seconds,
            "multiplier": multiplier,
            "local_max_seconds": local_max_seconds,
            "stable_successes": stable_successes,
        }
        payload_hash = runtime_payload_id(payload)
        profile_key = runtime_payload_id(
            {
                key: payload[key]
                for key in (
                    "provider_id",
                    "credential_fingerprint",
                    "model_id",
                    "profile_version",
                )
            }
        )
        with self._conn:
            if not self._claim(
                projection_id,
                source_run_id, source_event_id,
                kind,
                payload_hash,
                updated_at,
            ):
                return False
            self._conn.execute(
                """
                INSERT INTO backoff_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_key) DO UPDATE SET
                  baseline_seconds = excluded.baseline_seconds,
                  multiplier = excluded.multiplier,
                  local_max_seconds = excluded.local_max_seconds,
                  stable_successes = excluded.stable_successes,
                  source_projection_id = excluded.source_projection_id,
                  payload_hash = excluded.payload_hash,
                  updated_at = excluded.updated_at
                """,
                (
                    profile_key,
                    provider_id,
                    credential_fingerprint,
                    model_key,
                    profile_version,
                    baseline_seconds,
                    multiplier,
                    local_max_seconds,
                    stable_successes,
                    projection_id,
                    payload_hash,
                    updated_at,
                ),
            )
        return True

    def record_model_observation(self, **values: Any) -> str:
        """Record an observation and return its stable projection identity."""
        return self.record_model_observation_status(**values)[0]

    def record_model_observation_status(
        self,
        *,
        source_run_id: str,
        source_event_id: str,
        logical_request_id: str,
        candidate_id: str | None,
        provider_id: str,
        model_id: str,
        credential_id: str,
        event_type: str,
        payload: dict[str, Any] | None,
        created_at: str,
    ) -> tuple[str, bool]:
        """Return the projection ID and whether this call applied it."""
        for name, value in (
            ("logical_request_id", logical_request_id),
            ("model_id", model_id),
            ("credential_id", credential_id),
            ("event_type", event_type),
            ("created_at", created_at),
        ):
            _text(name, value)
        _provider(provider_id)
        _optional_text("candidate_id", candidate_id)
        _observation_payload(
            payload,
            event_type,
            source_event_id=source_event_id,
            logical_request_id=logical_request_id,
            credential_id=credential_id,
            provider_id=provider_id,
            model_id=model_id,
        )
        kind = "model_observation"
        projection_id = runtime_projection_id(source_run_id, source_event_id, kind=kind)
        payload_hash = runtime_payload_id({"logical_request_id": logical_request_id, "candidate_id": candidate_id, "provider_id": provider_id, "model_id": model_id, "credential_id": credential_id, "event_type": event_type, "payload": payload})
        event = (
            projection_id,
            logical_request_id,
            candidate_id,
            provider_id,
            model_id,
            credential_id,
            event_type,
            payload_hash,
            created_at,
        )
        with self._conn:
            if not self._claim(
                projection_id, source_run_id, source_event_id,
                kind,
                payload_hash,
                created_at,
            ):
                self._same_observation(projection_id, event, source_run_id, source_event_id)
                return projection_id, False
            self._conn.execute(
                "INSERT INTO model_observations VALUES(?,?,?,?,?,?,?,?,?)",
                event,
            )
        return projection_id, True

    def state_for(self, credential_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT next_eligible_at_ms AS next_eligible_at, disabled, reason,
                   source_projection_id, updated_at
            FROM credential_state WHERE credential_id = ?
            """,
            (credential_id,),
        ).fetchone()
        return dict(row) if row else None

    def lane_for(
        self, *, credential_id: str, model_id: str, lane_id: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT available, source_projection_id, updated_at
            FROM credential_models
            WHERE credential_id = ? AND model_id = ? AND lane_id = ?
            """,
            (credential_id, model_id, lane_id),
        ).fetchone()
        return dict(row) if row else None

    def profile_for(
        self,
        *,
        provider_id: str,
        credential_fingerprint: str,
        model_id: str | None,
        profile_version: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT baseline_seconds, multiplier, local_max_seconds,
                   stable_successes, source_projection_id, updated_at
            FROM backoff_profiles
            WHERE provider_id = ? AND credential_fingerprint = ?
              AND model_id = ? AND profile_version = ?
            """,
            (
                provider_id,
                credential_fingerprint,
                model_id or "",
                profile_version,
            ),
        ).fetchone()
        return dict(row) if row else None

    def debug_snapshot(self) -> dict[str, Any]:
        """Return only non-secret identities and current operational state."""
        return {
            "schema_version": self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0],
            "credentials": self._records(
                """
                SELECT credential_id, provider_id, credential_alias,
                       credential_fingerprint, created_at
                FROM credentials ORDER BY credential_id
                """
            ),
            "lanes": self._records(
                """
                SELECT credential_id, model_id, lane_id, available, updated_at
                FROM credential_models ORDER BY credential_id, model_id, lane_id
                """
            ),
            "state": self._records(
                """
                SELECT credential_id, next_eligible_at_ms AS next_eligible_at, disabled, updated_at
                FROM credential_state ORDER BY credential_id
                """
            ),
            "profiles": self._records(
                """
                SELECT provider_id, credential_fingerprint, model_id,
                       profile_version, baseline_seconds, multiplier,
                       local_max_seconds, stable_successes, updated_at
                FROM backoff_profiles
                ORDER BY provider_id, credential_fingerprint, model_id,
                         profile_version
                """
            ),
        }

    def _claim(
        self,
        projection_id: str,
        source_run_id: str,
        source_event_id: str,
        kind: str,
        payload_hash: str,
        applied_at: str,
    ) -> bool:
        expected = (source_run_id, source_event_id, kind, payload_hash)
        by_source = self._conn.execute(
            "SELECT projection_id FROM runtime_projection_events "
            "WHERE source_run_id = ? AND source_event_id = ? AND projection_kind = ?",
            (source_run_id, source_event_id, kind),
        ).fetchone()
        if by_source is not None and by_source["projection_id"] != projection_id:
            raise RuntimeStateError("runtime projection identity is inconsistent")
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO runtime_projection_events VALUES(?,?,?,?,?,?,?,?)",
            (projection_id, source_run_id, source_event_id, kind, "typed-v2", "typed-v2", payload_hash, applied_at),
        )
        row = self._conn.execute(
            """
            SELECT source_run_id, source_event_id, projection_kind, payload_hash
            FROM runtime_projection_events WHERE projection_id = ?
            """,
            (projection_id,),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise ValueError(
                "runtime projection replay differs from immutable record"
            )
        return cursor.rowcount == 1

    def _same_observation(
        self, projection_id: str, expected: tuple[Any, ...], source_run_id: str | None = None, source_event_id: str | None = None
    ) -> None:
        row = self._conn.execute(
            "SELECT * FROM model_observations WHERE observation_id = ?",
            (projection_id,),
        ).fetchone()
        if row is None and source_run_id is not None and source_event_id is not None:
            row = self._conn.execute("""SELECT o.* FROM model_observations o JOIN runtime_projection_events p ON p.projection_id=o.observation_id WHERE p.source_run_id=? AND p.source_event_id=? AND p.projection_kind='model_observation'""", (source_run_id, source_event_id)).fetchone()
            if row is not None:
                actual = tuple(row)[1:]
                if actual != expected[1:]:
                    raise ValueError("runtime projection replay differs from immutable record")
                return
        if row is None:
            raise RuntimeStateError("runtime observation projection is missing")
        if tuple(row) != expected:
            raise ValueError(
                "runtime projection replay differs from immutable record"
            )

    def _records(self, query: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(query)]


def _credential_identity(
    credential_id: str,
    provider_id: str,
    credential_alias: str,
    credential_fingerprint: str | None,
) -> None:
    _provider(provider_id)
    if credential_id != credential_alias:
        raise ValueError("credential id must equal its configured alias")
    credentialless = credential_alias == f"{provider_id}:credentialless"
    credentialed = re.fullmatch(
        rf"{re.escape(provider_id)}:[1-9][0-9]*", credential_alias
    )
    if not credentialless and credentialed is None:
        raise ValueError("credential id must be a configured alias")
    if credentialless and credential_fingerprint is not None:
        raise ValueError("credentialless identity forbids a fingerprint")
    if not credentialless:
        _fingerprint(credential_fingerprint)


def _provider(value: object) -> None:
    if not isinstance(value, str) or _PROVIDER.fullmatch(value) is None:
        raise ValueError("provider_id is invalid")


def _fingerprint(value: object) -> None:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError("credential fingerprint must be SHA-256")


def _text(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(f"{name} must be non-empty trimmed text")


def _optional_text(name: str, value: object) -> None:
    if value is not None:
        _text(name, value)

def _optional_int(name: str, value: object) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer")


def _nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _cooldown_state(payload: dict[str, Any] | None) -> tuple[int, int | None, int | None, int, str] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"next_eligible_at", "last_applied_seconds", "learned_seconds", "post_cooldown_successes", "policy_version"}:
        raise ValueError("cooldown state shape is invalid")
    _nonnegative("next_eligible_at", payload["next_eligible_at"])
    for field in ("last_applied_seconds", "learned_seconds"):
        if payload[field] is not None: _nonnegative(field, payload[field])
    _nonnegative("post_cooldown_successes", payload["post_cooldown_successes"])
    _text("policy_version", payload["policy_version"])
    _reject_secrets(payload)
    return tuple(payload[field] for field in ("next_eligible_at", "last_applied_seconds", "learned_seconds", "post_cooldown_successes", "policy_version"))

def _observation_payload(
    payload: dict[str, Any] | None,
    event_type: str,
    *,
    source_event_id: str | None = None,
    logical_request_id: str | None = None,
    credential_id: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
) -> None:
    if event_type not in {"started", "completed", "failed", "abandoned"}:
        raise ValueError("invalid model observation event type")
    if not isinstance(payload, dict):
        raise ValueError("runtime payload must be an object")
    _reject_secrets(payload)
    if event_type == "started":
        if payload:
            raise ValueError("runtime observation payload shape is invalid")
        return
    base = {
        "technical_result",
        "http_status",
        "latency_ms",
        "retry_cause",
        "answer_hash",
        "retry_after_seconds",
    }
    expected = base | ({"answer"} if event_type == "completed" else set())
    if set(payload) not in {frozenset(expected), frozenset(expected | {"scheduler_control"})}:
        raise ValueError("runtime observation payload shape is invalid")
    if event_type != "completed" and payload["answer_hash"] is not None:
        raise ValueError("runtime observation payload shape is invalid")
    latency = payload["latency_ms"]
    if event_type == "abandoned":
        if latency is not None:
            raise ValueError("abandoned latency_ms is invalid")
    elif isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency < 0:
        raise ValueError("latency_ms is invalid")
    for key in ("answer_hash", "technical_result", "retry_cause"):
        if key in payload and payload[key] is not None:
            _text(key, payload[key])
    if event_type == "completed":
        if (
            payload["technical_result"] != "answer_received"
            or payload["retry_cause"] is not None
            or payload["retry_after_seconds"] is not None
        ):
            raise ValueError("completed terminal semantics are invalid")
        _validate_answer(
            payload["answer"], payload["answer_hash"], logical_request_id
        )
    elif event_type == "failed":
        if (
            payload["technical_result"] not in _FAILURE_TECHNICAL_RESULTS
            or payload["retry_cause"] not in _RETRY_CAUSES - {"lease_expired"}
        ):
            raise ValueError("failed terminal semantics are invalid")
    elif (
        payload["technical_result"] != "not_started"
        or payload["retry_cause"] != "lease_expired"
        or payload["http_status"] is not None
        or payload["retry_after_seconds"] is not None
        or "scheduler_control" in payload
    ):
        raise ValueError("abandoned terminal semantics are invalid")
    if payload["http_status"] is not None and (isinstance(payload["http_status"], bool) or not isinstance(payload["http_status"], int) or not 100 <= payload["http_status"] <= 599):
        raise ValueError("http_status is invalid")
    if "retry_after_seconds" in payload and payload["retry_after_seconds"] is not None:
        _nonnegative("retry_after_seconds", payload["retry_after_seconds"])
        if payload["retry_cause"] != "rate_limited":
            raise ValueError("retry_after_seconds is invalid")
    if "scheduler_control" in payload and payload["scheduler_control"] is not None:
        control = payload["scheduler_control"]
        if not isinstance(control, dict) or set(control) != {"source_event_id", "cooldown", "lane", "profile"}:
            raise ValueError("scheduler_control shape is invalid")
        if source_event_id is not None and control["source_event_id"] != source_event_id:
            raise ValueError("scheduler_control source_event_id is invalid")
        if not any(control[key] is not None for key in ("cooldown", "lane", "profile")):
            raise ValueError("scheduler_control is empty")
        _validate_scheduler_cooldown(control["cooldown"])
        _validate_scheduler_lane(control["lane"])
        _validate_scheduler_profile(control["profile"])
        _validate_scheduler_binding(
            control,
            credential_id=credential_id,
            provider_id=provider_id,
            model_id=model_id,
        )

def _validate_answer(
    answer: object,
    answer_hash: object,
    logical_request_id: str | None,
) -> None:
    if not isinstance(answer, dict):
        raise ValueError("answer shape is invalid")
    request_id = str(answer.get("logical_request_id", ""))
    stages = (
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence", "jury2",
    )
    matched = [stage for stage in stages if f":{stage}:" in request_id]
    stage = matched[0] if len(matched) == 1 else None
    expected = {"decision", "logical_request_id", "payload_fingerprint"}
    if set(answer) != expected:
        raise ValueError("answer shape is invalid")
    _text("logical_request_id", answer["logical_request_id"])
    if logical_request_id is not None and answer["logical_request_id"] != logical_request_id:
        raise ValueError("answer logical request identity is invalid")
    _fingerprint(answer["payload_fingerprint"])
    _fingerprint(answer_hash)
    from core.verify.claim_evidence.domain.fingerprint import answer_fingerprint
    if answer_hash != answer_fingerprint(answer):
        raise ValueError("answer hash is invalid")
    if stage is None:
        raise ValueError("answer stage is invalid")
    from core.infra.db.llm_dispatches import _validate_answer_decision
    decision = answer["decision"]
    if not isinstance(decision, dict):
        raise ValueError("answer decision is invalid")
    canonical = answer
    if "provider_confidence" not in decision:
        if "provider_uncertain" in decision:
            raise ValueError("provider uncertainty is invalid")
        canonical = {
            **answer,
            "decision": {**decision, "provider_confidence": None},
        }
    _validate_answer_decision(canonical, stage)


def _validate_scheduler_cooldown(value: object) -> None:
    if value is None: return
    if not isinstance(value, dict): raise ValueError("scheduler cooldown is invalid")
    common={"credential_id","model_id","next_eligible_at","status","disabled","reason"}
    status=value.get("status")
    expected=common if status == "quarantined" else common | {"payload"}
    if set(value) != expected: raise ValueError("scheduler cooldown shape is invalid")
    _text("credential_id", value["credential_id"]); _text("model_id", value["model_id"])
    if status == "quarantined":
        if value["disabled"] is not True or value["reason"] != "credential_invalid" or value["next_eligible_at"] is not None: raise ValueError("scheduler quarantine is invalid")
    elif status == "cooldown":
        _nonnegative("next_eligible_at", value["next_eligible_at"])
        if value["disabled"] is not False or value["reason"] != "rate_limited": raise ValueError("scheduler cooldown is invalid")
        state = _cooldown_state(value["payload"])
        if state is None or value["next_eligible_at"] != state[0] * 1000:
            raise ValueError("scheduler cooldown eligibility is inconsistent")
    elif status == "cleared":
        if value["disabled"] is not False or value["reason"] is not None or value["next_eligible_at"] is not None: raise ValueError("scheduler cleared is invalid")
        _cooldown_state(value["payload"])
    else: raise ValueError("scheduler cooldown status is invalid")

def _validate_scheduler_lane(value: object) -> None:
    if value is None: return
    if not isinstance(value, dict) or set(value) != {"credential_id","model_id","lane_id","available"}: raise ValueError("scheduler lane shape is invalid")
    for key in ("credential_id","model_id","lane_id"): _text(key, value[key])
    if value["available"] is not False:
        raise ValueError("scheduler lane available is invalid")

def _validate_scheduler_profile(value: object) -> None:
    if value is None: return
    keys={"provider_id","credential_fingerprint","model_id","profile_version","baseline_seconds","multiplier","local_max_seconds","stable_successes"}
    if not isinstance(value, dict) or set(value) != keys: raise ValueError("scheduler profile shape is invalid")
    _provider(value["provider_id"]); _fingerprint(value["credential_fingerprint"]); _text("model_id", value["model_id"]); _text("profile_version", value["profile_version"])
    _nonnegative("baseline_seconds", value["baseline_seconds"]); _positive("multiplier", value["multiplier"]); _nonnegative("local_max_seconds", value["local_max_seconds"]); _nonnegative("stable_successes", value["stable_successes"])
    if value["local_max_seconds"] < value["baseline_seconds"]: raise ValueError("scheduler profile is invalid")


def _validate_scheduler_binding(
    control: dict[str, Any],
    *,
    credential_id: str | None,
    provider_id: str | None,
    model_id: str | None,
) -> None:
    for child_name in ("cooldown", "lane"):
        child = control[child_name]
        if child is not None and (
            credential_id is not None
            and child["credential_id"] != credential_id
            or model_id is not None
            and child["model_id"] != model_id
        ):
            raise ValueError("scheduler control observation binding is invalid")
    profile = control["profile"]
    if profile is not None and (
        provider_id is not None
        and profile["provider_id"] != provider_id
        or model_id is not None
        and profile["model_id"] != model_id
    ):
        raise ValueError("scheduler profile observation binding is invalid")


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _SECRET_FIELDS:
                raise ValueError("raw key material is forbidden")
            _reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)
    elif isinstance(value, str) and value.startswith(_SECRET_PREFIXES):
        raise ValueError("raw key material is forbidden")
