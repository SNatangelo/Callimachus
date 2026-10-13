# tests/infra/test_resolve_coordinate_comparisons.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db.repository import RunRepository
from core.parse.parsing_common import _make_reference
from core.resolve import service as resolve
from core.resolve.journal_authority import assess_local_journal, build_catalog
from core.resolve.matching import _cited_coordinate_comparisons, _metadata_match_profile
from core.resolve.providers import crossref_metadata
from tools.build_journal_authority import _marcxml_records, _nlm_records


def _ref(*, coordinates=()):
    return {
        "id": "r1",
        "raw_entry": "Smith, Jane. A cited article. Journal of Tests 12(3): 10-20 (2020).",
        "title": "A cited article",
        "year": 2020,
        "cited_coordinates": coordinates,
    }


def _crossref_record():
    return {
        "title": ["A cited article"],
        "author": [{"family": "Smith"}],
        "published": {"date-parts": [[2020]]},
        "container-title": ["Journal of Tests"],
        "volume": "12",
        "page": "10–20",
    }


def _coordinate(kind: str, value: str) -> dict:
    return {
        "kind": kind,
        "normalized_value": value,
        "raw_value": value,
        "rule_id": "test",
        "extractor_version": "test",
        "spans": [{"raw_start": 0, "raw_end": len(value)}],
    }


def _repo(tmp_path) -> RunRepository:
    repo = RunRepository.create(
        str(tmp_path), run_id="coordinate-attempt", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[], references=[{"id": "r1", "ref_number": 1, "raw_entry": "One"}], citations=[],
    )
    return repo


def _evidence(profile: dict) -> dict:
    return {
        "has_identifier": False,
        "has_searchable_title": True,
        "has_author": True,
        "has_year": True,
        "has_venue": True,
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "source_type_evidence": [],
        "indexability": "high",
        "checks_completed": [],
        "minimum_checks_completed": True,
        "best_candidate": None,
        "resolution_basis": "metadata_search",
        "existence_confidence": "high",
        "title_overlap": 1.0,
        "metadata_match": profile,
        "identifier_fallback": None,
        "synthetic_reference_risk": {"score": 0, "band": "none", "signals": []},
    }


def test_coordinate_comparisons_are_advisory_and_cover_match_mismatch_inconclusive():
    msg = _crossref_record()
    baseline = _metadata_match_profile(_ref(), msg, msg["title"][0])
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("container", "journal   of tests"),
        _coordinate("volume", "13"),
        _coordinate("issue", "3"),
        _coordinate("article_page_range", "10-20"),
    )), msg, msg["title"][0])

    assert {key: value for key, value in profile.items() if key != "coordinate_comparisons"} == baseline
    assert profile["coordinate_comparisons"] == [
        {"kind": "container", "cited_value": "journal   of tests", "matched_value": "Journal of Tests", "status": "match"},
        {"kind": "volume", "cited_value": "13", "matched_value": "12", "status": "mismatch"},
        {"kind": "issue", "cited_value": "3", "matched_value": None, "status": "inconclusive"},
        {"kind": "article_page_range", "cited_value": "10-20", "matched_value": "10–20", "status": "match"},
    ]


def test_all_missing_provider_coordinates_are_recorded_as_inconclusive():
    msg = _crossref_record()
    for key in ("container-title", "volume", "page"):
        del msg[key]
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("chapter_page_range", "10-20"),
        _coordinate("elocator", "e123"),
    )), msg, msg["title"][0])
    assert profile["coordinate_comparisons"] == [
        {"kind": "chapter_page_range", "cited_value": "10-20", "matched_value": None,
         "status": "inconclusive"},
        {"kind": "elocator", "cited_value": "e123", "matched_value": None,
         "status": "inconclusive"},
    ]


def test_provider_singleton_start_page_does_not_contradict_cited_page_range():
    msg = _crossref_record()
    msg["page"] = "1052"
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("article_page_range", "1052-1060"),
    )), msg, msg["title"][0])

    assert profile["coordinate_comparisons"] == [{
        "kind": "article_page_range",
        "cited_value": "1052-1060",
        "matched_value": "1052",
        "status": "inconclusive",
    }]


def test_safe_format_variants_match_and_ambiguous_venue_forms_stay_inconclusive():
    msg = _crossref_record()
    msg["container-title"] = ["The Cognition &amp; Emotion"]
    msg["page"] = "1509-1574"
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("container", "Cognition & Emotion"),
        _coordinate("article_page_range", "1509-74"),
    )), msg, msg["title"][0])
    assert [item["status"] for item in profile["coordinate_comparisons"]] == [
        "match", "match",
    ]

    msg["container-title"] = ["Journal of Experimental Psychology"]
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("container", "J. Exp. Psychol."),
    )), msg, msg["title"][0])
    assert profile["coordinate_comparisons"] == [{
        "kind": "container",
        "cited_value": "J. Exp. Psychol.",
        "matched_value": "Journal of Experimental Psychology",
        "status": "inconclusive",
    }]

    msg["container-title"] = ["British Medical Journal"]
    profile = _metadata_match_profile(_ref(coordinates=(
        _coordinate("container", "BMJ"),
    )), msg, msg["title"][0])
    assert profile["coordinate_comparisons"][0]["status"] == "inconclusive"


def test_direct_crossref_doi_retains_coordinate_comparisons(monkeypatch):
    msg = _crossref_record()
    msg["issue"] = "3"
    msg["DOI"] = "10.1000/example"
    monkeypatch.setattr(
        resolve,
        "_get",
        lambda *_args, **_kwargs: (200, __import__("json").dumps({"message": msg})),
    )
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    result = crossref_metadata.resolve_doi(
        "10.1000/example",
        ref=_ref(coordinates=(
            _coordinate("container", "Journal of Tests"),
            _coordinate("volume", "12"),
            _coordinate("issue", "3"),
            _coordinate("article_page_range", "10-20"),
        )),
    )
    assert [
        item["status"] for item in result["metadata_match"]["coordinate_comparisons"]
    ] == ["match", "match", "match", "match"]


def test_local_relational_catalog_recognizes_alias_and_rejects_collision(tmp_path):
    catalog = tmp_path / "journal-authority.sqlite"
    collision_catalog = tmp_path / "journal-authority-collision.sqlite"
    build_catalog(
        catalog,
        [{
            "record_id": "nlm:0001", "canonical_title": "British Medical Journal",
            "aliases": ["BMJ", "Br Med J"], "issns": ["0959-8138"],
        }],
        registry="nlm", registry_version="2026-09-12",
    )
    authority = assess_local_journal(
        _ref(coordinates=(_coordinate("container", "BMJ"),)), catalog,
    )
    assert authority["status"] == "recognized"
    assert authority["record_id"] == "nlm:0001"
    assert authority["match_basis"] == "registered_alias"
    assert len(authority["snapshot_sha256"]) == 64

    build_catalog(
        collision_catalog,
        [
            {
                "record_id": "one", "canonical_title": "First Journal",
                "aliases": ["FJ"], "issns": ["1234-5679"],
            },
            {
                "record_id": "two", "canonical_title": "Further Journal",
                "aliases": ["FJ"], "issns": ["2049-3630"],
            },
        ],
        registry="fixture", registry_version="2",
    )
    assert assess_local_journal(
        _ref(coordinates=(_coordinate("container", "FJ"),)), collision_catalog,
    ) is None


def test_local_catalog_does_not_infer_initialisms(tmp_path):
    catalog = tmp_path / "journal-authority.sqlite"
    build_catalog(
        catalog,
        [{
            "record_id": "nlm:0001", "canonical_title": "British Medical Journal",
            "aliases": [], "issns": ["0959-8138"],
        }],
        registry="nlm", registry_version="2026-09-12",
    )
    assert assess_local_journal(
        _ref(coordinates=(_coordinate("container", "BMJ"),)), catalog,
    ) is None


def test_local_catalog_rejects_duplicate_ids_without_explicit_grouping(tmp_path):
    records = [
        {
            "record_id": "duplicate", "canonical_title": "First Journal",
            "aliases": [], "issns": ["1234-5679"],
        },
        {
            "record_id": "duplicate", "canonical_title": "Second Journal",
            "aliases": [], "issns": ["2049-3630"],
        },
    ]
    with pytest.raises(ValueError, match="record ids must be unique"):
        build_catalog(
            tmp_path / "duplicate.sqlite", records,
            registry="fixture", registry_version="1",
        )


def test_local_catalog_keeps_non_latin_names_distinct_and_rejects_bad_issn(tmp_path):
    catalog = tmp_path / "journal-authority.sqlite"
    build_catalog(
        catalog,
        [
            {
                "record_id": "one", "canonical_title": "物理学报",
                "aliases": [], "issns": ["1234-5679"],
            },
            {
                "record_id": "two", "canonical_title": "化学学报",
                "aliases": [], "issns": ["2049-3630"],
            },
        ],
        registry="fixture", registry_version="1",
    )
    authority = assess_local_journal(
        _ref(coordinates=(_coordinate("container", "物理学报"),)), catalog,
    )
    assert authority["record_id"] == "one"

    with pytest.raises(ValueError, match="no valid ISSN"):
        build_catalog(
            tmp_path / "bad.sqlite",
            [{
                "record_id": "bad", "canonical_title": "Bad Journal",
                "aliases": [], "issns": ["X959-8138"],
            }],
            registry="fixture", registry_version="1",
        )


def test_local_catalog_hash_covers_registry_provenance(tmp_path):
    catalog = tmp_path / "journal-authority.sqlite"
    build_catalog(
        catalog,
        [{
            "record_id": "one", "canonical_title": "Journal of Tests",
            "aliases": ["J Tests"], "issns": ["1234-5679"],
        }],
        registry="fixture", registry_version="1",
    )
    with sqlite3.connect(catalog) as conn:
        conn.execute("UPDATE snapshot SET registry_version='tampered'")
    with pytest.raises(RuntimeError, match="snapshot hash"):
        assess_local_journal(
            _ref(coordinates=(_coordinate("container", "J Tests"),)), catalog,
        )


def test_nlm_import_preserves_registered_names_abbreviations_and_issns():
    records = _nlm_records("""
JournalTitle: British Medical Journal
MedAbbr: Br Med J
IsoAbbr: BMJ
ISSN (Print): 0959-8138
NlmId: 0001
--------------------------------------------------------
""")
    assert records == [{
        "record_id": "nlm:0001",
        "canonical_title": "British Medical Journal",
        "aliases": ["Br Med J", "BMJ"],
        "issns": ["0959-8138"],
    }]


def test_issn_marcxml_import_groups_manifestations_by_issnl(tmp_path):
    source = tmp_path / "issn-register.xml"
    source.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
  <record>
    <controlfield tag="001">print-record</controlfield>
    <datafield tag="022" ind1=" " ind2=" ">
      <subfield code="a">1234-5679</subfield>
    </datafield>
    <datafield tag="023" ind1="1" ind2=" ">
      <subfield code="a">2049-3630</subfield>
    </datafield>
    <datafield tag="023" ind1="0" ind2=" ">
      <subfield code="a">0959-8138</subfield>
    </datafield>
    <datafield tag="222" ind1=" " ind2=" ">
      <subfield code="a">Journal of Tests</subfield>
    </datafield>
    <datafield tag="210" ind1=" " ind2=" ">
      <subfield code="a">J. Tests</subfield>
    </datafield>
    <datafield tag="245" ind1="0" ind2="0">
      <subfield code="a">Journal of Tests :</subfield>
      <subfield code="b">print edition</subfield>
    </datafield>
  </record>
  <record>
    <controlfield tag="001">online-record</controlfield>
    <datafield tag="022" ind1=" " ind2=" ">
      <subfield code="a">2049-3630</subfield>
      <subfield code="l">0959-8138</subfield>
    </datafield>
    <datafield tag="222" ind1=" " ind2=" ">
      <subfield code="a">Journal of Tests</subfield>
    </datafield>
    <datafield tag="246" ind1=" " ind2=" ">
      <subfield code="a">Tests journal</subfield>
    </datafield>
  </record>
</collection>
""", encoding="utf-8")
    records = list(_marcxml_records(source))
    assert [record["record_id"] for record in records] == [
        "issnl:0959-8138", "issnl:0959-8138",
    ]

    catalog = tmp_path / "journal-authority.sqlite"
    reverse_catalog = tmp_path / "journal-authority-reverse.sqlite"
    for path, ordered in ((catalog, records), (reverse_catalog, reversed(records))):
        build_catalog(
            path, ordered, registry="issn-register", registry_version="2026-09",
            merge_duplicate_ids=True, created_at="2026-09-14T00:00:00Z",
        )

    authority = assess_local_journal(
        _ref(coordinates=(_coordinate("container", "J Tests"),)), catalog,
    )
    assert authority["record_id"] == "issnl:0959-8138"
    assert authority["canonical_title"] == "Journal of Tests"
    with sqlite3.connect(catalog) as conn:
        assert conn.execute("SELECT record_count FROM snapshot").fetchone()[0] == 1
        assert conn.execute(
            "SELECT issn FROM journal_issns ORDER BY issn"
        ).fetchall() == [("1234-5679",), ("2049-3630",)]
        first_hash = conn.execute(
            "SELECT snapshot_sha256 FROM snapshot"
        ).fetchone()[0]
    with sqlite3.connect(reverse_catalog) as conn:
        assert conn.execute(
            "SELECT snapshot_sha256 FROM snapshot"
        ).fetchone()[0] == first_hash


def test_issn_marcxml_import_rejects_conflicting_issnl(tmp_path):
    source = tmp_path / "conflicting-issnl.xml"
    source.write_text("""<record xmlns="http://www.loc.gov/MARC21/slim">
  <datafield tag="022"><subfield code="a">1234-5679</subfield></datafield>
  <datafield tag="023" ind1="0"><subfield code="a">0959-8138</subfield></datafield>
  <datafield tag="023" ind1="0"><subfield code="a">2049-3630</subfield></datafield>
  <datafield tag="222"><subfield code="a">Journal of Tests</subfield></datafield>
</record>""", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting ISSN-L"):
        list(_marcxml_records(source))


def test_local_authority_does_not_turn_retrieval_absence_into_fabrication_and_round_trips(
    tmp_path, monkeypatch,
):
    catalog = tmp_path / "journal-authority.sqlite"
    build_catalog(
        catalog,
        [{
            "record_id": "nlm:0001", "canonical_title": "British Medical Journal",
            "aliases": ["BMJ"], "issns": ["0959-8138"],
        }],
        registry="nlm", registry_version="2026-09-12",
    )
    monkeypatch.setenv("CALLIMACHUS_JOURNAL_AUTHORITY_DB", str(catalog))
    ref = {
        **_ref(coordinates=(_coordinate("container", "BMJ"),)),
        "source_kind": "article_like", "source_type_confidence": "high",
        "indexability": "high",
    }
    result = {
        "status": "unverified", "via": "crossref_metadata",
        "reason": "no metadata candidate on Crossref",
        "resolution_basis": "metadata_search", "existence_confidence": "low",
    }
    evidence = resolve._reference_evidence_profile(
        ref, result, "unverified", [result], None,
        {"resolution_basis": "metadata_search", "existence_confidence": "low"},
    )
    tag, risk, _reason = resolve._reference_status_tag(
        ref, "unverified", evidence, result,
    )
    assert (tag, risk) == ("unverified", "unknown")
    assert "recognized_venue_unlocated_work" not in evidence[
        "synthetic_reference_risk"
    ]["signals"]

    repo = _repo(tmp_path / "run")
    try:
        repo.upsert_resolve_result("r1", {
            "status": "unverified", "via": "crossref_metadata",
            "attempts": [result], "evidence_profile": evidence,
        })
        stored = repo.get_resolve_result("r1").evidence_profile
        assert stored["journal_authority"] == evidence["journal_authority"]
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM resolve_evidence_journal_authorities"
        ).fetchone()[0] == 1
        invalid = {
            **evidence,
            "journal_authority": {
                **evidence["journal_authority"], "snapshot_sha256": None,
            },
        }
        with pytest.raises(ValueError, match="snapshot hash"):
            repo.upsert_resolve_result("r1", {
                "status": "unverified", "via": "crossref_metadata",
                "attempts": [result], "evidence_profile": invalid,
            })
    finally:
        repo.close()


def test_profile_without_coordinates_is_unchanged():
    msg = _crossref_record()
    assert _metadata_match_profile(_ref(), msg, msg["title"][0]) == {
        "score": 0.85,
        "title_overlap": 1.0,
        "author_match": True,
        "cited_first_author": "smith",
        "matched_first_author": "smith",
        "year_match": True,
        "matched_year": 2020,
        "venue_overlap": None,
        "matched_venue": "Journal of Tests",
    }


def test_coordinate_comparisons_round_trip_through_typed_attempt_storage(tmp_path):
    repo = _repo(tmp_path)
    try:
        msg = _crossref_record()
        msg["ISSN"] = ["1234-5679"]
        profile = _metadata_match_profile(_ref(coordinates=(
            _coordinate("container", "J. Tests"),
            _coordinate("volume", "13"),
            _coordinate("issue", "3"),
        )), msg, msg["title"][0])
        payload = {
            "status": "resolved", "via": "crossref",
            "attempts": [{"status": "resolved", "via": "crossref", "metadata_match": profile}],
            "evidence_profile": _evidence(profile),
        }
        repo.upsert_resolve_result("r1", payload)
        result = repo.get_resolve_result("r1")
        assert result.attempts == payload["attempts"]
        assert result.evidence_profile["metadata_match"] == profile
        for table in (
            "resolve_attempt_metadata_coordinate_comparisons",
            "resolve_evidence_metadata_coordinate_comparisons",
        ):
            assert not any(
                "json" in row["name"].casefold()
                for row in repo._conn.execute(f'PRAGMA table_info("{table}")')
            )
    finally:
        repo.close()


def test_neutral_article_locator_range_overlap_is_inconclusive_but_distinct_start_mismatches():
    ref = _ref(coordinates=(_coordinate("article_locator", "814"),))
    exact = _metadata_match_profile(ref, {"page": "814"}, "Target article")
    ranged = _metadata_match_profile(ref, {"page": "814-820"}, "Target article")
    distinct_start = _metadata_match_profile(ref, {"page": "815-820"}, "Target article")
    assert exact["coordinate_comparisons"] == [{
        "kind": "article_locator", "cited_value": "814",
        "matched_value": "814", "status": "match",
    }]
    assert ranged["coordinate_comparisons"] == [{
        "kind": "article_locator", "cited_value": "814",
        "matched_value": "814-820", "status": "inconclusive",
    }]
    assert distinct_start["coordinate_comparisons"] == [{
        "kind": "article_locator", "cited_value": "814",
        "matched_value": "815-820", "status": "mismatch",
    }]


@pytest.mark.parametrize(
    ("raw", "expected_coordinates", "metadata", "bad_metadata", "mismatch_kind"),
    [
        pytest.param(
            "Sample, A. Citation coordinate forms. Journal of Tests 2023 "
            "613:7942 613, 138–144 (2023).",
            {"volume": "613", "issue": "7942", "article_page_range": "138-144"},
            {"volume": "613", "issue": "7942", "page": "138–144"},
            {"volume": "613", "issue": "7942", "page": "138-145"},
            "article_page_range",
            id="repeated-volume-issue-7942",
        ),
        pytest.param(
            "Sample, A. Citation coordinate forms. Journal of Tests 2016 "
            "534:7609 534, 684–687 (2016).",
            {"volume": "534", "issue": "7609", "article_page_range": "684-687"},
            {"volume": "534", "issue": "7609", "page": "684–687"},
            {"volume": "534", "issue": "7609", "page": "684-688"},
            "article_page_range",
            id="repeated-volume-issue-7609",
        ),
        pytest.param(
            "Sample, A. Citation coordinate forms. Journal of Tests 2024 "
            "18:5241 18, 251–259 (2024).",
            {"volume": "18", "issue": "5241", "article_page_range": "251-259"},
            {"volume": "18", "issue": "5241", "page": "251-259"},
            {"volume": "18", "issue": "5241", "page": "251-260"},
            "article_page_range",
            id="repeated-volume-synthetic-issue",
        ),
        pytest.param(
            "Sample, A. Electronic pages. Journal of Tests 3, e895–e896 (2022).",
            {"article_locator": "e895-e896"},
            {"article-number": "e895-e896"},
            {"article-number": "e895-e897"},
            "article_locator",
            id="prefixed-e-range-e895",
        ),
        pytest.param(
            "Sample, A. Electronic pages. Journal of Tests 4, e761–e762 (2023).",
            {"article_locator": "e761-e762"},
            {"article-number": "e761-e762"},
            {"article-number": "e761-e763"},
            "article_locator",
            id="prefixed-e-range-e761",
        ),
        pytest.param(
            "Sample, A. Electronic pages. Journal of Tests 5, D81–D84 (2024).",
            {"article_locator": "D81-D84"},
            {"article-number": "D81-D84"},
            {"article-number": "D81-D85"},
            "article_locator",
            id="prefixed-d-range",
        ),
        pytest.param(
            "Sample, A. Electronic pages. (2022)3(1) Journal of Tests e895–e896.",
            {"volume": "3", "issue": "1", "article_locator": "e895-e896"},
            {"volume": "3", "issue": "1", "article-number": "e895-e896"},
            {"volume": "3", "issue": "1", "article-number": "e895-e897"},
            "article_locator",
            id="post-year-volume-issue-prefixed-range",
        ),
        pytest.param(
            "Sample, A. Electronic pages. (2022)3 Journal of Tests e895–e896.",
            {"volume": "3", "article_locator": "e895-e896"},
            {"volume": "3", "article-number": "e895-e896"},
            {"volume": "3", "article-number": "e895-e897"},
            "article_locator",
            id="post-year-volume-prefixed-range",
        ),
        pytest.param(
            "Sample, A. Electronic pages. Journal of Tests 3, e895 – e896 (2022).",
            {"article_locator": "e895 - e896"},
            {"article-number": "e895-e896"},
            {"article-number": "e895-e897"},
            "article_locator",
            id="spaced-prefixed-range-matches-compact-provider",
        ),
        pytest.param(
            "Sample, A. A single page. Journal of Tests 12:37 (2020).",
            {"article_locator": "37"},
            {"page": "37"},
            {"page": "38"},
            "article_locator",
            id="real-volume-page",
        ),
        pytest.param(
            "Sample, A. A numeric identifier. Journal of Tests 15, 785132 (2019).",
            {"article_number": "785132"},
            {"article-number": "785132"},
            {"article-number": "785133"},
            "article_number",
            id="numeric-article-number",
        ),
    ],
)
def test_production_parsed_coordinates_survive_storage_and_compare_complete_values(
    tmp_path, raw, expected_coordinates, metadata, bad_metadata, mismatch_kind,
):
    reference = _make_reference(1, raw)
    reference["id"] = "r1"
    writer = _repo(tmp_path)
    try:
        writer.replace_parse_payload(claims=[], references=[reference], citations=[])
    finally:
        writer.close()

    reader = RunRepository.open_readonly(str(tmp_path))
    try:
        stored = reader.get_reference("r1")
        assert stored is not None
        by_kind = {item["kind"]: item for item in stored.cited_coordinates}
        for kind, expected in expected_coordinates.items():
            coordinate = by_kind[kind]
            assert coordinate["normalized_value"] == expected
            assert len(coordinate["spans"]) == 1
            span = coordinate["spans"][0]
            literal = stored.raw_entry[span["raw_start"]:span["raw_end"]]
            assert literal == coordinate["raw_value"]
        if "issue" in expected_coordinates:
            issue_spans = by_kind["issue"]["spans"]
            for locator_kind in (
                "article_page_range", "article_locator", "article_number", "elocator",
            ):
                for locator_span in by_kind.get(locator_kind, {}).get("spans", []):
                    assert all(
                        issue_span["raw_end"] <= locator_span["raw_start"]
                        or locator_span["raw_end"] <= issue_span["raw_start"]
                        for issue_span in issue_spans
                    )

        comparisons = _cited_coordinate_comparisons(
            {"cited_coordinates": stored.cited_coordinates}, metadata,
        )
        statuses = {item["kind"]: item["status"] for item in comparisons}
        assert {kind: statuses[kind] for kind in expected_coordinates} == {
            kind: "match" for kind in expected_coordinates
        }

        wrong_range = _cited_coordinate_comparisons(
            {"cited_coordinates": stored.cited_coordinates}, bad_metadata,
        )
        assert next(
            item for item in wrong_range if item["kind"] == mismatch_kind
        )["status"] == "mismatch"
    finally:
        reader.close()


@pytest.mark.parametrize("table", [
    "resolve_attempt_metadata_coordinate_comparisons",
    "resolve_evidence_metadata_coordinate_comparisons",
])
def test_coordinate_comparison_reader_fails_closed_on_missing_child(tmp_path, table):
    repo = _repo(tmp_path)
    try:
        msg = _crossref_record()
        profile = _metadata_match_profile(_ref(coordinates=(
            _coordinate("container", "Journal of Tests"),
        )), msg, msg["title"][0])
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "via": "crossref",
            "attempts": [{"status": "resolved", "via": "crossref", "metadata_match": profile}],
            "evidence_profile": _evidence(profile),
        })
        repo._conn.execute(f"DELETE FROM {table}")
        with pytest.raises(RuntimeError, match="coordinate comparison count"):
            repo.get_resolve_result("r1")
    finally:
        repo.close()
