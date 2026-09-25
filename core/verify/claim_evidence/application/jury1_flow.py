# core/verify/claim_evidence/application/jury1_flow.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Dispatch independent holistic Jury1 gates without sharing model answers."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from ..domain.fingerprint import provider_prompt_fingerprint
from ..domain.jury1_flow import (
    JURY1_FLOW_STAGES,
    Jury1FlowError,
    MappedJury1Decision,
    evaluate_jury1_flow,
)
from ..domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    TopicGateAnswer,
)
from ..domain.types import (
    TECHNICAL_FAILURE_CAUSES,
    LogicalRequest,
    TechnicalFailure,
)
from .ports import (
    CancellationPort,
    FatalDispatchError,
    JuryDispatchPort,
    RequestFactoryPort,
)


_EXPECTED_ANSWERS = {
    "support_gate": SupportGateAnswer,
    "full_support_gate": FullSupportGateAnswer,
    "contrary_gate": ContraryGateAnswer,
    "topic_gate": TopicGateAnswer,
    "explanation_evidence": ExplanationEvidenceAnswer,
}
_SHARED_PAYLOAD_FIELDS = (
    "claim",
    "claim_context",
    "citation_marker",
    "source_spans",
    "source_hash",
    "cited_source_mode",
)


@dataclass(frozen=True, slots=True)
class Jury1FlowRun:
    mapped: MappedJury1Decision | None
    request: LogicalRequest | None
    rejection_cause: str | None
    technical_cause: str | None
    cancelled: bool = False


class Jury1FlowRunner:
    def __init__(
        self,
        requests: RequestFactoryPort,
        dispatch: JuryDispatchPort,
        cancellation: CancellationPort,
    ) -> None:
        self._requests = requests
        self._dispatch = dispatch
        self._cancellation = cancellation

    def run(
        self,
        candidate_cycle: int,
        technical_cap: int,
    ) -> Jury1FlowRun:
        answers: dict[str, object] = {}
        stage = "support_gate"
        base_payload: Mapping[str, Any] | None = None
        determined_outcome: str | None = None

        while True:
            request = self._requests.jury1_flow_request(
                stage,
                candidate_cycle,
                determined_outcome=determined_outcome,
            )
            _validate_request(request, stage, candidate_cycle)
            current_payload = _payload(request)
            if base_payload is None:
                base_payload = current_payload
            elif _shared_payload(current_payload) != _shared_payload(base_payload):
                raise FatalDispatchError(
                    "Jury1 immutable payload differs between stages"
                )

            reply, cause, cancelled = self._dispatch_stage(request, technical_cap)
            if cancelled:
                return Jury1FlowRun(None, request, None, None, True)
            if reply is None:
                return Jury1FlowRun(
                    None,
                    request,
                    None,
                    cause or "provider_failure",
                )
            evaluation, invalid_cause = _evaluate_reply(
                stage, reply, request, base_payload, answers
            )
            if invalid_cause is not None:
                return Jury1FlowRun(None, request, invalid_cause, None)
            if evaluation is None:
                raise FatalDispatchError("Jury1 reply produced no evaluation")
            if evaluation.rejection_cause is not None:
                return Jury1FlowRun(
                    None,
                    request,
                    evaluation.rejection_cause,
                    None,
                )
            if evaluation.mapped is not None:
                return Jury1FlowRun(
                    evaluation.mapped,
                    request,
                    None,
                    None,
                )
            if evaluation.next_stage not in JURY1_FLOW_STAGES:
                raise FatalDispatchError("Jury1 flow produced no next stage")
            stage = evaluation.next_stage
            determined_outcome = evaluation.determined_outcome

    def _dispatch_stage(
        self,
        request: LogicalRequest,
        technical_cap: int,
    ) -> tuple[object | None, str | None, bool]:
        prior = self._requests.prior_technical_failures(
            request.stage,
            request.candidate_cycle,
        )
        if (
            type(prior) is not int
            or prior < 0
            or type(technical_cap) is not int
            or technical_cap <= 0
        ):
            raise FatalDispatchError("Jury1 flow retry state is invalid")

        cause: str | None = None
        for _attempt in range(prior, technical_cap):
            if self._cancellation.cancelled():
                return None, None, True
            reply = self._safe_dispatch(request)
            if not isinstance(reply, TechnicalFailure):
                return reply, None, False
            cause = _technical_cause(reply, request)
        return None, cause, False

    def _safe_dispatch(self, request: LogicalRequest) -> object:
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


def _validate_request(
    request: LogicalRequest,
    stage: str,
    cycle: int,
) -> None:
    if (
        not isinstance(request, LogicalRequest)
        or request.stage != stage
        or request.candidate_cycle != cycle
        or request.candidate_fingerprint is not None
        or not request.logical_request_id
        or not request.payload
        or not request.system_prompt
        or provider_prompt_fingerprint(
            request.system_prompt,
            request.payload,
        )
        != request.payload_fingerprint
    ):
        raise FatalDispatchError("Jury1 flow request provenance is invalid")


def _payload(request: LogicalRequest) -> Mapping[str, Any]:
    try:
        value = json.loads(request.payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Jury1FlowError("Jury1 flow request payload is invalid") from exc
    if not isinstance(value, dict):
        raise Jury1FlowError("Jury1 flow request payload is invalid")
    return value


def _shared_payload(payload: Mapping[str, Any]) -> tuple[object, ...]:
    try:
        return tuple(payload[field] for field in _SHARED_PAYLOAD_FIELDS)
    except KeyError as exc:
        raise FatalDispatchError(
            "Jury1 immutable payload field is missing"
        ) from exc


def _evaluate_reply(
    stage: str,
    reply: object,
    request: LogicalRequest,
    base_payload: Mapping[str, Any],
    answers: dict[str, object],
) -> tuple[Any | None, str | None]:
    if not isinstance(reply, _EXPECTED_ANSWERS[stage]):
        return None, "schema_invalid"
    if not _answer_matches(reply, request):
        return None, "provenance_invalid"
    answers[stage] = reply.decision
    try:
        return evaluate_jury1_flow(base_payload, answers), None
    except Jury1FlowError:
        return None, "schema_invalid"


def _answer_matches(answer: object, request: LogicalRequest) -> bool:
    return (
        getattr(answer, "logical_request_id", None)
        == request.logical_request_id
        and getattr(answer, "payload_fingerprint", None)
        == request.payload_fingerprint
    )


def _technical_cause(
    failure: TechnicalFailure,
    request: LogicalRequest,
) -> str:
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
