# tests/verify/claim_evidence/contracts/test_jury1_flow.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_FLOW_PROMPT_SPEC,
    JURY1_FLOW_SYSTEM_PROMPT,
    build_jury1_flow_payload,
    build_jury1_flow_prompt,
    parse_contrary_gate_response,
    parse_explanation_evidence_response,
    parse_full_support_gate_response,
    parse_support_gate_response,
    parse_topic_gate_response,
)
from core.verify.claim_evidence.contracts.schema import ContractError


SOURCE_HASH = "a" * 64
SPANS = [
    {"span_id": "s1", "text": "The source provides the basis."},
    {"span_id": "s2", "text": "The source limits that basis."},
]
CLAIM = "The exact manuscript claim."
CONTEXT = "The local manuscript context."
MARKER = "[20]"


def payload(task_id="support_gate", **extra):
    return build_jury1_flow_payload(
        task_id=task_id,
        cited_source_mode="full_text",
        source_hash=SOURCE_HASH,
        source_spans=SPANS,
        claim=CLAIM,
        claim_context=CONTEXT,
        citation_marker=MARKER,
        **extra,
    )


def explanation(**overrides):
    value = {
        "reason": "The selected passage establishes the determined outcome.",
        "supported_content": None,
        "unsupported_content": None,
        "incompatible_proposition": None,
        "evidence_span_ids": ["s1"],
        "non_decidable_reason": None,
    }
    value.update(overrides)
    return value


def test_payload_is_source_first_and_preserves_exact_claim_context_and_marker():
    value = payload()

    assert tuple(value) == (
        "source_spans", "source_hash", "cited_source_mode", "claim",
        "claim_context", "citation_marker", "task",
    )
    assert value["claim"] == CLAIM
    assert value["claim_context"] == CONTEXT
    assert value["citation_marker"] == MARKER
    assert value["source_spans"] == SPANS


def test_all_jury1_requests_share_system_prompt_and_source_first_common_prefix():
    prompts = [
        build_jury1_flow_prompt(
            **({"task_id": task_id} | (
                {"determined_outcome": "partial"}
                if task_id == "explanation_evidence" else {}
            )),
            cited_source_mode="full_text", source_hash=SOURCE_HASH,
            source_spans=SPANS, claim=CLAIM, claim_context=CONTEXT,
            citation_marker=MARKER,
        )
        for task_id, _ in JURY1_FLOW_PROMPT_SPEC.tasks
    ]

    assert {system for system, _ in prompts} == {JURY1_FLOW_SYSTEM_PROMPT}
    assert len({user.split(',"task":', 1)[0] for _, user in prompts}) == 1
    assert all(json.loads(user)["claim"] == CLAIM for _, user in prompts)


def test_explanation_payload_contains_only_code_determined_outcome_state():
    value = payload("explanation_evidence", determined_outcome="partial")
    assert tuple(value) == (
        "source_spans", "source_hash", "cited_source_mode", "claim",
        "claim_context", "citation_marker", "task", "determined_outcome",
    )
    assert value["determined_outcome"] == "partial"


@pytest.mark.parametrize(
    "task_id",
    ["jury1", "basis_gate", "fidelity_gate", "evidence_selection"],
)
def test_payload_rejects_legacy_or_unknown_task_ids(task_id):
    with pytest.raises(ContractError):
        payload(task_id)


@pytest.mark.parametrize("field", ["claim", "claim_context", "citation_marker"])
def test_payload_requires_nonempty_exact_claim_inputs(field):
    values = dict(
        task_id="support_gate", cited_source_mode="full_text",
        source_hash=SOURCE_HASH, source_spans=SPANS, claim=CLAIM,
        claim_context=CONTEXT, citation_marker=MARKER,
    )
    values[field] = ""
    with pytest.raises(ContractError):
        build_jury1_flow_payload(**values)


@pytest.mark.parametrize(
    "parser,value",
    [
        (parse_support_gate_response, {"source_supports_any": True}),
        (parse_full_support_gate_response, {"source_supports_fully": False}),
        (parse_contrary_gate_response, {"paper_demonstrates_opposite": True}),
        (parse_topic_gate_response, {"same_specific_subject": False}),
    ],
)
def test_classification_gate_schemas_are_boolean_only_and_exact(parser, value):
    assert parser(value)
    boolean_field = next(iter(value))
    for mutation in (
        {**value, "reason": "This must be deferred."},
        {**value, "outcome": "supports"},
        {boolean_field: None},
        {boolean_field: 1},
    ):
        with pytest.raises(ContractError):
            parser(mutation)


@pytest.mark.parametrize(
    "outcome,value",
    [
        (
            "supports",
            explanation(supported_content="The source establishes its assigned role."),
        ),
        (
            "partial",
            explanation(
                supported_content="The source supports the named method.",
                unsupported_content="The source does not establish the claimed comparison.",
            ),
        ),
        (
            "contradicts",
            explanation(
                incompatible_proposition="The paper demonstrates the opposite result.",
            ),
        ),
        ("related", explanation(evidence_span_ids=[])),
        ("off_topic", explanation(evidence_span_ids=[])),
    ],
)
def test_post_verdict_explanation_schema_preserves_reason_and_evidence(outcome, value):
    parsed = parse_explanation_evidence_response(value)
    assert parsed.reason
    assert parsed.evidence_span_ids == tuple(value["evidence_span_ids"])


def test_post_verdict_explanation_schema_is_exact():
    with pytest.raises(ContractError):
        parse_explanation_evidence_response({**explanation(), "outcome": "supports"})
    missing = explanation()
    missing.pop("unsupported_content")
    with pytest.raises(ContractError):
        parse_explanation_evidence_response(missing)


def test_explanation_evidence_accepts_six_distinct_spans():
    ids = [f"s{index}" for index in range(1, 7)]

    parsed = parse_explanation_evidence_response(
        explanation(evidence_span_ids=ids)
    )

    assert parsed.evidence_span_ids == tuple(ids)


@pytest.mark.parametrize(
    "ids,expected_code",
    [
        (["s1", "s1"], "response_list_duplicate"),
        (
            ["s1", "s2", "s3", "s4", "s5", "s6", "s7"],
            "evidence_cardinality_exceeded",
        ),
    ],
)
def test_explanation_evidence_exposes_stable_protocol_error_codes(ids, expected_code):
    with pytest.raises(ContractError) as raised:
        parse_explanation_evidence_response(explanation(evidence_span_ids=ids))
    assert raised.value.code == expected_code


def test_non_decidable_explanation_requires_empty_semantic_fields_and_evidence():
    parsed = parse_explanation_evidence_response(
        explanation(
            reason="The retrieved spans may omit decisive material.",
            evidence_span_ids=[],
            non_decidable_reason="retrieval_limit",
        )
    )
    assert parsed.non_decidable_reason == "retrieval_limit"

    with pytest.raises(ContractError) as raised:
        parse_explanation_evidence_response(
            explanation(non_decidable_reason="retrieval_limit")
        )
    assert raised.value.code == "evidence_non_decidable_nonempty"


def test_payload_rejects_nul_in_source_span_text():
    with pytest.raises(ContractError, match="NUL"):
        build_jury1_flow_payload(
            task_id="support_gate",
            cited_source_mode="full_text",
            source_hash=SOURCE_HASH,
            source_spans=[{"span_id": "s1", "text": "\x00source text"}],
            claim=CLAIM,
            claim_context=CONTEXT,
            citation_marker=MARKER,
        )
