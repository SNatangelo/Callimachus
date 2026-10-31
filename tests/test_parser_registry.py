#!/usr/bin/env python3
# tests/test_parser_registry.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for parser registries introduced by the parser rework."""

from tests._bootstrap import *  # noqa: F401,F403

import types
import unicodedata
from pathlib import Path
from unittest import mock

from core.parse import authoryear
from core.parse import extract as extract_mod
from core.parse import citation_schemes
from core.parse import extractors
from core.parse import parser_config


class TestExtractorRegistryScaffold(unittest.TestCase):
    def setUp(self):
        self._saved_config_path = parser_config._CONFIG_PATH

    def tearDown(self):
        parser_config._CONFIG_PATH = self._saved_config_path
        parser_config.reset_for_tests()
        extractors.reset_for_tests()

    def test_docx_tex_markdown_pdf_and_txt_modules_are_discovered(self):
        extractors.reset_for_tests()
        registry = extractors.get_registry()
        self.assertIn("docx", registry)
        self.assertIn("tex", registry)
        self.assertIn("markdown", registry)
        self.assertIn("pdf", registry)
        self.assertIn("txt", registry)
        self.assertEqual(registry["docx"].EXTENSIONS, (".docx",))
        self.assertEqual(registry["tex"].EXTENSIONS, (".tex",))
        self.assertEqual(registry["markdown"].EXTENSIONS, (".md", ".markdown"))
        self.assertEqual(registry["pdf"].EXTENSIONS, (".pdf",))
        self.assertTrue(callable(getattr(registry["pdf"], "postprocess", None)))
        self.assertEqual(registry["txt"].EXTENSIONS, (".txt",))
        self.assertIn("html", registry)
        self.assertEqual(registry["html"].EXTENSIONS, (".html", ".htm", ".xhtml"))
        self.assertEqual(
            extractors.supported_extensions(),
            (".docx", ".markdown", ".md", ".pdf", ".tex", ".txt", ".htm", ".html", ".xhtml"),
        )
        self.assertEqual(
            extract_mod.supported_extensions(),
            (".docx", ".markdown", ".md", ".pdf", ".tex", ".txt", ".htm", ".html", ".xhtml"),
        )

    def test_extract_for_unknown_extension_raises(self):
        extractors.reset_for_tests()
        with self.assertRaises(ValueError):
            extractors.extract_for("paper.xyz", ocr_lang="eng", ocr_notice=None, manuscript_ocr=None)

    def test_future_extractor_can_supply_its_own_content_probe(self):
        future = types.SimpleNamespace(
            __name__="core.parse.extractors.future",
            CONTENT_FAMILY="future-container",
            probe_file=lambda path: "future-container",
        )
        with mock.patch.object(
            extractors, "_extension_registry", return_value={".future": future}
        ):
            self.assertEqual(
                extract_mod.probe_file("article.future"),
                {
                    "declared_format": "future-container",
                    "detected_format": "future-container",
                },
            )

    def test_html_fragment_matches_the_html_content_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "fragment.html")
            path.write_text(
                "<article><h1>Title</h1><p>Full text.</p></article>",
                encoding="utf-8",
            )
            self.assertEqual(
                extract_mod.probe_file(str(path)),
                {"declared_format": "html", "detected_format": "html"},
            )

    def test_pdf_postprocess_normalizes_body_to_nfc_before_superscript_pass(self):
        extractors.reset_for_tests()
        pdf_module = extractors.get_registry()["pdf"]
        raw_text = "Cafe\u0301 1\n\nReferences\n[1] Example ref."
        processed = pdf_module.postprocess(raw_text, {})
        body = processed.split("\n\n", 1)[0]
        self.assertTrue(unicodedata.is_normalized("NFC", body))
        self.assertIn("Café", body)


    def test_disabled_extractor_extension_becomes_unsupported(self):
        with tempfile.TemporaryDirectory() as d:
            config_path = Path(d) / "parsers.json"
            config_path.write_text('{"extractors":{"pdf":{"enabled":false}}}', encoding="utf-8")
            parser_config._CONFIG_PATH = config_path
            parser_config.reset_for_tests()
            extractors.reset_for_tests()
            self.assertNotIn(".pdf", extractors.supported_extensions())
            self.assertNotIn(".pdf", extract_mod.supported_extensions())
            with self.assertRaises(ValueError):
                extract_mod.extract_text(
                    "paper.pdf",
                    ocr_lang="eng",
                    ocr_notice=None,
                    manuscript_ocr=True,
                )


class TestCitationSchemeRegistryScaffold(unittest.TestCase):
    def setUp(self):
        self._saved_config_path = parser_config._CONFIG_PATH

    def tearDown(self):
        parser_config._CONFIG_PATH = self._saved_config_path
        parser_config.reset_for_tests()
        citation_schemes.reset_for_tests()

    def test_numeric_scheme_module_is_discovered(self):
        citation_schemes.reset_for_tests()
        registry = citation_schemes.get_registry()
        self.assertIn("numeric", registry)
        self.assertIn("author-year", registry)
        self.assertIn("inline-doi", registry)
        self.assertEqual(registry["numeric"].NAME, "numeric")
        self.assertEqual(registry["author-year"].NAME, "author-year")
        self.assertEqual(registry["inline-doi"].NAME, "inline-doi")
        self.assertIs(citation_schemes.get("numeric"), registry["numeric"])
        self.assertIs(citation_schemes.get("author-year"), registry["author-year"])
        self.assertIs(citation_schemes.get("inline-doi"), registry["inline-doi"])
        self.assertEqual(
            [scheme.NAME for scheme in citation_schemes.all_schemes()],
            ["author-year", "inline-doi", "numeric", "mla"],
        )
        self.assertEqual(
            citation_schemes.mode_names(include_auto=True),
            ("auto", "author-year", "inline-doi", "numeric", "mla"),
        )

    def test_numeric_scheme_detect_and_build_via_registry(self):
        from core.parse.parse_manuscript import _make_reference

        citation_schemes.reset_for_tests()
        scheme = citation_schemes.get("numeric")
        refs = [_make_reference(n, f"Author {n}. Title {n}. Journal. 2020;1:1-2.")
                for n in range(1, 9)]
        sentences = [
            "First finding holds [1].", "Second finding holds [2].",
            "Third finding holds [3].", "Fourth finding holds [4].",
            "Fifth finding holds [5].",
            "The estimate equals two point five (2) in this cohort.",
        ]
        self.assertEqual(scheme.detect("", sentences, refs), 6.0)
        claims, citations, _rows, extra = scheme.build(sentences, refs, 1, "ms-x")
        self.assertEqual(len(claims), 5)
        self.assertEqual(len(extra.get("suppressed_markers", [])), 1)
        self.assertNotIn(6, [c["ref_number"] for c in citations if c.get("ref_number")])

    def test_select_explicit_unknown_scheme_raises(self):
        citation_schemes.reset_for_tests()
        with self.assertRaises(ValueError):
            citation_schemes.select("", [], [], mode="not-a-scheme")

    def test_select_explicit_numeric_returns_module(self):
        citation_schemes.reset_for_tests()
        selected = citation_schemes.select("", [], [], mode="numeric")
        self.assertEqual(selected.NAME, "numeric")

    def test_select_explicit_author_year_returns_module(self):
        citation_schemes.reset_for_tests()
        selected = citation_schemes.select("", [], [], mode="author-year")
        self.assertEqual(selected.NAME, "author-year")

    def test_select_explicit_inline_doi_returns_module(self):
        citation_schemes.reset_for_tests()
        selected = citation_schemes.select("", [], [], mode="inline-doi")
        self.assertEqual(selected.NAME, "inline-doi")

    def test_disabled_named_scheme_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            config_path = Path(d) / "parsers.json"
            config_path.write_text(
                '{"schemes":{"inline-doi":{"enabled":false}}}',
                encoding="utf-8",
            )
            parser_config._CONFIG_PATH = config_path
            parser_config.reset_for_tests()
            citation_schemes.reset_for_tests()
            with self.assertRaises(ValueError):
                citation_schemes.select("", [], [], mode="inline-doi")

    def test_select_auto_with_only_numeric_returns_numeric(self):
        citation_schemes.reset_for_tests()
        selected = citation_schemes.select("", ["A claim [1]."], [], mode="auto")
        self.assertEqual(selected.NAME, "numeric")

    def test_select_auto_defaults_to_numeric_on_no_signal(self):
        citation_schemes._REGISTRY_CACHE = {
            "numeric": types.SimpleNamespace(NAME="numeric", detect=lambda *_: 0.0),
            "author-year": types.SimpleNamespace(NAME="author-year", detect=lambda *_: 0.0),
            "inline-doi": types.SimpleNamespace(NAME="inline-doi", detect=lambda *_: 0.0),
        }
        selected = citation_schemes.select("", [], [], mode="auto")
        self.assertEqual(selected.NAME, "numeric")

    def test_select_auto_defaults_to_numeric_on_numeric_authoryear_tie(self):
        citation_schemes._REGISTRY_CACHE = {
            "numeric": types.SimpleNamespace(NAME="numeric", detect=lambda *_: 2.0),
            "author-year": types.SimpleNamespace(NAME="author-year", detect=lambda *_: 2.0),
            "inline-doi": types.SimpleNamespace(NAME="inline-doi", detect=lambda *_: 1.0),
        }
        selected = citation_schemes.select("", [], [], mode="auto")
        self.assertEqual(selected.NAME, "numeric")

    def test_select_auto_prefers_author_year_only_when_it_exceeds_numeric(self):
        citation_schemes._REGISTRY_CACHE = {
            "numeric": types.SimpleNamespace(NAME="numeric", detect=lambda *_: 2.0),
            "author-year": types.SimpleNamespace(NAME="author-year", detect=lambda *_: 3.0),
            "inline-doi": types.SimpleNamespace(NAME="inline-doi", detect=lambda *_: 1.0),
        }
        selected = citation_schemes.select("", [], [], mode="auto")
        self.assertEqual(selected.NAME, "author-year")

    def test_select_auto_prefers_inline_doi_on_equal_numeric_and_greater_than_author_year(self):
        citation_schemes._REGISTRY_CACHE = {
            "numeric": types.SimpleNamespace(NAME="numeric", detect=lambda *_: 2.0),
            "author-year": types.SimpleNamespace(NAME="author-year", detect=lambda *_: 1.0),
            "inline-doi": types.SimpleNamespace(NAME="inline-doi", detect=lambda *_: 2.0),
        }
        selected = citation_schemes.select("", [], [], mode="auto")
        self.assertEqual(selected.NAME, "inline-doi")

    def test_author_year_scheme_detect_and_build_via_registry(self):
        from core.parse.parse_manuscript import _make_reference

        citation_schemes.reset_for_tests()
        scheme = citation_schemes.get("author-year")
        refs = [
            _make_reference(1, "Smith, J. 2021. Alpha trends. Journal."),
            _make_reference(2, "Jones, K. 2019. Beta trends. Journal."),
        ]
        for ref in refs:
            sur, year, suf = authoryear.entry_key(ref["raw_entry"])
            ref["ay_surname"], ref["ay_year"], ref["ay_suffix"] = sur, year, suf
        sentences = [
            "Alpha values increased over time (Smith 2021), whereas beta values steadily decreased (Jones 2019)."
        ]
        self.assertEqual(scheme.detect(sentences[0], sentences, refs), 2.0)
        claims, citations, _rows, extra = scheme.build(sentences, refs, 1, "ms-x")
        self.assertEqual(len(claims), 2)
        self.assertEqual([c["ref_number"] for c in citations], [1, 2])
        self.assertEqual(extra.get("ambiguities"), [])

    def test_inline_doi_scheme_detect_synthesize_and_build_via_registry(self):
        citation_schemes.reset_for_tests()
        scheme = citation_schemes.get("inline-doi")
        sentences = [
            "The treatment reduces mortality (10.1038/s41586-022-05543-x).",
            "Transformers reshaped NLP (arXiv:1706.03762).",
        ]
        self.assertEqual(scheme.detect("", sentences, []), 2.0)
        refs = scheme.synthesize_references(sentences, "ms-x")
        self.assertEqual([ref["doi"] for ref in refs], [
            "10.1038/s41586-022-05543-x",
            "10.48550/arXiv.1706.03762",
        ])
        claims, citations, _rows, extra = scheme.build(sentences, refs, 1, "ms-x")
        self.assertEqual(len(claims), 2)
        self.assertEqual(len(citations), 2)
        self.assertEqual(extra.get("orphans"), [])

    def test_parse_uses_registry_select_for_mode_resolution(self):
        from core.parse.parse_manuscript import parse

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(
                    "# Draft\n\n"
                    "The treatment reduces mortality (10.1038/s41586-022-05543-x).\n"
                )
            with mock.patch("core.parse.parse_manuscript.citation_schemes.select",
                            wraps=citation_schemes.select) as select_mock:
                parsed, _debug = parse(p, 1, "auto")
        self.assertTrue(select_mock.called)
        self.assertEqual(parsed["manuscript"]["citation_mode"], "inline-doi")

    def test_scheme_order_controls_author_year_inline_doi_tie_above_numeric(self):
        with tempfile.TemporaryDirectory() as d:
            config_path = Path(d) / "parsers.json"
            config_path.write_text(
                '{"scheme_order":["inline-doi","author-year","numeric"]}',
                encoding="utf-8",
            )
            parser_config._CONFIG_PATH = config_path
            parser_config.reset_for_tests()
            citation_schemes.reset_for_tests()
            body = (
                "Alpha values increased over time (Smith 2021). "
                "The treatment reduces mortality (10.1038/s41586-022-05543-x)."
            )
            sentences = [
                "Alpha values increased over time (Smith 2021).",
                "The treatment reduces mortality (10.1038/s41586-022-05543-x).",
            ]
            selected = citation_schemes.select(body, sentences, [], mode="auto")
        self.assertEqual(selected.NAME, "inline-doi")
        self.assertEqual(
            citation_schemes.mode_names(include_auto=True),
            ("auto", "inline-doi", "author-year", "numeric", "mla"),
        )
