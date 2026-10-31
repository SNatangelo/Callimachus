# tests/verify/claim_evidence/adapters/test_runtime_repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import sqlite3

import pytest

from core.infra.db.schema_bootstrap import ensure_schema
from core.infra.db import llm_dispatches
from core.verify.claim_evidence.adapters.runtime_repository import (
    RuntimeRepository,
)
from core.verify.claim_evidence.domain.types import ProviderLane, ProviderPolicy
from core.verify.claim_evidence.domain.fingerprint import answer_fingerprint, payload_fingerprint, FINGERPRINT_VERSION_V2


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _run_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    manuscript = "run manuscript"
    conn.execute(
        "INSERT INTO manuscript_text VALUES(1,?,?,?)",
        (manuscript, _hash(manuscript), len(manuscript)),
    )
    conn.execute(
        "INSERT INTO run VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("test-run", "t", "t", "active", "verify", "input", "a" * 64,
         "standard", None, None, None, None, "b" * 64, None, "fresh"),
    )
    return conn


def _provider_policy():
    return ProviderPolicy(
        "provider", ("provider:1",), (_hash("credential"),), ("model",),
        "seeded_cross_product",
        (ProviderLane("provider:1", _hash("credential"), "model", True, True),),
    )


def _append_dispatch(
    conn,
    *,
    prefix,
    provider,
    model,
    credential_id,
    credential_fingerprint,
    terminal,
    technical_result,
    retry_cause,
    scheduler_control=None,
):
    request_id = f"{prefix}:request:support_gate:1"
    attempt_id = f"{prefix}:attempt"
    hashes = tuple(_hash(f"{prefix}:{index}") for index in range(6))
    payload = {
        "source_spans": [{"span_id": "span-1", "text": "source"}],
        "source_hash": hashes[0],
        "cited_source_mode": "full_text",
        "claim": f"{prefix}:claim",
        "claim_context": "manuscript context",
        "citation_marker": "[1]",
        "task": {"task_id": "support_gate", "instructions": "Question."},
    }
    llm_dispatches.append_logical_request(conn, request={
        "logical_request_id": request_id, "claim_id": f"{prefix}:claim",
        "ref_id": f"{prefix}:ref", "scope": "", "candidate_id": None,
        "candidate_cycle": 1, "stage": "support_gate", "payload": payload,
        "payload_hash": payload_fingerprint(payload, version=FINGERPRINT_VERSION_V2),
        "source_hash": hashes[0], "context_hash": hashes[1], "retrieval_hash": hashes[2],
        "prompt_hash": hashes[3], "model_hash": hashes[4], "policy_hash": hashes[5],
    }, created_at=f"{prefix}:queued")
    lane_id = f"{provider}:{model}:{credential_id}"
    conn.execute(
        "INSERT INTO llm_dispatch_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            request_id,
            provider,
            model,
            credential_id,
            credential_fingerprint,
            lane_id,
            0,
            0,
            _hash(f"{prefix}:selection"),
            _hash(f"{prefix}:pacing"),
            None,
            None,
            None,
            0,
            0,
            f"{prefix}:queued",
            f"{prefix}:leased",
            f"{prefix}:created",
        ),
    )
    llm_dispatches.append_dispatch_event(conn, event_id=f"{attempt_id}:started", logical_request_id=request_id, dispatch_attempt_id=attempt_id, event_type="started", payload={}, created_at=f"{prefix}:started-at")
    payload = {
        "technical_result": technical_result,
        "http_status": 429 if retry_cause == "rate_limited" else 200,
        "latency_ms": 8,
        "retry_cause": retry_cause,
        "retry_after_seconds": None,
        "answer_hash": _hash(f"{prefix}:answer")
        if terminal == "completed"
        else None,
    }
    if terminal == "completed":
        answer = {"logical_request_id": request_id, "payload_fingerprint": conn.execute("SELECT prompt_hash FROM llm_logical_requests WHERE logical_request_id=?", (request_id,)).fetchone()[0], "decision": {"source_supports_any": False, "provider_confidence": None}}
        payload["answer"] = answer
        payload["answer_hash"] = answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2)
    if scheduler_control is not None:
        payload["scheduler_control"] = scheduler_control
    terminal_id = f"{attempt_id}:terminal"
    llm_dispatches.append_dispatch_event(conn, event_id=terminal_id, logical_request_id=request_id, dispatch_attempt_id=attempt_id, event_type=terminal, payload=payload, created_at=f"{prefix}:terminal-at")
    return terminal_id, lane_id


def test_replay_is_idempotent_secret_free_and_does_not_mutate_run():
    run = _run_connection()
    rate_event, lane_id = _append_dispatch(
        run,
        prefix="rate",
        provider="provider",
        model="model",
        credential_id="provider:1",
        credential_fingerprint=_hash("credential"),
        terminal="failed",
        technical_result="rate_limited",
        retry_cause="rate_limited",
    )
    _append_dispatch(
        run,
        prefix="local",
        provider="local",
        model="model",
        credential_id="local:credentialless",
        credential_fingerprint=None,
        terminal="completed",
        technical_result="answer_received",
        retry_cause=None,
    )
    before = run.total_changes
    runtime_conn = sqlite3.connect(":memory:")
    adapter = RuntimeRepository(run, runtime_conn=runtime_conn)

    assert adapter.replay_dispatch_observations() == 4
    assert adapter.replay_dispatch_observations() == 0
    assert run.total_changes == before
    assert runtime_conn.execute(
        "SELECT COUNT(*) FROM model_observations"
    ).fetchone()[0] == 4
    credentials = runtime_conn.execute(
        """
        SELECT credential_id, credential_fingerprint
        FROM credentials ORDER BY credential_id
        """
    ).fetchall()
    assert [tuple(row) for row in credentials] == [
        ("local:credentialless", None),
        ("provider:1", _hash("credential")),
    ]

    assert adapter.project_lane(
        run_event_id=rate_event,
        credential_id="provider:1",
        model_id="model",
        lane_id=lane_id,
        available=True,
    )
    cooldown = dict(
        run_event_id=rate_event,
        credential_id="provider:1",
        model_id="model",
        next_eligible_at=1760000005000,
        status="cooldown",
        disabled=False,
        reason="rate_limited",
        payload={"next_eligible_at": 1760000005, "last_applied_seconds": 5, "learned_seconds": None, "post_cooldown_successes": 0, "policy_version": "v1"},
    )
    assert adapter.project_cooldown(**cooldown)
    assert not adapter.project_cooldown(**cooldown)
    profile = dict(
        run_event_id=rate_event,
        provider_id="provider",
        credential_fingerprint=_hash("credential"),
        model_id="model",
        profile_version="cooldown-policy-v1",
        baseline_seconds=5,
        multiplier=2,
        local_max_seconds=300,
        stable_successes=0,
    )
    assert adapter.project_backoff_profile(**profile)
    assert not adapter.project_backoff_profile(**profile)
    assert run.total_changes == before


def test_replay_scheduler_controls_disables_credential_invalid_alias_idempotently(
    tmp_path,
):
    run = _run_connection()
    control = {
        "source_event_id": "bad:attempt:terminal",
        "cooldown": {
            "credential_id": "provider:1",
            "model_id": "model",
            "next_eligible_at": None,
            "status": "quarantined",
            "disabled": True,
            "reason": "credential_invalid",
        },
        "lane": None,
        "profile": None,
    }
    _append_dispatch(
        run,
        prefix="bad",
        provider="provider",
        model="model",
        credential_id="provider:1",
        credential_fingerprint=_hash("credential"),
        terminal="failed",
        technical_result="credential_invalid",
        retry_cause="credential_invalid",
        scheduler_control=control,
    )
    runtime_path = tmp_path / "runtime.sqlite"
    runtime_conn = sqlite3.connect(runtime_path)
    adapter = RuntimeRepository(run, runtime_conn=runtime_conn)
    try:
        assert adapter.replay_scheduler_controls() == 1
        assert adapter.replay_scheduler_controls() == 0
        assert adapter.scheduler_controls()["disabled_credentials"] == frozenset(
            {("provider", "provider:1")}
        )
        state = runtime_conn.execute(
            """
            SELECT next_eligible_at_ms AS next_eligible_at, disabled, reason
            FROM credential_state
            WHERE credential_id = ?
            """,
            ("provider:1",),
        ).fetchone()
        assert state["disabled"] == 1
        assert state["reason"] == "credential_invalid"
        assert state["next_eligible_at"] is None
        credentials = runtime_conn.execute(
            """
            SELECT credential_id, provider_id, credential_alias,
                   credential_fingerprint
            FROM credentials ORDER BY credential_id
            """
        ).fetchall()
        assert [tuple(row) for row in credentials] == [
            ("provider:1", "provider", "provider:1", _hash("credential"))
        ]
    finally:
        adapter.close()
    reopened = RuntimeRepository(
        _run_connection(), runtime_conn=sqlite3.connect(runtime_path)
    )
    try:
        assert reopened.scheduler_controls()["disabled_credentials"] == frozenset(
            {("provider", "provider:1")}
        )
        assert reopened.replay_scheduler_controls() == 0
    finally:
        reopened.close()


def test_control_projection_must_match_an_authoritative_terminal_event():
    run = _run_connection()
    terminal_id, lane_id = _append_dispatch(
        run,
        prefix="lane",
        provider="provider",
        model="model",
        credential_id="provider:1",
        credential_fingerprint=_hash("credential"),
        terminal="failed",
        technical_result="lane_unavailable",
        retry_cause="lane_unavailable",
    )
    adapter = RuntimeRepository(
        run, runtime_conn=sqlite3.connect(":memory:")
    )
    with pytest.raises(ValueError, match="authoritative"):
        adapter.project_lane(
            run_event_id=terminal_id,
            credential_id="provider:2",
            model_id="model",
            lane_id=lane_id,
            available=False,
        )
    with pytest.raises(ValueError, match="terminal"):
        adapter.project_lane(
            run_event_id="lane:started",
            credential_id="provider:1",
            model_id="model",
            lane_id=lane_id,
            available=False,
        )


def test_scheduler_state_uses_started_queued_at_for_pacing_and_last_cursor():
    adapter = RuntimeRepository(runtime_conn=sqlite3.connect(":memory:"))
    snapshot = {
        "dispatch_attempts": [
            {
                "dispatch_attempt_id": "p:1",
                "provider_id": "p",
                "model_id": "m1",
                "credential_cursor": 1,
                "model_draw_index": 10,
                "queued_at": 100,
                "next_eligible_at": 1_000,
            },
            {
                "dispatch_attempt_id": "p:2",
                "provider_id": "p",
                "model_id": "m1",
                "credential_cursor": 2,
                "model_draw_index": 11,
                "queued_at": 250,
                "next_eligible_at": 2_000,
            },
            {
                "dispatch_attempt_id": "p:3",
                "provider_id": "p",
                "model_id": "m2",
                "credential_cursor": 3,
                "model_draw_index": 12,
                "queued_at": 400,
                "next_eligible_at": 3_000,
            },
            {
                "dispatch_attempt_id": "p:4",
                "provider_id": "p",
                "model_id": "m2",
                "credential_cursor": 9,
                "model_draw_index": 19,
                "queued_at": 999,
                "next_eligible_at": 4_000,
            },
        ],
        "dispatch_events": [
            {"dispatch_attempt_id": "p:1", "event_type": "started"},
            {"dispatch_attempt_id": "p:2", "event_type": "started"},
            {"dispatch_attempt_id": "p:3", "event_type": "started"},
            {"dispatch_attempt_id": "p:4", "event_type": "leased"},
        ],
    }
    snapshot["dispatch_attempts"] = [
        snapshot["dispatch_attempts"][index] for index in (2, 0, 3, 1)
    ]
    try:
        state = adapter.scheduler_state(snapshot, (_provider_policy(),))
        assert state.selection.credential_cursor == 9
        assert state.selection.model_draw_index == 19
        assert state.pacing.last_global_start_ms == 400
        assert state.pacing.last_model_starts_ms == (("p:m1", 250), ("p:m2", 400))
        assert state.key_eligibility_ms == ()
        assert state.disabled_credentials == frozenset()
        assert state.disabled_lanes == frozenset()
        assert state.dispatch_counter == 4
    finally:
        adapter.close()


def test_scheduler_state_excludes_foreign_controls_and_preserves_active_controls(
    monkeypatch,
):
    adapter = RuntimeRepository(runtime_conn=sqlite3.connect(":memory:"))
    controls = {
        "disabled_credentials": frozenset({
            ("provider", "provider:1"), ("foreign", "foreign:1"),
        }),
        "disabled_lanes": frozenset({
            ("provider", "provider:1", "model"),
            ("foreign", "foreign:1", "foreign-model"),
        }),
        "key_eligibility_ms": (
            (("provider", "provider:1"), 100),
            (("foreign", "foreign:1"), 200),
        ),
        "cooldowns": {
            ("provider", "provider:1", "model"): {
                "next_eligible_at": 100,
                "last_applied_seconds": 1,
                "learned_seconds": 1,
                "post_cooldown_successes": 0,
                "policy_version": "cooldown-policy-v1",
            },
            ("foreign", "foreign:1", "foreign-model"): {
                "next_eligible_at": 200,
                "last_applied_seconds": 1,
                "learned_seconds": 1,
                "post_cooldown_successes": 0,
                "policy_version": "cooldown-policy-v1",
            },
        },
    }
    monkeypatch.setattr(adapter, "scheduler_controls", lambda: controls)
    try:
        state = adapter.scheduler_state(
            {"dispatch_attempts": [], "dispatch_events": []},
            (_provider_policy(),),
        )
        assert state.disabled_credentials == frozenset({("provider", "provider:1")})
        assert state.disabled_lanes == frozenset({("provider", "provider:1", "model")})
        assert state.key_eligibility_ms == ((("provider", "provider:1"), 100),)
        assert state.cooldowns[0][0] == ("provider", "provider:1", "model")
    finally:
        adapter.close()
