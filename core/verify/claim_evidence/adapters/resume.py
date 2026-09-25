# core/verify/claim_evidence/adapters/resume.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure projection of persisted run facts into controller resume state."""
from __future__ import annotations

from typing import Any

from ..domain.types import (
    CandidateRecord,
    ControllerEvent,
    GroundedEvidence,
    Jury1Decision,
    Jury2Decision,
)


def project_resume_events(
    snapshot: dict[str, Any], identity: tuple[str, str, str],
) -> tuple[ControllerEvent, ...]:
    requests = [
        row for row in snapshot["logical_requests"]
        if (row["claim_id"], row["ref_id"], row["scope"]) == identity
    ]
    candidates = {
        row["candidate_cycle"]: row for row in snapshot["candidates"]
        if (row["claim_id"], row["ref_id"], row["scope"]) == identity
    }
    request_by = {
        (row["stage"], row["candidate_cycle"]): row for row in requests
    }
    if len(request_by) != len(requests):
        raise ValueError("duplicate logical request stage and cycle")
    request_ids = {row["logical_request_id"]: row for row in requests}
    rejection_by_cycle: dict[int, dict[str, Any]] = {}
    for rejection in snapshot["jury1_rejections"]:
        request = request_ids.get(rejection["logical_request_id"])
        if request is None:
            continue
        cycle = request["candidate_cycle"]
        if cycle in rejection_by_cycle:
            raise ValueError("candidate cycle has multiple Jury1 rejections")
        rejection_by_cycle[cycle] = rejection
    events: list[ControllerEvent] = []
    for cycle in sorted(set(candidates) | set(rejection_by_cycle)):
        rejection, candidate_row = rejection_by_cycle.get(cycle), candidates.get(cycle)
        if rejection is not None and candidate_row is not None:
            raise ValueError("candidate cycle has both candidate and rejection")
        if rejection is not None:
            events.append(ControllerEvent(
                f"jury1_rejected:{rejection['state_cause']}", cycle,
                request_id=rejection["logical_request_id"], cause=rejection["cause"],
            ))
            continue
        if candidate_row is None:
            continue
        if candidate_row["origin_logical_request_id"] not in request_ids:
            raise ValueError("persisted candidate lacks its Jury1 flow request")
        candidate = _candidate(candidate_row)
        events.append(ControllerEvent("jury1_candidate", cycle, candidate=candidate))
        jury2 = request_by.get(("jury2", cycle))
        if jury2 is None:
            continue
        events.extend(_technical_events(snapshot, jury2, "jury2"))
        lifecycle = snapshot["candidate_state"][candidate_row["candidate_id"]]
        requeue = lifecycle.get("requeued")
        if requeue is not None and requeue.get("cause") == "jury2_provider_uncertain":
            events.append(ControllerEvent("jury2_provider_uncertain", cycle))
            continue
        decision = lifecycle.get("jury2_yes") or lifecycle.get("jury2_no")
        if decision is not None:
            events.append(ControllerEvent(
                "jury2_decision", cycle,
                jury2_decision=Jury2Decision(
                bool(decision["answer"]), decision["reason"],
                decision.get("provider_confidence")
                ),
            ))
    return tuple(events)


def project_technical_failure_counts(
    snapshot: dict[str, Any], identity: tuple[str, str, str],
) -> dict[tuple[str, int], int]:
    requests = {
        row["logical_request_id"]: (row["stage"], row["candidate_cycle"])
        for row in snapshot["logical_requests"]
        if (row["claim_id"], row["ref_id"], row["scope"]) == identity
    }
    attempts = {
        row["dispatch_attempt_id"]: row["logical_request_id"]
        for row in snapshot["dispatch_attempts"]
    }
    counts: dict[tuple[str, int], int] = {}
    for event in snapshot["dispatch_events"]:
        if event["event_type"] not in {"failed", "abandoned"}:
            continue
        key = requests.get(attempts.get(event["dispatch_attempt_id"]))
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _candidate(row: dict[str, Any]) -> CandidateRecord:
    fields = row["outcome_fields"]
    decision = Jury1Decision(
        row["outcome"], tuple(row["evidence"]), row["explanation"],
        fields.get("supported_part"), fields.get("incompatible_proposition"),
        fields.get("reason"), fields.get("provider_confidence"),
    )
    grounded = tuple(
        GroundedEvidence(
            item["text"], item["raw_start"], item["raw_end"], item["span_id"],
            item["source_hash"], item.get("match_mode", "exact_raw"),
            item.get("score", 1.0),
        )
        for item in row["grounded"]
    )
    return CandidateRecord(
        row["candidate_cycle"], decision, grounded, row["fingerprint"],
        row.get("duplicate_of_cycle"),
    )


def _technical_events(
    snapshot: dict[str, Any], request: dict[str, Any], stage: str,
) -> list[ControllerEvent]:
    count = sum(
        event in {"failed", "abandoned"} for event in _terminals(snapshot, request)
    )
    return [
        ControllerEvent(f"{stage}_technical", request["candidate_cycle"])
        for _ in range(count)
    ]


def _terminals(
    snapshot: dict[str, Any], request: dict[str, Any],
) -> list[str]:
    attempts = {
        row["dispatch_attempt_id"] for row in snapshot["dispatch_attempts"]
        if row["logical_request_id"] == request["logical_request_id"]
    }
    return [
        row["event_type"] for row in snapshot["dispatch_events"]
        if row["dispatch_attempt_id"] in attempts
        and row["event_type"] in {"completed", "failed", "abandoned"}
    ]
