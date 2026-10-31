# tests/test_resolve_wrong_work_provider_candidates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Captured wrong-work candidates stay non-operative during discovery."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from core.resolve import decision, service
from core.resolve.providers import crossref_metadata, europepmc


TITLE_REPLY = (
    "A discriminative learning approach to differential expression analysis "
    "for single-cell RNA-seq"
)
REPLY_TITLE = f"Reply to “{TITLE_REPLY}”"
DOI_REPLY = "10.1101/648733"
DOI_REPLY_TARGET = "10.1038/s41592-018-0303-9"

TITLE_COMMENTARY = (
    "SARS-CoV-2 cell entry depends on ACE2 and TMPRSS2 and is blocked by a "
    "clinically proven protease inhibitor"
)
COMMENTARY_TITLE = f"Commentary: {TITLE_COMMENTARY}."
DOI_COMMENTARY = "10.3389/fonc.2020.01448"
DOI_COMMENTARY_TARGET = "10.1016/j.cell.2020.02.052"


def _epmc_record(
    title: str,
    doi: str,
    year: str,
    *,
    pmcid: str | None = None,
    open_access: str = "N",
    pub_type: str = "journal article",
) -> dict:
    return {
        "title": title,
        "doi": doi,
        "pubYear": year,
        "pmcid": pmcid,
        "isOpenAccess": open_access,
        "inEPMC": "Y" if open_access == "Y" else "N",
        "pubType": pub_type,
    }


def _epmc_search(monkeypatch, title: str, year: int, records: list[dict], *, doi=None):
    resolve_stub = SimpleNamespace(
        _rw=SimpleNamespace(is_retracted=lambda _doi: False),
        _normalize_doi_value=lambda value: str(value or "").strip().casefold(),
        _title_match_score=service._title_match_score,
        _title_key=service._title_key,
        _get=lambda _url: (
            200,
            json.dumps({"resultList": {"result": records}}).encode("utf-8"),
        ),
    )
    monkeypatch.setattr(europepmc, "_resolve_module", lambda: resolve_stub)
    return europepmc._europepmc(None, doi, title, year)


def _crossref_reference() -> dict:
    return {
        "id": "lmfit-ref",
        "ref_number": 1,
        "title": "LMFIT: non-linear least-square minimization and curve-fitting for Python",
        "raw_entry": (
            "Newville, A. et al. LMFIT: non-linear least-square minimization and "
            "curve-fitting for Python. Zenodo (2014)."
        ),
        "year": 2014,
        "source_type": "unknown",
        "source_kind": "unknown",
        "cited_coordinates": [],
    }


def _crossref_lmfit_candidate(**overrides) -> dict:
    candidate = {
        "DOI": "10.1017/9781009214179.003",
        "title": ["Least Square Curve Fitting"],
        "type": "other",
        "issued": {"date-parts": [[2022]]},
        "container-title": ["Computing in Scilab"],
        "page": "95-134",
    }
    candidate.update(overrides)
    return candidate


def _crossref_discover(monkeypatch, ref: dict, candidate: dict) -> dict:
    requests: list[str] = []

    def get(url, *_args, **_kwargs):
        requests.append(url)
        params = parse_qs(urlparse(url).query)
        # Match the captured path: the year-filtered result is empty and the
        # unfiltered fallback returns the candidate record.
        items = [] if "filter" in params else [candidate]
        return 200, json.dumps({"message": {"items": items}})

    monkeypatch.setattr(service, "_get", get)
    monkeypatch.setattr(service._rw, "is_retracted", lambda _doi: False)
    result = crossref_metadata.discover(ref)
    assert requests
    return result


@pytest.mark.parametrize(
    ("title", "year", "wrong_title", "wrong_doi", "right_doi", "pmcid", "open_access"),
    [
        (TITLE_REPLY, 2019, REPLY_TITLE, DOI_REPLY, DOI_REPLY_TARGET, None, "N"),
        (
            TITLE_COMMENTARY,
            2020,
            COMMENTARY_TITLE,
            DOI_COMMENTARY,
            DOI_COMMENTARY_TARGET,
            "PMC7260001",
            "Y",
        ),
    ],
)
def test_europepmc_prefers_exact_normalized_title_to_captured_related_record(
    monkeypatch,
    title,
    year,
    wrong_title,
    wrong_doi,
    right_doi,
    pmcid,
    open_access,
):
    wrong = _epmc_record(
        wrong_title,
        wrong_doi,
        str(year),
        pmcid=pmcid,
        open_access=open_access,
        pub_type="article-commentary" if wrong_title.startswith("Commentary:") else "preprint",
    )
    cited = _epmc_record(
        title,
        right_doi,
        str(year),
        pmcid=pmcid,
        open_access=open_access,
        pub_type="journal article",
    )

    # The provider response orders the related title first, as in the captures.
    result = _epmc_search(monkeypatch, title, year, [wrong, cited])

    assert result["status"] == "resolved"
    assert result["identifiers"]["doi"] == right_doi
    assert result["matched_title"] == title


def test_europepmc_exact_title_precedes_a_richer_related_title(monkeypatch):
    related = _epmc_record(
        REPLY_TITLE,
        DOI_REPLY,
        "2019",
        pmcid="PMC648733",
        open_access="Y",
        pub_type="preprint",
    )
    exact = _epmc_record(TITLE_REPLY, DOI_REPLY_TARGET, "2019")

    result = _epmc_search(monkeypatch, TITLE_REPLY, 2019, [related, exact])

    assert result["identifiers"]["doi"] == DOI_REPLY_TARGET


@pytest.mark.parametrize(
    ("title", "year", "related_title", "wrong_doi", "pub_type"),
    [
        (TITLE_REPLY, 2019, REPLY_TITLE, DOI_REPLY, "preprint"),
        (TITLE_COMMENTARY, 2020, COMMENTARY_TITLE, DOI_COMMENTARY, "article-commentary"),
    ],
)
def test_europepmc_related_title_is_withheld_when_cited_work_is_absent(
    monkeypatch, title, year, related_title, wrong_doi, pub_type,
):
    ref = {"title": title, "raw_entry": title, "year": year}
    candidate = _epmc_record(related_title, wrong_doi, str(year), pub_type=pub_type)
    result = _epmc_search(monkeypatch, title, year, [candidate])
    assert result["status"] == "resolved"  # discovery is gated at promotion

    monkeypatch.setattr(
        service,
        "_crossref_resolver_module",
        lambda: SimpleNamespace(
            resolve_doi=lambda _doi, **_kwargs: {
                "status": "resolved",
                "via": "crossref",
                "matched_title": related_title,
                "matched_authors": ["Unrelated"],
                "work_type": pub_type,
            }
        ),
    )
    attempts: list[dict] = []
    promoted = decision._promote_validated_identifier(ref, result, attempts)

    assert promoted["status"] == "unverified"
    assert promoted["candidate_identifiers"] == {"doi": wrong_doi}
    assert promoted["matched_title"] == related_title
    assert "candidate identity withheld" in promoted["reason"]
    assert not any(key in promoted for key in ("doi", "identifiers", "resolved_identifier"))
    assert attempts == []


@pytest.mark.parametrize(
    ("cited_title", "year", "record_title", "doi"),
    [
        (REPLY_TITLE, 2019, REPLY_TITLE, DOI_REPLY),
        (COMMENTARY_TITLE, 2020, COMMENTARY_TITLE, DOI_COMMENTARY),
    ],
)
def test_citation_that_itself_names_reply_or_commentary_remains_eligible(
    monkeypatch, cited_title, year, record_title, doi,
):
    result = _epmc_search(
        monkeypatch,
        cited_title,
        year,
        [_epmc_record(record_title, doi, str(year), pub_type="article-commentary")],
    )
    monkeypatch.setattr(
        service,
        "_crossref_resolver_module",
        lambda: SimpleNamespace(
            resolve_doi=lambda _doi, **_kwargs: {
                "status": "resolved",
                "via": "crossref",
                "matched_title": record_title,
                "matched_authors": ["Cited Author"],
            }
        ),
    )

    promoted = decision._promote_validated_identifier(
        {"title": cited_title, "raw_entry": cited_title, "year": year}, result, [],
    )

    assert promoted["status"] == "resolved"
    assert promoted["resolved_identifier"]["value"] == doi


def test_europepmc_declared_doi_lookup_keeps_the_strong_identifier_route(monkeypatch):
    record = _epmc_record(COMMENTARY_TITLE, DOI_COMMENTARY, "2020", pub_type="article-commentary")
    result = _epmc_search(monkeypatch, TITLE_COMMENTARY, 2020, [record], doi=DOI_COMMENTARY)

    assert result["status"] == "resolved"
    assert result["identifiers"]["doi"] == DOI_COMMENTARY
    assert "resolution_basis" not in result


def test_crossref_withholds_captured_title_fragment_with_wrong_year_and_no_support(monkeypatch):
    ref = _crossref_reference()
    candidate = _crossref_lmfit_candidate()
    profile = crossref_metadata._crossref_metadata_match_profile(ref, candidate)
    assert profile["title_overlap"] == 1.0
    assert profile["score"] == 0.45
    assert profile["author_match"] is False
    assert profile["year_match"] is False
    assert not any(item["status"] == "match" for item in profile.get("coordinate_comparisons", []))

    result = _crossref_discover(monkeypatch, ref, candidate)

    assert result["status"] == "unverified"
    assert not service._result_doi(result)
    assert result["candidate_identifiers"] == {"doi": candidate["DOI"]}
    rejected = next(item for item in result["_candidate_rejections"] if item.get("doi") == candidate["DOI"])
    assert rejected["matched_title"] == "Least Square Curve Fitting"
    assert "year" in rejected["reason"]


def test_crossref_exact_title_year_mismatch_remains_admissible(monkeypatch):
    ref = _crossref_reference()
    candidate = _crossref_lmfit_candidate(
        title=[ref["title"]],
        author=[],
        issued={"date-parts": [[2022]]},
    )

    result = _crossref_discover(monkeypatch, ref, candidate)

    assert result["status"] == "resolved", result
    assert service._result_doi(result) == candidate["DOI"]


@pytest.mark.parametrize("support", ["author", "coordinates"])
def test_crossref_supported_partial_title_remains_admissible(monkeypatch, support):
    ref = {
        "id": "partial-ref",
        "ref_number": 2,
        "title": "A careful randomized evaluation of science learning strategies among secondary students",
        "raw_entry": (
            "Smith, J. A careful randomized evaluation of science learning strategies "
            "among secondary students. Journal of Learning Methods 2021;27:49-58."
        ),
        "year": 2021,
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "source_type_evidence": ["journal_volume_issue_pages"],
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "Journal of Learning Methods"},
            {"kind": "volume", "normalized_value": "27"},
            {"kind": "article_page_range", "normalized_value": "49-58"},
        ],
    }
    candidate = {
        "DOI": "10.9999/supported-partial",
        "title": ["A careful randomized evaluation of science learning strategies"],
        "type": "journal-article",
        "issued": {"date-parts": [[2022]]},
        "container-title": ["Journal of Learning Methods"],
    }
    if support == "author":
        candidate["author"] = [{"family": "Smith"}]
    else:
        candidate.update({"volume": "27", "page": "49-58"})

    result = _crossref_discover(monkeypatch, ref, candidate)

    assert result["status"] == "resolved", result
    assert service._result_doi(result) == candidate["DOI"]


def test_crossref_unique_same_work_correction_remains_admissible(monkeypatch):
    ref = _crossref_reference()
    candidate = _crossref_lmfit_candidate()
    monkeypatch.setattr(service, "is_unique_same_work_correction_candidate", lambda *_args: True)

    result = _crossref_discover(monkeypatch, ref, candidate)

    assert result["status"] == "resolved", result
    assert service._result_doi(result) == candidate["DOI"]
    assert result["resolved_identifier"]["validated_via"] == "crossref:unique_same_work_correction"
