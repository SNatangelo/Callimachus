#!/usr/bin/env python3
# core/parse/format_handlers/plain_text.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Format handler for plain-text manuscripts — PDF, TXT, DOCX.

All three extractors produce space-aligned text.  Table detection here
uses two conservative score-layout signals:

* **Decimal density** — benchmark tables have many floating-point
  numbers crammed into one "sentence" with very little prose.
* **Compact percentage row** — a line-preserving backend may expose one
  model/citation/score row at a time, with one decimal but very little prose.

``&`` column separators are **not** a plain-text table signal (they
indicate LaTeX markup or prose like "Smith & Jones"); plain-text tables
are detected by decimal density only.  The ``&`` signal lives exclusively
in the LaTeX format handler, where it is a reliable artefact of the
extractor output.
"""

from __future__ import annotations

import re

FORMATS = ["pdf", "txt", "docx"]

# Two or more decimal numbers like 28.4, 91.5, 0.123 — common in benchmark
# tables, rare in prose sentences.
_DECIMAL_RE = re.compile(r"\b\d+\.\d+\b")
_FINITE_PREDICATIVE_VERB_RE = re.compile(
    r"\b(?:am|is|are|was|were|be|been|being|has|have|had|do|does|did|"
    r"can|could|will|would|shall|should|may|might|must|"
    r"achieved|achieves|increased|increases|decreased|decreases|"
    r"improved|improves|reached|reaches|reported|reports|"
    r"scored|scores|showed|shown|shows|yielded|yields|"
    r"outperformed|outperforms|exceeded|exceeds|rose|rises|fell|falls)\b",
    re.IGNORECASE,
)
_BASE_PREDICATIVE_VERB_RE = re.compile(
    r"\b(?:achieve|increase|decrease|improve|reach|report|score|show|"
    r"yield|outperform|exceed|rise|fall)\b",
    re.IGNORECASE,
)
_COMPACT_SCORE_HEADER_RE = re.compile(
    r"^\s*[A-Z][A-Za-z0-9_-]*\s+"
    r"(?:Score|Accuracy|Precision|Recall|F1|BLEU|ROUGE)\b"
)


def _has_predicative_verb(sentence: str) -> bool:
    if _COMPACT_SCORE_HEADER_RE.search(sentence):
        return False
    if _FINITE_PREDICATIVE_VERB_RE.search(sentence):
        return True
    base = _BASE_PREDICATIVE_VERB_RE.search(sentence)
    first_word = re.search(r"[A-Za-z]+", sentence)
    # A compact table label commonly begins with "Score" or "Report".
    # The same word after an explicit subject ("models score ...") is a verb.
    return bool(base and first_word and base.start() != first_word.start())


def is_table_row(sentence: str, markers: list) -> bool:
    """Plain-text table row: score layout with low alphabetic density.

    ``&`` column separators are **not** a plain-text table signal — they
    indicate LaTeX markup or prose like "Smith & Jones".  The ``&`` signal
    lives exclusively in the LaTeX format handler.

    **Decimal density** — at least two floating-point numbers (``28.4``,
    ``91.5``) with low alphabetic content:
    * **< 30 % alpha** — sparse text (few labels, mostly numbers):
      a single marker is enough.
    * **30–65 % alpha** — moderate text load (PDF mush): requires **≥ 3
      citation markers** as a second confirmation.
    * **≥ 65 % alpha** — prose; never suppressed.
    """
    if not markers:
        return False

    alpha = sum(1 for c in sentence if c.isalpha())
    total = len(sentence) or 1
    ratio = alpha / total

    # Signal: decimal density (benchmark tables)
    decimals = len(_DECIMAL_RE.findall(sentence))
    if decimals >= 2:
        if ratio < 0.30:
            return True
        # PDF mush: moderate alpha from concatenated labels but many markers
        if len(markers) >= 3 and ratio < 0.65:
            return True

    if decimals >= 1 and "%" in sentence and _COMPACT_SCORE_HEADER_RE.search(sentence):
        return True

    # A line-preserving PDF backend can expose one compact benchmark row per
    # sentence instead of folding the whole table together.  Such rows often
    # contain only a model label, one citation and one percentage
    # (``ResNet50 [16] 77.15%``), so decimal *density* never gets a chance to
    # fire.  Keep this deliberately narrow: a percentage is mandatory and the
    # row must remain mostly non-alphabetic.  Ordinary prose that reports one
    # score has a much higher alphabetic ratio and stays verifiable.
    if (
        decimals >= 1
        and "%" in sentence
        and ratio < 0.40
        and not _has_predicative_verb(sentence)
    ):
        return True

    return False
