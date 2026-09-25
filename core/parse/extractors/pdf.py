#!/usr/bin/env python3
# core/parse/extractors/pdf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""PDF manuscript extractor."""

from __future__ import annotations

import importlib
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


def _split_body_bibliography(text: str) -> tuple[str, int | None, list[str]]:
    """Body text, the index of the bibliography heading, and the bibliography lines.

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
    cut_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip().lower().rstrip(":.")
        if stripped in parser_text.BIBLIOGRAPHY_HEADINGS:
            cut_idx = i
    if cut_idx is None:
        return text, None, lines

    biblio = lines[cut_idx + 1:]
    end = parsing_common._find_biblio_end_index(biblio)
    tail = []
    if end is not None:
        biblio, tail = biblio[:end], biblio[end:]

    interposed: list[str] = []
    spans = parsing_common._interposed_body_spans(biblio)
    if spans:
        foreign = {i for first, last in spans for i in range(first, last + 1)}
        interposed = [biblio[i] for i in sorted(foreign)]
        biblio = [ln for i, ln in enumerate(biblio) if i not in foreign]

    body_lines = lines[:cut_idx] + interposed + tail
    return "\n".join(body_lines), cut_idx, [lines[cut_idx]] + biblio


def columnaware_bibliography_text(path: str) -> str | None:
    """Re-extract the reference pages in column reading order (left column fully,
    then right), for publisher PDFs whose two-column *back matter* the default
    extraction interleaves (the "References" heading ends up followed by a table
    or an author-contributions block instead of the entries).

    Scoped to the pages from the "References" heading onward — the reference pages
    are cleanly two-column, so left-then-right is correct there, whereas body
    pages carry full-width titles/figures that this simple ordering would break.
    Returns the text of those pages, or ``None`` when there is no such heading or
    no PDF backend.  The caller decides whether the result beats the default
    parse (see parse()), so a bad re-extraction is simply ignored.
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

        # Shared multilingual heading detection (tolerates a leading glyph like
        # "■References", ACS).
        header_re = parser_text.bibliography_heading_re()
        start = None
        for pno in range(doc.page_count):
            if header_re.search(doc[pno].get_text("text")):
                start = pno
                break
        if start is None:
            return None
        parts = []
        for pno in range(start, doc.page_count):
            page = doc[pno]
            blocks = [b for b in read_blocks(page) if (b[4] or "").strip()]
            if not blocks:
                continue
            mid = page.rect.width / 2.0
            left = sorted((b for b in blocks if b[0] < mid), key=lambda b: b[1])
            right = sorted((b for b in blocks if b[0] >= mid), key=lambda b: b[1])
            parts.append("\n".join(b[4] for b in left + right))
    finally:
        doc.close()
    return "\n".join(parts) or None


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


def postprocess(text: str, meta: dict) -> str:
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
    body, cut_idx, biblio_lines = _split_body_bibliography(text)
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
