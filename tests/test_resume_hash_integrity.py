# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from core.infra.db.repository import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.infra.db.schema_bootstrap import ensure_schema
from core.verify.claim_evidence.adapters.run_repository import (
    ClaimEvidenceRunRepository,
)
from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_PROMPT_SPEC,
    JURY1_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    candidate_fingerprint,
    payload_fingerprint,
    provider_prompt_fingerprint,
)


HASH_FIELDS = (
    "source_hash",
    "context_hash",
    "retrieval_hash",
    "prompt_hash",
    "model_hash",
    "policy_hash",
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _adapter():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    manuscript = "synthetic manuscript context"
    conn.execute(
        "INSERT INTO manuscript_text VALUES(1,?,?,?)",
        (manuscript, _hash(manuscript), len(manuscript)),
    )
    repository = RunRepository(".", conn)
    repository._schema_version = SCHEMA_VERSION
    return ClaimEvidenceRunRepository(repository), conn


def _request(identifier: str) -> dict:
    payload = {
        "source_spans": [{"span_id": "span-1", "text": "synthetic quote"}],
        "source_hash": _hash("source_hash"),
        "cited_source_mode": "full_text",
        "claim": "synthetic claim",
        "claim_context": "synthetic manuscript context",
        "citation_marker": "[1]",
        "task": {
            "task_id": "support_gate",
            "instructions": JURY1_PROMPT_SPEC.task_prompt("support_gate"),
        },
    }
    request = {
        "logical_request_id": identifier,
        "claim_id": "claim-1",
        "ref_id": "ref-1",
        "scope": "",
        "candidate_id": None,
        "candidate_cycle": 1,
        "stage": "support_gate",
        "payload": payload,
        "payload_hash": payload_fingerprint(payload, version=FINGERPRINT_VERSION_V2),
    }
    request.update({name: _hash(name) for name in HASH_FIELDS})
    request["prompt_hash"] = provider_prompt_fingerprint(
        JURY1_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
    return request


def _candidate_record() -> dict:
    record = {
        "outcome": "supports",
        "explanation": "synthetic explanation",
        "evidence": ["synthetic quote"],
        "outcome_fields": {
            "claim_hash": _hash("claim"),
            "source_hash": _hash("source_hash"),
            "supported_part": "the source-backed role",
            "incompatible_proposition": None,
            "reason": None,
            "provider_confidence": None,
        },
        "grounded": [
            {
                "span_id": "span-1",
                "text": "synthetic quote",
                "raw_start": 0,
                "raw_end": 15,
                "source_hash": _hash("source_hash"),
                "match_mode": "exact_raw",
                "score": 1.0,
            }
        ],
    }
    record.update({name: _hash(name) for name in HASH_FIELDS})
    record["prompt_hash"] = _request("candidate-origin")["prompt_hash"]
    record["fingerprint"] = candidate_fingerprint(
        outcome=record["outcome"],
        evidence=tuple(record["evidence"]),
        outcome_fields=record["outcome_fields"],
        grounded=tuple(
            {
                name: item[name]
                for name in ("span_id", "raw_start", "raw_end", "text", "source_hash")
            }
            for item in record["grounded"]
        ),
        version=FINGERPRINT_VERSION_V2,
    )
    return record


def _append_candidate(adapter) -> None:
    adapter.append_candidate(
        candidate_id="candidate-1",
        claim_id="claim-1",
        ref_id="ref-1",
        scope="",
        candidate_cycle=1,
        origin_logical_request_id="jury1-request",
        record=_candidate_record(),
    )


@pytest.mark.parametrize(
    ("target", "match"),
    [
        ("request", "stored logical request payload hash"),
        ("candidate", "stored candidate fingerprint"),
    ],
)
def test_resume_revalidates_persisted_hashes(target: str, match: str):
    adapter, conn = _adapter()
    adapter.append_logical_request(_request("jury1-request"))
    _append_candidate(adapter)
    if target == "request":
        conn.execute("DROP TRIGGER llm_logical_requests_no_update")
        conn.execute(
            "UPDATE llm_logical_requests SET payload_hash = ?",
            (_hash("tampered"),),
        )
    else:
        conn.execute("DROP TRIGGER verification_candidates_no_update")
        conn.execute(
            "UPDATE verification_candidates SET fingerprint = ?",
            (_hash("tampered"),),
        )

    with pytest.raises(ValueError, match=match):
        adapter.resume_snapshot()
