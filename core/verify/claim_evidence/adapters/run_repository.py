# core/verify/claim_evidence/adapters/run_repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run-local ledger adapter; persistence and resume stay outside the domain."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Iterator

from core.infra.db import jury1_rejections, llm_dispatches, verification_candidates
from core.infra.db.schema import SCHEMA_VERSION
from core.verify.claim_evidence.domain.fingerprint import (
    candidate_fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
    FINGERPRINT_VERSION_V2,
)
from core.verify.claim_evidence.domain.types import (
    ControllerEvent,
    CandidateRecord,
)
from .resume import project_resume_events, project_technical_failure_counts


class ClaimEvidenceRunRepository:
    """Fail-closed bridge to verifier ledgers and the established pair CAS."""

    def __init__(self, repository: Any, lock: Any | None = None) -> None:
        shared = getattr(repository, "_claim_evidence_connection_lock", None)
        self._repository, self._conn = repository, repository._conn
        self._lock = lock or shared or threading.RLock()

    def freeze_config(self, snapshot: dict[str, Any]) -> None:
        existing = self._repository.get_run_setting("verify_claim_evidence_config")
        if existing is None:
            self._repository.set_run_setting("verify_claim_evidence_config", snapshot)
        elif existing != snapshot:
            raise ValueError("active claim-evidence config differs from the run")

    def pair_state(self, *, claim_id: str, ref_id: str, scope: str) -> dict[str, Any] | None:
        with self._lock:
            return self._repository.get_verification_pair_state(claim_id=claim_id, ref_id=ref_id, scope=scope)

    def append_logical_request(self, request: dict[str, Any]) -> None:
        llm_dispatches.validate_request_shape(request)
        if request["payload_hash"] != payload_fingerprint(request["payload"], version=self.fingerprint_version()):
            raise ValueError("logical request payload hash mismatch")
        self._require_prompt_hash(request)
        if request["stage"] == "jury2":
            candidate = self._candidate_row(request.get("candidate_id"))
            verification_candidates.require_same_pair_cycle(request, candidate)
        with self._transaction():
            llm_dispatches.append_logical_request(
                self._conn, request=request, created_at=_now())

    def _require_prompt_hash(self, request: dict[str, Any]) -> None:
        from core.verify.claim_evidence.contracts.jury1_flow import JURY1_PROMPT_SPEC
        from core.verify.claim_evidence.contracts.jury2 import JURY2_PROMPT_SPEC
        spec = {
            "support_gate": JURY1_PROMPT_SPEC,
            "full_support_gate": JURY1_PROMPT_SPEC,
            "contrary_gate": JURY1_PROMPT_SPEC,
            "topic_gate": JURY1_PROMPT_SPEC,
            "explanation_evidence": JURY1_PROMPT_SPEC,
            "jury2": JURY2_PROMPT_SPEC,
        }.get(request.get("stage"))
        if spec is None:
            raise ValueError("logical request stage has no prompt specification")
        if request.get("prompt_hash") != provider_prompt_fingerprint(spec.system_prompt, json.dumps(request["payload"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")): raise ValueError("logical request provider prompt hash mismatch")

    def append_candidate(
        self,
        *,
        candidate_id: str,
        claim_id: str,
        ref_id: str,
        scope: str,
        candidate_cycle: int,
        origin_logical_request_id: str,
        record: dict[str, Any],
    ) -> None:
        origin = self._request_row(origin_logical_request_id)
        verification_candidates.validate_candidate_input(candidate_id=candidate_id, claim_id=claim_id, ref_id=ref_id, scope=scope, candidate_cycle=candidate_cycle, origin_logical_request_id=origin_logical_request_id, record=record, origin=origin)
        expected = candidate_fingerprint(outcome=record.get("outcome"), evidence=tuple(record.get("evidence", [])), outcome_fields=record.get("outcome_fields", {}), grounded=_candidate_grounding(record), version=self.fingerprint_version())
        if record.get("fingerprint") != expected:
            raise ValueError("candidate fingerprint mismatch")
        with self._transaction():
            jury1_rejections.require_no_rejection(self._conn, origin_logical_request_id)
            verification_candidates.append_candidate(self._conn, candidate_id=candidate_id, claim_id=claim_id, ref_id=ref_id, scope=scope, candidate_cycle=candidate_cycle, origin_logical_request_id=origin_logical_request_id, fingerprint=expected, record=record, created_at=_now())

    def append_jury1_rejection(
        self,
        *,
        event_id: str,
        logical_request_id: str,
        state_cause: str,
        cause: str,
    ) -> None:
        with self._transaction():
            jury1_rejections.append_for_logical_request(self._conn, event_id=event_id, logical_request_id=logical_request_id, state_cause=state_cause, cause=cause, created_at=_now())

    def append_candidate_event(
        self,
        *,
        event_id: str,
        candidate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        verification_candidates.validate_event_input(
            event_id=event_id, event_type=event_type, payload=payload)
        self._candidate_row(candidate_id)
        with self._transaction():
            verification_candidates.append_candidate_event(
                self._conn, event_id=event_id, candidate_id=candidate_id,
                event_type=event_type, payload=payload, created_at=_now())

    def lease_dispatch(self, attempt: dict[str, Any]) -> None:
        llm_dispatches.validate_dispatch_shape(attempt)
        with self._transaction():
            llm_dispatches.lease_dispatch(
                self._conn, attempt=attempt, created_at=_now())

    def start_dispatch(self, *, logical_request_id: str, dispatch_attempt_id: str) -> None:
        with self._transaction():
            llm_dispatches.start_dispatch(self._conn, logical_request_id=logical_request_id, dispatch_attempt_id=dispatch_attempt_id, created_at=_now())

    def request_payload_hash(self, logical_request_id: str) -> str:
        return self._request_row(logical_request_id)["payload_hash"]

    def fingerprint_version(self) -> str:
        with self._lock:
            version = getattr(self._repository, "_schema_version", None)
            if version != SCHEMA_VERSION:
                raise ValueError("unsupported verification ledger schema version")
            row = self._conn.execute("SELECT fingerprint_version FROM verification_ledger_metadata WHERE singleton = 1").fetchone()
            if row is None or row["fingerprint_version"] != FINGERPRINT_VERSION_V2:
                raise ValueError("verification ledger fingerprint version is missing or invalid")
            return FINGERPRINT_VERSION_V2

    def jury2_payload_hash(self, logical_request_id: str, candidate_id: str) -> str:
        """Return the persisted Jury2 hash after validating its candidate link."""
        request = self._request_row(logical_request_id)
        candidate = self._candidate_row(candidate_id)
        if request["stage"] != "jury2" or request["candidate_id"] != candidate_id:
            raise ValueError("Jury2 request candidate linkage is invalid")
        verification_candidates.require_same_pair_cycle(request, candidate)
        return request["payload_hash"]

    def finish_dispatch(
        self,
        *,
        logical_request_id: str,
        dispatch_attempt_id: str,
        result: str,
        technical_result: str,
        http_status: int | None = None,
        latency_ms: float | None = None,
        retry_cause: str | None = None,
        answer_hash: str | None = None,
        retry_after_seconds: int | None = None,
        protocol_error_code: str | None = None,
        response_hash: str | None = None,
        answer: dict[str, Any] | None = None,
        scheduler_control: dict[str, Any] | None = None,
    ) -> None:
        payload = llm_dispatches.dispatch_terminal_payload(result=result, technical_result=technical_result, http_status=http_status, latency_ms=latency_ms,
            retry_cause=retry_cause, answer_hash=answer_hash, retry_after_seconds=retry_after_seconds, protocol_error_code=protocol_error_code, response_hash=response_hash, answer=answer, scheduler_control=scheduler_control)
        with self._transaction():
            llm_dispatches.finish_dispatch(self._conn, logical_request_id=logical_request_id, dispatch_attempt_id=dispatch_attempt_id, result=result, payload=payload, created_at=_now())

    def append_pacing_wait(
        self,
        *,
        logical_request_id: str,
        pacing_hash: str,
        prior_global_start_at: str | None,
        prior_model_start_at: str | None,
        next_eligible_at: str,
        global_interval_ms: int,
        model_interval_ms: int,
    ) -> None:
        self._request_row(logical_request_id)
        llm_dispatches.require_hash("pacing_hash", pacing_hash)
        llm_dispatches.require_nonnegative_int("global_interval_ms", global_interval_ms)
        llm_dispatches.require_nonnegative_int("model_interval_ms", model_interval_ms)
        payload = {
            "pacing_hash": pacing_hash, "prior_global_start_at": prior_global_start_at, "prior_model_start_at": prior_model_start_at,
            "next_eligible_at": next_eligible_at, "global_interval_ms": global_interval_ms, "model_interval_ms": model_interval_ms}
        event_id = "pacing:" + payload_fingerprint({"logical_request_id": logical_request_id, **payload}, version=self.fingerprint_version())
        with self._transaction():
            llm_dispatches.append_dispatch_event(self._conn, event_id=event_id, logical_request_id=logical_request_id, dispatch_attempt_id=None, event_type="pacing_wait", payload=payload, created_at=_now())

    def resume_snapshot(self) -> dict[str, Any]:
        """Rebuild and validate facts solely from structured rows and events."""
        with self._lock:
            requests, candidates, candidate_events, candidate_state, rejections = self._semantic_resume()
            attempts, dispatch_events, leases, pacing = self._dispatch_resume(requests)
            terminals = self._rows("verification_pair_state", "claim_id, ref_id, scope", where="status != 'open'")
            return {
                "candidates": list(candidates.values()), "candidate_events": candidate_events,
                "candidate_state": candidate_state, "logical_requests": list(requests.values()), "jury1_rejections": rejections,
                "dispatch_attempts": attempts, "dispatch_leases": leases, "dispatch_events": dispatch_events, "raw_call_count": sum(
                    event["event_type"] == "started" for event in dispatch_events),
                "pacing": pacing, "terminals": terminals}

    def resume_events(
        self,
        snapshot: dict[str, Any],
        *,
        claim_id: str,
        ref_id: str,
        scope: str,
    ) -> tuple[ControllerEvent, ...]:
        """Project one pair's structured facts into deterministic replay events."""
        return project_resume_events(snapshot, (claim_id, ref_id, scope))

    def completed_answers(
        self, snapshot: dict[str, Any], *, claim_id: str, ref_id: str, scope: str,
    ) -> dict[str, dict[str, Any]]:
        identity = (claim_id, ref_id, scope)
        requests = {row["logical_request_id"] for row in snapshot["logical_requests"] if (row["claim_id"], row["ref_id"], row["scope"]) == identity}
        attempts = {row["dispatch_attempt_id"]: row["logical_request_id"] for row in snapshot["dispatch_attempts"]}
        return {attempts[event["dispatch_attempt_id"]]: event["payload"] for event in snapshot["dispatch_events"] if event["event_type"] == "completed" and attempts.get(event["dispatch_attempt_id"]) in requests}

    def completed_answer(self, logical_request_id: str) -> dict[str, Any] | None:
        """Return the one durable completion for a shared logical request."""
        snapshot = self.resume_snapshot()
        attempts = {
            row["dispatch_attempt_id"]: row["logical_request_id"]
            for row in snapshot["dispatch_attempts"]
        }
        completed = [
            event["payload"]
            for event in snapshot["dispatch_events"]
            if event["event_type"] == "completed"
            and attempts.get(event["dispatch_attempt_id"]) == logical_request_id
        ]
        if len(completed) > 1:
            raise ValueError("logical request has multiple completed answers")
        return completed[0] if completed else None

    def technical_failure_count(self, logical_request_id: str) -> int:
        """Count durable failed attempts for one shared logical request."""
        snapshot = self.resume_snapshot()
        attempts = {
            row["dispatch_attempt_id"]: row["logical_request_id"]
            for row in snapshot["dispatch_attempts"]
        }
        return sum(
            event["event_type"] in {"failed", "abandoned"}
            and attempts.get(event["dispatch_attempt_id"]) == logical_request_id
            for event in snapshot["dispatch_events"]
        )

    def technical_failure_counts(
        self, snapshot: dict[str, Any], *, claim_id: str, ref_id: str,
        scope: str,
    ) -> dict[tuple[str, int], int]:
        return project_technical_failure_counts(
            snapshot, (claim_id, ref_id, scope)
        )

    def _semantic_resume(self):
        version = self.fingerprint_version()
        requests = {row["logical_request_id"]: llm_dispatches.decode_request(row, conn=self._conn)
                    for row in self._rows("llm_logical_requests", "created_at, logical_request_id")}
        candidates = {row["candidate_id"]: verification_candidates.decode_candidate(row, conn=self._conn)
                      for row in self._rows("verification_candidates", "candidate_cycle, candidate_id")}
        for candidate in candidates.values():
            expected = candidate_fingerprint(outcome=candidate["outcome"], evidence=tuple(candidate["evidence"]), outcome_fields=candidate["outcome_fields"], grounded=_candidate_grounding(candidate), version=version)
            if candidate["fingerprint"] != expected:
                raise ValueError("stored candidate fingerprint mismatch")
        for request in requests.values():
            if request["payload_hash"] != payload_fingerprint(request["payload"], version=version):
                raise ValueError("stored logical request payload hash mismatch")
            self._require_prompt_hash(request)
        verification_candidates.validate_links(requests, candidates)
        candidate_events = self._event_rows("verification_candidate_events")
        for event in candidate_events:
            verification_candidates.validate_event_input(
                event_id=event["event_id"], event_type=event["event_type"],
                payload=event["payload"])
        verification_candidates.validate_event_links(candidate_events, requests)
        candidate_state = verification_candidates.candidate_state(candidate_events, candidates)
        return requests, candidates, candidate_events, candidate_state, jury1_rejections.validate_resume(self._conn, requests)

    def _dispatch_resume(self, requests):
        attempts = self._rows("llm_dispatch_attempts", "created_at, dispatch_attempt_id")
        dispatch_events = self._event_rows("llm_dispatch_events")
        leases = self._rows("llm_dispatch_leases", "logical_request_id")
        llm_dispatches.validate_resume(requests, attempts, dispatch_events, leases)
        pacing = {event["logical_request_id"]: event["payload"]
                  for event in dispatch_events if event["event_type"] == "pacing_wait"}
        return attempts, dispatch_events, leases, pacing

    def ensure_pair(self, *, claim_id: str, ref_id: str, scope: str) -> None:
        with self._lock:
            self._repository.ensure_verification_pair(
                claim_id=claim_id, ref_id=ref_id, scope=scope)

    def publish_terminal(
        self,
        *,
        claim_id: str,
        ref_id: str,
        scope: str,
        status: str,
        outcome: str | None,
        cause: str | None,
        candidate_id: str | None,
    ) -> bool:
        """Delegate terminal publication exclusively to the established CAS."""
        with self._lock:
            return self._repository.claim_verification_pair_terminal(
                claim_id=claim_id, ref_id=ref_id, scope=scope, status=status,
                outcome=outcome, cause=cause, call_id=candidate_id)

    def _candidate_row(self, candidate_id: object) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM verification_candidates WHERE candidate_id = ?",
                (candidate_id,)).fetchone()
        if row is None:
            raise ValueError("verification candidate is missing")
        return row

    def _request_row(self, logical_request_id: object) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM llm_logical_requests WHERE logical_request_id = ?",
                (logical_request_id,)).fetchone()
        if row is None:
            raise ValueError("logical request is missing")
        return row

    def _rows(
        self,
        table: str,
        order: str,
        *,
        where: str | None = None,
    ) -> list[dict[str, Any]]:
        clause = f" WHERE {where}" if where else ""
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                f"SELECT * FROM {table}{clause} ORDER BY {order}")]

    def _event_rows(self, table: str) -> list[dict[str, Any]]:
        rows = self._rows(table, "created_at, event_id")
        if table == "verification_candidate_events":
            return [verification_candidates.decode_candidate_event(
                row, conn=self._conn,
            ) for row in rows]
        return [llm_dispatches.decode_event(row, conn=self._conn) for row in rows]

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            writer = getattr(self._repository, "_write_transaction", None)
            if callable(writer):
                with writer():
                    yield
            else:
                with self._conn:
                    yield

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLedger:
    """Persist controller facts for one immutable claim/reference pair."""

    def __init__(
        self,
        repository: ClaimEvidenceRunRepository,
        pair_id: str,
        task: dict[str, Any],
    ) -> None:
        self._repo, self._pair, self._task = repository, pair_id, task

    def append(self, event: ControllerEvent) -> None:
        candidate_id = self._candidate_id(
            event.candidate_cycle, event.candidate_fingerprint)
        terminal_candidate_id = candidate_id
        if event.terminal is not None and event.terminal.representative is not None:
            representative = event.terminal.representative
            terminal_candidate_id = self._candidate_id(
                representative.cycle, representative.fingerprint)
        is_flow_origin = (
            event.request_id is not None
            and self._repo._request_row(event.request_id)["stage"]
            in {
                "support_gate", "full_support_gate", "contrary_gate",
                "topic_gate", "explanation_evidence",
            }
        )
        if event.candidate is not None and is_flow_origin:
            self._append_candidate(
                event.candidate, candidate_id, event.request_id
            )
        rejection = jury1_rejections.controller_event_fact(kind=event.kind, request_id=event.request_id, candidate=event.candidate, cause=event.cause)
        if rejection is not None:
            self._repo.append_jury1_rejection(event_id=f"{self._pair}:jury1-rejection:{event.candidate_cycle}", logical_request_id=rejection[0], state_cause=rejection[1], cause=rejection[2])
        if event.jury2_decision is not None and candidate_id is not None:
            accepted = event.jury2_decision.passages_fit_claim
            kind = "jury2_yes" if accepted else "jury2_no"
            self._event(candidate_id, kind, {
                "logical_request_id": event.request_id,
                "jury2_payload_hash": self._repo.jury2_payload_hash(event.request_id or "", candidate_id),
                "answer": accepted, "reason": event.jury2_decision.reason,
                "provider_confidence": event.jury2_decision.provider_confidence})
            if not accepted and event.terminal is None:
                self._event(candidate_id, "requeued", {"cause": "jury2_rejected"})
        if (
            event.kind == "jury2_provider_uncertain"
            and event.terminal is None
            and candidate_id is not None
        ):
            self._event(candidate_id, "requeued", {"cause": "jury2_provider_uncertain"})
        if event.kind == "exhausted:jury2_technical" and candidate_id is not None:
            self._event(candidate_id, "jury2_technical_exhausted", {
                "logical_request_id": event.request_id,
                "jury2_payload_hash": self._repo.jury2_payload_hash(event.request_id or "", candidate_id),
                "failure_cause": event.cause or "provider_failure"})
        if event.terminal is not None and terminal_candidate_id is not None:
            self._event(terminal_candidate_id, "terminal", {
                "resolution": event.terminal.resolution, "assurance": event.terminal.assurance})

    def _append_candidate(
        self,
        candidate: CandidateRecord,
        candidate_id: str | None,
        origin_id: str | None,
    ) -> None:
        if candidate_id is None or origin_id is None:
            raise ValueError("candidate identity is missing")
        origin = self._repo._request_row(origin_id)
        decision = candidate.decision
        record = {
            "outcome": decision.outcome, "explanation": decision.explanation,
            "evidence": list(decision.evidence),
            "outcome_fields": {
                "claim_hash": hashlib.sha256(
                    self._task["claim_evidence_payload"]["claim"].encode()).hexdigest(),
                "source_hash": origin["source_hash"],
                "supported_part": decision.supported_part,
                "incompatible_proposition": decision.incompatible_proposition,
                "reason": decision.reason,
                "provider_confidence": decision.provider_confidence},
            "grounded": [{
                "text": row.text, "raw_start": row.raw_start, "raw_end": row.raw_end, "span_id": row.span_id,
                "source_hash": row.source_hash, "match_mode": row.match_mode,
                "score": row.score} for row in candidate.grounded],
            "fingerprint": candidate.fingerprint,
            **{name: origin[name] for name in verification_candidates.HASH_FIELDS}}
        self._repo.append_candidate(candidate_id=candidate_id, claim_id=self._task["claim_id"], ref_id=self._task["ref_id"], scope=self._task.get("scope", ""),
                                    candidate_cycle=candidate.cycle, origin_logical_request_id=origin_id, record=record)
        self._event(candidate_id, "guard_accepted", {"guard_result": {"accepted": True}, "cause": None}, suffix="guard")

    def _event(
        self,
        candidate_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        suffix: str | None = None,
    ) -> None:
        self._repo.append_candidate_event(
            event_id=f"{candidate_id}:{suffix or kind}",
            candidate_id=candidate_id, event_type=kind, payload=payload)

    def _candidate_id(
        self,
        cycle: int,
        candidate_fingerprint: str | None,
    ) -> str | None:
        return (f"{self._pair}:candidate:{cycle}:{candidate_fingerprint}"
                if candidate_fingerprint else None)


def _candidate_grounding(record: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    fields = ("span_id", "raw_start", "raw_end", "text", "source_hash")
    return tuple({name: item[name] for name in fields if name in item} for item in record.get("grounded", []))
