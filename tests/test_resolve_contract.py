#!/usr/bin/env python3
# tests/test_resolve_contract.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests for additive Phase 1 resolver output fields."""

from __future__ import annotations

from tests.test_resolve_golden import load_cases, run_case


import unittest


CASE_EXPECTATIONS = {
    "doi_crossref_resolved": {
        "identity_state": "resolved_strong_declared",
        "identity": {"scheme": "doi", "value": "10.1000/valid", "is_strong": True, "class_": "global"},
    },
    "doi_crossref_and_handle_not_found": {
        "identity_state": "fabricated",
        "identity": {"scheme": "doi", "value": "10.1000/missing", "is_strong": True, "class_": "global"},
    },
    "doi_identifier_mismatch": {
        "identity_state": "fabricated",
        "identity": {"scheme": "doi", "value": "10.1000/wrong", "is_strong": True, "class_": "global"},
    },
    "pmid_pubmed_confirms_after_epmc_empty": {
        "identity_state": "resolved_strong_declared",
        "identity": {"scheme": "pmid", "value": "12345", "is_strong": True, "class_": "global"},
    },
    "title_only_openalex_resolved": {
        "identity_state": "weakly_corroborated",
        "identity": {"scheme": "doi", "value": "10.48550/arxiv.1706.03762", "is_strong": True, "class_": "global"},
    },
    "title_only_openalex_late_correct": {
        "identity_state": "weakly_corroborated",
        "identity": {"scheme": "doi", "value": "10.1000/good", "is_strong": True, "class_": "global"},
    },
    "arxiv_title_search_without_doi": {
        "identity_state": "resolved_strong_discovered",
        "identity": {"scheme": "arxiv_id", "value": "1409.3215", "is_strong": True, "class_": "global"},
    },
    "acl_direct_url": {
        "identity_state": "resolved_strong_discovered",
        "identity": {"scheme": "acl_id", "value": "J93-2004", "is_strong": True, "class_": "authority_local"},
    },
    "isbn_resolved_openlibrary": {
        "identity_state": "resolved_strong_declared",
        "identity": {"scheme": "isbn", "value": "9780226458083", "is_strong": True, "class_": "global"},
    },
    "book_title_only_absent": {
        "identity_state": "unverified",
        "identity": {"scheme": "none", "value": "", "is_strong": False, "class_": "none"},
    },
    "retracted_doi": {
        "identity_state": "resolved_strong_declared",
        "identity": {"scheme": "doi", "value": "10.1000/retracted", "is_strong": True, "class_": "global"},
    },
    "no_identifier_unverified": {
        "identity_state": "unverified",
        "identity": {"scheme": "none", "value": "", "is_strong": False, "class_": "none"},
    },
}


class TestResolveContract(unittest.TestCase):
    def test_phase_one_fields_are_present_and_mapped(self):
        for case in load_cases():
            expected = CASE_EXPECTATIONS[case["name"]]
            with self.subTest(case=case["name"]):
                result = run_case(case)
                self.assertEqual(result["identity_state"], expected["identity_state"])
                self.assertEqual(result["identity"], expected["identity"])
                self.assertIn("evidence_payload", result)
                self.assertIn("retraction", result)
                self.assertIn("trace", result)
                self.assertEqual(result["evidence_payload"]["via"], result["via"])
                self.assertEqual(result["evidence_payload"]["matched_title"], result["matched_title"])
                self.assertEqual(result["evidence_payload"]["oa_status"], result["oa_status"])
                self.assertEqual(result["evidence_payload"]["fulltext_exists"], result["fulltext_exists"])
                self.assertEqual(result["retraction"]["status"], "retracted" if result["retracted"] else result["retraction"]["status"])
                self.assertTrue(result["trace"])
                self.assertEqual(result["trace"][-1]["stage"], "final_resolution")
                self.assertEqual(result["trace"][-1]["outcome"], result["identity_state"])

    def test_retraction_projection_matches_resolver_state(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                result = run_case(case)
                if result["retracted"]:
                    self.assertEqual(result["retraction"]["status"], "retracted")
                elif result["retraction"]["checked_via"] == "authority_metadata":
                    self.assertEqual(result["retraction"]["status"], "not_retracted")
