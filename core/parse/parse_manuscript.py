#!/usr/bin/env python3
# core/parse/parse_manuscript.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
parse_manuscript.py — Phase 1, deterministic.

manuscript (.docx | .tex | .pdf | .md | .txt) ->
  { manuscript, claims[], references[], citations[] } + debug view.
Per-format text extraction lives in core/extract.py; for LaTeX the \\cite{key}
commands arrive already converted to numeric markers [n].

No LLM. The parser does not guess anything that is not readable from the text:
every scoping decision (which sentences are claims, which markers map to which
sources) is recorded here as data. Within a sentence, ADJACENT markers ([1], [2])
form one marker group; each group becomes its OWN claim scoped to the sentence
fragment that precedes it (deterministic split at group boundaries, with fallback
to the whole sentence when a fragment would carry too little text — the decision
is recorded in claim_scope). The disambiguation "which source covers which
fragment" in a range [12-18] is NOT done here: every source in the range receives
the SAME claim; disambiguation is deferred to the Interpreter (Phase 3).

Citation schemes:
  * numeric — [n], [n,m], [n-m], (n), (n,m), (n-m), and superscript
    (plus LaTeX \\cite via conversion);
  * author-year — "(Smith 2021)", "Smith et al. (2021)" (see authoryear.py);
  * inline-doi ("draft" format) — the paper identifier in parentheses right
    after the statement, with no bibliography: "(10.1038/s41586-022-05543-x)",
    "(arXiv:1706.03762)", "(https://doi.org/10.1001/jama.2020.1585)". Each
    distinct identifier synthesises its own reference.
'auto' picks whichever scheme dominates the body.

Usage:
  python parse_manuscript.py --input in.docx --debug parse_debug.md [--window 1]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone

try:
    from core.parse.extract import extract_text
    from core.parse.extract import supported_extensions as _supported_input_extensions
    from core.parse import authoryear
    from core.parse import boilerplate
    from core.parse import citation_schemes
    from core.parse import format_handlers
    from core.parse import manuscript_identity
    from core.parse import parser_text
except ImportError:  # direct file execution
    from extract import extract_text
    from extract import supported_extensions as _supported_input_extensions
    import authoryear
    import boilerplate
    import citation_schemes
    import format_handlers
    import manuscript_identity
    import parser_text

try:
    from core.parse.parsing_common import (
        _expand_numbers, _classify_reference, _extract_arxiv_doi,
        _extract_title, _split_prose_entry_parts, _split_before_year,
        _make_reference, _AY_YEAR, mark_further_reading, RANGE_DASHES,
        apply_reference_readers,
        # Default parsing hooks, imported under their local alias names
        _clean_biblio_line,
        _is_biblio_noise_line,
        _is_biblio_tail_line,
        _looks_authoryear_ref_start,
        _merge_hyphen_wraps,
        _find_biblio_end_index,
        _split_authoryear_references,
        _truncate_bleed,
        _merge_standalone_doi_url,
        _PAR_STRUCTURE_RE,
        _par_in_equation_context,
        _brk_in_formula_context,
        STRUCTURAL_TABLE_SENTINEL,
        STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL,
        default_split_body_bibliography as _split_body_bibliography,
        default_segment_sentences as _segment_sentences,
        default_parse_references as _parse_references,
    )
except ImportError:
    from parsing_common import (
        _expand_numbers, _classify_reference, _extract_arxiv_doi,
        _extract_title, _split_prose_entry_parts, _split_before_year,
        _make_reference, _AY_YEAR, mark_further_reading, RANGE_DASHES,
        apply_reference_readers,
        _clean_biblio_line,
        _is_biblio_noise_line,
        _is_biblio_tail_line,
        _looks_authoryear_ref_start,
        _merge_hyphen_wraps,
        _find_biblio_end_index,
        _split_authoryear_references,
        _truncate_bleed,
        _merge_standalone_doi_url,
        _PAR_STRUCTURE_RE,
        _par_in_equation_context,
        _brk_in_formula_context,
        STRUCTURAL_TABLE_SENTINEL,
        STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL,
        default_split_body_bibliography as _split_body_bibliography,
        default_segment_sentences as _segment_sentences,
        default_parse_references as _parse_references,
    )

PARSER_VERSION = "cv-parser/0.4"


def _load_coverage_config() -> tuple[float, int]:
    path = os.path.join(os.path.dirname(__file__), "config", "coverage.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return float(raw["min_coverage_pct"]), int(raw["min_references"])
    except (OSError, ValueError, KeyError, TypeError):
        return 50.0, 5


# Edit config/coverage.json, not here.
_MIN_COVERAGE_PCT, _MIN_COVERAGE_REFS = _load_coverage_config()

# What an extractor says about raised digits, and what each answer is worth.
#
#   "glyphs"        PDF — read off the document's own glyph flags
#   "markup"        DOCX (w:vertAlign), and any txt/markdown that spells its
#                   superscripts out: <sup>1</sup>, ^1^, ¹ (see extractors/superscript)
#   "cite-commands" LaTeX — citations are \cite{}, never conveyed typographically
#   "text-heuristic" PDF with no usable glyph metadata (scan, flattened) — inferred
#   "disabled"      PDF, superscript recovery switched off by the operator
#   None            nothing in this file says a digit is raised, and nothing in it
#                   could have — the markers, if there were any, are gone
#   absent          an extractor that does not say (third-party) — treated as unknown
#
# Only the None case is hopeless, and it is hopeless in a specific way: the information
# was destroyed before the file reached us.  Note it is a property of the FILE, not of
# the format — a .md that writes "cases^1,2^" is as sighted here as a DOCX.
_SUPERSCRIPT_GROUND_TRUTH = frozenset({"glyphs", "markup", "cite-commands"})
_SUPERSCRIPT_UNDECLARED = "unknown"


ENV_VERIFY_TABLE_CITATIONS = "CITATION_VERIFIER_VERIFY_TABLE_CITATIONS"


def verify_table_citations() -> bool:
    """Whether a citation found inside a table row becomes a verification task.

    Default OFF, and the default is the honest one: a benchmark row ("BERT 88.5
    [12]") is a label in a column, not an assertion — there is no sentence to check
    the source against, so a verdict on it would be a verdict on nothing.  Dropped
    markers are never lost silently: they are counted for reference coverage and
    listed in the report as "cited only in a table — not verified".

    Turned on (``--verify-table-citations`` / ``CITATION_VERIFIER_VERIFY_TABLE_CITATIONS=1``)
    the table markers stay in, and the row becomes an ordinary claim like any other.
    Useful when the tables ARE the argument (a review's comparison table), at the
    price of low-signal verdicts on rows that assert nothing."""
    return (os.environ.get(ENV_VERIFY_TABLE_CITATIONS, "0").strip().lower()
            in ("1", "true", "yes", "on"))


def _consume_footnote_source_metadata(
        references: list[dict], citations: list[dict], manuscript_id: str
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Project private footnote extraction evidence into typed parse payload rows."""
    notes: list[dict] = []
    sources: list[dict] = []
    parents: list[dict] = []
    note_for_ref: dict[str, str] = {}
    seen_notes: set[str] = set()
    for ref in references:
        projection = ref.pop("_footnote_projection", None)
        source = ref.pop("_footnote_source", None)
        if projection is not None:
            number = projection["note_number"]
            note_id = "footnote-" + hashlib.sha256(
                f"{manuscript_id}:{number}".encode("utf-8")
            ).hexdigest()[:24]
            if projection["role"] == "parent":
                if note_id not in seen_notes:
                    notes.append({"note_id": note_id, "manuscript_id": manuscript_id,
                                  "note_number": number,
                                  "raw_note": projection["raw_note"],
                                  "extraction_status": projection["extraction_status"]})
                    seen_notes.add(note_id)
                note_for_ref[ref["id"]] = note_id
                parents.append({"note_id": note_id, "ref_id": ref["id"]})
            elif projection["role"] == "source":
                sources.append({"note_id": note_id, "ref_id": ref["id"],
                                "source_order": projection["source_order"],
                                "raw_start": projection["raw_start"],
                                "raw_end": projection["raw_end"]})
            else:  # private parse metadata must never silently create a source.
                raise ValueError("unknown footnote projection role")
            continue
        if source is None:
            continue
        number = source["note_number"]
        note_id = "footnote-" + hashlib.sha256(
            f"{manuscript_id}:{number}".encode("utf-8")
        ).hexdigest()[:24]
        if note_id not in seen_notes:
            notes.append({"note_id": note_id, "manuscript_id": manuscript_id,
                          "note_number": number,
                          "raw_note": source["raw_note"],
                          "extraction_status": source["extraction_status"]})
            seen_notes.add(note_id)
        if source["extraction_status"] == "sources_extracted":
            sources.append({"note_id": note_id, "ref_id": ref["id"],
                            "source_order": source["source_order"],
                            "raw_start": source["raw_start"], "raw_end": source["raw_end"]})
        note_for_ref[ref["id"]] = note_id
        parents.append({"note_id": note_id, "ref_id": ref["id"]})
    claim_footnotes = []
    seen_claim_notes: set[tuple[str, str]] = set()
    for citation in citations:
        claim_id = citation.get("claim_id")
        note_id = note_for_ref.get(citation.get("ref_id"))
        if not claim_id or note_id is None or (claim_id, note_id) in seen_claim_notes:
            continue
        seen_claim_notes.add((claim_id, note_id))
        claim_footnotes.append({"claim_id": claim_id, "note_id": note_id})
    return notes, sources, claim_footnotes, parents


def _coverage_references(references: list[dict], footnote_sources: list[dict],
                         footnote_parents: list[dict]) -> list[dict]:
    """Keep Parse coverage at the printed-note level after a source projection."""
    parent_ids = {row["ref_id"] for row in footnote_parents}
    child_ids = {row["ref_id"] for row in footnote_sources} - parent_ids
    return [ref for ref in references if ref["id"] not in child_ids]


class CitationMarkersLost(ValueError):
    """The document's citation markers are not in the file we were given.

    Raised, rather than warned, when the format cannot represent them at all: there
    is nothing to salvage and no reason to spend a verification run on 40% of a
    manuscript.  The fix is not in our code, it is a better copy of the paper."""


def _reference_coverage(references: list[dict], citations: list[dict],
                        superscript_source, table_only=()) -> dict:
    """How many of the references the document lists are actually cited.

    This is the only signal that catches a SILENT parse failure.  The orphan count
    cannot: an orphan is a citation whose source is missing, so a citation that was
    never DETECTED cannot become one.  A paper that cites by superscript, handed to
    us as .txt, parses to no markers, no claims and no orphans — plain text has no
    superscripts, so "in all cases.35" is just a number — and every check we have
    reports success on a document we read 60% of.

    A document that lists N references cites nearly all of them; when it does not,
    the markers were eaten.  On the corpus a healthy parse covers 76-100% of its
    references and a mutilated one covers 0-33%, which is a gap, not a gradient.

    A citation inside a table counts.  "Is this reference cited anywhere?" and "is
    this sentence a claim we can verify?" are two different questions, and only the
    second one has a reason to refuse a table row: a benchmark row is a label in a
    column, not an assertion.  But the reference it names IS cited — "Zhu et al.
    (2013) [40]" in Table 4 of *Attention Is All You Need* is the only mention that
    paper makes of [40] — and counting it as uncited would have this measure raise
    an alarm about the parser every time a paper compares itself to prior work in a
    table, which is most of them.  So the table markers, already collected by
    ``_table_only_citations``, are counted here and only here; they still produce no
    claim and no verification task.

    ``uncited`` is what is left: references the document lists and never names, in
    prose or in a table.  That is the list worth reading — every entry is either a
    marker we failed to find or a reference the authors never cited.

    ``fatal`` is the subset we refuse outright: coverage collapsed AND the format
    cannot carry a superscript, so the markers are not recoverable from this file by
    any means.  Low coverage in a format that CAN carry them is a parser problem —
    ours to fix, and no reason to reject the manuscript — so it only warns."""
    in_prose = {c.get("ref_id") for c in citations if c.get("ref_id")}
    in_tables = {t.get("ref_id") for t in (table_only or ()) if t.get("ref_id")}
    cited_ids = in_prose | in_tables
    cited = len(cited_ids)
    # A "Further reading" entry is not a reference the paper failed to cite, it is one
    # it never meant to cite — so it belongs in neither half of this ratio.  Only an
    # UNCITED one is set aside: should the flag have reached a real reference, the
    # citation that names it keeps it counted, so the flag can never hide a marker we
    # failed to read (which is the one thing this measure exists to catch).
    further = [r.get("ref_number") for r in references
               if r.get("further_reading") and r.get("id") not in cited_ids]
    counted = [r for r in references
               if not (r.get("further_reading") and r.get("id") not in cited_ids)]
    total = len(counted)
    pct = (100.0 * cited / total) if total else 0.0
    table_only_count = len(in_tables - in_prose)
    # Calibrated against the 18-paper local corpus: its largest healthy
    # table-only share is 5 versus 35 prose references (Attention).  This is a
    # diagnostic only: table markers still count toward coverage because they
    # are real citations.  A large table-only majority is useful evidence that
    # a layout detector may have classified prose as rows.
    table_disproportionate = table_only_count >= 3 and (
        not in_prose or table_only_count * 4 > len(in_prose)
    )
    table_warning = (
        f"{table_only_count} references appear only in table-like regions versus "
        f"{len(in_prose)} in prose; inspect structural table suppression"
        if table_disproportionate else None
    )
    out = {"cited": cited, "total": total, "pct": round(pct, 1),
           "in_prose": len(in_prose), "in_tables": len(in_tables - in_prose),
           "uncited": [r.get("ref_number") for r in counted
                       if r.get("id") not in cited_ids],
           "warning": None, "fatal": False}
    if table_warning:
        out["table_warning"] = table_warning
    if further:  # said only when there is a second list — most papers have none
        out["further_reading"] = further
    if total < _MIN_COVERAGE_REFS or pct >= _MIN_COVERAGE_PCT:
        return out
    # Say WHY, when we know.  A format that cannot carry superscripts is not a
    # suspicion, it is a fact about the input — and the only case where the fault
    # is certainly not the parser's.
    format_is_blind = superscript_source is None
    cause = ("this format cannot represent a superscript, so a paper citing by "
             "superscript arrives with its markers already gone"
             if format_is_blind
             else "the body's citation markers may not have been recognised")
    out["warning"] = (
        f"only {cited} of {total} references are ever cited ({pct:.0f}%) — "
        f"citation markers were probably lost: {cause}")
    out["fatal"] = format_is_blind
    return out


# Protected abbreviations: a period here does NOT end the sentence.
# Numeric citation marker. The sentinel ⟦SUP:...⟧ is injected for superscript runs
# (see _docx_text).
# Numbers limited to 1-3 digits: "(2020)" or "[2020]" is almost certainly a year,
# not a citation (bibliographies with >999 entries: out of scope, documented).
_DASH = RANGE_DASHES
MARKER_RE = re.compile(
    rf"(?P<brk>\[(?P<brk_n>\d{{1,3}}(?:\s*[{_DASH}]\s*\d{{1,3}}|\s*,\s*\d{{1,3}})*)\])"
    rf"|(?P<par>\((?P<par_n>\d{{1,3}}(?:\s*[{_DASH}]\s*\d{{1,3}}|\s*,\s*\d{{1,3}})*)\))"
    rf"|(?P<sup>⟦SUP:(?P<sup_n>[\d,{_DASH}]+)⟧)"
)

# Fragment-level guards: when a sentence-fragment after marker removal starts
# with one of these syntactic glue words or phrases, the split is abandoned —
# the claim keeps the whole sentence as context.  They must be matched as
# tokens, never as raw prefixes: "in" must not catch "increased".
_FRAGMENT_CONJUNCTION_STARTS = frozenset({
    "and", "or", "but", "such as", "including", "especially",
    "particularly", "notably", "unlike", "similar", "compared",
    "following", "as", "in",
})

# Author-year builders may pre-cluster a subsequent marker group that begins
# in the middle of the same grammatical proposition ("..., which ... (B,
# 2021)" or "..., is ... (B, 2021)").  This is deliberately *not* a generic
# fragment fallback: numeric/MLA/DOI markers keep their historical per-group
# scoping even when the prose contains a relative clause.
_FRAGMENT_RELATIVE_STARTS = frozenset({"which", "that", "who", "whose", "where"})
_FRAGMENT_DEPENDENT_STARTS = _FRAGMENT_RELATIVE_STARTS | frozenset({
    "is", "are", "was", "were",
})


def _leading_fragment_phrases(text: str) -> frozenset[str]:
    """Return the first one- and two-token phrases in a fragment.

    Fragment fallback must use lexical tokens rather than raw string prefixes:
    ``in contrast`` is a dependent opener, while ``increased`` is a complete
    word that may begin an independently verifiable clause.
    """
    tokens = re.findall(r"[a-z]+(?:[-’'][a-z]+)*", text.lower())
    phrases = set(tokens[:1])
    if len(tokens) >= 2:
        phrases.add(" ".join(tokens[:2]))
    return frozenset(phrases)


_RELATIVE_COORDINATOR_RE = re.compile(
    r"\b(?:and|or|but|while|whereas)\b", re.IGNORECASE)


def _coalesce_dependent_marker_groups(sentence: str, groups: list[list],
                                      group_spans: list[tuple]) -> tuple[list[list], list[tuple]]:
    """Join adjacent author-year groups only for a direct dependent fragment.

    This conservative repair is for author-year parser splits where the next
    group begins with an exact relative/copular continuation.  Postposed
    numeric/MLA/DOI marker groups stay source-specific: a relative can modify
    an earlier noun phrase without making every later citation co-owned.
    """
    if len(groups) < 2 or len(groups) != len(group_spans):
        return groups, group_spans
    clustered_groups: list[list] = []
    clustered_spans: list[tuple] = []
    for index, (group, span) in enumerate(zip(groups, group_spans)):
        dependent = False
        if index:
            previous_end = group_spans[index - 1][1]
            fragment_end = span[1] if index < len(group_spans) - 1 else len(sentence)
            fragment = sentence[previous_end:fragment_end].strip().strip(",;:").strip()
            stripped = MARKER_RE.sub(" ", fragment).strip()
            lead = _leading_fragment_phrases(stripped)
            leading_relative = bool(_FRAGMENT_RELATIVE_STARTS.intersection(lead))
            leading_dependent = bool(_FRAGMENT_DEPENDENT_STARTS.intersection(lead))
            leading_continues = leading_dependent and (
                not leading_relative or not _RELATIVE_COORDINATOR_RE.search(fragment))
            dependent = leading_continues
        if dependent:
            clustered_groups[-1].extend(group)
            clustered_spans[-1] = (clustered_spans[-1][0], span[1])
        else:
            clustered_groups.append(list(group))
            clustered_spans.append(span)
    return clustered_groups, clustered_spans


def _numeric_scheme_module():
    return citation_schemes.require_numeric()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Fold every apostrophe variant a PDF/DOCX may carry to a single ASCII "'", so a
# surname like O'Malley keys and matches identically however it was typeset.
_APOSTROPHE_FOLD = str.maketrans({
    "’": "'",  # ’ right single quotation mark (most common)
    "‘": "'",  # ‘ left single quotation mark
    "ʼ": "'",  # ʼ modifier letter apostrophe
    "ʹ": "'",  # ʹ modifier letter prime
    "′": "'",  # ′ prime
    "＇": "'",  # ＇ fullwidth apostrophe
    "՚": "'",  # ՚ armenian apostrophe
    "‵": "'",  # ‵ reversed prime
})


# A LaTeX-built PDF often typesets an accented letter as two glyphs: a spacing
# accent plus the bare letter ("H´enaff", "A¨aron", "Erd˝os"), or, for the marks
# written under the letter, the letter plus the accent ("Franc¸ois").  Neither
# shape is a name to any pattern here — a surname regex stops at the accent — so
# the citation is not detected and the bibliography entry does not start where it
# should.  Fold the pair back into the precomposed character.
_ACCENT_ABOVE = {
    "´": "́",  # ´ acute
    "`": "̀",  # ` grave
    "ˆ": "̂",  # ˆ circumflex
    "˜": "̃",  # ˜ tilde
    "¨": "̈",  # ¨ diaeresis
    "ˇ": "̌",  # ˇ caron
    "˚": "̊",  # ˚ ring
    "¯": "̄",  # ¯ macron
    "˘": "̆",  # ˘ breve
    "˙": "̇",  # ˙ dot above
    "˝": "̋",  # ˝ double acute
}
_ACCENT_BELOW = {
    "¸": "̧",  # ¸ cedilla
    "˛": "̨",  # ˛ ogonek
}
# Both accent and letter must sit inside a word, so an accent glyph used as
# punctuation is left alone: "don´t" keeps its apostrophe, LaTeX quotes ``all''
# keep theirs.
_ACCENT_ABOVE_RE = re.compile(
    r"(?<=[A-Za-z])([" + "".join(_ACCENT_ABOVE) + r"])([A-Za-z])")
_ACCENT_BELOW_RE = re.compile(
    r"([A-Za-z])([" + "".join(_ACCENT_BELOW) + r"])(?=[A-Za-z])")


def _compose(letter: str, combining: str, whole: str) -> str:
    """The letter with the mark, when Unicode has that character; else untouched.

    The composition test is the guard: no precomposed character means the pair was
    never an accented letter (there is no "t with acute"), so "don´t" survives."""
    composed = unicodedata.normalize("NFC", letter + combining)
    return composed if len(composed) == 1 else whole


def _fold_spacing_accents(s: str) -> str:
    s = _ACCENT_ABOVE_RE.sub(
        lambda m: _compose(m.group(2), _ACCENT_ABOVE[m.group(1)], m.group(0)), s)
    return _ACCENT_BELOW_RE.sub(
        lambda m: _compose(m.group(1), _ACCENT_BELOW[m.group(2)], m.group(0)), s)


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFC", s.replace("\x00", "\uFFFD")).translate(
        _APOSTROPHE_FOLD
    )
    return _fold_spacing_accents(s)


class Marker:
    """One citation marker occurrence: raw text, numbers, span, marker kind."""
    __slots__ = ("raw", "nums", "span", "kind")

    def __init__(self, raw, nums, span, kind):
        self.raw, self.nums, self.span, self.kind = raw, nums, span, kind


def _find_markers(sentence: str, suppressed_out: list | None = None,
                  sentence_index: int = 0) -> list[Marker]:
    return _numeric_scheme_module().find_markers(sentence, suppressed_out, sentence_index)


def _display(sentence: str) -> str:
    """Converts superscript sentinels back to readable form for the report."""
    sentence = sentence.replace(STRUCTURAL_TABLE_SENTINEL, "").replace(
        STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL, ""
    )
    return re.sub(rf"⟦SUP:([\d,{_DASH}]+)⟧", r"[\1]", sentence)


# Glue between two markers that cite together: only punctuation/conjunctions
# ("[1], [2]", "[1] and [2]") — any real word in between separates the groups.
_MARKER_GLUE_RE = re.compile(r"^[\s,;&]*(?:(?:and|e|und|et)\s+)?[\s,;&]*$", re.IGNORECASE)


def _group_markers(sentence: str, markers: list) -> list[list]:
    """Adjacent markers (separated only by punctuation/'and') form one group:
    they cite the same statement. Markers with prose in between scope
    different fragments and become separate groups."""
    groups = [[markers[0]]]
    for mk in markers[1:]:
        between = sentence[groups[-1][-1].span[1]:mk.span[0]]
        if _MARKER_GLUE_RE.match(between):
            groups[-1].append(mk)
        else:
            groups.append([mk])
    return groups


def _fragment_texts(sentence: str, group_spans: list[tuple], *,
                    exact_openers: bool = False) -> tuple[list[str], bool]:
    """One text fragment per marker group: from the end of the previous group
    to the end of this group (the last fragment runs to the end of the
    sentence). Returns (fragments, split_applied). Falls back to the whole
    sentence for EVERY group when any fragment would carry fewer than two
    words — the split must never produce a claim with no verifiable text."""
    if len(group_spans) == 1:
        return [sentence], False
    author_year_groups = any(
        re.search(r"\b(?:19|20)\d{2}[a-z]?\b", sentence[start:end], re.I)
        for start, end in group_spans
    )
    frags, start = [], 0
    for gi, (g_start, g_end) in enumerate(group_spans):
        end = g_end if gi < len(group_spans) - 1 else len(sentence)
        frags.append(sentence[start:end].strip().strip(",;:").strip())
        start = end
    for frag in frags:
        words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", MARKER_RE.sub(" ", frag))
        if len(words) < 2:
            return [sentence] * len(group_spans), False
        # A short marker fragment cannot express a predicate safely.  In
        # particular, comparative openers ("Unlike X ...") are the left half
        # of one claim, not independent predicate-less claims.
        stripped = MARKER_RE.sub(" ", frag).strip().lower()
        # A short fragment is unsafe only when it is also predicate-less.
        # Ordinary coordinated prose ("while beta collapsed ...") must keep
        # the historical per-marker split; comparative shells such as
        # "Unlike Peters ..." are the fragments this guard is meant to merge.
        has_predicate = re.search(
            r"\b(?:is|are|was|were|be|been|use|used|apply|applied|"
            r"show|shows|make|makes|allow|allows|achieve|achieves|"
            r"increase|increased|decrease|decreased|collapse|collapsed|"
            r"rise|rose|fall|fell|drop|dropped|train|trained|describe|described)\w*\b",
            stripped, re.I)
        explicit_predicate = re.search(
            r"\b(?:am|is|are|was|were|has|have|had|do|does|did|"
            r"can|could|will|would|shall|should|may|might|must|"
            r"use|used|uses|apply|applied|applies|show|showed|shown|shows|"
            r"make|made|makes|allow|allowed|allows|achieve|achieved|achieves|"
            r"increase|increased|increases|decrease|decreased|decreases|"
            r"improve|improved|improves|report|reported|reports|"
            r"score|scored|scores|train|trained|trains|"
            r"describe|described|describes)\b",
            stripped,
            re.I,
        )
        if (
            len(words) < 8
            and not has_predicate
            and not (author_year_groups and explicit_predicate)
        ):
            return [sentence] * len(group_spans), False
        # M41 tightens opener matching only for the two affected parser
        # schemes (numeric and author-year).  MLA/DOI retain their historical
        # fallback so a grammar calibration cannot alter their certified
        # source-specific claim projection.
        if exact_openers:
            opener = bool(_FRAGMENT_CONJUNCTION_STARTS.intersection(
                _leading_fragment_phrases(stripped)))
        else:
            opener = any(stripped.startswith(conj)
                         for conj in _FRAGMENT_CONJUNCTION_STARTS)
        if opener:
            return [sentence] * len(group_spans), False
        if not re.search(r"[a-z]", MARKER_RE.sub(" ", frag)):
            return [sentence] * len(group_spans), False
    return frags, True


def _claim_structural_provenance(sentences, idx: int, sentence_text=None):
    """Return parser-owned layout lineage for a claim, when the segmenter has it.

    Citation schemes still receive a normal list of strings.  The default
    segmenter enriches that list with physical-layout lineage; custom segmenters
    may omit this observational field.  No content decision is
    made here.
    """
    lineage = getattr(sentences, "lineage", None)
    if not isinstance(lineage, list) or idx >= len(lineage):
        return None
    base = lineage[idx]
    if not isinstance(base, dict):
        return None
    out = dict(base)
    if sentence_text is not None:
        full = " ".join(str(sentences[idx]).split())
        fragment = " ".join(str(sentence_text).split())
        out["claim_text_alignment"] = (
            "exact_subspan" if fragment and fragment in full else "derived_fragment")
    else:
        out["claim_text_alignment"] = "full_segment"
    # Keep a direct data flag for consumers that cannot depend on a particular
    # status vocabulary.  It is purely a consequence of mixed layout kinds.
    out["structural_ambiguous"] = bool(
        out.get("crosses_incompatible_segments")
        or out.get("status") == "ambiguous")
    return out


def _new_claim(manuscript_id, sentences, idx, window, marker_raw, marker_numbers,
               multi, sentence_text=None, scope="sentence", *,
               marker_group_index=None, marker_group_count=None):
    """Build one claim and attest the marker topology owned by this parser.

    The offsets are calculated while the parser still owns both the displayed
    claim and its selected marker.  Later verification must consume these
    facts, never rediscover a marker in prose as a repair step.
    """
    lo = max(0, idx - window)
    hi = min(len(sentences), idx + window + 1)
    displayed_sentence = _display(sentence_text if sentence_text is not None
                                  else sentences[idx])
    displayed_marker = _display(marker_raw)
    marker_start = displayed_sentence.find(displayed_marker)
    if (
        marker_start < 0
        or not displayed_marker
        or displayed_sentence.count(displayed_marker) != 1
    ):
        marker_start = None
        marker_end = None
    else:
        marker_end = marker_start + len(displayed_marker)
    claim = {
        "id": f"claim-{uuid.uuid4().hex[:10]}",
        "manuscript_id": manuscript_id,
        "sentence": displayed_sentence,
        "context_window": " ".join(_display(s) for s in sentences[lo:hi]),
        "section": None, "char_start": None, "char_end": None,
        "marker_raw": displayed_marker,
        "marker_numbers": marker_numbers,
        "is_multisource": multi,
        "claim_scope": scope,
        # Custom parser callers may omit group coordinates. Built-in schemes
        # always set the sentence/group facts below.
        "parser_sentence_index": idx,
        "marker_group_index": marker_group_index,
        "marker_group_count": marker_group_count,
        "marker_start": marker_start,
        "marker_end": marker_end,
    }
    provenance = _claim_structural_provenance(sentences, idx, sentence_text)
    if provenance is not None:
        claim["structural_provenance"] = provenance
    return claim


def _table_only_citations(suppressed, citations, references):
    """References cited ONLY inside a suppressed table row — never in prose.

    Table-row suppression correctly drops benchmark-score rows, but a citation
    that names a comparison system (e.g. ``CSE (Akbik et al., 2018)``) is still a
    real reference.  When such a reference appears nowhere else it would vanish
    silently and never be verified; collect these so the report can flag them for
    manual review instead."""
    cited = {row.get("ref_number") for row in citations if row.get("ref_number")}
    ref_by_num = {r["ref_number"]: r for r in references}
    out, seen = [], set()
    for s in suppressed:
        if s.get("reason") != "table_row":
            continue
        for rn in s.get("ref_numbers") or []:
            if rn in cited or rn in seen:
                continue
            seen.add(rn)
            ref = ref_by_num.get(rn, {})
            out.append({"ref_number": rn, "ref_id": ref.get("id"),
                        "marker_raw": s.get("marker_raw"),
                        "raw_entry": (ref.get("raw_entry") or "")[:160]})
    return out


def _build_numeric(sentences, references, window, manuscript_id, fmt=None):
    return _numeric_scheme_module().build(sentences, references, window, manuscript_id, fmt=fmt)


def _refs_from_link_layer(link_layer) -> list[dict]:
    """Rebuild a reference list from the PDF hyperlink layer's anchors, numbered
    in reading order (page, then y).  Only anchors that carry entry text are
    kept.  The last-resort recovery when reference parsing collapses; see the
    call site in parse() for the (narrow) conditions under which it is adopted."""
    refs = []
    ordered = sorted(link_layer.references.values(), key=lambda r: (r.page, r.y))
    for a in ordered:
        entry = (a.text or "").strip()
        if entry:
            refs.append(_make_reference(len(refs) + 1, entry))
    return refs


_DOI_RESOLVER_HOSTS = frozenset({
    "doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org",
})
_ANNOTATION_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)


def _annotation_doi_url(url: object) -> str | None:
    """Return a DOI only from a canonical HTTP(S) DOI-resolver annotation."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if (parsed.hostname or "").casefold() not in _DOI_RESOLVER_HOSTS:
        return None
    doi = urllib.parse.unquote(parsed.path.lstrip("/")).strip().rstrip(".,;>")
    return doi if _ANNOTATION_DOI_RE.fullmatch(doi) else None


def _attach_external_reference_urls(references: list[dict], links: list[dict] | None) -> None:
    """Attach PDF URLs through unique DOI completion or exact visible title.

    DOI completion requires the parsed DOI as a visible strict prefix of one
    unique DOI-resolver annotation.  The title route deliberately avoids fuzzy
    matching because external annotations also occur throughout manuscript
    prose and could otherwise be assigned to an unrelated bibliography entry.
    """
    doi_completions: dict[int, list[tuple[dict, str]]] = {}
    completed_by: dict[str, set[int]] = {}
    for ref_index, ref in enumerate(references):
        cited_doi = str(ref.get("doi") or "").strip()
        if not cited_doi:
            continue
        candidates: list[tuple[dict, str]] = []
        for link in links or []:
            if not isinstance(link, dict):
                continue
            annotation_doi = _annotation_doi_url(link.get("url"))
            text = str(link.get("text") or "")
            # PyMuPDF can expose phantom spaces inside the glyph run covered by
            # one annotation (``do i.o r g/...03. 032``).  Removing whitespace
            # only for this corroboration check preserves the requirement that
            # the cited prefix is visibly present under the DOI link.
            visible_doi_text = re.sub(r"\s+", "", text).casefold()
            if (
                annotation_doi is not None
                and annotation_doi.casefold() != cited_doi.casefold()
                and annotation_doi.casefold().startswith(cited_doi.casefold())
                and cited_doi.casefold() in visible_doi_text
            ):
                candidates.append((link, annotation_doi))
                completed_by.setdefault(annotation_doi.casefold(), set()).add(ref_index)
        if candidates:
            doi_completions[ref_index] = candidates

    for ref_index, candidates in doi_completions.items():
        if len(candidates) != 1:
            continue
        link, completed_doi = candidates[0]
        if len(completed_by[completed_doi.casefold()]) != 1:
            continue
        existing_url = references[ref_index].get("url")
        existing_doi = _annotation_doi_url(existing_url)
        cited_doi = str(references[ref_index].get("doi") or "").strip()
        if existing_url and (existing_doi is None or existing_doi.casefold() != cited_doi.casefold()):
            continue
        references[ref_index]["doi"] = completed_doi
        references[ref_index]["url"] = str(link["url"]).strip()
        references[ref_index]["url_discovered_via"] = "pdf_annotation_doi_prefix"

    by_title: dict[str, list[dict]] = {}
    for ref in references:
        key = " ".join(re.findall(r"[a-z0-9]+", str(ref.get("title") or "").lower()))
        if key:
            by_title.setdefault(key, []).append(ref)
    for link in links or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        key = " ".join(re.findall(r"[a-z0-9]+", str(link.get("text") or "").lower()))
        matches = by_title.get(key) or []
        if len(matches) == 1 and url and not matches[0].get("url"):
            matches[0]["url"] = url
            matches[0]["url_discovered_via"] = "pdf_annotation_exact_title"


def parse(path: str, window: int, citations_mode: str = "auto",
          *, ocr_lang: str = "eng", ocr_notice=None,
          interactive: bool = False, ask=input) -> tuple[dict, str]:
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    # The manuscript is the document under verification: if it is a scanned PDF with no
    # text layer, OCR it automatically (manuscript_ocr=True) rather than fail.
    text, fmt, extract_meta = extract_text(
        path, manuscript_ocr=True, ocr_lang=ocr_lang, ocr_notice=ocr_notice)
    text = normalize(text)
    title_evidence = manuscript_identity.collect_local_evidence(
        path, fmt, text, extract_meta)
    # The PDF hyperlink layer (when the extractor found one) is consumed by
    # citation resolution, not by the text seams; pop it out of the extract meta
    # so it never lands in the JSON-serialised debug view.
    link_layer = extract_meta.pop("pdf_links", None) if isinstance(extract_meta, dict) else None
    external_links = extract_meta.pop("pdf_external_links", None) if isinstance(extract_meta, dict) else None
    family = format_handlers.select(fmt)
    body, biblio, cut_debug, biblio_end_index = family.split_body_bibliography(text, meta=extract_meta)
    # The page's own furniture (running head, page number) is printed wherever the page
    # breaks — including through the middle of a citation, which then matches no pattern
    # and is lost without a trace.  Detected on the whole document (a line is furniture
    # only if it REPEATS and looks like furniture), dropped from the body before the
    # sentences are cut.
    body_furniture = (
        boilerplate.boilerplate_lines(text)
        | boilerplate.journal_running_head_lines(text)
    )
    body = boilerplate.strip_page_breaks(body, body_furniture)
    body = boilerplate.strip_interposed_affiliation_blocks(body)
    sentences = family.segment_sentences(body, meta=extract_meta)
    references = family.parse_references(biblio, meta=extract_meta)
    manuscript_id = f"ms-{uuid.uuid4().hex[:10]}"

    def _assign_keys(refs):
        for r in refs:
            r["manuscript_id"] = manuscript_id
            sur, yr, suf = authoryear.entry_key(r["raw_entry"])
            r["ay_surname"], r["ay_year"], r["ay_suffix"] = sur, yr, suf

    _assign_keys(references)

    # Column-aware bibliography recovery: some publisher PDFs have a two-column
    # *back matter* whose default reading order interleaves the reference entries
    # with a table or an author-contributions block, so the entries barely
    # segment.  Re-extract the reference pages in column order and adopt them
    # only when they yield substantially MORE entries — a self-validating guard,
    # so a correctly-extracted document (which re-extracts to the same count) is
    # never touched.  The body/sentences keep the default extraction.
    if fmt == "pdf":
        try:
            from core.parse.extractors import pdf as _pdfx
        except ImportError:  # pragma: no cover - direct-execution import path
            _pdfx = None
        ca_text = _pdfx.columnaware_bibliography_text(path) if _pdfx else None
        if ca_text:
            ca_text = normalize(ca_text)
            # Reading the pages as blocks keeps what the default extraction leaves
            # behind: the page's own running head, printed between two entries and
            # glueing them into one — the second reference is then not merely
            # mis-segmented, it is GONE, and the citation naming it can only ever be
            # an orphan.  The default text never carries these lines, so the furniture
            # detected on it (above) cannot see them; they must be found here, on the
            # text that actually has them.
            heads = (boilerplate.running_head_lines(ca_text)
                     | boilerplate.boilerplate_lines(ca_text))
            if heads:
                ca_text = boilerplate.strip_boilerplate(ca_text, heads)
            # A fresh meta: the re-extraction is independent, so it must not
            # inherit the main extraction's footnote_references / page state.
            _, ca_biblio, _, _ = family.split_body_bibliography(ca_text, meta={})
            ca_refs = family.parse_references(ca_biblio, meta={})
            if len(ca_refs) >= 5 and len(ca_refs) > len(references) * 1.5:
                references = ca_refs
                _assign_keys(references)

    # Last-resort recovery from the PDF link layer: when reference parsing was a
    # disaster (very few entries) but the hyperlink layer carries many more
    # anchors, rebuild the list from those anchors (clean entry text, numbered by
    # reading order).  Deliberately narrow — only a genuine collapse (< 5 parsed
    # references) triggers it, so healthy documents are never touched — and it
    # only supplies reference entries: resolution stays normal, so a fabricated
    # or absent citation still becomes an orphan.
    if link_layer and len(references) < 5 and len(link_layer.references) >= 5:
        link_refs = _refs_from_link_layer(link_layer)
        if len(link_refs) > len(references):
            references = link_refs
            _assign_keys(references)

    _attach_external_reference_urls(references, external_links)

    # A "Further reading" list is not a bibliography: nobody cites it, by design.
    # Flagged (never dropped) once the references are final, so the entries are still
    # resolved and fetched like any other — only the coverage denominator, which asks
    # "did we read this paper's citation markers?", stops counting them against us.
    mark_further_reading(references, biblio if isinstance(biblio, str) else "\n".join(biblio))

    # Demand-driven boilerplate cleanup: re-segment a copy of the bibliography
    # with repeated watermark/header/footer lines removed, so the author-year
    # scheme can recover any reference a watermark glued into its neighbour.  Only
    # the residual orphans are recovered from this (see _rescue_via_boilerplate);
    # the primary parse above is untouched, so a false-positive removal loses
    # nothing.  Text-based, so it also applies to .txt inputs.
    boilerplate_refs = None
    boiler = boilerplate.boilerplate_lines(text)
    if boiler:
        cleaned_biblio = boilerplate.strip_boilerplate(biblio, boiler)
        if cleaned_biblio != biblio:
            boilerplate_refs = family.parse_references(cleaned_biblio, meta=extract_meta)
            for r in boilerplate_refs:
                r["manuscript_id"] = manuscript_id
                sur, yr, suf = authoryear.entry_key(r["raw_entry"])
                r["ay_surname"], r["ay_year"], r["ay_suffix"] = sur, yr, suf

    selected_scheme = citation_schemes.select(
        body, sentences, references, mode=citations_mode)
    mode = selected_scheme.NAME

    if citation_schemes.synthesizes_references(selected_scheme):
        # Drafts carry no bibliography; the inline identifiers ARE the references.
        # Number them after any entries a stray bibliography heading produced.
        references = references + selected_scheme.synthesize_references(
            sentences, manuscript_id, start_num=len(references))
    claims, citations, debug_rows, extra = selected_scheme.build(
        sentences, references, window, manuscript_id, fmt=fmt, link_layer=link_layer,
        boilerplate_refs=boilerplate_refs)

    # The bibliography is only now complete — the author-year scheme recovers
    # entries buried in the body during build — so this is the first point at
    # which the list can say what format it is written in.
    apply_reference_readers(references)
    footnote_notes, footnote_note_sources, claim_footnotes, footnote_note_parents = (
        _consume_footnote_source_metadata(references, citations, manuscript_id)
    )
    coverage_references = _coverage_references(
        references, footnote_note_sources, footnote_note_parents
    )

    result = {
        "manuscript": {
            "id": manuscript_id,
            "filename": os.path.basename(path),
            "format": fmt,
            "citation_mode": mode,
            "ocr": bool(extract_meta.get("ocr")),
            "ocr_method": extract_meta.get("pdf_method") if extract_meta.get("ocr") else None,
            "sha256": sha,
            "parser_version": PARSER_VERSION,
            "uploaded_at": now(),
            "full_text": text,
            "title_evidence": title_evidence,
        },
        "claims": claims,
        "references": references,
        "citations": citations,
        "ambiguities": extra.get("ambiguities", []),
        "table_only_citations": extra.get("table_only_citations", []),
        "table_citations_verified": verify_table_citations(),
        "_debug": {
            "bibliography_cut": cut_debug,
            "table_citations_verified": verify_table_citations(),
            "extract": extract_meta,
            "citation_mode": mode,
            "n_sentences": len(sentences),
            "n_claims": len(claims),
            "n_references": len(references),
            "n_markers": sum(n for _, n, _ in debug_rows),
            "ambiguities": extra.get("ambiguities", []),
            "orphans": extra.get("orphans", []),
            "suppressed_markers": extra.get("suppressed_markers", []),
            "missegmented_recovered": extra.get("missegmented_recovered", []),
            "table_only_citations": extra.get("table_only_citations", []),
            "reference_coverage": _reference_coverage(
                coverage_references, citations,
                # Absent (an extractor that does not declare) is NOT the same as a
                # declared None (a format that structurally cannot); only the latter
                # is grounds for refusing the document.
                extract_meta.get("superscript_source", _SUPERSCRIPT_UNDECLARED),
                table_only=extra.get("table_only_citations", [])),
            "pdf_links": ({
                "references": len(link_layer.references),
                "citations": len(link_layer.citations),
                "resolved": len(link_layer.resolved()),
            } if link_layer else None),
        },
    }
    if footnote_notes:
        result.update({
            "footnote_notes": footnote_notes,
            "footnote_note_sources": footnote_note_sources,
            "claim_footnotes": claim_footnotes,
            "footnote_note_parents": footnote_note_parents,
        })
    cov = result["_debug"]["reference_coverage"]
    if cov["fatal"] and os.environ.get(
            "CITATION_VERIFIER_ALLOW_LOST_MARKERS", "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        raise CitationMarkersLost(
            f"{cov['warning']}. Supply a copy that kept its superscripts: a PDF or DOCX "
            f"always does, and a Markdown export does when the converter writes them out "
            f"(<sup>1</sup>, ^1^, ¹). This file shows none. "
            f"(To parse it anyway, knowing most citations are missing: "
            f"CITATION_VERIFIER_ALLOW_LOST_MARKERS=1.)")
    # Optional assisted manual resolution of doubtful orphans (interactive only).
    # A genuine defect with no similar reference is never offered, so it stays a
    # flagged orphan; the user's choices are recorded as user_selected.
    if interactive:
        try:
            from core.parse import orphan_match as _orphan_match
        except ImportError:  # pragma: no cover - direct-execution import path
            import orphan_match as _orphan_match
        _orphan_match.interactive_resolve(result, ask=ask)
    debug_md = _render_debug(result, debug_rows, cut_debug)
    return result, debug_md


def _render_debug(result: dict, rows, cut_debug) -> str:
    d = result["_debug"]
    out = ["# Parser debug view (§11)\n"]
    out.append(f"- Format: **{result['manuscript']['format']}**")
    out.append(f"- Citation mode: **{d.get('citation_mode')}**")
    em = d.get("extract") or {}
    if em.get("bib_source"):
        out.append(f"- LaTeX bibliography from: `{em['bib_source']}`")
    if em.get("unknown_cite_keys"):
        out.append(f"- ⚠️ \\cite keys without a bibliography entry (→ orphan citations): "
                   + ", ".join(f"`{k}`→[{n}]" for k, n in em["unknown_cite_keys"].items()))
    if em.get("pdf_method"):
        out.append(f"- PDF extraction: `{em['pdf_method']}` (best-effort: check the sentences below)")
    out.append(f"- Total sentences: **{d['n_sentences']}**")
    out.append(f"- Claim sentences (with marker): **{d['n_claims']}**")
    out.append(f"- Total markers: **{d['n_markers']}**  "
               f"(claims ≠ markers: adjacent markers merge into one group = 1 claim; "
               f"marker groups separated by prose = separate claims)")
    if citation_schemes.synthesizes_references(d.get("citation_mode")):
        out.append(f"- References synthesised from inline DOIs/arXiv ids "
                   f"(draft format, no bibliography): **{d['n_references']}**\n")
    else:
        out.append(f"- References in bibliography: **{d['n_references']}**\n")
    cov = d.get("reference_coverage") or {}
    if cov.get("warning"):
        out.append(f"- 🚨 **Citation markers probably lost** — {cov['warning']}\n")
    if cov.get("total"):
        out.append(
            f"- Reference coverage: **{cov['cited']}/{cov['total']} "
            f"({cov['pct']:.1f}%)** cited — {cov.get('in_prose', 0)} in prose"
            + (f", {cov['in_tables']} only in a table" if cov.get("in_tables") else "")
        )
        # The list to read.  A reference the document never names, anywhere, is
        # either a marker we failed to find or one the authors never cited — and
        # there is no third possibility, which is what makes it worth printing.
        uncited = cov.get("uncited") or []
        if uncited:
            shown = ", ".join(f"[{n}]" for n in uncited[:25])
            out.append(
                f"- ⚠️ **Cited nowhere — not in prose, not in a table: "
                f"{len(uncited)}** — {shown}"
                + (" …" if len(uncited) > 25 else "")
            )
        out.append("")
    if cov.get("table_warning"):
        out.append(
            f"- ⚠️ **Table-only citation concentration** — "
            f"{cov['table_warning']}\n"
        )
    if d.get("table_citations_verified"):
        out.append("- Table citations: **verified** (`--verify-table-citations`) — a "
                   "marker inside a table row produces a claim like any other\n")
    sup = d.get("suppressed_markers") or []
    if sup:
        paren_sup = [s for s in sup if s.get("reason") is None]
        formula_sup = [s for s in sup if s.get("reason") == "formula_context"]
        table_sup = [s for s in sup if s.get("reason") == "table_row"]
        ambiguous_table_sup = [
            s for s in table_sup if s.get("structural_ambiguity")
        ]
        if paren_sup:
            out.append(
                f"- Parenthetical numbers suppressed as non-citations "
                f"(document citation style is [n]/superscript): **{len(paren_sup)}** — "
                + ", ".join(
                    f"`{s['marker_raw']}` (sent. {s['sentence_index']})"
                    for s in paren_sup[:15]
                )
                + (" …" if len(paren_sup) > 15 else "")
                + "\n"
            )
        if formula_sup:
            out.append(
                f"- Bracket markers suppressed (formula/expression context): "
                f"**{len(formula_sup)}** — "
                + ", ".join(
                    f"`{s['marker_raw']}` (sent. {s['sentence_index']})"
                    for s in formula_sup[:15]
                )
                + (" …" if len(formula_sup) > 15 else "")
                + "\n"
            )
        if table_sup:
            out.append(
                f"- Markers suppressed (table / benchmark row): "
                f"**{len(table_sup)}** — "
                + ", ".join(
                    f"`{s['marker_raw']}` (sent. {s['sentence_index']})"
                    for s in table_sup[:15]
                )
                + (" …" if len(table_sup) > 15 else "")
                + "\n"
            )
        if ambiguous_table_sup:
            out.append(
                "- ⚠️ **Markers at ambiguous checklist boundaries (suppressed "
                f"by default, opt in with `--verify-table-citations`): "
                f"{len(ambiguous_table_sup)}** — "
                + ", ".join(
                    f"`{s['marker_raw']}` (sent. {s['sentence_index']})"
                    for s in ambiguous_table_sup[:15]
                )
                + (" …" if len(ambiguous_table_sup) > 15 else "")
                + "\n"
            )
    table_only = d.get("table_only_citations") or []
    if table_only:
        out.append(
            f"- ⚠️ **Cited ONLY inside a table (never in prose → NOT verified, "
            f"review by hand): {len(table_only)}**\n"
        )
        out.append("## Table-only citations — no prose mention, no verify task\n")
        for t in table_only:
            entry = (t.get("raw_entry") or "").strip()
            out.append(
                f"- `{t.get('marker_raw')}` → [{t.get('ref_number')}]"
                + (f" {entry}" if entry else "")
            )
        out.append("")
    out.append("## Bibliography cut point\n")
    out.append(f"- Cut line: `{cut_debug.get('cut_line_index')}` "
               f"of `{cut_debug.get('total_lines')}`")
    out.append(f"- Context around cut:\n\n> {cut_debug.get('context', '')}\n")
    amb = d.get("ambiguities") or []
    orph = d.get("orphans") or []
    if amb:
        out.append(f"- **Ambiguous** citations (resolution suspended, user decides): "
                   f"**{len(amb)}**")
        if amb:
            out.append("## AMBIGUOUS citations — which source? (you decide)\n")
            for a in amb:
                cands = "; ".join(f"[{c['ref_number']}] {c['raw_entry']}" for c in a["candidates"])
                out.append(f"- `{a['marker_raw']}` → candidates: {cands}")
            out.append("")
    if orph:
        out.append(f"- **Orphan** citations (no entry in bibliography): **{len(orph)}**\n")
        if orph:
            out.append("## ORPHAN citations (not in bibliography)\n")
            for o in orph:
                out.append(f"- `{o.get('marker_raw', '?')}`")
            out.append("")
    user_res = d.get("user_resolved") or []
    if user_res:
        out.append(f"- **User-selected** associations (manually chosen for an "
                   f"orphan): **{len(user_res)}**\n")
        out.append("## USER-SELECTED associations (chosen by hand)\n")
        for u in user_res:
            entry = (u.get("raw_entry") or "").strip()
            out.append(f"- `{u.get('marker_raw')}` → [{u.get('ref_number')}]"
                       + (f" {entry}" if entry else ""))
        out.append("")
    rec = d.get("missegmented_recovered") or []
    if rec:
        out.append(f"- **Recovered** sources (present in bibliography but "
                   f"mis-segmented, split out with correct metadata): **{len(rec)}**\n")
        out.append("## RECOVERED sources (split from a mis-segmented entry)\n")
        for r in rec:
            if r.get("provenance") == "link_aliased":
                # No new entry here: the PDF's link says this citation names a
                # reference we already read, under a name the authors mistyped.
                out.append(f"- ({r['surname']}, {r['year']}) → existing ref "
                           f"[{r['ref_number']}] “{r.get('title') or '?'}” "
                           f"(the PDF link points there; the citation spells it "
                           f"differently)")
                continue
            out.append(f"- ({r['surname']}, {r['year']}) → new ref [{r['ref_number']}] "
                       f"“{r.get('title') or '?'}” (split from [{r.get('split_from_ref_number')}])")
        out.append("")
    out.append("## Claim sentences found (numbered)\n")
    for n, nmark, text in rows:
        flag = f"  ⟵ {nmark} marker(s)" if nmark else ""
        out.append(f"{n}. {text}{flag}")
    return "\n".join(out) + "\n"


def main():
    supported_input_formats = _supported_input_extensions()
    citation_mode_choices = citation_schemes.mode_names(include_auto=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "--docx", dest="input", required=True,
                    help=f"manuscript: {' | '.join(supported_input_formats)}")
    ap.add_argument("--debug", required=True)
    ap.add_argument("--window", type=int, default=1)
    ap.add_argument("--citations", default="auto",
                    choices=list(citation_mode_choices),
                    help="citation scheme; 'auto' detects the scheme from the manuscript text")
    ap.add_argument("--auto", action="store_true",
                    help="resolve everything automatically; do not prompt to match "
                         "doubtful orphan citations by hand")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: file not found: {args.input}", file=sys.stderr)
        sys.exit(2)
    # Offer manual orphan matching only in a real terminal and when --auto is off,
    # so scripted/piped runs are never blocked on input.
    interactive = (not args.auto) and sys.stdin.isatty() and sys.stderr.isatty()
    try:
        result, debug_md = parse(args.input, args.window, args.citations,
                                 interactive=interactive)
    except zipfile.BadZipFile:
        bad_ext = os.path.splitext(args.input)[1].lower() or "zip-backed input"
        print(f"ERROR: unreadable zip container for {bad_ext}.", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        # Insufficient extraction (unreadable PDF) or unsupported format:
        # EXPLICIT failure, never a silently empty parse.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)

    with open(args.debug, "w", encoding="utf-8") as f:
        f.write(debug_md)

    d = result["_debug"]

    # No verifiable claims: this is a bibliography/reference list, not a manuscript
    # with in-context citations. CitationVerifier verifies CITATIONS IN CONTEXT, not
    # the bare existence of references — for that there are dedicated tools. Stop
    # explicitly (exit 4) instead of running the whole pipeline on nothing. The parse
    # outputs are still written so the user can inspect what was detected.
    if d["n_claims"] == 0:
        print(json.dumps({
            "ok": False,
            "stop_reason": "no_verifiable_claims",
            "n_references": d["n_references"],
            "n_sentences": d["n_sentences"],
            "debug": args.debug,
            "parse": result,
        }, ensure_ascii=False))
        print(
            "STOP: no verifiable claims found (0 in-context citations). This document "
            "looks like a bibliography / reference list, not a manuscript with "
            "citations in context. CitationVerifier checks whether cited sources "
            "support the statements that cite them; it does not audit bare reference "
            "lists (other tools do that). If you expected claims, check the "
            "citation scheme and the extracted text in the debug view.",
            file=sys.stderr)
        sys.exit(4)

    print(json.dumps({
        "ok": True,
        "citation_mode": d["citation_mode"],
        "n_claims": d["n_claims"],
        "n_references": d["n_references"],
        "n_markers": d["n_markers"],
        "ambiguous": len(d.get("ambiguities") or []),
        "orphans": len(d.get("orphans") or []),
        "debug": args.debug,
        "parse": result,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
