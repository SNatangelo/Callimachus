"""Titleless ECitMatch records need coordinates, not fabricated title overlap."""
from __future__ import annotations

from copy import deepcopy

import pytest

from core.parse.parsing_common import _make_reference
from core.resolve import service as resolve
from core.resolve.bibliographic_adjudication import (
    _resolved_identity_is_locked,
    adjudicate_bibliographic_evidence,
)
from core.resolve.matching import (
    _has_structurally_titleless_article_coordinates,
    _metadata_match_profile,
)


_CASES = (
    {
        "name": "springmann",
        "raw": "Springmann, M. et al. Nature 562, 519–525 (2018).",
        "title": "Options for keeping the food system within environmental limits.",
        "author": "Springmann",
        "year": "2018",
        "venue": "Nature",
        "volume": "562",
        "page": "519-525",
        "doi": "10.1038/s41586-018-0594-0",
        "pmid": "30305731",
    },
    {
        "name": "abbott",
        "raw": "Abbott, B. P. et al. Phys. Rev. Lett. 116, 061102 (2016).",
        "title": "Observation of Gravitational Waves from a Binary Black Hole Merger.",
        "author": "Abbott",
        "year": "2016",
        "venue": "Physical review letters",
        "volume": "116",
        "page": "061102",
        "doi": "10.1103/PhysRevLett.116.061102",
        "pmid": "26918975",
    },
    {
        "name": "clark",
        "raw": "Clark, I. C. et al. Nature 614, 318–325 (2023).",
        "title": "HIV silencing and cell survival signatures in infected T cell reservoirs.",
        "author": "Clark",
        "year": "2023",
        "venue": "Nature",
        "volume": "614",
        "page": "318-325",
        "doi": "10.1038/s41586-022-05556-6",
        "pmid": "36599978",
    },
)


def _reference(raw: str) -> dict:
    ref = _make_reference(1, raw)
    ref.update({"id": "titleless-test", "ref_number": 1, "raw_entry": raw})
    return ref


def _coordinate(ref: dict, kind: str) -> str:
    return next(
        item["normalized_value"]
        for item in ref.get("cited_coordinates") or ()
        if item.get("kind") == kind
    )


def _candidate(ref: dict, case: dict, **overrides) -> dict:
    locator = next(
        item for item in ref.get("cited_coordinates") or ()
        if item.get("kind") in {
            "article_page_range", "elocator", "article_number", "article_locator",
        }
    )
    locator_value = locator["normalized_value"]
    first_page = locator_value.split("-", 1)[0].strip()
    author_query = case["author"].casefold()
    candidate = {
        "status": "resolved",
        "via": "pubmed_citation_match",
        "matched_title": case["title"],
        "matched_authors": [case["author"]],
        "matched_year": case["year"],
        "matched_venue": case["venue"],
        "matched_volume": case["volume"],
        "matched_page": case["page"],
        "pmid": case["pmid"],
        "article_ids": {"pubmed": case["pmid"], "doi": case["doi"]},
        "ecitmatch_http_status": 200,
        "ecitmatch_body": (
            f"{_coordinate(ref, 'container')}|{case['year']}|"
            f"{case['volume']}|{first_page}|{author_query}|callimachus|"
            f"{case['pmid']}\n"
        ),
    }
    candidate.update(overrides)
    return candidate


def _adjudicate(ref: dict, candidate: dict, *, status: str, attempts=()):
    match = candidate.get("metadata_match") or {}
    return adjudicate_bibliographic_evidence(
        ref,
        status=status,
        evidence={
            "resolution_basis": "metadata_search",
            "existence_confidence": candidate.get("existence_confidence", "medium"),
            "metadata_match": match,
            "has_author": True,
            "has_year": True,
            "minimum_checks_completed": True,
        },
        result=candidate,
        attempts=list(attempts),
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_exact_pubmed_tuple_admits_structurally_titleless_reference(monkeypatch, case):
    ref = _reference(case["raw"])
    if case["name"] == "clark":
        # A captured parser output for this raw tuple was the venue/coordinate
        # tail; keep that specific failure shape covered even if parsing changes.
        ref["title"] = "Nature 614, 318–325 (2023)"
        assert _has_structurally_titleless_article_coordinates(ref)
    candidate = _candidate(ref, case)
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    corrected = resolve._pubmed_coordinate_correction_search(ref)

    assert corrected["status"] == "resolved"
    assert corrected["metadata_match"]["title_overlap"] is None
    assert corrected["resolved_identifier"] == {
        "type": "doi",
        "value": case["doi"],
        "validated_via": "pubmed_citation_match",
    }
    assert corrected["identifiers"] == {"doi": case["doi"], "pmid": case["pmid"]}
    assert _adjudicate(ref, corrected, status="resolved")["identity_status"] == "identified"


def test_titleless_same_author_pubmed_record_is_not_a_contradictory_occupant(monkeypatch):
    case = _CASES[0]
    ref = _reference(case["raw"])
    candidate = _candidate(ref, case)
    message = {
        "title": [candidate["matched_title"]],
        "author": [{"family": case["author"]}],
        "published": {"date-parts": [[case["year"]]]},
        "container-title": [case["venue"]],
        "volume": case["volume"],
        "page": case["page"],
    }
    candidate["metadata_match"] = _metadata_match_profile(
        ref, message, candidate["matched_title"],
    )
    candidate["status"] = "unverified"
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    occupancy = resolve._pubmed_coordinate_occupancy_search(
        ref, prior_candidate=candidate,
    )

    assert occupancy["status"] == "unverified"
    verdict = _adjudicate(
        ref, occupancy, status="unverified", attempts=[occupancy],
    )
    assert verdict["identity_status"] == "not_identified"
    assert not any(
        item["kind"] == "coordinate_occupied_by_other_work"
        for item in verdict["refutations"]
    )


def test_same_work_pubmed_author_date_tuple_is_not_refuted(monkeypatch):
    raw = (
        "Rennie D (1995) Reporting randomized controlled trials. "
        "An experiment and a call for responses from readers. "
        "JAMA 273: 1054–1055."
    )
    ref = {
        "source_kind": "article_like",
        "source_type": "article",
        "year": 1995,
        "ay_surname": "rennie",
        "title": "Reporting randomized controlled trials",
        "raw_entry": raw,
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "JAMA"},
            {"kind": "volume", "normalized_value": "273"},
            {"kind": "article_page_range", "normalized_value": "1054-1055"},
        ],
    }
    candidate = {
        "status": "unverified",
        "via": "pubmed_citation_match",
        "matched_title": (
            "Reporting randomized controlled trials: An experiment and a call "
            "for responses from readers"
        ),
        "matched_authors": ["Rennie D"],
        "matched_year": 1995,
        "matched_venue": "JAMA",
        "matched_volume": "273",
        "matched_page": "1054-1055",
        "pmid": "7897791",
    }
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    occupancy = resolve._pubmed_coordinate_occupancy_search(
        ref, prior_candidate=candidate,
    )

    assert occupancy["status"] == "unverified"
    assert occupancy["metadata_match"]["title_overlap"] == 1.0
    assert occupancy["metadata_match"]["author_match"] is True
    assert occupancy["metadata_match"]["cited_first_author"] == "rennie"
    verdict = _adjudicate(ref, occupancy, status="unverified", attempts=[occupancy])
    assert verdict["identity_status"] == "not_identified"
    assert verdict["outcome"] == "not_corroborated"
    assert not any(
        item["kind"] == "coordinate_occupied_by_other_work"
        for item in verdict["refutations"]
    )


@pytest.mark.parametrize(
    "case, overrides",
    [
        (_CASES[0], {"matched_authors": ["Other"]}),
        (_CASES[0], {"matched_year": "2019"}),
        (_CASES[0], {"matched_volume": "563"}),
        (_CASES[0], {"matched_page": "519"}),
        (_CASES[0], {"matched_page": "519-524"}),
        (_CASES[1], {"matched_venue": "A different journal"}),
        (_CASES[0], {"pmid": None}),
        (_CASES[0], {"article_ids": {"pubmed": "30305731"}}),
        (_CASES[0], {"ecitmatch_body": "Nature|2018|562|519|wrong|callimachus|30305731\n"}),
        (_CASES[0], {"via": "crossref"}),
        (_CASES[0], {"ecitmatch_http_status": 429}),
    ],
)
def test_titleless_admission_rejects_missing_or_conflicting_independent_evidence(
    monkeypatch, case, overrides,
):
    ref = _reference(case["raw"])
    monkeypatch.setattr(
        resolve,
        "_pubmed_citation_match",
        lambda **_kwargs: _candidate(ref, case, **overrides),
    )

    corrected = resolve._pubmed_coordinate_correction_search(ref)

    assert corrected["status"] != "resolved"
    assert corrected.get("resolved_identifier") is None


@pytest.mark.parametrize(
    "raw, remove_container_span",
    [
        (
            "Springmann, M. et al. A different cited article title. "
            "Nature 562, 519–525 (2018).",
            False,
        ),
        (
            "Springmann, M. et al. ‘A different cited article title.’ "
            "Nature 562, 519–525 (2018).",
            False,
        ),
        (
            "Springmann, M. et al. Nature 562, 519–525 (2018).",
            True,
        ),
        (
            "Springmann, M. et al. Nature 562, 519–525 (2018). Additional prose.",
            False,
        ),
    ],
)
def test_titleless_rule_requires_exact_author_to_container_tail(
    monkeypatch, raw, remove_container_span,
):
    ref = _reference(raw)
    ref["title"] = None
    if remove_container_span:
        next(item for item in ref["cited_coordinates"] if item["kind"] == "container").pop("spans")
    case = _CASES[0]
    monkeypatch.setattr(
        resolve,
        "_pubmed_citation_match",
        lambda **_kwargs: _candidate(ref, case),
    )

    corrected = resolve._pubmed_coordinate_correction_search(ref)

    assert corrected["status"] != "resolved"
    assert corrected.get("resolved_identifier") is None


def test_exact_title_candidate_keeps_existing_title_comparison(monkeypatch):
    raw = (
        "Springmann, M. et al. Options for keeping the food system within "
        "environmental limits. Nature 562, 519–525 (2018)."
    )
    ref = _reference(raw)
    case = dict(_CASES[0])
    candidate = _candidate(ref, case)
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    corrected = resolve._pubmed_coordinate_correction_search(ref)

    assert corrected["status"] == "resolved"
    assert corrected["metadata_match"]["title_overlap"] >= 0.90


def test_titleless_candidate_with_same_printed_range_and_e_extension_is_admitted(monkeypatch):
    raw = "Oginuma, M. et al. Dev. Cell 40, 342–353 (2017)."
    ref = _reference(raw)
    case = {
        "title": (
            "A Gradient of Glycolytic Activity Coordinates FGF and Wnt Signaling "
            "during Elongation of the Body Axis in Amniote Embryos."
        ),
        "author": "Oginuma",
        "year": "2017",
        "venue": "Developmental cell",
        "volume": "40",
        "page": "342-353.e10",
        "doi": "10.9999/printed-range-extension",
        "pmid": "28245921",
    }
    monkeypatch.setattr(
        resolve,
        "_pubmed_citation_match",
        lambda **_kwargs: _candidate(ref, case),
    )

    corrected = resolve._pubmed_coordinate_correction_search(ref)

    assert corrected["status"] == "resolved"
    assert corrected["metadata_match"]["title_overlap"] is None
    assert corrected["metadata_match"]["author_match"] is True
    assert corrected["metadata_match"]["year_match"] is True
    comparisons = corrected["metadata_match"]["coordinate_comparisons"]
    assert next(
        item for item in comparisons if item["kind"] == "container"
    )["status"] in {"match", "inconclusive"}
    assert next(
        item for item in comparisons if item["kind"] == "volume"
    )["status"] == "match"
    page_comparison = next(
        item for item in corrected["metadata_match"]["coordinate_comparisons"]
        if item["kind"] == "article_page_range"
    )
    assert page_comparison["cited_value"] == "342-353"
    assert page_comparison["matched_value"] == "342-353.e10"
    assert page_comparison["status"] == "match"
    assert corrected["via"] == "pubmed_citation_match"
    assert corrected["resolved_identifier"]["value"] == case["doi"]


def test_titleless_occupancy_still_refutes_a_genuine_author_contradiction(monkeypatch):
    case = _CASES[0]
    ref = _reference(case["raw"])
    candidate = _candidate(ref, case, matched_authors=["Other"])
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    correction = resolve._pubmed_coordinate_correction_search(ref)
    assert correction["status"] == "unverified"
    assert "matched_venue" not in correction
    occupancy = resolve._pubmed_coordinate_occupancy_search(
        ref, prior_candidate=correction,
    )

    assert occupancy["status"] == "resolved"
    verdict = _adjudicate(
        ref, occupancy, status="unverified", attempts=[occupancy],
    )
    assert verdict["outcome"] == "refuted"
    assert any(
        item["kind"] == "coordinate_occupied_by_other_work"
        for item in verdict["refutations"]
    )


def test_hyphenated_et_al_surname_is_recognized_from_source_spans():
    ref = _reference("Campos-Mercade, P. et al. Science 374, 879–882 (2021).")

    assert _has_structurally_titleless_article_coordinates(ref)


def test_complete_initialized_author_list_can_use_native_pubmed_identity(monkeypatch):
    case = {
        "author": "Smith",
        "year": "2021",
        "venue": "J. Exp. Methods",
        "volume": "12",
        "page": "345-367",
        "doi": "10.1234/example.2021.1",
        "pmid": "12345678",
        "title": "A synthetic article title used only by the stub.",
    }
    ref = _reference(
        "Smith, J., Jones, R. & Brown, C. J. Exp. Methods "
        "12, 345–367 (2021)."
    )
    ref["title"] = None
    candidate = _candidate(ref, case)
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)

    correction = resolve._pubmed_coordinate_correction_search(ref)

    assert correction["status"] == "resolved"
    assert correction["resolved_identifier"] == {
        "type": "doi",
        "value": case["doi"],
        "validated_via": "pubmed_citation_match",
    }
    assert correction["metadata_match"]["title_overlap"] is None
    assert _adjudicate(ref, correction, status="resolved")["identity_status"] == "identified"


def test_titleless_final_identity_lock_requires_pubmed_tuple_and_ids(monkeypatch):
    case = _CASES[0]
    ref = _reference(case["raw"])
    candidate = _candidate(ref, case)
    monkeypatch.setattr(resolve, "_pubmed_citation_match", lambda **_kwargs: candidate)
    corrected = resolve._pubmed_coordinate_correction_search(ref)
    evidence = {
        "resolution_basis": "metadata_search",
        "metadata_match": corrected["metadata_match"],
    }

    assert _resolved_identity_is_locked(ref, evidence, corrected)

    altered_results = []
    wrong_source = deepcopy(corrected)
    wrong_source["via"] = "crossref"
    altered_results.append(wrong_source)
    wrong_validation = deepcopy(corrected)
    wrong_validation["resolved_identifier"]["validated_via"] = "crossref"
    altered_results.append(wrong_validation)
    missing_pmid = deepcopy(corrected)
    missing_pmid["identifiers"].pop("pmid")
    altered_results.append(missing_pmid)
    missing_doi = deepcopy(corrected)
    missing_doi["identifiers"].pop("doi")
    altered_results.append(missing_doi)
    mismatched_doi = deepcopy(corrected)
    mismatched_doi["identifiers"]["doi"] = "10.9999/other"
    altered_results.append(mismatched_doi)
    coordinate_conflict = deepcopy(corrected)
    comparison = next(
        row for row in coordinate_conflict["metadata_match"]["coordinate_comparisons"]
        if row["kind"] == "article_page_range"
    )
    comparison["status"] = "mismatch"
    altered_results.append(coordinate_conflict)

    assert all(not _resolved_identity_is_locked(ref, evidence, result) for result in altered_results)
