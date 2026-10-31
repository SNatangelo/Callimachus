# tests/verify/claim_evidence/adapters/llm/test_typesafe.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.backends import typesafe as backend
from core.verify.claim_evidence.adapters.llm.typesafe import decode
from core.verify.claim_evidence.contracts.schema import ContractError


def _choice(confidence=0.9):
    return json.dumps({
        "model": "jev-1.13.0",
        "answers": {
            "decision": {
                "type": "choice",
                "choice": "true",
                "probabilities": {"true": 0.9, "false": 0.1},
                "confidence": confidence,
            }
        },
    })


def test_choice_is_probability_only_and_below_threshold_is_valid():
    result = decode("jury2", _choice(), b"{}", 0.8, "jev-1.13.0")
    assert result.passages_fit_claim is True
    assert result.reason is None
    assert result.provider_confidence == 0.9
    below_threshold = decode("jury2", _choice(0.7), b"{}", 0.8, "jev-1.13.0")
    assert below_threshold.passages_fit_claim is True
    assert below_threshold.provider_confidence == 0.7
    assert below_threshold.provider_uncertain is True

    mismatched = json.loads(_choice())
    mismatched["answers"]["decision"]["choice"] = "false"
    with pytest.raises(ContractError):
        decode("jury2", json.dumps(mismatched), b"{}", 0.8, "jev-1.13.0")


def test_span_noul_excludes_ambiguous_spans_without_rejecting_response():
    payload = json.dumps({
        "determined_outcome": "supports",
        "source_spans": [
            {"span_id": "a", "text": "A"},
            {"span_id": "b", "text": "B"},
        ],
    }).encode()
    raw = json.dumps({
        "model": "jev-1.13.0",
        "answers": {
            "a": {"type": "noul", "noul": 0.8},
            "b": {"type": "noul", "noul": 0.2},
        },
    })
    result = decode("explanation_evidence", raw, payload, 0.75, "jev-1.13.0")
    assert result.evidence_span_ids == ("a",)
    assert result.reason is None and result.provider_confidence == 0.8
    ambiguous = json.loads(raw)
    ambiguous["answers"]["b"]["noul"] = 0.55
    result = decode("explanation_evidence", json.dumps(ambiguous), payload, 0.75, "jev-1.13.0")
    assert result.evidence_span_ids == ("a",)
    assert result.provider_uncertain is False

    no_sufficient_span = json.loads(raw)
    no_sufficient_span["answers"]["a"]["noul"] = 0.55
    no_sufficient_span["answers"]["b"]["noul"] = 0.45
    result = decode(
        "explanation_evidence",
        json.dumps(no_sufficient_span),
        payload,
        0.75,
        "jev-1.13.0",
    )
    assert result.evidence_span_ids == ()
    assert result.non_decidable_reason == "provider_uncertain"
    assert result.provider_uncertain is True
    with pytest.raises(ContractError):
        invalid = json.dumps({
            "model": "jev-1.13.0",
            "answers": {
                "a": {"type": "noul", "noul": 2},
                "b": {"type": "noul", "noul": 0.2},
            },
        })
        decode("explanation_evidence", invalid, payload, 0.75, "jev-1.13.0")


def test_non_evidence_outcome_uses_confirmation_without_evidence():
    payload = json.dumps({
        "determined_outcome": "off_topic",
        "source_spans": [],
    }).encode()
    result = decode("explanation_evidence", _choice(), payload, 0.8, "jev-1.13.0")
    assert result.evidence_span_ids == ()
    assert result.non_decidable_reason is None
    assert result.provider_confidence == 0.9


def test_backend_uses_pinned_systemone_wire_contract(monkeypatch):
    sent = {}
    monkeypatch.setattr(backend, "credential_value", lambda _: "secret")
    monkeypatch.setattr(backend, "llm_timeout", lambda: 17)
    monkeypatch.setattr(
        backend,
        "get_json_post",
        lambda: lambda url, body, headers, timeout: sent.update(
            url=url, body=body, headers=headers, timeout=timeout
        ) or {"model": "jev-1.13.0", "answers": {}},
    )
    payload = {
        "task": {"task_id": "support_gate", "instructions": "Is support present?"}
    }
    result = json.loads(
        backend._call("state", json.dumps(payload), "jev-1.13.0", None)
    )
    assert result == {"model": "jev-1.13.0", "answers": {}}
    assert sent["url"] == "https://api.typesafe.ai/v1/systemone"
    assert sent["body"]["state"] == payload
    assert set(sent["body"]["questions"]["decision"]) == {
        "type", "instructions", "criteria",
    }
    assert "Is support present?" in sent["body"]["questions"]["decision"]["instructions"]


def test_backend_builds_explicit_span_questions(monkeypatch):
    sent = {}
    monkeypatch.setattr(backend, "credential_value", lambda _: "secret")
    monkeypatch.setattr(
        backend,
        "get_json_post",
        lambda: lambda url, body, headers, timeout: sent.update(body=body)
        or {"model": "jev-1.13.0", "answers": {}},
    )
    payload = {
        "task": {
            "task_id": "explanation_evidence",
            "instructions": "Select evidence.",
        },
        "determined_outcome": "partial",
        "source_spans": [{"span_id": "S1", "text": "Evidence text."}],
    }
    backend._call("system", json.dumps(payload), "jev-1.13.0", None)
    question = sent["body"]["questions"]["S1"]
    assert question["type"] == "noul"
    assert "partial" in question["instructions"]
    assert "S1" in question["instructions"]
