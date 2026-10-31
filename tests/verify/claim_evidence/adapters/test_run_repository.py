# tests/verify/claim_evidence/adapters/test_run_repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.infra.db.schema_bootstrap import ensure_schema
from core.infra.db import verification_candidates
from core.infra.db.repository import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.verify.claim_evidence.adapters.run_repository import (
    ClaimEvidenceRunRepository,
)
from core.verify.claim_evidence.domain.fingerprint import (
    candidate_fingerprint,
    answer_fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
    FINGERPRINT_VERSION_V2,
)
from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_PROMPT_SPEC,
    JURY1_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.contracts.jury2 import JURY2_SYSTEM_PROMPT


HASH_FIELDS = (
    "source_hash",
    "context_hash",
    "retrieval_hash",
    "prompt_hash",
    "model_hash",
    "policy_hash",
)


def _hash(label):
    return hashlib.sha256(label.encode()).hexdigest()


def _adapter():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    manuscript = "manuscript"
    conn.execute(
        "INSERT INTO manuscript_text VALUES(1,?,?,?)",
        (manuscript, _hash(manuscript), len(manuscript)),
    )
    return ClaimEvidenceRunRepository(
        type("Repo", (), {"_conn": conn, "_schema_version": SCHEMA_VERSION})()
    ), conn


def test_fingerprint_version_accepts_current_schema_and_rejects_future_schema():
    adapter, conn = _adapter()
    adapter._repository._schema_version = SCHEMA_VERSION
    assert adapter.fingerprint_version() == FINGERPRINT_VERSION_V2

    adapter._repository._schema_version = SCHEMA_VERSION - 1
    with pytest.raises(ValueError, match="unsupported verification ledger schema"):
        adapter.fingerprint_version()

    adapter._repository._schema_version = SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="unsupported verification ledger schema"):
        adapter.fingerprint_version()

    conn.close()


def test_fingerprint_version_waits_for_shared_connection_lock():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    adapter = ClaimEvidenceRunRepository(
        type("Repo", (), {"_conn": conn, "_schema_version": SCHEMA_VERSION})()
    )
    started = threading.Event()
    completed = threading.Event()

    def fingerprint() -> str:
        started.set()
        version = adapter.fingerprint_version()
        completed.set()
        return version

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with adapter._lock:
            future = executor.submit(fingerprint)
            assert started.wait(timeout=1)
            assert not completed.is_set()
        assert future.result(timeout=1) == FINGERPRINT_VERSION_V2
        assert completed.is_set()
    finally:
        executor.shutdown()
        conn.close()


def _request(
    identifier="request-1",
    *,
    stage="support_gate",
    candidate_id=None,
    candidate_cycle=1,
):
    if stage == "jury2":
        payload = {
            "asserted_relation": "basis",
            "passage_subject": "the source-backed role",
            "selected_passages": [
                {"span_id": "span-1", "text": "quoted evidence"}
            ],
        }
    else:
        payload = {
            "source_spans": [{"span_id": "span-1", "text": "quoted evidence"}],
            "source_hash": _hash("source_hash"),
            "cited_source_mode": "full_text",
            "claim": "c",
            "claim_context": "manuscript context",
            "citation_marker": "[1]",
            "task": {
                "task_id": stage,
                "instructions": JURY1_PROMPT_SPEC.task_prompt(stage),
            },
        }
        if stage == "explanation_evidence":
            payload["determined_outcome"] = "partial"
    request = {
        "logical_request_id": identifier,
        "claim_id": "claim-1",
        "ref_id": "ref-1",
        "scope": "",
        "candidate_id": candidate_id,
        "candidate_cycle": candidate_cycle,
        "stage": stage,
        "payload": payload,
        "payload_hash": payload_fingerprint(payload, version=FINGERPRINT_VERSION_V2),
    }
    request.update({name: _hash(name) for name in HASH_FIELDS})
    request["prompt_hash"] = provider_prompt_fingerprint(
        JURY1_SYSTEM_PROMPT if stage != "jury2" else JURY2_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    return request


def _record():
    record = {
        "outcome": "supports",
        "explanation": "because",
        "evidence": ["quoted evidence"],
        "outcome_fields": {"claim_hash": _hash("c"), "source_hash": _hash("source_hash"), "supported_part": "the source-backed role", "incompatible_proposition": None, "reason": None, "provider_confidence": None},
        "grounded": [{"text": "quoted evidence", "span_id": "span-1", "raw_start": 0, "raw_end": 15, "source_hash": _hash("source_hash"), "match_mode": "exact_raw", "score": 1.0}],
    }
    record.update({name: _hash(name) for name in HASH_FIELDS})
    record["prompt_hash"] = _request()["prompt_hash"]
    record["fingerprint"] = candidate_fingerprint(
        outcome=record["outcome"],
        evidence=tuple(record["evidence"]),
        outcome_fields=record["outcome_fields"],
        grounded=tuple({name: item[name] for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")} for item in record["grounded"]), version=FINGERPRINT_VERSION_V2,
    )
    return record


def _append_candidate(adapter):
    adapter.append_candidate(
        candidate_id="candidate-1",
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        candidate_cycle=1,
        origin_logical_request_id="request-1",
        record=_record(),
    )


def test_probability_only_non_decidable_keeps_structural_reason_without_rationale():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request())
    record = _record()
    record.update({
        "outcome": "non_decidable",
        "explanation": None,
        "evidence": [],
        "grounded": [],
    })
    record["outcome_fields"].update({
        "supported_part": None,
        "reason": "no_consensus",
        "provider_confidence": 0.86,
    })
    record["fingerprint"] = candidate_fingerprint(
        outcome=record["outcome"],
        evidence=(),
        outcome_fields=record["outcome_fields"],
        grounded=(),
        version=FINGERPRINT_VERSION_V2,
    )

    adapter.append_candidate(
        candidate_id="candidate-1",
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        candidate_cycle=1,
        origin_logical_request_id="request-1",
        record=record,
    )

    row = conn.execute(
        "SELECT explanation,provider_confidence,non_decidable_reason "
        "FROM verification_candidates WHERE candidate_id='candidate-1'"
    ).fetchone()
    assert tuple(row) == (None, 0.86, "no_consensus")
    conn.close()


def _attempt(identifier, *, logical_request_id="request-1", stage="support_gate"):
    return {
        "dispatch_attempt_id": identifier,
        "logical_request_id": logical_request_id,
        "payload_hash": _request(logical_request_id, stage=stage)["payload_hash"],
        "provider_id": "provider",
        "model_id": "model",
        "credential_id": "provider:1",
        "credential_fingerprint": _hash("credential"),
        "lane_id": "provider:model:provider:1",
        "credential_cursor": 0,
        "model_draw_index": 0,
        "selection_hash": _hash("selection"),
        "pacing_hash": _hash("pacing"),
        "global_interval_ms": 0,
        "model_interval_ms": 0,
    }


@pytest.mark.parametrize(
    ("stage", "decision", "detail_table"),
    [
        ("support_gate", {"source_supports_any": True, "provider_confidence": None}, "llm_dispatch_support_gate_answers"),
        ("full_support_gate", {"source_supports_fully": True, "provider_confidence": None}, "llm_dispatch_full_support_gate_answers"),
        ("contrary_gate", {"paper_demonstrates_opposite": False, "provider_confidence": None}, "llm_dispatch_contrary_gate_answers"),
        ("topic_gate", {"same_specific_subject": True, "provider_confidence": None}, "llm_dispatch_topic_gate_answers"),
        (
            "explanation_evidence",
            {
                "reason": "The source supports only the method premise.",
                "supported_content": "the method premise",
                "unsupported_content": "the broader conclusion",
                "incompatible_proposition": None,
                "evidence_span_ids": ["span-1"],
                "non_decidable_reason": None,
                "provider_confidence": None,
            },
            "llm_dispatch_explanation_evidence_answers",
        ),
    ],
)
def test_v10_jury1_requests_persist_exact_stage_payloads_and_typed_decisions(
    stage, decision, detail_table,
):
    adapter, conn = _adapter()
    request = _request(f"request-{stage}", stage=stage)
    adapter.append_logical_request(request)
    stored = adapter.resume_snapshot()["logical_requests"][0]["payload"]

    assert stored["claim"] == "c"
    assert stored["claim_context"] == "manuscript context"
    assert stored["citation_marker"] == "[1]"
    assert stored["task"]["task_id"] == stage
    assert "citation_scope" not in stored
    assert "targets" not in stored
    assert "coverage" not in stored
    if stage == "explanation_evidence":
        assert stored["determined_outcome"] == "partial"

    attempt = _attempt(
        f"dispatch-{stage}",
        logical_request_id=request["logical_request_id"],
        stage=stage,
    )
    adapter.lease_dispatch(attempt)
    adapter.start_dispatch(
        logical_request_id=request["logical_request_id"],
        dispatch_attempt_id=attempt["dispatch_attempt_id"],
    )
    answer = {
        "logical_request_id": request["logical_request_id"],
        "payload_fingerprint": request["prompt_hash"],
        "decision": decision,
    }
    adapter.finish_dispatch(
        logical_request_id=request["logical_request_id"],
        dispatch_attempt_id=attempt["dispatch_attempt_id"],
        result="completed",
        technical_result="answer_received",
        http_status=200,
        latency_ms=1,
        answer=answer,
        answer_hash=answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2),
    )
    assert conn.execute(f"SELECT COUNT(*) FROM {detail_table}").fetchone()[0] == 1


def test_request_candidate_and_events_are_immutable_and_linked():
    adapter, _ = _adapter()
    request = _request()
    adapter.append_logical_request(request)
    adapter.append_logical_request(request)
    _append_candidate(adapter)
    _append_candidate(adapter)
    adapter.append_candidate_event(
        event_id="candidate-1:guard",
        candidate_id="candidate-1",
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    adapter.append_candidate_event(
        event_id="candidate-1:guard",
        candidate_id="candidate-1",
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    with pytest.raises(ValueError, match="event replay differs"):
        adapter.append_candidate_event(
            event_id="candidate-1:guard",
            candidate_id="candidate-1",
            event_type="guard_rejected",
            payload={"guard_result": {"accepted": False}, "cause": "grounding_invalid"},
        )
    jury2 = _request(
        "request-jury2",
        stage="jury2",
        candidate_id="candidate-1",
    )
    assert jury2["payload"] == {
        "asserted_relation": "basis",
        "passage_subject": "the source-backed role",
        "selected_passages": [{"span_id": "span-1", "text": "quoted evidence"}],
    }
    adapter.append_logical_request(jury2)
    jury2["candidate_cycle"] = 2
    with pytest.raises(ValueError, match="candidate identity"):
        adapter.append_logical_request(jury2)


def test_candidate_events_use_typed_details_with_exact_payloads():
    event_specs = (
        ("guard-accepted", "guard_accepted", {"guard_result": {"accepted": True}, "cause": None}, None),
        ("guard-rejected", "guard_rejected", {"guard_result": {"accepted": False}, "cause": "grounding_invalid"}, "verification_candidate_guard_rejections"),
        ("jury2-yes", "jury2_yes", {"answer": True, "reason": "yes", "provider_confidence": None}, "verification_candidate_jury2_decisions"),
        ("jury2-no", "jury2_no", {"answer": False, "reason": "no", "provider_confidence": None}, "verification_candidate_jury2_decisions"),
        ("jury2-tech", "jury2_technical_exhausted", {"failure_cause": "timeout"}, "verification_candidate_jury2_technical_failures"),
        ("requeued", "requeued", {"cause": "jury2_rejected"}, "verification_candidate_requeues"),
        ("requeued-uncertain", "requeued", {"cause": "jury2_provider_uncertain"}, "verification_candidate_requeues"),
        ("terminal", "terminal", {"resolution": "jury2_accepted", "assurance": "passed"}, "verification_candidate_terminals"),
    )
    for event_id, event_type, partial_payload, detail_table in event_specs:
        adapter, conn = _adapter()
        adapter.append_logical_request(_request())
        _append_candidate(adapter)
        jury2 = _request("request-jury2", stage="jury2", candidate_id="candidate-1")
        adapter.append_logical_request(jury2)
        payload = dict(partial_payload)
        if event_type.startswith("jury2_"):
            payload.update(
                logical_request_id="request-jury2",
                jury2_payload_hash=jury2["payload_hash"],
            )
        adapter.append_candidate_event(event_id=event_id, candidate_id="candidate-1", event_type=event_type, payload=payload)
        adapter.append_candidate_event(event_id=event_id, candidate_id="candidate-1", event_type=event_type, payload=payload)
        assert "payload_json" not in {
            row["name"]
            for row in conn.execute("PRAGMA table_info(verification_candidate_events)")
        }
        event = adapter.resume_snapshot()["candidate_events"][0]
        assert event["event_type"] == event_type
        assert event["payload"] == payload
        if detail_table is not None:
            assert conn.execute(f"SELECT COUNT(*) FROM {detail_table}").fetchone()[0] == 1
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(f"DELETE FROM {detail_table}")

    adapter, _ = _adapter()
    adapter.append_logical_request(_request())
    _append_candidate(adapter)
    jury2 = _request("request-jury2", stage="jury2", candidate_id="candidate-1")
    adapter.append_logical_request(jury2)
    with pytest.raises(ValueError, match="requeue cause"):
        adapter.append_candidate_event(event_id="invalid", candidate_id="candidate-1", event_type="requeued", payload={"cause": "jury2_rejected", "extra": True})
    with pytest.raises(ValueError, match="technical cause"):
        adapter.append_candidate_event(event_id="invalid-cause", candidate_id="candidate-1", event_type="jury2_technical_exhausted", payload={"logical_request_id": "request-jury2", "jury2_payload_hash": jury2["payload_hash"], "failure_cause": "unknown"})


def test_candidate_event_writer_rejects_conflicts_and_jury2_link_mismatches():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request())
    _append_candidate(adapter)
    jury2 = _request("request-jury2", stage="jury2", candidate_id="candidate-1")
    adapter.append_logical_request(jury2)
    adapter.append_candidate_event(
        event_id="guard-accepted",
        candidate_id="candidate-1",
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    with pytest.raises(ValueError, match="events conflict"):
        adapter.append_candidate_event(
            event_id="guard-rejected",
            candidate_id="candidate-1",
            event_type="guard_rejected",
            payload={
                "guard_result": {"accepted": False},
                "cause": "grounding_invalid",
            },
        )
    with pytest.raises(ValueError, match="request linkage"):
        adapter.append_candidate_event(
            event_id="jury2-wrong-hash",
            candidate_id="candidate-1",
            event_type="jury2_yes",
            payload={
                "logical_request_id": "request-jury2",
                "jury2_payload_hash": _hash("wrong"),
            "answer": True,
            "reason": "yes",
            "provider_confidence": None,
        },
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_candidate_events"
    ).fetchone()[0] == 1


def test_candidate_event_reader_rejects_missing_or_wrong_typed_detail():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request())
    _append_candidate(adapter)
    adapter.append_candidate_event(
        event_id="guard", candidate_id="candidate-1", event_type="guard_rejected",
        payload={"guard_result": {"accepted": False}, "cause": "grounding_invalid"},
    )
    conn.execute("DROP TRIGGER verification_candidate_guard_rejections_no_delete")
    conn.execute("DELETE FROM verification_candidate_guard_rejections WHERE event_id = 'guard'")
    with pytest.raises(ValueError, match="typed detail cardinality"):
        adapter.resume_snapshot()

    adapter, conn = _adapter()
    adapter.append_logical_request(_request())
    _append_candidate(adapter)
    adapter.append_candidate_event(
        event_id="accepted", candidate_id="candidate-1", event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    conn.execute("INSERT INTO verification_candidate_requeues VALUES('accepted', 'jury2_rejected')")
    with pytest.raises(ValueError, match="typed detail cardinality"):
        adapter.resume_snapshot()


def test_direct_candidate_event_primitive_rejects_invalid_shape_without_row():
    _, conn = _adapter()
    with pytest.raises(ValueError, match="payload keys"):
        verification_candidates.append_candidate_event(
            conn, event_id="bad", candidate_id="missing", event_type="jury2_yes",
            payload={"unexpected": True}, created_at="now",
        )
    assert conn.execute("SELECT COUNT(*) FROM verification_candidate_events").fetchone()[0] == 0


def test_origin_and_provenance_mismatches_fail_closed():
    adapter, _ = _adapter()
    adapter.append_logical_request(_request())
    record = _record()
    record["source_hash"] = _hash("different-source")
    with pytest.raises(ValueError, match="provenance hashes"):
        adapter.append_candidate(
            candidate_id="candidate-1",
            claim_id="claim-1",
            ref_id="ref-1",
            scope="",
            candidate_cycle=1,
            origin_logical_request_id="request-1",
            record=record,
        )
    with pytest.raises(ValueError, match="origin request identity"):
        adapter.append_candidate(
            candidate_id="candidate-1",
            claim_id="other-claim",
            ref_id="ref-1",
            scope="",
            candidate_cycle=1,
            origin_logical_request_id="request-1",
            record=_record(),
        )


def test_dispatch_lease_retry_and_terminal_lifecycle_are_append_only():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request())
    adapter.lease_dispatch(_attempt("dispatch-1"))
    with pytest.raises(ValueError, match="live or completed"):
        adapter.lease_dispatch(_attempt("dispatch-raced"))
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts "
            "WHERE dispatch_attempt_id = 'dispatch-raced'"
        ).fetchone()[0]
        == 0
    )
    answer = {"logical_request_id": "request-1", "payload_fingerprint": _request()["prompt_hash"], "decision": {"source_supports_any": False, "provider_confidence": None}}
    adapter.finish_dispatch(
        logical_request_id="request-1",
        dispatch_attempt_id="dispatch-1",
        result="abandoned",
        technical_result="not_started",
        retry_cause="lease_expired",
    )
    adapter.lease_dispatch(_attempt("dispatch-2"))
    adapter.start_dispatch(
        logical_request_id="request-1",
        dispatch_attempt_id="dispatch-2",
    )
    adapter.finish_dispatch(
        logical_request_id="request-1",
        dispatch_attempt_id="dispatch-2",
        result="failed",
        technical_result="provider_failure",
        http_status=503,
        latency_ms=12.5,
        retry_cause="provider_failure",
    )
    adapter.lease_dispatch(_attempt("dispatch-3"))
    adapter.start_dispatch(
        logical_request_id="request-1",
        dispatch_attempt_id="dispatch-3",
    )
    adapter.finish_dispatch(
        logical_request_id="request-1",
        dispatch_attempt_id="dispatch-3",
        result="completed",
        technical_result="answer_received",
        http_status=200,
        latency_ms=8,
        answer=answer,
        answer_hash=answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2),
    )
    with pytest.raises(ValueError, match="live or completed"):
        adapter.lease_dispatch(_attempt("dispatch-4"))
    state = adapter.resume_snapshot()
    assert state["raw_call_count"] == 2
    failed = next(
        event for event in state["dispatch_events"]
        if event["event_type"] == "failed"
    )
    assert failed["payload"]["http_status"] == 503
    assert state["dispatch_leases"][0]["status"] == "terminal"


def test_hash_and_secret_shaped_inputs_fail_closed():
    adapter, _ = _adapter()
    request = _request()
    request["payload_hash"] = "wrong"
    with pytest.raises(ValueError, match="SHA-256"):
        adapter.append_logical_request(request)
    adapter.append_logical_request(_request())
    bad = _attempt("dispatch-1")
    bad["credential_id"] = "sk-live-secret"
    with pytest.raises(ValueError, match="configured alias"):
        adapter.lease_dispatch(bad)
    bad = _attempt("dispatch-2")
    bad["credential_fingerprint"] = "raw-secret"
    with pytest.raises(ValueError, match="SHA-256"):
        adapter.lease_dispatch(bad)


def test_competing_terminal_publishers_use_existing_pair_cas():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    repository = RunRepository(".", conn)
    adapter = ClaimEvidenceRunRepository(repository)
    adapter.ensure_pair(
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
    )
    results = [
        adapter.publish_terminal(
            claim_id="claim-1",
            ref_id="ref-1",
            scope="",
            status="accepted",
            outcome="supports",
            cause=None,
            candidate_id=candidate_id,
        )
        for candidate_id in ("candidate-1", "candidate-2")
    ]
    assert results == [True, False]
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_pair_transitions"
    ).fetchone()[0] == 1
