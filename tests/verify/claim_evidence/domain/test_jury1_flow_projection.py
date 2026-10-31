# tests/verify/claim_evidence/domain/test_jury1_flow_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.verify.claim_evidence.contracts.jury1_flow import build_jury1_flow_payload
from core.verify.claim_evidence.domain.jury1_decisions import (
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    Jury1FlowError,
    SupportGateDecision,
    TopicGateDecision,
)
from core.verify.claim_evidence.domain.jury1_flow import evaluate_jury1_flow


def payload(mode="full_text", span_count=2):
    return build_jury1_flow_payload(
        task_id="support_gate", cited_source_mode=mode, source_hash="a" * 64,
        source_spans=[
            {"span_id": f"s{index}", "text": f"Evidence {index}."}
            for index in range(1, span_count + 1)
        ],
        claim="The exact claim.", claim_context="Its exact context.",
        citation_marker="[1]",
    )


def explanation(
    *,
    reason="Post-verdict explanation.",
    supported=None,
    unsupported=None,
    incompatible=None,
    ids=("s1",),
    non_decidable=None,
):
    return ExplanationEvidenceDecision(
        reason,
        supported,
        unsupported,
        incompatible,
        ids,
        non_decidable,
    )


def test_flow_routes_support_then_full_support_or_contrary_then_topic():
    assert evaluate_jury1_flow(payload(), {}).next_stage == "support_gate"
    assert evaluate_jury1_flow(
        payload(), {"support_gate": SupportGateDecision(True)}
    ).next_stage == "full_support_gate"
    assert evaluate_jury1_flow(
        payload(), {"support_gate": SupportGateDecision(False)}
    ).next_stage == "contrary_gate"


def test_low_confidence_gate_becomes_non_decidable_before_candidate_selection():
    result = evaluate_jury1_flow(
        payload(), {"support_gate": SupportGateDecision(True, 0.7, True)}
    )
    assert result.next_stage is None
    assert result.mapped.decision.outcome == "non_decidable"
    assert result.mapped.decision.reason == "provider_uncertain"
    assert result.mapped.source_span_ids == ()
    answers = {
        "support_gate": SupportGateDecision(False),
        "contrary_gate": ContraryGateDecision(False),
    }
    assert evaluate_jury1_flow(payload(), answers).next_stage == "topic_gate"


@pytest.mark.parametrize(
    "full,outcome,supported,unsupported",
    [
        (True, "supports", "The source establishes its assigned role.", None),
        (
            False,
            "partial",
            "The source supports the named method.",
            "The source does not establish the claimed comparison.",
        ),
    ],
)
def test_support_branch_fixes_verdict_before_explanation(
    full, outcome, supported, unsupported,
):
    answers = {
        "support_gate": SupportGateDecision(True),
        "full_support_gate": FullSupportGateDecision(full),
    }
    pending = evaluate_jury1_flow(payload(), answers)
    assert pending.next_stage == "explanation_evidence"
    assert pending.determined_outcome == outcome

    mapped = evaluate_jury1_flow(
        payload(),
        {
            **answers,
            "explanation_evidence": explanation(
                supported=supported,
                unsupported=unsupported,
            ),
        },
    ).mapped
    assert mapped.decision.outcome == outcome
    assert mapped.decision.supported_part == supported
    assert mapped.source_span_ids == ("s1",)
    if outcome in {"supports", "partial"}:
        assert f"Supported: {supported}" in mapped.decision.explanation
    if outcome == "partial":
        assert f"Unsupported: {unsupported}" in mapped.decision.explanation


def test_contrary_branch_fixes_verdict_before_explanation():
    answers = {
        "support_gate": SupportGateDecision(False),
        "contrary_gate": ContraryGateDecision(True),
    }
    pending = evaluate_jury1_flow(payload(), answers)
    assert pending.next_stage == "explanation_evidence"
    assert pending.determined_outcome == "contradicts"

    proposition = "The source demonstrates the opposite proposition."
    mapped = evaluate_jury1_flow(
        payload(),
        {
            **answers,
            "explanation_evidence": explanation(incompatible=proposition),
        },
    ).mapped
    assert mapped.decision.outcome == "contradicts"
    assert mapped.decision.incompatible_proposition == proposition


@pytest.mark.parametrize("same_subject,outcome", [(True, "related"), (False, "off_topic")])
def test_no_support_no_contrary_requires_post_verdict_explanation(
    same_subject, outcome,
):
    answers = {
        "support_gate": SupportGateDecision(False),
        "contrary_gate": ContraryGateDecision(False),
        "topic_gate": TopicGateDecision(same_subject),
    }
    pending = evaluate_jury1_flow(payload(), answers)
    assert pending.next_stage == "explanation_evidence"
    assert pending.determined_outcome == outcome

    mapped = evaluate_jury1_flow(
        payload(),
        {
            **answers,
            "explanation_evidence": explanation(ids=()),
        },
    ).mapped
    assert mapped.decision.outcome == outcome
    assert mapped.decision.explanation == "Post-verdict explanation."
    assert mapped.source_span_ids == ()


def test_only_post_verdict_explanation_can_project_non_decidable():
    answers = {
        "support_gate": SupportGateDecision(True),
        "full_support_gate": FullSupportGateDecision(True),
        "explanation_evidence": explanation(
            reason="Retrieved material may omit decisive evidence.",
            ids=(),
            non_decidable="retrieval_limit",
        ),
    }
    result = evaluate_jury1_flow(payload("extractive_rag"), answers)
    assert result.mapped.decision.outcome == "non_decidable"


def test_retrieval_limit_is_only_valid_for_rag():
    answers = {
        "support_gate": SupportGateDecision(True),
        "full_support_gate": FullSupportGateDecision(True),
        "explanation_evidence": explanation(
            reason="Retrieved material may omit decisive evidence.",
            ids=(),
            non_decidable="retrieval_limit",
        ),
    }
    with pytest.raises(Jury1FlowError, match="extractive_rag"):
        evaluate_jury1_flow(payload(), answers)


def test_explanation_evidence_accepts_six_and_rejects_seven_spans():
    def answers(ids):
        return {
            "support_gate": SupportGateDecision(True),
            "full_support_gate": FullSupportGateDecision(True),
            "explanation_evidence": explanation(
                supported="The source establishes its assigned role.",
                ids=ids,
            ),
        }

    six_ids = tuple(f"s{index}" for index in range(1, 7))
    accepted = evaluate_jury1_flow(payload(span_count=6), answers(six_ids))

    assert accepted.mapped is not None
    assert accepted.mapped.source_span_ids == six_ids

    seven_ids = tuple(f"s{index}" for index in range(1, 8))
    with pytest.raises(Jury1FlowError, match="exceeds six spans"):
        evaluate_jury1_flow(payload(span_count=7), answers(seven_ids))


@pytest.mark.parametrize(
    "answers",
    [
        {
            "support_gate": SupportGateDecision(True),
            "contrary_gate": ContraryGateDecision(False),
        },
        {
            "support_gate": SupportGateDecision(False),
            "topic_gate": TopicGateDecision(True),
        },
        {
            "support_gate": SupportGateDecision(True),
            "full_support_gate": FullSupportGateDecision(True),
            "explanation_evidence": explanation(ids=("unknown",)),
        },
    ],
)
def test_wrong_branch_and_unknown_evidence_are_rejected(answers):
    with pytest.raises(Jury1FlowError):
        evaluate_jury1_flow(payload(), answers)
