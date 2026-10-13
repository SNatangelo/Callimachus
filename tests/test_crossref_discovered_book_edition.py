"""Crossref title discovery must distinguish a book title from its edition."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from core.resolve import service
from core.resolve.providers import crossref_metadata


TITLE = "A Practical Guide to Synthetic Statistical Analysis"
WRONG_EDITION_DOI = "10.5555/synthetic.book.2009"
SAME_EDITION_DOI = "10.5555/synthetic.book.2016"
OLD_ISBN = "9780387981406"
NEW_ISBN = "9780387981413"


def _reference(*, year: int = 2016, isbn: str | None = None, doi: str | None = None,
               source_type: str = "book", source_kind: str = "book_like") -> dict:
    return {
        "id": "synthetic-book-edition",
        "title": TITLE,
        "raw_entry": f"Miller, A. {TITLE}. Northfield Press, {year}.",
        "year": year,
        "source_type": source_type,
        "source_kind": source_kind,
        "isbn": isbn,
        "doi": doi,
        "cited_coordinates": [],
    }


def _candidate(*, year: int = 2009, doi: str = WRONG_EDITION_DOI,
               isbn: str | None = OLD_ISBN, **changes) -> dict:
    item = {
        "DOI": doi,
        "title": [TITLE],
        "author": [{"family": "Miller", "given": "A."}],
        "issued": {"date-parts": [[year]]},
        "type": "book",
    }
    if isbn is not None:
        item["ISBN"] = [isbn]
    item.update(changes)
    return item


def _discover(monkeypatch, ref: dict, search_results) -> tuple[dict, list[str]]:
    requests: list[str] = []

    def get(url, *_args, **_kwargs):
        requests.append(url)
        params = parse_qs(urlparse(url).query)
        return 200, json.dumps({"message": {"items": search_results(params)}})

    monkeypatch.setattr(service, "_get", get)
    monkeypatch.setattr(service._rw, "is_retracted", lambda _doi: False)
    return crossref_metadata.discover(ref), requests


def _all_rejections(result: dict) -> list[dict]:
    return [item for item in result.get("_candidate_rejections", []) if isinstance(item, dict)]


def test_crossref_title_only_does_not_admit_an_unsupported_book_edition(monkeypatch):
    old_edition = _candidate()
    result, _requests = _discover(
        monkeypatch,
        _reference(),
        lambda _params: [old_edition],
    )

    assert result["status"] != "resolved", result
    assert any(
        rejected.get("doi") == WRONG_EDITION_DOI
        and "edition" in str(rejected.get("reason", "")).casefold()
        for rejected in _all_rejections(result)
    ), result


def test_crossref_title_only_does_not_admit_unsupported_monograph_type(monkeypatch):
    older_monograph = _candidate(isbn=None, type="monograph")
    result, _requests = _discover(
        monkeypatch,
        _reference(),
        lambda _params: [older_monograph],
    )

    assert result["status"] != "resolved", result
    assert any(
        rejected.get("doi") == WRONG_EDITION_DOI
        and "edition" in str(rejected.get("reason", "")).casefold()
        for rejected in _all_rejections(result)
    ), result


def test_crossref_title_only_does_not_admit_unsupported_edited_book_edition(monkeypatch):
    ref = {
        "id": "illegality-inc",
        "title": "Illegality, Inc.: Clandestine Migration and the Business of Bordering Europe",
        "raw_entry": (
            "Andersson, Ruben. Illegality, Inc.: Clandestine Migration and the "
            "Business of Bordering Europe. University of California Press, 2014."
        ),
        "year": 2014, "source_type": "book", "source_kind": "book_like",
        "isbn": None, "doi": None, "cited_coordinates": [],
    }
    edited_ebook = {
        "DOI": "10.1525/9780520958289",
        "title": [ref["title"]],
        "author": [{"family": "Andersson", "given": "Ruben"}],
        "issued": {"date-parts": [[2019]]},
        "ISBN": ["9780520958289"],
        "type": "edited-book",
    }
    result, _requests = _discover(monkeypatch, ref, lambda _params: [edited_ebook])

    assert result["status"] != "resolved", result
    assert any(
        rejected.get("doi") == "10.1525/9780520958289"
        and "edition" in str(rejected.get("reason", "")).casefold()
        for rejected in _all_rejections(result)
    ), result


@pytest.mark.parametrize("unsupported_type", ["book", "edited-book"])
def test_withheld_book_edition_does_not_stop_search_for_the_cited_edition(
    monkeypatch, unsupported_type: str,
):
    old_edition = _candidate(type=unsupported_type)
    cited_edition = _candidate(
        year=2016,
        doi=SAME_EDITION_DOI,
        isbn=NEW_ISBN,
    )

    def search_results(params):
        return [old_edition] if "filter" in params else [cited_edition]

    result, requests = _discover(monkeypatch, _reference(), search_results)

    assert result["status"] == "resolved", result
    assert result["metadata_match"]["matched_year"] == 2016
    assert any("filter" in parse_qs(urlparse(url).query) for url in requests)
    assert any(
        rejected.get("doi") == WRONG_EDITION_DOI
        for rejected in _all_rejections(result)
    ), result


def test_same_year_monograph_candidate_remains_eligible(monkeypatch):
    same_year = _candidate(year=2016, doi=SAME_EDITION_DOI, isbn=NEW_ISBN)
    result, _requests = _discover(
        monkeypatch,
        _reference(),
        lambda _params: [same_year],
    )

    assert result["status"] == "resolved", result
    assert result["metadata_match"]["matched_year"] == 2016
    assert not _all_rejections(result)


def test_print_online_year_variance_remains_eligible(monkeypatch):
    online_print = _candidate(
        year=2016,
        doi=SAME_EDITION_DOI,
        isbn=OLD_ISBN,
        **{
            "published-online": {"date-parts": [[2015]]},
            "published-print": {"date-parts": [[2016]]},
        },
    )
    result, _requests = _discover(
        monkeypatch,
        _reference(year=2015),
        lambda _params: [online_print],
    )

    assert result["status"] == "resolved", result
    assert result["metadata_match"]["matched_year"] == 2016
    assert result["metadata_match"]["year_mismatch_plausible"] is True


@pytest.mark.parametrize(
    ("cited_isbn", "native_isbn"),
    [
        (NEW_ISBN, "978-0-387-98141-3"),
        ("0-387-98141-1", NEW_ISBN),
    ],
)
def test_matching_cited_isbn_supports_the_edition_despite_year_difference(
    monkeypatch, cited_isbn: str, native_isbn: str,
):
    old_edition = _candidate(isbn=native_isbn)
    result, _requests = _discover(
        monkeypatch,
        _reference(isbn=cited_isbn),
        lambda _params: [old_edition],
    )

    assert result["status"] == "resolved", result
    assert result["metadata_match"]["matched_year"] == 2009


@pytest.mark.parametrize("native_isbn", [OLD_ISBN, "978/0/387/98141/3"])
def test_nonmatching_cited_isbn_does_not_support_the_old_edition(monkeypatch, native_isbn: str):
    old_edition = _candidate(isbn=native_isbn)
    result, _requests = _discover(
        monkeypatch,
        _reference(isbn=NEW_ISBN),
        lambda _params: [old_edition],
    )

    assert result["status"] != "resolved", result
    assert any(rejected.get("doi") == WRONG_EDITION_DOI for rejected in _all_rejections(result))


@pytest.mark.parametrize(
    ("source_type", "source_kind", "doi"),
    [
        ("book", "book_like", "10.5555/source.declared"),
        ("article", "article_like", None),
        ("unknown", "unknown", None),
    ],
)
def test_guard_does_not_expand_to_declared_doi_or_nonbook_references(
    source_type: str, source_kind: str, doi: str | None,
):
    candidate = _candidate()
    ref = _reference(source_type=source_type, source_kind=source_kind, doi=doi)
    profile = crossref_metadata._crossref_metadata_match_profile(ref, candidate)

    assert crossref_metadata._crossref_discovered_book_edition_rejection_reason(
        ref, candidate, profile,
    ) is None


def test_chapter_candidate_is_outside_the_monograph_edition_guard():
    ref = _reference()
    candidate = _candidate(type="book-chapter")
    profile = crossref_metadata._crossref_metadata_match_profile(ref, candidate)

    assert crossref_metadata._crossref_discovered_book_edition_rejection_reason(
        ref, candidate, profile,
    ) is None
