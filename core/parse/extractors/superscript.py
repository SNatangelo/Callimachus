#!/usr/bin/env python3
# core/parse/extractors/superscript.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Superscripts a text format CAN carry, and how to read them.

Plain text is blind to a raised digit: the .txt of a paper that cites by superscript
arrives with its citations already destroyed.  Markdown is not.  It has three ways to
say "this digit is raised", and the real converters use them — pandoc writes a DOCX
superscript run as ``^1,2^``, HTML-to-Markdown tools keep the ``<sup>1,2</sup>``, and
the Unicode raised digits (``¹²³``) survive any encoding that can hold the rest of the
paper, .txt included.

All three are an explicit typographic act by an author, or by a converter that held the
information and chose to keep it.  That puts them in the same class as DOCX's
``w:vertAlign`` and a PDF's glyph flags: we are TOLD which digits are raised, we do not
infer it.  So "can this file carry a citation marker" is not a question about the
extension — it is a question about the file, and a .md (or a .txt) that writes its
markers as ``^1^`` has them.

What still needs guarding is therefore not *is this digit raised* — that is settled —
but *is this raised digit a citation*: the exponent (``10^-5^``) and the unit
(``cm^2^``) are raised too.  That is the same short list the PDF converter screens for,
and it is screened for here with the same list.
"""

from __future__ import annotations

import re

try:
    from core.parse.parsing_common import RANGE_DASHES, NUMBER_RUN
except ImportError:  # standalone use, as the other extractors allow
    from parsing_common import RANGE_DASHES, NUMBER_RUN


# Raised digits that are NOT citations: the tail of a unit or a function.  Shared with
# the PDF text heuristic, which meets exactly the same false friends.
FORMULA_PREFIXES = {
    "sin", "cos", "tan", "cot", "csc", "log", "ln", "exp",
    "km", "cm", "mm", "nm", "um", "kg", "mg", "ml", "dl", "dm", "ms",
}
CONTEXT_WORDS = (
    r"(?:pp|p|vol|nos|no|nr|figs|fig|tbl|table|sections|section|sec|eq|chap|ch|"
    r"doi|isbn)"
)

UNICODE_DIGITS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}

# Comma and every dash a typesetter joins two citation numbers with: the minus sign
# of "11−18" is one of them, and a dash missing here does not fail loudly — it cuts
# the range at its first number and loses the rest in silence.
_SEPARATORS = "," + RANGE_DASHES
_UNI = "".join(UNICODE_DIGITS)

# <sup>1,2</sup> — HTML, and so also every Markdown dialect.
_HTML_SUP_RE = re.compile(r"<sup>([^<>]{1,40}?)</sup>", re.I)
# ^1,2^ — Pandoc, which is what a DOCX superscript run becomes on the way to Markdown.
_PANDOC_SUP_RE = re.compile(r"\^([^\s^]{1,20}?)\^")
# ¹²³ — the raised digits themselves, which need no markup to survive.
_UNICODE_SUP_RE = re.compile(f"[{_UNI}][{_UNI}{_SEPARATORS}]*")

_DIGITS_RE = re.compile(rf"\s*\d{{1,3}}(?:\s*[{_SEPARATORS}]\s*\d{{1,3}})*\s*")


def _line_start(text: str, idx: int) -> int:
    nl = text.rfind("\n", 0, idx)
    return nl + 1


def _is_citation(text: str, start: int, digits: str) -> bool:
    """Is this raised run a citation marker, rather than an exponent or a unit?"""
    nums = [int(n) for n in re.findall(r"\d{1,3}", digits)]
    if not nums:
        return False
    for n in nums:
        if n < 1 or n > 150 or 1900 <= n <= 2099:
            return False
    left = text[_line_start(text, start):start].rstrip()
    if not left:
        # Nothing to attach to: a marker cites something, and there is no something.
        return False
    if left[-1].isdigit():
        # An exponent: 10⁻⁵, 2³, x^2^.
        return False
    if not (left[-1].isalpha() or left[-1] in ".,;:)]}”’\"'"):
        return False
    tok = re.search(r"([A-Za-z]+)$", left)
    if tok:
        word = tok.group(1)
        if len(word) == 1:
            # A single letter carries a unit or a variable, not a citation: m², x².
            return False
        if word.lower() in FORMULA_PREFIXES:
            return False
    if re.search(rf"\b{CONTEXT_WORDS}\.?\s*$", left, re.I):
        return False
    return True


def _to_ascii(raw: str) -> str:
    return re.sub(r"\s+", "", "".join(UNICODE_DIGITS.get(c, c) for c in raw))


def _convert(text: str, pattern: re.Pattern, group: int) -> tuple[str, int]:
    out: list[str] = []
    last = 0
    found = 0
    for m in pattern.finditer(text):
        digits = _to_ascii(m.group(group))
        if not _DIGITS_RE.fullmatch(digits):
            # Raised, but not a number: "21<sup>st</sup>", a dagger, a footnote letter,
            # "10^-5^".  Not ours to touch.
            continue
        if not _is_citation(text, m.start(), digits):
            continue
        out.append(text[last:m.start()])
        out.append(f"⟦SUP:{digits}⟧")
        last = m.end()
        found += 1
    out.append(text[last:])
    return "".join(out), found


def convert(text: str) -> tuple[str, bool]:
    """Turn every superscript citation the document spells out into a ⟦SUP:n⟧ marker.

    Returns the rewritten text, and whether the document actually spelled any out.  That
    second value is what decides whether a low reference coverage can be trusted: a file
    that wrote its raised digits down is not blind, whatever its extension says, and a
    file that wrote none is — because "this paper does not cite by superscript" and
    "this paper's superscripts were flattened on the way here" look identical from here,
    and only the first is safe to assume.
    """
    total = 0
    for pattern, group in ((_UNICODE_SUP_RE, 0), (_HTML_SUP_RE, 1), (_PANDOC_SUP_RE, 1)):
        text, found = _convert(text, pattern, group)
        total += found
    return text, total > 0
