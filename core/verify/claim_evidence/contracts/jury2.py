# core/verify/claim_evidence/contracts/jury2.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen Jury2 passage-fit contract; it never assigns a public outcome."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.types import CandidateRecord, Jury2Decision
from .prompt_loader import load_prompt_spec
from .schema import ContractError, exact_object, nonempty_string


JURY2_FIELDS = frozenset({"passages_fit_claim", "reason"})
JURY2_PROMPT_SPEC = load_prompt_spec(
    Path(__file__).with_name("prompts") / "jury2.json",
    "jury2_system_prompt",
)
JURY2_SYSTEM_PROMPT = JURY2_PROMPT_SPEC.system_prompt


def jury2_eligible(candidate: CandidateRecord) -> bool:
    return (
        isinstance(candidate, CandidateRecord)
        and candidate.decision.outcome
        in {"supports", "partial", "contradicts"}
    )


def build_jury2_payload(
    claim: str,
    candidate: CandidateRecord,
) -> dict[str, Any]:
    nonempty_string(claim, "claim")
    if not jury2_eligible(candidate):
        raise ContractError("Jury2 is not permitted for this candidate")
    if not candidate.grounded:
        raise ContractError("Jury2 selected passages are missing")

    decision = candidate.decision
    asserted_relation = (
        "contrary" if decision.outcome == "contradicts" else "basis"
    )
    if asserted_relation == "contrary":
        passage_subject = nonempty_string(
            decision.incompatible_proposition or claim,
            "incompatible_proposition",
        )
    elif decision.outcome in {"supports", "partial"}:
        passage_subject = nonempty_string(
            decision.supported_part or claim,
            "supported_part",
        )

    return {
        "asserted_relation": asserted_relation,
        "passage_subject": passage_subject,
        "selected_passages": [
            {"span_id": item.span_id, "text": item.text}
            for item in candidate.grounded
        ],
    }


def build_jury2_prompt(
    claim: str,
    candidate: CandidateRecord,
) -> tuple[str, str]:
    payload = build_jury2_payload(claim, candidate)
    return (
        JURY2_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def parse_jury2_response(value: Any) -> Jury2Decision:
    answer = exact_object(value, JURY2_FIELDS)
    if type(answer["passages_fit_claim"]) is not bool:
        raise ContractError(
            "passages_fit_claim must be boolean",
            code="response_boolean_invalid",
        )
    return Jury2Decision(
        answer["passages_fit_claim"],
        nonempty_string(answer["reason"], "reason"),
    )
