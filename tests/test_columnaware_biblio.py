#!/usr/bin/env python3
# tests/test_columnaware_biblio.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Column-aware bibliography recovery for two-column publisher PDFs.

Some publisher PDFs have a two-column *back matter* whose default reading order
interleaves the reference entries with a table or an author-contributions block,
so they barely segment.  We re-extract the reference pages in column order (left
column fully, then right) and adopt them only when they yield substantially more
entries — a self-validating guard, so a correctly-extracted document is never
touched.  A leading section-marker glyph on the heading ("■References", ACS) is
tolerated.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import zipfile
from xml.sax.saxutils import escape

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tests._bootstrap import *  # noqa: F401,F403

from core.parse import parser_text
from core.parse.extractors import docx as docx_extractor, pdf as pdf_extractor
from core.parse.parsing_common import default_split_body_bibliography


class TestBulletHeading(unittest.TestCase):
    def test_glyph_prefixed_references_heading_detected(self):
        # "■References" (ACS section-marker glyph) still splits body/bibliography.
        _body, biblio, _dbg, _end = default_split_body_bibliography(
            "We build on prior work [1].\n"
            "■References\n"
            "1. Smith, J. A first paper. Journal 2020, 1, 10.\n"
            "2. Jones, K. A second paper. Journal 2021, 2, 20.")
        self.assertIn("Smith, J.", biblio)
        self.assertIn("Jones, K.", biblio)

    def test_plain_references_heading_still_works(self):
        _body, biblio, _dbg, _end = default_split_body_bibliography(
            "Body [1].\nReferences\n1. Smith, J. Paper. Journal 2020.")
        self.assertIn("Smith, J.", biblio)

    def test_numbered_bibliography_heading_uses_shared_matcher(self):
        heading_re = parser_text.bibliography_heading_re()
        self.assertIsNotNone(heading_re.fullmatch("8. References"))
        self.assertIsNotNone(heading_re.fullmatch("8.1 References"))
        self.assertIsNotNone(heading_re.fullmatch("8.1.2. References"))
        self.assertIsNone(heading_re.fullmatch("This section 8. References"))
        self.assertIsNone(
            parser_text.further_reading_heading_re().fullmatch("8. Further reading")
        )

    def test_numbered_references_heading_splits_body_and_bibliography(self):
        body, biblio, debug, _end = default_split_body_bibliography(
            "The model is supported by [1].\n"
            "8. References\n"
            "[1] Smith, J. A useful paper. Journal. 2020."
        )

        self.assertEqual(body, "The model is supported by [1].")
        self.assertEqual(debug["cut_line_index"], 1)
        self.assertIn("Smith, J.", biblio)

    def test_numbered_heading_text_inside_prose_does_not_split(self):
        text = "The authors mention 8. References in the introduction."

        body, biblio, debug, _end = default_split_body_bibliography(text)

        self.assertEqual(body, text)
        self.assertEqual(biblio, "")
        self.assertIsNone(debug["cut_line_index"])

    def test_numbered_references_heading_splits_pdf_body_and_bibliography(self):
        body, heading_index, bibliography = pdf_extractor._split_body_bibliography(
            "The model is supported by [1].\n"
            "8. References\n"
            "[1] Smith, J. A useful paper. Journal. 2020."
        )

        self.assertEqual(body, "The model is supported by [1].")
        self.assertEqual(heading_index, 1)
        self.assertEqual(bibliography[0], "8. References")
        self.assertIn("Smith, J.", "\n".join(bibliography))


class TestNumberedHeadingDocx(LinksDisabledMixin, unittest.TestCase):
    def test_numbered_references_heading_parses_and_binds_marker(self):
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>Docx superscript claim</w:t></w:r>'
            '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:t>1</w:t></w:r><w:r><w:t>.</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>8. References</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>[1] Gamma G. Superscript source. Journal. 2020.'
            '</w:t></w:r></w:p>'
            '</w:body></w:document>'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "numbered_references.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            result, _debug = parse(str(path), 1, "auto")  # noqa: F405

        self.assertEqual(len(result["references"]), 1)
        self.assertEqual(len(result["claims"]), 1)
        claim = result["claims"][0]
        reference = result["references"][0]
        self.assertEqual(claim["marker_numbers"], [1])
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(result["citations"][0]["claim_id"], claim["id"])
        self.assertEqual(result["citations"][0]["ref_id"], reference["id"])


class TestDocxAutomaticReferenceNumbering(LinksDisabledMixin, unittest.TestCase):
    _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    @staticmethod
    def _paragraph(text, *, num_id=None, style_id=None):
        properties = []
        if style_id is not None:
            properties.append(f'<w:pStyle w:val="{style_id}"/>')
        if num_id is not None:
            properties.append(
                '<w:numPr><w:ilvl w:val="0"/>'
                f'<w:numId w:val="{num_id}"/></w:numPr>'
            )
        ppr = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
        return (
            f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">"
            f"{escape(text)}</w:t></w:r></w:p>"
        )

    @classmethod
    def _style(cls, style_id, *, based_on=None, num_id=None):
        inheritance = (
            f'<w:basedOn w:val="{based_on}"/>' if based_on is not None else ""
        )
        numbering = ""
        if num_id is not None:
            numbering = (
                '<w:pPr><w:numPr><w:ilvl w:val="0"/>'
                f'<w:numId w:val="{num_id}"/></w:numPr></w:pPr>'
            )
        return (
            f'<w:style w:type="paragraph" w:styleId="{style_id}">'
            f"{inheritance}{numbering}</w:style>"
        )

    @staticmethod
    def _superscript_claim(number):
        return (
            "<w:p><w:r><w:t>A claim supports its conclusion with source "
            "</w:t></w:r><w:r><w:rPr><w:vertAlign w:val=\"superscript\"/>"
            f"</w:rPr><w:t>{number}</w:t></w:r><w:r><w:t>.</w:t></w:r></w:p>"
        )

    @classmethod
    def _write_docx(
        cls, path, paragraphs, *, start_override=1, with_numbering=True, styles=None
    ):
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{cls._W}"><w:body>'
            + "".join(paragraphs)
            + "</w:body></w:document>"
        )
        numbering_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:numbering xmlns:w="{cls._W}">'
            '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0">'
            '<w:start w:val="1"/><w:numFmt w:val="decimal"/>'
            '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
            '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>'
            '<w:num w:numId="5"><w:abstractNumId w:val="2"/>'
            '<w:lvlOverride w:ilvl="0">'
            f'<w:startOverride w:val="{start_override}"/>'
            '</w:lvlOverride></w:num></w:numbering>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            if with_numbering:
                archive.writestr("word/numbering.xml", numbering_xml)
            if styles is not None:
                archive.writestr(
                    "word/styles.xml",
                    f'<?xml version="1.0"?><w:styles xmlns:w="{cls._W}">{styles}</w:styles>',
                )

    def test_automatic_numbers_split_institutional_and_legal_entries(self):
        entries = [
            "Smith, J. A study of citation verification. Journal 2020;10:1-5.",
            "Jones, A. A second study of clinical evidence. Journal 2021;11:6-10.",
            "National Cancer Institute. Cancer stat facts for lung and bronchus cancer. Accessed October 2026.",
            "European Society for Medical Oncology. ESMO living guideline for oncogene-addicted metastatic lung cancer. Accessed September 2026.",
            "Legge 8 marzo 2017, n. 24. Disposizioni in materia di sicurezza delle cure.",
            "Brown, B. Evidence on clinical prediction. Journal 2024;12:10-12.",
        ]
        paragraphs = [
            self._superscript_claim(1),
            self._paragraph("A body paragraph has two lines.\nIts continuation remains in the body."),
            self._paragraph("A numbered methods item is not a citation.", num_id=2),
            self._paragraph("8. References"),
            *[
                self._paragraph(entry, num_id=5)
                for entry in entries
            ],
            self._paragraph("References"),
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "automatic_references.docx"
            self._write_docx(path, paragraphs)

            extracted = docx_extractor._docx_text(str(path))
            result, _debug = parse(str(path), 1, "auto")  # noqa: F405

        self.assertIn(
            "A body paragraph has two lines.\nIts continuation remains in the body.\n"
            "A numbered methods item is not a citation.\n8. References",
            extracted,
        )
        references = result["references"]
        self.assertEqual([ref["ref_number"] for ref in references], list(range(1, 7)))
        self.assertEqual(
            [ref["raw_entry"] for ref in references],
            entries,
        )
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["claims"][0]["marker_numbers"], [1])
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(
            result["citations"][0]["ref_id"], references[0]["id"]
        )

    def test_literal_number_is_preserved_and_conflict_is_rejected(self):
        matching_entry = "[1] Smith, J. A cited source. Journal 2020;4:1-2."
        for entry, conflicts in ((matching_entry, False),
                                 ("[9] Smith, J. A cited source. Journal 2020;4:1-2.", True)):
            with self.subTest(entry=entry):
                paragraphs = [
                    self._paragraph("8. References"),
                    self._paragraph(entry, num_id=5),
                ]
                with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
                    path = Path(temp_dir) / "literal_reference_number.docx"
                    self._write_docx(path, paragraphs)

                    if conflicts:
                        with self.assertRaisesRegex(ValueError, "conflicts"):
                            docx_extractor._docx_text(str(path))
                        continue

                    extracted = docx_extractor._docx_text(str(path))
                    result, _debug = parse(str(path), 1, "auto")  # noqa: F405

                self.assertTrue(extracted.endswith(entry))
                self.assertEqual([ref["ref_number"] for ref in result["references"]], [1])
                self.assertEqual(result["references"][0]["raw_entry"], entry[4:])

    def test_start_override_and_body_counter_continue_across_heading(self):
        paragraphs = [
            self._paragraph("A preliminary list item stays body text.", num_id=5),
            self._superscript_claim(8),
            self._paragraph("8. References"),
            self._paragraph("Gamma, G. First clinical source. Journal 2020;4:1-2.", num_id=5),
            self._paragraph("Delta, D. Second clinical source. Journal 2021;5:3-4.", num_id=5),
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "continued_numbering.docx"
            self._write_docx(path, paragraphs, start_override=7)

            extracted = docx_extractor._docx_text(str(path))
            result, _debug = parse(str(path), 1, "auto")  # noqa: F405

        self.assertIn(
            "A preliminary list item stays body text.\n", extracted
        )
        self.assertNotIn("7. A preliminary list item", extracted)
        self.assertEqual(
            [ref["ref_number"] for ref in result["references"]], [8, 9]
        )
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(
            result["citations"][0]["ref_id"], result["references"][0]["id"]
        )

    def test_missing_numbering_definition_does_not_invent_labels(self):
        paragraphs = [
            self._paragraph("8. References"),
            self._paragraph("Gamma, G. A source. Journal 2020;4:1-2.", num_id=5),
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "missing_numbering.docx"
            self._write_docx(path, paragraphs, with_numbering=False)

            with self.assertRaisesRegex(ValueError, "numbering"):
                docx_extractor._docx_text(str(path))

    def test_style_numbering_inherits_based_on_and_preserves_binding(self):
        entries = [
            "National Cancer Institute. Cancer statistics for lung cancer. Accessed October 2026.",
            "European Society for Medical Oncology. Living guideline for metastatic cancer. Accessed September 2026.",
            "Ministero della Salute. Piano nazionale della prevenzione. Roma, 2024.",
        ]
        styles = self._style("NumberedBase", num_id=5) + self._style(
            "BibEntry", based_on="NumberedBase"
        )
        paragraphs = [
            self._superscript_claim(1),
            self._paragraph("8. References"),
            *[self._paragraph(entry, style_id="BibEntry") for entry in entries],
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "style_numbered.docx"
            self._write_docx(path, paragraphs, styles=styles)

            extracted = docx_extractor._docx_text(str(path))
            result, _debug = parse(str(path), 1, "auto")  # noqa: F405

        self.assertEqual(
            [ref["raw_entry"] for ref in result["references"]], entries
        )
        self.assertEqual([ref["ref_number"] for ref in result["references"]], [1, 2, 3])
        self.assertIn("1. " + entries[0], extracted)
        self.assertIn("2. " + entries[1], extracted)
        self.assertIn("3. " + entries[2], extracted)
        self.assertEqual(result["claims"][0]["marker_numbers"], [1])
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(
            result["citations"][0]["ref_id"], result["references"][0]["id"]
        )

    def test_style_numbering_continues_body_counter_without_labeling_body(self):
        styles = self._style("BibEntry", num_id=5)
        paragraphs = [
            self._paragraph(
                "A preliminary list item stays body text.", style_id="BibEntry"
            ),
            self._superscript_claim(8),
            self._paragraph("8. References"),
            self._paragraph("Gamma, G. First clinical source. Journal 2020;4:1-2.", style_id="BibEntry"),
            self._paragraph("Delta, D. Second clinical source. Journal 2021;5:3-4.", style_id="BibEntry"),
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "continued_style_numbering.docx"
            self._write_docx(path, paragraphs, start_override=7, styles=styles)

            extracted = docx_extractor._docx_text(str(path))
            result, _debug = parse(str(path), 1, "auto")  # noqa: F405

        self.assertIn("A preliminary list item stays body text.\n", extracted)
        self.assertNotIn("7. A preliminary list item", extracted)
        self.assertEqual([ref["ref_number"] for ref in result["references"]], [8, 9])
        self.assertEqual(
            result["citations"][0]["ref_id"], result["references"][0]["id"]
        )

    def test_direct_numpr_override_and_zero_disable_style_numbering(self):
        paragraphs = [
            self._paragraph("Body style is missing but unnumbered.", style_id="MissingBodyStyle"),
            self._paragraph("8. References"),
            self._paragraph("Direct list override source.", style_id="BibEntry", num_id=2),
            self._paragraph("This paragraph disables style numbering.", style_id="BibEntry", num_id=0),
            self._paragraph("Inherited style numbering resumes here.", style_id="BibEntry"),
        ]
        with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
            path = Path(temp_dir) / "style_numbering_overrides.docx"
            self._write_docx(
                path,
                paragraphs,
                start_override=7,
                styles=self._style("BibEntry", num_id=5),
            )

            extracted = docx_extractor._docx_text(str(path))

        self.assertIn("1. Direct list override source.", extracted)
        self.assertIn("This paragraph disables style numbering.", extracted)
        self.assertNotIn("8. This paragraph disables style numbering.", extracted)
        self.assertIn("7. Inherited style numbering resumes here.", extracted)

    def test_ambiguous_or_cyclic_numbered_style_definitions_fail_closed(self):
        cases = (
            (
                "cyclic",
                self._style("BibEntry", based_on="Loop", num_id=5)
                + self._style("Loop", based_on="BibEntry"),
                "cyclic",
            ),
            (
                "ambiguous",
                self._style("BibEntry", num_id=5)
                + self._style("BibEntry", num_id=5),
                "ambiguous",
            ),
        )
        for name, styles, message in cases:
            with self.subTest(style_error=name):
                paragraphs = [
                    self._paragraph("8. References"),
                    self._paragraph("Gamma, G. A source. Journal 2020;4:1-2.", style_id="BibEntry"),
                ]
                with tempfile.TemporaryDirectory(dir=_PROJECT_ROOT) as temp_dir:
                    path = Path(temp_dir) / f"{name}_style_numbering.docx"
                    self._write_docx(path, paragraphs, styles=styles)

                    with self.assertRaisesRegex(ValueError, message):
                        docx_extractor._docx_text(str(path))




if __name__ == "__main__":
    unittest.main()
