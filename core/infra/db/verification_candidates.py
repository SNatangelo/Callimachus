# core/infra/db/verification_candidates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only persistence primitives for verification candidates."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import jury1_rejections
from .llm_dispatches import require_hash, require_positive_int, require_text


HASH_FIELDS = (
    "source_hash",
    "context_hash",
    "retrieval_hash",
    "prompt_hash",
    "model_hash",
    "policy_hash",
)
CANDIDATE_EVENTS = frozenset(
    {
        "guard_accepted",
        "guard_rejected",
        "jury2_yes",
        "jury2_no",
        "jury2_technical_exhausted",
        "requeued",
        "terminal",
    }
)
_JURY2_TECHNICAL_CAUSES = frozenset({
    "rate_limited", "credential_invalid", "lane_unavailable", "timeout",
    "transport", "provider_failure",
})
_TERMINAL_ASSURANCES = frozenset({"passed", "not_evaluated", "contested", "non_crediting"})
_TERMINAL_COMBINATIONS = frozenset({
    ("jury2_not_eligible", "not_evaluated"),
    ("jury2_off", "not_evaluated"),
    ("jury2_accepted", "passed"),
    ("jury2_rejected_nonbinding", "contested"),
    ("majority_fallback", "contested"),
    ("jury1_technical", "non_crediting"),
    ("jury1_guard", "non_crediting"),
    ("jury2_technical", "non_crediting"),
    ("no_consensus", "non_crediting"),
    ("jury2_rejected", "non_crediting"),
    ("jury1_provider_uncertain", "non_crediting"),
    ("jury2_provider_uncertain", "non_crediting"),
    ("cancelled", "non_crediting"),
})
_CANDIDATE_FIELDS = (
    "candidate_id",
    "claim_id",
    "ref_id",
    "scope",
    "candidate_cycle",
    "origin_logical_request_id",
    "fingerprint",
    "outcome",
    "explanation",
    "provider_confidence",
    "claim_hash",
    "supported_part",
    "incompatible_proposition",
    "non_decidable_reason",
    "source_hash",
    "context_hash",
    "retrieval_hash",
    "prompt_hash",
    "model_hash",
    "policy_hash",
)


def append_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    claim_id: str,
    ref_id: str,
    scope: str,
    candidate_cycle: int,
    origin_logical_request_id: str,
    fingerprint: str,
    record: dict[str, Any],
    created_at: str,
) -> None:
    """Append one candidate or accept an exact immutable replay."""
    values = (
        candidate_id,
        claim_id,
        ref_id,
        scope,
        candidate_cycle,
        origin_logical_request_id,
        fingerprint,
        record.get("outcome"),
        record.get("explanation"),
        _candidate_fields(record)["provider_confidence"],
        _candidate_fields(record)["claim_hash"],
        _candidate_fields(record)["supported_part"],
        _candidate_fields(record)["incompatible_proposition"],
        _candidate_fields(record)["non_decidable_reason"],
        record["source_hash"],
        record["context_hash"],
        record["retrieval_hash"],
        record["prompt_hash"],
        record["model_hash"],
        record["policy_hash"],
        created_at,
    )
    row = conn.execute(
        "SELECT * FROM verification_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is not None:
        if tuple(row[name] for name in _CANDIDATE_FIELDS) != values[:-1]:
            raise ValueError("candidate replay differs from immutable record")
        persisted = dict(row)
        _decode_candidate_children(conn, persisted)
        if (persisted["outcome_fields"], persisted["evidence"], persisted["grounded"]) != (record.get("outcome_fields"), record.get("evidence"), record.get("grounded")):
            raise ValueError("candidate replay children differ from immutable record")
        return
    conn.execute(
        """INSERT INTO verification_candidates(
          candidate_id, claim_id, ref_id, scope, candidate_cycle,
        origin_logical_request_id, fingerprint, outcome, explanation, provider_confidence,
          claim_hash, supported_part, incompatible_proposition, non_decidable_reason,
          source_hash, context_hash, retrieval_hash, prompt_hash, model_hash,
          policy_hash, created_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    _append_candidate_children(conn, candidate_id, record)


def append_candidate_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    candidate_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    """Append an event, treating an exact identity replay as a no-op."""
    validate_event_input(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    row = conn.execute(
        "SELECT * FROM verification_candidate_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is not None:
        if (row["candidate_id"], row["event_type"]) != (candidate_id, event_type):
            raise ValueError("candidate event replay differs from immutable record")
        if decode_candidate_event(dict(row), conn=conn)["payload"] != payload:
            raise ValueError("candidate event replay differs from immutable record")
        return
    candidate = conn.execute(
        "SELECT * FROM verification_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise ValueError("candidate event references a missing candidate")
    event = {
        "event_id": event_id,
        "candidate_id": candidate_id,
        "event_type": event_type,
        "payload": payload,
    }
    if event_type.startswith("jury2_"):
        logical_request_id = payload["logical_request_id"]
        request = conn.execute(
            "SELECT * FROM llm_logical_requests WHERE logical_request_id = ?",
            (logical_request_id,),
        ).fetchone()
        validate_event_links(
            [event],
            {} if request is None else {logical_request_id: dict(request)},
        )
    existing = [
        decode_candidate_event(dict(item), conn=conn)
        for item in conn.execute(
            """SELECT * FROM verification_candidate_events
               WHERE candidate_id = ? ORDER BY created_at, event_id""",
            (candidate_id,),
        )
    ]
    candidate_state(existing + [event], {candidate_id: dict(candidate)})
    conn.execute(
        """
        INSERT INTO verification_candidate_events(
          event_id, candidate_id, event_type, created_at
        ) VALUES(?,?,?,?)
        """,
        (event_id, candidate_id, event_type, created_at),
    )
    _append_candidate_event_detail(conn, event_id, event_type, payload)


def validate_candidate_input(
    *,
    candidate_id: object,
    claim_id: object,
    ref_id: object,
    scope: object,
    candidate_cycle: object,
    origin_logical_request_id: object,
    record: dict[str, Any],
    origin: Any,
) -> None:
    """Validate immutable linkage and provenance before persistence."""
    for name, value in (
        ("candidate_id", candidate_id),
        ("claim_id", claim_id),
        ("ref_id", ref_id),
        ("origin_logical_request_id", origin_logical_request_id),
    ):
        require_text(name, value)
    if not isinstance(scope, str):
        raise ValueError("scope must be text")
    require_positive_int("candidate_cycle", candidate_cycle)
    expected = (claim_id, ref_id, scope, candidate_cycle)
    actual = tuple(
        origin[name]
        for name in ("claim_id", "ref_id", "scope", "candidate_cycle")
    )
    if actual != expected or origin["stage"] not in {
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence",
    }:
        raise ValueError("candidate origin request identity mismatch")
    require_hashes(record)
    if any(record[name] != origin[name] for name in HASH_FIELDS):
        raise ValueError("candidate provenance hashes differ from origin request")
    _validate_candidate_payload(record)


def validate_event_input(
    *,
    event_id: object,
    event_type: object,
    payload: object,
) -> None:
    if event_type not in CANDIDATE_EVENTS:
        raise ValueError("invalid candidate event")
    require_text("candidate event id", event_id)
    if not isinstance(payload, dict):
        raise ValueError("candidate event payload must be an object")
    if event_type in {"guard_accepted", "guard_rejected"}:
        if set(payload) != {"guard_result", "cause"}:
            raise ValueError("candidate guard event payload keys are invalid")
        result = payload.get("guard_result")
        expected = event_type == "guard_accepted"
        if not isinstance(result, dict) or set(result) != {"accepted"} or result.get("accepted") is not expected:
            raise ValueError("candidate guard event result is invalid")
        if expected and payload.get("cause") is not None:
            raise ValueError("accepted guard event cause is invalid")
        if not expected and payload.get("cause") not in jury1_rejections.CAUSES:
            raise ValueError("guard cause is invalid")
    elif event_type in {"jury2_yes", "jury2_no"}:
        if set(payload) != {"logical_request_id", "jury2_payload_hash", "answer", "reason", "provider_confidence"}:
            raise ValueError("candidate Jury2 decision payload keys are invalid")
        require_text("Jury2 logical request id", payload.get("logical_request_id"))
        require_hash("Jury2 payload hash", payload.get("jury2_payload_hash"))
        if payload.get("answer") is not (event_type == "jury2_yes"):
            raise ValueError("candidate Jury2 semantic answer is invalid")
        confidence = payload.get("provider_confidence")
        probability_only = (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
            and 0.0 <= confidence <= 1.0
        )
        if probability_only:
            if payload.get("reason") is not None:
                raise ValueError("probability-only Jury2 event contains rationale")
        elif confidence is None:
            require_text("Jury2 reason", payload.get("reason"))
        else:
            raise ValueError("Jury2 provider confidence is invalid")
    elif event_type == "jury2_technical_exhausted":
        if set(payload) != {"logical_request_id", "jury2_payload_hash", "failure_cause"}:
            raise ValueError("candidate Jury2 technical payload keys are invalid")
        require_text("Jury2 logical request id", payload.get("logical_request_id"))
        require_hash("Jury2 payload hash", payload.get("jury2_payload_hash"))
        if payload.get("failure_cause") not in _JURY2_TECHNICAL_CAUSES:
            raise ValueError("Jury2 technical cause is invalid")
    elif event_type == "requeued":
        if set(payload) != {"cause"} or payload.get("cause") not in {"jury2_rejected", "jury2_provider_uncertain"}:
            raise ValueError("requeue cause is invalid")
    elif event_type == "terminal":
        if set(payload) != {"resolution", "assurance"}:
            raise ValueError("terminal payload keys are invalid")
        if payload.get("assurance") not in _TERMINAL_ASSURANCES or (payload.get("resolution"), payload.get("assurance")) not in _TERMINAL_COMBINATIONS:
            raise ValueError("terminal resolution/assurance is invalid")


def decode_candidate_event(
    row: dict[str, Any], *, conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Decode one current typed candidate event."""
    record = dict(row)
    record["payload"] = _decode_candidate_event_detail(conn, record)
    validate_event_input(event_id=record["event_id"], event_type=record["event_type"], payload=record["payload"])
    return record


def _append_candidate_event_detail(conn: sqlite3.Connection, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "guard_rejected":
        conn.execute("INSERT INTO verification_candidate_guard_rejections VALUES(?,?)", (event_id, payload["cause"]))
    elif event_type in {"jury2_yes", "jury2_no"}:
        conn.execute("INSERT INTO verification_candidate_jury2_decisions VALUES(?,?,?,?,?)", (event_id, payload["logical_request_id"], payload["jury2_payload_hash"], payload["reason"], payload["provider_confidence"]))
    elif event_type == "jury2_technical_exhausted":
        conn.execute("INSERT INTO verification_candidate_jury2_technical_failures VALUES(?,?,?,?)", (event_id, payload["logical_request_id"], payload["jury2_payload_hash"], payload["failure_cause"]))
    elif event_type == "requeued":
        conn.execute("INSERT INTO verification_candidate_requeues VALUES(?,?)", (event_id, payload["cause"]))
    elif event_type == "terminal":
        conn.execute("INSERT INTO verification_candidate_terminals VALUES(?,?,?)", (event_id, payload["resolution"], payload["assurance"]))


def _decode_candidate_event_detail(conn: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
    event_id, event_type = record["event_id"], record["event_type"]
    expected_table = {
        "guard_accepted": None,
        "guard_rejected": "verification_candidate_guard_rejections",
        "jury2_yes": "verification_candidate_jury2_decisions",
        "jury2_no": "verification_candidate_jury2_decisions",
        "jury2_technical_exhausted": "verification_candidate_jury2_technical_failures",
        "requeued": "verification_candidate_requeues",
        "terminal": "verification_candidate_terminals",
    }[event_type]
    detail_tables = (
        "verification_candidate_guard_rejections",
        "verification_candidate_jury2_decisions",
        "verification_candidate_jury2_technical_failures",
        "verification_candidate_requeues",
        "verification_candidate_terminals",
    )
    present = [table for table in detail_tables if conn.execute(
        f"SELECT 1 FROM {table} WHERE event_id = ?", (event_id,)
    ).fetchone() is not None]
    if present != ([] if expected_table is None else [expected_table]):
        raise ValueError("candidate event typed detail cardinality is invalid")
    if event_type == "guard_accepted":
        return {"guard_result": {"accepted": True}, "cause": None}
    table, columns = {
        "guard_rejected": ("verification_candidate_guard_rejections", "cause"),
        "jury2_yes": ("verification_candidate_jury2_decisions", "logical_request_id, jury2_payload_hash, reason, provider_confidence"),
        "jury2_no": ("verification_candidate_jury2_decisions", "logical_request_id, jury2_payload_hash, reason, provider_confidence"),
        "jury2_technical_exhausted": ("verification_candidate_jury2_technical_failures", "logical_request_id, jury2_payload_hash, failure_cause"),
        "requeued": ("verification_candidate_requeues", "cause"),
        "terminal": ("verification_candidate_terminals", "resolution, assurance"),
    }[event_type]
    row = conn.execute(f"SELECT {columns} FROM {table} WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("candidate event typed detail is missing")
    values = dict(row)
    if event_type == "guard_rejected": return {"guard_result": {"accepted": False}, "cause": values["cause"]}
    if event_type in {"jury2_yes", "jury2_no"}: return {**values, "answer": event_type == "jury2_yes"}
    return values


def decode_candidate(
    row: dict[str, Any], *, conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Decode one candidate from the current typed child relations."""
    record = dict(row)
    _decode_candidate_children(conn, record)
    require_hashes(record)
    _validate_candidate_payload(record)
    return record


_OUTCOMES = frozenset({"supports", "partial", "contradicts", "related", "off_topic", "non_decidable"})
_EVIDENCE_OUTCOMES = frozenset({"supports", "partial", "contradicts"})
_NON_DECIDABLE = frozenset({"attribution", "material_limit", "no_consensus", "verification_unavailable", "retrieval_limit", "provider_uncertain"})


def _candidate_fields(record: dict[str, Any]) -> dict[str, str | None]:
    fields = record.get("outcome_fields")
    if not isinstance(fields, dict):
        raise ValueError("candidate outcome fields are invalid")
    required = {"claim_hash", "supported_part", "incompatible_proposition", "reason", "provider_confidence"}
    allowed = required | {"source_hash"}
    if set(fields) != required and set(fields) != allowed:
        raise ValueError("candidate outcome fields contain unknown keys")
    if "source_hash" in fields and fields["source_hash"] != record.get("source_hash"):
        raise ValueError("candidate outcome source provenance is invalid")
    return {"claim_hash": fields.get("claim_hash"), "supported_part": fields.get("supported_part"), "incompatible_proposition": fields.get("incompatible_proposition"), "non_decidable_reason": fields.get("reason"), "provider_confidence": fields.get("provider_confidence")}


def _validate_candidate_payload(record: dict[str, Any]) -> None:
    outcome = record.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError("candidate outcome is invalid")
    fields = _candidate_fields(record)
    require_hash("claim_hash", fields["claim_hash"])
    confidence = fields["provider_confidence"]
    probability_only = (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and 0.0 <= confidence <= 1.0
    )
    if confidence is not None and not probability_only:
        raise ValueError("candidate provider confidence is invalid")
    if probability_only:
        if record.get("explanation") is not None or any(
            fields[name] is not None
            for name in ("supported_part", "incompatible_proposition")
        ):
            raise ValueError("probability-only candidate contains rationale")
    else:
        require_text("candidate explanation", record.get("explanation"))
    evidence = record.get("evidence")
    grounded = record.get("grounded")
    if not isinstance(evidence, list) or any(not isinstance(x, str) or not x.strip() for x in evidence) or len(set(evidence)) != len(evidence):
        raise ValueError("candidate evidence is invalid")
    if not probability_only and (outcome in _EVIDENCE_OUTCOMES) != bool(evidence):
        raise ValueError("candidate evidence cardinality is invalid")
    if not isinstance(grounded, list) or len(grounded) != len(evidence):
        raise ValueError("candidate grounding cardinality is invalid")
    if (outcome in {"supports", "partial"}) != (isinstance(fields["supported_part"], str) and bool(fields["supported_part"].strip())):
        raise ValueError("candidate supported_part is invalid")
    if outcome not in {"supports", "partial"} and fields["supported_part"] is not None: raise ValueError("candidate supported_part is invalid")
    if (outcome == "contradicts") != (isinstance(fields["incompatible_proposition"], str) and bool(fields["incompatible_proposition"].strip())):
        raise ValueError("candidate incompatible_proposition is invalid")
    if outcome != "contradicts" and fields["incompatible_proposition"] is not None: raise ValueError("candidate incompatible_proposition is invalid")
    if outcome == "non_decidable":
        if fields["non_decidable_reason"] not in _NON_DECIDABLE: raise ValueError("candidate non_decidable reason is invalid")
    elif fields["non_decidable_reason"] is not None: raise ValueError("candidate non_decidable reason is invalid")
    for evidence_text, item in zip(evidence, grounded):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip(): raise ValueError("candidate grounding text is invalid")
        require_text("grounding span_id", item.get("span_id"))
        start, end = item.get("raw_start"), item.get("raw_end")
        if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int) or start < 0 or end <= start: raise ValueError("candidate grounding range is invalid")
        if item.get("source_hash") != record["source_hash"]: raise ValueError("candidate grounding provenance is invalid")
        mode, score = item.get("match_mode"), item.get("score")
        if mode not in {"exact_raw", "normalized", "fuzzy"} or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score): raise ValueError("candidate grounding match is invalid")
        if mode == "exact_raw" and item["text"] != evidence_text: raise ValueError("candidate exact grounding text is invalid")
        if mode in {"exact_raw", "normalized"} and score != 1.0: raise ValueError("candidate grounding score is invalid")
        if mode == "fuzzy" and not 0.92 <= score <= 1.0: raise ValueError("candidate grounding score is invalid")


def _append_candidate_children(conn: sqlite3.Connection, candidate_id: str, record: dict[str, Any]) -> None:
    for index, (quotation, item) in enumerate(zip(record["evidence"], record["grounded"])):
        conn.execute("INSERT INTO verification_candidate_evidence VALUES(?,?,?)", (candidate_id, index, quotation))
        conn.execute("INSERT INTO verification_candidate_grounding VALUES(?,?,?,?,?,?,?,?)", (candidate_id, index, item["text"], item["raw_start"], item["raw_end"], item["span_id"], item["match_mode"], item["score"]))


def _decode_candidate_children(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    identifier = record["candidate_id"]
    evidence = conn.execute("SELECT evidence_order, quotation FROM verification_candidate_evidence WHERE candidate_id = ? ORDER BY evidence_order", (identifier,)).fetchall()
    grounding = conn.execute("SELECT evidence_order, text, raw_start, raw_end, span_id, match_mode, score FROM verification_candidate_grounding WHERE candidate_id = ? ORDER BY evidence_order", (identifier,)).fetchall()
    if [x["evidence_order"] for x in evidence] != list(range(len(evidence))) or [x["evidence_order"] for x in grounding] != list(range(len(evidence))): raise ValueError("candidate child ordering is invalid")
    record["evidence"] = [x["quotation"] for x in evidence]
    record["grounded"] = [{"text": x["text"], "raw_start": x["raw_start"], "raw_end": x["raw_end"], "span_id": x["span_id"], "source_hash": record["source_hash"], "match_mode": x["match_mode"], "score": x["score"]} for x in grounding]
    record["outcome_fields"] = {"claim_hash": record.pop("claim_hash"), "source_hash": record["source_hash"], "supported_part": record.pop("supported_part"), "incompatible_proposition": record.pop("incompatible_proposition"), "reason": record.pop("non_decidable_reason"), "provider_confidence": record.pop("provider_confidence")}


def validate_links(
    requests: dict[str, dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> None:
    for candidate in candidates.values():
        origin = requests.get(candidate["origin_logical_request_id"])
        if origin is None or origin["stage"] not in {
            "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
            "explanation_evidence",
        }:
            raise ValueError("candidate origin request is missing or not Jury1")
        require_same_pair_cycle(origin, candidate)
        if any(candidate[name] != origin[name] for name in HASH_FIELDS):
            raise ValueError("candidate provenance differs from origin request")
    for request in requests.values():
        if request["stage"] != "jury2":
            continue
        candidate = candidates.get(request["candidate_id"])
        if candidate is None:
            raise ValueError("Jury2 request candidate is missing")
        require_same_pair_cycle(request, candidate)


def validate_event_links(
    events: list[dict[str, Any]],
    requests: dict[str, dict[str, Any]],
) -> None:
    for event in events:
        if not event["event_type"].startswith("jury2_"):
            continue
        payload = event["payload"]
        request = requests.get(payload["logical_request_id"])
        if (
            request is None
            or request["stage"] != "jury2"
            or request["candidate_id"] != event["candidate_id"]
            or request["payload_hash"] != payload["jury2_payload_hash"]
        ):
            raise ValueError("candidate Jury2 event request linkage is invalid")


def candidate_state(
    events: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    state = {candidate_id: {} for candidate_id in candidates}
    exclusive = (
        {"guard_accepted", "guard_rejected"},
        {"jury2_yes", "jury2_no", "jury2_technical_exhausted"},
    )
    for event in events:
        target = state.get(event["candidate_id"])
        if target is None:
            raise ValueError("candidate event references a missing candidate")
        event_type = event["event_type"]
        if event_type in target:
            raise ValueError("candidate lifecycle event is duplicated")
        if any(event_type in group and set(target) & group for group in exclusive):
            raise ValueError("candidate lifecycle events conflict")
        target[event_type] = event["payload"]
    return state


def require_same_pair_cycle(request: Any, candidate: Any) -> None:
    fields = ("claim_id", "ref_id", "scope", "candidate_cycle")
    if tuple(request[name] for name in fields) != tuple(
        candidate[name] for name in fields
    ):
        raise ValueError("logical request candidate identity mismatch")


def require_hashes(value: dict[str, Any]) -> None:
    for name in HASH_FIELDS:
        require_hash(name, value.get(name))
