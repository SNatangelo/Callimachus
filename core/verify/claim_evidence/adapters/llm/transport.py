# core/verify/claim_evidence/adapters/llm/transport.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""One provider-visible attempt with typed, non-semantic failure mapping."""
from dataclasses import replace
import json
import math
import time
from typing import Any

from core.verify.backends._chat_transport import (
    reset_trace_context,
    set_trace_context,
)
from core.verify.backends.errors import (
    AuthError,
    BackendError,
    BackendTimeoutError,
    ConfigError,
    LaneError,
    RateLimitError,
    TransientError,
)

from ...contracts.jury1_flow import (
    JURY1_PROMPT_SPEC,
    build_jury1_flow_payload,
    parse_support_gate_response,
    parse_contrary_gate_response,
    parse_explanation_evidence_response,
    parse_full_support_gate_response,
    parse_topic_gate_response,
)
from ...contracts.jury2 import (
    JURY2_PROMPT_SPEC,
    build_jury2_payload,
    parse_jury2_response,
)
from ...contracts.schema import ContractError
from ...domain.fingerprint import (
    fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
)
from ...domain.jury1_flow import JURY1_FLOW_STAGES
from ...domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    Jury2Answer,
    FullSupportGateAnswer,
    TopicGateAnswer,
    normalise_jury_reply,
)
from ...domain.types import (
    CandidateRecord,
    DispatchAssignment,
    Jury2Decision,
    LogicalRequest,
    TechnicalFailure,
    technical_failure,
)
from ...application.ports import FatalDispatchError, JuryReply
from core.verify.source_spans import build_catalog
from .answer_codec import answer_hash, durable_answer, persisted_answer
from .credentials import CredentialError, CredentialResolver
from .registry import ProviderRegistry, RegistryError


class ClaimEvidenceTransport:
    def __init__(
        self,
        registry: ProviderRegistry,
        credentials: CredentialResolver,
        *,
        provider_confidence_threshold: float | None = None,
    ) -> None:
        self._registry = registry
        self._credentials = credentials
        self._provider_confidence_threshold = provider_confidence_threshold

    def dispatch(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
    ) -> JuryReply:
        if not _valid_boundary(request, assignment):
            return _failure(request, assignment, "provider_failure")
        trace_token = set_trace_context(
            logical_request_id=request.logical_request_id,
            dispatch_attempt_id=assignment.dispatch_attempt_id,
            jury_stage=request.stage,
            provider=assignment.provider,
            model=assignment.model,
            credential_alias=assignment.credential_alias,
            credential_fingerprint=assignment.credential_fingerprint,
            lane_id=":".join(assignment.lane_id),
            started_at_ms=assignment.started_at_ms,
        )
        try:
            return self._call(request, assignment)
        finally:
            reset_trace_context(trace_token)

    def _call(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
    ) -> JuryReply:
        raw: Any = _MISSING_RESPONSE
        try:
            adapter = self._registry.get(assignment.provider)
            secret = self._credentials.resolve(
                assignment.credential_alias,
                assignment.credential_fingerprint,
            )
            raw = adapter.call(
                request.system_prompt,
                request.payload.decode("utf-8"),
                assignment.model,
                secret,
            )
            decision = self._parse_decision(request, assignment, raw)
        except (CredentialError, AuthError):
            return _failure(request, assignment, "credential_invalid", status=401)
        except (RegistryError, LaneError, ConfigError):
            return _failure(request, assignment, "lane_unavailable")
        except (BackendTimeoutError, TimeoutError):
            return _failure(request, assignment, "timeout")
        except RateLimitError as exc:
            return _failure(
                request,
                assignment,
                "rate_limited",
                retry_after=_retry_after(exc.retry_after),
                status=429,
            )
        except TransientError:
            return _failure(request, assignment, "transport")
        except UnicodeDecodeError:
            return _protocol_failure(
                request, assignment, "invalid_utf8_payload"
            )
        except ContractError as exc:
            return _protocol_failure(request, assignment, exc.code, raw)
        except BackendError as exc:
            cause = "transport" if exc.retryable else "provider_failure"
            return _failure(request, assignment, cause)
        except Exception:
            return _failure(request, assignment, "transport")
        answer_type = {
            "support_gate": SupportGateAnswer,
            "full_support_gate": FullSupportGateAnswer,
            "contrary_gate": ContraryGateAnswer,
            "topic_gate": TopicGateAnswer,
            "explanation_evidence": ExplanationEvidenceAnswer,
            "jury2": Jury2Answer,
        }[request.stage]
        return answer_type(request.logical_request_id, request.payload_fingerprint, decision)

    def _parse_decision(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
        raw: Any,
    ) -> Any:
        adapter = self._registry.get(assignment.provider)
        if adapter.decode is not None:
            return adapter.decode(
                request.stage, raw, request.payload,
                self._provider_confidence_threshold, assignment.model,
            )
        parser = {
            "support_gate": parse_support_gate_response,
            "full_support_gate": parse_full_support_gate_response,
            "contrary_gate": parse_contrary_gate_response,
            "topic_gate": parse_topic_gate_response,
            "explanation_evidence": parse_explanation_evidence_response,
            "jury2": parse_jury2_response,
        }[request.stage]
        return parser(raw)

class AuditedTransport:
    """Persist one physical attempt around an injected provider transport."""

    def __init__(
        self,
        transport: Any,
        run_repository: Any,
        runtime_repository: Any,
        *,
        selection_hash: str,
        pacing_hash: str,
        control_payload: Any | None = None,
    ) -> None:
        self._transport = transport
        self._run = run_repository
        self._runtime = runtime_repository
        self._selection_hash = selection_hash
        self._pacing_hash = pacing_hash
        self._control_payload = control_payload
        self._fingerprint_version = run_repository.fingerprint_version()

    def dispatch(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
    ) -> JuryReply:
        try:
            self._runtime.replay_scheduler_controls()
            self._runtime.replay_dispatch_observations()
            self._run.lease_dispatch(
                _attempt_record(
                    request, assignment, self._run.request_payload_hash(
                        request.logical_request_id),
                    self._selection_hash, self._pacing_hash)
            )
            self._run.start_dispatch(
                logical_request_id=request.logical_request_id,
                dispatch_attempt_id=assignment.dispatch_attempt_id,
            )
        except Exception as exc:
            raise FatalDispatchError(
                "dispatch audit could not acquire a unique live lease"
            ) from exc
        started = time.perf_counter()
        try:
            result = self._transport.dispatch(request, assignment)
        except Exception:
            result = technical_failure(
                request, "transport",
                dispatch_id=assignment.dispatch_attempt_id)
        result = normalise_jury_reply(request, assignment, result)
        if isinstance(result, TechnicalFailure):
            result = replace(result, observed_at_ms=int(time.time() * 1000))
        control = self._control_payload(assignment, result) if callable(self._control_payload) else None
        try:
            self._finish(
                request, assignment, result,
                (time.perf_counter() - started) * 1000, control)
            self._runtime.replay_scheduler_controls()
            self._runtime.replay_dispatch_observations()
        except Exception as exc:
            raise FatalDispatchError(
                "dispatch terminal audit or shared replay failed"
            ) from exc
        return result

    def _finish(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
        result: JuryReply,
        latency_ms: float,
        scheduler_control: dict[str, Any] | None,
    ) -> None:
        completed = not isinstance(result, TechnicalFailure)
        encoded_answer = durable_answer(result) if completed else None
        self._run.finish_dispatch(
            logical_request_id=request.logical_request_id,
            dispatch_attempt_id=assignment.dispatch_attempt_id,
            result="completed" if completed else "failed",
            technical_result="answer_received" if completed else (result.detail or result.cause),
            http_status=None if completed else result.http_status,
            latency_ms=latency_ms,
            retry_cause=None if completed else result.cause,
            answer_hash=answer_hash(
                result, version=self._fingerprint_version) if completed else None,
            retry_after_seconds=None if completed else result.retry_after_seconds,
            protocol_error_code=(
                None if completed else result.protocol_error_code
            ),
            response_hash=None if completed else result.response_hash,
            answer=encoded_answer,
            scheduler_control=scheduler_control,
        )


class ReplayDispatch:
    """Return durable completed answers before delegating to the scheduler."""

    def __init__(
        self,
        base: Any,
        answers: dict[str, dict[str, Any]],
        *,
        fingerprint_version: str,
    ) -> None:
        self._base, self._answers = base, dict(answers)
        self._fingerprint_version = fingerprint_version

    def dispatch(self, request: LogicalRequest) -> Any:
        if request.logical_request_id in self._answers:
            return persisted_answer(
                request,
                self._answers.pop(request.logical_request_id),
                version=self._fingerprint_version,
            )
        return self._base.dispatch(request)


class RunRequestFactory:
    """Encode and persist one pair's provider-neutral logical requests."""

    def __init__(
        self,
        repository: Any,
        config: Any,
        task: dict[str, Any],
        context: Any,
        source_text: str,
        pair: str,
        *,
        failure_counts: dict[tuple[str, int], int] | None = None,
    ) -> None:
        self._repo, self._task, self._context, self._pair = repository, task, context, pair
        self._config = config
        payload = task["claim_evidence_payload"]
        self._scope_payload = payload
        self._claim = payload["claim"]
        self._context_mode = context.mode
        self._failure_counts = dict(failure_counts or {})
        catalog = build_catalog(source_text)
        visible_spans = catalog.visible_spans(context.text)
        aliases = catalog.selection_aliases(visible_spans)
        self._source_spans = [
            {"span_id": alias, "text": span.text}
            for alias, span in zip(aliases, visible_spans)
        ]
        self._fingerprint_version = repository.fingerprint_version()
        self._base = {
            "source_hash": context.source_hash,
            "context_hash": context.context_hash,
            "retrieval_hash": fingerprint({
                "algorithm": context.retrieval_algorithm,
                "config": dict(context.retrieval_config),
                "ranges": [(row.span_id, row.raw_start, row.raw_end)
                           for row in context.ranges],
            }, version=self._fingerprint_version),
            "model_hash": fingerprint([
                (provider.name, provider.models) for provider in config.providers
            ], version=self._fingerprint_version),
            "policy_hash": config.execution_policy_hash,
        }

    def jury1_flow_request(
        self,
        stage: str,
        candidate_cycle: int,
        *,
        determined_outcome: str | None = None,
    ) -> LogicalRequest:
        payload = build_jury1_flow_payload(
            task_id=stage,
            cited_source_mode=self._context_mode,
            source_hash=self._context.source_hash,
            source_spans=self._source_spans,
            claim=self._scope_payload["claim"],
            claim_context=self._scope_payload["claim_context"],
            citation_marker=self._scope_payload["citation_marker"],
            determined_outcome=determined_outcome,
        )
        return self._request(stage, candidate_cycle, payload, None)

    def prior_technical_failures(
        self, stage: str, candidate_cycle: int,
    ) -> int:
        return self._failure_counts.get((stage, candidate_cycle), 0)

    def jury2_request(self, candidate: CandidateRecord) -> LogicalRequest:
        return self._request(
            "jury2", candidate.cycle,
            build_jury2_payload(self._claim, candidate), candidate
        )

    def _request(self, stage: str, cycle: int, payload: dict[str, Any],
                 candidate: CandidateRecord | None) -> LogicalRequest:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        system_prompt = self._system_prompt(stage); wire_hash = provider_prompt_fingerprint(system_prompt, encoded)
        request_id = f"{self._pair}:{stage}:{cycle}"
        candidate_id = (
            f"{self._pair}:candidate:{candidate.cycle}:{candidate.fingerprint}"
            if candidate else None
        )
        self._repo.append_logical_request({
            "logical_request_id": request_id,
            "claim_id": self._task["claim_id"],
            "ref_id": self._task["ref_id"],
            "scope": self._task.get("scope", ""),
            "candidate_id": candidate_id, "candidate_cycle": cycle,
            "stage": stage, "payload": payload,
            "payload_hash": payload_fingerprint(payload, version=self._fingerprint_version),
            **self._base, "prompt_hash": wire_hash,
        })
        return LogicalRequest(
            stage, request_id, encoded, wire_hash, cycle, system_prompt,
            candidate.fingerprint if candidate else None
        )

    def _system_prompt(self, stage: str) -> str:
        spec = JURY1_PROMPT_SPEC if stage in JURY1_FLOW_STAGES else JURY2_PROMPT_SPEC
        prefix = "jury1_prompt_" if stage in JURY1_FLOW_STAGES else stage + "_prompt_"
        actual = tuple(getattr(self._config, prefix + field, None) for field in ("id", "version", "sha256"))
        if actual != (spec.prompt_id, spec.version, spec.sha256): raise FatalDispatchError("frozen prompt identity differs from current prompt")
        return spec.system_prompt


def _attempt_record(
    request: LogicalRequest,
    assignment: DispatchAssignment,
    payload_hash: str,
    selection_hash: str,
    pacing_hash: str,
) -> dict[str, Any]:
    return {
        "dispatch_attempt_id": assignment.dispatch_attempt_id,
        "logical_request_id": request.logical_request_id,
        "provider_id": assignment.provider,
        "model_id": assignment.model,
        "credential_id": assignment.credential_alias,
        "credential_fingerprint": assignment.credential_fingerprint,
        "lane_id": ":".join(assignment.lane_id),
        "credential_cursor": assignment.credential_cursor,
        "model_draw_index": assignment.model_draw_index,
        "selection_hash": selection_hash,
        "pacing_hash": pacing_hash,
        "payload_hash": payload_hash,
        "prior_global_start_at": _millis(assignment.prior_global_start_ms),
        "prior_model_start_at": _millis(assignment.prior_model_start_ms),
        "next_eligible_at": _millis(assignment.next_eligible_ms),
        "global_interval_ms": assignment.global_interval_ms,
        "model_interval_ms": assignment.model_interval_ms,
        "queued_at": _millis(assignment.started_at_ms),
    }


def _millis(value: int | None) -> str | None:
    return None if value is None else str(value)


def _failure(
    request: LogicalRequest,
    assignment: DispatchAssignment,
    cause: str,
    *,
    retry_after: int | None = None,
    status: int | None = None,
    detail: str | None = None,
    protocol_error_code: str | None = None,
    response_hash: str | None = None,
) -> TechnicalFailure:
    return TechnicalFailure(
        request.stage,
        request.logical_request_id,
        request.payload_fingerprint,
        cause,
        detail=detail,
        protocol_error_code=protocol_error_code,
        response_hash=response_hash,
        retry_after_seconds=retry_after,
        http_status=status,
        dispatch_attempt_id=assignment.dispatch_attempt_id or None,
    )


_MISSING_RESPONSE = object()


def _protocol_failure(
    request: LogicalRequest,
    assignment: DispatchAssignment,
    code: str,
    raw: Any = _MISSING_RESPONSE,
) -> TechnicalFailure:
    return _failure(
        request,
        assignment,
        "provider_failure",
        detail="protocol_invalid",
        protocol_error_code=code,
        response_hash=_protocol_response_hash(raw),
    )


def _protocol_response_hash(value: Any) -> str | None:
    if value is _MISSING_RESPONSE:
        return None
    try:
        return fingerprint(("provider-response-v1", value))
    except Exception:
        return None


def _retry_after(value: float | None) -> int | None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return math.ceil(value)


def _valid_boundary(
    request: LogicalRequest,
    assignment: DispatchAssignment,
) -> bool:
    if not isinstance(request, LogicalRequest) or not isinstance(
        assignment, DispatchAssignment
    ):
        return False
    expected_lane = (
        assignment.provider,
        assignment.credential_alias,
        assignment.model,
    )
    return (
        request.stage in {*JURY1_FLOW_STAGES, "jury2"}
        and bool(request.logical_request_id)
        and bool(request.payload)
        and provider_prompt_fingerprint(request.system_prompt, request.payload)
        == request.payload_fingerprint
        and bool(assignment.provider)
        and bool(assignment.credential_alias)
        and bool(assignment.model)
        and assignment.lane_id == expected_lane
        and bool(assignment.dispatch_attempt_id)
        and isinstance(assignment.started_at_ms, int)
        and not isinstance(assignment.started_at_ms, bool)
        and assignment.started_at_ms >= 0
    )
