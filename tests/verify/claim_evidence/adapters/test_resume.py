# tests/verify/claim_evidence/adapters/test_resume.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import sqlite3

import pytest

from core.infra.db.schema_bootstrap import ensure_schema
from core.infra.db.repository import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.verify.claim_evidence.adapters.run_repository import (
    ClaimEvidenceRunRepository, RunLedger,
)
from core.verify.claim_evidence.domain.fingerprint import (
    candidate_fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
    FINGERPRINT_VERSION_V2,
)
from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_PROMPT_SPEC,
    JURY1_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.contracts.jury2 import JURY2_SYSTEM_PROMPT
from core.verify.claim_evidence.application.controller import (
    ClaimEvidenceController,
    resume_controller,
)
from core.verify.claim_evidence.domain.state_machine import (
    admit_jury1,
    apply_jury2,
    make_candidate,
    start,
)
from core.verify.claim_evidence.domain.types import (
    ControllerEvent,
    GroundedEvidence,
    Jury1Decision,
    Jury2Decision,
    LogicalRequest,
    StateMachinePolicy,
)


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
    manuscript = "manuscript context"
    conn.execute(
        "INSERT INTO manuscript_text VALUES(1,?,?,?)",
        (manuscript, _hash(manuscript), len(manuscript)),
    )
    repository = RunRepository(".", conn)
    repository._schema_version = SCHEMA_VERSION
    return ClaimEvidenceRunRepository(repository), conn


def _request(
    identifier, *, stage="support_gate", candidate_id=None, candidate_cycle=1
):
    payload = (
        {
            "source_spans": [{"span_id": "span-1", "text": "quote"}],
            "source_hash": _hash("source_hash"),
            "cited_source_mode": "full_text",
            "claim": "claim",
            "claim_context": "manuscript context",
            "citation_marker": "[1]",
            "task": {
                "task_id": stage,
                "instructions": JURY1_PROMPT_SPEC.task_prompt(stage),
            },
        }
        if stage != "jury2" else
        {
            "asserted_relation": "basis",
            "passage_subject": "the source-backed role",
            "selected_passages": [{"span_id": "span-1", "text": "quote"}],
        }
    )
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
        "evidence": ["quote"],
        "outcome_fields": {"claim_hash": _hash("claim"), "source_hash": _hash("source_hash"), "supported_part": "the source-backed role", "incompatible_proposition": None, "reason": None, "provider_confidence": None},
        "grounded": [{"span_id": "span-1", "text": "quote", "raw_start": 0, "raw_end": 5, "source_hash": _hash("source_hash"), "match_mode": "exact_raw", "score": 1.0}],
    }
    record.update({name: _hash(name) for name in HASH_FIELDS})
    record["prompt_hash"] = _request("jury1-record")["prompt_hash"]
    record["fingerprint"] = candidate_fingerprint(
        outcome=record["outcome"],
        evidence=tuple(record["evidence"]),
        outcome_fields=record["outcome_fields"],
        grounded=tuple({name: item[name] for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")} for item in record["grounded"]), version=FINGERPRINT_VERSION_V2,
    )
    return record


def _candidate(adapter, *, candidate_id="candidate-1", origin_logical_request_id="jury1-request"):
    adapter.append_candidate(
        candidate_id=candidate_id,
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        candidate_cycle=1,
        origin_logical_request_id=origin_logical_request_id,
        record=_record(),
    )


def test_jury1_grounding_rejections_persist_without_candidates_and_resume():
    adapter, conn = _adapter()
    for cycle in range(1, 6):
        request_id = f"jury1-request-{cycle}"
        adapter.append_logical_request(_request(request_id, candidate_cycle=cycle))
        adapter.append_jury1_rejection(
            event_id=f"rejection-{cycle}", logical_request_id=request_id,
            state_cause="grounding_invalid",
            cause="grounding/no_guarded_fuzzy_match")

    snapshot = adapter.resume_snapshot()
    assert conn.execute("SELECT COUNT(*) FROM verification_candidates").fetchone()[0] == 0
    assert snapshot["logical_requests"][0]["payload"]["source_spans"] == [
        {"span_id": "span-1", "text": "quote"}
    ]
    assert snapshot["logical_requests"][0]["payload"]["cited_source_mode"] == "full_text"
    assert [row["cause"] for row in snapshot["jury1_rejections"]] == [
        "grounding/no_guarded_fuzzy_match"
    ] * 5
    events = adapter.resume_events(
        snapshot, claim_id="claim-1", ref_id="ref-1", scope="")
    assert [(event.kind, event.cause) for event in events] == [
        ("jury1_rejected:grounding_invalid", "grounding/no_guarded_fuzzy_match")
    ] * 5
    resumed = resume_controller(StateMachinePolicy(5, 3, 3, "off"), events)
    assert resumed.candidate_cycles == resumed.mechanical_rejections == 5
    assert resumed.terminal is not None


def test_jury1_rejection_rejects_invalid_or_conflicting_facts():
    adapter, _ = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    with pytest.raises(ValueError, match="cause is invalid"):
        adapter.append_jury1_rejection(
            event_id="invalid-cause", logical_request_id="jury1-request",
            state_cause="grounding_invalid", cause="grounding/free-text")
    adapter.append_jury1_rejection(
        event_id="rejection", logical_request_id="jury1-request",
        state_cause="grounding_invalid", cause="grounding/no_guarded_fuzzy_match")
    with pytest.raises(ValueError, match="conflicts"):
        _candidate(adapter)

    adapter.append_logical_request(_request("jury1-for-jury2"))
    _candidate(
        adapter, candidate_id="candidate-for-jury2",
        origin_logical_request_id="jury1-for-jury2")
    with pytest.raises(ValueError, match="conflicts"):
        adapter.append_jury1_rejection(
            event_id="candidate-conflict", logical_request_id="jury1-for-jury2",
            state_cause="grounding_invalid", cause="grounding/no_guarded_fuzzy_match")
    adapter.append_logical_request(
        _request("jury2-request", stage="jury2", candidate_id="candidate-for-jury2"))
    with pytest.raises(ValueError, match="stage is invalid"):
        adapter.append_jury1_rejection(
            event_id="wrong-stage", logical_request_id="jury2-request",
            state_cause="grounding_invalid", cause="grounding/no_guarded_fuzzy_match")


@pytest.mark.parametrize(
    "kind", ("exhausted:jury1_guard", "candidate_cap_exhausted")
)
def test_run_ledger_persists_final_candidate_cap_rejection(kind):
    adapter, conn = _adapter()
    pair = "pair-1"
    request_id = f"{pair}:support_gate:1"
    adapter.append_logical_request(_request(request_id))

    RunLedger(adapter, pair, {}).append(ControllerEvent(
        kind,
        1,
        request_id=request_id,
        cause="grounding/no_guarded_fuzzy_match",
    ))

    row = conn.execute(
        "SELECT logical_request_id, state_cause, cause "
        "FROM jury1_rejection_events"
    ).fetchone()
    assert tuple(row) == (
        request_id,
        "grounding_invalid",
        "grounding/no_guarded_fuzzy_match",
    )


def test_run_ledger_persists_duplicate_evidence_text_rejection():
    adapter, conn = _adapter()
    pair = "pair-duplicate-text"
    request_id = f"{pair}:support_gate:1"
    adapter.append_logical_request(_request(request_id))

    RunLedger(adapter, pair, {}).append(ControllerEvent(
        "jury1_rejected:grounding_invalid",
        1,
        request_id=request_id,
        cause="grounding/duplicate_evidence_text",
    ))

    row = conn.execute(
        "SELECT logical_request_id, state_cause, cause "
        "FROM jury1_rejection_events"
    ).fetchone()
    assert tuple(row) == (
        request_id,
        "grounding_invalid",
        "grounding/duplicate_evidence_text",
    )


def test_resume_marks_guard_jury2_and_terminal_work_as_completed():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    _candidate(adapter)
    assert adapter.resume_snapshot()["candidate_state"]["candidate-1"] == {}

    adapter.append_candidate_event(
        event_id="candidate-1:guard",
        candidate_id="candidate-1",
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    guarded = adapter.resume_snapshot()["candidate_state"]["candidate-1"]
    assert guarded["guard_accepted"]["guard_result"]["accepted"] is True

    jury2_request = _request(
        "jury2-request",
        stage="jury2",
        candidate_id="candidate-1",
    )
    adapter.append_logical_request(jury2_request)
    adapter.append_candidate_event(
        event_id="candidate-1:jury2",
        candidate_id="candidate-1",
        event_type="jury2_yes",
        payload={
            "logical_request_id": "jury2-request",
            "jury2_payload_hash": jury2_request["payload_hash"],
            "answer": True,
        "reason": "consistent", "provider_confidence": None,
        },
    )
    jury2 = adapter.resume_snapshot()["candidate_state"]["candidate-1"]
    assert jury2["jury2_yes"]["reason"] == "consistent"

    adapter.append_candidate_event(
        event_id="candidate-1:terminal",
        candidate_id="candidate-1",
        event_type="terminal",
        payload={"resolution": "jury2_accepted", "assurance": "passed"},
    )
    adapter.ensure_pair(claim_id="claim-1", ref_id="ref-1", scope="")
    assert adapter.publish_terminal(
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        status="accepted",
        outcome="supports",
        cause=None,
        candidate_id="candidate-1",
    )
    resumed = adapter.resume_snapshot()
    assert resumed["candidate_state"]["candidate-1"]["terminal"] == {
        "assurance": "passed",
        "resolution": "jury2_accepted",
    }
    assert resumed["terminals"][0]["winner_call_id"] == "candidate-1"
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_pair_transitions"
    ).fetchone()[0] == 1


def test_jury2_event_uses_persisted_canonical_hash_not_wire_hash():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    record = _record()
    pair = "pair-1"
    candidate_id = f"{pair}:candidate:1:{record['fingerprint']}"
    adapter.append_candidate(
        candidate_id=candidate_id,
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        candidate_cycle=1,
        origin_logical_request_id="jury1-request",
        record=record,
    )
    jury2 = _request(
        "jury2-request", stage="jury2", candidate_id=candidate_id,
    )
    adapter.append_logical_request(jury2)
    wire_hash = _hash("wire-bytes-differ-from-canonical-payload")
    assert wire_hash != jury2["payload_hash"]
    RunLedger(adapter, pair, {"claim_id": "claim-1", "ref_id": "ref-1"}).append(
        ControllerEvent(
            "jury2_decision", 1,
            request_id="jury2-request",
            payload_fingerprint=wire_hash,
            candidate_fingerprint=record["fingerprint"],
            jury2_decision=Jury2Decision(True, "consistent"),
        )
    )
    payload_hash = conn.execute(
        "SELECT jury2_payload_hash FROM verification_candidate_jury2_decisions"
    ).fetchone()[0]
    assert payload_hash == jury2["payload_hash"]
    assert payload_hash != wire_hash
    assert adapter.resume_snapshot()["candidate_state"][candidate_id]["jury2_yes"][
        "jury2_payload_hash"
    ] == jury2["payload_hash"]


def test_pacing_resume_is_structured_and_creates_no_attempt():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    kwargs = {
        "logical_request_id": "jury1-request",
        "pacing_hash": _hash("pacing"),
        "prior_global_start_at": "2029-12-31T23:59:59+00:00",
        "prior_model_start_at": None,
        "next_eligible_at": "2030-01-01T00:00:00+00:00",
        "global_interval_ms": 100,
        "model_interval_ms": 250,
    }
    adapter.append_pacing_wait(**kwargs)
    adapter.append_pacing_wait(**kwargs)
    resumed = adapter.resume_snapshot()
    assert resumed["raw_call_count"] == 0
    assert resumed["dispatch_attempts"] == []
    assert resumed["pacing"]["jury1-request"]["next_eligible_at"].startswith("2030")
    assert resumed["pacing"]["jury1-request"]["model_interval_ms"] == 250
    assert conn.execute(
        "SELECT COUNT(*) FROM llm_dispatch_events"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("target", ["request", "candidate"])
def test_resume_revalidates_persisted_hashes(target):
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    _candidate(adapter)
    if target == "request":
        conn.execute("DROP TRIGGER llm_logical_requests_no_update")
        conn.execute(
            "UPDATE llm_logical_requests SET payload_hash = ?",
            (_hash("tampered"),),
        )
        match = "stored logical request payload hash"
    else:
        conn.execute("DROP TRIGGER verification_candidates_no_update")
        conn.execute(
            "UPDATE verification_candidates SET fingerprint = ?",
            (_hash("tampered"),),
        )
        match = "stored candidate fingerprint"
    with pytest.raises(ValueError, match=match):
        adapter.resume_snapshot()


def test_conflicting_candidate_lifecycle_fails_closed_before_append():
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    _candidate(adapter)
    adapter.append_candidate_event(
        event_id="candidate-1:accepted",
        candidate_id="candidate-1",
        event_type="guard_accepted",
        payload={"guard_result": {"accepted": True}, "cause": None},
    )
    with pytest.raises(ValueError, match="events conflict"):
        adapter.append_candidate_event(
            event_id="candidate-1:rejected",
            candidate_id="candidate-1",
            event_type="guard_rejected",
            payload={
                "guard_result": {"accepted": False},
                "cause": "grounding_invalid",
            },
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_candidate_events"
    ).fetchone()[0] == 1


def test_majority_fallback_persists_duplicate_history_and_replays_exactly():
    adapter, _ = _adapter()
    pair = "pair-1"
    policy = StateMachinePolicy(3, 3, 3, "medium")
    ledger = RunLedger(
        adapter,
        pair,
        {
            "claim_id": "claim-1",
            "ref_id": "ref-1",
            "scope": "",
            "claim_evidence_payload": {"claim": "c"},
        },
    )

    class _Unused:
        pass

    controller = ClaimEvidenceController(
        _Unused(),
        _Unused(),
        ledger,
        _Unused(),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    grounded = (
        GroundedEvidence(
            "quoted evidence",
            0,
            15,
            "span-1",
            _hash("source_hash"),
            "exact_raw",
            1.0,
        ),
    )
    partial = Jury1Decision(
        "partial",
        ("quoted evidence",),
        "Only one part is supported.",
        "c",
        None,
        None,
    )
    supports = Jury1Decision(
        "supports",
        ("quoted evidence",),
        "The claim is supported.",
        "The source-backed role.",
        None,
        None,
    )
    jury2_no = Jury2Decision(False, "The passage does not fit every target.")

    def origin_request(cycle):
        row = _request(
            f"{pair}:support_gate:{cycle}",
            candidate_cycle=cycle,
        )
        adapter.append_logical_request(row)
        payload = json.dumps(
            row["payload"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return LogicalRequest(
            row["stage"],
            row["logical_request_id"],
            payload,
            provider_prompt_fingerprint(JURY1_SYSTEM_PROMPT, payload),
            cycle,
            JURY1_SYSTEM_PROMPT,
        )

    def jury2_request(candidate):
        candidate_id = f"{pair}:candidate:{candidate.cycle}:{candidate.fingerprint}"
        row = _request(
            f"{pair}:jury2:{candidate.cycle}",
            stage="jury2",
            candidate_id=candidate_id,
            candidate_cycle=candidate.cycle,
        )
        adapter.append_logical_request(row)
        payload = json.dumps(
            row["payload"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return LogicalRequest(
            row["stage"],
            row["logical_request_id"],
            payload,
            provider_prompt_fingerprint(JURY2_SYSTEM_PROMPT, payload),
            candidate.cycle,
            JURY2_SYSTEM_PROMPT,
            candidate.fingerprint,
        )

    state = start(policy)
    first = make_candidate(
        state,
        partial,
        grounded,
        claim_hash=_hash("c"),
        source_hash=_hash("source_hash"),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    transition = admit_jury1(state, first)
    controller._record(transition, origin_request(1))
    state = transition.state
    transition = apply_jury2(state, jury2_no)
    controller._record(
        transition,
        jury2_request(first),
        jury2_decision=jury2_no,
    )
    state = transition.state

    duplicate = make_candidate(
        state,
        partial,
        grounded,
        claim_hash=_hash("c"),
        source_hash=_hash("source_hash"),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    transition = admit_jury1(state, duplicate)
    controller._record(transition, origin_request(2))
    state = transition.state

    final = make_candidate(
        state,
        supports,
        grounded,
        claim_hash=_hash("c"),
        source_hash=_hash("source_hash"),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    transition = admit_jury1(state, final)
    controller._record(transition, origin_request(3))
    state = transition.state
    transition = apply_jury2(state, jury2_no)
    controller._record(
        transition,
        jury2_request(final),
        jury2_decision=jury2_no,
    )

    snapshot = adapter.resume_snapshot()
    assert [row["candidate_cycle"] for row in snapshot["candidates"]] == [1, 2, 3]
    duplicate_id = f"{pair}:candidate:2:{duplicate.fingerprint}"
    final_id = f"{pair}:candidate:3:{final.fingerprint}"
    assert snapshot["candidate_state"][duplicate_id]["terminal"] == {
        "resolution": "majority_fallback",
        "assurance": "contested",
    }
    assert snapshot["candidate_state"][final_id]["jury2_no"]["reason"] == (
        jury2_no.reason
    )

    events = adapter.resume_events(
        snapshot,
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
    )
    resumed = resume_controller(policy, events)
    assert resumed == transition.state
    assert resumed.terminal is not None
    assert resumed.terminal.representative is not None
    assert resumed.terminal.representative.cycle == 2
