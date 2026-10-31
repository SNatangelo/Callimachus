# tests/test_report_html_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused contracts for the presentation-neutral human report projection."""

from __future__ import annotations

import json

from tests._bootstrap import *  # noqa: F401,F403

from core.infra.credential_catalog import credential_catalog
from core.invocation import project_venv_executable


_CREDENTIAL_COUNT_FIELDS = (
    "calls", "successful_http_responses", "http_401", "http_403",
    "http_429", "other_http_errors", "network_errors",
)


def _credential_metrics(*, core_counts=None):
    rows = []
    for item in credential_catalog():
        provider, env_name = item["provider"], item["env_name"]
        counts = {field: 0 for field in _CREDENTIAL_COUNT_FIELDS}
        if env_name == "CORE_API_KEY" and core_counts is not None:
            counts.update(core_counts)
        calls = counts["calls"]
        warning = (
            "credential_rejected_or_not_entitled"
            if calls and counts["http_401"] + counts["http_403"] == calls
            else None
        )
        rows.append({
            "provider": provider,
            "env_name": env_name,
            "present_at_start": env_name == "CORE_API_KEY",
            "used": calls > 0,
            **counts,
            "by_channel": {
                "resolve": dict(counts),
                "fetch": {field: 0 for field in _CREDENTIAL_COUNT_FIELDS},
                "search": {field: 0 for field in _CREDENTIAL_COUNT_FIELDS},
            },
            "warning_code": warning,
        })
    totals = {
        field: sum(row[field] for row in rows)
        for field in _CREDENTIAL_COUNT_FIELDS
    }
    totals.update({
        "keys_present_at_start": sum(row["present_at_start"] for row in rows),
        "keys_used": sum(row["used"] for row in rows),
    })
    return {
        "recorded": True,
        "recorded_at": "2026-09-06T00:00:00Z",
        "rows": rows,
        "totals": totals,
    }


def _core_row(metrics):
    return next(
        row for row in metrics["rows"] if row["env_name"] == "CORE_API_KEY"
    )


class TestHumanReportProjection(unittest.TestCase):
    def _run_projection(self):
        return {
            "parse_effective": {
                "manuscript": {
                    "id": "run-1", "filename": "paper.pdf", "sha256": "abc",
                    "title": "A Clear Report Title for Readers",
                },
                "claims": [
                    {"id": "c2", "sentence": "Second claim [2].", "marker_raw": "[2]"},
                    {"id": "c1", "sentence": "First claim [1].", "marker_raw": "[1]"},
                ],
                "references": [
                    {"id": "r2", "ref_number": 2, "title": "Second source", "cited_coordinates": [
                        {"kind": "container", "normalized_value": "Journal"},
                        {"kind": "volume", "normalized_value": "1"},
                    ]},
                    {"id": "r1", "ref_number": 1, "title": "First source"},
                ],
                "citations": [
                    {"claim_id": "c2", "ref_id": "r2", "ref_number": 2},
                    {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
                    {"claim_id": "c2", "ref_id": None, "marker_raw": "[missing]"},
                ],
                "table_only_citations": [
                    {"ref_id": "r1", "ref_number": 1, "marker_raw": "[1]"},
                ],
                "coverage": {"claim_count": 2},
            },
            "resolve_map": {
                "r1": {"status": "resolved", "title": "First source"},
                "r2": {"status": "unverified", "title": "Second source"},
            },
            "manifest": {"entries": [
                {"ref_id": "r1", "source_text_id": "source-1", "tier": "fulltext"},
                {"ref_id": "r2", "source_text_id": "source-2", "tier": "abstract"},
            ]},
            "fetch_attempts_by_ref": {"r1": [{"attempt_id": "fetch-1", "attempt_order": 1}]},
            "verification_pair_states": [
                {"claim_id": "c1", "ref_id": "r1", "scope": "fulltext_complete", "status": "accepted",
                 "terminal_at": "2026-01-01T00:00:00Z"},
                {"claim_id": "c2", "ref_id": "r2", "scope": "abstract_only", "status": "exhausted",
                 "terminal_at": "2026-01-01T00:00:00Z"},
            ],
            "verification_projection": [
                {"claim_id": "c1", "ref_id": "r1", "scope": "fulltext_complete",
                 "semantic_outcome": "supports", "result_class": "positive",
                 "crediting": True, "operational_complete": True},
                {"claim_id": "c2", "ref_id": "r2", "scope": "abstract_only",
                 "semantic_outcome": None, "result_class": "unresolved",
                 "crediting": False, "operational_complete": True},
            ],
            "verification_raw": {
                "candidates": [], "candidate_events": [], "logical_requests": [],
                "jury1_rejections": [], "dispatch_attempts": [], "dispatch_events": [],
            },
            "verify_runtime": {"accuracy": "maximum"},
            "verify_policy": {"jury2_level": "medium", "context_mode": "full_text"},
            "execution": {"debug_mode": False, "debug_labels": []},
            "manuscript_text": (
                "A Clear Report Title for\nReaders\n"
                "Ada Lovelace\nGrace Hopper\nExample University\nAbstract\nBody"
            ),
            "llm_metrics": {"by_provider_model_role": [], "totals": {"attempts": 0}},
            "manual_parse_adjudication": [],
            "unreadable": {"entries": []},
        }

    def test_review_labels_are_informational_and_do_not_change_assessment_or_concerns(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        assessment_before = build_human_report_projection(
            projection
        ).as_dict()["overview"]["assessment"]
        projection["resolve_map"]["r2"].update({
            "attempts": [
                {"identity_search": {"resolver": "crossref", "completion": "complete", "outcome": "no_compatible_identity"}},
                {"identity_search": {"resolver": "openalex", "completion": "complete", "outcome": "candidate_incompatible"}},
            ],
            "evidence_profile": {
                "bibliographic_adjudication": {"outcome": "checks_incomplete", "identity_status": "not_identified", "check_status": "incomplete", "correction_status": "not_found"},
                "resolver_coverage": {"authority": {"record_id": "nlm:1", "snapshot_sha256": "a" * 64, "canonical_title": "Journal"}, "article_lookups": [{"resolver": "pubmed", "query_contract": "coordinates", "scope": "canonical journal/year/volume/first locator", "completion": "complete", "match_status": "no_compatible_article", "http_status": 200, "source_url": "https://example.invalid", "response_sha256": "b" * 64, "reason": "complete lookup"}]},
            },
        })

        result = build_human_report_projection(projection).as_dict()
        source = next(item for item in result["sources"] if item["reference"]["id"] == "r2")
        self.assertEqual(
            [item["code"] for item in source["bibliographic_review_labels"]],
            ["incomplete_bibliographic_source", "not_found_after_completed_searches", "no_compatible_article_at_cited_coordinates"],
        )
        self.assertIsNone(source["bibliographic_concern"])
        self.assertEqual(result["overview"]["sources_completed_search_review"], 1)
        self.assertEqual(result["overview"]["sources_coordinate_lookup_review"], 1)
        self.assertEqual(result["overview"]["sources_incomplete_bibliographic_source"], 1)
        self.assertEqual(result["overview"]["sources_suspected_fabricated"], 0)
        self.assertEqual(result["overview"]["sources_elevated_bibliographic_suspicion"], 0)
        self.assertEqual(result["overview"]["assessment"], assessment_before)

    def test_author_list_discrepancy_requires_review_without_refuting_identity(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        projection["parse_effective"]["references"][0].update({
            "doi": "10.1001/jama.2017.3091",
            "year": 2017,
            "raw_entry": (
                "Tringale KR, Hattangadi-Gluth JA, Tannenbaum SL, et al. "
                "Types and distribution. JAMA. 2017."
            ),
        })
        projection["resolve_map"]["r2"].update({
            "status": "resolved",
            "attempts": [{
                "status": "resolved", "via": "crossref",
                "matched_authors": ["Tringale", "Marshall", "Hattangadi-Gluth"],
            }],
            "evidence_profile": {
                "bibliographic_adjudication": {
                    "outcome": "identified", "identity_status": "identified",
                },
            },
        })
        projection["verification_projection"][1].update({
            "semantic_outcome": "supports", "result_class": "positive", "crediting": True,
        })

        result = build_human_report_projection(projection).as_dict()
        source = next(item for item in result["sources"] if item["reference"]["id"] == "r2")

        self.assertEqual(source["resolve"]["status"], "resolved")
        self.assertIsNone(source["bibliographic_concern"])
        self.assertEqual(source["bibliographic_review_labels"][0]["code"], "author_list_discrepancy")
        self.assertEqual(result["overview"]["assessment"], {
            "state": "yellow",
            "hard_findings": 0,
            "review_findings": 1,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": ["review.source.author_list_discrepancy"],
        })

    def _support_mix_projection(self, *, partials: int, total: int = 10):
        projection = self._run_projection()
        projection["parse_effective"]["claims"] = [
            {"id": f"c{index}", "sentence": f"Claim {index} [1].", "marker_raw": "[1]"}
            for index in range(1, total + 1)
        ]
        projection["parse_effective"]["citations"] = [
            {"claim_id": f"c{index}", "ref_id": "r1", "ref_number": 1}
            for index in range(1, total + 1)
        ]
        projection["parse_effective"]["table_only_citations"] = []
        projection["verification_pair_states"] = []
        projection["verification_projection"] = [
            {
                "claim_id": f"c{index}", "ref_id": "r1",
                "scope": "fulltext_complete",
                "semantic_outcome": "partial" if index <= partials else "supports",
                "result_class": "incomplete" if index <= partials else "positive",
                "crediting": index > partials,
                "operational_complete": True,
            }
            for index in range(1, total + 1)
        ]
        return projection

    def test_projection_is_deterministic_json_and_cross_linked(self):
        from core.report.human.projection import build_human_report_projection

        first = build_human_report_projection(self._run_projection()).as_dict()
        second = build_human_report_projection(self._run_projection()).as_dict()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(json.dumps(first, sort_keys=True)), first)
        self.assertEqual(first["overview"], {
            "claims_total": 2,
            "references_total": 2,
            "references_cited": 2,
            "citations_total": 3,
            "orphan_citations": 1,
            "pairs_total": 2,
            "pairs_completed": 2,
            "pairs_by_semantic_outcome": {"no_outcome": 1, "supports": 1},
            "pairs_by_result_class": {"positive": 1, "unresolved": 1},
            "claims_with_crediting_support": 1,
            "source_identity_counts": {"resolved": 1, "unverified": 1},
            "sources_identity_confirmed": 1,
            "sources_identity_not_automatically_confirmed": 1,
            "sources_suspected_fabricated": 0,
            "sources_elevated_bibliographic_suspicion": 0,
            "sources_completed_search_review": 0,
            "sources_coordinate_lookup_review": 0,
            "sources_incomplete_bibliographic_source": 0,
            "sources_retracted": 0,
            "table_only_citations": 1,
            "assessment": {
                "state": "yellow",
                "hard_findings": 1,
                "review_findings": 0,
                "minor_findings": 0,
                "technical_findings": 1,
                "reason_codes": [
                    "hard.claim.orphan_citation",
                    "technical.claim.no_semantic_outcome",
                ],
            },
        })
        self.assertEqual(first["execution"], {
            "debug_mode": False, "debug_labels": [],
        })
        self.assertEqual(first["manuscript"]["display_title"], "A Clear Report Title for Readers")
        self.assertEqual([source["reference"]["id"] for source in first["sources"]], ["r1", "r2"])
        self.assertEqual([source["display_id"] for source in first["sources"]], ["[1]", "[2]"])
        self.assertEqual(first["sources"][0]["claim_ids"], ["c1"])
        self.assertEqual(first["sources"][0]["claim_display_ids"], ["C2"])
        self.assertEqual([claim["claim"]["id"] for claim in first["claims"]], ["c2", "c1"])
        self.assertEqual([claim["display_id"] for claim in first["claims"]], ["C1", "C2"])
        self.assertEqual([claim["focus_text"] for claim in first["claims"]], [
            "Second claim.", "First claim.",
        ])
        self.assertEqual(first["claims"][1]["pair_ids"], [{
            "claim_id": "c1", "claim_display_id": "C2",
            "ref_id": "r1", "source_display_id": "[1]",
        }])
        self.assertEqual([pair["claim_id"] for pair in first["pairs"]], ["c2", "c1"])
        self.assertEqual(
            [(pair["claim_display_id"], pair["source_display_id"]) for pair in first["pairs"]],
            [("C1", "[2]"), ("C2", "[1]")],
        )
        self.assertEqual(first["configuration"]["verify_policy"]["jury2_level"], "medium")
        self.assertEqual(first["table_only_citations"], [{
            "marker_raw": "[1]", "ref_id": "r1", "ref_number": 1,
            "source_display_citation": "First source", "source_display_id": "[1]",
        }])
        self.assertEqual(first["attention"], [
            {"entity": "citation", "claim_id": "c2", "claim_display_id": "C1", "reason": "orphan",
             "citation": {"claim_id": "c2", "marker_raw": "[missing]", "ref_id": None}},
            {"entity": "pair", "claim_id": "c2", "claim_display_id": "C1",
             "ref_id": "r2", "source_display_id": "[2]", "source_display_citation": "Second source",
             "result_class": "unresolved", "semantic_outcome": None, "resolution": None},
            {"entity": "source", "ref_id": "r2", "source_display_id": "[2]",
             "source_display_citation": "Second source", "identity_status": "unverified"},
        ])

    def test_claimless_link_silent_marker_is_coverage_not_a_report_pair(self):
        from core.report.human.projection import build_human_report_projection
        from core.report.human.render import render_human_report

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = [
            {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
            {
                "claim_id": None,
                "ref_id": "r2",
                "ref_number": 2,
                "provenance": "link_silent_resolved",
                "marker_raw": "2020",
            },
        ]
        projection["verification_pair_states"] = [
            projection["verification_pair_states"][0]
        ]
        projection["verification_projection"] = [
            projection["verification_projection"][0]
        ]

        value = build_human_report_projection(projection).as_dict()
        rendered = render_human_report(build_human_report_projection(projection))

        self.assertEqual(value["overview"]["citations_total"], 2)
        self.assertEqual(value["overview"]["references_cited"], 1)
        self.assertEqual(value["overview"]["pairs_total"], 1)
        self.assertEqual([(pair["claim_id"], pair["ref_id"]) for pair in value["pairs"]], [("c1", "r1")])
        source = next(item for item in value["sources"] if item["reference"]["id"] == "r2")
        self.assertEqual(source["claim_ids"], [])
        self.assertIn("<!doctype html>", rendered.body)

    def test_remediation_guidance_dedupes_terminal_causes_and_rejects_private_fields(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_pair_states"][1]["terminal_cause"] = "protocol_invalid"
        projection["remediation"] = {
            "interventions": [
                {
                    "kind": "operator_fetch", "effect": "operator_supplied_evidence",
                    "task_id": "task-1", "answer_id": "answer-1", "claim_id": None,
                    "ref_id": "r1", "source_id": "source-1", "action": "submitted",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "admission_classification": "authority_authenticated_operator",
                },
                {
                    "kind": "source_identity_attestation",
                    "effect": "operator_identity_attestation",
                    "task_id": "task-2", "answer_id": "answer-2", "claim_id": None,
                    "ref_id": "r2", "source_id": "source-2",
                    "action": "attest_identity", "applied_at": "2026-01-02T00:00:00Z",
                    "admission_classification": "local_operator_unattested",
                },
            ],
            "child_run": {
                "run_origin": "remediated_from_completed", "parent_run_id": "parent-1",
                "provenance": {
                    "parent_run_id": "parent-1", "parent_input_sha256": "a" * 64,
                    "parent_report_sha256": "b" * 64, "parent_journal_sha256": "c" * 64,
                    "source_inventory_sha256": "d" * 64, "created_at": "2026-01-01T00:00:00Z",
                },
            },
            "footnote_split_required": [{"note_number": 7}],
        }
        value = build_human_report_projection(projection).as_dict()["remediation"]
        assert any(item["finding_kind"] == "terminal_cause" and item["code"] == "protocol_invalid" for item in value["guidance"])
        attribution = next(item for item in value["guidance"] if item["code"] == "hard.claim.orphan_citation")
        assert attribution["child_run_command"] is None
        assert attribution["child_run_routes"][0]["child_start_command"].endswith("--manual-review")
        footnote = next(item for item in value["guidance"] if item["code"] == "footnote_source_review")
        assert footnote["child_run_routes"][0]["child_start_command"].endswith("--manual-review")
        assert value["interventions"][0]["answer_id"] == "answer-1"
        assert value["interventions"][1]["effect"] == "operator_identity_attestation"
        assert "authority_id" not in json.dumps(value)
        projection["remediation"]["interventions"][0]["raw_payload"] = {"url": "https://secret.test"}
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            build_human_report_projection(projection)

    def test_incomplete_coverage_uses_generic_completed_child_command(self):
        from core.report.human.projection import _remediation_projection

        value = _remediation_projection(
            {"reason_codes": ["technical.claim.incomplete_pair_coverage"]}, [],
            {"remediation": {"interventions": [], "child_run": None}},
        )
        assert value["guidance"][0]["child_run_routes"] == []
        assert value["guidance"][0]["child_run_command"] == (
            f"{project_venv_executable()} run.py --remediate-completed "
            "{PARENT_RUN} --run {CHILD_RUN}"
        )

    def test_current_projection_never_guesses_title_from_manuscript_prose(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["manuscript"].pop("title")
        projection["manuscript_text"] = (
            "Provided proper attribution is provided, Google hereby grants permission "
            "to reproduce the tables and figures in this paper.\n"
            "Attention Is All You Need\nAshish Vaswani∗\nAbstract\nBody"
        )

        result = build_human_report_projection(projection).as_dict()

        self.assertIsNone(result["manuscript"]["display_title"])

    def test_assessment_is_green_for_clean_table_only_and_unverified_sources(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        projection["verification_projection"][1].update({
            "semantic_outcome": "supports",
            "result_class": "positive",
            "crediting": True,
        })

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "green",
            "hard_findings": 0,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": [],
        })

    def test_assessment_requires_review_for_parser_low_reference_coverage(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        projection["parse_effective"]["coverage"] = {
            "cited": 1,
            "total": 15,
            "pct": 6.7,
            "in_prose": 1,
            "in_tables": 0,
            "uncited": list(range(2, 16)),
            "warning": "only 1 of 15 references are ever cited",
            "fatal": False,
        }
        projection["verification_projection"][1].update({
            "semantic_outcome": "supports",
            "result_class": "positive",
            "crediting": True,
        })

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "yellow",
            "hard_findings": 0,
            "review_findings": 1,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": ["review.manuscript.low_reference_coverage"],
        })

    def test_assessment_is_yellow_for_partial_unresolved_and_one_hard_claim(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_projection"][0].update({
            "semantic_outcome": "partial",
            "result_class": "incomplete",
        })

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "yellow",
            "hard_findings": 1,
            "review_findings": 1,
            "minor_findings": 0,
            "technical_findings": 1,
            "reason_codes": [
                "hard.claim.orphan_citation",
                "review.claim.partial_support",
                "technical.claim.no_semantic_outcome",
            ],
        })

    def test_assessment_treats_ambiguous_citation_as_technical_not_orphan(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = [
            {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
            {
                "claim_id": "c2",
                "ref_id": None,
                "marker_raw": "[1 or 2]",
                "candidate_ref_ids": ["r1", "r2"],
            },
        ]
        projection["verification_projection"] = [
            projection["verification_projection"][0]
        ]

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "green",
            "hard_findings": 0,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 1,
            "reason_codes": [
                "technical.claim.ambiguous_citation",
                "technical.claim.no_semantic_outcome",
            ],
        })

    def test_assessment_is_green_for_a_minor_share_of_partial_support(self):
        from core.report.human.projection import build_human_report_projection

        result = build_human_report_projection(
            self._support_mix_projection(partials=1)
        ).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "green",
            "hard_findings": 0,
            "review_findings": 0,
            "minor_findings": 1,
            "technical_findings": 0,
            "reason_codes": ["minor.claim.partial_support"],
        })

    def test_assessment_is_yellow_when_partial_support_exceeds_ten_percent(self):
        from core.report.human.projection import build_human_report_projection

        result = build_human_report_projection(
            self._support_mix_projection(partials=2)
        ).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "yellow",
            "hard_findings": 0,
            "review_findings": 2,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": ["review.claim.partial_support"],
        })

    def test_assessment_is_red_for_two_distinct_contradicted_claims(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        for row in projection["verification_projection"]:
            row.update({"semantic_outcome": "contradicts", "result_class": "negative"})

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "red",
            "hard_findings": 2,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": ["hard.claim.uncontested_contradiction"],
        })

    def test_assessment_deduplicates_a_bad_source_used_by_many_claims(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = [
            {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
            {"claim_id": "c2", "ref_id": "r1", "ref_number": 1},
        ]
        projection["verification_projection"] = [{
            "claim_id": "c1", "ref_id": "r1", "scope": "fulltext_complete",
            "semantic_outcome": "supports", "result_class": "positive",
            "crediting": True, "operational_complete": True,
        }, {
            "claim_id": "c2", "ref_id": "r1", "scope": "fulltext_complete",
            "semantic_outcome": "supports", "result_class": "positive",
            "crediting": True, "operational_complete": True,
        }]
        projection["resolve_map"]["r1"]["status"] = "suspected_fabricated"

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "red",
            "hard_findings": 1,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": [
                "hard.source.high_fabrication_suspicion",
                "hard.source.suspected_fabricated",
            ],
        })

    def test_report_promotes_persisted_high_and_elevated_bibliographic_signals(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = (
            projection["parse_effective"]["citations"][:2]
        )
        for row in projection["verification_projection"]:
            row.update({
                "semantic_outcome": "supports", "result_class": "positive",
                "crediting": True,
            })
        projection["resolve_map"]["r1"] = {
            "status": "unverified",
            "evidence_profile": {"resolver_coverage": {
                "suspicion_level": "high",
                "conclusion": "fresh_covered_same_resolver_complete_article_miss",
                "authority": {
                    "canonical_title": "Journal of Tests", "issns": ["1234-5678"],
                },
                "observations": [{
                    "resolver": "pubmed", "fresh": True, "status": "covered",
                    "completion": "complete", "http_status": 200,
                }],
                "article_lookups": [{
                    "resolver": "pubmed", "completion": "complete",
                    "match_status": "no_compatible_article", "http_status": 200,
                    "scope": "canonical journal/year/volume/first locator",
                }],
            }},
        }
        projection["resolve_map"]["r2"] = {
            "status": "unverified",
            "evidence_profile": {"bibliographic_suspicion": {
                "suspicion_level": "elevated",
                "conclusion": "unconfirmed_or_unrecognized_journal_with_completed_bibliographic_misses",
                "providers": ["crossref", "openalex"],
            }},
        }

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["sources_suspected_fabricated"], 1)
        self.assertEqual(
            result["overview"]["sources_elevated_bibliographic_suspicion"], 1,
        )
        self.assertEqual(result["overview"]["assessment"], {
            "state": "red",
            "hard_findings": 1,
            "review_findings": 1,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": [
                "hard.source.high_fabrication_suspicion",
                "review.source.elevated_bibliographic_suspicion",
            ],
        })
        concerns = {
            item["reference"]["id"]: item["bibliographic_concern"]
            for item in result["sources"]
        }
        self.assertEqual(concerns["r1"]["level"], "high_fabrication_suspicion")
        self.assertEqual(concerns["r1"]["resolver"], "pubmed")
        self.assertEqual(
            concerns["r2"]["level"], "elevated_bibliographic_suspicion",
        )
        self.assertEqual(concerns["r2"]["providers"], ["crossref", "openalex"])

    def test_report_distinguishes_positive_refutation_from_search_absence(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["resolve_map"]["r1"] = {
            "status": "unverified", "reference_status_tag": "suspected_fabricated",
            "evidence_profile": {"bibliographic_adjudication": {
                "outcome": "refuted",
                "refutations": [{
                    "kind": "coordinate_occupied_by_other_work",
                    "source": "pubmed_coordinate_occupancy",
                    "cited_value": "Journal, 23, 261",
                    "observed_value": "A different article",
                }],
            }},
        }

        result = build_human_report_projection(projection).as_dict()
        concern = next(
            item["bibliographic_concern"] for item in result["sources"]
            if item["reference"]["id"] == "r1"
        )

        self.assertEqual(concern["level"], "reference_refuted")
        self.assertEqual(concern["basis"], "positive_refutation")
        self.assertEqual(concern["observed_work"], "A different article")
        self.assertEqual(result["overview"]["assessment"]["state"], "red")

    def test_report_preserves_complete_issue_absence_without_inventing_an_observed_work(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["resolve_map"]["r1"] = {
            "status": "unverified", "reference_status_tag": "suspected_fabricated",
            "evidence_profile": {"bibliographic_adjudication": {
                "outcome": "refuted",
                "refutations": [{
                    "kind": "absent_from_complete_issue",
                    "source": "pmc_complete_issue",
                    "cited_value": "Journal, volume 23, issue 4",
                    "observed_value": None,
                    "basis": "complete issue inventory does not contain the cited work",
                }],
            }},
        }

        result = build_human_report_projection(projection).as_dict()
        concern = next(
            item["bibliographic_concern"] for item in result["sources"]
            if item["reference"]["id"] == "r1"
        )

        self.assertEqual(concern["level"], "reference_refuted")
        self.assertEqual(concern["basis"], "documented_refutation")
        self.assertEqual(concern["refutation_kind"], "absent_from_complete_issue")
        self.assertIsNone(concern["observed_work"])

    def test_elevated_issue_absence_keeps_incomplete_identity_provenance(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["resolve_map"]["r1"] = {
            "status": "unverified",
            "evidence_profile": {
                "bibliographic_adjudication": {
                    "outcome": "checks_incomplete",
                    "refutations": [{
                        "kind": "absent_from_complete_issue",
                        "source": "springer_complete_issue",
                        "cited_value": "Journal of Tests, volume 23, issue 4, page 261",
                        "observed_value": None,
                        "basis": "complete official issue inventory excludes the cited work",
                    }],
                },
                "bibliographic_suspicion": {
                    "suspicion_level": "elevated",
                    "conclusion": "complete_issue_absence_with_incomplete_identity_checks",
                    "providers": ["crossref", "openalex"],
                },
            },
        }

        result = build_human_report_projection(projection).as_dict()
        concern = next(
            item["bibliographic_concern"] for item in result["sources"]
            if item["reference"]["id"] == "r1"
        )

        self.assertEqual(concern["level"], "elevated_bibliographic_suspicion")
        self.assertEqual(
            concern["basis"],
            "complete_issue_absence_with_incomplete_identity_checks",
        )
        self.assertEqual(concern["refutation_kind"], "absent_from_complete_issue")
        self.assertEqual(concern["resolver"], "springer_complete_issue")
        self.assertEqual(
            concern["cited_coordinates"],
            "Journal of Tests, volume 23, issue 4, page 261",
        )
        self.assertEqual(result["overview"]["sources_suspected_fabricated"], 0)
        self.assertEqual(
            result["overview"]["sources_elevated_bibliographic_suspicion"], 1,
        )

    def test_assessment_is_red_for_a_contradiction_and_retracted_source(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        projection["verification_projection"][0].update({
            "semantic_outcome": "contradicts", "result_class": "negative",
        })
        projection["verification_projection"][1].update({
            "semantic_outcome": "supports", "result_class": "positive", "crediting": True,
        })
        projection["resolve_map"]["r2"]["retracted"] = True

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["assessment"], {
            "state": "red",
            "hard_findings": 2,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": [
                "hard.claim.uncontested_contradiction",
                "hard.source.retracted",
            ],
        })

    def test_retracted_resolved_source_is_counted_and_changes_overall_assessment(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"] = projection["parse_effective"]["citations"][:2]
        for row in projection["verification_projection"][:2]:
            row.update({
                "semantic_outcome": "supports",
                "result_class": "positive",
                "crediting": True,
            })
        projection["resolve_map"]["r2"].update({"status": "resolved", "retracted": True})

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["overview"]["sources_retracted"], 1)
        self.assertEqual(result["overview"]["assessment"], {
            "state": "yellow",
            "hard_findings": 1,
            "review_findings": 0,
            "minor_findings": 0,
            "technical_findings": 0,
            "reason_codes": ["hard.source.retracted"],
        })
        self.assertIn(
            "r2",
            [
                item["ref_id"]
                for item in result["attention"]
                if item["entity"] == "source"
            ],
        )

    def test_projection_rejects_citation_to_unknown_source(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["parse_effective"]["citations"][0]["ref_id"] = "missing"

        with self.assertRaisesRegex(ValueError, "unknown source"):
            build_human_report_projection(projection)

    def test_projection_preserves_and_validates_debug_execution_metadata(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["execution"] = {
            "debug_mode": True,
            "debug_labels": ["fork:frozen_fetch_verify"],
        }

        result = build_human_report_projection(projection).as_dict()

        self.assertEqual(result["execution"], projection["execution"])

        projection["execution"]["debug_mode"] = "true"
        with self.assertRaisesRegex(ValueError, "debug_mode must be boolean"):
            build_human_report_projection(projection)

        projection["execution"] = {"debug_mode": True, "debug_labels": [""]}
        with self.assertRaisesRegex(ValueError, "debug_labels must be nonempty text"):
            build_human_report_projection(projection)

    def test_projection_keeps_guarded_requests_and_pacing_events(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_raw"].update({
            "logical_requests": [{
                "logical_request_id": "request-1", "claim_id": "c2", "ref_id": "r2",
                "scope": "abstract_only", "candidate_cycle": 1, "stage": "support_gate",
                "created_at": "2026-01-01T00:00:00Z",
            }],
            "jury1_rejections": [{
                "event_id": "rejection-1", "logical_request_id": "request-1",
                "created_at": "2026-01-01T00:00:02Z", "cause": "schema_invalid",
            }],
            "dispatch_attempts": [{
                "dispatch_attempt_id": "attempt-1", "logical_request_id": "request-1",
                "created_at": "2026-01-01T00:00:01Z", "provider_id": "provider-a",
                "model_id": "model-a",
            }],
            "dispatch_events": [{
                "event_id": "pacing-1", "logical_request_id": "request-1",
                "dispatch_attempt_id": None, "event_type": "pacing_wait",
                "created_at": "2026-01-01T00:00:00Z", "payload": {},
            }],
        })

        result = build_human_report_projection(projection).as_dict()
        pair = next(item for item in result["pairs"] if item["claim_id"] == "c2")

        self.assertEqual([row["logical_request_id"] for row in pair["logical_requests"]], ["request-1"])
        self.assertEqual([row["event_id"] for row in pair["jury1_rejections"]], ["rejection-1"])
        self.assertEqual([row["event_id"] for row in pair["dispatch_events"]], ["pacing-1"])
        self.assertEqual(pair["verdict_provenance"], {
            "proposal": None,
            "jury2_reviews": [],
            "finalizer": {
                "status": "exhausted",
                "terminal_outcome": None,
                "terminal_cause": None,
                "winner_call_id": None,
            },
        })

    def test_projection_exposes_guard_rejected_jury1_outcome_as_unaccepted(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_raw"].update({
            "logical_requests": [{
                "logical_request_id": "request-guarded", "claim_id": "c2", "ref_id": "r2",
                "scope": "abstract_only", "candidate_cycle": 3,
                "stage": "explanation_evidence",
                "payload": {"determined_outcome": "supports"},
            }],
            "jury1_rejections": [{
                "event_id": "rejection-guarded", "logical_request_id": "request-guarded",
                "created_at": "2026-01-01T00:00:02Z",
                "cause": "grounding/decision_or_context_invalid",
                "state_cause": "grounding_invalid",
            }],
        })

        result = build_human_report_projection(projection).as_dict()
        pair = next(item for item in result["pairs"] if item["claim_id"] == "c2")

        self.assertEqual(pair["rejected_jury1_attempts"], [{
            "logical_request_id": "request-guarded",
            "candidate_cycle": 3,
            "stage": "explanation_evidence",
            "jury1_outcome": "supports",
            "cause": "grounding/decision_or_context_invalid",
            "state_cause": "grounding_invalid",
        }])

    def test_projection_attributes_only_the_winning_proposal_and_jury2_review(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_pair_states"][0].update({
            "terminal_outcome": "supports",
            "terminal_cause": "jury2_accepted",
            "winner_call_id": "candidate-b",
        })
        projection["verification_raw"].update({
            "candidates": [{
                "candidate_id": "candidate-b", "claim_id": "c1", "ref_id": "r1",
                "origin_logical_request_id": "proposal-b", "candidate_cycle": 2,
                "outcome": "supports", "explanation": "The source supports the claim.",
                "outcome_fields": {"supported_part": "The central proposition."},
                "evidence": ["Supporting passage."],
            }],
            "candidate_events": [{
                "event_id": "jury2-c-yes", "candidate_id": "candidate-b",
                "event_type": "jury2_yes", "created_at": "2026-01-01T00:00:05Z",
                "payload": {"logical_request_id": "jury2-c"},
            }],
            "logical_requests": [
                {"logical_request_id": "gate-a", "claim_id": "c1", "ref_id": "r1", "stage": "support_gate", "candidate_cycle": 1},
                {"logical_request_id": "proposal-b", "claim_id": "c1", "ref_id": "r1", "stage": "explanation_evidence", "candidate_cycle": 2},
                {"logical_request_id": "jury2-c", "claim_id": "c1", "ref_id": "r1", "stage": "jury2", "candidate_id": "candidate-b", "candidate_cycle": 2},
            ],
            "dispatch_attempts": [
                {"dispatch_attempt_id": "attempt-a", "logical_request_id": "gate-a", "provider_id": "provider-a", "model_id": "model-a"},
                {"dispatch_attempt_id": "attempt-b", "logical_request_id": "proposal-b", "provider_id": "provider-b", "model_id": "model-b"},
                {"dispatch_attempt_id": "attempt-c", "logical_request_id": "jury2-c", "provider_id": "provider-c", "model_id": "model-c"},
            ],
            "dispatch_events": [
                {"event_id": "completed-a", "logical_request_id": "gate-a", "dispatch_attempt_id": "attempt-a", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
                {"event_id": "completed-b", "logical_request_id": "proposal-b", "dispatch_attempt_id": "attempt-b", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
                {"event_id": "completed-c", "logical_request_id": "jury2-c", "dispatch_attempt_id": "attempt-c", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
            ],
        })

        result = build_human_report_projection(projection).as_dict()
        pair = next(item for item in result["pairs"] if item["claim_id"] == "c1")

        self.assertEqual(pair["verdict_provenance"], {
            "proposal": {"logical_request_id": "proposal-b", "provider_id": "provider-b", "model_id": "model-b"},
            "jury2_reviews": [{"event_type": "jury2_yes", "logical_request_id": "jury2-c", "provider_id": "provider-c", "model_id": "model-c"}],
            "finalizer": {
                "status": "accepted",
                "terminal_outcome": "supports",
                "terminal_cause": "jury2_accepted",
                "winner_call_id": "candidate-b",
            },
        })
        self.assertEqual(pair["decision"], {
            "candidate_cycle": 2,
            "outcome": "supports",
            "explanation": "The source supports the claim.",
            "supported_content": "The central proposition.",
            "incompatible_proposition": None,
            "reason": None,
            "provider_confidence": None,
            "evidence": ["Supporting passage."],
        })
        self.assertNotIn("model-a", pair["verdict_provenance"].__repr__())

    def test_projection_links_rejected_jury2_cycle_to_its_jury1_verdict(self):
        from core.report.human.projection import build_human_report_projection

        projection = self._run_projection()
        projection["verification_pair_states"][0].update({
            "terminal_outcome": "supports",
            "terminal_cause": "jury2_accepted",
            "winner_call_id": "candidate-b",
        })
        projection["verification_raw"].update({
            "candidates": [
                {
                    "candidate_id": "candidate-a", "claim_id": "c1", "ref_id": "r1",
                    "origin_logical_request_id": "proposal-a", "candidate_cycle": 1,
                    "outcome": "partial", "explanation": "Only part was supported.",
                    "outcome_fields": {}, "evidence": ["First passage."],
                },
                {
                    "candidate_id": "candidate-b", "claim_id": "c1", "ref_id": "r1",
                    "origin_logical_request_id": "proposal-b", "candidate_cycle": 2,
                    "outcome": "supports", "explanation": "The source supports the claim.",
                    "outcome_fields": {}, "evidence": ["Second passage."],
                },
            ],
            "candidate_events": [
                {
                    "event_id": "jury2-a-no", "candidate_id": "candidate-a",
                    "event_type": "jury2_no", "created_at": "2026-01-01T00:00:03Z",
                    "payload": {
                        "logical_request_id": "jury2-a",
                        "reason": "The selected passage did not establish the comparison.",
                    },
                },
                {
                    "event_id": "jury2-b-yes", "candidate_id": "candidate-b",
                    "event_type": "jury2_yes", "created_at": "2026-01-01T00:00:06Z",
                    "payload": {"logical_request_id": "jury2-b"},
                },
            ],
            "logical_requests": [
                {"logical_request_id": "proposal-a", "claim_id": "c1", "ref_id": "r1", "stage": "explanation_evidence", "candidate_cycle": 1},
                {"logical_request_id": "jury2-a", "claim_id": "c1", "ref_id": "r1", "stage": "jury2", "candidate_id": "candidate-a", "candidate_cycle": 1},
                {"logical_request_id": "proposal-b", "claim_id": "c1", "ref_id": "r1", "stage": "explanation_evidence", "candidate_cycle": 2},
                {"logical_request_id": "jury2-b", "claim_id": "c1", "ref_id": "r1", "stage": "jury2", "candidate_id": "candidate-b", "candidate_cycle": 2},
            ],
            "dispatch_attempts": [
                {"dispatch_attempt_id": "attempt-proposal-a", "logical_request_id": "proposal-a", "provider_id": "provider-a", "model_id": "jury1-a"},
                {"dispatch_attempt_id": "attempt-jury2-a", "logical_request_id": "jury2-a", "provider_id": "provider-b", "model_id": "jury2-a"},
                {"dispatch_attempt_id": "attempt-proposal-b", "logical_request_id": "proposal-b", "provider_id": "provider-c", "model_id": "jury1-b"},
                {"dispatch_attempt_id": "attempt-jury2-b", "logical_request_id": "jury2-b", "provider_id": "provider-d", "model_id": "jury2-b"},
            ],
            "dispatch_events": [
                {"event_id": "completed-proposal-a", "logical_request_id": "proposal-a", "dispatch_attempt_id": "attempt-proposal-a", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
                {"event_id": "completed-jury2-a", "logical_request_id": "jury2-a", "dispatch_attempt_id": "attempt-jury2-a", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
                {"event_id": "completed-proposal-b", "logical_request_id": "proposal-b", "dispatch_attempt_id": "attempt-proposal-b", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
                {"event_id": "completed-jury2-b", "logical_request_id": "jury2-b", "dispatch_attempt_id": "attempt-jury2-b", "event_type": "completed", "payload": {"technical_result": "answer_received"}},
            ],
        })

        result = build_human_report_projection(projection).as_dict()
        pair = next(item for item in result["pairs"] if item["claim_id"] == "c1")

        self.assertEqual(pair["rejected_jury2_attempts"], [{
            "candidate_id": "candidate-a",
            "candidate_cycle": 1,
            "jury1_outcome": "partial",
            "proposal": {
                "logical_request_id": "proposal-a",
                "provider_id": "provider-a",
                "model_id": "jury1-a",
            },
            "jury2_review": {
                "event_type": "jury2_no",
                "logical_request_id": "jury2-a",
                "provider_id": "provider-b",
                "model_id": "jury2-a",
                "reason": "The selected passage did not establish the comparison.",
            },
        }])

    def test_projection_loads_the_real_run_repository_shape(self):
        from core.report.human.projection import build_human_report_projection
        from core.report.io import load_run_projection
        from tests.test_run_report import TestVerifyRunGate

        with tempfile.TemporaryDirectory() as temporary:
            run = TestVerifyRunGate()._build_claim_evidence_run(temporary, [{
                "claim_id": "c1",
                "ref_id": "r1",
                "source_text": "The sky appears blue because of Rayleigh scattering.",
                "outcome": "supports",
                "pair_status": "accepted",
                "terminal_outcome": "supports",
                "terminal_cause": "jury2_accepted",
                "terminal_resolution": "jury2_accepted",
                "terminal_assurance": "passed",
                "evidence": ["blue because of Rayleigh scattering"],
                "grounded": [{
                    "span_id": "span-1",
                    "text": "blue because of Rayleigh scattering",
                }],
            }])

            loaded = load_run_projection(run)
            result = build_human_report_projection(loaded).as_dict()

        self.assertEqual(result["overview"]["pairs_total"], 1)
        self.assertIn("manuscript_text", loaded)
        self.assertIn("verify_policy", loaded)
        self.assertEqual(result["pairs"][0]["verification"]["semantic_outcome"], "supports")
        self.assertEqual(result["pairs"][0]["candidates"][0]["candidate_id"], "candidate-1")
        self.assertEqual(result["pairs"][0]["logical_requests"][0]["stage"], "explanation_evidence")
        self.assertEqual(result["pairs"][0]["dispatch_attempts"][0]["model_id"], "model-a")
        self.assertEqual(result["models"]["totals"]["attempts"], 1)

    def test_real_repository_remediation_projection_and_render_are_redacted(self):
        from core.infra.db import RunRepository
        from core.report.human.projection import build_human_report_projection
        from core.report.human.render import render_human_report
        from core.report.io import load_run_projection

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = os.path.join(temporary, "child")
            repo = RunRepository.create(
                run_dir, run_id="child", input_path="/private/child-manuscript.pdf",
                input_sha256="a" * 64, accuracy="standard", style="vancouver",
                model_id=None, http_profile="plain", challenge_mode="off",
                fixture_fingerprint="fixture", parent_run_id="parent-run",
                run_origin="remediated_from_completed",
            )
            repo.replace_parse_payload(
                manuscript_text="Claim [1]. Orphan [99].",
                claims=[{"id": "c1", "sentence": "Claim [1].", "marker_raw": "[1]"}],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Reference one."}],
                citations=[
                    {"claim_id": "c1", "ref_id": "r1", "ref_number": 1},
                    {"claim_id": "c1", "ref_id": None, "ref_number": 99, "marker_raw": "[99]"},
                ],
            )
            repo.create_manual_parse_review(
                task_id="identity-review", review_kind="reference_identity_review",
                target_id="r1", instructions="Correct identity.",
            )
            target_sha = repo.get_task("identity-review").task_payload["target_sha256"]
            repo.submit_task_answer(
                task_id="identity-review", actor_type="user", answer_id="answer-1",
                raw_payload={
                    "action": "correct_identity", "target_sha256": target_sha,
                    "reason": "SENTINEL_ANSWER /private/answer.txt", "title": "Canonical title",
                    "doi": "10.1234/example",
                },
            )
            repo.append_task_answer_provenance({
                "answer_id": "answer-1", "producer_class": "operator",
                "producer_identity": "local-unattested", "authenticated_uid": 0,
                "ingress_kind": "controlled_metadata", "admitted_at": "2026-01-01T00:00:00Z",
                "file_count": 0, "authority_id": "local-unattested",
            }, [])
            repo.apply_manual_parse_review("identity-review")
            repo.update_run_phase("done")
            repo.update_run_status("completed")
            repo.write_completed_remediation_provenance({
                "parent_run_id": "parent-run", "parent_input_sha256": "b" * 64,
                "parent_report_sha256": "c" * 64, "parent_journal_sha256": "d" * 64,
                "source_inventory_sha256": "e" * 64, "created_at": "2026-01-01T00:00:00Z",
            })
            repo.close()

            loaded = load_run_projection(run_dir)
            projected = build_human_report_projection(loaded).as_dict()
            rendered = render_human_report(build_human_report_projection(loaded))
            remediation = projected["remediation"]
            intervention = remediation["interventions"][0]
            lineage = remediation["child_run"]
            body = rendered.body

        self.assertEqual(intervention["kind"], "manual_parse_override")
        self.assertEqual(intervention["effect"], "override")
        self.assertEqual(intervention["action"], "correct_identity")
        self.assertEqual(intervention["admission_classification"], "local_operator_unattested")
        self.assertEqual(lineage["run_origin"], "remediated_from_completed")
        self.assertEqual(lineage["parent_run_id"], "parent-run")
        self.assertEqual(lineage["provenance"]["parent_input_sha256"], "b" * 64)
        self.assertEqual(lineage["provenance"]["parent_report_sha256"], "c" * 64)
        self.assertEqual(lineage["provenance"]["parent_journal_sha256"], "d" * 64)
        self.assertEqual(lineage["provenance"]["source_inventory_sha256"], "e" * 64)
        overview = projected["overview"]
        self.assertIn("hard.claim.orphan_citation", overview["assessment"]["reason_codes"])
        orphan = next(item for item in remediation["guidance"] if item["code"] == "hard.claim.orphan_citation")
        self.assertTrue(orphan["preserve_automatic_finding"])
        serialized = json.dumps(projected, sort_keys=True)
        for sentinel in ("SENTINEL_ANSWER", "/private/answer.txt"):
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(sentinel, body)
        self.assertNotIn("/private/child-manuscript.pdf", serialized)
        self.assertNotIn("/private/child-manuscript.pdf", body)
        self.assertNotIn("local-unattested", serialized)
        self.assertNotIn("local-unattested", body)
        self.assertIn("local_operator_unattested", serialized)

    def test_projection_omits_all_local_path_values_without_mutating_run_data(self):
        from core.report.human import render_human_report
        from core.report.human.privacy import sanitize_local_paths
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        manuscript = source["parse_effective"]["manuscript"]
        manuscript["input_path"] = "/private/input.pdf"
        source["manifest"]["entries"][0]["stored_as"] = "sources/r1.txt"
        source["manifest"]["entries"][1]["source_ref"] = r"C:\private\source.txt"
        source["unreadable"] = {"entries": [{"kept_as": r"\\host\share\raw.pdf"}]}
        source["resolve_map"]["r1"]["diagnostic"] = "resolver saw `/private/resolve.json`"
        source["resolve_map"]["r1"]["evidence_profile"] = {
            "resolver_coverage": {"observations": [
                {"query_contract": "GET /journals/{issn}"},
                {"query_contract": "GET /private/resolver-state"},
            ]},
        }
        source["fetch_attempts_by_ref"]["r1"][0]["debug"] = "file:///private/fetch.txt"
        source["verify_runtime"]["debug"] = "C:/private/config.json"
        source["verify_runtime"]["mixed"] = (
            "https://example.test/public /private/secret.txt"
        )
        source["execution"]["debug_labels"] = ["debug /private/label.txt"]
        source["manual_parse_adjudication"] = [{
            "subject_type": "reference", "subject_id": "r1", "status": "applied",
            "original_title": "/private/manual.txt",
        }]
        source["legacy"] = {"diagnostic": "legacy /private/record.json"}

        projected = build_human_report_projection(source).as_dict()
        serialized = json.dumps(projected, sort_keys=True)
        body = render_human_report(
            build_human_report_projection(source)
        ).body

        for path in (
            "/private/input.pdf", r"C:\private\source.txt", r"\\host\share\raw.pdf",
            "/private/resolve.json", "file:///private/fetch.txt", "C:/private/config.json",
            "/private/label.txt", "/private/manual.txt", "/private/record.json",
            "/private/secret.txt",
        ):
            self.assertNotIn(path, serialized)
            self.assertNotIn(path, body)
        self.assertNotIn("input_path", projected["manuscript"])
        self.assertIn("sources/r1.txt", serialized)
        self.assertIn("Crossref journal lookup by ISSN", serialized)
        self.assertNotIn("GET /journals/{issn}", serialized)
        self.assertNotIn("GET /private/resolver-state", serialized)
        self.assertEqual(sanitize_local_paths(projected), projected)
        self.assertEqual(manuscript["input_path"], "/private/input.pdf")
        self.assertEqual(source["resolve_map"]["r1"]["diagnostic"], "resolver saw `/private/resolve.json`")

    def test_projection_omits_backticked_posix_paths_without_redacting_safe_text(self):
        from core.report.human.privacy import sanitize_local_paths

        self.assertEqual(
            sanitize_local_paths("Missing file `/private/input.pdf`"),
            "[local path omitted]",
        )
        for value in (
            "https://example.test/public/path",
            "sources/r1.txt",
            "privacy and/or trade",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_local_paths(value), value)

    def test_projection_preserves_public_urls_relative_paths_hashes_and_commands(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        safe_values = {
            "url": "https://example.test/a/path?source=public",
            "url_in_text": "See https://example.test/a/path and continue",
            "relative": "sources/r1.txt",
            "hash": "a" * 64,
            "doi": "10.1000/example",
            "command": "python run.py --run {RUN_DIR}",
            "text": "and/or <section>public</section>",
            "profile": "profile: standard",
        }
        source["verify_runtime"] = safe_values

        projected = build_human_report_projection(source).as_dict()

        self.assertEqual(projected["configuration"]["verify_runtime"], safe_values)

    def test_projection_rejects_local_path_object_keys(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        source["verify_runtime"] = {"/private/key": "value"}

        with self.assertRaisesRegex(ValueError, "local path object key"):
            build_human_report_projection(source)

    def test_projection_validates_credential_metrics_fail_closed(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        source["credential_metrics"] = {
            "recorded": False,
            "rows": [],
            "totals": {
                **{field: 0 for field in _CREDENTIAL_COUNT_FIELDS},
                "keys_present_at_start": 0,
                "keys_used": 0,
            },
            "secret": "sentinel",
        }
        with self.assertRaisesRegex(ValueError, "invalid credential metrics"):
            build_human_report_projection(source)

        metrics = _credential_metrics()
        metrics["rows"][0]["env_name"] = "actual-secret-sentinel"
        source["credential_metrics"] = metrics
        with self.assertRaisesRegex(ValueError, "invalid credential metric row"):
            build_human_report_projection(source)

    def test_projection_aggregates_and_validates_credentials(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        source["credential_metrics"] = _credential_metrics(core_counts={
            "calls": 3,
            "successful_http_responses": 1,
            "http_429": 1,
            "network_errors": 1,
        })
        credentials = build_human_report_projection(source).as_dict()[
            "configuration"
        ]["credentials"]
        self.assertEqual(credentials["totals"]["calls"], 3)
        core = _core_row(credentials)
        self.assertEqual(core["env_name"], "CORE_API_KEY")
        self.assertEqual(
            core["by_channel"]["resolve"]["http_429"],
            1,
        )

        _core_row(source["credential_metrics"])["calls"] = 2
        with self.assertRaisesRegex(ValueError, "invalid credential metric row"):
            build_human_report_projection(source)

    def test_projection_rejects_inconsistent_credential_channel_metrics(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        source["credential_metrics"] = _credential_metrics(core_counts={
            "calls": 1,
            "successful_http_responses": 1,
        })
        core = _core_row(source["credential_metrics"])
        core["by_channel"]["fetch"] = {
            **core["by_channel"]["fetch"],
            "calls": 1,
            "successful_http_responses": 1,
        }

        with self.assertRaisesRegex(
            ValueError, "credential channel metrics do not match row"
        ):
            build_human_report_projection(source)

    def test_projection_requires_the_derived_credential_warning(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        source["credential_metrics"] = _credential_metrics(core_counts={
            "calls": 2,
            "http_401": 1,
            "http_403": 1,
        })
        credentials = build_human_report_projection(source).as_dict()[
            "configuration"
        ]["credentials"]
        self.assertEqual(
            _core_row(credentials)["warning_code"],
            "credential_rejected_or_not_entitled",
        )

        _core_row(source["credential_metrics"])["warning_code"] = None
        with self.assertRaisesRegex(ValueError, "invalid credential metric row"):
            build_human_report_projection(source)

    def test_projection_distinguishes_initial_presence_from_later_use(self):
        from core.report.human.projection import build_human_report_projection

        source = self._run_projection()
        metrics = _credential_metrics(core_counts={
            "calls": 1,
            "successful_http_responses": 1,
        })
        _core_row(metrics)["present_at_start"] = False
        metrics["totals"]["keys_present_at_start"] = 0
        source["credential_metrics"] = metrics

        credentials = build_human_report_projection(source).as_dict()[
            "configuration"
        ]["credentials"]
        self.assertFalse(_core_row(credentials)["present_at_start"])
        self.assertTrue(_core_row(credentials)["used"])
