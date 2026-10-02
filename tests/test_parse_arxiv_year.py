#!/usr/bin/env python3
# tests/test_parse_arxiv_year.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Publication years after journal page ranges remain authoritative."""

from __future__ import annotations

from pathlib import Path

import sys

from tests._bootstrap import *  # noqa: F401,F403

from core.parse.parsing_common import _make_reference, _trim_title_tail, _extract_title

from core.parse import authoryear

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
