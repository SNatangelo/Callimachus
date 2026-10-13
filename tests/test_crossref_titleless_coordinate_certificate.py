"""Crossref can identify a titleless article only from a complete tuple."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from core.parse.parsing_common import _make_reference
from core.resolve import service as resolve
from core.resolve.providers import crossref_metadata


def _ref(raw: str = "Smith, J. Example Journal 12, 34-45 (2020).") -> dict:
    ref = _make_reference(1, raw)
    ref.update({"id": "coordinate-certificate", "ref_number": 1, "raw_entry": raw})
    return ref


def _candidate(**overrides) -> dict:
    return {
        "DOI": "10.5555/example.2020.12",
        "type": "journal-article",
        "title": ["A Demonstration of Coordinate-Based Article Identity"],
        "author": [{"family": "Smith"}],
        "published-print": {"date-parts": [[2020]]},
        "container-title": ["Example Journal"],
        "volume": "12",
        "page": "34-45",
        "abstract": "validated article abstract",
        **overrides,
    }


def _occupancy_ref(
    title: str | None = None,
    raw: str = "Novotny, L.; van Hulst, N. Antennas for light. Nat. Photonics 2011, 5, 83-90.",
) -> dict:
    ref = _ref(raw)
    ref.update({
        "title": title,
        "year": 2011,
        "cited_coordinates": [
            {"kind": "container", "normalized_value": "Nat. Photonics"},
            {"kind": "volume", "normalized_value": "5"},
            {"kind": "article_page_range", "normalized_value": "83-90"},
        ],
    })
    return ref


def _occupant(**overrides) -> dict:
    return _candidate(**{
        "DOI": "10.1038/nphoton.2010.237",
        "title": ["Antennas for light"],
        "author": [{"family": "Novotny"}, {"family": "van Hulst"}],
        "published-print": {"date-parts": [[2011]]},
        "container-title": ["Nat. Photonics"],
        "volume": "5",
        "page": "83-90",
        **overrides,
    })


def _install_crossref(
    monkeypatch, raw_items, direct_by_doi=None, *, coordinate_items=None,
    raw_total=None, coordinate_total=None,
):
    calls: list[str] = []
    direct_by_doi = direct_by_doi or {}
    coordinate_items = raw_items if coordinate_items is None else coordinate_items

    def fake_get(url: str):
        calls.append(url)
        parsed = urlparse(url)
        if parsed.path.rstrip("/").endswith("/works") and parsed.query:
            query = parse_qs(parsed.query)
            is_coordinate_query = "query.author" in query
            items = coordinate_items if is_coordinate_query else raw_items
            return 200, json.dumps({
                "message": {
                    "items": items,
                    "total-results": (
                        coordinate_total if is_coordinate_query and coordinate_total is not None
                        else raw_total if not is_coordinate_query and raw_total is not None
                        else len(items)
                    ),
                },
            })
        doi = parsed.path.split("/works/", 1)[-1]
        payload = direct_by_doi.get(doi)
        if payload is None:
            return 404, json.dumps({"message": {}})
        return 200, json.dumps({"message": payload})

    monkeypatch.setattr(resolve, "_get", fake_get)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    return calls


def test_unique_complete_titleless_tuple_is_directly_validated(monkeypatch):
    ref = _ref()
    candidate = _candidate()
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved"
    assert result["doi"] == candidate["DOI"]
    assert result["resolved_identifier"] == {
        "type": "doi",
        "value": candidate["DOI"],
        "validated_via": "crossref:complete_coordinate_certificate",
    }
    assert result["resolution_basis"] == "metadata_search"
    assert result["existence_confidence"] == "medium"
    assert result["metadata_match"]["title_overlap"] is None
    assert any(url.startswith("https://api.crossref.org/works/10.5555/") for url in calls)


def test_definite_elocator_is_certified_without_promoting_a_title(monkeypatch):
    ref = _ref("Smith, J. Example Journal 12, e123456 (2020).")
    candidate = _candidate(page="e123456")
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})

    result = crossref_metadata.discover(ref)

    assert crossref_metadata._titleless_coordinate_details(ref)[-1] == "elocator"
    assert result["status"] == "resolved"
    assert result["metadata_match"]["title_overlap"] is None
    assert any(url.startswith("https://api.crossref.org/works/10.5555/") for url in calls)


def test_missing_parsed_title_does_not_make_coordinate_occupant_a_different_work(monkeypatch):
    ref = _occupancy_ref()
    candidate = _occupant()
    _install_crossref(monkeypatch, [candidate])

    profile = crossref_metadata._crossref_metadata_match_profile(ref, candidate)
    result = crossref_metadata.occupy_coordinates(ref)

    assert ref["title"] is None
    assert profile["author_match"] is True
    assert profile["year_match"] is True
    assert result["status"] == "unverified"


def test_missing_parsed_title_still_allows_positive_author_conflict(monkeypatch):
    ref = _occupancy_ref()
    candidate = _occupant(author=[{"family": "Different"}])
    _install_crossref(monkeypatch, [candidate])

    result = crossref_metadata.occupy_coordinates(ref)

    assert result["status"] == "resolved"
    assert result["metadata_match"]["author_match"] is False


def test_uncertain_title_boundary_keeps_independent_author_evidence(monkeypatch):
    raw = "Novotny, L.; van Hulst, N. Antennas for light. Nat. Photonics 2011, 5, 83-90."
    ref = _occupancy_ref(raw=raw)
    candidate = _occupant(author=[{"family": "Different"}])
    _install_crossref(monkeypatch, [candidate])

    assert ref["bibliographic_parse"]["status"] == "parsed"
    assert ref["title"] is None
    assert ref.get("journal") is None
    assert ref["authors"] == ["Novotny, L.", "van Hulst, N."]
    assert ref["bibliographic_parse"]["conflicts"] == [{
        "field": "title", "reason": "ambiguous_title_journal_boundary",
    }, {
        "field": "journal", "reason": "ambiguous_title_journal_boundary",
    }]
    result = crossref_metadata.occupy_coordinates(ref)

    assert result["status"] == "resolved"
    assert result["metadata_match"]["author_match"] is False


def test_known_title_conflict_still_refutes_a_coordinate_occupant():
    raw = "Novotny, L. A completely different cited title. Nat. Photonics 2011, 5, 83-90."
    ref = _occupancy_ref("A completely different cited title", raw=raw)
    candidate = _occupant()
    profile = {
        "title_overlap": 0.0,
        "author_match": True,
        "cited_first_author": "novotny",
        "matched_first_author": "novotny",
    }

    result = crossref_metadata._coordinate_occupancy_result(
        {candidate["DOI"]: (candidate, profile)}, year=2011, ref=ref,
    )

    assert result["status"] == "resolved"
    assert result["metadata_match"]["title_overlap"] == 0.0


def test_raw_query_miss_uses_bounded_author_coordinate_query(monkeypatch):
    ref = _ref()
    candidate = _candidate()
    calls = _install_crossref(
        monkeypatch, [], {candidate["DOI"]: candidate}, coordinate_items=[candidate],
    )

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved"
    assert result["doi"] == candidate["DOI"]
    coordinate_url = next(url for url in calls if "query.author" in url)
    query = parse_qs(urlparse(coordinate_url).query)
    assert query["query.author"] == ["smith"]
    assert query["query.bibliographic"] == ["2020 12 34-45"]
    assert query["filter"] == ["from-pub-date:2020-01-01,until-pub-date:2020-12-31"]
    assert query["rows"] == ["1000"]
    assert "query.container-title" not in query


def test_registered_short_container_title_can_prove_venue_without_catalog(monkeypatch):
    ref = _ref()
    candidate = _candidate(
        **{
            "container-title": ["International Review Journal"],
            "short-container-title": ["Example Journal"],
        },
    )
    assert crossref_metadata._titleless_coordinate_details(ref) is not None
    assert crossref_metadata._titleless_coordinate_venue_alias(ref, candidate) == "Example Journal"
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved"
    assert result["metadata_match"]["matched_venue"] == "International Review Journal"
    container = next(
        row for row in result["metadata_match"]["coordinate_comparisons"]
        if row["kind"] == "container"
    )
    assert container["matched_value"] == "Example Journal"
    assert container["status"] == "match"
    assert any("/works/10.5555/" in url for url in calls)


@pytest.mark.parametrize(
    ("raw", "native_venue", "doi", "author", "year", "volume", "page"),
    [
        (
            "Mudryk, L. et al. Cryosphere 14, 2495–2514 (2020).",
            "The Cryosphere", "10.5194/tc-14-2495-2020", "Mudryk", 2020,
            "14", "2495-2514",
        ),
        (
            "Vale, N. et al. Journal of Synthetic Snow 8, 21–32 (2022).",
            "The Journal of Synthetic Snow", "10.5555/synthetic.2022.8", "Vale",
            2022, "8", "21-32",
        ),
    ],
    ids=["cryosphere-primary-container", "independent-synthetic-journal"],
)
def test_optional_leading_the_uses_native_container_normalization(
    raw, native_venue, doi, author, year, volume, page,
):
    ref = _ref(raw)
    candidate = _candidate(
        DOI=doi,
        author=[{"family": author}],
        **{
            "published-print": {"date-parts": [[year]]},
            "container-title": [native_venue],
            "volume": volume,
            "page": page,
        },
    )

    assert crossref_metadata._titleless_coordinate_venue_alias(ref, candidate) == ""
    assert crossref_metadata._crossref_titleless_coordinate_certificate(ref, candidate)


def test_optional_leading_the_does_not_match_a_different_venue_suffix():
    ref = _ref("Mudryk, L. et al. Cryosphere 14, 2495–2514 (2020).")
    candidate = _candidate(
        author=[{"family": "Mudryk"}],
        **{
            "published-print": {"date-parts": [[2020]]},
            "container-title": ["The Cryosphere Communications"],
            "volume": "14",
            "page": "2495-2514",
        },
    )

    assert crossref_metadata._titleless_coordinate_venue_alias(ref, candidate) is None
    assert not crossref_metadata._crossref_titleless_coordinate_certificate(ref, candidate)


def test_dotted_uppercase_initialism_normalization_does_not_expand_abbreviations():
    assert crossref_metadata._coordinate_venue_key("U.S.A. Journal") == "usa journal"
    assert crossref_metadata._coordinate_venue_key("U.S.A. Journal") != (
        crossref_metadata._coordinate_venue_key("United States of America Journal")
    )


@pytest.mark.parametrize("overrides", [
    {"author": [{"family": "Jones"}]},
    {"published-print": {"date-parts": [[2021]]}},
    {"volume": "13"},
    {"page": "34-46"},
    {"container-title": ["Example Journal of Medicine"]},
    {"type": "book"},
    {"type": "dataset"},
    {"type": "posted-content"},
    {"title": [""]},
    {"DOI": "not-a-doi"},
], ids=[
    "wrong-author", "wrong-year", "wrong-volume", "wrong-end-page",
    "prefix-compatible-venue", "book", "dataset", "preprint", "missing-title", "invalid-doi",
])
def test_unproved_candidate_never_reaches_direct_validation(monkeypatch, overrides):
    candidate = _candidate(**overrides)
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})

    result = crossref_metadata.discover(_ref())

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert not any("/works/10.5555/" in url for url in calls)


def test_two_coherent_dois_remain_ambiguous(monkeypatch):
    first = _candidate()
    second = _candidate(DOI="10.5555/other.2020.12")
    calls = _install_crossref(
        monkeypatch, [first, second], {first["DOI"]: first, second["DOI"]: second},
    )

    result = crossref_metadata.discover(_ref())

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert not any("/works/10.5555/" in url for url in calls)


def test_truncated_coordinate_query_cannot_claim_unique_candidate(monkeypatch):
    candidate = _candidate()
    calls = _install_crossref(
        monkeypatch,
        [],
        {candidate["DOI"]: candidate},
        coordinate_items=[candidate],
        coordinate_total=1001,
    )

    result = crossref_metadata.discover(_ref())

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert not any("/works/10.5555/" in url for url in calls)


def test_incomplete_raw_query_is_replaced_by_complete_tuple_search(monkeypatch):
    candidate = _candidate()
    calls = _install_crossref(
        monkeypatch,
        [candidate],
        {candidate["DOI"]: candidate},
        coordinate_items=[candidate],
        raw_total=1001,
        coordinate_total=1,
    )

    result = crossref_metadata.discover(_ref())

    assert result["status"] == "resolved"
    assert any("query.author" in url for url in calls)
    assert result["identity_search"]["completion"] == "complete"


def test_direct_crossref_metadata_must_repeat_the_search_certificate(monkeypatch):
    candidate = _candidate()
    direct = _candidate(page="34-46")
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: direct})

    result = crossref_metadata.discover(_ref())

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert any(url.startswith("https://api.crossref.org/works/10.5555/") for url in calls)


def test_direct_crossref_doi_must_match_the_selected_search_candidate(monkeypatch):
    candidate = _candidate()
    direct = _candidate(DOI="10.5555/not-the-selected-work")
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: direct})

    result = crossref_metadata.discover(_ref())

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert "direct Crossref metadata certificate failed" in result["reason"]
    assert any(url.startswith("https://api.crossref.org/works/10.5555/") for url in calls)


def test_cited_issue_conflict_blocks_titleless_certificate(monkeypatch):
    candidate = _candidate(issue="3")
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})
    ref = _ref("Smith, J. Example Journal 12(2), 34-45 (2020).")

    result = crossref_metadata.discover(ref)

    assert result["status"] != "resolved"
    assert result.get("doi") is None
    assert not any(url.startswith("https://api.crossref.org/works/10.5555/") for url in calls)


def test_declared_doi_does_not_enter_titleless_coordinate_fallback(monkeypatch):
    candidate = _candidate()
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})
    ref = _ref()
    ref["doi"] = "10.5555/already-declared"

    result = crossref_metadata.discover(ref)

    assert result["status"] != "resolved"
    assert not any("query.author" in url for url in calls)
    assert not any("/works/10.5555%2F" in url for url in calls)


def test_unparsed_title_in_raw_does_not_enter_titleless_coordinate_fallback(monkeypatch):
    candidate = _candidate()
    calls = _install_crossref(monkeypatch, [candidate], {candidate["DOI"]: candidate})
    ref = _ref("Smith, J. A real but unparsed article title. Example Journal 12, 34-45 (2020).")
    ref["title"] = None

    result = crossref_metadata.discover(ref)

    assert result["status"] != "resolved"
    assert not any("query.author" in url for url in calls)
