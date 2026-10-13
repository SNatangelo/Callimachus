from __future__ import annotations

import json

import pytest

from core.infra.db.repository import RunRepository
from core.resolve import service as resolve
from core.resolve import decision, matching
from core.resolve.bibliographic_adjudication import adjudicate_bibliographic_evidence
from core.resolve.providers import crossref_metadata


def _ref3() -> dict:
    return {
        "title": (
            "Consensus disease definitions for neurologic immune-related "
            "adverse events of ICI"
        ),
        "raw_entry": (
            "Guidon AC, et al. Consensus disease definitions for neurologic "
            "immune-related adverse events of ICI. J Immunother Cancer "
            "2021;9:e002890."
        ),
        "year": 2021,
        "ay_surname": "guidon",
        "source_type": "article",
        "source_kind": "article_like",
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "J Immunother Cancer"},
            {"kind": "volume", "normalized_value": "9"},
            {"kind": "elocator", "normalized_value": "e002890"},
        ],
    }


def _jco_abstract() -> dict:
    return {
        "DOI": "10.1200/jco.2021.39.15_suppl.2647",
        "type": "journal-article",
        "title": [
            "Consensus disease definitions for the spectrum of neurologic "
            "immune related adverse events."
        ],
        "author": [{"family": "Guidon"}],
        "published-print": {"date-parts": [[2021]]},
        "container-title": ["Journal of Clinical Oncology"],
        "volume": "39",
        "issue": "15_suppl",
        "page": "2647",
        "abstract": "JCO conference abstract text",
    }


def _jitc_article() -> dict:
    return {
        "DOI": "10.1136/jitc-2021-002890",
        "type": "journal-article",
        "title": [_ref3()["title"]],
        "author": [{"family": "Guidon"}],
        "published-print": {"date-parts": [[2021]]},
        "container-title": ["Journal for ImmunoTherapy of Cancer"],
        "volume": "9",
        "page": "e002890",
        "abstract": "JITC article text",
    }


def _discover(monkeypatch, items: list[dict]) -> dict:
    monkeypatch.setattr(
        resolve,
        "_get",
        lambda *_args, **_kwargs: (
            200,
            json.dumps({"message": {"items": items}}),
        ),
    )
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    return crossref_metadata.discover(_ref3())


def test_crossref_does_not_admit_guidon_related_conference_abstract(monkeypatch):
    result = _discover(monkeypatch, [_jco_abstract()])

    assert result["status"] == "unverified"
    assert result.get("doi") is None
    assert result.get("abstract") is None
    assert not result.get("fulltext_links")
    assert not result.get("identifiers")
    assert not result.get("resolved_identifier")
    assert result["candidate_identifiers"] == {
        "doi": "10.1200/jco.2021.39.15_suppl.2647"
    }
    assert result["_candidate_rejections"][0]["metadata_match"][
        "coordinate_comparisons"
    ]


@pytest.mark.parametrize("items", [
    [_jco_abstract(), _jitc_article()],
    [_jitc_article(), _jco_abstract()],
])
def test_crossref_filters_related_abstract_before_candidate_ranking(monkeypatch, items):
    result = _discover(monkeypatch, items)

    assert result["status"] == "resolved"
    assert resolve._result_doi(result) == "10.1136/jitc-2021-002890"
    assert len(result["_candidate_rejections"]) == 1
    rejected = result["_candidate_rejections"][0]
    assert rejected["status"] == "unverified"
    assert rejected["doi"] == "10.1200/jco.2021.39.15_suppl.2647"
    assert "volume and article locator" in rejected["reason"]
    assert not any(key in rejected for key in (
        "abstract", "fulltext_links", "auxiliary_fulltext_links",
        "identifiers", "resolved_identifier",
    ))


def test_rejected_candidate_provenance_survives_later_wrong_year_return(monkeypatch):
    nearby = {
        "DOI": "10.1136/example.2023.0042",
        "type": "journal-article",
        "title": ["Clinical burden and management of checkpoint inhibitor toxicities"],
        "author": [{"family": "Guidon"}],
        "published-print": {"date-parts": [[2023]]},
        "container-title": ["Journal for ImmunoTherapy of Cancer"],
        "volume": "9",
        "page": "e002890",
    }
    items = [_jco_abstract(), nearby]
    monkeypatch.setattr(
        resolve,
        "_get",
        lambda url: (
            200,
            json.dumps({"message": {"items": [_jco_abstract()] if "filter=" in url else items}}),
        ),
    )
    original_profile = resolve._metadata_match_profile

    def profile_with_wrong_year(ref, msg, title):
        profile = original_profile(ref, msg, title)
        if title == nearby["title"][0]:
            return {
                **profile,
                "title_overlap": 0.70,
                "score": 0.80,
                "author_match": True,
                "matched_year": 2023,
                "year_match": False,
            }
        return profile

    monkeypatch.setattr(resolve, "_metadata_match_profile", profile_with_wrong_year)
    result = crossref_metadata.discover(_ref3())

    assert result["status"] == "unverified"
    assert "wrong year" in result["reason"]
    assert result["_candidate_rejections"][0]["doi"] == _jco_abstract()["DOI"]
    attempts = []
    resolve._append_provider_attempt(attempts, result)
    assert "_candidate_rejections" not in result
    assert attempts[0]["doi"] == _jco_abstract()["DOI"]
    assert "wrong year" in attempts[-1]["reason"]


def test_shared_promotion_withholds_coordinates_conflicting_metadata_identity(monkeypatch):
    ref = _ref3()
    candidate = _jco_abstract()
    profile = matching._metadata_match_profile(ref, candidate, candidate["title"][0])
    result = {
        "status": "resolved",
        "resolution_basis": "metadata_search",
        "via": "openalex_search",
        "doi": candidate["DOI"],
        "identifiers": {"doi": candidate["DOI"], "pmid": "12345"},
        "resolved_identifier": {
            "type": "doi", "value": candidate["DOI"], "validated_via": "crossref",
        },
        "matched_title": candidate["title"][0],
        "abstract": candidate["abstract"],
        "fulltext_links": [{"url": "https://example.invalid/wrong.pdf"}],
        "auxiliary_fulltext_links": [{"url": "https://example.invalid/wrong-aux.pdf"}],
        "metadata_match": profile,
    }
    monkeypatch.setattr(
        resolve,
        "_crossref_resolver_module",
        lambda: type("Crossref", (), {
            "resolve_doi": staticmethod(
                lambda _doi: pytest.fail("rejected candidate DOI must not be validated")
            ),
        }),
    )

    guarded = decision._promote_validated_identifier(ref, result, [])

    assert guarded["status"] == "unverified"
    assert guarded["candidate_identifiers"] == {"doi": candidate["DOI"]}
    assert "volume and article locator" in guarded["reason"]
    for field in (
        "doi", "identifiers", "resolved_identifier", "abstract",
        "fulltext_links", "auxiliary_fulltext_links",
    ):
        assert field not in guarded


def test_one_page_locator_mismatch_does_not_reject_candidate(monkeypatch):
    ref = _ref3()
    ref["cited_coordinates"] = [
        {"kind": "volume", "normalized_value": "9"},
        {"kind": "article_page_range", "normalized_value": "e002890"},
    ]
    candidate = _jitc_article()
    candidate["page"] = "e002891"

    result = _discover(monkeypatch, [candidate])

    assert result["status"] == "resolved"
    assert result.get("_candidate_rejections") is None
    assert resolve._result_doi(result) == candidate["DOI"]


@pytest.mark.parametrize("coordinates", [
    [
        {"kind": "volume", "normalized_value": "9"},
        {"kind": "issue", "normalized_value": "3"},
    ],
    [
        {"kind": "volume", "normalized_value": "9"},
        {"kind": "chapter_page_range", "normalized_value": "25-43"},
    ],
    [
        {"kind": "volume", "normalized_value": "9"},
        {"kind": "article_locator", "normalized_value": "e002890"},
    ],
])
def test_issue_chapter_or_ambiguous_locator_does_not_complete_conflict_pair(coordinates):
    ref = {**_ref3(), "cited_coordinates": coordinates}
    profile = matching._metadata_match_profile(ref, _jco_abstract(), _jco_abstract()["title"][0])

    assert matching._metadata_candidate_has_volume_and_locator_conflict(profile) is False


def test_declared_identifier_resolution_bypasses_metadata_candidate_gate(monkeypatch):
    candidate = _jco_abstract()
    ref = {**_ref3(), "doi": candidate["DOI"]}
    direct = {
        "status": "resolved",
        "resolution_basis": "doi",
        "doi": candidate["DOI"],
        "resolved_identifier": {
            "type": "doi", "value": candidate["DOI"], "validated_via": "crossref",
        },
        "metadata_match": matching._metadata_match_profile(
            ref, candidate, candidate["title"][0],
        ),
    }
    monkeypatch.setattr(
        resolve,
        "_crossref_resolver_module",
        lambda: pytest.fail("declared identifier result must bypass discovered-ID promotion"),
    )

    assert decision._promote_validated_identifier(ref, direct, []) is direct


def test_crossref_rejection_attempt_is_validated_and_stored(tmp_path, monkeypatch):
    result = _discover(monkeypatch, [_jco_abstract(), _jitc_article()])
    attempts = []
    resolve._append_provider_attempt(attempts, result)
    assert "_candidate_rejections" not in result
    repo = RunRepository.create(
        str(tmp_path), run_id="identity-gate", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[],
        references=[{"id": "r1", "ref_number": 1, "raw_entry": _ref3()["raw_entry"]}],
        citations=[],
    )
    try:
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "via": "crossref_metadata", "attempts": attempts,
        })
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        assert stored.attempts[0]["status"] == "unverified"
        assert stored.attempts[0]["doi"] == "10.1200/jco.2021.39.15_suppl.2647"
        assert stored.attempts[1]["matched_title"] == _jitc_article()["title"][0]
    finally:
        repo.close()


def test_all_rejected_candidates_leave_only_typed_audit_attempts(tmp_path, monkeypatch):
    result = _discover(monkeypatch, [_jco_abstract()])
    attempts = []
    resolve._append_provider_attempt(attempts, result)

    assert "_candidate_rejections" not in result
    assert result["status"] == "unverified"
    assert result["candidate_identifiers"] == {
        "doi": "10.1200/jco.2021.39.15_suppl.2647"
    }
    assert attempts[0]["doi"] == "10.1200/jco.2021.39.15_suppl.2647"
    assert "candidate_identifiers" not in attempts[-1]
    assert "_candidate_rejections" not in attempts[-1]

    repo = RunRepository.create(
        str(tmp_path), run_id="rejected-only", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[],
        references=[{"id": "r1", "ref_number": 1, "raw_entry": _ref3()["raw_entry"]}],
        citations=[],
    )
    try:
        repo.upsert_resolve_result("r1", {
            "status": "unverified", "via": "crossref_metadata", "attempts": attempts,
        })
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        assert len(stored.attempts) == 2
        assert stored.attempts[0]["doi"] == "10.1200/jco.2021.39.15_suppl.2647"
    finally:
        repo.close()


def test_optional_resolver_route_consumes_candidate_envelope(monkeypatch):
    class CrossrefMetadata:
        NAME = "crossref_metadata"
        RESOLVE_NAME = "crossref_metadata"

        @staticmethod
        def supports(_ref):
            return True

        @staticmethod
        def discover(_ref):
            return _discover(monkeypatch, [_jco_abstract(), _jitc_article()])

    monkeypatch.setattr(resolve, "_resolver_registry", lambda: {
        "crossref_metadata": CrossrefMetadata,
    })
    monkeypatch.setattr(resolve, "_load_fallback_order", lambda: ["crossref_metadata"])
    attempts = []

    result = resolve._try_resolvers_with_fallback(
        "crossref_metadata",
        _ref3(),
        result=None,
        attempts=attempts,
    )

    assert result is not None
    assert result["status"] == "resolved"
    assert "_candidate_rejections" not in result
    assert attempts[0]["status"] == "unverified"
    assert attempts[0]["doi"] == "10.1200/jco.2021.39.15_suppl.2647"
    assert "_candidate_rejections" not in attempts[-1]


def test_rejected_best_candidate_cannot_lock_identity_but_selected_match_can(monkeypatch):
    discovery = _discover(monkeypatch, [_jco_abstract(), _jitc_article()])
    rejected = discovery["_candidate_rejections"][0]
    best_candidate = {
        "source": rejected["via"],
        "title": rejected["matched_title"],
        "metadata_match": rejected["metadata_match"],
        "status": rejected["status"],
        "reason": rejected["reason"],
    }
    wrong_only_candidate = {
        "status": "resolved",
        "via": "crossref_metadata",
        "resolution_basis": "metadata_search",
        "doi": rejected["doi"],
        "matched_title": rejected["matched_title"],
        "metadata_match": rejected["metadata_match"],
    }
    monkeypatch.setattr(
        resolve,
        "_crossref_resolver_module",
        lambda: pytest.fail("coordinate-incompatible candidate must not be DOI-validated"),
    )
    wrong_only_result = decision._promote_validated_identifier(
        _ref3(), wrong_only_candidate, [],
    )
    wrong_verdict = adjudicate_bibliographic_evidence(
        _ref3(),
        status=wrong_only_result["status"],
        evidence={
            "resolution_basis": "metadata_search",
            "metadata_match": rejected["metadata_match"],
            "best_candidate": best_candidate,
            "minimum_checks_completed": True,
        },
        result=wrong_only_result,
        attempts=[rejected],
    )
    assert wrong_verdict["identity_status"] == "not_identified"

    correct = _jitc_article()
    correct_profile = matching._metadata_match_profile(
        _ref3(), correct, correct["title"][0],
    )
    selected_result = {
        "status": "resolved",
        "via": "openalex_search",
        "resolution_basis": "metadata_search",
        "matched_title": correct["title"][0],
        "resolved_identifier": {
            "type": "doi", "value": correct["DOI"], "validated_via": "crossref",
        },
        "metadata_match": correct_profile,
    }
    selected_verdict = adjudicate_bibliographic_evidence(
        _ref3(),
        status="resolved",
        evidence={
            "resolution_basis": "metadata_search",
            "metadata_match": correct_profile,
            "best_candidate": best_candidate,
            "minimum_checks_completed": True,
        },
        result=selected_result,
        attempts=[rejected],
    )
    assert selected_verdict["identity_status"] == "identified"
