#!/usr/bin/env python3
# tests/test_verdict_validation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for core.verdict_validation.note_validation_reason."""
from ._bootstrap import *  # noqa: F401,F403

from core.verify.verdict_validation import note_validation_reason


class TestNoteValidation(unittest.TestCase):
    def test_present_note_passes(self):
        self.assertIsNone(note_validation_reason({"note": "because the passage matches"}))

    def test_missing_note_key_rejected(self):
        self.assertIsNotNone(note_validation_reason({"outcome": "supports"}))

    def test_none_note_rejected(self):
        self.assertIsNotNone(note_validation_reason({"note": None}))

    def test_blank_note_rejected(self):
        self.assertIsNotNone(note_validation_reason({"note": "   "}))

    def test_non_dict_rejected(self):
        self.assertIsNotNone(note_validation_reason(None))
        self.assertIsNotNone(note_validation_reason("not a dict"))


if __name__ == "__main__":
    unittest.main()
