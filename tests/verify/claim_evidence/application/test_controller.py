# tests/verify/claim_evidence/application/test_controller.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.claim_evidence.application.controller import (
    ClaimEvidenceController,
    ControllerError,
    validate_dispatch_resume,
)
from core.verify.claim_evidence.application.ports import FatalDispatchError
from core.verify.claim_evidence.contracts.jury1_flow import build_jury1_flow_payload
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    provider_prompt_fingerprint,
)
from core.verify.claim_evidence.domain.jury1_flow import (
    SupportGateDecision,
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    TopicGateDecision,
)
from core.verify.claim_evidence.domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    Jury2Answer,
    TopicGateAnswer,
)
from core.verify.claim_evidence.domain.types import (
    Jury2Decision,
    LogicalRequest,
    StateMachinePolicy,
    TechnicalFailure,
)
from core.verify.claim_evidence.evidence.context import build_effective_context
from core.verify.source_spans import build_catalog


SOURCE = (
    "Alpha evidence supports the claim.\n\n"
    "Beta evidence supports only one part."
)
CLAIM = "The claim has two parts."
CLAIM_CONTEXT = "The surrounding passage supplies the cited context."
CITATION_MARKER = "[20]"
CATALOG = build_catalog(SOURCE)
VISIBLE_SPANS = CATALOG.visible_spans(SOURCE)
SPAN_ALIASES = CATALOG.selection_aliases(VISIBLE_SPANS)
SPANS = [
    {"span_id": alias, "text": CATALOG.get(span_id).text}
    for alias, span_id in SPAN_ALIASES.items()
]
ALPHA_TEXT = "Alpha evidence supports the claim."
BETA_TEXT = "Beta evidence supports only one part."
ALPHA_CANONICAL_ID = CATALOG.span_for_range(0, len(ALPHA_TEXT)).id
ALPHA_ID = next(
    alias for alias, span_id in SPAN_ALIASES.items()
    if span_id == ALPHA_CANONICAL_ID
)
BETA_START = SOURCE.index(BETA_TEXT)
BETA_ID = CATALOG.span_for_range(BETA_START, BETA_START + len(BETA_TEXT)).id


def test_dispatch_resume_ignores_another_pairs_live_lease():
    snapshot = {
        "logical_requests": [{
            "logical_request_id": "request-1", "claim_id": "claim-1",
            "ref_id": "ref-1", "scope": "fulltext",
        }],
        "dispatch_attempts": [{
            "dispatch_attempt_id": "attempt-1",
            "logical_request_id": "request-1",
        }],
        "dispatch_events": [],
        "dispatch_leases": [{"dispatch_attempt_id": "attempt-1", "status": "live"}],
    }

    validate_dispatch_resume(snapshot, ("claim-2", "ref-2", "fulltext"))
    with pytest.raises(ControllerError, match="cannot be resumed"):
        validate_dispatch_resume(snapshot)
    with pytest.raises(ControllerError, match="cannot be resumed"):
        validate_dispatch_resume(snapshot, ("claim-1", "ref-1", "fulltext"))


class Requests:
    def __init__(self, failures=None, *, context_mode="full_text"):
        self.jury1 = []
        self.jury2 = []
        self.failures = dict(failures or {})
        self.context_mode = context_mode

    def jury1_flow_request(
        self,
        stage,
        candidate_cycle,
        *,
        determined_outcome=None,
    ):
        payload = build_jury1_flow_payload(
            task_id=stage,
            cited_source_mode=self.context_mode,
            source_hash="a" * 64,
            source_spans=SPANS,
            claim=CLAIM,
            claim_context=CLAIM_CONTEXT,
            citation_marker=CITATION_MARKER,
            determined_outcome=determined_outcome,
        )
        request = self._request(stage, candidate_cycle, payload)
        self.jury1.append(request)
        return request

    def prior_technical_failures(self, stage, candidate_cycle):
        return self.failures.get((stage, candidate_cycle), 0)

    def jury2_request(self, candidate):
        request = self._request(
            "jury2", candidate.cycle,
            {"candidate": candidate.fingerprint}, candidate.fingerprint,
        )
        self.jury2.append(request)
        return request

    @staticmethod
    def _request(stage, cycle, payload, candidate_fingerprint=None):
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        system_prompt = "test system prompt"
        return LogicalRequest(
            stage, f"{stage}-{cycle}", encoded,
            provider_prompt_fingerprint(system_prompt, encoded), cycle,
            system_prompt, candidate_fingerprint,
        )


class Dispatch:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def dispatch(self, request):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("unexpected dispatch")
        item = self.script.pop(0)
        if callable(item):
            return item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return TechnicalFailure(
                request.stage, request.logical_request_id,
                request.payload_fingerprint, item,
            )
        answer_type = {
            SupportGateDecision: SupportGateAnswer,
            FullSupportGateDecision: FullSupportGateAnswer,
            ContraryGateDecision: ContraryGateAnswer,
            TopicGateDecision: TopicGateAnswer,
            ExplanationEvidenceDecision: ExplanationEvidenceAnswer,
            Jury2Decision: Jury2Answer,
        }[type(item)]
        return answer_type(
            request.logical_request_id, request.payload_fingerprint, item,
        )


class Ledger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


class NeverCancelled:
    @staticmethod
    def cancelled():
        return False


class AlwaysCancelled:
    @staticmethod
    def cancelled():
        return True


def support_gate(value=True):
    return SupportGateDecision(value)


def no_support():
    return SupportGateDecision(False)


def full_support(value=True):
    return FullSupportGateDecision(value)


def no_contrary():
    return ContraryGateDecision(False)


def evidence(
    *span_ids,
    supported_content=None,
    unsupported_content=None,
    incompatible_proposition=None,
    reason="Selected passage documents the determined outcome.",
    non_decidable_reason=None,
):
    if span_ids and supported_content is None and incompatible_proposition is None:
        supported_content = "The source establishes its complete assigned role."
    return ExplanationEvidenceDecision(
        reason,
        supported_content,
        unsupported_content,
        incompatible_proposition,
        tuple(span_ids),
        non_decidable_reason,
    )


def _run(
    script, *, level="high", cap=5, jury1_technical=3,
    jury2_technical=3, context_mode="full_text", failures=None,
):
    requests = Requests(failures, context_mode=context_mode)
    dispatch, ledger = Dispatch(script), Ledger()
    controller = ClaimEvidenceController(
        requests, dispatch, ledger, NeverCancelled(),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    if context_mode == "full_text":
        context = build_effective_context(
            SOURCE, mode="full_text", budget=len(SOURCE)
        )
    else:
        context = build_effective_context(
            SOURCE, mode="extractive_rag", budget=len(SOURCE),
            retrieved_ranges=((0, len(SOURCE)),),
            retrieval_algorithm="fixture-rag-v1",
        )
    state = controller.run(
        StateMachinePolicy(cap, jury1_technical, jury2_technical, level),
        claim=CLAIM, source_text=SOURCE, context=context,
    )
    return state, requests, dispatch, ledger


def test_related_uses_independent_gates_and_bypasses_jury2():
    state, requests, dispatch, _ = _run([
        no_support(), no_contrary(),
        TopicGateDecision(True),
        evidence(reason="The source is related but supplies no usable support."),
    ])

    assert state.terminal.outcome == "related"
    assert state.terminal.resolution == "jury2_not_eligible"
    assert requests.jury2 == []
    assert [call.stage for call in dispatch.calls] == [
        "support_gate", "contrary_gate", "topic_gate", "explanation_evidence",
    ]
    for call in dispatch.calls:
        payload = json.loads(call.payload)
        assert "targets" not in payload
        assert "coverage" not in payload
        assert payload["claim"] == CLAIM
        assert payload["claim_context"] == CLAIM_CONTEXT
        assert payload["citation_marker"] == CITATION_MARKER


def test_support_then_full_support_maps_partial_and_jury2_accepts():
    state, _, dispatch, _ = _run([
        support_gate(),
        full_support(False),
        evidence(
            ALPHA_ID,
            supported_content="The first part is supported.",
            unsupported_content="The remaining claim is unsupported.",
        ),
        Jury2Decision(True, "Passage fits the claim assessment."),
    ])

    assert [call.stage for call in dispatch.calls] == [
        "support_gate", "full_support_gate", "explanation_evidence", "jury2",
    ]
    assert state.terminal.outcome == "partial"
    assert state.terminal.jury2_passed is True


def test_invalid_evidence_after_positive_gates_is_rejected_and_requeued():
    state, _, _, ledger = _run([
        support_gate(), full_support(), evidence(),
        no_support(), no_contrary(),
        TopicGateDecision(False),
        evidence(reason="The source concerns a different subject."),
    ], cap=2)

    assert state.mechanical_rejections == 1
    assert state.terminal.outcome == "off_topic"
    assert ledger.events[0].kind == "jury1_rejected:schema_invalid"


def test_support_gate_technical_retry_reuses_identical_request():
    state, requests, dispatch, _ = _run([
        "timeout", no_support(), no_contrary(),
        TopicGateDecision(True),
        evidence(reason="The source is related but supplies no usable support."),
    ], jury1_technical=2)

    assert dispatch.calls[0] is dispatch.calls[1]
    assert len(requests.jury1) == 4
    assert state.terminal.outcome == "related"


def test_prior_stage_failures_are_counted_without_repeating_old_attempts():
    state, _, dispatch, _ = _run([
        no_support(), no_contrary(),
        TopicGateDecision(False),
        evidence(reason="The source concerns a different subject."),
    ], jury1_technical=2, failures={("support_gate", 1): 1})

    assert [item.stage for item in dispatch.calls] == [
        "support_gate", "contrary_gate", "topic_gate", "explanation_evidence",
    ]
    assert state.jury1_technical_failures_total == 0
    assert state.terminal.outcome == "off_topic"


def test_full_text_retrieval_limit_is_rejected_and_rag_allows_it():
    limited = evidence(
        reason="Retrieved material may omit decisive evidence.",
        non_decidable_reason="retrieval_limit",
    )
    state, _, _, ledger = _run([
        support_gate(), full_support(), limited,
        no_support(), no_contrary(),
        TopicGateDecision(True),
        evidence(reason="The source is related but supplies no usable support."),
    ], cap=2)
    assert state.mechanical_rejections == 1
    assert ledger.events[0].kind == "jury1_rejected:schema_invalid"

    state, _, _, _ = _run(
        [support_gate(), full_support(), limited],
        context_mode="extractive_rag",
    )
    assert state.terminal.outcome == "non_decidable"


def test_jury2_technical_retries_are_byte_identical():
    state, requests, dispatch, _ = _run([
        support_gate(), full_support(), evidence(ALPHA_ID), "timeout",
        RuntimeError("transport failed"),
        Jury2Decision(True, "Passage fits the claim assessment."),
    ])

    jury2_calls = dispatch.calls[3:]
    assert jury2_calls[0] is jury2_calls[1] is jury2_calls[2]
    assert len(requests.jury2) == 1
    assert state.jury2_technical_failures_total == 2
    assert state.terminal.jury2_passed is True


def test_wrong_jury2_provenance_retries_same_request():
    def wrong(request):
        return Jury2Answer(
            "different", request.payload_fingerprint,
            Jury2Decision(True, "Must not be trusted."),
        )

    state, _, dispatch, ledger = _run([
        support_gate(), full_support(), evidence(ALPHA_ID), wrong,
        Jury2Decision(True, "Trusted."),
    ])

    assert dispatch.calls[3] is dispatch.calls[4]
    assert state.terminal.jury2_passed is True
    assert any(event.cause == "provenance_invalid" for event in ledger.events)


def test_cancellation_stops_before_dispatch():
    requests, dispatch, ledger = Requests(), Dispatch([]), Ledger()
    controller = ClaimEvidenceController(
        requests, dispatch, ledger, AlwaysCancelled(),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )

    state = controller.run(
        StateMachinePolicy(5, 3, 3, "high"),
        claim=CLAIM,
        source_text=SOURCE,
        context=build_effective_context(
            SOURCE, mode="full_text", budget=len(SOURCE)
        ),
    )

    assert state.terminal.status == "cancelled"
    assert dispatch.calls == []
    assert requests.jury1 == []


def test_request_factory_provenance_fails_closed_before_dispatch():
    class BadRequests(Requests):
        def jury1_flow_request(
            self,
            stage,
            candidate_cycle,
            *,
            determined_outcome=None,
        ):
            return LogicalRequest(
                "jury2", "bad", b"payload", "not-a-hash", 99, "prompt"
            )

    controller = ClaimEvidenceController(
        BadRequests(), Dispatch([]), Ledger(), NeverCancelled(),
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    with pytest.raises(FatalDispatchError, match="provenance"):
        controller.run(
            StateMachinePolicy(5, 3, 3, "high"),
            claim=CLAIM,
            source_text=SOURCE,
            context=build_effective_context(
                SOURCE, mode="full_text", budget=len(SOURCE)
            ),
        )
