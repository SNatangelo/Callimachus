"""Structured bibliographic fields must survive storage with their raw evidence."""
from __future__ import annotations

import copy
import json

import pytest

from core.infra.db.repository import RunRepository
from core.parse.parsing_common import _make_reference


RAW = "Novotny, L.; van Hulst, N. Antennas for light. Nature Photonics 2011, 5, 83−90."
AMBIGUOUS_RAW = (
    "Novotny, L.; van Hulst, N. Antennas for light. Nat. Photonics 2011, 5, "
    "83−90. https://doi.org/10.1038/nphoton.2010.237"
)


def _repo(path):
    return RunRepository.create(
        str(path), run_id="fields", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )


def test_reference_fields_reach_effective_resolve_projection(tmp_path):
    ref = _make_reference(7, RAW)
    evidence = copy.deepcopy(ref["bibliographic_parse"])
    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        projected = repo.effective_parse_payload()["references"][0]
        assert projected["id"] == ref["id"]
        assert projected["ref_number"] == 7
        assert projected["raw_entry"] == RAW
        assert projected["title"] == "Antennas for light"
        assert projected["year"] == 2011
        assert projected["authors"] == ["Novotny, L.", "van Hulst, N."]
        assert projected["journal"] == "Nature Photonics"
        assert projected["volume"] == "5"
        assert projected["bibliographic_parse"] == evidence
        for field in evidence["fields"].values():
            for span in field["spans"]:
                assert RAW[span["start"]:span["end"]] == span["raw_value"]
    finally:
        repo.close()
    reopened = RunRepository.open(str(tmp_path))
    try:
        assert reopened.parse_payload()["references"][0]["bibliographic_parse"] == evidence
    finally:
        reopened.close()


def test_abbreviated_venue_partial_fields_survive_effective_projection(tmp_path):
    ref = _make_reference(7, AMBIGUOUS_RAW)
    evidence = copy.deepcopy(ref["bibliographic_parse"])
    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        projected = repo.effective_parse_payload()["references"][0]
        assert projected["raw_entry"] == AMBIGUOUS_RAW
        assert projected["title"] is None
        assert projected.get("journal") is None
        assert projected["authors"] == ["Novotny, L.", "van Hulst, N."]
        assert projected["first_author"] == "Novotny"
        assert projected["year"] == 2011
        assert projected["volume"] == "5"
        assert projected["pages"] == "83−90"
        assert projected["doi"] == "10.1038/nphoton.2010.237"
        assert projected["url"] == "https://doi.org/10.1038/nphoton.2010.237"
        assert projected["bibliographic_parse"] == evidence
        assert evidence["status"] == "parsed"
        assert "title" not in evidence["fields"]
        assert "journal" not in evidence["fields"]
        assert evidence["conflicts"] == [{
            "field": "title", "reason": "ambiguous_title_journal_boundary"
        }, {
            "field": "journal", "reason": "ambiguous_title_journal_boundary"
        }]
        for field in evidence["fields"].values():
            for span in field["spans"]:
                assert AMBIGUOUS_RAW[span["start"]:span["end"]] == span["raw_value"]

        # Reading the effective DB projection validates stored raw coordinate spans.
        assert projected["cited_coordinates"]
        coordinate_kinds = {coordinate["kind"] for coordinate in projected["cited_coordinates"]}
        assert "container" not in coordinate_kinds
        assert {"volume", "article_page_range"} <= coordinate_kinds
        for coordinate in projected["cited_coordinates"]:
            raw_value = "".join(
                AMBIGUOUS_RAW[span["raw_start"]:span["raw_end"]]
                for span in coordinate["spans"]
            )
            assert raw_value == coordinate["raw_value"]
    finally:
        repo.close()

    reopened = RunRepository.open(str(tmp_path))
    try:
        persisted = reopened.effective_parse_payload()["references"][0]
        assert persisted["raw_entry"] == AMBIGUOUS_RAW
        assert persisted["title"] is None
        assert persisted.get("journal") is None
        assert persisted["year"] == 2011
        assert persisted["authors"] == ["Novotny, L.", "van Hulst, N."]
        assert persisted["volume"] == "5"
        assert persisted["pages"] == "83−90"
        assert persisted["doi"] == "10.1038/nphoton.2010.237"
        assert persisted["bibliographic_parse"] == evidence
    finally:
        reopened.close()


@pytest.mark.parametrize("mutation", ["span", "title", "year"])
def test_inconsistent_field_evidence_is_rejected_atomically(tmp_path, mutation):
    ref = _make_reference(7, RAW)
    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        before = repo.parse_payload()
        bad = copy.deepcopy(ref)
        if mutation == "span":
            bad["bibliographic_parse"]["fields"]["title"]["spans"][0]["start"] += 1
        elif mutation == "title":
            bad["title"] = "A different work"
        else:
            bad["year"] = 2012
        with pytest.raises(ValueError):
            repo.replace_parse_payload(claims=[], references=[bad], citations=[])
        assert repo.parse_payload() == before
    finally:
        repo.close()


def test_corrupted_stored_field_provenance_fails_closed(tmp_path):
    ref = _make_reference(7, RAW)
    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        bad = copy.deepcopy(ref["bibliographic_parse"])
        bad["fields"]["title"]["spans"][0]["raw_value"] = "invented"
        repo._conn.execute(
            "UPDATE reference_entries SET bibliographic_parse_json=? WHERE ref_id=?",
            (json.dumps(bad), ref["id"]),
        )
        with pytest.raises(RuntimeError, match="bibliographic Parse evidence"):
            repo.parse_payload()
    finally:
        repo.close()
