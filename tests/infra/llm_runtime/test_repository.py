# tests/infra/llm_runtime/test_repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json
import sqlite3

import pytest
from core.infra.llm_runtime.repository import LLMRuntimeRepository
from core.verify.claim_evidence.domain.fingerprint import answer_fingerprint

FINGERPRINT = "a" * 64


def _repository(conn=None):
    return LLMRuntimeRepository(conn or sqlite3.connect(":memory:"))


def _register(repo, *, created_at="2026-07-31T00:00:00Z"):
    repo.register_credential(
        credential_id="provider:1",
        provider_id="provider",
        credential_alias="provider:1",
        credential_fingerprint=FINGERPRINT,
        created_at=created_at,
    )


def test_credentials_are_non_secret_stable_cross_run_identities():
    repo = _repository()
    with pytest.raises(ValueError, match="SHA-256"):
        repo.register_credential(
            credential_id="provider:1",
            provider_id="provider",
            credential_alias="provider:1",
            credential_fingerprint=None,
            created_at="t",
        )
    with pytest.raises(ValueError, match="forbids"):
        repo.register_credential(
            credential_id="provider:credentialless",
            provider_id="provider",
            credential_alias="provider:credentialless",
            credential_fingerprint=FINGERPRINT,
            created_at="t",
        )
    _register(repo, created_at="first")
    _register(repo, created_at="later-run")
    assert repo._conn.execute(
        "SELECT created_at FROM credentials"
    ).fetchone()[0] == "first"
    with pytest.raises(ValueError, match="immutable identity"):
        repo.register_credential(
            credential_id="provider:1",
            provider_id="provider",
            credential_alias="provider:1",
            credential_fingerprint="b" * 64,
            created_at="other",
        )


def test_model_observation_replay_is_idempotent_and_secret_free():
    repo = _repository()
    _register(repo)
    answer = {"logical_request_id": "request:support_gate:1", "payload_fingerprint": "a" * 64, "decision": {"source_supports_any": False}}
    values = dict(
        source_run_id="run", source_event_id="event",
        logical_request_id="request:support_gate:1",
        candidate_id=None,
        provider_id="provider",
        model_id="model",
        credential_id="provider:1",
        event_type="completed",
        payload={"technical_result": "answer_received", "http_status": 200, "latency_ms": 3, "retry_cause": None, "retry_after_seconds": None, "answer_hash": answer_fingerprint(answer), "answer": answer},
        created_at="t",
    )
    first, applied = repo.record_model_observation_status(**values)
    assert applied
    assert repo.record_model_observation(**values) == first
    with pytest.raises(ValueError, match="hash is invalid"):
        repo.record_model_observation(
            **{**values, "payload": {**values["payload"], "answer_hash": "c" * 64}}
        )
    with pytest.raises(ValueError, match="raw key"):
        repo.record_model_observation(
            **{**values, "source_event_id": "secret", "payload": {"api_key": "x"}}
        )


def test_cooldown_event_and_state_commit_or_roll_back_together():
    repo = _repository()
    values = dict(
        source_run_id="run", source_event_id="event",
        credential_id="provider:1",
        model_id="model",
        observed_at="2026-07-31T00:00:00Z",
        next_eligible_at=1760000005000,
        status="cooldown",
        disabled=False,
        reason="rate_limited",
        payload={"next_eligible_at": 1760000005, "last_applied_seconds": 5, "learned_seconds": None, "post_cooldown_successes": 0, "policy_version": "v1"},
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.apply_cooldown(**values)
    assert repo._conn.execute(
        "SELECT COUNT(*) FROM runtime_projection_events"
    ).fetchone()[0] == 0
    assert repo._conn.execute(
        "SELECT COUNT(*) FROM cooldown_events"
    ).fetchone()[0] == 0

    _register(repo)
    assert repo.apply_cooldown(**values)
    assert not repo.apply_cooldown(**values)
    state = repo.state_for("provider:1")
    assert state["next_eligible_at"] == 1760000005000
    assert state["disabled"] == 0
    with pytest.raises(ValueError, match="differs"):
        repo.apply_cooldown(
            **{
                **values,
                "next_eligible_at": 1760000010000,
            }
        )
    with pytest.raises(ValueError, match="inconsistent"):
        repo.apply_cooldown(
            **{
                **values,
                "source_event_id": "bad-quarantine",
                "status": "quarantined",
                "disabled": True,
            }
        )
    assert repo._conn.execute(
        "SELECT COUNT(*) FROM cooldown_events"
    ).fetchone()[0] == 1


def test_lane_and_nullable_model_profile_are_mutable_current_projections():
    repo = _repository()
    _register(repo)
    lane = dict(
        credential_id="provider:1",
        model_id="model",
        lane_id="provider:model:provider:1",
    )
    assert repo.apply_lane(
        source_run_id="run", source_event_id="lane-disabled",
        available=False,
        updated_at="t1",
        **lane,
    )
    assert repo.apply_lane(
        source_run_id="run", source_event_id="lane-enabled",
        available=True,
        updated_at="t2",
        **lane,
    )
    assert repo.lane_for(**lane)["available"] == 1

    profile = dict(
        provider_id="provider",
        credential_fingerprint=FINGERPRINT,
        model_id=None,
        profile_version="cooldown-policy-v1",
        baseline_seconds=5,
        multiplier=2,
        local_max_seconds=300,
        stable_successes=0,
    )
    assert repo.apply_backoff_profile(
        source_run_id="run", source_event_id="profile-1", updated_at="t1", **profile
    )
    assert repo.apply_backoff_profile(
        source_run_id="run", source_event_id="profile-2",
        updated_at="t2",
        **{**profile, "baseline_seconds": 10, "stable_successes": 2},
    )
    assert not repo.apply_backoff_profile(
        source_run_id="run", source_event_id="profile-2",
        updated_at="t2",
        **{**profile, "baseline_seconds": 10, "stable_successes": 2},
    )
    learned = repo.profile_for(
        provider_id="provider",
        credential_fingerprint=FINGERPRINT,
        model_id=None,
        profile_version="cooldown-policy-v1",
    )
    assert learned["baseline_seconds"] == 10
    assert learned["stable_successes"] == 2
    assert repo._conn.execute(
        "SELECT COUNT(*) FROM backoff_profiles"
    ).fetchone()[0] == 1


def test_new_connection_sees_quarantine_cooldown_lanes_and_profiles():
    uri = "file:phase2b-runtime-reopen?mode=memory&cache=shared"
    anchor = sqlite3.connect(uri, uri=True)
    first = _repository(anchor)
    _register(first)
    first.apply_cooldown(
        source_run_id="run", source_event_id="cooldown",
        credential_id="provider:1",
        model_id="model",
        observed_at="t1",
        next_eligible_at=1760000005000,
        status="cooldown",
        disabled=False,
        reason="rate_limited",
        payload={"next_eligible_at": 1760000005, "last_applied_seconds": 5, "learned_seconds": None, "post_cooldown_successes": 0, "policy_version": "v1"},
    )
    first.register_credential(
        credential_id="provider:2",
        provider_id="provider",
        credential_alias="provider:2",
        credential_fingerprint="b" * 64,
        created_at="t1",
    )
    first.apply_cooldown(
        source_run_id="run", source_event_id="quarantine",
        credential_id="provider:2",
        model_id="model",
        observed_at="t1",
        next_eligible_at=None,
        status="quarantined",
        disabled=True,
        reason="credential_invalid",
    )
    first.apply_lane(
        source_run_id="run", source_event_id="lane",
        credential_id="provider:1",
        model_id="model",
        lane_id="lane",
        available=False,
        updated_at="t1",
    )
    first.apply_backoff_profile(
        source_run_id="run", source_event_id="profile",
        provider_id="provider",
        credential_fingerprint=FINGERPRINT,
        model_id="model",
        profile_version="v1",
        baseline_seconds=5,
        multiplier=2,
        local_max_seconds=300,
        stable_successes=1,
        updated_at="t1",
    )
    second = _repository(sqlite3.connect(uri, uri=True))
    assert second.state_for("provider:1")["disabled"] == 0
    assert second.state_for("provider:1")["next_eligible_at"] == 1760000005000
    assert second.state_for("provider:2")["disabled"] == 1
    assert second.lane_for(
        credential_id="provider:1", model_id="model", lane_id="lane"
    )["available"] == 0
    assert second.profile_for(
        provider_id="provider",
        credential_fingerprint=FINGERPRINT,
        model_id="model",
        profile_version="v1",
    )["baseline_seconds"] == 5
    rendered = json.dumps(second.debug_snapshot(), sort_keys=True)
    assert "sk-live-sentinel" not in rendered
    assert "payload_json" not in rendered
