# tests/verify/claim_evidence/domain/test_fingerprint.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    answer_fingerprint,
    candidate_fingerprint,
    fingerprint,
    pair_fingerprint,
    payload_fingerprint,
    policy_fingerprint,
    provider_prompt_fingerprint,
)


def _candidate(*, outcome="supports", evidence=("quoted text",), grounded=None):
    return candidate_fingerprint(
        outcome=outcome,
        evidence=evidence,
        outcome_fields={"supported_part": "p"},
        grounded=grounded or ({"span_id": "span-1", "raw_start": 4},),
    )


def test_fingerprints_are_canonical_and_namespaced():
    assert payload_fingerprint({"b": [1], "a": "x"}) == payload_fingerprint(
        {"a": "x", "b": [1]}
    )
    values = {
        pair_fingerprint(claim_id="claim-1", ref_id="ref-1", scope=""),
        payload_fingerprint({"claim_id": "claim-1", "ref_id": "ref-1"}),
        policy_fingerprint({"claim_id": "claim-1", "ref_id": "ref-1"}),
        _candidate(),
    }
    assert len(values) == 4
    assert all(len(value) == 64 for value in values)


def test_provider_prompt_fingerprint_binds_exact_system_and_user_bytes():
    baseline = provider_prompt_fingerprint("system", b'{"claim":"x"}')
    assert baseline != provider_prompt_fingerprint("system changed", b'{"claim":"x"}')
    assert baseline != provider_prompt_fingerprint("system", b'{"claim":"y"}')


def test_candidate_fingerprint_tracks_semantics_but_not_explanation():
    # Explanation is deliberately absent from the API and therefore cannot
    # perturb semantic candidate identity.
    assert _candidate() == _candidate()
    assert _candidate(outcome="partial") != _candidate()
    assert _candidate(evidence=("different",)) != _candidate()
    assert _candidate(
        grounded=({"span_id": "span-2", "raw_start": 4},)
    ) != _candidate()


def test_fingerprint_rejects_non_typed_canonical_values():
    with pytest.raises(ValueError, match="typed canonical"):
        payload_fingerprint({"bad": {1}})


def test_v2_distinguishes_types_and_namespaces():
    value_list = ["x", 1]
    value_tuple = ("x", 1)
    list_v2 = payload_fingerprint(value_list)
    tuple_v2 = payload_fingerprint(value_tuple)
    assert list_v2 == "3a483875c99185768aae5a097e5aba4129ec405615c2ff22e0cfb64953139029"
    assert tuple_v2 == "764bb40c50bcc832286a683655c497b2d83f04d2208024c1b294cc03243611fc"
    assert list_v2 != tuple_v2

    namespace_value = {"claim_id": "claim-1", "ref_id": "ref-1"}
    assert payload_fingerprint(namespace_value, version=FINGERPRINT_VERSION_V2) == "19b67d78bf88e20630f1e17784453bb5722a491fd6a015a3b88612b5df2eeee3"
    assert policy_fingerprint(namespace_value, version=FINGERPRINT_VERSION_V2) == "3401fbb9a4b107a968061d978bf1dab9d6c8acb3f9e278698d63bd08c0416578"
    assert fingerprint(namespace_value, version=FINGERPRINT_VERSION_V2) == "a658065025808dcded42e817de234b8618e703a3bf9b7b1aba719f786eb37964"


def test_v2_fingerprint_fails_closed_for_unknown_version_or_typed_rejection():
    with pytest.raises(ValueError, match="unknown fingerprint version"):
        payload_fingerprint({}, version="v3")
    with pytest.raises(ValueError, match="typed canonical"):
        payload_fingerprint({"bad": {1}}, version=FINGERPRINT_VERSION_V2)


def test_answer_fingerprint_uses_v2_and_rejects_other_versions():
    answer = {"logical_request_id": "request", "decision": {"evidence": ["x"]}}
    assert answer_fingerprint(answer) == answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2)
    with pytest.raises(ValueError, match="unknown fingerprint version"):
        answer_fingerprint(answer, version="v1")
