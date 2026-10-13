from __future__ import annotations

from core.parse import reference_readers as readers
from core.parse.reference_readers import apa_journal, semicolon_journal
from core.parse.parsing_common import _make_reference


def _assert_literal_field(raw: str, parsed: dict, name: str) -> None:
    item = parsed["fields"][name]
    span = item["spans"][0]
    assert raw[span["start"]:span["end"]] == item["value"]
    assert span["raw_value"] == item["value"]


def _assert_partial_boundary_parse(raw: str, parsed: dict, reader: str,
                                   expected_fields: dict) -> None:
    assert parsed["reader"] == reader
    assert parsed["status"] == "parsed"
    assert parsed["conflicts"] == [
        {"field": "title", "reason": "ambiguous_title_journal_boundary"},
        {"field": "journal", "reason": "ambiguous_title_journal_boundary"},
    ]
    assert "title" not in parsed["fields"]
    assert "journal" not in parsed["fields"]
    for name, expected in expected_fields.items():
        assert parsed["fields"][name]["value"] == expected
    for item in parsed["fields"].values():
        for span in item["spans"]:
            assert raw[span["start"]:span["end"]] == span["raw_value"]
    assert readers.validate_parse(parsed, raw)


def test_apa_compound_title_retains_independent_fields_when_boundary_is_ambiguous():
    raw = ("Doe, J. (2020). A short title. A second sentence. "
           "Journal of Tests, 5(1), 12-34.")

    parsed = readers.parse_entry(raw)

    _assert_partial_boundary_parse(raw, parsed, "apa-journal", {
        "authors": ["Doe, J."], "first_author": "Doe", "year": 2020,
        "volume": "5", "issue": "1", "pages": "12-34",
    })


def test_semicolon_compound_title_retains_independent_fields_without_false_journal():
    raw = ("Doe, J.; Roe, A. A short title. A second sentence. "
           "Nature 2012, 6, 737-748.")

    parsed = readers.parse_entry(raw, include_generic=True)

    _assert_partial_boundary_parse(raw, parsed, "semicolon-journal", {
        "authors": ["Doe, J.", "Roe, A."], "first_author": "Doe",
        "year": 2012, "volume": "6", "pages": "737-748",
    })


def test_short_one_word_title_sentences_are_not_silently_absorbed():
    cases = (
        ("apa-journal",
         "Doe, J. (2020). A short title. Yes. Journal of Tests, 5(1), 12-34.",
         {"authors": ["Doe, J."], "first_author": "Doe", "year": 2020,
          "volume": "5", "issue": "1", "pages": "12-34"}),
        ("semicolon-journal",
         "Doe, J.; Roe, A. A short title. Now. Nature 2012, 6, 737-748.",
         {"authors": ["Doe, J.", "Roe, A."], "first_author": "Doe",
          "year": 2012, "volume": "6", "pages": "737-748"}),
    )

    for reader, raw, expected_fields in cases:
        parsed = readers.parse_entry(raw, include_generic=True)

        _assert_partial_boundary_parse(raw, parsed, reader, expected_fields)


def test_partial_apa_and_semicolon_references_keep_coordinates_without_container():
    cases = (
        ("Doe, J. (2020). A short title. Yes. Journal of Tests, 5(1), 12-34.",
         ["Doe, J."], "Doe", 2020, "5", "12-34", "1"),
        ("Doe, J.; Roe, A. A short title. Now. Nature 2012, 6, 737-748. "
         "https://doi.org/10.1038/nphoton.2010.237",
         ["Doe, J.", "Roe, A."], "Doe", 2012, "6", "737-748", None),
    )

    for raw, authors, first_author, year, volume, pages, issue in cases:
        ref = _make_reference(7, raw)

        assert ref["title"] is None
        assert ref.get("journal") is None
        assert ref["authors"] == authors
        assert ref["first_author"] == first_author
        assert ref["year"] == year
        assert ref["volume"] == volume
        assert ref["pages"] == pages
        assert ref.get("issue") == issue
        evidence = ref["bibliographic_parse"]
        assert evidence["status"] == "parsed"
        assert evidence["conflicts"] == [
            {"field": "title", "reason": "ambiguous_title_journal_boundary"},
            {"field": "journal", "reason": "ambiguous_title_journal_boundary"},
        ]
        assert readers.validate_parse(evidence, raw)

        coordinates = ref["cited_coordinates"]
        kinds = {coordinate["kind"] for coordinate in coordinates}
        assert "container" not in kinds
        assert {"volume", "article_page_range"} <= kinds
        for coordinate in coordinates:
            raw_value = "".join(
                raw[span["raw_start"]:span["raw_end"]]
                for span in coordinate["spans"]
            )
            assert raw_value == coordinate["raw_value"]
        if "doi.org" in raw:
            assert ref["doi"] == "10.1038/nphoton.2010.237"
            assert ref["url"] == "https://doi.org/10.1038/nphoton.2010.237"


def test_ordinary_apa_title_and_journal_keep_literal_spans():
    raw = "Smith, J. (2020). A clear title. Journal of Tests, 5(1), 12-34."

    parsed = apa_journal.parse_fields(raw)

    assert parsed["status"] == "parsed"
    assert parsed["fields"]["title"]["value"] == "A clear title"
    assert parsed["fields"]["journal"]["value"] == "Journal of Tests"
    _assert_literal_field(raw, parsed, "title")
    _assert_literal_field(raw, parsed, "journal")


def test_abbreviated_semicolon_journal_with_competing_cuts_keeps_other_fields():
    raw = ("Kauranen, M.; Zayats, A. V. Nonlinear plasmonics. "
           "Nat. Photonics 2012, 6, 737−748.")

    parsed = readers.parse_entry(raw, include_generic=True)

    _assert_partial_boundary_parse(raw, parsed, "semicolon-journal", {
        "authors": ["Kauranen, M.", "Zayats, A. V."],
        "first_author": "Kauranen", "year": 2012, "volume": "6",
        "pages": "737−748",
    })


def test_apa_abbreviated_journal_with_competing_cuts_keeps_other_fields():
    raw = "Doe, J. (2020). A clear title. J. Clin. Oncol., 5(1), 12-34."

    parsed = readers.parse_entry(raw, include_generic=True)

    _assert_partial_boundary_parse(raw, parsed, "apa-journal", {
        "authors": ["Doe, J."], "first_author": "Doe", "year": 2020,
        "volume": "5", "issue": "1", "pages": "12-34",
    })


def test_multiple_initial_authors_remain_supported_with_unabbreviated_journal():
    raw = ("Kauranen, M.; Zayats, A. V. Nonlinear plasmonics. "
           "Nature Photonics 2012, 6, 737−748.")

    parsed = semicolon_journal.parse_fields(raw)

    assert parsed["status"] == "parsed"
    assert parsed["fields"]["authors"]["value"] == [
        "Kauranen, M.", "Zayats, A. V."
    ]
    assert parsed["fields"]["title"]["value"] == "Nonlinear plasmonics"
    assert parsed["fields"]["journal"]["value"] == "Nature Photonics"
    _assert_literal_field(raw, parsed, "title")
    _assert_literal_field(raw, parsed, "journal")
