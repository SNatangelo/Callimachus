# tests/infra/llm_runtime/test_runtime_v2.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import sqlite3
import json
import pytest

from core.infra.llm_runtime.repository import LLMRuntimeRepository
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    answer_fingerprint,
)


def test_v2_has_queryable_identity_no_json_payload_and_rejects_bad_runtime_forms():
    conn = sqlite3.connect(":memory:")
    repo = LLMRuntimeRepository(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runtime_projection_events)")}
    assert {"source_run_id", "source_event_id"} <= columns
    assert not any("payload_json" in row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
    repo.register_credential(credential_id="p:1", provider_id="p", credential_alias="p:1", credential_fingerprint="a" * 64, created_at="t")
    with pytest.raises(ValueError, match="source_event_id"):
        repo.apply_lane(source_run_id="run", source_event_id=" ", credential_id="p:1", model_id="m", lane_id="l", available=True, updated_at="t")
    with pytest.raises(ValueError, match="non-negative integer"):
        repo.apply_cooldown(source_run_id="run", source_event_id="event", credential_id="p:1", model_id="m", observed_at="t", next_eligible_at="late", status="cooldown", disabled=False, reason="rate_limited", payload=None)
    with pytest.raises(ValueError, match="state is required"):
        repo.apply_cooldown(source_run_id="run", source_event_id="event", credential_id="p:1", model_id="m", observed_at="t", next_eligible_at=1, status="cooldown", disabled=False, reason="rate_limited", payload=None)
    with pytest.raises(ValueError, match="event type"):
        repo.record_model_observation(source_run_id="run", source_event_id="event", logical_request_id="r", candidate_id=None, provider_id="p", model_id="m", credential_id="p:1", event_type="unknown", payload={}, created_at="t")


def test_completed_jury_answer_hash_and_scheduler_control_are_fail_closed():
    repo = LLMRuntimeRepository(sqlite3.connect(":memory:"))
    repo.register_credential(credential_id="p:1", provider_id="p", credential_alias="p:1", credential_fingerprint="a" * 64, created_at="t")
    payload = {"technical_result":"answer_received","http_status":200,"latency_ms":1.5,"retry_cause":None,"answer_hash":"0" * 64,"answer":{},"scheduler_control":{"source_event_id":"wrong","cooldown":None,"lane":None,"profile":None}}
    with pytest.raises(ValueError):
        repo.record_model_observation(source_run_id="run", source_event_id="event", logical_request_id="r", candidate_id=None, provider_id="p", model_id="m", credential_id="p:1", event_type="completed", payload=payload, created_at="t")


def test_valid_jury2_completed_answer_is_accepted():
    repo = LLMRuntimeRepository(sqlite3.connect(":memory:"))
    repo.register_credential(credential_id="p:1", provider_id="p", credential_alias="p:1", credential_fingerprint="a" * 64, created_at="t")
    answer = {"logical_request_id":"r:jury2:1","payload_fingerprint":"b" * 64,"decision":{"passages_fit_claim":True,"reason":"ok"}}
    payload = {"technical_result":"answer_received","http_status":200,"latency_ms":1.0,"retry_cause":None,"retry_after_seconds":None,"answer":answer,"answer_hash":answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2)}
    assert repo.record_model_observation_status(source_run_id="run",source_event_id="event",logical_request_id="r:jury2:1",candidate_id=None,provider_id="p",model_id="m",credential_id="p:1",event_type="completed",payload=payload,created_at="t")[1]


def test_valid_support_gate_answer_is_accepted_and_cross_fields_fail_closed():
    repo = LLMRuntimeRepository(sqlite3.connect(":memory:"))
    repo.register_credential(credential_id="p:1", provider_id="p", credential_alias="p:1", credential_fingerprint="a" * 64, created_at="t")
    answer = {
        "logical_request_id": "r:support_gate:1",
        "payload_fingerprint": "b" * 64,
        "decision": {"source_supports_any": True},
    }
    payload = {
        "technical_result": "answer_received",
        "http_status": 200,
        "latency_ms": 1.0,
        "retry_cause": None,
        "retry_after_seconds": None,
        "answer": answer,
        "answer_hash": answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2),
    }
    assert repo.record_model_observation_status(
        source_run_id="run", source_event_id="valid", logical_request_id="r:support_gate:1",
        candidate_id=None, provider_id="p", model_id="m", credential_id="p:1",
        event_type="completed", payload=payload, created_at="t",
    )[1]
    invalid = json.loads(json.dumps(payload))
    invalid["answer"]["decision"]["source_span_ids"] = ["span-1"]
    invalid["answer_hash"] = answer_fingerprint(
        invalid["answer"], version=FINGERPRINT_VERSION_V2
    )
    with pytest.raises(ValueError):
        repo.record_model_observation(
            source_run_id="run", source_event_id="invalid",
            logical_request_id="r:support_gate:1", candidate_id=None, provider_id="p",
            model_id="m", credential_id="p:1", event_type="completed",
            payload=invalid, created_at="t",
        )


@pytest.mark.parametrize("child", [
    ("cooldown", {"credential_id":"p:1","model_id":"m","next_eligible_at":None,"status":"quarantined","disabled":True,"reason":"credential_invalid"}),
    ("cooldown", {"credential_id":"p:1","model_id":"m","next_eligible_at":2000,"status":"cooldown","disabled":False,"reason":"rate_limited","payload":{"next_eligible_at":2,"last_applied_seconds":1,"learned_seconds":None,"post_cooldown_successes":0,"policy_version":"v"}}),
    ("cooldown", {"credential_id":"p:1","model_id":"m","next_eligible_at":None,"status":"cleared","disabled":False,"reason":None,"payload":{"next_eligible_at":0,"last_applied_seconds":1,"learned_seconds":None,"post_cooldown_successes":1,"policy_version":"v"}}),
    ("lane", {"credential_id":"p:1","model_id":"m","lane_id":"l","available":False}),
    ("profile", {"provider_id":"p","credential_fingerprint":"a" * 64,"model_id":"m","profile_version":"v","baseline_seconds":1,"multiplier":2,"local_max_seconds":2,"stable_successes":0}),
])
def test_scheduler_controls_validate_deep_children(child):
    repo=LLMRuntimeRepository(sqlite3.connect(":memory:")); repo.register_credential(credential_id="p:1",provider_id="p",credential_alias="p:1",credential_fingerprint="a" * 64,created_at="t")
    key,value=child; control={"source_event_id":"event","cooldown":None,"lane":None,"profile":None}; control[key]=value
    payload={"technical_result":"provider_failure","http_status":500,"latency_ms":1,"retry_cause":"provider_failure","retry_after_seconds":None,"answer_hash":None,"scheduler_control":control}
    assert repo.record_model_observation_status(source_run_id="run",source_event_id="event",logical_request_id="r",candidate_id=None,provider_id="p",model_id="m",credential_id="p:1",event_type="failed",payload=payload,created_at="t")[1]
    payload["scheduler_control"][key] = {**value, "extra": 1}
    with pytest.raises(ValueError): repo.record_model_observation(source_run_id="run",source_event_id="other",logical_request_id="r",candidate_id=None,provider_id="p",model_id="m",credential_id="p:1",event_type="failed",payload=payload,created_at="t")


@pytest.mark.parametrize(
    "event_type,payload",
    [
        (
            "failed",
            {"technical_result":"provider_failure","http_status":500,"latency_ms":1,"retry_cause":None,"retry_after_seconds":None,"answer_hash":None},
        ),
        (
            "abandoned",
            {"technical_result":"provider_failure","http_status":None,"latency_ms":None,"retry_cause":"lease_expired","retry_after_seconds":None,"answer_hash":None},
        ),
        (
            "abandoned",
            {"technical_result":"not_started","http_status":None,"latency_ms":0,"retry_cause":"lease_expired","retry_after_seconds":None,"answer_hash":None},
        ),
    ],
)
def test_terminal_semantics_reject_invalid_failed_and_abandoned(event_type, payload):
    repo = LLMRuntimeRepository(sqlite3.connect(":memory:"))
    repo.register_credential(credential_id="p:1",provider_id="p",credential_alias="p:1",credential_fingerprint="a" * 64,created_at="t")
    with pytest.raises(ValueError, match="terminal|latency"):
        repo.record_model_observation(
            source_run_id="run", source_event_id="event", logical_request_id="r",
            candidate_id=None, provider_id="p", model_id="m", credential_id="p:1",
            event_type=event_type, payload=payload, created_at="t",
        )


def test_scheduler_control_rejects_inconsistent_values_and_observation_binding():
    repo = LLMRuntimeRepository(sqlite3.connect(":memory:"))
    repo.register_credential(credential_id="p:1",provider_id="p",credential_alias="p:1",credential_fingerprint="a" * 64,created_at="t")
    base = {"technical_result":"lane_unavailable","http_status":None,"latency_ms":1,"retry_cause":"lane_unavailable","retry_after_seconds":None,"answer_hash":None}
    bad_controls = (
        {"source_event_id":"event","cooldown":None,"lane":{"credential_id":"p:1","model_id":"m","lane_id":"l","available":True},"profile":None},
        {"source_event_id":"event","cooldown":{"credential_id":"p:1","model_id":"m","next_eligible_at":1000,"status":"cooldown","disabled":False,"reason":"rate_limited","payload":{"next_eligible_at":2,"last_applied_seconds":1,"learned_seconds":None,"post_cooldown_successes":0,"policy_version":"v"}},"lane":None,"profile":None},
        {"source_event_id":"event","cooldown":None,"lane":{"credential_id":"p:2","model_id":"m","lane_id":"l","available":False},"profile":None},
    )
    for index, control in enumerate(bad_controls):
        with pytest.raises(ValueError):
            repo.record_model_observation(
                source_run_id="run", source_event_id="event", logical_request_id="r",
                candidate_id=None, provider_id="p", model_id="m", credential_id="p:1",
                event_type="failed", payload={**base, "scheduler_control": control},
                created_at=f"t{index}",
            )
