#!/usr/bin/env python3
# core/parse/extractors/pdf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""PDF manuscript extractor."""

from __future__ import annotations

import importlib
import math
import os
import re
import unicodedata
import zlib

try:
    from core.parse import parser_text, parsing_common
    from core.parse.extractors import superscript
except ImportError:
    import parser_text
    import parsing_common
    import superscript


EXTENSIONS = (".pdf",)
# A raised digit that is a unit or a function tail is not a citation, and that is true
# of every format — so the list lives with the other superscript reading, next door.
_SUP_FORMULA_PREFIXES = superscript.FORMULA_PREFIXES
_SUP_CONTEXT_WORDS = superscript.CONTEXT_WORDS
# The dash that joins two citation numbers into a range — every shape of it.
_DASH = parsing_common.RANGE_DASHES


def _resolve_extract_module():
    try:
        return importlib.import_module("core.parse.extract")
    except ImportError:
        return importlib.import_module("extract")


def _pdf_unescape(b: bytes) -> str:
    s = b[1:-1]
    s = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), s)
    for a, c in (
        (rb"\\(", b"("),
        (rb"\\)", b")"),
        (rb"\\n", b" "),
        (rb"\\r", b""),
        (rb"\\t", b" "),
        (rb"\\\\", b"\\"),
    ):
        s = s.replace(a, c)
    return s.decode("latin-1", errors="replace")


def _pdf_text_fallback(path: str) -> str:
    """Minimal stdlib extractor: stream (zlib or raw) -> strings from BT/ET blocks."""
    with open(path, "rb") as f:
        raw = f.read()
    lines = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.DOTALL):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        if b"BT" not in data:
            continue
        for bt in re.finditer(rb"BT(.*?)ET", data, re.DOTALL):
            parts = [
                _pdf_unescape(sm.group(0))
                for sm in re.finditer(rb"\((?:\\.|[^()\\])*\)", bt.group(1))
            ]
            if parts:
                lines.append("".join(parts))
    return "\n".join(lines)


def _pdf_text(path: str) -> tuple[str, dict]:
    try:
        from core.fetch.extraction import pdf as _pdf_mod
    except ImportError:
        try:
            import pdf as _pdf_mod
        except ImportError:
            _pdf_mod = None

    if _pdf_mod is not None:
        try:
            # Request form-feed page separators so postprocess can recover
            # per-page footnote references; it strips them again afterwards.
            if hasattr(_pdf_mod, "extract_with_quality_audit"):
                text, method, flags, audit = _pdf_mod.extract_with_quality_audit(
                    path, page_sep="\n\n\f")
            else:  # pragma: no cover - compatibility import path
                text, method, flags = _pdf_mod.extract_with_quality(
                    path, page_sep="\n\n\f")
                audit = None
            if text.strip():
                meta = {"pdf_method": method}
                if flags:
                    meta["pdf_structure_flags"] = flags
                if audit:
                    meta["pdf_variant_audit"] = audit
                return text, meta
        except Exception:
            pass

    return _pdf_text_fallback(path), {"pdf_method": "fallback_stdlib"}


def _paren_content_before(line: str, close_idx: int) -> str | None:
    depth = 0
    for i in range(close_idx, -1, -1):
        ch = line[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                return line[i + 1:close_idx]
    return None


# How much text to the left of a candidate is used to locate it in the PDF's own
# glyph stream.  Long enough to land on one spot, short enough to survive the small
# differences between the extractor's text and the raw spans.
_SUP_ORACLE_CONTEXT = 12

# What separates a base from the expression around it.  A hyphen is deliberately not
# here: "SARS-CoV-2" and "HIV-1" spell a name with one, they do not compute anything.
_MATH_OPERATOR_RE = re.compile(r"[=+*/^±×·⋅<>≈≤≥~]")


def _superscript_oracle(path: str):
    """What the PDF itself says about which digits are raised.

    ``_convert_pdf_superscripts`` reconstructs citation markers from plain text, where
    a superscript is indistinguishable from any other digit — hence its long tail of
    guards (decimals, clock fields, formula prefixes).  But the document *knows*: a
    citation marker is a span with the superscript flag set, in a smaller font, while
    the 2 of "BRCA2" and the 35 of "P = .35" sit in ordinary spans.

    Returns ``(chars, bits)`` — the glyph stream with whitespace removed, and one bit
    per character saying whether it was raised — or ``None`` when the document does
    not answer.  Whitespace goes because the extractor's text and the raw spans
    disagree about it constantly; everything else lines up.

    This stream is not read, it is *looked up* — by the body text, keyed on the
    characters to the candidate's left (see ``_oracle_denies_superscript``).  So it
    must be spelled the way that text is spelled, and postprocess() rewrites the text
    before the lookup: soft hyphens go, ligatures expand, NFC composes, and words
    broken across a line are rejoined.  Each of those is mirrored here.  Dehyphenation
    is the one that bites hardest, because it is invisible: the glyphs hold
    "acad-emy.311", the body holds "academy.311", the key matches nothing, and the
    oracle answers "I cannot locate it" — which reads exactly like "not raised".  On
    7-135-krishnamurthy that silently cost 11 footnotes, all of them past the 150
    ceiling, where an unlocatable candidate is dropped.

    Self-validating: a document that reports NO superscript digit at all is one whose
    answer we cannot trust (or one with no superscript citations to begin with), so we
    return None and leave the text heuristic exactly as it was."""
    try:
        import pymupdf as fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(path)
    except Exception:
        return None
    buf: list[str] = []
    bits = bytearray()
    saw_superscript_digit = False

    def _drop_last() -> None:
        del bits[len(bits) - len(buf.pop()):]

    try:
        for page in doc:
            try:
                page_dict = page.get_text("dict")
            except Exception:
                continue
            for block in page_dict.get("blocks", ()):
                # True when the previous glyph line broke a word across it; the next
                # letter we see closes the break and the hyphen goes, exactly as
                # _dehyphenate does to the text that will look this stream up.  Reset
                # per block: a block boundary is a paragraph break in the extracted
                # text, and _dehyphenate does not join across one either.
                wrapped = False
                for line in block.get("lines", ()):
                    for span in line.get("spans", ()):
                        raised = bool(span.get("flags", 0) & 1)
                        for ch in span.get("text", ""):
                            if ch.isspace() or ch == "\xad":
                                continue
                            # Normalize per character, so a ligature expanding to two
                            # characters keeps its bits aligned with the text.
                            norm = _normalize_ligatures(unicodedata.normalize("NFC", ch))
                            if wrapped:
                                wrapped = False
                                if norm[:1].isalpha():
                                    _drop_last()
                            buf.append(norm)
                            bits.extend((1 if raised else 0,) * len(norm))
                            if raised and ch.isdigit():
                                saw_superscript_digit = True
                    wrapped = (len(buf) >= 2 and buf[-1] == "-"
                               and buf[-2][-1:].isalpha())
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass
    if not saw_superscript_digit:
        return None
    return "".join(buf), bits


def _oracle_denies_superscript(oracle, line: str, start: int, digits: str) -> bool:
    """True when the PDF says these digits are NOT raised — so they are a number, not
    a citation marker.

    The candidate is located by the text immediately to its left, which is what tells
    "P = .35" apart from the AMA marker in "…all cases.35": same digits after a dot,
    different glyphs.  Fails open — an unlocatable candidate is left to the heuristic —
    and a candidate that is raised ANYWHERE it occurs is kept, so the veto only ever
    removes a marker the document itself contradicts.

    Only the DIGITS have to be raised.  A list of markers is not necessarily set as one
    raised run: "actions9,10" carries the 9 and the 10 raised at 7pt while the comma
    between them is the body font at 11pt, unraised.  Demanding that the separator be
    raised too would let the document deny its own citation."""
    chars, bits = oracle
    context = re.sub(r"\s+", "", line[max(0, start - _SUP_ORACLE_CONTEXT):start])
    if len(context) < 2:
        return False
    key = context + digits
    digit_offsets = [j for j, ch in enumerate(digits) if ch.isdigit()]
    denied = False
    i = chars.find(key)
    if i < 0:
        return False
    while i >= 0:
        digit_at = i + len(key) - len(digits)
        if all(bits[digit_at + j] for j in digit_offsets):
            return False
        denied = True
        i = chars.find(key, i + 1)
    return denied


def _oracle_confirms_superscript(oracle, line: str, start: int, digits: str) -> bool:
    """True when the PDF says these digits are raised and the glyph before them is not.

    The mirror image of the veto, and the only place the oracle is allowed to ADD a
    marker rather than remove one — because it is the only place where the text cannot
    even form the question.  The heuristic requires a marker to sit directly behind a
    letter or a punctuation mark, and that rule is what keeps "27.1" and "10-5" out, so
    where the typesetting takes a different shape it never proposes the candidate at all
    and the veto never gets to see it.  Two such shapes reach us::

        running CheckM v1.2.2³¹ on all the genomes   ->   ...CheckM v1.2.231 on all...
        concentrated in the past decades¹˒²          ->   ...past decades 1 , 2. Across...

    In the first the marker is glued to a version number and arrives as one run of
    digits; in the second the group is spaced out and left detached from the word it
    belongs to.  The glyphs know exactly what happened in both: the digits are raised,
    what precedes them is not.

    Demanding that the preceding glyph be UNRAISED does not by itself rule out an
    exponent ("10⁶" raises a digit after a flat one too), so the caller that can meet
    one must still rule it out on its own: a version is a word with digits in it, an
    exponent is only digits.  Every occurrence in the document must agree; one that
    contradicts is enough to leave the digits alone."""
    chars, bits = oracle
    context = re.sub(r"\s+", "", line[max(0, start - _SUP_ORACLE_CONTEXT):start])
    if len(context) < 2:
        return False
    key = context + digits
    digit_offsets = [j for j, ch in enumerate(digits) if ch.isdigit()]
    i = chars.find(key)
    if i < 0:
        return False
    confirmed = False
    while i >= 0:
        digit_at = i + len(key) - len(digits)
        raised = all(bits[digit_at + j] for j in digit_offsets)
        base_is_flat = digit_at > 0 and not bits[digit_at - 1]
        if not (raised and base_is_flat):
            return False
        confirmed = True
        i = chars.find(key, i + 1)
    return confirmed


def _date_carries_its_note(base: str, group: str, notes) -> bool:
    """Is this a date with a footnote welded to it, rather than a power?

    "…the United States in 1795145 and the United Kingdom in 1871146 providing for free
    navigation…" — two treaty dates, each carrying its note.  Extraction glues them into
    one run of digits, and the glued pass declines a base with no letters in it because
    that is exactly the shape of an exponent: "10⁶" is a flat base under raised digits
    too, and the glyphs cannot tell the two apart.  Nor can they be told apart by how
    the paper cites: an exponent is not a citation style, it is arithmetic, and it lives
    happily alongside any of them.

    What tells them apart is the document's own footnote apparatus.  Reading a note
    number off the page is not a guess about the document, it is the document — the same
    kind of evidence as the raised bits — and a paper that PRINTS a note 145 at the foot
    of a page is not raising 1795 to the 145th power.  So three things must hold, and
    all three are read, not assumed:

      * the document has a numbered apparatus at all (a physics paper has none, so this
        can never fire there — which is where the exponents live);
      * every number offered is one that apparatus actually defines;
      * the base is a DATE, not a quantity.  This is what keeps "10⁵" out of a medical
        paper that has both five footnotes and exponents — fmed is exactly that — and it
        costs nothing, because a date is the only thing this shape has ever turned out
        to be.  Nobody raises a year to a power.
    """
    if not notes:
        return False
    digits = re.sub(r"\s+", "", group)
    if not base.endswith(digits):
        return False
    stem = base[:-len(digits)]
    if not (len(stem) == 4 and stem.isdigit() and 1000 <= int(stem) <= 2099):
        return False
    nums = [int(x) for x in re.findall(r"\d{1,3}", digits)]
    return bool(nums) and all(n in notes for n in nums)


def _convert_pdf_superscripts(body: str, oracle=None, notes=None) -> str:
    """Convert bare numbers in PDF-extracted body text into ⟦SUP:n⟧ markers.

    *notes* is the document's footnote apparatus (a mapping keyed by note number), or
    None where it has none; see _date_carries_its_note for the one thing it decides.
    """
    lines = body.split("\n")
    converted: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"\d{1,3}", stripped):
            converted.append(line)
            continue
        # A line opening on a numbered thing — "14.16 of the US-Mexico-Canada
        # Agreement", "3.1 Introduction" — must not read that number as a marker.
        # Only that number: the rule used to drop the WHOLE line, and a line is not
        # a unit of meaning, it is where the type happened to wrap.  GroJIL breaks a
        # sentence mid-clause ("…is Article / 14.16 of the … (USMCA).80 This…"), so an
        # article number at the margin silently took a footnote sixty characters away
        # down with it — a marker the rules below read correctly the moment they are
        # allowed to see it.  Guard the span, not the line.
        lead = re.match(r"^\d{1,2}\.\d+", stripped)
        guard_end = (line.index(stripped) + lead.end()) if lead else 0

        def _maybe_sup(m: "re.Match") -> str:
            if m.start(1) < guard_end:
                return m.group(0)
            group = m.group(1)
            nums = [int(x) for x in re.findall(r"\d{1,3}", group)]
            if not nums:
                return m.group(0)
            for n in nums:
                if n < 1 or 1900 <= n <= 2099:
                    return m.group(0)
            start = m.start(1)
            # 150 is a guess about how many notes a paper may have, and a law review
            # settles it: 7-135-krishnamurthy carries 418, so the ceiling alone made
            # every marker past 150 unreadable and left 268 of its references cited by
            # nobody — the parser deciding the document was wrong about its own
            # footnotes.  The number cannot be raised to a better guess either; what a
            # count is plausible depends on the document, and the document is what the
            # glyphs report.  So above the ceiling the text no longer decides: the
            # digits must be RAISED for the marker to stand.  (Below it nothing
            # changes — the ceiling was never what kept ordinary prose numbers out;
            # the lookbehind and the guards below are.)
            if any(n > 150 for n in nums):
                if not (oracle is not None and _oracle_confirms_superscript(
                        oracle, line, start, re.sub(r"\s+", "", group))):
                    return m.group(0)
            if start >= 1 and line[start - 1] == ".":
                # The digits sit right after a dot — which is the shape of BOTH an AMA
                # marker ("…in all cases.35 ILC is…") and a number written without its
                # leading zero, the house style for P values ("P = .35", "P < .001").
                # What separates them is what the dot is attached to.  A sentence-ending
                # dot terminates a word, so a LETTER or a closing bracket/quote comes
                # before it.  A decimal point belongs to a number, and sits either after
                # its integer part ("0.5", "27.1") or after nothing at all when the
                # leading zero is dropped ("P = .35") — and in a table the P value is a
                # cell of its own, ".35" with an empty line to its left, which is why
                # this rule and not the glyph oracle catches it: the oracle anchors on
                # left context, and there is none to anchor on.
                prev = line[start - 2] if start >= 2 else ""
                if not prev or not (prev.isalpha() or prev in ")]}”’\"'"):
                    # A sentence can END on a number, and then this shape is neither a
                    # decimal nor a P value: "…ISDS cases reached 1,332 in 2023.16 This
                    # ISDS case count…" is a year, a full stop, and a footnote marker.
                    # A law review is full of them — years, articles, dates — and the
                    # text cannot tell that from "P = .35" because the two are spelled
                    # the same.  The glyphs can, so ask before dropping it: in the year
                    # the digits are raised off a flat dot, in the P value they are not.
                    # (The table cell ".35" stays out either way — it has no left
                    # context, so the oracle declines to answer at all.)
                    if not (oracle is not None and _oracle_confirms_superscript(
                            oracle, line, start, re.sub(r"\s+", "", group))):
                        return m.group(0)
            end = m.end(1)
            if re.match(r"\.\d", line[end:end + 2]):
                return m.group(0)
            # Clock-time field ("07:46:51"): a number flanked by a ':' and a
            # digit is an hour/minute/second, not a citation marker.  (The IP in
            # the same download-stamp line, "212.210.15.146", is already caught
            # by the decimal guards above.)
            if start >= 2 and line[start - 1] == ":" and line[start - 2].isdigit():
                return m.group(0)
            if line[end:end + 1] == ":" and line[end + 1:end + 2].isdigit():
                return m.group(0)
            mb = re.search(r"([A-Za-z]+)$", line[:start])
            if mb:
                tok = mb.group(1)
                if len(tok) == 1:
                    return m.group(0)
                if tok.isupper() and len(tok) <= 3:
                    # A short acronym with digits welded on is how a name is spelled —
                    # "BRCA2", "HIV1" — so the digits belong to the word.  But a field
                    # whose vocabulary IS acronyms spells a citation the same way: the
                    # law review's "Cabo Verde-Hungary BIT98" and "Sri Lanka-Singapore
                    # FTA93" are a treaty and its footnote, not an identifier.  The two
                    # are the same string; only the glyphs differ, so let them decide —
                    # in the treaty the digits are raised off a flat "T", in BRCA2 they
                    # are not raised at all.
                    if not (oracle is not None and _oracle_confirms_superscript(
                            oracle, line, start, re.sub(r"\s+", "", group))):
                        return m.group(0)
                if tok.lower() in _SUP_FORMULA_PREFIXES:
                    return m.group(0)
            if start >= 1 and line[start - 1] == ")":
                content = _paren_content_before(line, start - 1)
                if content is not None and re.search(r"[=+*/^±×·−]", content):
                    return m.group(0)
            before = line[max(0, start - 10):start]
            if re.search(rf"\b{_SUP_CONTEXT_WORDS}\.?\s*$", before, re.I):
                return m.group(0)
            norm = re.sub(r"\s+", "", group)
            # Last word to the document itself, which is the only source here that
            # KNOWS rather than infers.  It overrules every guess above — including a
            # "yes" — because the guesses read plain text and it reads the glyphs.
            if oracle is not None and _oracle_denies_superscript(oracle, line, start, norm):
                return m.group(0)
            return f"⟦SUP:{norm}⟧"

        # What a marker may sit behind: a word, or the punctuation that closes one.
        # "?" and "!" end a sentence exactly as "." does — krishnamurthy asks "…to each
        # of the other Member States”?115 Should the only recourse…" — and their absence
        # here was not a judgement that a question takes no footnote, only that nobody
        # had written a question yet.  The guards below are unchanged and still run; this
        # decides which candidates get to be considered at all.
        line = re.sub(
            r"(?<=[A-Za-z.,;:?!)”’\"'])"
            r"(?![A-Za-z])"
            rf"(\d{{1,3}}(?:\s*[,{_DASH}]\s*\d{{1,3}})*)"
            r"(?!\d)"
            r"(?![A-Za-z])",
            _maybe_sup,
            line,
        )

        # Second pass, and the only one that can ADD a marker: the citation glued to the
        # end of a version number ("CheckM v1.2.2³¹" -> "CheckM v1.2.231").  The rule
        # above requires a marker to follow a letter or a punctuation mark — that rule is
        # what keeps "27.1" and "10-5" out, and it stays — so it cannot even propose
        # these digits, and the veto never sees them.  Only the glyphs can settle it, so
        # only the glyphs are asked, and no glyphs means no marker.
        glued = line
        if oracle is not None:

            def _maybe_glued_sup(m: "re.Match") -> str:
                group = m.group(1)
                start = m.start(1)
                # An exponent is raised after an unraised digit too ("3×10⁶"), so the
                # glyphs alone cannot tell it from a citation.  What tells them apart is
                # the base the digits are attached to: a version or an identifier is a
                # WORD with digits in it, an exponent hangs off a bare quantity.
                #
                # The base is what follows the last operator, not the whole token: in
                # "n=2³" the letter is on the far side of the "=" and belongs to the
                # variable, not to the base — the digits are raised off a plain 2, and
                # that is an exponent.  A hyphen is NOT an operator here: it is how a
                # name is spelled ("SARS-CoV-2³¹", "HIV-1⁴"), and cutting there would
                # throw away a real citation.
                #
                # A base with no letter at all can still be an identifier rather than a
                # quantity, and its second dot says so: "version 4.0.0⁴⁴" is a release,
                # nothing is ever raised to the power of "4.0.0".  A quantity has one
                # dot ("0.5²") or none ("10⁶"), which is what keeps the exponent out.
                token = glued[glued.rfind(" ", 0, start) + 1:m.end(1)]
                base = _MATH_OPERATOR_RE.split(token)[-1]
                if not any(c.isalpha() for c in base) and base.count(".") < 2:
                    # ...unless the base is a date and the apparatus defines the note it
                    # carries, which no exponent can claim.
                    if not _date_carries_its_note(base, group, notes):
                        return m.group(0)
                # The version and the marker reach us as ONE run of digits, and the run
                # is read greedily: "FastANI v1.33³⁸" arrives as "…v1.3338", whose first
                # three digits, "338", are no citation number at all — so the marker is
                # never offered to the oracle and the citation is lost in silence.
                # WHERE the raised digits start is precisely what the document knows, so
                # when the whole run does not read as a marker, offer it the shorter
                # suffixes too.  Each is still confirmed glyph by glyph, and the digit in
                # front of it must still be flat, so a wrong split cannot be confirmed.
                for offset in range(len(group)):
                    cand = group[offset:]
                    if not cand[0].isdigit():
                        continue
                    nums = [int(x) for x in re.findall(r"\d{1,3}", cand)]
                    if not nums or any(n < 1 or n > 150 or 1900 <= n <= 2099 for n in nums):
                        continue
                    norm = re.sub(r"\s+", "", cand)
                    if _oracle_confirms_superscript(oracle, glued, start + offset, norm):
                        return group[:offset] + f"⟦SUP:{norm}⟧"
                return m.group(0)

            line = re.sub(
                rf"(?<=\d)(\d{{1,3}}(?:\s*[,{_DASH}]\s*\d{{1,3}})*)(?!\d)(?![A-Za-z])",
                _maybe_glued_sup,
                glued,
            )

        converted.append(line)

    body = "\n".join(converted)
    if oracle is not None and _cites_by_superscript(body):
        body = _convert_detached_superscripts(body, oracle)
    return body


# A citation set between brackets, which is the other way a numbered reference can be
# cited: "[12]", "[3, 4]", "[7–9]".
_BRACKET_MARKER_RE = re.compile(rf"\[\s*\d{{1,3}}(?:\s*[,{_DASH}]\s*\d{{1,3}})*\s*\]")


def _cites_by_superscript(body: str) -> bool:
    """Does this document cite by raising the number, or by bracketing it?

    A paper cites one way.  Where the brackets do the citing, a raised digit is a
    FOOTNOTE — "…extremely small gradients⁴" in Attention points at a note explaining
    the dot products, not at reference [4] — and reading it as a citation does not
    merely miss something, it fabricates a citation that the paper never made and hangs
    a claim on it.  Nothing downstream can catch that: the reference counts as cited,
    the coverage looks perfect, and the verdict is passed on a sentence the authors
    never wrote about that source.

    So the detached rule below is offered only to documents that demonstrably cite by
    raising the number: at least a few markers the strict rule already found (one raised
    digit is a footnote; a citation style repeats), and more of them than brackets."""
    raised = body.count("⟦SUP:")
    return raised >= 3 and raised > len(_BRACKET_MARKER_RE.findall(body))


def _convert_detached_superscripts(body: str, oracle) -> str:
    """The marker the typesetter left DETACHED from its word.

    A spaced-out group of raised digits extracts as plain text with the spaces kept::

        …with intensification concentrated in the past decades¹˒²
        ->  …with intensification concentrated in the past decades 1 , 2.

    The strict rule wants the marker directly behind the word it belongs to — that rule
    is what keeps "27.1" and "10-5" out, and it stays — so it proposes nothing here and
    the citation is lost in silence: no orphan is raised, the reference simply comes out
    "never cited", and only the coverage ever says so.

    Nothing in the text can settle this: " 1 , 2" reads exactly like a stray number, and
    a document numbering its lines in the margin puts stray numbers mid-sentence all day
    ("…concentrated 3 in the past decades").  Only the glyphs can, so only they are
    asked — a line number, set flat in the body font, is refused by the same question
    that accepts the marker.

    A footnote's own number is raised too, and a Word-exported PDF prints its notes in
    the middle of the body text.  What tells the two apart is where each sits: the note
    number OPENS a line, a citation marker never does.  The word this rule requires in
    front of the digits is that distinction."""
    out: list[str] = []
    for line in body.split("\n"):

        def _maybe_detached_sup(m: "re.Match", line=line) -> str:
            group = m.group(1)
            start = m.start(1)
            nums = [int(x) for x in re.findall(r"\d{1,3}", group)]
            if not nums or any(n < 1 or n > 150 or 1900 <= n <= 2099 for n in nums):
                return m.group(0)
            norm = re.sub(r"\s+", "", group)
            if not _oracle_confirms_superscript(oracle, line, start, norm):
                return m.group(0)
            return f"⟦SUP:{norm}⟧"  # the space goes: the marker belongs to the word

        out.append(re.sub(
            rf"(?<=[A-Za-z)”’\"'])[ \t]+(\d{{1,3}}(?:\s*[,{_DASH}]\s*\d{{1,3}})*)(?!\d)(?![A-Za-z])",
            _maybe_detached_sup,
            line,
        ))
    return "\n".join(out)


def _split_body_bibliography(
    text: str, *, allow_headerless: bool = True,
) -> tuple[str, int | None, list[str]]:
    """Body text, the bibliography boundary index, and bibliography lines.

    Text the PDF leaves inside the reference region but that is not a reference —
    ViLT's appendix past the last entry, the column of Nature's Methods dropped in
    the middle of the list — belongs to the body, and it has to come back here,
    *before* the caller reads the superscripts: raised digits are converted on the
    body only, so text left behind keeps its citations as bare digits, which no
    marker pattern can see.  That is a citation lost in silence, and the references
    it cites read as "never cited".

    This is the PDF's split, and the defect is the PDF's: a two-column page has a
    reading order, and the extractor guesses at it.  Plain text has no columns.
    """
    lines = text.split("\n")
    cut_idx = parsing_common._select_biblio_heading_index(lines)
    headerless = cut_idx is None
    if cut_idx is None:
        if not allow_headerless:
            return text, None, lines
        start_idx = parsing_common._headerless_bibliography_start_index(lines)
        if start_idx is None:
            return text, None, lines
        boundary_idx = start_idx
        body_prefix = lines[:start_idx]
        biblio_heading = []
        biblio = lines[start_idx:]
    else:
        boundary_idx = cut_idx
        body_prefix = lines[:cut_idx]
        biblio_heading = [lines[cut_idx]]
        biblio = lines[cut_idx + 1:]
    opaque_keyed_state = parsing_common._opaque_keyed_spine_status("\n".join(biblio))
    end = parsing_common._find_biblio_end_index(
        biblio,
        opaque_keyed_spine=opaque_keyed_state is not None,
    )
    tail = []
    if end is not None:
        biblio, tail = biblio[:end], biblio[end:]

    interposed: list[str] = []
    spans = (
        parsing_common._headerless_interposed_body_spans(biblio)
        if headerless
        else parsing_common._interposed_body_spans(biblio)
    )
    if spans:
        foreign = {i for first, last in spans for i in range(first, last + 1)}
        interposed = [biblio[i] for i in sorted(foreign)]
        biblio = [ln for i, ln in enumerate(biblio) if i not in foreign]

    body_lines = body_prefix + interposed + tail
    return "\n".join(body_lines), boundary_idx, biblio_heading + biblio


def _columnaware_page_bands(
    blocks, page_width: float, *, minimum_bottom_y: float | None = None,
):
    """Infer text-column x bands from aligned paragraph blocks and positive gutters.

    Wide spanning blocks and short figure labels are not column evidence.  Returning
    ``None`` means the remaining paragraph bands overlap, so their reading order is
    ambiguous; an empty list means the page has no detectable multi-column text.
    """
    min_width = page_width * 0.17
    max_width = page_width * 0.49
    start_tolerance = max(8.0, page_width * 0.035)
    candidates = []
    for block in blocks:
        try:
            x0, _y0, x1, y1 = map(float, block[:4])
            text = " ".join(str(block[4] or "").split())
        except (IndexError, TypeError, ValueError):
            continue
        if minimum_bottom_y is not None and y1 < minimum_bottom_y:
            continue
        width = x1 - x0
        if min_width <= width <= max_width and len(text) >= 32:
            candidates.append((x0, x1))
    if not candidates:
        return []

    candidates.sort()
    clusters: list[list[tuple[float, float]]] = []
    for interval in candidates:
        if (clusters
                and interval[0] - clusters[-1][0][0] <= start_tolerance):
            clusters[-1].append(interval)
        else:
            clusters.append([interval])
    bands = [
        (min(x0 for x0, _x1 in cluster), max(x1 for _x0, x1 in cluster))
        for cluster in clusters
    ]
    bands.sort()
    min_gutter = max(1.0, page_width * 0.003)
    for previous, following in zip(bands, bands[1:]):
        if following[0] - previous[1] < min_gutter:
            return None
    return bands


def _columnaware_block_band(block, bands, page_width: float) -> int | None:
    """Return the unique text band containing a non-spanning block."""
    try:
        x0, _y0, x1, _y1 = map(float, block[:4])
    except (IndexError, TypeError, ValueError):
        return None
    tolerance = max(8.0, page_width * 0.035)
    if x1 <= x0 or x1 - x0 > page_width * 0.49:
        return None
    matches = [
        index for index, (left, right) in enumerate(bands)
        if left - tolerance <= x0 <= left + tolerance and x1 <= right + tolerance
    ]
    return matches[0] if len(matches) == 1 else None


def _columnaware_start_band(block, bands, page_width: float) -> int | None:
    """Locate a block's aligned start even when it crosses a column gutter."""
    try:
        x0 = float(block[0])
    except (IndexError, TypeError, ValueError):
        return None
    tolerance = max(8.0, page_width * 0.035)
    matches = [
        index for index, (left, _right) in enumerate(bands)
        if left - tolerance <= x0 <= left + tolerance
    ]
    return matches[0] if len(matches) == 1 else None


def _columnaware_has_bibliography_end(parts: list[str]) -> bool:
    """Use the normal bibliography end detector to stop before unrelated pages."""
    lines = "\n".join(parts).splitlines()
    heading = parsing_common._select_biblio_heading_index(lines)
    if heading is None:
        return False
    region = lines[heading + 1:]
    opaque = parsing_common._opaque_keyed_spine_status("\n".join(region))
    return parsing_common._find_biblio_end_index(
        region, opaque_keyed_spine=opaque is not None,
    ) is not None


def _columnaware_prefix_source_anchor(
    blocks, declared_document_doi: str | None,
) -> bool:
    """Return whether a pre-heading column prefix contains source evidence.

    A DOI or URL is source evidence unless every DOI token is the unique
    front-matter DOI and each URL carrying it repeats that same identifier.
    This exception is only considered after a separate end-matter proof exists.
    """
    text = "\n".join(str(block[4] or "") for block in blocks)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if parsing_common._region_has_bibliography_entry(lines):
        return True
    if re.search(r"\bISBN\b", text, re.I):
        return True
    evidence_text = re.sub(
        r"((?:https?://|www\.)[^\r\n]*[./])\s*[\r\n]+\s*"
        r"([A-Z0-9][A-Z0-9./#?=&%+_-]*)",
        r"\1\2",
        text,
        flags=re.I,
    )

    expected = (
        declared_document_doi.strip().rstrip(".,;:)]}").casefold()
        if isinstance(declared_document_doi, str) else ""
    )
    doi_re = r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+"
    doi_tokens = [
        match.group(0).rstrip(".,;:)]}").casefold()
        for match in re.finditer(doi_re, evidence_text, re.I)
    ]
    url_re = r"(?:https?://|www\.)\S+"
    urls = re.findall(url_re, evidence_text, re.I)
    if urls:
        if not expected:
            return True
        outside_urls = re.sub(url_re, " ", evidence_text, flags=re.I)
        outside_dois = [
            match.group(0).rstrip(".,;:)]}").casefold()
            for match in re.finditer(doi_re, outside_urls, re.I)
        ]
        if any(token != expected for token in outside_dois):
            return True
        for url in urls:
            if (
                not re.search(re.escape(expected) + r"(?=$|[/?#])", url, re.I)
                or len(re.findall(r"\b10\.\d{4,9}/", url, re.I)) != 1
            ):
                return True
    elif doi_tokens and (
        not expected
        or any(token != expected for token in doi_tokens)
    ):
        return True
    if re.search(r"\bdoi\s*:", evidence_text, re.I) and not doi_tokens:
        return True
    text_without_doi = re.sub(
        doi_re, " ", evidence_text, flags=re.I,
    )
    signal_text = re.sub(url_re, " ", text_without_doi, flags=re.I)
    if parsing_common._has_biblio_signal(signal_text):
        return True
    return False


def _columnaware_endmatter_heading_proof(blocks) -> bool:
    """Require multiple exact post-bibliography headings followed by prose."""
    headings = parser_text._POST_BIBLIO_HEADINGS

    def normalize(line: str) -> str:
        value = unicodedata.normalize("NFKC", line)
        value = re.sub(r"\s+", " ", value).strip().casefold()
        return value.rstrip(" .:;")

    lines = [
        line.strip()
        for block in sorted(blocks, key=lambda item: (float(item[1]), float(item[0])))
        for line in str(block[4] or "").splitlines()
        if line.strip()
    ]
    normalized = [normalize(line) for line in lines]
    proved = set()
    for index, heading in enumerate(normalized):
        if heading not in headings:
            continue
        for following in normalized[index + 1:]:
            if following in headings:
                break
            if parsing_common._entry_start_num(following) is not None:
                break
            words = re.findall(r"[^\W_]+", following, re.UNICODE)
            if len(words) >= 3:
                proved.add(heading)
            break
    return len(proved) >= 2


def _columnaware_later_band_prefix_decision(
    prefix_blocks,
    suffix_blocks,
    prior_parts: list[str],
    heading_y: float,
    bands,
    band_index: int,
    page_width: float,
    declared_document_doi: str | None = None,
) -> str:
    """Classify a later-column heading-page prefix without deleting ambiguity.

    The returned reason is intentionally private but stable enough for focused
    regression tests to distinguish an unsupported layout from a source-anchor
    veto.
    """
    if not prefix_blocks:
        return "no_prefix"

    has_source_anchor = _columnaware_prefix_source_anchor(
        prefix_blocks, declared_document_doi,
    )
    has_endmatter = _columnaware_endmatter_heading_proof(prefix_blocks)
    if has_source_anchor and has_endmatter:
        return "source_anchor_veto"
    if has_source_anchor:
        return "preserve_source_prefix"
    if not has_endmatter:
        return "ambiguous_prefix"

    # A block crossing the heading plane cannot be split without losing text.
    if any(
        float(block[1]) < heading_y <= float(block[3])
        for block in prefix_blocks
    ):
        return "ambiguous_crossing_block"
    if not suffix_blocks:
        return "unsupported_resume"

    ordered_suffix = sorted(
        suffix_blocks, key=lambda block: (float(block[1]), float(block[0])),
    )
    if _columnaware_block_band(
        ordered_suffix[0], bands, page_width,
    ) != band_index:
        return "unaligned_resume"

    suffix_lines = [
        line.strip()
        for block in ordered_suffix
        for line in str(block[4] or "").splitlines()
        if line.strip()
    ]
    first_block_lines = [
        line.strip()
        for line in str(ordered_suffix[0][4] or "").splitlines()
        if line.strip()
    ]
    first_block_text = "\n".join(first_block_lines)
    first_block_supported = (
        parsing_common._region_has_bibliography_entry(first_block_lines)
        or parsing_common._has_biblio_signal(first_block_text)
    )
    suffix_supported = (
        parsing_common._region_has_bibliography_entry(suffix_lines)
        or parsing_common._has_biblio_signal("\n".join(suffix_lines))
    )
    if not first_block_supported or not suffix_supported:
        return "unsupported_resume"

    if parsing_common._region_has_sustained_bibliography_spine(suffix_lines):
        return "trim_endmatter_prefix"

    prior_numbers = [
        number
        for part in prior_parts
        for line in part.splitlines()
        if (number := parsing_common._entry_start_num(line.strip())) is not None
    ]
    suffix_numbers = [
        number for line in suffix_lines
        if (number := parsing_common._entry_start_num(line)) is not None
    ]
    if (
        prior_numbers
        and len(suffix_numbers) >= 2
        and suffix_numbers[0] == prior_numbers[-1] + 1
        and all(
            following == previous + 1
            for previous, following in zip(suffix_numbers, suffix_numbers[1:])
        )
    ):
        return "trim_endmatter_prefix"
    return "unsupported_resume"


def _columnaware_block_is_at_page_margin(block, page) -> bool:
    """Return whether a block touches a page edge where furniture is expected."""
    try:
        x0, y0, x1, y1 = map(float, block[:4])
        width, height = float(page.rect.width), float(page.rect.height)
    except (IndexError, TypeError, ValueError, AttributeError):
        return False
    return (
        x1 <= width * 0.06 or x0 >= width * 0.94
        or y1 <= height * 0.05 or y0 >= height * 0.92
    )


def _columnaware_margin_block_is_recognized_furniture(
    block, page, furniture_lines,
) -> bool:
    """Drop only a margin block whose every nonempty line is known furniture."""
    if not _columnaware_block_is_at_page_margin(block, page):
        return False
    lines = [line.strip() for line in str(block[4] or "").splitlines() if line.strip()]
    if not lines:
        return True
    return all(line in furniture_lines for line in lines)


def _columnaware_has_declared_document_doi_seed(
    text: str, declared_document_doi: str | None,
) -> bool:
    """Recognize a repeated header seed only from one declared manuscript DOI.

    A DOI is source evidence by default.  It can seed page-furniture proof only
    when every DOI token is the same front-matter identifier and the block has
    no independent bibliography-entry, ISBN, or URL evidence.
    """
    if not isinstance(declared_document_doi, str):
        return False
    expected = declared_document_doi.strip().rstrip(".,;:)]}").casefold()
    if not re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", expected, re.I):
        return False

    doi_tokens = [
        match.group(0).rstrip(".,;:)]}").casefold()
        for match in re.finditer(
            r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I,
        )
    ]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if (
        not doi_tokens
        or any(token != expected for token in doi_tokens)
        or re.search(r"\bISBN\b|https?://|\bwww\.", text, re.I)
        or parsing_common._region_has_bibliography_entry(lines)
    ):
        return False
    return True


def _columnaware_repeated_margin_furniture_blocks(
    page_blocks, pages, furniture_lines, declared_document_doi: str | None = None,
) -> list[set[int]]:
    """Prove repeated page-edge blocks from stable layout and a furniture seed.

    A complete non-counter text shape must recur at the same normalized page
    bounds on at least three pages.  A varying counter, an existing furniture
    line, or a source-free wide top-margin block seeds the proof.
    A DOI-bearing block additionally needs a unique,
    exact match to the manuscript's bounded front-matter DOI and must contain
    no source-entry or other identifier evidence.
    """
    try:
        from core.parse import boilerplate as _boilerplate
    except ImportError:  # pragma: no cover - direct-execution import path
        import boilerplate as _boilerplate

    def normalize_line(line: str) -> str:
        value = unicodedata.normalize("NFKC", line)
        value = re.sub(r"\s+", " ", value).strip()
        if "|" in value:
            # Running heads may reverse field order on alternating pages.
            fields = sorted(part.strip() for part in value.split("|") if part.strip())
            return " | ".join(fields)
        return value

    normalized_furniture = {normalize_line(line) for line in furniture_lines}
    groups: dict[tuple[tuple[float, ...], tuple[str, ...]], dict[int, list[int]]] = {}
    for page_index, (blocks, page) in enumerate(zip(page_blocks, pages)):
        width, height = float(page.rect.width), float(page.rect.height)
        for block_index, block in enumerate(blocks):
            if not _columnaware_block_is_at_page_margin(block, page):
                continue
            try:
                x0, y0, x1, y1 = map(float, block[:4])
                text = str(block[4] or "")
            except (IndexError, TypeError, ValueError):
                continue
            lines = [normalize_line(line) for line in text.splitlines() if line.strip()]
            if not lines:
                continue

            counter_lines = [
                line for line in lines
                if (_boilerplate._PAGE_FURNITURE_RE.match(line)
                    or _boilerplate._BARE_PAGE_FURNITURE_RE.match(line)
                    or _boilerplate._PAGE_NUMBER_RE.match(line))
            ]
            known_furniture_seed = any(line in normalized_furniture for line in lines)
            document_doi_seed = _columnaware_has_declared_document_doi_seed(
                text, declared_document_doi,
            )
            # A full-width repeated running title can lack a page counter and
            # use mixed case.  Its complete block must sit above the body, span
            # both page edges, and contain no independent source evidence.
            wide_top_header_seed = (
                y1 <= height * 0.05
                and x0 <= width * 0.10 and x1 >= width * 0.90
                and not re.search(
                    r"\b10\.\d{4,9}/\S+|\bdoi\s*:|\bISBN\b|https?://|\bwww\.",
                    text, re.I,
                )
                and not parsing_common._has_biblio_signal(text)
            )
            if not (
                counter_lines or known_furniture_seed
                or document_doi_seed or wide_top_header_seed
            ):
                continue

            # Counter values vary by page; every other nonempty line must match in
            # its original order as part of the same complete block shape.
            shape = tuple(line for line in lines if line not in counter_lines)
            if not shape:
                continue

            residual = _boilerplate.strip_boilerplate(text, furniture_lines)
            residual_lines = [line.strip() for line in residual.splitlines() if line.strip()]
            if parsing_common._region_has_bibliography_entry(residual_lines):
                continue
            if document_doi_seed:
                # The dedicated seed validator above has already required every
                # DOI to match the declared manuscript DOI and rejected source,
                # ISBN, and URL evidence in the whole block.
                pass
            elif re.search(
                r"\b10\.\d{4,9}/\S+|\bdoi\s*:|\bISBN\b|https?://",
                residual, re.I,
            ):
                continue

            bounds = (
                round(x0 / width, 3), round(y0 / height, 3),
                round(x1 / width, 3), round(y1 / height, 3),
            )
            key = (bounds, shape)
            groups.setdefault(key, {}).setdefault(page_index, []).append(block_index)

    proven = [set() for _page in page_blocks]
    for occurrences in groups.values():
        if len(occurrences) < 3:
            continue
        for page_index, block_indexes in occurrences.items():
            proven[page_index].update(block_indexes)
    return proven


def _columnaware_bibliography_candidate(
    path: str, *, declared_document_doi: str | None = None,
) -> tuple[str, bool] | None:
    """Return column-ordered bibliography-page text and a three-column proof.

    The proof is deliberately geometric; the caller still requires at least five
    parsed entries before it can bypass the legacy count-improvement guard.
    """
    try:
        import pymupdf as fitz
    except Exception:
        return None
    try:
        from core.fetch.extraction.pdf import page_blocks_reader
    except Exception:
        page_blocks_reader = None
    try:
        doc = fitz.open(path)
    except Exception:
        return None
    try:
        # A producer that scatters phantom spaces through its words breaks this
        # path exactly as it breaks the default one, and the damage lands where it
        # hurts most: a reference entry reading "coho rt Oto mo rpha. Ne ot ropic
        # Ichthyol" matches no work, and a correct DOI beside it gets downgraded to
        # "points elsewhere" because the cited title cannot be read.  Rebuild from
        # the glyphs here too, on the same test the default extraction uses — the
        # repair belongs to the document, not to one route through it.
        read_blocks = (page_blocks_reader(doc) if page_blocks_reader is not None
                       else (lambda page: page.get_text("blocks")))

        pages = [doc[pno] for pno in range(doc.page_count)]
        page_blocks = [
            [b for b in read_blocks(page) if (b[4] or "").strip()]
            for page in pages
        ]
        try:
            from core.parse import boilerplate as _boilerplate
        except ImportError:  # pragma: no cover - direct-execution import path
            import boilerplate as _boilerplate
        # Shared multilingual heading detection (tolerates a leading glyph like
        # "■References", ACS).
        header_re = parser_text.bibliography_heading_re()
        start = None
        start_blocks = None
        heading_block_index = None
        heading_match = None
        for pno in range(doc.page_count):
            page = pages[pno]
            if not header_re.search(page.get_text("text")):
                continue
            blocks = page_blocks[pno]
            for block_index, block in enumerate(blocks):
                match = header_re.search(str(block[4] or ""))
                if match:
                    start = pno
                    start_blocks = blocks
                    heading_block_index = block_index
                    heading_match = match
                    break
            if start is not None:
                break
        if start is None:
            return None
        # The broad all-caps running-head detector is only decisive within the
        # pages containing the bibliography.  Whole-document use could promote
        # a repeated body label and erase a one-off source block at the margin.
        bibliography_page_text = "\n".join(
            str(block[4])
            for blocks in page_blocks[start:]
            for block in blocks
        )
        all_page_text = "\n".join(
            str(block[4]) for blocks in page_blocks for block in blocks
        )
        furniture_lines = (
            _boilerplate.running_head_lines(bibliography_page_text)
            | _boilerplate.boilerplate_lines(all_page_text)
        )
        repeated_margin_furniture = _columnaware_repeated_margin_furniture_blocks(
            page_blocks, pages, furniture_lines, declared_document_doi,
        )
        parts = []
        has_three_column_bibliography = False
        for pno in range(start, doc.page_count):
            page = doc[pno]
            blocks = start_blocks if pno == start else page_blocks[pno]
            if not blocks:
                continue
            heading_y = (
                float(blocks[heading_block_index][1])
                if pno == start and heading_block_index is not None else None
            )
            bands = _columnaware_page_bands(
                blocks, float(page.rect.width),
                minimum_bottom_y=heading_y,
            )
            if bands is None:
                return None

            if pno == start:
                # This route is useful only when the heading sits in a detected
                # multi-column page.  Locate its exact block, then start there in
                # physical column order.  Later-column prefixes are classified
                # separately because they may contain references or back matter.
                if len(bands) < 2 or heading_block_index is None or heading_match is None:
                    return None
                heading_block = blocks[heading_block_index]
                if heading_y is None:
                    return None
                heading_band = _columnaware_block_band(
                    heading_block, bands, float(page.rect.width),
                )
                if heading_band is None:
                    return None
                used_bands = set()
                assigned_blocks = {
                    block_index
                    for block_index, block in enumerate(blocks)
                    if _columnaware_block_band(
                        block, bands, float(page.rect.width),
                    ) is not None
                }
                for band_index in range(heading_band, len(bands)):
                    current = []
                    for block_index, block in enumerate(blocks):
                        block_band = _columnaware_block_band(
                            block, bands, float(page.rect.width),
                        )
                        if block_band != band_index:
                            continue
                        if band_index == heading_band:
                            if block_index == heading_block_index:
                                block_text = str(block[4] or "")[heading_match.start():]
                            elif float(block[1]) < float(heading_block[1]):
                                continue
                            else:
                                block_text = str(block[4] or "")
                        else:
                            block_text = str(block[4] or "")
                        if block_text.strip():
                            current.append((float(block[1]), float(block[0]), block_text))
                    if band_index > heading_band:
                        band_blocks = [
                            block for block in blocks
                            if _columnaware_block_band(
                                block, bands, float(page.rect.width),
                            ) == band_index
                        ]
                        prefix_blocks = [
                            block for block in band_blocks
                            if float(block[1]) < heading_y
                        ]
                        if prefix_blocks:
                            suffix_blocks = [
                                block for block in band_blocks
                                if float(block[1]) >= heading_y
                            ]
                            decision = _columnaware_later_band_prefix_decision(
                                prefix_blocks,
                                suffix_blocks,
                                parts,
                                heading_y,
                                bands,
                                band_index,
                                float(page.rect.width),
                                declared_document_doi,
                            )
                            if decision == "trim_endmatter_prefix":
                                current = [
                                    (float(block[1]), float(block[0]), str(block[4] or ""))
                                    for block in suffix_blocks
                                ]
                            elif decision not in (
                                "preserve_source_prefix", "no_prefix",
                            ):
                                return None
                    current.sort(key=lambda item: (item[0], item[1]))
                    if current:
                        used_bands.add(band_index)
                        parts.extend(text for _y0, _x0, text in current)
                has_three_column_bibliography = len(used_bands) >= 3
                for block_index, block in enumerate(blocks):
                    if block_index in assigned_blocks:
                        continue
                    start_band = _columnaware_start_band(
                        block, bands, float(page.rect.width),
                    )
                    if (
                        float(block[3]) < float(heading_block[1])
                        and (start_band is None or start_band <= heading_band)
                    ):
                        continue
                    if (_columnaware_margin_block_is_recognized_furniture(
                            block, page, furniture_lines,
                        ) or block_index in repeated_margin_furniture[pno]):
                        continue
                    if _columnaware_has_bibliography_end(parts):
                        parts.append(str(block[4]))
                        continue
                    if _columnaware_block_is_at_page_margin(block, page):
                        # Location alone cannot prove that an unrecognized line is
                        # disposable furniture or safely assign it to a column.
                        return None
                    return None
            elif bands:
                # Each page can change its column count or begin with only a
                # partial final column.  Use all qualified bands on this page;
                # blocks crossing a gutter must not disappear from an active
                # bibliography region.
                used_bands = set()
                assigned_blocks = set()
                for band_index in range(len(bands)):
                    current = []
                    for block_index, block in enumerate(blocks):
                        block_band = _columnaware_block_band(
                            block, bands, float(page.rect.width),
                        )
                        if block_band is not None:
                            assigned_blocks.add(block_index)
                        if block_band == band_index:
                            current.append((float(block[1]), float(block[0]), str(block[4])))
                    current.sort(key=lambda item: (item[0], item[1]))
                    if current:
                        used_bands.add(band_index)
                        parts.extend(text for _y0, _x0, text in current)
                if len(used_bands) >= 3:
                    has_three_column_bibliography = True
                for block_index, block in enumerate(blocks):
                    if block_index in assigned_blocks:
                        continue
                    if (_columnaware_margin_block_is_recognized_furniture(
                            block, page, furniture_lines,
                        ) or block_index in repeated_margin_furniture[pno]):
                        continue
                    if _columnaware_has_bibliography_end(parts):
                        parts.append(str(block[4]))
                        continue
                    if _columnaware_block_is_at_page_margin(block, page):
                        # Location alone cannot prove that an unrecognized line is
                        # disposable furniture or safely assign it to a column.
                        return None
                    return None
            else:
                # A later single-column page needs no reordering.  Keep its
                # extracted blocks in source order for the existing bibliography
                # end-boundary logic in the parser.
                parts.extend(str(block[4]) for block in blocks)
            if _columnaware_has_bibliography_end(parts):
                break
    finally:
        doc.close()
    if not parts:
        return None
    return "\n".join(parts), has_three_column_bibliography


def columnaware_bibliography_text(path: str) -> str | None:
    """Return re-extracted bibliography-page text in physical column order.

    The public text-only helper remains available to existing callers.  Its
    private companion also returns whether the bibliography region itself passed
    an unambiguous three-column geometry check for the Parse adoption guard.
    """
    candidate = _columnaware_bibliography_candidate(path)
    return candidate[0] if candidate is not None else None


def _pdf_links_enabled() -> bool:
    """Hyperlink-layer resolution is on by default; a debug env flag disables it.

    Set ``CITATION_VERIFIER_PDF_LINKS=0`` (or false/no/off) to force the classic
    text-only pathway, e.g. to compare behaviour or work around a bad link layer.
    """
    return (os.environ.get("CITATION_VERIFIER_PDF_LINKS", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    text, meta = _pdf_text(path)
    extract_mod = _resolve_extract_module()
    if not extract_mod._pdf_quality_ok(text):
        if manuscript_ocr:
            text, meta = extract_mod._manuscript_ocr(path, ocr_lang, meta, ocr_notice)
        else:
            raise ValueError(
                "PDF extraction insufficient (sparse text or binary output): extract "
                "the text with a dedicated tool and re-supply a .txt/.md; method tried: "
                f"{meta.get('pdf_method')}"
            )
    # Attach the PDF hyperlink layer (hyperref named citation links) when present
    # and enabled; the parser prefers it for resolution and falls back to the
    # text pipeline when it is absent.  A scanned/OCR'd PDF has no link layer.
    if _pdf_links_enabled() and not meta.get("ocr"):
        try:
            from core.parse import pdf_links as _pdf_links
        except ImportError:  # pragma: no cover - direct-execution import path
            import pdf_links as _pdf_links
        try:
            layer = _pdf_links.extract_link_layer(path)
        except Exception:
            layer = None
        if layer:
            meta["pdf_links"] = layer
        try:
            external_links = _pdf_links.extract_external_links(path)
        except Exception:
            external_links = []
        if external_links:
            meta["pdf_external_links"] = external_links
    # postprocess() needs the file to ask it which digits are raised; an OCR'd PDF
    # has no glyph metadata to ask about.
    if not meta.get("ocr"):
        meta["pdf_path"] = path
    return text, "pdf", meta


def _strip_soft_hyphens(text: str) -> str:
    """U+00AD is a *discretionary* hyphen: it marks where a word MAY break and is
    invisible unless the line actually breaks there.  Extraction keeps the codepoint
    either way (plus the newline, when the break did happen), so "Kusch\xadminder"
    and "Kusch\xad\nminder" both read as two tokens and the surname stops matching
    its bibliography entry.  Deleting it — with whatever break follows — puts the
    word back together."""
    return re.sub("­\\s*", "", text)


def _dehyphenate(text: str) -> str:
    """Join words that were hyphenated at line breaks by a text extractor.

    Two-column LaTeX PDFs often produce words like ``em-\\nbedding``,
    ``Col-\\nlobert``, or proper names like ``Fei-\\nFei`` that break
    citation detection.  The heuristic requires a letter on both sides of
    the ``-\\n`` — the line-break hyphen is the distinguishing signal."""
    return re.sub(r"([A-Za-z])-\n([A-Za-z])", r"\1\2", text)


# Latin typographic ligatures that PDF extraction leaves intact.  NFC does not
# decompose them (only NFKC/NFKD would, but those also fold superscripts, math
# symbols, and full-width forms we rely on), so map the handful of Latin
# ligatures explicitly.  This is lossless and cannot touch our ⟦SUP⟧ markers.
_LIGATURES = str.maketrans({
    "ﬀ": "ff",   # ﬀ
    "ﬁ": "fi",   # ﬁ
    "ﬂ": "fl",   # ﬂ
    "ﬃ": "ffi",  # ﬃ
    "ﬄ": "ffl",  # ﬄ
    "ﬅ": "ft",   # ﬅ (long-s t)
    "ﬆ": "st",   # ﬆ
})


def _normalize_ligatures(text: str) -> str:
    return text.translate(_LIGATURES)


# A page that ends in the middle of its sentence, and the blank line that would keep
# it there.  Anything that could END a sentence — a full stop, a question mark, the
# quote or bracket that closes after one — leaves the paragraph break alone.
_MID_SENTENCE_PAGE_BREAK = re.compile(r"(?<![.!?…:;])[ \t]*\n\n\f[ \t]*")


# This is deliberately narrower than the font-aware footnote apparatus reader.
# It recognizes one extraction hazard only: PyMuPDF has placed a compact, numbered
# bottom-margin note directly between a hyphenated body tail and the next page's
# continuation.  The note is retained as text, but made a hard paragraph boundary
# before the form-feed markers disappear.  Other extractors do not provide a
# compatible block order, and must remain untouched.
_BOTTOM_MARGIN_NOTE_NUMBER_RE = re.compile(r"^\d{1,3}(?=[A-Za-z])")


def _dominant_span_font(spans) -> float | None:
    """Return the character-weighted dominant font size, if the page reports it."""
    weights: dict[float, int] = {}
    for span in spans:
        try:
            size = float(span.get("size"))
        except (AttributeError, TypeError, ValueError):
            continue
        weight = len(str(span.get("text", "")).strip())
        if weight:
            weights[size] = weights.get(size, 0) + weight
    if not weights:
        return None
    return max(weights, key=lambda size: (weights[size], size))


def _bottom_margin_note_texts(
    page_dict: dict, page_height: float, raw_blocks=None,
) -> list[str]:
    """Select the one narrowly evidenced PyMuPDF bottom-note subtype.

    This is geometry only.  A candidate that does not expose all required layout
    facts is not selected; callers consequently leave its text exactly as extracted.
    """
    if page_height <= 0:
        return []
    blocks = page_dict.get("blocks", ())
    all_spans = [
        span
        for block in blocks
        for line in block.get("lines", ())
        for span in line.get("spans", ())
    ]
    body_font = _dominant_span_font(all_spans)
    if body_font is None:
        return []

    raw_text_by_bbox = {}
    for raw in raw_blocks or ():
        if len(raw) >= 5:
            raw_text_by_bbox[tuple(round(float(value), 3) for value in raw[:4])] = raw[4].strip()

    selected: list[str] = []
    for block in blocks:
        bbox = block.get("bbox", ())
        if len(bbox) < 2:
            continue
        try:
            y0 = float(bbox[1])
        except (TypeError, ValueError):
            continue
        if y0 / page_height < 0.875:
            continue
        spans = [
            span
            for line in block.get("lines", ())
            for span in line.get("spans", ())
        ]
        block_font = _dominant_span_font(spans)
        if block_font is None or block_font > body_font * 0.85:
            continue
        bbox_key = tuple(round(float(value), 3) for value in bbox[:4])
        # ``dict`` spans can expose an invisible duplicate glyph.  The selected
        # PyMuPDF text stream is the only spelling that may be traced and replaced.
        candidate = raw_text_by_bbox.get(
            bbox_key,
            "".join(str(span.get("text", "")) for span in spans).strip(),
        )
        if _BOTTOM_MARGIN_NOTE_NUMBER_RE.match(candidate):
            selected.append(candidate)
    return selected


def _pymupdf_bottom_margin_notes(path: str) -> list[list[str]] | None:
    """Return selected note texts per page, or ``None`` when PDF geometry is absent."""
    try:
        import pymupdf as fitz
        doc = fitz.open(path)
    except Exception:
        return None
    try:
        return [
            _bottom_margin_note_texts(
                page.get_text("dict"), float(page.rect.height), page.get_text("blocks"),
            )
            for page in doc
        ]
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _isolate_pymupdf_bottom_margin_notes(text: str, meta: dict) -> str:
    """Make uniquely traceable selected notes hard boundaries in their own pages."""
    if not isinstance(meta, dict) or meta.get("pdf_method") != "pymupdf":
        return text
    path = meta.get("pdf_path")
    if not path or "\f" not in text:
        return text
    notes_by_page = _pymupdf_bottom_margin_notes(path)
    if notes_by_page is None:
        return text
    pages = text.split("\f")
    if len(pages) != len(notes_by_page):
        return text
    isolated = []
    for page_text, notes in zip(pages, notes_by_page):
        for note in notes:
            # Exact, single-page traceability is the audit guard.  If the text was
            # reordered, normalized, or duplicated, there is no safe replacement.
            if page_text.count(note) != 1:
                continue
            note_start = page_text.find(note)
            # A small bottom note is not itself proof that it interrupted body text.
            # The causal extraction shape is a hyphenated body tail immediately before
            # it; URLs and ordinary footer material must keep their original flow.
            if not re.search(r"[A-Za-z]-[ \t\r\n]*$", page_text[:note_start]):
                continue
            page_text = page_text.replace(note, f"\n\n{note}\n\n", 1)
        isolated.append(page_text)
    return "\f".join(isolated)


def _selected_block_text(raw_blocks, bbox) -> str | None:
    """Return the uniquely bbox-matched spelling from PyMuPDF's selected stream."""
    target = tuple(round(float(value), 3) for value in bbox[:4])
    matches = [
        str(block[4]).strip()
        for block in raw_blocks
        if len(block) >= 5
        and tuple(round(float(value), 3) for value in block[:4]) == target
    ]
    return matches[0] if len(matches) == 1 and matches[0] else None


def _pymupdf_page_layout_continuations(
    path: str, selected_pages: list[str],
) -> list[str] | None:
    """Return exactly traceable delayed continuations proven by page geometry.

    This does not infer a textual continuation.  It recognizes only a prior
    selected-stream line ending in ``letter-``, followed next page by a
    body-sized narrow lowercase block directly below a wide body-font block.
    Any missing geometry, duplicate block spelling, or extraction-order
    disagreement is a no-op.
    """
    try:
        import pymupdf as fitz
        doc = fitz.open(path)
    except Exception:
        return None
    try:
        if len(doc) != len(selected_pages):
            return None
        continuations: list[str] = [""] * len(selected_pages)
        for page_index in range(1, len(selected_pages)):
            previous_lines = [
                line.strip() for line in selected_pages[page_index - 1].splitlines()
                if line.strip()
            ]
            if not previous_lines or not re.search(r"[A-Za-z]-$", previous_lines[-1]):
                continue
            page = doc[page_index]
            page_dict = page.get_text("dict")
            body_font = _dominant_span_font([
                span
                for block in page_dict.get("blocks", ())
                for line in block.get("lines", ())
                for span in line.get("spans", ())
            ])
            if body_font is None or page.rect.width <= 0 or page.rect.height <= 0:
                continue
            text_blocks = [
                block for block in page_dict.get("blocks", ()) if block.get("lines")
            ]
            raw_blocks = page.get_text("blocks")
            candidates: list[str] = []
            for wide, narrow in zip(text_blocks, text_blocks[1:]):
                wide_bbox = wide.get("bbox", ())
                narrow_bbox = narrow.get("bbox", ())
                if len(wide_bbox) < 4 or len(narrow_bbox) < 4:
                    continue
                wide_width = (wide_bbox[2] - wide_bbox[0]) / page.rect.width
                wide_y0 = wide_bbox[1] / page.rect.height
                narrow_width = (narrow_bbox[2] - narrow_bbox[0]) / page.rect.width
                if not (
                    wide_width >= 0.65
                    and 0.20 <= wide_y0 <= 0.50
                    and narrow_width <= 0.50
                    and narrow_bbox[1] >= wide_bbox[3]
                ):
                    continue
                wide_font = _dominant_span_font([
                    span for line in wide["lines"] for span in line.get("spans", ())
                ])
                narrow_font = _dominant_span_font([
                    span for line in narrow["lines"] for span in line.get("spans", ())
                ])
                if (
                    wide_font is None
                    or narrow_font is None
                    or not 0.85 <= wide_font / body_font <= 1.0
                    or narrow_font / body_font < 0.95
                ):
                    continue
                candidate = _selected_block_text(raw_blocks, narrow_bbox)
                if candidate and candidate[:1].islower():
                    candidates.append(candidate)
            if len(candidates) == 1 and selected_pages[page_index].count(candidates[0]) == 1:
                continuations[page_index] = candidates[0]
        return continuations
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _tag_pymupdf_page_layout_interruptions(text: str, meta: dict) -> str:
    """Repair exact geometry-proven delayed continuations, otherwise tag them.

    A repair is deliberately narrower than detection: the selected-stream tail
    on the preceding page must also be unique.  This retains the existing
    interruption marker when a text-preserving splice is not unambiguous.
    """
    if not isinstance(meta, dict) or meta.get("pdf_method") != "pymupdf":
        return text
    path = meta.get("pdf_path")
    if not path or "\f" not in text:
        return text
    pages = text.split("\f")
    continuations = _pymupdf_page_layout_continuations(path, pages)
    if continuations is None or len(continuations) != len(pages):
        return text
    for index, continuation in enumerate(continuations):
        if not continuation:
            continue
        previous_lines = [
            line.strip() for line in pages[index - 1].splitlines() if line.strip()
        ] if index else []
        tail = previous_lines[-1] if previous_lines else ""
        if (
            tail
            and re.search(r"[A-Za-z]-$", tail)
            and pages[index - 1].count(tail) == 1
            and pages[index].count(continuation) == 1
        ):
            pages[index - 1] = pages[index - 1].replace(
                tail,
                tail[:-1]
                + parsing_common.PAGE_LAYOUT_REPAIRED_SENTINEL
                + continuation
                + "\n\n",
                1,
            )
            pages[index] = pages[index].replace(continuation, "", 1)
            continue
        pages[index] = pages[index].replace(
            continuation,
            parsing_common.PAGE_LAYOUT_INTERRUPTION_SENTINEL + continuation,
            1,
        )
    return "\f".join(pages)


def _relocate_pymupdf_preheading_blocks(text: str, meta: dict) -> str:
    """Move exactly traceable pre-heading blocks out of the bibliography.

    PDF stream order can emit a physically higher column after reference text.
    Geometry proves its position before an isolated bibliography heading. Each
    moved block and the heading must occur exactly once in the selected stream;
    ambiguous geometry or spelling leaves that source text untouched.
    """
    if not isinstance(meta, dict) or meta.get("pdf_method") != "pymupdf":
        return text
    path = meta.get("pdf_path")
    flags = meta.get("pdf_structure_flags")
    if not path or (flags is not None and not isinstance(flags, list)):
        return text
    try:
        import pymupdf as fitz
        doc = fitz.open(path)
    except (ImportError, OSError, RuntimeError):
        return text
    pages = text.split("\f")
    moved = False
    try:
        if len(doc) != len(pages):
            return text
        heading_re = parser_text.bibliography_heading_re()
        for number, selected in enumerate(pages):
            blocks = [
                block for block in doc[number].get_text("blocks")
                if len(block) >= 5 and isinstance(block[4], str) and block[4].strip()
            ]
            headings = [block for block in blocks if heading_re.fullmatch(block[4].strip())]
            if len(headings) != 1:
                continue
            heading = headings[0]
            heading_text = heading[4]
            if selected.count(heading_text) != 1:
                continue
            heading_start = selected.index(heading_text)
            heading_bbox = tuple(float(value) for value in heading[:4])
            if not all(math.isfinite(value) for value in heading_bbox):
                continue
            relocations = []
            for block in blocks:
                bbox = tuple(float(value) for value in block[:4])
                if not (
                    all(math.isfinite(value) for value in bbox)
                    and bbox[2] > bbox[0] and bbox[3] > bbox[1]
                    and bbox[3] <= heading_bbox[1]
                    and selected.count(block[4]) == 1
                ):
                    continue
                start = selected.index(block[4])
                if start >= heading_start + len(heading_text):
                    relocations.append((start, start + len(block[4]), block[4]))
            relocations.sort()
            if not relocations or any(
                before[1] > after[0]
                for before, after in zip(relocations, relocations[1:])
            ):
                continue
            corrected = selected
            for start, end, _block_text in reversed(relocations):
                corrected = corrected[:start] + corrected[end:]
            insert_at = corrected.index(heading_text)
            corrected = (
                corrected[:insert_at]
                + "".join(row[2] for row in relocations)
                + corrected[insert_at:]
            )
            pages[number] = corrected
            moved = True
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        return text
    finally:
        doc.close()
    if moved:
        flag = "bibliography_preheading_block_relocated"
        flags = meta.setdefault("pdf_structure_flags", [])
        if flag not in flags:
            flags.append(flag)
    return "\f".join(pages)


def postprocess(text: str, meta: dict) -> str:
    text = _relocate_pymupdf_preheading_blocks(text, meta)
    # The apparatus, where there is one: lifted below, and consulted by the conversion
    # further down for the one shape only the document itself can settle.
    notes = None
    # Recover Chicago Notes-Bibliography footnotes (numbered per-page references)
    # while the form-feed page markers are still present, then strip the markers
    # so the rest of the pipeline sees exactly the pre-existing "\n\n"-joined text.
    if "\f" in text:
        text = _isolate_pymupdf_bottom_margin_notes(text, meta)
        text = _tag_pymupdf_page_layout_interruptions(text, meta)
        try:
            from core.parse import footnotes as _footnotes
        except ImportError:  # pragma: no cover - import path fallback
            import footnotes as _footnotes
        notes = _footnotes.extract_footnote_references(text)
        # A law review carries no end bibliography — its sources are its numbered
        # footnotes — and the text scan above does not lift them when the note format or a
        # wrapped note number defeats it.  Fall back to the PDF's font sizes, which set
        # the footnote apparatus a step below the body and so isolate it cleanly.
        if not notes and isinstance(meta, dict) and meta.get("pdf_path"):
            notes = _footnotes.extract_footnote_references_from_pdf(meta["pdf_path"])
        if notes and isinstance(meta, dict):
            meta["footnote_references"] = notes
        # Preserve a hard boundary where a two-column text layer emits a
        # substantial right-column section before returning to the delayed
        # continuation of the left column.  This must run while form feeds are
        # still present; the sentence segmenter cannot recover the page seam
        # after the markers below are stripped.
        text = parsing_common.isolate_interleaved_page_continuations(text)
        # A page break falls where it falls, and it can fall inside a citation: one page
        # ends on '... migration management" (IOM' and the next opens with '2024).'.  The
        # blank line between the pages is a paragraph break to the segmenter, and the two
        # halves of the marker never meet — it is never seen, and a marker never seen
        # raises no orphan and no warning.  Where the page did not finish its sentence,
        # the pages are read as one.  (boilerplate.strip_page_breaks did this by removing
        # the running head that stood between the halves, and can only do it where there
        # IS a running head; the page break itself is the honest signal, and here — while
        # the form feeds are still standing — is the one place that knows it.)
        text = _MID_SENTENCE_PAGE_BREAK.sub("\n", text)
        text = text.replace("\f", "")
    text = _strip_soft_hyphens(text)
    body, cut_idx, biblio_lines = _split_body_bibliography(
        text, allow_headerless=not bool(notes),
    )
    body = _dehyphenate(body)
    body = unicodedata.normalize("NFC", body)
    body = _normalize_ligatures(body)
    body = re.sub(r"\(cid:\[?\d+\]?\)", "", body)
    pdf_path = meta.pop("pdf_path", None) if isinstance(meta, dict) else None
    # Whether the raised digits were read off the document or guessed from the text.
    # A scan, or a PDF that flags nothing, leaves us guessing — but a PDF always has
    # SOMETHING to say, which is what separates it from plain text (see txt.py): the
    # answer may be poor, it is never structurally absent.
    source = "disabled"
    if (
        body.strip()
        and os.environ.get("CITATION_VERIFIER_PDF_SUPERSCRIPTS", "1").strip()
        not in ("0", "false", "no", "off")
    ):
        oracle = _superscript_oracle(pdf_path) if pdf_path else None
        body = _convert_pdf_superscripts(body, oracle, notes)
        source = "glyphs" if oracle else "text-heuristic"
    if isinstance(meta, dict):
        meta["superscript_source"] = source
    if cut_idx is None:
        return body
    bib_text = _normalize_ligatures(_dehyphenate("\n".join(biblio_lines)))
    return body + "\n" + bib_text
