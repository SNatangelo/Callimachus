"""Regressions for label-preserving structural parsing and reader controls."""

from __future__ import annotations

from contextlib import contextmanager
import unicodedata
import unittest
from unittest import mock

from core.infra.db.repository import RunRepository
from core.parse import footnotes
from core.parse import parser_config
from core.parse import parsing_common as pc
from core.parse import reference_readers as rr


_BODY = (
    "Chan AW, Altman DG (2005) Epidemiology and reporting of cafe\u0301 trials. "
    "Lancet 365: 1159."
)
_GENERIC_RAW = "A 2020 Perspective on Metadata. No closed publication date."


@contextmanager
def _generic_reader_enabled(enabled: bool):
    config = parser_config.load()
    config["reference_readers"]["generic-text"]["enabled"] = enabled
    with mock.patch.object(rr.parser_config, "load", return_value=config):
        rr.reset_for_tests()
        try:
            yield
        finally:
            rr.reset_for_tests()


class TestReferenceParserControls(unittest.TestCase):
    def _assert_full_entry_spans(self, reference: dict) -> None:
        raw = reference["raw_entry"]
        parsed = reference["bibliographic_parse"]
        self.assertTrue(rr.validate_parse(parsed, raw))
        for item in parsed["fields"].values():
            for span in item["spans"]:
                self.assertEqual(raw[span["start"]:span["end"]], span["raw_value"])

    def test_scoped_and_opaque_labels_preserve_full_entry_structural_proofs(self):
        nfc_body = unicodedata.normalize("NFC", _BODY)
        direct = pc._make_reference(1, _BODY)

        scoped = pc.default_parse_references(
            f"[1] {_BODY}", meta={"_preserve_numbered_entry_labels": True},
        )[0]
        opaque_text = "\n".join((
            f"[SM20] {nfc_body}",
            "[SM21] Smith JB (2020). Another clear title. "
            "Journal of Tests, 5(1), 12-34.",
        ))
        opaque = pc.default_parse_references(opaque_text)[0]

        for reference in (direct, scoped, opaque):
            with self.subTest(raw_entry=reference["raw_entry"][:12]):
                self.assertEqual(reference["bibliographic_parse"]["status"], "parsed")
                self.assertEqual(reference["bibliographic_parse"]["reader"], "vancouver-fields")
                field_values = {
                    name: item["value"]
                    for name, item in reference["bibliographic_parse"]["fields"].items()
                }
                direct_values = {
                    name: item["value"]
                    for name, item in direct["bibliographic_parse"]["fields"].items()
                }
                self.assertEqual(field_values, direct_values)
                self._assert_full_entry_spans(reference)

        self.assertEqual(scoped["raw_entry"], f"[1] {nfc_body}")
        self.assertEqual(opaque["raw_entry"], f"[SM20] {nfc_body}")

    def test_scoped_multiple_edition_ambiguity_keeps_label_and_withholds_year(self):
        body = (
            "John Doe, Jane Roe, A Collaborative Work (2d ed. 2021); "
            "revised (3d ed. 2023)."
        )
        reference = pc.default_parse_references(
            f"[1] {body}", meta={"_preserve_numbered_entry_labels": True},
        )[0]

        self.assertEqual(reference["raw_entry"], f"[1] {body}")
        self.assertEqual(reference["bibliographic_parse"]["status"], "ambiguous")
        self.assertEqual(reference["bibliographic_parse"]["fields"], {})
        self.assertIsNone(reference["title"])
        self.assertIsNone(reference["year"])

    def test_explicit_analysis_body_must_be_an_exact_nfc_raw_suffix(self):
        with self.assertRaisesRegex(ValueError, "_analysis_raw must be an exact NFC suffix"):
            pc._make_reference(1, "[SM20] body with trailing material", _analysis_raw="body")
        with self.assertRaisesRegex(ValueError, "_analysis_raw must be a non-empty body"):
            pc._make_reference(1, "[SM20] body", _analysis_raw="")

    def test_invalid_translated_proof_fails_without_rereading_labeled_entry(self):
        parsed = {
            "reader": "test-reader", "version": "1", "status": "parsed",
            "fields": {
                "title": {
                    "value": "body", "spans": [{
                        "start": 0, "end": 4, "raw_value": "body",
                    }], "rule": "test-reader",
                },
            },
            "conflicts": [],
        }
        with (mock.patch.object(rr, "parse_entry", return_value=parsed) as parse_entry,
              mock.patch.object(rr, "validate_parse", return_value=False)):
            with self.assertRaisesRegex(
                ValueError, "spans do not validate against the preserved raw entry",
            ):
                pc._make_reference(1, "[1] body", _analysis_raw="body")

        parse_entry.assert_called_once_with("body", chicago_hanging=False)

    def test_disabling_generic_text_stops_unrecognized_title_and_year_fallbacks(self):
        with _generic_reader_enabled(False):
            reference = pc._make_reference(1, _GENERIC_RAW)
            footnote = "A 2020 Perspective on Metadata (2020)."

            self.assertEqual(reference["bibliographic_parse"]["status"], "unrecognized")
            self.assertIsNone(pc._extract_title(_GENERIC_RAW))
            self.assertIsNone(reference["title"])
            self.assertIsNone(reference["year"])
            self.assertIsNone(footnotes._note_title(footnote, pc))
            self.assertIsNone(footnotes._note_year(footnote))
            generic_quoted_title = 'See "A Quoted Generic Title" (2020).'
            self.assertEqual(
                rr.parse_entry(generic_quoted_title, include_generic=False)["status"],
                "unrecognized",
            )
            self.assertIsNone(footnotes._note_title(generic_quoted_title, pc))

            structural = pc._make_reference(
                2,
                "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised "
                "trials. Lancet 365: 1159.",
            )
            self.assertEqual(structural["bibliographic_parse"]["status"], "parsed")
            self.assertEqual(structural["bibliographic_parse"]["reader"], "vancouver-fields")
            self.assertEqual(structural["year"], 2005)

    def test_enabled_generic_projection_retains_existing_title_and_year_behavior(self):
        with _generic_reader_enabled(True):
            reference = pc._make_reference(1, _GENERIC_RAW)

        self.assertEqual(reference["bibliographic_parse"]["reader"], "generic-text")
        self.assertEqual(reference["title"], "No closed publication date")
        self.assertEqual(reference["year"], 2020)

    def test_other_generic_extension_does_not_reenable_generic_text_fallbacks(self):
        extension = mock.Mock(
            NAME="other-generic", STRUCTURAL=False, GENERIC=True,
        )
        extension.parse_fields.return_value = None
        with mock.patch.object(rr, "get_registry", return_value=[extension]):
            reference = pc._make_reference(1, _GENERIC_RAW)

        self.assertEqual(reference["bibliographic_parse"]["status"], "unrecognized")
        self.assertIsNone(reference["title"])
        self.assertIsNone(reference["year"])

    def test_parsed_structural_fields_retain_withheld_title_conflict(self):
        raw = "Example Author (2020) A title. Journal 4: 12."
        year_start = raw.index("2020")
        conflict = {
            "field": "title", "reason": "ambiguous_title_journal_boundary",
        }
        structural = mock.Mock(
            NAME="partial-structural", VERSION="1", STRUCTURAL=True,
        )
        structural.parse_fields.return_value = {
            "reader": "partial-structural", "version": "1", "status": "parsed",
            "fields": {
                "year": {
                    "value": 2020,
                    "spans": [{
                        "start": year_start, "end": year_start + 4,
                        "raw_value": "2020",
                    }],
                    "rule": "partial-structural",
                },
            },
            "conflicts": [conflict],
        }
        generic = mock.Mock(
            NAME="generic-text", VERSION="1", STRUCTURAL=False, GENERIC=True,
        )

        with mock.patch.object(rr, "get_registry", return_value=[structural, generic]):
            parsed = rr.parse_entry(raw)

        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["fields"]["year"]["value"], 2020)
        self.assertTrue(rr.validate_parse(parsed, raw))
        self.assertEqual(parsed["conflicts"], [conflict])
        generic.parse_fields.assert_not_called()


def test_scoped_field_proofs_survive_parse_storage_and_effective_projection(tmp_path):
    reference = pc.default_parse_references(
        f"[1] {_BODY}", meta={"_preserve_numbered_entry_labels": True},
    )[0]
    repo = RunRepository.create(
        str(tmp_path), run_id="scoped-parser-controls", input_path="input.pdf",
        input_sha256="input-sha", accuracy="standard", style=None,
        model_id=None, http_profile=None, challenge_mode="off",
        fixture_fingerprint="scoped-parser-controls",
    )
    try:
        repo.replace_parse_payload(claims=[], references=[reference], citations=[])
        projected = repo.effective_parse_payload()["references"][0]

        assert projected["raw_entry"] == reference["raw_entry"]
        assert projected["bibliographic_parse"] == reference["bibliographic_parse"]
        assert projected["bibliographic_parse"]["status"] == "parsed"
        assert projected["authors"] == reference["authors"]
        assert projected["title"] == reference["title"]
        assert projected["journal"] == reference["journal"]
        assert projected["year"] == reference["year"]
        assert rr.validate_parse(
            projected["bibliographic_parse"], projected["raw_entry"],
        )
        for item in projected["bibliographic_parse"]["fields"].values():
            for span in item["spans"]:
                assert projected["raw_entry"][span["start"]:span["end"]] == span["raw_value"]
    finally:
        repo.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
