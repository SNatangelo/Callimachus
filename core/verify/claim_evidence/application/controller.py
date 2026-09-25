# core/verify/claim_evidence/application/controller.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Thin action executor over the pure claim-evidence state machine."""
import hashlib

from ..domain.state_machine import (
    StateMachineError,
    admit_jury1,
    apply_jury2,
    cancel,
    jury1_technical_failure,
    jury2_technical_failure,
    jury2_provider_uncertain,
    provider_uncertain,
    make_candidate,
    reject_jury1,
    start,
)
from ..domain.types import (
    TECHNICAL_FAILURE_CAUSES,
    ControllerEvent,
    GroundedEvidence,
    Jury2Decision,
    LogicalRequest,
    MachineState,
    MachineTransition,
    StateMachinePolicy,
    TechnicalFailure,
)
from ..domain.jury1_flow import MappedJury1Decision
from ..domain.replies import Jury2Answer
from ..domain.fingerprint import provider_prompt_fingerprint
from ..evidence.grounding import GroundingError, ground_jury1_span_decision
from .ports import (
    CancellationPort,
    FatalDispatchError,
    JuryDispatchPort,
    LedgerPort,
    RequestFactoryPort,
)
from .jury1_flow import Jury1FlowRunner


class ControllerError(RuntimeError):
    """Application dependencies violated the action-execution contract."""


class ClaimEvidenceController:
    def __init__(
        self,
        requests: RequestFactoryPort,
        dispatch: JuryDispatchPort,
        ledger: LedgerPort,
        cancellation: CancellationPort,
        *,
        fingerprint_version: str,
    ) -> None:
        self._requests = requests
        self._dispatch = dispatch
        self._ledger = ledger
        self._cancellation = cancellation
        self._fingerprint_version = fingerprint_version
        self._jury1_flow = Jury1FlowRunner(requests, dispatch, cancellation)

    def run(
        self,
        policy: StateMachinePolicy,
        *,
        claim: str,
        source_text: str,
        context: object,
        initial_state: MachineState | None = None,
    ) -> MachineState:
        if not isinstance(claim, str) or not claim.strip():
            raise ControllerError("claim must be non-empty")
        if not isinstance(source_text, str) or not source_text:
            raise ControllerError("source text must be non-empty")
        if initial_state is not None and (
            not isinstance(initial_state, MachineState)
            or initial_state.policy != policy
        ):
            raise ControllerError("resume state differs from the active policy")
        state = initial_state or start(policy)
        jury2_request: LogicalRequest | None = None
        while state.next_action != "terminal":
            if self._cancellation.cancelled():
                state = self._record(cancel(state), None).state
                break
            if state.next_action == "jury1":
                state = self._jury1(state, claim, source_text, context)
                jury2_request = None
            else:
                state, jury2_request = self._jury2(state, jury2_request)
        return state

    def _jury1(
        self,
        state: MachineState,
        claim: str,
        source_text: str,
        context: object,
    ) -> MachineState:
        result = self._jury1_flow.run(
            state.candidate_cycles + 1, state.policy.jury1_technical_cap
        )
        if result.cancelled:
            return self._record(cancel(state), result.request).state
        if result.technical_cause is not None:
            transition = jury1_technical_failure(state)
            while transition.state.next_action != "terminal":
                transition = jury1_technical_failure(transition.state)
            return self._record(
                transition, result.request, result.technical_cause
            ).state
        if result.rejection_cause is not None:
            transition = reject_jury1(state, result.rejection_cause)
            return self._record(
                transition, result.request, result.rejection_cause
            ).state
        if result.mapped is None or result.request is None:
            raise ControllerError("Jury1 flow returned no result")
        if (
            result.mapped.decision.outcome == "non_decidable"
            and result.mapped.decision.reason == "provider_uncertain"
        ):
            return self._record(
                provider_uncertain(state, "jury1"), result.request,
                "provider_uncertain",
            ).state
        transition, cause = self._admit_or_reject(
            state, result.mapped, claim, source_text, context
        )
        return self._record(transition, result.request, cause).state

    def _admit_or_reject(
        self,
        state: MachineState,
        mapped: MappedJury1Decision,
        claim: str,
        source_text: str,
        context: object,
    ) -> tuple[MachineTransition, str | None]:
        decision = mapped.decision
        if decision.outcome == "non_decidable" and decision.reason == "retrieval_limit" and getattr(context, "mode", None) == "full_text":
            return reject_jury1(state, "schema_invalid"), "schema_invalid"
        try:
            grounded = ground_jury1_span_decision(
                decision, mapped.source_span_ids, source_text, context
            )
            evidence = tuple(
                GroundedEvidence(
                    item.text,
                    item.raw_start,
                    item.raw_end,
                    item.span_id,
                    item.source_hash,
                    item.match_mode,
                    item.score,
                )
                for item in grounded.quotes
            )
            candidate = make_candidate(
                state,
                decision,
                evidence,
                claim_hash=_sha256(claim),
                source_hash=_sha256(source_text),
                fingerprint_version=self._fingerprint_version,
            )
        except GroundingError as exc:
            return reject_jury1(state, "grounding_invalid"), f"grounding/{exc.code}"
        except StateMachineError:
            return reject_jury1(state, "provenance_invalid"), "provenance_invalid"
        return admit_jury1(state, candidate), None

    def _jury2(
        self,
        state: MachineState,
        request: LogicalRequest | None,
    ) -> tuple[MachineState, LogicalRequest | None]:
        candidate = state.current_candidate
        if candidate is None:
            raise ControllerError("Jury2 action has no candidate")
        request = request or self._requests.jury2_request(candidate)
        _validate_request(
            request, "jury2", candidate.cycle, candidate.fingerprint
        )
        reply = self._safe_dispatch(request)
        if isinstance(reply, TechnicalFailure):
            transition = jury2_technical_failure(state)
            cause = _technical_cause(reply, request)
            return self._record(transition, request, cause).state, request
        if not isinstance(reply, Jury2Answer) or not _answer_matches(reply, request):
            transition = jury2_technical_failure(state)
            return self._record(transition, request, "provenance_invalid").state, request
        try:
            transition = apply_jury2(state, reply.decision)
        except StateMachineError:
            transition = jury2_technical_failure(state)
            return self._record(transition, request, "provider_failure").state, request
        if reply.decision.provider_uncertain:
            return self._record(transition, request, "provider_uncertain").state, None
        return self._record(
            transition,
            request,
            jury2_decision=reply.decision,
        ).state, None

    def _safe_dispatch(
        self,
        request: LogicalRequest,
    ) -> Jury2Answer | TechnicalFailure | None:
        try:
            return self._dispatch.dispatch(request)
        except FatalDispatchError:
            raise
        except Exception:
            return TechnicalFailure(
                request.stage,
                request.logical_request_id,
                request.payload_fingerprint,
                "transport",
            )

    def _record(
        self,
        transition: MachineTransition,
        request: LogicalRequest | None,
        cause: str | None = None,
        jury2_decision: Jury2Decision | None = None,
    ) -> MachineTransition:
        request_candidate = next(
            (item for item in reversed(transition.state.candidates)
             if request is not None
             and item.cycle == request.candidate_cycle
             and (request.candidate_fingerprint is None
                  or item.fingerprint == request.candidate_fingerprint)),
            None,
        )
        candidate = (
            request_candidate
            or transition.state.current_candidate
            or (
                transition.state.terminal.representative
                if request is None and transition.state.terminal
                else None
            )
        )
        self._ledger.append(ControllerEvent(
            kind=transition.event,
            candidate_cycle=(candidate.cycle if candidate
                             else transition.state.candidate_cycles),
            request_id=request.logical_request_id if request else None,
            payload_fingerprint=request.payload_fingerprint if request else None,
            candidate_fingerprint=(
                candidate.fingerprint
                if candidate
                else request.candidate_fingerprint
                if request
                else None
            ),
            cause=cause,
            terminal=transition.state.terminal,
            candidate=candidate,
            jury2_decision=jury2_decision,
        ))
        return transition


def resume_controller(
    policy: StateMachinePolicy,
    events: tuple[ControllerEvent, ...],
) -> MachineState:
    """Replay validated semantic facts without provider or repository access."""
    state = start(policy)
    for event in events:
        if event.kind == "jury1_technical":
            transition = jury1_technical_failure(state)
        elif event.kind.startswith("jury1_rejected:"):
            transition = reject_jury1(state, event.kind.split(":", 1)[1])
        elif event.kind == "jury1_candidate":
            if event.candidate is None:
                raise ControllerError("resume candidate is missing")
            transition = admit_jury1(state, event.candidate)
        elif event.kind == "jury2_technical":
            transition = jury2_technical_failure(state)
        elif event.kind == "jury2_provider_uncertain":
            transition = jury2_provider_uncertain(state)
        elif event.kind == "jury2_decision":
            if event.jury2_decision is None:
                raise ControllerError("resume Jury2 decision is missing")
            transition = apply_jury2(state, event.jury2_decision)
        elif event.kind == "cancelled":
            transition = cancel(state)
        else:
            raise ControllerError("resume event is invalid")
        state = transition.state
    return state


def validate_dispatch_resume(snapshot: dict[str, object], identity: tuple[str, str, str] | None = None) -> None:
    """Reject ambiguous live leases before a resumed provider dispatch."""
    requests = {row["logical_request_id"]: (row["claim_id"], row["ref_id"], row["scope"])
                for row in snapshot["logical_requests"]}  # type: ignore[index]
    attempts = {
        row["dispatch_attempt_id"]: row["logical_request_id"]
        for row in snapshot["dispatch_attempts"]  # type: ignore[index]
    }
    terminal = {
        event["dispatch_attempt_id"]
        for event in snapshot["dispatch_events"]  # type: ignore[index]
        if event["event_type"] in {"completed", "failed", "abandoned"}
    }
    for lease in snapshot["dispatch_leases"]:  # type: ignore[index]
        attempt = lease["dispatch_attempt_id"]
        request_id = attempts.get(attempt)
        if request_id is None or request_id not in requests:
            raise ControllerError("dispatch lease references an unknown attempt")
        if identity is not None and requests[request_id] != identity:
            continue
        if lease["status"] == "live" or (
            lease["status"] == "terminal" and attempt not in terminal
        ):
            raise ControllerError("live or inconsistent dispatch cannot be resumed safely")


def _validate_request(
    request: LogicalRequest,
    stage: str,
    cycle: int,
    candidate_fingerprint: str | None,
) -> None:
    if (
        not isinstance(request, LogicalRequest)
        or request.stage != stage
        or request.candidate_cycle != cycle
        or request.candidate_fingerprint != candidate_fingerprint
        or not isinstance(request.logical_request_id, str) or not request.logical_request_id
        or not isinstance(request.payload, bytes) or not request.payload
        or not isinstance(request.system_prompt, str) or not request.system_prompt
        or not _is_hash(request.payload_fingerprint) or provider_prompt_fingerprint(request.system_prompt, request.payload) != request.payload_fingerprint
    ):
        raise ControllerError("request factory returned invalid provenance")


def _answer_matches(
    answer: Jury2Answer,
    request: LogicalRequest,
) -> bool:
    return (
        answer.logical_request_id == request.logical_request_id
        and answer.payload_fingerprint == request.payload_fingerprint
    )


def _technical_cause(failure: TechnicalFailure, request: LogicalRequest) -> str:
    if (
        failure.stage != request.stage
        or failure.logical_request_id != request.logical_request_id
        or failure.payload_fingerprint != request.payload_fingerprint
    ):
        return "provenance_invalid"
    return (
        failure.cause
        if failure.cause in TECHNICAL_FAILURE_CAUSES
        else "provider_failure"
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
