# core/verify/backends/typesafe.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""TypeSafe SystemOne backend (Jev is a pinned audit model)."""
from __future__ import annotations

import json

from ._registry import BackendSpec, register
from ._chat_transport import credential_value, get_json_post, llm_timeout
from .errors import ConfigError

ENV_TYPESAFE_KEY = "TYPESAFE_API_KEY"
ENV_TYPESAFE_MODEL = "TYPESAFE_MODEL"
_URL = "https://api.typesafe.ai/v1/systemone"


def _available() -> bool:
    return bool(credential_value(ENV_TYPESAFE_KEY))


def _request(system: str, user: str) -> tuple[dict, dict]:
    try:
        payload = json.loads(user)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConfigError("TypeSafe jury payload is invalid") from exc
    task = payload.get("task", {})
    stage = task.get("task_id")
    outcome = payload.get("determined_outcome")
    if stage == "explanation_evidence" and outcome in {
        "supports", "partial", "contradicts",
    }:
        questions = {
            row["span_id"]: {
                "type": "noul",
                "instructions": (
                    f"Does source span {row['span_id']} provide direct evidence for "
                    f"the fixed citation-verification outcome '{outcome}' for the "
                    "exact claim? Evaluate only that span."
                ),
                "criteria": {
                    "true": "The span substantively establishes the required relationship.",
                    "false": "The span does not establish the required relationship.",
                },
            }
            for row in payload.get("source_spans", [])
        }
    else:
        task_instructions = task.get("instructions")
        instructions = system
        if task_instructions is not None:
            instructions += "\n\nTask: " + json.dumps(
                task_instructions, ensure_ascii=False, separators=(",", ":")
            )
        if stage == "explanation_evidence":
            instructions += (
                f"\n\nConfirm whether the fixed outcome '{outcome}' accurately "
                "describes the relationship between the exact claim and visible spans."
            )
        questions = {
            "decision": {
                "type": "choice",
                "instructions": instructions,
                "criteria": {
                    "true": "The requested condition is satisfied.",
                    "false": "The requested condition is not satisfied.",
                },
            }
        }
    return payload, questions


def _call(system: str, user: str, model: str | None, max_tokens: int | None) -> str:
    key = credential_value(ENV_TYPESAFE_KEY)
    if not key:
        raise ConfigError("TYPESAFE_API_KEY not set - cannot use the TypeSafe backend")
    if not isinstance(model, str) or not model:
        raise ConfigError("TypeSafe model is required")
    payload, questions = _request(system, user)
    response = get_json_post()(_URL, {"state": payload, "model": model, "questions": questions},
                               {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                               timeout=llm_timeout())
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


def _claim_evidence_decode(
    stage: str, raw: str, payload: bytes, minimum: float | None, model: str,
) -> object:
    """Lazy boundary to avoid making common Verify know TypeSafe's wire shape."""
    from core.verify.claim_evidence.adapters.llm.typesafe import decode

    if minimum is None:
        raise ConfigError("TYPESAFE_MIN_CONFIDENCE is required")
    return decode(stage, raw, payload, minimum, model)


register(BackendSpec(name="typesafe", env_key=ENV_TYPESAFE_KEY,
                     env_model=ENV_TYPESAFE_MODEL, available=_available,
                     call=_call, supports_omitted_max_tokens=True,
                     claim_evidence_decode=_claim_evidence_decode,
                     claim_evidence_capabilities={
                         "confidence_env": "TYPESAFE_MIN_CONFIDENCE",
                         "policy_id": "typesafe-citation-confidence-v1",
                         "pinned_model_pattern": r"jev-\d+\.\d+\.\d+",
                         "jury1_context_mode": "extractive_rag",
                         "jury1_context_help": (
                             "Set CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag "
                             "and CITATION_VERIFIER_MAX_SOURCE_CHARS=<positive integer>, "
                             "or make every TypeSafe lane Jury2-only with "
                             "CITATION_VERIFIER_VERIFY_JURY2_ONLY=typesafe:<jev-model>."
                         ),
                         "provider_label": "TypeSafe",
                     }))
