# tests/test_resolve_published_preprint_unit.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Published citation coordinates keep a preprint from becoming the cited unit."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import unittest
from unittest import mock

from tests._bootstrap import *  # noqa: F401,F403
from core.parse.citation_coordinates import extract_cited_coordinates
from core.resolve import decision, service
from core.resolve.providers import crossref_metadata


RAW = (
    "O. J. C. Dias, J. E. Santos, and B. Way. Localised AdS5 x S5 black holes. "
    "Phys. Rev. Lett. 117 (2016) 151101 [arXiv:1605.04911]."
)
TITLE = "Localised AdS5 x S5 black holes"


def _published_ref(**changes):
    ref = {
        "raw_entry": RAW,
        "title": TITLE,
        "year": 2016,
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "cited_coordinates": extract_cited_coordinates(RAW, TITLE),
        "doi": None,
    }
    ref.update(changes)
    return ref


def _arxiv_candidate(**changes):
    candidate = {
        "status": "resolved",
        "via": "arxiv_search",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "doi": "10.48550/arXiv.1605.04911",
        "resolved_identifier": {
            "type": "doi",
            "value": "10.48550/arXiv.1605.04911",
        },
        "matched_title": TITLE,
        "abstract": "Preprint abstract.",
        "fulltext_links": [
            {
                "url": "https://arxiv.org/pdf/1605.04911.pdf",
                "content_type": "pdf",
                "content_version": "preprint",
            }
        ],
        "metadata_match": {
            "title_overlap": 1.0,
            "score": 0.95,
            "author_match": True,
            "year_match": True,
        },
    }
    candidate.update(changes)
    return candidate


def _synthetic_published_ref():
    raw = (
        "A. Example. A synthetic publication-unit test. "
        "Journal of Synthetic Studies 17, 101–108 (2024)."
    )
    title = "A synthetic publication-unit test"
    return {
        "raw_entry": raw,
        "title": title,
        "year": 2024,
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "cited_coordinates": extract_cited_coordinates(raw, title),
        "doi": None,
    }


def _europepmc_preprint_attempt(doi, **changes):
    attempt = {
        "via": "europepmc",
        "status": "resolved",
        "enrichment_only": True,
        "work_type": "preprint",
        "doi": doi,
        "identifiers": {"doi": doi},
    }
    attempt.update(changes)
    return attempt


def _crossref_preprint_payload(doi, *, returned_doi=None, work_type="posted-content", subtype="preprint"):
    return json.dumps({
        "message": {
            "DOI": returned_doi or doi,
            "type": work_type,
            "subtype": subtype,
            "title": ["A synthetic publication-unit test"],
            "author": [{"family": "Example"}],
            "issued": {"date-parts": [[2024]]},
            "container-title": ["Journal of Synthetic Studies"],
            "volume": "17",
            "page": "101-108",
        },
    })


def _crossref_native_work_proof(
    doi, *, returned_doi=None, http_status=200, work_type="posted-content",
    subtype="preprint",
):
    body = _crossref_preprint_payload(
        doi, returned_doi=returned_doi, work_type=work_type, subtype=subtype,
    )
    return {
        "version": 1,
        "requested_doi": doi,
        "request_url": f"https://api.crossref.org/works/{urllib.parse.quote(doi)}",
        "http_status": http_status,
        "response_body": body,
        "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


class TestPublishedPreprintUnit(unittest.TestCase):
    def test_native_crossref_preprint_proof_requires_an_exact_successful_direct_response(self):
        doi = "10.9999/synthetic-crossref-preprint-31415"
        ref = _synthetic_published_ref()
        body = _crossref_preprint_payload(doi)

        with (
            mock.patch.object(service, "_get", return_value=(200, body)) as get,
            mock.patch.object(service._rw, "is_retracted", return_value=False),
        ):
            resolved = crossref_metadata.resolve_doi(doi, ref=ref)

        proof = resolved.get("_crossref_native_work_proof")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(get.call_args.args[0], f"https://api.crossref.org/works/{doi}")
        self.assertEqual(proof, _crossref_native_work_proof(doi))

        for status, returned_doi, work_type, subtype in (
            (503, doi, "posted-content", "preprint"),
            (200, "10.9999/synthetic-other-record-31416", "posted-content", "preprint"),
            (200, doi, "posted-content", None),
            (200, doi, "journal-article", "preprint"),
        ):
            with self.subTest(
                status=status, returned_doi=returned_doi,
                work_type=work_type, subtype=subtype,
            ):
                bad_body = _crossref_preprint_payload(
                    doi, returned_doi=returned_doi,
                    work_type=work_type,
                    subtype=subtype,
                )
                with (
                    mock.patch.object(service, "_get", return_value=(status, bad_body)),
                    mock.patch.object(service._rw, "is_retracted", return_value=False),
                ):
                    bad = crossref_metadata.resolve_doi(doi, ref=ref)
                self.assertNotIn("_crossref_native_work_proof", bad)

    def test_native_crossref_preprint_proof_blocks_only_its_selected_doi(self):
        ref = _synthetic_published_ref()
        doi = "10.9999/synthetic-crossref-preprint-31415"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "work_type": "posted-content",
            "matched_title": ref["title"],
            "metadata_match": {
                "title_overlap": 1.0, "score": 0.95,
                "author_match": True, "year_match": True,
                "matched_year": ref["year"],
            },
        }
        body = _crossref_preprint_payload(doi)
        with (
            mock.patch.object(service, "_get", return_value=(200, body)),
            mock.patch.object(service._rw, "is_retracted", return_value=False),
        ):
            promoted = decision._promote_validated_identifier(ref, candidate, [])

        self.assertEqual(promoted["status"], "unverified")
        self.assertIsNone(promoted.get("resolved_identifier"))

        proof = _crossref_native_work_proof(doi)
        for bad_proof in (
            _crossref_native_work_proof(doi, subtype=None),
            _crossref_native_work_proof(
                doi, returned_doi="10.9999/synthetic-other-record-31416",
            ),
            _crossref_native_work_proof(doi, http_status=503),
            {**proof, "response_body": proof["response_body"] + " "},
        ):
            attempt = {
                "via": "identifier_validation:crossref", "status": "resolved",
                "identifier_type": "doi", "identifier_value": doi,
                "_crossref_native_work_proof": bad_proof,
            }
            self.assertFalse(
                decision._is_published_preprint_candidate(ref, candidate, [attempt])
            )

        mismatch = {
            "via": "identifier_validation:crossref", "status": "resolved",
            "identifier_type": "doi", "identifier_value": "10.9999/synthetic-other-record-31416",
            "_crossref_native_work_proof": proof,
        }
        self.assertFalse(decision._is_published_preprint_candidate(ref, candidate, [mismatch]))
        publisher_file = {
            **candidate,
            "work_type": "journal-article",
            "fulltext_links": [{"url": "https://example.invalid/preprint.pdf", "content_version": "preprint"}],
        }
        self.assertFalse(
            decision._is_published_preprint_candidate(
                ref, publisher_file,
                [{"via": "identifier_validation:crossref", "status": "resolved",
                  "identifier_type": "doi", "identifier_value": doi,
                  "_crossref_native_work_proof": _crossref_native_work_proof(
                      doi, work_type="journal-article", subtype=None,
                  )}],
            )
        )
        self.assertIs(
            decision._prefer_metadata_candidate(ref, None, publisher_file),
            publisher_file,
        )
        unproven_source = {
            **ref,
            "raw_entry": "A. Example. A synthetic publication-unit test. (2024).",
            "cited_coordinates": [],
        }
        self.assertFalse(
            decision._is_published_preprint_candidate(
                unproven_source,
                candidate,
                [{
                    "via": "identifier_validation:crossref", "status": "resolved",
                    "identifier_type": "doi", "identifier_value": doi,
                    "_crossref_native_work_proof": proof,
                }],
            )
        )

    def test_finalize_rechecks_persisted_native_crossref_preprint_proof(self):
        ref = _synthetic_published_ref()
        ref["id"] = "synthetic-crossref-preprint-finalize-ref"
        doi = "10.9999/synthetic-crossref-preprint-31415"
        proof = _crossref_native_work_proof(doi)
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi, "validated_via": "crossref"},
            "work_type": "posted-content",
            "matched_title": ref["title"],
            "metadata_match": {
                "title_overlap": 1.0, "score": 0.95,
                "author_match": True, "year_match": True,
                "matched_year": ref["year"],
            },
        }
        attempts = [{
            "via": "identifier_validation:crossref", "status": "resolved",
            "identifier_type": "doi", "identifier_value": doi,
            "_crossref_native_work_proof": proof,
        }]

        with (
            mock.patch.object(service, "_enrich_resolved_metadata", side_effect=lambda _ref, result, _attempts, **_kw: dict(result)),
            mock.patch.object(service, "_pubmed_coordinate_correction_search", return_value=None),
            mock.patch.object(service, "_pubmed_coordinate_occupancy_search", return_value=None),
            mock.patch.object(service.resolver_modules, "article_lookups", return_value=[]),
            mock.patch.object(service.resolver_modules, "occupy_coordinates", return_value=[]),
            mock.patch.object(service, "_issue_attestation_checks", return_value=[]),
        ):
            finalized = service._finalize_resolution(ref, candidate, attempts)

        self.assertEqual(finalized["status"], "unverified")
        self.assertNotIn("resolved_identifier", finalized)
        self.assertEqual(finalized["identity"]["scheme"], "none")
        self.assertEqual(attempts[0]["_crossref_native_work_proof"], proof)

    def test_same_doi_native_europepmc_preprint_blocks_synthetic_published_unit(self):
        ref = _synthetic_published_ref()
        doi = "10.7777/synthetic-native-record-31415"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi},
            "work_type": "posted-content",
            "matched_title": ref["title"],
        }
        attempts = [
            {
                "via": "identifier_validation:crossref",
                "status": "resolved",
                "identifier_type": "doi",
                "identifier_value": doi,
                "work_type": "posted-content",
            },
            _europepmc_preprint_attempt(doi),
        ]

        self.assertTrue(decision._has_published_journal_unit(ref))
        self.assertTrue(decision._is_published_preprint_candidate(ref, candidate, attempts))
        withheld = decision._promote_validated_identifier(ref, candidate, attempts)

        self.assertEqual(withheld["status"], "unverified")
        self.assertIsNone(withheld.get("resolved_identifier"))
        self.assertNotIn("doi", withheld.get("identifiers") or {})

    def test_literal_initialized_title_and_short_locator_withhold_native_preprint(self):
        title = "A cascade forecasting model for seasonal weather"
        raw = (
            f"Rao, P. et al. {title}. "
            "Journal of Synthetic Studies 6, 190 (2023)."
        )
        ref = _published_ref(
            raw_entry=raw, title=title, year=2023,
            source_type="unknown", source_kind="unknown",
            source_type_confidence="low",
            cited_coordinates=extract_cited_coordinates(raw, title),
        )
        doi = "10.7777/synthetic-native-short-locator"
        candidate = {
            "status": "resolved", "via": "europepmc",
            "resolution_basis": "metadata_search", "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi},
            "matched_title": title, "work_type": "preprint",
        }
        attempts = [
            {"via": "identifier_validation:crossref", "status": "resolved",
             "identifier_type": "doi", "identifier_value": doi,
             "work_type": "posted-content"},
            _europepmc_preprint_attempt(doi),
        ]
        self.assertTrue(decision._has_published_journal_unit(ref))
        self.assertTrue(decision._is_published_preprint_candidate(ref, candidate, attempts))
        withheld = decision._promote_validated_identifier(ref, candidate, attempts)
        self.assertEqual(withheld["status"], "unverified")
        self.assertIsNone(withheld.get("resolved_identifier"))

    def test_finalize_rechecks_native_preprint_evidence_added_by_enrichment(self):
        ref = _synthetic_published_ref()
        ref["id"] = "synthetic-preprint-enrichment-ref"
        doi = "10.7777/synthetic-enrichment-record-27182"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "resolved_identifier": {
                "type": "doi", "value": doi, "validated_via": "crossref",
            },
            "work_type": "posted-content",
            "matched_title": ref["title"],
            "metadata_match": {
                "title_overlap": 1.0,
                "score": 0.95,
                "author_match": True,
                "year_match": True,
                "matched_year": ref["year"],
            },
        }
        attempts = []

        def enrich_with_native_preprint(ref_arg, result_arg, attempts_arg, *, continuation=None):
            attempts_arg.append(_europepmc_preprint_attempt(doi))
            return dict(result_arg)

        with (
            mock.patch.object(
                service, "_enrich_resolved_metadata", side_effect=enrich_with_native_preprint,
            ),
            mock.patch.object(service, "_pubmed_coordinate_correction_search", return_value=None),
            mock.patch.object(service, "_pubmed_coordinate_occupancy_search", return_value=None),
            mock.patch.object(service.resolver_modules, "article_lookups", return_value=[]),
            mock.patch.object(service.resolver_modules, "occupy_coordinates", return_value=[]),
            mock.patch.object(service, "_issue_attestation_checks", return_value=[]),
        ):
            finalized = service._finalize_resolution(ref, candidate, attempts)

        self.assertEqual(finalized["status"], "unverified")
        self.assertNotIn("resolved_identifier", finalized)
        self.assertEqual(finalized["identity"]["scheme"], "none")

    def test_matching_validation_does_not_skip_native_preprint_recheck(self):
        ref = _synthetic_published_ref()
        doi = "10.8888/synthetic-resume-record-16180"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "resolved_identifier": {
                "type": "doi", "value": doi, "validated_via": "crossref",
            },
            "work_type": "posted-content",
        }
        attempts = [
            {
                "via": "identifier_validation:crossref",
                "status": "resolved",
                "identifier_type": "doi",
                "identifier_value": doi,
            },
            _europepmc_preprint_attempt(doi),
        ]

        promoted = service._promote_validated_identifier_once(ref, candidate, attempts)

        self.assertEqual(promoted["status"], "unverified")
        self.assertIsNone(promoted.get("resolved_identifier"))

    def test_native_preprint_binding_uses_identifier_not_enrichment_delivery(self):
        ref = _synthetic_published_ref()
        doi = "10.9999/synthetic-direct-record-57721"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi},
        }

        for enrichment_only in (False, None):
            with self.subTest(enrichment_only=enrichment_only):
                attempt = _europepmc_preprint_attempt(
                    f"https://doi.org/{doi}",
                    identifiers={"doi": doi},
                    enrichment_only=enrichment_only,
                )
                self.assertTrue(
                    decision._is_published_preprint_candidate(ref, candidate, [attempt])
                )

    def test_incomplete_conflicting_or_untyped_native_attempt_does_not_block(self):
        ref = _synthetic_published_ref()
        doi = "10.9999/synthetic-selected-record-57721"
        other_doi = "10.9999/synthetic-other-record-57722"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi},
            "work_type": "posted-content",
        }
        attempts = [
            _europepmc_preprint_attempt(doi, identifiers={}),
            _europepmc_preprint_attempt(other_doi),
            _europepmc_preprint_attempt(doi, status="incomplete"),
            _europepmc_preprint_attempt(doi, identifiers={"doi": other_doi}),
            _europepmc_preprint_attempt(doi, work_type="posted-content"),
            {
                "via": "europepmc",
                "status": "resolved",
                "is_preprint": True,
                "doi": doi,
                "identifiers": {"doi": doi},
            },
        ]

        for attempt in attempts:
            with self.subTest(attempt=attempt):
                self.assertFalse(
                    decision._is_published_preprint_candidate(ref, candidate, [attempt])
                )

    def test_non_arxiv_doi_prefix_does_not_create_preprint_evidence(self):
        ref = _synthetic_published_ref()
        doi = "10.1101/synthetic-published-journal-record.2024.0042"
        candidate = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": doi,
            "resolved_identifier": {"type": "doi", "value": doi},
            "work_type": "journal-article",
        }

        self.assertFalse(decision._is_published_preprint_candidate(ref, candidate, []))
        self.assertIs(decision._prefer_metadata_candidate(ref, None, candidate), candidate)

    def test_arxiv_search_result_cannot_win_published_journal_citation(self):
        ref = _published_ref()
        candidate = _arxiv_candidate()
        current = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "doi": "10.1234/published-work",
            "matched_title": TITLE,
            "metadata_match": {
                "title_overlap": 0.90,
                "score": 0.85,
                "author_match": True,
                "year_match": True,
            },
        }

        self.assertIs(decision._prefer_metadata_candidate(ref, None, candidate), None)
        self.assertIs(decision._prefer_metadata_candidate(ref, current, candidate), current)

    def test_preprint_identifier_is_withheld_even_when_already_resolved(self):
        candidate = _arxiv_candidate(resolution_basis="canonical_source")

        withheld = decision._promote_validated_identifier(_published_ref(), candidate, [])

        self.assertEqual(withheld["status"], "unverified")
        self.assertIsNone(withheld.get("resolved_identifier"))
        self.assertIsNone(withheld.get("identifiers"))
        self.assertIsNone(withheld.get("fulltext_links"))
        self.assertEqual(
            withheld.get("candidate_identifiers"),
            {"doi": "10.48550/arXiv.1605.04911"},
        )

    def test_metadata_work_type_alone_is_not_preprint_identity(self):
        posted = {
            "status": "resolved",
            "via": "crossref_metadata",
            "resolution_basis": "metadata_search",
            "work_type": "posted-content",
            "doi": "10.1234/publisher-record",
            "matched_title": TITLE,
            "metadata_match": {
                "title_overlap": 0.95,
                "score": 0.90,
                "author_match": True,
                "year_match": True,
            },
        }

        self.assertIs(decision._prefer_metadata_candidate(_published_ref(), None, posted), posted)

    def test_explicit_preprint_content_version_is_a_preprint_signal(self):
        candidate = _arxiv_candidate(
            via="openalex_search",
            doi=None,
            resolved_identifier=None,
            work_type="journal-article",
            content_version="submittedVersion",
        )

        self.assertIs(decision._prefer_metadata_candidate(_published_ref(), None, candidate), None)

    def test_accepted_manuscript_version_is_not_preprint_identity(self):
        candidate = _arxiv_candidate(
            via="openalex_search",
            doi=None,
            resolved_identifier=None,
            work_type="journal-article",
            content_version="acceptedManuscript",
        )

        self.assertIs(
            decision._prefer_metadata_candidate(_published_ref(), None, candidate),
            candidate,
        )

    def test_published_doi_identity_keeps_identity_but_filters_preprint_file(self):
        ref = _published_ref()
        candidate = _arxiv_candidate(
            via="openalex_search",
            doi="10.1234/published-work",
            resolved_identifier={"type": "doi", "value": "10.1234/published-work"},
            fulltext_exists=True,
        )

        self.assertIs(decision._prefer_metadata_candidate(ref, None, candidate), candidate)
        projected = service._without_published_preprint_links(ref, candidate)
        self.assertEqual(projected["doi"], "10.1234/published-work")
        self.assertEqual(projected["fulltext_links"], [])
        self.assertTrue(projected["fulltext_exists"])

    def test_nonparenthesized_high_article_like_coordinates_are_accepted(self):
        raw = "J. Test. 12, 2018: 45-52."
        coordinates = [
            {"kind": "container", "raw_value": "J. Test", "normalized_value": "J. Test", "spans": [{"raw_start": 0, "raw_end": 7}]},
            {"kind": "volume", "raw_value": "12", "normalized_value": "12", "spans": [{"raw_start": 9, "raw_end": 11}]},
            {"kind": "article_page_range", "raw_value": "45-52", "normalized_value": "45-52", "spans": [{"raw_start": 19, "raw_end": 24}]},
        ]
        ref = {
            "raw_entry": raw,
            "year": 2018,
            "source_kind": "article_like",
            "source_type_confidence": "high",
            "cited_coordinates": coordinates,
        }

        self.assertTrue(decision._has_published_journal_unit(ref))

    def test_real_standard_post_locator_year_published_unit_blocks_arxiv_alias(self):
        raw = (
            "Webb, T. M. A. et al. The star formation history of BCGs to z = 1.8 from "
            "the SpARCS/SWIRE Survey: evidence for significant in situ star formation "
            "at high redshift. Astrophys. J. 814, 96–107 (2015)."
        )
        title = (
            "The star formation history of BCGs to z = 1.8 from the SpARCS/SWIRE "
            "Survey: evidence for significant in situ star formation at high redshift"
        )
        ref = {
            "raw_entry": raw,
            "title": title,
            "year": 2015,
            "source_kind": "article_like",
            "source_type_confidence": "high",
            "cited_coordinates": extract_cited_coordinates(raw, title),
        }
        candidate = {
            "status": "resolved",
            "via": "openalex_search",
            "doi": "https://doi.org/10.48550/arxiv.1508.07302",
            "resolved_identifier": {
                "type": "doi",
                "value": "https://doi.org/10.48550/arxiv.1508.07302",
            },
        }

        self.assertTrue(decision._has_published_journal_unit(ref))
        self.assertTrue(decision._is_published_preprint_candidate(ref, candidate))
        self.assertIsNone(decision._prefer_metadata_candidate(ref, None, candidate))
        withheld = decision._promote_validated_identifier(ref, candidate, [])
        self.assertEqual(withheld["status"], "unverified")
        self.assertIsNone(withheld.get("resolved_identifier"))

    def test_post_locator_year_accepts_only_closed_tuple_and_known_repository_suffix(self):
        raw = (
            "Webb et al. A study. Astrophys. J. 814, 96–107 (2015) "
            "[arXiv:1508.07302] [INSPIRE]."
        )
        title = "A study"
        ref = {
            "raw_entry": raw,
            "title": title,
            "year": 2015,
            "source_kind": "article_like",
            "source_type_confidence": "high",
            "cited_coordinates": extract_cited_coordinates(raw, title),
        }
        malformed_raw = raw.replace("(2015)", "2015")
        malformed_year = {
            **ref,
            "raw_entry": malformed_raw,
            "cited_coordinates": extract_cited_coordinates(malformed_raw, title),
        }
        single_locator_raw = "Webb et al. A study. Astrophys. J. 814, 96 (2015)."
        single_locator = {
            **ref,
            "raw_entry": single_locator_raw,
            "cited_coordinates": extract_cited_coordinates(single_locator_raw, title),
        }
        unknown_trailing_prose = {
            **ref,
            "raw_entry": raw.replace("[INSPIRE].", "[INSPIRE] extra note."),
        }

        self.assertTrue(decision._has_published_journal_unit(ref))
        self.assertFalse(decision._has_published_journal_unit(malformed_year))
        self.assertFalse(decision._has_published_journal_unit(single_locator))
        self.assertFalse(decision._has_published_journal_unit(unknown_trailing_prose))

    def test_closed_catalogue_suffix_does_not_depend_on_a_named_service(self):
        raw = (
            "Walker et al. A study. Journal of Tests 24, 120–129 (2021) "
            "[arXiv:2101.01234] [BibliographicCatalog42]."
        )
        ref = {
            "raw_entry": raw,
            "title": "A study",
            "year": 2021,
            "source_kind": "article_like",
            "source_type_confidence": "high",
            "cited_coordinates": extract_cited_coordinates(raw, "A study"),
        }

        self.assertTrue(decision._has_published_journal_unit(ref))

    def test_inferred_repository_alias_is_not_projected_as_declared_journal_identity(self):
        from core.parse.parsing_common import _make_reference

        raw = (
            "Walker, J. et al. A robust measurement. Journal of Tests 24, "
            "120–129 (2021). [arXiv:2101.01234]"
        )
        inferred = _make_reference(1, raw)
        literal = _make_reference(2, raw + " doi:10.48550/arxiv.2101.01234")
        preprint_only = _make_reference(
            3, "Walker, J. A robust measurement. arXiv:2101.01234 (2021).",
        )

        for ref, expected_scheme in (
            (inferred, "none"), (literal, "doi"), (preprint_only, "doi"),
        ):
            with self.subTest(expected_scheme=expected_scheme):
                candidate = {
                    "status": "resolved", "via": "crossref",
                    "resolution_basis": "doi", "doi": ref["doi"],
                    "abstract": "Candidate abstract.",
                }
                guarded = decision._promote_validated_identifier(ref, candidate, [])
                projected = decision._build_resolution_decision(
                    ref, guarded, [], status=guarded["status"], via=guarded["via"],
                    reference_status_tag=None, fabrication_risk=None,
                    resolution_basis="doi", meta=guarded,
                    bibliographic_identity_status="not_identified",
                )
                self.assertEqual(projected.identity.scheme, expected_scheme)
                if expected_scheme == "none":
                    self.assertEqual(guarded["status"], "unverified")
                    self.assertTrue(guarded.get("candidate_identifiers", {}).get("doi"))
                    self.assertFalse(projected.evidence_payload.get("abstract"))

    def test_forged_or_partial_coordinates_do_not_block_preprint_only_citation(self):
        ref = _published_ref()
        forged = [dict(item) for item in ref["cited_coordinates"]]
        container_index = next(
            index for index, item in enumerate(forged) if item["kind"] == "container"
        )
        forged[container_index] = dict(
            forged[container_index], spans=[{"raw_start": 0, "raw_end": 3}],
        )
        partial = [item for item in ref["cited_coordinates"] if item["kind"] != "article_locator"]
        candidate = _arxiv_candidate()

        self.assertIs(
            decision._prefer_metadata_candidate(
                {**ref, "cited_coordinates": forged}, None, candidate,
            ),
            candidate,
        )
        self.assertIs(
            decision._prefer_metadata_candidate(
                {**ref, "cited_coordinates": partial}, None, candidate,
            ),
            candidate,
        )

    def test_literal_doi_and_preprint_only_reference_remain_eligible(self):
        candidate = _arxiv_candidate()
        literal = _published_ref(
            raw_entry=RAW + " doi:10.48550/arXiv.1605.04911",
            doi="https://doi.org/10.48550/arXiv.1605.04911",
        )
        preprint_only = {
            "raw_entry": "O. J. C. Dias et al. Localised AdS5 x S5 black holes [arXiv:1605.04911].",
            "title": TITLE,
            "year": 2016,
            "source_type": "article",
            "source_kind": "article_like",
            "source_type_confidence": "high",
            "cited_coordinates": [],
            "doi": None,
        }

        self.assertIs(decision._prefer_metadata_candidate(literal, None, candidate), candidate)
        self.assertIs(decision._prefer_metadata_candidate(preprint_only, None, candidate), candidate)

    def test_preprint_sibling_is_not_projected_into_published_result(self):
        ref = _published_ref()
        published = {
            "status": "resolved",
            "via": "crossref_metadata",
            "doi": "10.1234/published-work",
            "matched_title": TITLE,
            "metadata_match": {
                "title_overlap": 0.95,
                "author_match": True,
                "matched_first_author": "dias",
                "cited_first_author": "dias",
                "year_match": True,
                "matched_year": 2016,
            },
        }
        preprint_sibling = _arxiv_candidate(
            doi=None,
            resolved_identifier=None,
            metadata_match={
                "title_overlap": 1.0,
                "author_match": True,
                "matched_first_author": "dias",
                "cited_first_author": "dias",
                "year_match": True,
                "matched_year": 2016,
            },
        )

        result = service._harvest_co_identified_siblings(
            published, [preprint_sibling], ref=ref,
        )

        self.assertNotIn("Preprint abstract.", result.values())
        self.assertNotIn(preprint_sibling["fulltext_links"][0], result.get("fulltext_links", []))

    def test_arxiv_fallback_candidate_is_retained_as_attempt_but_not_returned(self):
        ref = _published_ref()
        attempts = []
        registry = {"openalex_search": mock.Mock(discover=mock.Mock())}
        with (
            mock.patch.object(service, "_resolver_registry", return_value=registry),
            mock.patch.object(service, "_load_fallback_order", return_value=["openalex_search"]),
            mock.patch.object(service, "_call_registry_resolver", return_value=_arxiv_candidate()),
        ):
            result = service._try_resolvers_with_fallback(
                "openalex_search", ref, result=None, attempts=attempts,
            )

        self.assertIsNone(result)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["via"], "arxiv_search")

    def test_preprint_file_is_filtered_from_published_result_projection(self):
        ref = _published_ref()
        result = {
            "status": "resolved",
            "via": "crossref_metadata",
            "doi": "10.1234/published-work",
            "abstract": "Published-record abstract.",
            "fulltext_links": [
                {"url": "https://example.org/article.pdf", "content_type": "pdf", "content_version": "published"},
                {"url": "https://arxiv.org/pdf/1605.04911.pdf", "content_type": "pdf", "content_version": "preprint"},
            ],
        }

        projected = service._without_published_preprint_links(ref, result)

        self.assertEqual(len(projected["fulltext_links"]), 1)
        self.assertEqual(projected["fulltext_links"][0]["url"], "https://example.org/article.pdf")
        self.assertEqual(projected["abstract"], "Published-record abstract.")

    def test_filtering_only_preprint_file_does_not_assert_fulltext_absent(self):
        ref = _published_ref()
        result = {
            "status": "resolved",
            "fulltext_exists": "unknown",
            "fulltext_links": [
                {
                    "url": "https://arxiv.org/pdf/1605.04911.pdf",
                    "content_type": "pdf",
                    "content_version": "preprint",
                }
            ],
        }

        projected = service._without_published_preprint_links(ref, result)

        self.assertEqual(projected["fulltext_links"], [])
        self.assertEqual(projected["fulltext_exists"], "unknown")


if __name__ == "__main__":
    unittest.main()
