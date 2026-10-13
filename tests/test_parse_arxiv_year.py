#!/usr/bin/env python3
# tests/test_parse_arxiv_year.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""arXiv-identifier artefacts in reference year extraction and titles.

An arXiv id ("arXiv:1903.10520") leads with a ``YYYY.NNNNN`` group whose first
four digits look like a year.  Two independent code paths read a reference's
year — ``_make_reference`` (metadata, all styles) and ``authoryear.entry_key``
(author-year matching) — and both must ignore the arXiv-id digits so a 2019
paper is not filed under 1903, which would orphan its in-text citations.

Also pins the title trim for a bare ``arXiv`` venue token and the collapse of
line-wrap whitespace a PDF leaves inside a title.
"""

from __future__ import annotations

from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tests._bootstrap import *  # noqa: F401,F403

from core.parse.parsing_common import _make_reference, _trim_title_tail, _extract_title
from core.parse import authoryear


_ARXIV_ENTRY = (
    "Siyuan Qiao, Huiyu Wang, Chenxi Liu, Wei Shen, and Alan Yuille. Weight "
    "standardization. arXiv preprint arXiv:1903.10520, 2019."
)


class TestMakeReferenceYear(unittest.TestCase):
    def test_year_not_taken_from_arxiv_id(self):
        ref = _make_reference(1, _ARXIV_ENTRY)
        self.assertEqual(ref["year"], 2019)

    def test_plain_year_unchanged(self):
        ref = _make_reference(2, "Some Author. A title. In NIPS, 2012.")
        self.assertEqual(ref["year"], 2012)

    def test_page_range_dot_is_not_stripped_as_arxiv(self):
        # A real "vol:page" or ", 2001;" year survives (no YYYY.NNNNN token here).
        ref = _make_reference(3, "Author A. Title. Journal 2001; 10: 1-5.")
        self.assertEqual(ref["year"], 2001)


class TestAuthorYearEntryKey(unittest.TestCase):
    def test_year_not_taken_from_arxiv_id(self):
        surname, year, suffix = authoryear.entry_key(_ARXIV_ENTRY)
        self.assertEqual((surname, year), ("qiao", 2019))

    def test_year_suffix_preserved(self):
        surname, year, suffix = authoryear.entry_key(
            "Xiaohua Zhai, Avital Oliver, and Lucas Beyer. S4L. In ICCV, 2019a."
        )
        self.assertEqual((surname, year, suffix), ("zhai", 2019, "a"))

    def test_arxiv_id_with_version_suffix(self):
        surname, year, _ = authoryear.entry_key(
            "Hugo Touvron and Herve Jegou. Fixing resolution. arXiv:2003.08237v2, 2020."
        )
        self.assertEqual((surname, year), ("touvron", 2020))

    def test_suffixed_year_after_arxiv_id_stays_a_real_year(self):
        raw = "Zhai, X. A work. arXiv:2003.08237v2, 2020a."
        self.assertEqual(_make_reference(1, raw)["year"], 2020)
        self.assertEqual(authoryear.entry_key(raw), ("zhai", 2020, "a"))


class TestJournalIssuePageRangeYear(unittest.TestCase):
    _ENTRY = (
        "Nitish Srivastava, Geoffrey Hinton, Alex Krizhevsky, Ilya Sutskever, "
        "and Ruslan Salakhutdinov. Dropout: A Simple Way to Prevent Neural "
        "Networks from Overfitting. Journal of Machine Learning Research, "
        "15(1):1929–1958, 2014."
    )

    def test_terminal_publication_year_replaces_page_endpoint_in_both_readers(self):
        ref = _make_reference(1, self._ENTRY)
        ay_surname, ay_year, ay_suffix = authoryear.entry_key(ref["raw_entry"])
        # parse_manuscript assigns this entry_key year to the ay_year field.
        self.assertEqual((ref["year"], ay_year), (2014, 2014))
        self.assertEqual((ay_surname, ay_suffix), ("srivastava", ""))

    def test_earlier_entry_year_keeps_first_year_precedence(self):
        raw = (
            "Smith, John 2018. A study mentioning 2016. Journal 15(1):"
            "1929–1958, 2020."
        )
        ref = _make_reference(2, raw)
        self.assertEqual(ref["year"], 2018)
        self.assertEqual(authoryear.entry_key(raw)[1], 2018)

    def test_reprint_original_year_remains_the_author_year_key(self):
        raw = "Fanon, Frantz [1963] 2008 Black Skin, White Masks."
        self.assertEqual(_make_reference(3, raw)["year"], 1963)
        self.assertEqual(authoryear.entry_key(raw), ("fanon", 1963, ""))


class TestTitleTrimAndWhitespace(unittest.TestCase):
    def test_bare_arxiv_token_trimmed(self):
        self.assertEqual(
            _trim_title_tail("Are we done with imagenet? arXiv"),
            "Are we done with imagenet?",
        )

    def test_internal_linewrap_collapsed(self):
        self.assertEqual(
            _trim_title_tail("Are\nwe done with imagenet?"),
            "Are we done with imagenet?",
        )

    def test_extract_title_bare_arxiv_and_wrap(self):
        raw = (
            "Lucas Beyer, Olivier Henaff, and Alexander Kolesnikov. Are\nwe done "
            "with imagenet? arXiv, 2020."
        )
        self.assertEqual(_extract_title(raw), "Are we done with imagenet?")

    def test_wrapped_accented_author_list_is_not_the_title(self):
        # ViT refs 5 and 22: periods after J. / S. M. are initials, including
        # when the next surname begins with an accented lowercase letter.
        raw = (
            "Olivier J. Hénaff, Aravind Srinivas, Jeffrey De Fauw, Ali Razavi, "
            "Carl Doersch, S. M. Ali Eslami,\nand Aaron van den Oord. "
            "Data-efficient image recognition with contrastive predictive coding. "
            "In ICML, 2020."
        )
        ref = _make_reference(22, raw)
        self.assertEqual(ref["raw_entry"], raw)  # metadata parsing never rewrites evidence
        self.assertEqual(ref["title"], "Data-efficient image recognition with contrastive predictive coding")


class TestVitRealPdf(LinksDisabledMixin, unittest.TestCase):
    """Integration on the real ViT (author-year) paper, when present."""

    _PDF = _PROJECT_ROOT / "Paper test" / "vit_image_worth_16x16.pdf"

    @unittest.skipUnless(_PDF.exists(), "ViT sample PDF not available")
    def test_arxiv_year_orphans_resolved(self):
        try:
            import fitz  # noqa: F401
        except Exception:  # pragma: no cover - depends on environment
            self.skipTest("no PDF extraction backend available")
        result, _debug = parse(str(self._PDF), 1, "auto")  # noqa: F405
        refs = result["references"]
        # Contiguous numbering: 57 after the running-header split, +1 for the
        # mis-segmented Krizhevsky 2009 entry the on-demand rescue splits out.
        self.assertEqual(sorted(r["ref_number"] for r in refs), list(range(1, 59)))
        # No title carries an arXiv venue tail or an internal newline.
        bad = [r["ref_number"] for r in refs
               if "arXiv" in (r.get("title") or "") or "\n" in (r.get("title") or "")]
        self.assertEqual(bad, [])
        by_number = {ref["ref_number"]: ref for ref in refs}
        self.assertEqual(by_number[5]["title"], "Are we done with imagenet?")
        self.assertEqual(by_number[8]["year"], 2020)
        self.assertEqual(
            by_number[22]["title"],
            "Data-efficient image recognition with contrastive predictive coding",
        )
        self.assertNotIn("Models\nDataset", by_number[57]["raw_entry"])
        # The arXiv-year orphans (Qiao 2019, Touvron 2020, Zhai, Wang) are gone;
        # only the segmentation/source-inconsistency orphans remain.
        orphan_surnames = {o["surname"] for o in (result.get("_debug") or {}).get("orphans") or []}
        self.assertNotIn("qiao", orphan_surnames)
        self.assertNotIn("zhai", orphan_surnames)


if __name__ == "__main__":
    unittest.main()
