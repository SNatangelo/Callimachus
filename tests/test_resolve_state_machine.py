#!/usr/bin/env python3
# tests/test_resolve_state_machine.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase 3 state-machine regression tests for resolver outputs."""

from __future__ import annotations

import unittest

from core.resolve.decision import _build_resolution_decision, _normalize_identity_value
from tests.test_resolve_golden import load_cases, run_case


def _case_map() -> dict[str, dict]:
    return {case["name"]: case for case in load_cases()}


def _trace_row(result: dict, stage: str) -> dict:
    for row in result.get("trace") or []:
        if row.get("stage") == stage:
            return row
    raise AssertionError(f"Missing trace stage {stage!r}")


def _crossref_landing_doi_decision(declared_doi: str, landing_doi: str):
    ref = {
        "title": "Synthetic case-equivalence control",
        "doi": declared_doi,
        "pmid": None,
    }
    result = {
        "status": "resolved",
        "via": "crossref",
        "fulltext_links": [{
            "url": f"HTTPS://DX.DOI.ORG/{landing_doi}",
            "content_type": "text/html",
        }],
        "matched_title": ref["title"],
        "auxiliary_fulltext_links": [],
        "oa_status": "unknown",
        "fulltext_exists": "unknown",
        "work_type": None,
    }
    decision = _build_resolution_decision(
        ref,
        result,
        [],
        status="resolved",
        via="crossref",
        reference_status_tag=None,
        fabrication_risk=None,
        resolution_basis="metadata_search",
        meta=result,
        bibliographic_identity_status="identified",
    )
    return decision, result


class TestResolveStateMachine(unittest.TestCase):
    def test_case_only_crossref_landing_doi_matches_declared_identity(self):
        decision, result = _crossref_landing_doi_decision(
            "https://doi.org/10.5555/Synthetic.CaseOnlyX",
            "10.5555/synthetic.caseonlyx",
        )
        self.assertNotIn("doi", result)
        self.assertNotIn("resolved_identifier", result)
        self.assertEqual(decision.identity_state, "resolved_strong_declared")

    def test_doi_equivalence_is_ascii_only_and_preserves_other_schemes(self):
        normalize = _normalize_identity_value
        self.assertEqual(
            normalize("doi", "HTTPS://DX.DOI.ORG/10.5555/Synthetic.CaseX"),
            "10.5555/synthetic.casex",
        )
        self.assertNotEqual(
            normalize("doi", "10.5555/Synthetic.À"),
            normalize("doi", "10.5555/Synthetic.à"),
        )
        self.assertNotEqual(
            normalize("doi", "10.5555/Synthetic.CaseX"),
            normalize("doi", "10.5555/Synthetic.CaseY"),
        )
        self.assertEqual(
            normalize("url", "HTTPS://Example.test/Case"),
            "HTTPS://Example.test/Case",
        )
        different = _crossref_landing_doi_decision(
            "10.5555/Synthetic.CaseX", "10.5555/Synthetic.CaseY",
        )[0]
        unicode_case_pair = _crossref_landing_doi_decision(
            "10.5555/Synthetic.À", "10.5555/Synthetic.à",
        )[0]
        self.assertEqual(different.identity_state, "weakly_corroborated")
        self.assertEqual(unicode_case_pair.identity_state, "weakly_corroborated")

    def test_discovered_doi_requires_typed_validation_before_strong_trace(self):
        ref = {"title": "The cited paper", "doi": None, "pmid": None}
        result = {
            "status": "resolved", "via": "europepmc",
            "doi": "10.1000/candidate", "matched_title": "The cited paper",
            "fulltext_links": [], "auxiliary_fulltext_links": [],
            "oa_status": "unknown", "fulltext_exists": "unknown", "work_type": None,
        }

        def decision_for(candidate):
            return _build_resolution_decision(
                ref, candidate, [], status="resolved", via="europepmc",
                reference_status_tag=None, fabrication_risk=None,
                resolution_basis="metadata_search", meta=candidate,
                bibliographic_identity_status="identified",
            )

        unvalidated = decision_for(result)
        self.assertEqual(unvalidated.identity.to_dict()["value"], "10.1000/candidate")
        self.assertEqual(unvalidated.identity_state, "weakly_corroborated")
        stages = [row["stage"] for row in unvalidated.trace]
        self.assertNotIn("strong_discovery", stages)
        self.assertNotIn("strong_confirmed", stages)

        validated = decision_for({
            **result,
            "resolved_identifier": {
                "type": "doi", "value": "10.1000/candidate",
                "validated_via": "crossref",
            },
        })
        self.assertEqual(validated.identity_state, "resolved_strong_discovered")
        stages = [row["stage"] for row in validated.trace]
        self.assertIn("strong_discovery", stages)
        self.assertIn("strong_confirmed", stages)

    def test_fabrication_bar(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                before = case["expected"]["identity_state"]
                after = run_case(case)["identity_state"]
                self.assertFalse(before != "fabricated" and after == "fabricated")

    def test_declared_strong_resolution_trace(self):
        result = run_case(_case_map()["doi_crossref_resolved"])
        self.assertEqual(result["identity_state"], "resolved_strong_declared")
        self.assertEqual(_trace_row(result, "declared_present")["outcome"], "present")
        self.assertEqual(_trace_row(result, "declared_cache_lookup")["outcome"], "not_checked")
        self.assertEqual(_trace_row(result, "declared_validation")["outcome"], "matched")
        self.assertEqual(_trace_row(result, "strong_confirmed")["outcome"], "confirmed")
        self.assertEqual(_trace_row(result, "final_resolution")["outcome"], "resolved_strong_declared")

    def test_unadmitted_discovered_identifier_stays_nonoperative(self):
        case = _case_map()["title_only_openalex_resolved"]
        openalex_response = next(
            response for response in case["responses"]
            if "api.openalex.org/works?" in response["match"]
        )
        openalex_response["json"]["results"][0]["authorships"][0]["author"][
            "display_name"
        ] = "Synthetic Different Author"
        result = run_case(case)
        candidate_attempt = next(
            attempt for attempt in result["attempts"]
            if attempt.get("via") == "openalex_search"
        )
        self.assertFalse(candidate_attempt["metadata_match"]["author_match"])
        self.assertTrue(any(
            link.get("url") == "https://doi.org/10.48550/arXiv.1706.03762"
            for link in candidate_attempt.get("fulltext_links") or []
        ))
        self.assertEqual(result["identity_state"], "unverified")
        self.assertEqual(result["identity"]["scheme"], "none")
        self.assertEqual(result["identity"]["value"], "")
        self.assertNotIn("resolved_identifier", result)
        self.assertNotIn("identifiers", result)
        self.assertEqual(_trace_row(result, "declared_present")["outcome"], "absent")
        self.assertEqual(_trace_row(result, "weak_aggregation")["outcome"], "unverified")
        self.assertEqual(_trace_row(result, "final_resolution")["outcome"], "unverified")
        stages = [row["stage"] for row in result["trace"]]
        self.assertNotIn("strong_discovery", stages)
        self.assertNotIn("strong_confirmed", stages)

    def test_unvalidated_openalex_metadata_candidate_stays_weak(self):
        result = run_case(_case_map()["title_only_openalex_resolved"])
        self.assertEqual(result["identity_state"], "weakly_corroborated")
        self.assertEqual(
            result["identity"]["value"], "10.48550/arxiv.1706.03762",
        )
        self.assertTrue(result["evidence_profile"]["metadata_match"]["author_match"])
        self.assertNotIn("resolved_identifier", result)
        self.assertNotIn("identifiers", result)
        stages = [row["stage"] for row in result["trace"]]
        self.assertNotIn("strong_discovery", stages)
        self.assertNotIn("strong_confirmed", stages)

    def test_declared_failure_trace_stays_nonrecoverable_when_fixture_is_fabricated(self):
        result = run_case(_case_map()["doi_crossref_and_handle_not_found"])
        self.assertEqual(result["identity_state"], "fabricated")
        self.assertEqual(_trace_row(result, "declared_validation")["outcome"], "failed")
        self.assertEqual(_trace_row(result, "weak_aggregation")["outcome"], "fabricated")
        self.assertEqual(_trace_row(result, "final_resolution")["outcome"], "fabricated")

    def test_unverified_without_identifier_stays_weak_aggregation(self):
        result = run_case(_case_map()["no_identifier_unverified"])
        self.assertEqual(result["identity_state"], "unverified")
        self.assertEqual(_trace_row(result, "declared_present")["outcome"], "absent")
        self.assertEqual(_trace_row(result, "weak_aggregation")["outcome"], "unverified")
        self.assertEqual(_trace_row(result, "final_resolution")["outcome"], "unverified")
        stages = [row["stage"] for row in result["trace"]]
        self.assertNotIn("declared_validation", stages)
