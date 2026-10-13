"""Raw fallback must run after provider candidates fail the Resolve author gate."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from core.resolve import service
from core.resolve.providers import crossref_metadata


TITLE = (
    "A careful randomized evaluation of science learning strategies among "
    "secondary students"
)
RAW = (
    "Smith, J. A careful randomized evaluation of science learning strategies "
    "among secondary students. Journal of Learning Methods 2021;27:49-58."
)
WRONG_DOI = "10.9999/wrong-author"
RIGHT_DOI = "10.9999/cited-work"


def _reference() -> dict:
    return {
        "id": "r1",
        "ref_number": 1,
        "title": TITLE,
        "raw_entry": RAW,
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


def _record(doi: str, title: str, author: str) -> dict:
    return {
        "DOI": doi,
        "title": [title],
        "author": [{"family": author}],
        "published-print": {"date-parts": [[2021]]},
        "container-title": ["Journal of Learning Methods"],
        "volume": "27",
        "page": "49-58",
        "type": "journal-article",
    }


def _wrong_record() -> dict:
    return _record(
        WRONG_DOI,
        TITLE.replace("strategies", "interventions"),
        "Jones",
    )


def _right_record() -> dict:
    return _record(RIGHT_DOI, TITLE, "Smith")


def _install_search(monkeypatch, *, initial, raw, raw_status=200):
    requests: list[str] = []

    def get(url, *_args, **_kwargs):
        requests.append(url)
        query = parse_qs(urlparse(url).query)["query.bibliographic"][0]
        if query == RAW:
            return raw_status, json.dumps({"message": {"items": raw}})
        return 200, json.dumps({"message": {"items": initial}})

    monkeypatch.setattr(service, "_get", get)
    monkeypatch.setattr(service._rw, "is_retracted", lambda *_args: False)
    return requests


def _finish_without_network(monkeypatch, ref: dict, result: dict) -> dict:
    attempts: list[dict] = []
    service._append_provider_attempt(attempts, result)
    monkeypatch.setattr(
        service,
        "_crossref_resolver_module",
        lambda: type("Crossref", (), {
            "resolve_doi": staticmethod(lambda _doi, **_kwargs: {
                "status": "resolved", "via": "crossref", "matched_title": TITLE,
                "retracted": False,
            }),
        })(),
    )
    monkeypatch.setattr(service, "_resolver_registry", lambda: {})
    monkeypatch.setattr(service, "_load_fallback_order", lambda: [])
    monkeypatch.setattr(service, "_attest_repository_record", lambda _ref, candidate, _attempts: candidate)
    monkeypatch.setattr(
        service, "_enrich_resolved_metadata",
        lambda _ref, candidate, _attempts, **_kwargs: candidate,
    )
    monkeypatch.setattr(service, "_annotate_primary_fulltext_links", lambda candidate: candidate)
    return service._complete_discovery_after_optional(
        ref, result, attempts, discovery_only=False,
    )


def test_discovery_queries_use_the_parsed_first_author(monkeypatch):
    ref = _reference()
    ref["first_author"] = "Smith"
    requests = _install_search(monkeypatch, initial=[], raw=[])
    monkeypatch.setattr(service, "_first_author_key", lambda _raw: "unassigned-tail")

    crossref_metadata.discover(ref)

    query = parse_qs(urlparse(requests[0]).query)["query.bibliographic"][0]
    assert query.endswith(" smith")
    assert "unassigned-tail" not in query


def test_author_conflicting_first_result_runs_raw_fallback_and_full_resolve_identifies_cited_work(
    monkeypatch,
):
    ref = _reference()
    wrong = _wrong_record()
    profile = crossref_metadata._crossref_metadata_match_profile(ref, wrong)
    assert 0.85 <= profile["title_overlap"] < 0.90
    assert profile["author_match"] is False
    assert service._genuine_author_conflict(profile)
    requests = _install_search(
        monkeypatch, initial=[wrong], raw=[_right_record()],
    )

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved", result
    assert service._result_doi(result) == RIGHT_DOI
    assert len(requests) <= 4
    assert parse_qs(urlparse(requests[-1]).query)["query.bibliographic"] == [RAW]
    rejected = next(item for item in result["_candidate_rejections"] if item.get("doi") == WRONG_DOI)
    assert rejected["matched_title"] == wrong["title"][0]
    assert rejected["metadata_match"] == profile

    resolved = _finish_without_network(monkeypatch, ref, result)
    assert resolved["status"] == "resolved"
    assert resolved["identity"]["value"] == RIGHT_DOI, resolved


def test_complete_unique_same_work_correction_remains_admitted(monkeypatch):
    ref = _reference()
    wrong = _wrong_record()
    requests = _install_search(monkeypatch, initial=[wrong], raw=[])
    monkeypatch.setattr(
        service, "is_unique_same_work_correction_candidate", lambda *_args: True,
    )

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved", result
    assert all(parse_qs(urlparse(url).query)["query.bibliographic"][0] != RAW for url in requests)
    assert result["resolved_identifier"] == {
        "type": "doi",
        "value": WRONG_DOI,
        "validated_via": "crossref:unique_same_work_correction",
    }


def test_raw_failure_keeps_author_conflict_unadmitted_and_search_incomplete(monkeypatch):
    wrong = _wrong_record()
    requests = _install_search(
        monkeypatch, initial=[wrong], raw=[], raw_status=429,
    )

    result = crossref_metadata.discover(_reference())

    assert len(requests) <= 4
    assert result["status"] != "resolved"
    assert not service._result_doi(result)
    assert result["identity_search"]["completion"] == "incomplete"
    assert result["identity_search"]["outcome"] == "inconclusive"
    rejected = next(item for item in result["_candidate_rejections"] if item.get("doi") == WRONG_DOI)
    assert rejected["matched_title"] == wrong["title"][0]


def test_higher_scoring_conflicting_candidate_cannot_outrank_raw_candidate(monkeypatch):
    ref = _reference()
    wrong = _wrong_record()
    right = _right_record()
    profile = crossref_metadata._crossref_metadata_match_profile

    def high_wrong_score(reference, candidate):
        result = profile(reference, candidate)
        if candidate.get("DOI") == WRONG_DOI:
            result["score"] = 0.99
        elif candidate.get("DOI") == RIGHT_DOI:
            result["score"] = 0.60
        return result

    monkeypatch.setattr(crossref_metadata, "_crossref_metadata_match_profile", high_wrong_score)
    requests = _install_search(monkeypatch, initial=[wrong], raw=[wrong, right])

    result = crossref_metadata.discover(ref)

    assert len(requests) <= 4
    assert result["status"] == "resolved", result
    assert service._result_doi(result) == RIGHT_DOI
    rejected = next(item for item in result["_candidate_rejections"] if item.get("doi") == WRONG_DOI)
    assert rejected["metadata_match"]["score"] > result["metadata_match"]["score"]
