# tests/test_bibliographic_adjudication.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from core.parse.parsing_common import _make_reference
from core.resolve.bibliographic_adjudication import adjudicate_bibliographic_evidence
from core.resolve.decision import _retraction_payload
from core.resolve import service as resolve
from core.resolve import resolver_coverage
from core.resolve.matching import _metadata_match_profile
from core.resolve.providers import europepmc


def _evidence(**overrides):
    value = {
        "minimum_checks_completed": True,
        "metadata_match": None,
        "identifier_fallback": None,
    }
    value.update(overrides)
    return value


def test_authoritative_identifier_absence_plus_completed_correction_refutes_reference():
    result = {
        "status": "not_found", "via": "doi.org",
        "reason": "DOI not found by DOI resolver (Handle responseCode 100)",
        "identifier_fallback": {
            "status": "not_found", "via": "crossref_metadata", "reason": "no candidate",
        },
    }
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/missing"}, status="not_found",
        evidence=_evidence(identifier_fallback=result["identifier_fallback"]), result=result,
    )
    assert verdict["outcome"] == "refuted"
    assert verdict["correction_status"] == "not_found"
    assert verdict["refutations"][0]["kind"] == "identifier_not_found"


def test_crossref_metadata_retraction_is_attributed_to_authority_attempt():
    payload = _retraction_payload(
        {"retracted": True, "via": "crossref_metadata"},
        [{"retracted": True, "via": "crossref_metadata"}],
    )
    assert payload["status"] == "retracted"
    assert payload["checked_via"] == "authority_metadata"
    assert payload["evidence"]["attempt_via"] == "crossref_metadata"


def test_identifier_error_with_same_work_correction_is_identified_with_errors():
    fallback = {
        "status": "resolved", "via": "crossref_metadata",
        "metadata_match": {"coordinate_comparisons": []},
    }
    result = {
        "status": "identifier_mismatch", "via": "crossref", "matched_title": "Other work",
        "reason": "DOI points to a different title", "identifier_fallback": fallback,
        "identifier_error_recovered": True,
    }
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/wrong"}, status="identifier_mismatch",
        evidence=_evidence(identifier_fallback=fallback), result=result,
    )
    assert verdict["outcome"] == "identified_with_errors"
    assert verdict["correction_status"] == "identified"
    assert verdict["refutations"][0]["kind"] == "identifier_targets_other_work"


def test_resolved_fallback_without_recovery_does_not_lock_identity():
    fallback = {
        "status": "resolved", "via": "crossref_metadata",
        "metadata_match": {
            "score": 0.6, "title_overlap": 1.0,
            "author_match": False, "year_match": True,
        },
    }
    result = {
        "status": "identifier_mismatch", "via": "crossref",
        "matched_title": "Other work", "reason": "DOI targets another work",
        "identifier_fallback": fallback,
    }

    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/wrong"}, status="identifier_mismatch",
        evidence=_evidence(identifier_fallback=fallback), result=result,
    )

    assert verdict["outcome"] == "refuted"
    assert verdict["identity_status"] == "not_identified"
    assert verdict["correction_status"] == "not_found"


def test_recovery_flag_cannot_override_fallback_ordinal_conflict():
    fallback = {
        "status": "resolved", "via": "crossref_metadata",
        "metadata_match": {
            "score": 0.0, "title_overlap": 0.938,
            "author_match": True, "year_match": True,
            "ordinal_conflict": True,
        },
    }
    result = {
        "status": "identifier_mismatch", "via": "crossref",
        "matched_title": "Other work", "reason": "DOI targets another work",
        "identifier_fallback": fallback, "identifier_error_recovered": True,
    }

    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/wrong"}, status="identifier_mismatch",
        evidence=_evidence(identifier_fallback=fallback), result=result,
    )

    assert verdict["outcome"] == "refuted"
    assert verdict["identity_status"] == "not_identified"
    assert verdict["correction_status"] == "not_found"


def test_unverified_correction_does_not_clear_identifier_error():
    fallback = {
        "status": "unverified", "via": "crossref_metadata",
        "metadata_match": {"score": 0.5},
    }
    result = {
        "status": "identifier_mismatch", "via": "crossref",
        "matched_title": "Other work", "reason": "DOI targets another work",
        "identifier_fallback": fallback,
    }
    evidence = _evidence(identifier_fallback=fallback)
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/wrong"}, status="identifier_mismatch",
        evidence=evidence, result=result,
    )
    tag, risk, _reason = resolve._reference_status_tag(
        {"doi": "10.1234/wrong"}, "identifier_mismatch",
        {**evidence, "bibliographic_adjudication": verdict,
         "synthetic_reference_risk": {"score": 0, "band": "none", "signals": []}},
        result,
    )
    assert verdict["correction_status"] == "not_found"
    assert tag == "identifier_error_weak_metadata"
    assert risk == "medium"


def test_recognized_journal_without_closed_world_inventory_is_only_not_corroborated():
    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified",
        evidence=_evidence(journal_authority={"status": "recognized"}),
        result={"status": "unverified", "via": "crossref_metadata"},
    )
    assert verdict == {
        "rule_version": "bibliographic-adjudication/v4",
        "outcome": "not_corroborated",
        "identity_status": "not_identified",
        "check_status": "complete",
        "correction_status": "not_attempted",
        "refutations": [],
    }


def test_complete_issue_absence_refutes_without_turning_search_failure_into_evidence():
    inventory = {
        "provider": "pmc_complete_issue",
        "status": "complete",
        "target_status": "absent",
        "target_member_order": None,
        "cited_container": "Journal Name",
        "cited_volume": "12",
        "cited_issue": "3",
        "completeness_basis": "all issue members enumerated",
        "members": [{"title": "The article occupying the cited pages"}],
    }

    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified",
        evidence=_evidence(
            minimum_checks_completed=False,
            issue_attestations=[inventory],
        ),
        result={
            "status": "unverified",
            "via": "crossref_metadata",
            "issue_attestations": [inventory],
        },
    )

    assert verdict["outcome"] == "refuted"
    assert verdict["check_status"] == "complete"
    assert verdict["identity_status"] == "not_identified"
    assert verdict["correction_status"] == "not_found"
    assert verdict["refutations"] == [{
        "kind": "absent_from_complete_issue",
        "field": "issue_inventory",
        "cited_value": "Journal Name, volume 12, issue 3",
        "observed_value": None,
        "source": "pmc_complete_issue",
        "basis": "all issue members enumerated",
    }]


def test_incomplete_issue_inventory_is_never_negative_evidence():
    inventory = {
        "provider": "pmc_complete_issue",
        "status": "incomplete",
        "target_status": "inconclusive",
        "members": [],
    }

    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified",
        evidence=_evidence(issue_attestations=[inventory]),
        result={"status": "unverified", "issue_attestations": [inventory]},
    )

    assert verdict["outcome"] == "not_corroborated"
    assert verdict["refutations"] == []


def test_complete_issue_contradiction_does_not_complete_failed_correction_protocol():
    inventory = {
        "provider": "pmc_complete_issue",
        "status": "complete",
        "target_status": "absent",
        "target_member_order": None,
        "cited_container": "Journal Name",
        "cited_volume": "12",
        "cited_issue": "3",
        "completeness_basis": "all issue members enumerated",
        "members": [],
    }

    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified",
        evidence=_evidence(issue_attestations=[inventory]),
        result={"status": "unverified", "issue_attestations": [inventory]},
        attempts=[{"status": "unresolved", "via": "crossref_metadata"}],
    )

    assert verdict["outcome"] == "checks_incomplete"
    assert verdict["check_status"] == "incomplete"
    assert verdict["refutations"][0]["kind"] == "absent_from_complete_issue"


def test_catalog_refresh_failure_preserves_complete_issue_suspicion(monkeypatch):
    from core.infra.db.repository import _validate_resolve_evidence_profile

    inventory = {
        "provider": "springer_complete_issue", "rule_version": "publisher-issue-attestation/v1",
        "status": "complete", "reason": "official issue enumerated", "scope": "issue",
        "target_status": "absent", "target_member_order": None,
        "cited_container": "Journal Name", "cited_volume": "12", "cited_issue": "3",
        "journal_title": "Journal Name", "completeness_basis": "official complete issue inventory",
        "members": [{
            "record_id": "springer:known", "title": "Known work", "first_author": "Known",
            "year": 2022, "journal": "Journal Name", "volume": "12", "issue": "3",
            "locator": "1", "pmid": None, "pmcid": None, "doi": None, "url": None,
        }],
        "sources": [{
            "role": "official_issue", "url": "https://example.invalid/issue",
            "response_sha256": "b" * 64,
        }],
        "observations": [],
    }
    attempts = [
        {"identity_search": {"resolver": "crossref", "query_contract": "q", "completion": "complete", "outcome": "no_compatible_identity"}},
        {"identity_search": {"resolver": "openalex", "query_contract": "q", "completion": "complete", "outcome": "no_compatible_identity"}},
        {"status": "unresolved", "via": "pubmed_citation_match"},
    ]
    ref = {
        "title": "Claimed work", "raw_entry": "Smith. Claimed work.", "year": 2022,
        "ay_surname": "Smith", "source_kind": "unknown",
        "cited_coordinates": [{"kind": "container", "normalized_value": "Journal Name"}],
    }
    result = {"status": "unverified", "issue_attestations": [inventory]}
    monkeypatch.setattr(resolver_coverage, "coverage_authority", lambda _ref: {
        "record_id": "fixture:journal", "snapshot_sha256": "a" * 64,
        "canonical_title": "Journal Name", "issns": ("1234-5678",),
    })
    monkeypatch.setattr(resolver_coverage, "evaluate", lambda _ref: (_ for _ in ()).throw(RuntimeError("catalog unavailable")))

    evidence = resolve._reference_evidence_profile(
        ref, result, "unverified", attempts, None,
        {"resolution_basis": "metadata_search", "existence_confidence": "low"},
    )

    adjudication = evidence["bibliographic_adjudication"]
    assert adjudication["outcome"] == "checks_incomplete"
    assert adjudication["refutations"] == [{
        "kind": "absent_from_complete_issue", "field": "issue_inventory",
        "cited_value": "Journal Name, volume 12, issue 3", "observed_value": None,
        "source": "springer_complete_issue", "basis": "official complete issue inventory",
    }]
    assert evidence["bibliographic_suspicion"] == {
        "suspicion_level": "elevated",
        "conclusion": "complete_issue_absence_with_incomplete_identity_checks",
        "providers": ["crossref", "openalex"],
    }
    assert _validate_resolve_evidence_profile(evidence)["bibliographic_suspicion"] == evidence["bibliographic_suspicion"]
    tag, risk, reason = resolve._reference_status_tag(ref, "unverified", evidence, result)
    assert (tag, risk) == ("unverified", "unknown")
    assert "official complete issue inventory excludes" in reason
    assert "identity checks remain incomplete" in reason


def test_issue_absence_refutation_uses_the_adapter_that_proved_absence():
    present = {
        "provider": "enumerating_provider",
        "status": "enumerated",
        "target_status": "present",
        "target_member_order": 0,
        "members": [{"title": "Recovered title"}],
    }
    absent = {
        "provider": "closed_issue_provider",
        "status": "complete",
        "target_status": "absent",
        "target_member_order": None,
        "cited_container": "Journal Name",
        "cited_volume": "12",
        "cited_issue": "3",
        "completeness_basis": "official complete issue",
        "members": [],
    }

    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified",
        evidence=_evidence(issue_attestations=[present, absent]),
        result={"status": "unverified", "issue_attestations": [present, absent]},
    )

    refutation = next(
        item for item in verdict["refutations"]
        if item["kind"] == "absent_from_complete_issue"
    )
    assert refutation["source"] == "closed_issue_provider"
    assert refutation["basis"] == "official complete issue"


def test_coordinate_mismatch_is_refutation_only_for_locked_identity():
    match = {"coordinate_comparisons": [{
        "kind": "volume", "cited_value": "12", "matched_value": "13",
        "status": "mismatch",
    }]}
    locked = adjudicate_bibliographic_evidence(
        {}, status="resolved", evidence=_evidence(metadata_match=match),
        result={"status": "resolved", "via": "crossref", "metadata_match": match},
    )
    unlocked = adjudicate_bibliographic_evidence(
        {}, status="unverified", evidence=_evidence(metadata_match=match),
        result={"status": "unverified", "via": "crossref_metadata", "metadata_match": match},
    )
    assert locked["outcome"] == "identified_with_errors"
    assert locked["refutations"][0]["kind"] == "coordinate_mismatch"
    assert unlocked["outcome"] == "not_corroborated"
    assert unlocked["refutations"] == []


def test_neutral_article_locator_overlap_does_not_refute_locked_identity():
    ref = {
        "title": "A cited study", "year": 2020, "ay_surname": "Smith",
        "cited_coordinates": [{"kind": "article_locator", "normalized_value": "186"}],
    }
    match = _metadata_match_profile(ref, {"page": "186-187"}, ref["title"])
    match.update({
        "title_overlap": 1.0, "author_match": True,
        "year_match": True, "matched_year": 2020,
    })
    result = {
        "status": "resolved", "via": "crossref_metadata",
        "resolution_basis": "metadata_search", "matched_title": ref["title"],
        "metadata_match": match,
        "resolved_identifier": {
            "type": "doi", "value": "10.1000/locked", "validated_via": "crossref",
        },
    }

    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(metadata_match=match), result=result,
    )

    assert match["coordinate_comparisons"][0]["status"] == "inconclusive"
    assert verdict["outcome"] == "identified"
    assert verdict["refutations"] == []


@pytest.mark.parametrize("locator", ["M517-M525", "Q83-Q91"])
def test_parsed_prefixed_range_and_opaque_article_number_do_not_refute_locked_identity(locator):
    raw = (
        "Smith, J. (2020). A cited study. Journal of Tests, 5(1), "
        f"{locator}."
    )
    ref = _make_reference(1, raw)
    coordinate = next(
        row for row in ref["cited_coordinates"]
        if row["kind"] in {"article_locator", "elocator"}
    )
    match = _metadata_match_profile(
        ref, {"article-number": "gkz935"}, ref["title"],
    )
    match.update({
        "title_overlap": 1.0, "author_match": True,
        "year_match": True, "matched_year": 2020,
    })
    result = {
        "status": "resolved", "via": "crossref_metadata",
        "resolution_basis": "metadata_search", "matched_title": ref["title"],
        "metadata_match": match,
        "resolved_identifier": {
            "type": "doi", "value": "10.1000/locked", "validated_via": "crossref",
        },
    }

    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(metadata_match=match), result=result,
    )

    assert coordinate["kind"] == "article_locator"
    assert match["coordinate_comparisons"][-1]["status"] == "inconclusive"
    assert verdict["outcome"] == "identified"
    assert verdict["refutations"] == []


def test_positive_coordinate_occupancy_refutes_even_when_other_checks_are_incomplete():
    occupancy = {
        "status": "resolved", "via": "pubmed_coordinate_occupancy",
        "matched_title": "A different work", "reason": "tuple occupied",
        "metadata_match": {"coordinate_comparisons": [
            {"kind": "container", "cited_value": "Journal", "matched_value": "Journal", "status": "match"},
            {"kind": "volume", "cited_value": "27", "matched_value": "27", "status": "match"},
            {"kind": "article_page_range", "cited_value": "49-58", "matched_value": "49-57", "status": "mismatch"},
        ]},
    }
    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified", evidence=_evidence(),
        result={"status": "unverified"},
        attempts=[occupancy, {"status": "unresolved", "via": "crossref_metadata"}],
    )
    assert verdict["outcome"] == "refuted"
    assert verdict["check_status"] == "incomplete"
    assert verdict["refutations"][-1]["kind"] == "coordinate_occupied_by_other_work"

    tag, risk, reason = resolve._reference_status_tag(
        {"source_kind": "article_like", "indexability": "high"},
        "unverified",
        {
            "bibliographic_adjudication": verdict,
            "synthetic_reference_risk": {},
        },
        {"status": "unverified"},
    )
    assert (tag, risk) == ("suspected_fabricated", "high")
    assert "another work" in reason
    assert "PubMed" not in reason


def test_pubmed_inverse_coordinate_occupancy_requires_positive_tuple(monkeypatch):
    calls = []

    def match(**kwargs):
        calls.append(kwargs["author"])
        if kwargs["author"]:
            return {"status": "not_found", "via": "pubmed_citation_match", "reason": "none"}
        return {
            "status": "resolved", "matched_title": "Different article",
            "matched_authors": ["Other"], "matched_year": "2021",
            "matched_venue": "Journal Name", "matched_volume": "27",
            "matched_issue": "1", "matched_page": "49-57", "pmid": "1",
        }

    monkeypatch.setattr(resolve, "_pubmed_citation_match", match)
    ref = {
        "source_kind": "article_like", "year": 2021, "ay_surname": "Wrong",
        "raw_entry": "Wrong. Claimed article. Journal Name.",
        "title": "Claimed article", "cited_coordinates": [
            {"kind": "container", "normalized_value": "Journal Name"},
            {"kind": "volume", "normalized_value": "27"},
            {"kind": "article_page_range", "normalized_value": "49-58"},
        ],
    }
    occupancy = resolve._pubmed_coordinate_occupancy_search(ref)
    assert calls == ["Wrong", ""]
    assert occupancy["status"] == "resolved"
    assert occupancy["via"] == "pubmed_coordinate_occupancy"

    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "ambiguous", "via": "pubmed_citation_match", "reason": "ambiguous",
    })
    assert resolve._pubmed_coordinate_occupancy_search(ref)["status"] == "ambiguous"


def test_pubmed_coordinate_occupancy_does_not_refute_exact_title_without_authors(monkeypatch):
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "resolved", "matched_title": "Exact cited article title",
        "matched_authors": [], "matched_year": "2021", "matched_venue": "Journal Name",
        "matched_volume": "27", "matched_page": "49-58", "pmid": "1",
    })
    ref = {
        "source_kind": "article_like", "year": 2021, "title": "Exact cited article title",
        "raw_entry": "Wrong. Exact cited article title. Journal Name. 2021.",
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "Journal Name"},
            {"kind": "volume", "normalized_value": "27"},
            {"kind": "article_page_range", "normalized_value": "49-58"},
        ],
    }
    assert resolve._pubmed_coordinate_occupancy_search(ref)["status"] == "unverified"


@pytest.mark.parametrize(
    ("parsed_title", "matched_title"),
    [
        ("Organization in autobiograph-\nical memory", "Organization in autobiographical memory"),
        ("Autobiographical memory and de-\npression", "Autobiographical memory and depression"),
    ],
)
def test_pubmed_coordinate_occupancy_does_not_refute_dehyphenated_same_work(
    monkeypatch, parsed_title, matched_title,
):
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "resolved", "matched_title": matched_title,
        "matched_authors": ["Conway"], "matched_year": "1987",
        "matched_venue": "Memory & Cognition", "matched_volume": "15",
        "matched_page": "119-132", "pmid": "1",
    })
    ref = {
        "source_kind": "article_like", "year": 1987, "ay_surname": "Conway",
        "title": parsed_title,
        "raw_entry": f"Conway. {parsed_title}. Memory & Cognition. 1987;15:119-132.",
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "Memory & Cognition"},
            {"kind": "volume", "normalized_value": "15"},
            {"kind": "article_page_range", "normalized_value": "119-132"},
        ],
    }
    assert resolve._pubmed_coordinate_occupancy_search(ref)["status"] == "unverified"


def test_pubmed_coordinate_occupancy_does_not_refute_when_parser_kept_journal_as_title(
    monkeypatch,
):
    matched_title = (
        "Doubling the efficiency of third harmonic generation by positioning ITO "
        "nanocrystals into the hot-spot of plasmonic gap-antennas"
    )
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "resolved", "matched_title": matched_title,
        "matched_authors": ["Metzger"], "matched_year": "2014",
        "matched_venue": "Nano Letters", "matched_volume": "14",
        "matched_page": "2867-2872", "pmid": "1",
    })
    ref = {
        "source_kind": "article_like", "year": 2014, "ay_surname": "Metzger",
        "title": "Nano Lett",
        "raw_entry": (
            "Metzger, B.; Hentschel, M. Doubling the effciency of third harmonic "
            "generation by positioning ITO nanocrystals into the hot-spot of "
            "plasmonic gap-antennas. Nano Lett. 2014, 14, 2867-2872."
        ),
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "Nano Letters"},
            {"kind": "volume", "normalized_value": "14"},
            {"kind": "article_page_range", "normalized_value": "2867-2872"},
        ],
    }
    assert resolve._pubmed_coordinate_occupancy_search(ref)["status"] == "unverified"


def test_pubmed_inverse_reuses_author_specific_candidate_without_second_request(monkeypatch):
    ref = {
        "source_kind": "article_like", "year": 2021, "ay_surname": "Attia",
        "raw_entry": "Attia. Claimed article. Journal Name. 2021;27:49-58.",
        "title": "Claimed article", "cited_coordinates": [
            {"kind": "container", "normalized_value": "Journal Name"},
            {"kind": "volume", "normalized_value": "27"},
            {"kind": "article_page_range", "normalized_value": "49-58"},
        ],
    }
    prior = {
        "status": "unverified", "via": "pubmed_citation_match",
        "matched_title": "A completely different published article",
        "matched_authors": ["Other"], "matched_year": "2021", "pmid": "1",
        "metadata_match": {
            "title_overlap": 0.0, "author_match": False, "year_match": True,
            "coordinate_comparisons": [
                {"kind": "container", "cited_value": "Journal Name", "matched_value": "Journal Name", "status": "match"},
                {"kind": "volume", "cited_value": "27", "matched_value": "27", "status": "match"},
                {"kind": "article_page_range", "cited_value": "49-58", "matched_value": "49-57", "status": "mismatch"},
            ],
        },
    }
    monkeypatch.setattr(
        resolve, "_pubmed_citation_match",
        lambda **_kwargs: pytest.fail("the positive prior candidate must be reused"),
    )

    occupancy = resolve._pubmed_coordinate_occupancy_search(ref, prior)

    assert occupancy["status"] == "resolved"
    assert occupancy["pmid"] == "1"


def test_unique_crossref_same_work_correction_locks_identity_with_field_errors():
    ref = {
        "title": (
            "Artificial intelligence-enabled electrocardiography for identification "
            "of patients with low ejection fraction: a pragmatic study"
        ),
        "raw_entry": (
            "Attia ZI, Kapa S, Lopez-Jimenez F et al (2021) Artificial "
            "intelligence-enabled electrocardiography for identification of patients "
            "with low ejection fraction: a pragmatic study. Nat Med 27(1):49-58"
        ),
        "year": 2021,
        "ay_surname": "attia",
    }
    match = {
        "title_overlap": 0.9, "year_match": True, "matched_year": 2021,
        "author_match": False, "cited_first_author": "attia",
        "matched_first_author": "yao",
        "matched_venue": "Nature Medicine", "coordinate_comparisons": [
            {"kind": "container", "cited_value": "Nat Med", "matched_value": "Nature Medicine", "status": "inconclusive"},
            {"kind": "volume", "cited_value": "27", "matched_value": "27", "status": "match"},
            {"kind": "issue", "cited_value": "1", "matched_value": "5", "status": "mismatch"},
            {"kind": "article_page_range", "cited_value": "49-58", "matched_value": "815-819", "status": "mismatch"},
        ],
    }
    result = {
        "status": "resolved", "via": "crossref_metadata",
        "resolution_basis": "metadata_search",
        "matched_title": (
            "Artificial intelligence–enabled electrocardiograms for identification "
            "of patients with low ejection fraction: a pragmatic, randomized clinical trial"
        ),
        "matched_authors": ["Yao", "Kapa", "Lopez-Jimenez", "Attia", "Friedman"],
        "metadata_match": match,
        "resolved_identifier": {"type": "doi", "value": "10.9999/ref7", "validated_via": "crossref:unique_same_work_correction"},
    }
    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(metadata_match=match), result=result,
    )
    assert verdict["outcome"] == "identified_with_errors"
    assert [item["kind"] for item in verdict["refutations"]] == [
        "coordinate_mismatch", "coordinate_mismatch", "author_mismatch",
    ]


@pytest.mark.parametrize(
    ("match_update", "expected_outcome"),
    [
        ({"year_match": False, "matched_year": 2021}, "identified_with_errors"),
        ({"coordinate_comparisons": [{
            "kind": "issue", "cited_value": "1", "matched_value": "5", "status": "mismatch",
        }]}, "identified_with_errors"),
        ({"coordinate_comparisons": [{
            "kind": "article_page_range", "cited_value": "49-58", "matched_value": "815-819", "status": "mismatch",
        }]}, "identified_with_errors"),
        ({"title_overlap": 0.86}, "identified"),
    ],
    ids=("year", "issue", "page", "title_coverage"),
)
def test_identity_locked_field_variants_remain_identified(match_update, expected_outcome):
    ref = {"title": "A cited study", "year": 2020, "ay_surname": "Smith"}
    match = {
        "title_overlap": 1.0, "author_match": True, "year_match": True,
        "matched_year": 2020, "coordinate_comparisons": [],
    }
    match.update(match_update)
    result = {
        "status": "resolved", "via": "crossref_metadata",
        "resolution_basis": "metadata_search", "matched_title": "A cited study",
        "metadata_match": match,
        "resolved_identifier": {
            "type": "doi", "value": "10.1000/locked", "validated_via": "crossref",
        },
    }

    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(metadata_match=match), result=result,
    )

    assert verdict["outcome"] == expected_outcome
    assert verdict["identity_status"] in {"identified", "identified_with_errors"}
    assert verdict["outcome"] not in {"refuted", "not_corroborated", "checks_incomplete"}


def test_weak_resolved_metadata_is_not_an_identity_lock():
    match = {
        "score": 0.45, "title_overlap": 0.93,
        "author_match": False, "year_match": False,
        "coordinate_comparisons": [{
            "kind": "volume", "cited_value": "12", "matched_value": "13",
            "status": "mismatch",
        }],
    }
    evidence = _evidence(
        resolution_basis="metadata_search", existence_confidence="medium",
        has_author=True, has_year=True, metadata_match=match,
    )

    verdict = adjudicate_bibliographic_evidence(
        {}, status="resolved", evidence=evidence,
        result={
            "status": "resolved", "via": "crossref_metadata",
            "resolution_basis": "metadata_search", "metadata_match": match,
        },
    )

    assert verdict["outcome"] == "not_corroborated"
    assert verdict["identity_status"] == "not_identified"
    assert verdict["correction_status"] == "not_attempted"
    assert verdict["refutations"] == []


@pytest.mark.parametrize(
    ("year_match", "coordinates"),
    [
        (False, []),
        (True, [{
            "kind": "volume", "cited_value": "12", "matched_value": "13",
            "status": "mismatch",
        }]),
    ],
)
def test_discovered_identifier_missing_author_cannot_lock_conflicting_metadata(
    year_match, coordinates,
):
    match = {
        "title_overlap": 1.0,
        "author_match": False,
        "matched_first_author": None,
        "year_match": year_match,
        "matched_year": 2019,
        "coordinate_comparisons": coordinates,
    }
    verdict = adjudicate_bibliographic_evidence(
        {"year": 2023, "first_author_surname": "Example"},
        status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search", existence_confidence="medium",
            has_author=True, has_year=True, metadata_match=match,
        ),
        result={
            "status": "resolved", "via": "openalex_search",
            "resolution_basis": "metadata_search", "metadata_match": match,
            "resolved_identifier": {
                "type": "doi", "value": "10.1000/synthetic",
                "validated_via": "crossref",
            },
        },
    )
    assert verdict["identity_status"] == "not_identified"
    assert verdict["refutations"] == []


def test_validated_discovered_doi_locks_candidate_without_erasing_year_error():
    match = {
        "title_overlap": 1.0,
        "author_match": True,
        "year_match": False,
        "matched_year": 2019,
        "coordinate_comparisons": [],
    }
    evidence = _evidence(
        resolution_basis="metadata_search", existence_confidence="medium",
        has_author=True, has_year=True, metadata_match=match,
    )

    verdict = adjudicate_bibliographic_evidence(
        {"year": 2023}, status="resolved", evidence=evidence,
        result={
            "status": "resolved", "via": "openalex_search",
            "resolution_basis": "metadata_search", "metadata_match": match,
            "resolved_identifier": {
                "type": "doi", "value": "10.1000/candidate",
                "validated_via": "crossref",
            },
        },
    )

    assert verdict["outcome"] == "identified_with_errors"
    assert verdict["identity_status"] == "identified_with_errors"
    assert verdict["refutations"] == [{
        "kind": "year_mismatch", "field": "year",
        "cited_value": "2023", "observed_value": "2019",
        "source": "openalex_search",
        "basis": "locked candidate metadata contradicts the cited publication year",
    }]


def test_exact_arxiv_record_with_canonical_identifier_locks_identity():
    match = {"title_overlap": 1.0, "matched_year": None}
    result = {
        "status": "resolved", "via": "arxiv_search",
        "resolution_basis": "metadata_search", "metadata_match": match,
        "fulltext_links": [{
            "url": "https://arxiv.org/pdf/1409.3215.pdf",
            "identity_context": {
                "canonical_host": True,
                "identifiers": {"arxiv_id": "1409.3215"},
            },
        }],
    }

    verdict = adjudicate_bibliographic_evidence(
        {}, status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search", existence_confidence="medium",
            has_author=True, has_year=True, metadata_match=match,
        ),
        result=result,
    )

    assert verdict["outcome"] == "identified"
    assert verdict["identity_status"] == "identified"


@pytest.mark.parametrize(
    ("via", "url"),
    [
        ("jmlr_search", "https://www.jmlr.org/papers/volume15/example/example.pdf"),
        (
            "neurips_search",
            "https://proceedings.neurips.cc/paper_files/paper/2008/file/example-Paper.pdf",
        ),
    ],
)
def test_authoritative_catalogue_record_locks_identity_and_retains_year_error(via, url):
    match = {
        "title_overlap": 1.0,
        "author_match": True,
        "year_match": False,
        "matched_year": 2008,
    }
    result = {
        "status": "resolved",
        "via": via,
        "resolution_basis": "metadata_search",
        "matched_title": "An exact catalogue title",
        "metadata_match": match,
        "fulltext_links": [{
            "url": url,
            "content_type": "pdf",
            "identity_context": {"canonical_host": True, "provider": via},
        }],
    }

    verdict = adjudicate_bibliographic_evidence(
        {"title": result["matched_title"], "year": 2009},
        status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search",
            existence_confidence="high",
            has_author=True,
            has_year=True,
            metadata_match=match,
        ),
        result=result,
    )

    assert verdict["outcome"] == "identified_with_errors"
    assert verdict["identity_status"] == "identified_with_errors"
    assert [item["kind"] for item in verdict["refutations"]] == ["year_mismatch"]


def test_authoritative_catalogue_link_does_not_override_author_conflict():
    match = {
        "title_overlap": 1.0,
        "author_match": False,
        "cited_first_author": "smith",
        "matched_first_author": "jones",
        "year_match": True,
        "matched_year": 2020,
    }
    result = {
        "status": "resolved",
        "via": "jmlr_search",
        "resolution_basis": "metadata_search",
        "metadata_match": match,
        "fulltext_links": [{
            "url": "https://www.jmlr.org/papers/volume20/wrong/wrong.pdf",
            "content_type": "pdf",
            "identity_context": {"canonical_host": True, "provider": "jmlr_search"},
        }],
    }

    verdict = adjudicate_bibliographic_evidence(
        {"title": "An exact catalogue title", "year": 2020},
        status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search",
            existence_confidence="high",
            has_author=True,
            has_year=True,
            metadata_match=match,
        ),
        result=result,
    )

    assert verdict["identity_status"] == "not_identified"


def test_network_failure_never_becomes_negative_evidence():
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/example"}, status="unresolved",
        evidence=_evidence(minimum_checks_completed=False),
        result={"status": "unresolved", "via": "doi.org", "reason": "HTTP 429"},
    )
    assert verdict["outcome"] == "checks_incomplete"
    assert verdict["refutations"] == []


def test_inconclusive_attempt_keeps_unverified_search_incomplete():
    verdict = adjudicate_bibliographic_evidence(
        {}, status="unverified", evidence=_evidence(),
        result={"status": "unverified", "via": "crossref_metadata"},
        attempts=[{
            "status": "unresolved", "via": "pubmed_citation_match",
            "reason": "HTTP 429",
        }],
    )

    assert verdict["outcome"] == "checks_incomplete"
    assert verdict["check_status"] == "incomplete"


def test_failed_optional_enrichment_does_not_reopen_identification_checks():
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/example"}, status="resolved",
        evidence=_evidence(resolution_basis="doi", existence_confidence="high"),
        result={"status": "resolved", "via": "crossref"},
        attempts=[{
            "status": "unresolved", "via": "europepmc", "enrichment_only": True,
            "reason": "HTTP 429",
        }],
    )

    assert verdict["outcome"] == "identified"
    assert verdict["check_status"] == "complete"


def _biomedical_corroboration_case():
    metadata_match = {
        "score": 0.70,
        "title_overlap": 0.667,
        "author_match": True,
        "cited_first_author": "moradi",
        "matched_first_author": "moradi",
        "year_match": True,
        "matched_year": 2000,
        "matched_venue": "Journal of Anxiety Disorders",
        "coordinate_comparisons": [
            {"kind": "container", "cited_value": "Journal of Anxiety Disorders",
             "matched_value": "Journal of Anxiety Disorders", "status": "match"},
            {"kind": "volume", "cited_value": "14", "matched_value": "14",
             "status": "match"},
            {"kind": "article_page_range", "cited_value": "521-534",
             "matched_value": "521-534", "status": "match"},
        ],
    }
    result = {
        "status": "resolved",
        "via": "crossref_metadata",
        "matched_title": (
            "Memory Bias for Emotional Information in Children and Adolescents "
            "with Posttraumatic Stress Disorder"
        ),
        "matched_authors": ["Moradi", "Taghavi"],
        "abstract": "A registry abstract for the identified article.",
        "abstract_via": "europepmc",
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "paywalled",
        "work_type": "journal-article",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "reason": "metadata search match",
        "metadata_match": metadata_match,
        "fulltext_links": [{
            "url": "https://doi.org/10.1016/S0887-6185(00)00037-2",
            "content_type": "doi",
        }],
        "identifiers": {
            "doi": "10.1016/S0887-6185(00)00037-2",
            "pmid": "11095544",
        },
    }
    attempts = [
        dict(result),
        {
            "status": "resolved",
            "via": "europepmc",
            "enrichment_only": True,
            "doi": "10.1016/S0887-6185(00)00037-2",
            "pmid": "11095544",
            "abstract": result["abstract"],
        },
    ]
    return metadata_match, result, attempts


def test_independent_doi_pmid_enrichment_locks_coherent_metadata_candidate():
    metadata_match, result, attempts = _biomedical_corroboration_case()
    verdict = adjudicate_bibliographic_evidence(
        {},
        status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search",
            existence_confidence="medium",
            has_author=True,
            has_year=True,
            metadata_match=metadata_match,
        ),
        result=result,
        attempts=attempts,
    )

    assert verdict["outcome"] == "identified"
    assert verdict["identity_status"] == "identified"


@pytest.mark.parametrize(
    "broken_signal",
    ["selected_doi", "enrichment_doi", "pmid", "author", "year", "title", "coordinates"],
)
def test_doi_pmid_enrichment_does_not_override_missing_or_conflicting_identity_signal(
    broken_signal,
):
    metadata_match, result, attempts = _biomedical_corroboration_case()
    enrichment = attempts[1]
    if broken_signal == "selected_doi":
        result["fulltext_links"] = []
    elif broken_signal == "enrichment_doi":
        enrichment["doi"] = "10.1016/unrelated"
    elif broken_signal == "pmid":
        enrichment["pmid"] = None
    elif broken_signal == "author":
        metadata_match["author_match"] = False
    elif broken_signal == "year":
        metadata_match["year_match"] = False
    elif broken_signal == "title":
        metadata_match["title_overlap"] = 0.4
    else:
        metadata_match["coordinate_comparisons"] = [
            {"kind": "volume", "status": "match"},
            {"kind": "article_page_range", "status": "inconclusive"},
        ]

    verdict = adjudicate_bibliographic_evidence(
        {},
        status="resolved",
        evidence=_evidence(
            resolution_basis="metadata_search",
            existence_confidence="medium",
            has_author=True,
            has_year=True,
            metadata_match=metadata_match,
        ),
        result=result,
        attempts=attempts,
    )

    assert verdict["outcome"] == "not_corroborated"
    assert verdict["identity_status"] == "not_identified"


def test_finalization_projects_abstract_after_independent_doi_pmid_corroboration(monkeypatch):
    metadata_match, result, attempts = _biomedical_corroboration_case()
    ref = {
        "id": "ref-biomedical",
        "ref_number": 1,
        "raw_entry": (
            "Moradi et al. Memory bias for emotional information in children and "
            "adolescents with PTSD: A preliminary study. Journal of Anxiety "
            "Disorders. 2000;14:521-534."
        ),
        "title": (
            "Memory bias for emotional information in children and adolescents "
            "with PTSD: A preliminary study"
        ),
        "year": 2000,
        "ay_surname": "moradi",
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "indexability": "high",
    }
    monkeypatch.setattr(resolve, "assess_local_journal", lambda _ref: None)

    out = resolve._finalize_resolution(
        ref,
        result,
        attempts,
        enrichment_continuation={"kind": "enrichment_done", "enriched": result},
    )

    assert out["abstract"] == result["abstract"]
    assert out["abstract_via"] == "europepmc"
    assert out["evidence_profile"]["bibliographic_adjudication"]["identity_status"] == "identified"


def test_pubmed_coordinate_correction_verifies_returned_candidate(monkeypatch):
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "resolved", "via": "pubmed_citation_match", "reason": None,
        "pmid": "123", "matched_title": "A precise article title for testing",
        "matched_authors": ["Smith"], "matched_year": "2020",
        "matched_venue": "British Medical Journal", "matched_volume": "323",
        "matched_issue": "1", "matched_page": "42-46", "abstract": "Abstract",
    })
    ref = {
        "source_kind": "article_like", "year": 2020, "ay_surname": "Smith",
        "title": "A precise article title for testing",
        "raw_entry": (
            "Smith. A precise article title for testing. BMJ. 2020;323(1):42-46."
        ),
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "BMJ"},
            {"kind": "volume", "normalized_value": "323"},
            {"kind": "issue", "normalized_value": "1"},
            {"kind": "article_page_range", "normalized_value": "42-46"},
        ],
    }
    candidate = resolve._pubmed_coordinate_correction_search(ref)
    assert candidate["status"] == "resolved"
    assert candidate["pmid"] == "123"
    assert candidate["metadata_match"]["author_match"] is True
    assert all(
        comparison["status"] != "mismatch"
        for comparison in candidate["metadata_match"]["coordinate_comparisons"]
    )


def test_pubmed_coordinate_correction_rejects_ordinal_conflict(monkeypatch):
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: {
        "status": "resolved", "via": "pubmed_citation_match", "reason": None,
        "pmid": "123", "matched_title": "Third international study of outcomes",
        "matched_authors": ["Smith"], "matched_year": "2020",
        "matched_venue": "British Medical Journal", "matched_volume": "323",
        "matched_issue": "1", "matched_page": "42-46", "abstract": "Other work",
    })
    ref = {
        "source_kind": "article_like", "year": 2020, "ay_surname": "Smith",
        "title": "Second international study of outcomes",
        "raw_entry": (
            "Smith. Second international study of outcomes. BMJ. "
            "2020;323(1):42-46."
        ),
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "BMJ"},
            {"kind": "volume", "normalized_value": "323"},
            {"kind": "issue", "normalized_value": "1"},
            {"kind": "article_page_range", "normalized_value": "42-46"},
        ],
    }

    candidate = resolve._pubmed_coordinate_correction_search(ref)

    assert candidate["status"] == "unverified"
    assert candidate["existence_confidence"] == "low"
    assert candidate["metadata_match"]["ordinal_conflict"] is True
    assert candidate["metadata_match"]["score"] == 0.0


def test_pubmed_coordinate_correction_requires_closed_input_tuple(monkeypatch):
    called = False

    def unexpected(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(resolve, "_pubmed_citation_match", unexpected)
    assert resolve._pubmed_coordinate_correction_search({
        "source_kind": "article_like", "year": 2020,
        "cited_coordinates": [{"kind": "container", "normalized_value": "BMJ"}],
    }) is None
    assert called is False


def test_ecitmatch_not_found_is_distinct_from_inconclusive_transport(monkeypatch):
    monkeypatch.setattr(resolve, "_get", lambda *_args, **_kwargs: (
        200, "BMJ|2020|323|42|Smith|callimachus|NOT_FOUND\n",
    ))
    missing = europepmc._pubmed_citation_match(
        journal="BMJ", year="2020", volume="323", first_page="42", author="Smith",
    )
    assert missing["status"] == "not_found"

    monkeypatch.setattr(resolve, "_get", lambda *_args, **_kwargs: (
        200, "unexpected response",
    ))
    inconclusive = europepmc._pubmed_citation_match(
        journal="BMJ", year="2020", volume="323", first_page="42", author="Smith",
    )
    assert inconclusive["status"] == "unresolved"
    assert "NOT fabrication" in inconclusive["reason"]


def test_ecitmatch_ambiguous_is_preserved_as_ambiguous_correction(monkeypatch):
    monkeypatch.setattr(resolve, "_get", lambda *_args, **_kwargs: (
        200, "BMJ|2020|323|42|Smith|callimachus|AMBIGUOUS\n",
    ))
    candidate = europepmc._pubmed_citation_match(
        journal="BMJ", year="2020", volume="323", first_page="42", author="Smith",
    )
    assert candidate["status"] == "ambiguous"
    verdict = adjudicate_bibliographic_evidence(
        {"doi": "10.1234/wrong"}, status="identifier_mismatch",
        evidence=_evidence(identifier_fallback=candidate),
        result={
            "status": "identifier_mismatch", "via": "crossref",
            "reason": "DOI targets another work", "identifier_fallback": candidate,
        },
    )
    assert verdict["correction_status"] == "ambiguous"


def test_ecitmatch_pmid_is_returned_only_with_fetchable_metadata(monkeypatch):
    monkeypatch.setattr(resolve, "_get", lambda *_args, **_kwargs: (
        200, "BMJ|2020|323|42|Smith|callimachus|12345\n",
    ))
    monkeypatch.setattr(
        europepmc,
        "_pubmed_fetch_metadata",
        lambda pmid, *, include_citation_fields: {
            "status": "resolved", "via": "pubmed", "matched_title": "Title",
            "abstract": None, "pmid": pmid, "article_ids": {"pubmed": pmid},
        },
    )
    candidate = europepmc._pubmed_citation_match(
        journal="BMJ", year="2020", volume="323", first_page="42", author="Smith",
    )
    assert candidate["status"] == "resolved"
    assert candidate["via"] == "pubmed_citation_match"
    assert candidate["pmid"] == "12345"


def test_identifier_failure_is_recovered_by_verified_coordinate_correction(monkeypatch):
    correction = {
        "status": "resolved", "via": "pubmed_citation_match",
        "reason": "candidate passed", "matched_title": "A precise article title",
        "matched_authors": ["Smith"], "matched_year": "2020", "pmid": "12345",
        "abstract": "Abstract", "retracted": False, "fulltext_exists": "unknown",
        "oa_status": "unknown", "work_type": "article",
        "resolution_basis": "metadata_search", "existence_confidence": "medium",
        "metadata_match": {
            "score": 1.0, "title_overlap": 1.0, "author_match": True,
            "cited_first_author": "smith", "matched_first_author": "smith",
            "year_match": True, "matched_year": 2020, "venue_overlap": 1.0,
            "matched_venue": "BMJ",
        },
        "fulltext_links": [],
    }
    monkeypatch.setattr(resolve, "_identifier_fallback_search", lambda _ref: {
        "status": "not_found", "via": "crossref_metadata", "reason": "no candidate",
    })
    monkeypatch.setattr(resolve, "_pubmed_coordinate_correction_search", lambda _ref: correction)
    ref = {
        "id": "r1", "ref_number": 1, "doi": "10.1234/wrong", "pmid": None,
        "isbn": None, "url": None, "year": 2020, "ay_surname": "Smith",
        "title": "A precise article title", "raw_entry": "Smith. A precise article title.",
        "source_kind": "article_like", "source_type": "article",
        "source_type_confidence": "high", "source_type_evidence": [],
        "indexability": "high", "cited_coordinates": [],
    }
    resolved = resolve._finalize_resolution(
        ref,
        {"status": "not_found", "via": "doi.org", "reason": "Handle responseCode 100"},
        [],
    )
    assert resolved["status"] == "not_found"
    assert resolved["reference_status_tag"] == "verified_with_identifier_error"
    assert resolved["evidence_profile"]["bibliographic_adjudication"]["outcome"] == (
        "identified_with_errors"
    )
    assert [attempt.get("via") for attempt in resolved["attempts"]] == [
        "crossref_metadata", "pubmed_citation_match",
    ]


def test_admitted_correction_replaces_wrong_identifier_target_content(monkeypatch):
    correction = {
        "status": "resolved", "via": "pubmed_citation_match",
        "reason": "candidate passed", "matched_title": "The cited article",
        "matched_authors": ["Smith"], "matched_year": "2020", "pmid": "12345",
        "abstract": "Correct cited article abstract", "retracted": False,
        "fulltext_exists": True, "oa_status": "open", "work_type": "article",
        "resolution_basis": "metadata_search", "existence_confidence": "medium",
        "metadata_match": {
            "score": 1.0, "title_overlap": 1.0, "author_match": True,
            "cited_first_author": "smith", "matched_first_author": "smith",
            "year_match": True, "matched_year": 2020,
        },
        "fulltext_links": [{
            "url": "https://example.org/correct.pdf", "content_type": "pdf",
        }],
    }
    monkeypatch.setattr(resolve, "_identifier_fallback_search", lambda _ref: {
        "status": "not_found", "via": "crossref_metadata", "reason": "no candidate",
    })
    monkeypatch.setattr(resolve, "_pubmed_coordinate_correction_search", lambda _ref: correction)
    ref = {
        "id": "r-replaced", "ref_number": 1, "doi": "10.1234/wrong",
        "pmid": None, "isbn": None, "url": None, "year": 2020,
        "title": "The cited article", "ay_surname": "Smith",
        "raw_entry": "Smith. The cited article. Journal. 2020.",
        "source_kind": "article_like", "source_type": "article",
        "source_type_confidence": "high", "source_type_evidence": [],
        "indexability": "high", "cited_coordinates": [],
    }
    primary = {
        "status": "identifier_mismatch", "via": "crossref",
        "reason": "DOI targets another work", "matched_title": "The other article",
        "abstract": "Incorrect primary article abstract", "retracted": True,
        "fulltext_exists": True, "oa_status": "open", "work_type": "article",
        "fulltext_links": [{
            "url": "https://example.org/wrong.pdf", "content_type": "pdf",
        }],
    }

    resolved = resolve._finalize_resolution(ref, primary, [dict(primary)])

    assert resolved["evidence_profile"]["bibliographic_adjudication"]["outcome"] == (
        "identified_with_errors"
    )
    assert resolved["abstract"] == "Correct cited article abstract"
    assert resolved["abstract_via"] == "pubmed_citation_match"
    assert resolved["matched_title"] == "The cited article"
    assert resolved["retracted"] is False
    assert [link["url"] for link in resolved["fulltext_links"]] == [
        "https://example.org/correct.pdf",
    ]
    assert resolved["fulltext_links"][0]["discovered_via"] == "pubmed_citation_match"
    assert "wrong.pdf" not in str(resolved["evidence_payload"])
    assert resolved["evidence_payload"]["matched_title"] == "The cited article"
    assert resolved["evidence_payload"]["via"] == "pubmed_citation_match"
    assert resolved["retraction"] == {
        "status": "unknown",
        "checked_via": "none",
        "evidence": {
            "retracted": False,
            "resolver_via": "pubmed_citation_match",
            "attempt_via": None,
        },
    }
    assert resolved["trace"][-2]["outcome"] == "unknown"
    refutations = resolved["evidence_profile"]["bibliographic_adjudication"][
        "refutations"
    ]
    assert refutations[0]["observed_value"] == "The other article"
    assert refutations[0]["source"] == "crossref"


def test_recovered_crossref_metadata_default_is_not_retraction_evidence():
    wrong_target = {
        "status": "identifier_mismatch", "via": "crossref",
        "matched_title": "The other article", "retracted": True,
    }
    correction = {
        "status": "resolved", "via": "crossref_metadata",
        "matched_title": "The cited article", "retracted": False,
    }
    recovered = resolve._project_recovered_content(wrong_target, correction)
    recovered["identifier_error_recovered"] = True

    assert _retraction_payload(recovered, [wrong_target, correction]) == {
        "status": "unknown",
        "checked_via": "none",
        "evidence": {
            "retracted": False,
            "resolver_via": "crossref_metadata",
            "attempt_via": None,
        },
    }


def test_identifier_fallback_ordinal_conflict_never_projects_content(monkeypatch):
    fallback = {
        "status": "resolved", "via": "crossref_metadata",
        "reason": "metadata candidate", "matched_title": "Third international study",
        "abstract": "Other work", "fulltext_links": [{
            "url": "https://example.org/other.pdf", "content_type": "pdf",
        }],
        "fulltext_exists": True, "oa_status": "open", "work_type": "article",
        "resolution_basis": "metadata_search", "existence_confidence": "medium",
        "metadata_match": {
            "score": 0.0, "title_overlap": 0.938,
            "author_match": True, "year_match": True,
            "ordinal_conflict": True,
        },
    }
    monkeypatch.setattr(resolve, "_identifier_fallback_search", lambda _ref: fallback)
    ref = {
        "id": "r-conflict", "ref_number": 1, "doi": "10.1234/wrong",
        "pmid": None, "isbn": None, "url": None, "year": 2020,
        "title": "Second international study", "ay_surname": "Smith",
        "raw_entry": "Smith. Second international study. Journal. 2020.",
        "source_kind": "article_like", "source_type": "article",
        "source_type_confidence": "high", "source_type_evidence": [],
        "indexability": "high", "cited_coordinates": [],
    }

    resolved = resolve._finalize_resolution(
        ref,
        {"status": "not_found", "via": "doi.org", "reason": "Handle responseCode 100"},
        [],
    )

    assert resolved["evidence_profile"]["bibliographic_adjudication"]["identity_status"] == (
        "not_identified"
    )
    assert resolved["reference_status_tag"] != "verified_with_identifier_error"
    assert resolved["abstract"] is None
    assert resolved["fulltext_links"] == []


def _validated_author_case(cited="Rivera", observed="Merton"):
    raw = f"{cited}, P. et al. Ion transport across layered membranes. Physical Science 12, 45 (2024)."
    ref = {"raw_entry": raw, "title": "Ion transport across layered membranes", "year": 2024}
    selected_doi = "10.1234/layered-membranes"
    result = {
        "status": "resolved", "via": "europepmc", "resolution_basis": "metadata_search",
        "matched_title": ref["title"],
        "metadata_match": {"title_overlap": 1.0, "matched_year": 2024},
        "resolved_identifier": {"type": "doi", "value": selected_doi, "validated_via": "crossref"},
        "fulltext_links": [{"url": f"https://doi.org/{selected_doi}"}],
    }
    attempt = {
        "via": "identifier_validation:crossref", "status": "resolved",
        "identifier_type": "doi", "identifier_value": selected_doi,
        "matched_title": ref["title"], "matched_authors": [observed],
        "metadata_match": {
            "title_overlap": 1.0, "author_match": cited == observed,
            "cited_first_author": cited.lower(), "matched_first_author": observed.lower(),
        },
    }
    return ref, result, attempt


@pytest.mark.parametrize(("cited", "observed"), [("Rivera", "Merton"), ("Kowalski", "Baxter")])
def test_bound_direct_validation_exposes_closed_source_author_conflict(cited, observed):
    ref, result, attempt = _validated_author_case(cited, observed)
    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(), result=result, attempts=[attempt],
    )
    assert verdict["identity_status"] == "identified_with_errors"
    assert verdict["refutations"] == [{
        "kind": "author_mismatch", "field": "author",
        "cited_value": cited.lower(), "observed_value": observed.lower(),
        "source": "identifier_validation:crossref",
        "basis": "identifier-bound validation contradicts the closed cited first author",
    }]


@pytest.mark.parametrize("broken", [
    "different_doi", "unverified", "search_attempt", "missing_author",
    "stale_cited_author", "open_author_head", "conflicting_selected_id",
    "equivalent_diacritics", "matching_author",
])
def test_unproven_direct_validation_author_conflict_is_not_refuted(broken):
    ref, result, attempt = _validated_author_case()
    if broken == "different_doi":
        attempt["identifier_value"] = "10.1234/another-work"
    elif broken == "unverified":
        attempt["status"] = "unverified"
    elif broken == "search_attempt":
        attempt["via"] = "crossref_metadata"
    elif broken == "missing_author":
        attempt["metadata_match"]["matched_first_author"] = None
    elif broken == "stale_cited_author":
        attempt["metadata_match"]["cited_first_author"] = "baxter"
    elif broken == "open_author_head":
        ref["raw_entry"] = "A discussion of Rivera and ion transport across layered membranes."
    elif broken == "conflicting_selected_id":
        result["resolved_identifier"]["value"] = "10.1234/another-work"
    elif broken == "equivalent_diacritics":
        ref, result, attempt = _validated_author_case("Zarate", "Zárate")
    elif broken == "matching_author":
        ref, result, attempt = _validated_author_case("Rivera", "Rivera")
    verdict = adjudicate_bibliographic_evidence(
        ref, status="resolved", evidence=_evidence(), result=result, attempts=[attempt],
    )
    assert not any(item["kind"] == "author_mismatch" for item in verdict["refutations"])
