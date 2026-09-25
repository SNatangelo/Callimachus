#!/usr/bin/env python3
# core/parse/citation_schemes/author_year.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Author-year citation scheme."""

from __future__ import annotations

import importlib
import re

NAME = "author-year"

# Table-row detection: common column labels and metrics, deliberately broad
# enough for result tables across domains rather than any one paper or field.
_DECIMAL_RE = re.compile(r"\d+\.\d+")
_BENCHMARK_HEADER_RE = re.compile(
    r"^\s*(?:system|model|method|approach)\b"
    r"(?:(?:\s+|[/|,–—-])+(?:dev|test|validation|score|accuracy|f1|bleu|rouge)\b){2,}",
    re.IGNORECASE,
)
_BENCHMARK_PROSE_PREDICATE_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|achiev(?:e|es|ed)|"
    r"score(?:s|d)?|report(?:s|ed)?|obtain(?:s|ed)?|reach(?:es|ed)?|"
    r"remain(?:s|ed)?|show(?:s|ed)?|indicat(?:e|es|ed)|"
    r"demonstrat(?:e|es|ed)|suggest(?:s|ed)?|perform(?:s|ed)?|"
    r"outperform(?:s|ed)?|improv(?:e|es|ed)|exceed(?:s|ed)?)\b",
    re.IGNORECASE,
)
_YEAR_PAREN_RE = re.compile(r"\([^()]*\b(?:19|20)\d{2}[a-z]?[^()]*\)")


def _benchmark_header_has_prose(sentence: str) -> bool:
    """Reject the table signal when the metric header participates in prose."""
    if _BENCHMARK_PROSE_PREDICATE_RE.search(sentence):
        return True
    header = _BENCHMARK_HEADER_RE.match(sentence)
    remainder = sentence[header.end():] if header else sentence
    without_parenthetical_cites = _YEAR_PAREN_RE.sub(" ", remainder)
    lower_words = [
        word
        for word in re.findall(r"[A-Za-zÀ-ÿ]+", without_parenthetical_cites)
        if word.islower() and word not in {"et", "al", "and", "vs", "v"}
    ]
    if (
        len(lower_words) >= 2
        and re.search(
            r"\b(?:a|an|the|this|that|these|those|which|who|while|whereas)\b",
            without_parenthetical_cites,
        )
    ):
        return True
    # A score followed directly by an em-dash clause is prose even when its
    # finite verb is outside the compact vocabulary above ("92.2—yielded...").
    return bool(re.search(r"\d\s*[–—]\s*[a-z]", remainder))


def _resolve_parse_module():
    try:
        return importlib.import_module("core.parse.parse_manuscript")
    except ImportError:
        return importlib.import_module("parse_manuscript")


def _resolve_authoryear_module():
    try:
        return importlib.import_module("core.parse.authoryear")
    except ImportError:
        return importlib.import_module("authoryear")


def _resolve_parsing_common():
    try:
        return importlib.import_module("core.parse.parsing_common")
    except ImportError:
        return importlib.import_module("parsing_common")


def _find_buried_reference(references, surname, year):
    """Locate a mis-segmented sub-reference for (surname, year) buried inside an
    entry whose own head key is something else.

    Anchored on the *known* surname+year, right after a previous entry's terminal
    year ("… 2020. Alex Krizhevsky. … 2009."), so it only fires where the source
    is literally present in the bibliography — never inventing a match for a
    citation with no textual trace (those must stay orphans)."""
    esc = re.escape(surname)
    # author block (Capitalised start) that contains the surname within its
    # first ~60 chars, then anything up to the target year on the same line.
    pat = re.compile(
        r"(?<=\d{4}[.)])\s+"
        r"([A-Z][\w.'’,&\- ]{0,60}?\b(?i:" + esc + r")\b[^\n]*?\b"
        + re.escape(str(year)) + r"\b[.)]?)"
    )
    for r in references:
        if (r.get("ay_surname"), r.get("ay_year")) == (surname, year):
            continue  # already this entry's own head — not buried
        m = pat.search(r.get("raw_entry") or "")
        if m:
            return r, m
    return None, None


def _rescue_missegmented_sources(sentences, references, by_sy, manuscript_id,
                                 authoryear_mod):
    """Split out sources the segmentation buried inside another entry.

    Demand-driven: only citations that are otherwise ORPHAN and whose
    (surname, year) is literally present (buried) in the bibliography are
    rescued.  A citation with no textual trace anywhere stays an orphan — that
    is the fabricated/absent-source signal the verifier must keep flagging.

    The buried span becomes its own reference with correct metadata (title, year
    via ``_make_reference``) and the *known* key assigned directly, sidestepping
    the single-author surname heuristic.  Mutates ``references`` in place and
    returns the provenance records of what was recovered."""
    pc = _resolve_parsing_common()
    wanted: dict[tuple, str] = {}
    for sent in sentences:
        for c in authoryear_mod.find_intext(sent):
            key = (c["surname"], c["year"])
            if key not in by_sy:
                wanted.setdefault(key, c.get("suffix", ""))
    if not wanted:
        return []
    recovered = []
    next_num = max((r.get("ref_number") or 0 for r in references), default=0)
    for (surname, year), suffix in wanted.items():
        host, m = _find_buried_reference(references, surname, year)
        if host is None:
            continue  # genuinely absent -> stays an orphan
        buried = m.group(1).strip()
        next_num += 1
        new_ref = pc._make_reference(next_num, buried)
        new_ref["manuscript_id"] = manuscript_id
        # Assign the known cited key rather than re-deriving it (the buried
        # single-author "First Last." would key on the first name otherwise).
        new_ref["ay_surname"] = surname
        new_ref["ay_year"] = year
        new_ref["ay_suffix"] = suffix
        new_ref["provenance"] = "split_missegmented"
        new_ref["split_from_ref_number"] = host.get("ref_number")
        # Trim the buried tail off the host; its head-derived metadata is intact.
        host["raw_entry"] = (host.get("raw_entry") or "")[:m.start(1)].rstrip()
        references.append(new_ref)
        recovered.append({
            "surname": surname, "year": year, "ref_number": next_num,
            "split_from_ref_number": host.get("ref_number"),
            "title": new_ref.get("title"),
        })
    return recovered


def _link_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9'\-]", "", (s or "").lower().replace("’", "'"))


def _link_target_for(cites, surname: str, year: int) -> str | None:
    """The unique link target for an orphan (surname, year), or None.

    Follows the PDF's own pointer: among the citation link boxes, keep those whose
    text contains the surname and that carry the year (inside the box or just to
    its right).  Returns the target only when it is *unique* — 0 matches (no link:
    a genuinely absent/fabricated source) or >1 targets (two same-year works, would
    need an a/b/c suffix to tell apart) both leave the citation an orphan."""
    sn = _link_norm(surname)
    if not sn:
        return None
    targets = {c.target for c in cites
               if sn in _link_norm(c.text) and year in (c.years or ())}
    return next(iter(targets)) if len(targets) == 1 else None


def _entry_fingerprint(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _existing_reference_for(entry, by_sy, authoryear_mod):
    """The reference already in the list that IS this link target, or None.

    The pointer often lands on an entry the text pipeline read perfectly well: the
    citation went orphan only because the name the authors *wrote* is a typo of the
    name the entry *carries* ("(Winkleman, 2012)" pointing at Winkelman's entry).
    Appending the target as a new reference then enters the same work twice — the
    bibliography grows an entry the paper does not have, the fetcher pulls the source
    twice, and the original is reported as cited nowhere while its copy carries the
    citation.  So resolve to the entry we already have and let the misspelled key
    become an alias of it (:func:`authoryear.build_index`).

    Two signals, both needed.  The target entry's own (surname, year) must select
    exactly one reference — a pair shared by two works says nothing about which one
    the link means.  And that reference's text must be the target's text: same author,
    same year and same title, not merely the same key."""
    src_surname, src_year, src_suffix = authoryear_mod.entry_key(entry)
    if not src_surname or src_year is None:
        return None
    hits = [r for r in by_sy.get((src_surname, src_year), [])
            if (r.get("ay_suffix") or "") == (src_suffix or "")]
    if len(hits) != 1:
        return None
    have, want = _entry_fingerprint(hits[0].get("raw_entry")), _entry_fingerprint(entry)
    if not have or not want:
        return None
    # Compare on the head of the entry — authors, year, title — because the two texts
    # come from different extractions of the same page and diverge at the tail (the
    # link layer keeps the DOI the text pipeline wrapped onto its own line, and so on).
    n = min(len(have), len(want), 60)
    if n < 30:
        return hits[0] if have == want else None
    return hits[0] if have[:n] == want[:n] else None


def _fuzzy_existing_for(surname, year, references):
    """The reference a link's citation resolves to when its target's entry text is too
    garbled to match on — same year, a surname a hair from the cited one ("Brennen"
    pointing at "Brennan"'s entry).

    Safe where a bare fuzzy match would not be: the PDF link has ALREADY proved this
    citation is real and points at a reference, so this only keeps the link-recovered
    entry from becoming a second copy of one the paper printed.  One clearly-closest
    same-year candidate, or nothing."""
    from difflib import SequenceMatcher
    cands = sorted(
        ((SequenceMatcher(None, surname or "", r.get("ay_surname") or "").ratio(), r)
         for r in references if r.get("ay_year") == year and r.get("ay_surname")),
        key=lambda t: t[0], reverse=True)
    # An EXACT same-name same-year hit is a DIFFERENT work — _existing_reference_for
    # keeps those apart by title, and the link may genuinely point at a new one.  Only
    # a near-miss (a typo of the printed name) is a dedup, so 1.0 is excluded.
    cands = [(s, r) for s, r in cands if s < 1.0]
    if cands and cands[0][0] >= 0.82 and (len(cands) == 1 or cands[1][0] < cands[0][0] - 0.1):
        return cands[0][1]
    return None


def _orphan_wanted(sentences, references, by_sy, authoryear_mod):
    """(surname, year) -> suffix for citations that are genuine orphans.

    Mirrors build()'s per-sentence suppression, on both counts: a forward cite whose
    year is already resolved by another cite in the same sentence (e.g. the
    reverse-lookup match for a compound surname — "Tjong Kim Sang" keys on "sang" but
    reads as "tjong"), or that the reverse pass resolved within its own span (see
    _is_forward_miskey), is NOT an orphan.  A rescue must not treat it as one and
    invent an entry for a surname the manuscript never cited.  Shared by the link and
    boilerplate rescues."""
    wanted: dict[tuple, str] = {}
    for sent in sentences:
        forward = authoryear_mod.find_intext(sent)
        reverse = authoryear_mod.find_intext_reverse(sent, references)
        resolved_years = set()
        for c in forward + reverse:
            if authoryear_mod.match(c, by_sy)[0] == "unique":
                resolved_years.add(c["year"])
        resolved_reverse_spans = [
            tuple(c["key_span"]) for c in reverse
            if c.get("key_span") and authoryear_mod.match(c, by_sy)[0] == "unique"]
        span_count: dict[tuple, int] = {}
        for c in forward:
            span = tuple(c.get("span") or (0, 0))
            span_count[span] = span_count.get(span, 0) + 1
        for c in forward:
            key = (c["surname"], c["year"])
            if key in by_sy or c["year"] in resolved_years:
                continue
            if _is_forward_miskey(c, span_count, resolved_reverse_spans):
                continue
            wanted.setdefault(key, c.get("suffix", ""))
    return wanted


def _rescue_via_boilerplate(sentences, references, by_sy, manuscript_id,
                            authoryear_mod, boilerplate_refs):
    """Recover orphans a watermark/running-header glued into another entry.

    ``boilerplate_refs`` is the bibliography re-segmented from a copy with the
    repeated watermark lines removed (see :mod:`core.parse.boilerplate`).  For a
    genuine orphan whose (surname, year) is a *unique* entry in that cleaned
    segmentation, the entry is added with provenance ``boilerplate_recovered``.

    Demand-driven and additive: the normal parse is untouched and only orphans
    are recovered, so a false-positive boilerplate removal can never lose a
    reference the pipeline already parsed."""
    if not boilerplate_refs:
        return []
    pc = _resolve_parsing_common()
    clean_by_sy: dict[tuple, list] = {}
    for r in boilerplate_refs:
        key = (r.get("ay_surname"), r.get("ay_year"))
        if key[0] and key[1]:
            clean_by_sy.setdefault(key, []).append(r)
    if not clean_by_sy:
        return []
    wanted = _orphan_wanted(sentences, references, by_sy, authoryear_mod)
    if not wanted:
        return []
    recovered = []
    next_num = max((r.get("ref_number") or 0 for r in references), default=0)
    for (surname, year), suffix in wanted.items():
        hits = clean_by_sy.get((surname, year))
        if not hits or len(hits) != 1:
            continue  # absent, or still ambiguous after cleaning -> stays orphan
        entry = (hits[0].get("raw_entry") or "").strip()
        if not entry:
            continue
        next_num += 1
        new_ref = pc._make_reference(next_num, entry)
        new_ref["manuscript_id"] = manuscript_id
        new_ref["ay_surname"] = surname
        new_ref["ay_year"] = year
        new_ref["ay_suffix"] = suffix
        new_ref["provenance"] = "boilerplate_recovered"
        new_ref["split_from_ref_number"] = None
        references.append(new_ref)
        recovered.append({
            "surname": surname, "year": year, "ref_number": next_num,
            "split_from_ref_number": None, "title": new_ref.get("title"),
            "provenance": "boilerplate_recovered",
        })
    return recovered


def _rescue_via_links(sentences, references, by_sy, manuscript_id,
                      authoryear_mod, link_layer):
    """Resolve otherwise-orphan citations by following the PDF hyperlink pointer.

    When a PDF carries ``hyperref`` named links, each in-text citation points at
    its exact reference (see :mod:`core.parse.pdf_links`).  For a citation the
    text pipeline left orphan, we follow that pointer to the authoritative entry
    and add it with provenance ``link_resolved`` — recovering both sources buried
    by mis-segmentation (the entry was present but swallowed by e.g. a watermark)
    and sources whose in-text text was mistyped/garbled (``citation_text_mismatch``
    flags the latter, since the pointer resolves the author's intent even when the
    written name does not match).

    Three invariants are preserved:
      * a citation with **no** link stays an orphan — in a hyperref PDF you cannot
        link a key that does not exist, so no-link is the fabricated/absent signal;
      * a citation whose (surname, year) maps to **more than one** target stays an
        orphan (ambiguous — would need a suffix to disambiguate);
      * a target we **already have** does not become a second entry: the citation is
        aliased onto the reference the paper printed (see
        :func:`_existing_reference_for`), so the bibliography never grows a work the
        paper does not carry.

    Purely additive: non-orphan citations and PDFs without links are untouched, so
    documents that already resolve cleanly cannot regress."""
    if not link_layer or not getattr(link_layer, "citations", None):
        return []
    pc = _resolve_parsing_common()
    cites = link_layer.citations
    wanted = _orphan_wanted(sentences, references, by_sy, authoryear_mod)
    if not wanted:
        return []
    recovered = []
    next_num = max((r.get("ref_number") or 0 for r in references), default=0)
    for (surname, year), suffix in wanted.items():
        target = _link_target_for(cites, surname, year)
        if target is None:
            continue  # no link (absent/fabricated) or ambiguous -> stays orphan
        ref = link_layer.references.get(target)
        entry = (ref.text or "").strip() if ref else ""
        # The pointer may land on an entry we already have: then the citation joins it
        # under an alias, and the bibliography stays the bibliography the paper printed.
        # With target text, match on it — key AND title, so a DIFFERENT work by the same
        # author and year is not merged in.  With NO target text (the anchor's slice came
        # out empty), that check is impossible, so fall back to the orphan's near-name at
        # the same year — a dedup the link corroborates (it proved the citation real).
        existing = (_existing_reference_for(entry, by_sy, authoryear_mod) if entry
                    else _fuzzy_existing_for(surname, year, references))
        if existing is not None and (suffix or "") == (existing.get("ay_suffix") or ""):
            aliases = existing.setdefault("ay_aliases", [])
            if [surname, year] not in aliases:
                aliases.append([surname, year])
            existing["citation_text_mismatch"] = True
            existing["linked_target"] = target
            recovered.append({
                "surname": surname, "year": year,
                "ref_number": existing.get("ref_number"),
                "split_from_ref_number": None, "title": existing.get("title"),
                "provenance": "link_aliased", "linked_target": target,
                "citation_text_mismatch": True,
            })
            continue
        if not entry:
            continue  # nothing to alias onto, and no text to build a new entry from
        next_num += 1
        new_ref = pc._make_reference(next_num, entry)
        new_ref["manuscript_id"] = manuscript_id
        # The cited key is the orphan's own (surname, year); the entry text is the
        # intended source the pointer resolved to (may differ on a typo/garble).
        new_ref["ay_surname"] = surname
        new_ref["ay_year"] = year
        new_ref["ay_suffix"] = suffix
        new_ref["provenance"] = "link_resolved"
        new_ref["split_from_ref_number"] = None
        source_surname = authoryear_mod.entry_key(entry)[0]
        mismatch = bool(source_surname) and source_surname != surname
        new_ref["citation_text_mismatch"] = mismatch
        new_ref["linked_target"] = target
        references.append(new_ref)
        recovered.append({
            "surname": surname, "year": year, "ref_number": next_num,
            "split_from_ref_number": None, "title": new_ref.get("title"),
            "provenance": "link_resolved", "linked_target": target,
            "citation_text_mismatch": mismatch,
        })
    return recovered


def _is_forward_miskey(cite, span_count, resolved_reverse_spans) -> bool:
    """True when this forward orphan is the forward pass mis-reading a mention the
    reverse pass already resolved, rather than a citation of its own.

    "Frantz Fanon (2008)" is read by the organisational-author pattern as a body
    named "Frantz …", keying the cite on the given name — a surname no bibliography
    carries, hence a permanent orphan — while the reverse pass finds Fanon's entry
    inside that very same mention.  The same-year test below cannot catch it: a
    reprint keys on the bracketed original ("[1963] 2008") and is cited by the
    edition, so the two readings do not even share a year.

    Two conditions, both needed.  The reverse match (its key_span — surname to year,
    the citation proper) must sit INSIDE this cite's span: the two are then readings
    of one mention, not two citations.  And that span must belong to this cite alone:
    the citations of a
    parenthetical group all share the group's span, so without this test a
    fabricated source cited next to a real one — "(Tjong Kim Sang 2003; Nonexistent
    1999)" — would be suppressed by its neighbour's reverse match.  A cite alone on
    its span has no neighbour whose match it could borrow."""
    span = tuple(cite.get("span") or (0, 0))
    if span_count.get(span, 0) != 1:
        return False
    start, end = span
    return any(start <= r_start and r_end <= end
               for r_start, r_end in resolved_reverse_spans)


def _already_read_forward(cite, forward_hits, by_sy, authoryear_mod) -> bool:
    """True when the forward pass already read THIS mention, of THIS work.

    The reverse pass is a gap-filler: it goes looking in the sentence for bibliography
    surnames the forward regex could not see.  Where the forward pass now sees them
    after all — "Frantz Fanon (2008)" keys on the given name, and the entry's own
    surname answers for it (authoryear._own_name_keys) — the gap it was filling is
    closed, and its cite is a second reading of a mention already counted.  The
    (surname, year) filter cannot catch it: the whole point is that the two passes key
    the same mention on different words.

    So ask what a duplicate actually is: the same REFERENCE (the resolved entry, not a
    key), read at the same PLACE (the reverse match's key_span — surname to year — sits
    inside the forward cite's span).  Two citations of one work in one sentence are not
    at one place, and a work the forward pass never resolved is not this."""
    kind, res = authoryear_mod.match(cite, by_sy)
    if kind != "unique":
        return False
    start, end = cite.get("key_span") or cite.get("span") or (0, 0)
    return any(f_kind == "unique" and f_res is res and f_start <= start and end <= f_end
               for (f_start, f_end), (f_kind, f_res) in forward_hits)


def detect(body: str, sentences: list[str], references: list[dict]) -> float:
    del references
    authoryear_mod = _resolve_authoryear_module()
    return float(authoryear_mod.looks_authoryear(body, sentences))


def _is_ay_table_row(sentence: str) -> bool:
    """Heuristics that catch author-year citations embedded in table rows.

    Table rows are NOT prose — they are data: benchmark scores, F1 values,
    BLEU columns.  Two format-agnostic signals, with tiered thresholds:

    * **Decimal density** — >=2 decimal numbers (``\\d+\\.\\d+``) with low
      alphabetic density relative to the number of decimals.  The more
      decimals, the more certain we are this is tabular data rather than
      prose, so the alpha ceiling rises with decimal count.
    * **LaTeX ``&`` separators** — >=2 ampersands with < 30 % alpha.
    """
    if _resolve_parsing_common().STRUCTURAL_TABLE_SENTINEL in sentence:
        return True

    alpha = sum(1 for c in sentence if c.isalpha())
    total = len(sentence) or 1
    n_dec = len(_DECIMAL_RE.findall(sentence))

    # Signal A: >=2 decimal numbers.
    # Tiered alpha thresholds — more decimals → more confident it's a table.
    if n_dec >= 5:
        # Dense decimal cluster: nearly always a table, even with longer
        # text labels (column headers, model names, approach descriptions).
        if alpha / total < 0.70:
            return True
    elif n_dec >= 2 and alpha / total < 0.45:
        return True

    # A compact benchmark row can carry enough alphabetic model names to miss
    # the density ceiling (e.g. "System Dev F1 Test F1 ELMo (...) 95.7 ...").
    # Header vocabulary + several cited systems + several scores is a stronger
    # structural signature than alpha density alone.
    cited_years = re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", sentence)
    if (
        n_dec >= 2
        and len(cited_years) >= 2
        and _BENCHMARK_HEADER_RE.match(sentence)
        and not _benchmark_header_has_prose(sentence)
    ):
        return True

    # Signal B: LaTeX & column separators (>=2) + very low alpha density.
    if sentence.count('&') >= 2 and alpha / total < 0.30:
        return True

    return False


def _deshatter_link_key(text, years):
    """The (surname, year) a link's shattered in-text text names — "( Zav i a ˇci ˇc
    1999 ," -> ("zaviacic", 1999) — spaces and broken accents folded away so it meets
    the reference index the way a clean citation would.  A year-only link ("1880 ,",
    the narrative "Alexander Skene … in 1880") yields (None, 1880)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    m = re.search(r"(1[6-9]\d\d|20\d\d)", t)
    year = int(m.group(1)) if m else (min(years) if years else None)
    head = t[:m.start()] if m else t
    surname = re.sub(r"[^a-z]", "", head.lower()) or None
    return surname, year


def _rescue_uncited_via_links(references, citations, by_sy, link_layer):
    """Mark a reference cited when the PDF hyperlinks it but the text never read the
    citation — a *silent miss*: a name the extractor shattered ("Zav i a ˇci ˇc") or a
    year-only link the narrative buried ("Alexander Skene … in 1880").  Neither raises
    an orphan, so the orphan-driven link rescue above never reaches it.

    Link-only and additive: it consults the PDF's own pointers, only ever marks an
    OTHERWISE-uncited reference (never re-points a match), and does nothing on the
    text-off pathway (no link layer).  Two matches, both corroborated by the link:
      * (surname, year) — the shattered name folded back, joined on the index;
      * (year) alone, but only when that year is UNIQUE among the references, so it
        cannot name any other."""
    if not link_layer or not getattr(link_layer, "citations", None):
        return []
    cited = {c.get("ref_id") for c in citations if c.get("ref_id")}
    uncited_ids = {r["id"] for r in references
                   if r.get("id") and r.get("id") not in cited}
    if not uncited_ids:
        return []
    by_year: dict = {}
    for r in references:
        by_year.setdefault(r.get("ay_year"), []).append(r)
    added, newly = [], set()
    for c in link_layer.citations:
        surname, year = _deshatter_link_key(getattr(c, "text", ""),
                                            getattr(c, "years", None) or set())
        if year is None:
            continue
        target = None
        if surname:
            grp = by_sy.get((surname, year)) or []
            if len(grp) == 1:
                target = grp[0]
        if target is None:                       # year-unique fallback
            same_year = by_year.get(year) or []
            if len(same_year) == 1:
                target = same_year[0]
        rid = target.get("id") if target else None
        if rid is None or rid not in uncited_ids or rid in newly:
            continue
        newly.add(rid)
        added.append({
            "ref_id": rid, "ref_number": target.get("ref_number"), "claim_id": None,
            "provenance": "link_silent_resolved",
            "marker_raw": (getattr(c, "text", "") or "").strip(),
        })
    return added


def build(sentences, references, window, manuscript_id, fmt=None, link_layer=None,
          boilerplate_refs=None):
    parse_mod = _resolve_parse_module()
    authoryear_mod = _resolve_authoryear_module()
    by_sy = authoryear_mod.build_index(references)
    # Rescue sources the two-column segmentation buried inside another entry
    # (present-but-mis-segmented), then rebuild the index.  Citations with no
    # textual trace remain orphans below.
    recovered = _rescue_missegmented_sources(
        sentences, references, by_sy, manuscript_id, authoryear_mod)
    if recovered:
        by_sy = authoryear_mod.build_index(references)
    # Recover orphans a watermark/running-header glued into another entry, using
    # a boilerplate-cleaned re-segmentation as the source.  No-op without one.
    boiler_recovered = _rescue_via_boilerplate(
        sentences, references, by_sy, manuscript_id, authoryear_mod, boilerplate_refs)
    if boiler_recovered:
        by_sy = authoryear_mod.build_index(references)
        recovered = recovered + boiler_recovered
    # Last resort: when the PDF carries a hyperlink layer, resolve remaining
    # orphans by following its pointers (also catches mistyped/garbled names).
    link_recovered = _rescue_via_links(
        sentences, references, by_sy, manuscript_id, authoryear_mod, link_layer)
    if link_recovered:
        by_sy = authoryear_mod.build_index(references)
        recovered = recovered + link_recovered
    claims, citations, rows = [], [], []
    ambiguities, orphans, suppressed = [], [], []
    for idx, sent in enumerate(sentences):
        cites = authoryear_mod.find_intext(sent)
        # A no-year "(Author, in press)" citation is demand-driven: kept only when it
        # reaches an in-press entry.  A discourse word misread as the first author
        # ("First, Spinhoven … (in press)") reaches none, so it is dropped, never
        # surfaced as an orphan — the fallback only ever resolves, never accuses.
        cites = [c for c in cites
                 if c["year"] != authoryear_mod.NOYEAR
                 or authoryear_mod.match(c, by_sy)[0] == "unique"]
        # Reverse lookup: search for bibliography (surname, year) pairs
        # that the forward regex missed — compound surnames, garbled PDF text.
        # Only search for references whose (surname, year) was NOT found by the
        # forward pass, so we fill gaps without creating duplicates.
        forward_keys = {(c["surname"], c["year"]) for c in cites}
        extra = authoryear_mod.find_intext_reverse(sent, references)
        # Keep only reverse cites that add new (surname, year) pairs.
        extra = [c for c in extra if (c["surname"], c["year"]) not in forward_keys]
        # The forward pass now reads for itself the names this reverse pass was written
        # to rescue: "Frantz Fanon (2008)" keys on the forename, and the entry's own
        # surname answers for it (authoryear._own_name_keys).  The two passes then read
        # the SAME mention of the SAME work — under two different keys, so the (surname,
        # year) filter above cannot see it — and the paper is reported as citing Fanon
        # twice where it cited him once.
        forward_hits = [(tuple(c.get("span") or (0, 0)), authoryear_mod.match(c, by_sy))
                        for c in cites]
        extra = [c for c in extra if not _already_read_forward(c, forward_hits, by_sy,
                                                               authoryear_mod)]
        # Where the reverse pass actually read a citation, for the cites of it that
        # resolve to an entry.
        resolved_reverse_spans = [
            tuple(c["key_span"]) for c in extra
            if c.get("key_span") and authoryear_mod.match(c, by_sy)[0] == "unique"]
        span_count: dict[tuple, int] = {}
        for c in cites:
            span = tuple(c.get("span") or (0, 0))
            span_count[span] = span_count.get(span, 0) + 1
        # Compound surnames: a reverse-lookup cite (e.g. "sang" from "Tjong Kim
        # Sang and De Meulder, 2003") lands at a sub-span INSIDE a forward
        # parenthetical group (e.g. "(Tjong Kim Sang ...; Rajpurkar ...)"), whose
        # forward cites all share the whole-parenthesis span.  Re-map the reverse
        # cite onto the enclosing forward span so co-cited sources stay in ONE
        # group (one claim) instead of splitting off a duplicate claim.
        forward_spans = [tuple(c["span"]) for c in cites if c.get("span")]
        for c in extra:
            cs, ce = c.get("span") or (0, 0)
            for fs, fe in forward_spans:
                if fs <= cs and ce <= fe:
                    c["span"] = (fs, fe)
                    break
        cites = cites + extra
        # Table-row detection: suppress all citations in lines that look like
        # tabular data (benchmark scores, LaTeX column separators with low alpha).
        # With --verify-table-citations the row is kept and verified like prose.
        if cites and _is_ay_table_row(sent) and not parse_mod.verify_table_citations():
            parsing_common = _resolve_parsing_common()
            ambiguous_table = (
                parsing_common.STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL in sent
            )
            for c in cites:
                entry = {"marker_raw": c["marker_raw"], "sentence_index": idx + 1,
                         "reason": "table_row",
                         "structural_ambiguity": ambiguous_table}
                kind, res = authoryear_mod.match(c, by_sy)
                if kind == "unique":
                    entry["ref_numbers"] = [res["ref_number"]]
                suppressed.append(entry)
            rows.append((idx + 1, 0, parse_mod._display(sent)))
            continue
        rows.append((idx + 1, len(cites), parse_mod._display(sent)))
        if not cites:
            continue
        # Citations sharing a span (same parenthetical group "(A 2020; B 2021)")
        # cite together; distinct groups scope different sentence fragments and
        # become separate claims, mirroring the numeric builder.
        group_map: dict[tuple, list] = {}
        for cite in cites:
            group_map.setdefault(tuple(cite.get("span") or (0, 0)), []).append(cite)
        group_spans = sorted(group_map)
        groups = [group_map[k] for k in group_spans]
        det = authoryear_mod.detection_text(sent)
        groups, group_spans = parse_mod._coalesce_dependent_marker_groups(
            det, groups, group_spans)
        frags, split = parse_mod._fragment_texts(
            det, group_spans, exact_openers=True)
        # A leading comparative shell ("Unlike A and B, we ...") assigns both
        # sources to one proposition.  When the generic fragment validator also
        # refuses to split it, building one duplicate full-sentence claim per
        # group creates C39/C40-style copies with different references.  Keep
        # one claim and let its citation rows carry every source instead.
        comparative_shell = bool(
            re.match(
                r"^\s*(?:unlike|similar to|compared (?:with|to)|in contrast to)\b",
                det,
                re.IGNORECASE,
            )
            and re.match(r"\s*,", det[max(end for _start, end in group_spans):])
        )
        if not split and len(groups) > 1 and comparative_shell:
            groups = [[cite for group in groups for cite in group]]
            frags = [det]
        # Pre-scan: collect years resolved by ANY cite in this sentence.
        # Forward orphans (e.g. surname="tjong") and reverse-resolved cites
        # (e.g. surname="sang") can land in different span groups; per-sentence
        # tracking ensures an orphan whose year is resolved elsewhere in the
        # same sentence is suppressed.
        sentence_resolved_years: set[int] = set()
        for cite in cites:
            kind, res = authoryear_mod.match(cite, by_sy)
            if kind == "unique":
                sentence_resolved_years.add(cite["year"])
        for group_index, (group, frag) in enumerate(zip(groups, frags)):
            matched_nums, cit_rows = [], []
            grp_amb, grp_orph = [], []
            kept_raw: list[str] = []
            for cite in group:
                kind, res = authoryear_mod.match(cite, by_sy)
                # A heading word or other noise can take the surname slot the org scan
                # keys on ("… Ruminative Structures Conway and Pleydell-Pearce's (2000)"
                # keys on "structures", and its own-name fallback then reads the run's
                # real first author as an ambiguous alternative; the reverse pass reads
                # "conway" but without its co-author, leaving it ambiguous too).  Re-read
                # the marker's own author run and key on its FIRST author — never a
                # trailing co-author — supplying the co-authors that disambiguate, and
                # accept it only when it uniquely resolves, so a mis-read or under-read
                # name is corrected without inventing a match.
                if kind != "unique":
                    retraced = authoryear_mod.retrace_surname(
                        cite["marker_raw"], cite["year"])
                    if retraced is not None:
                        rk, rr = authoryear_mod.match(retraced, by_sy)
                        if rk == "unique":
                            kind, res = "unique", rr
                if kind == "unique":
                    matched_nums.append(res["ref_number"])
                    cit_rows.append({"ref_id": res["id"], "ref_number": res["ref_number"]})
                    kept_raw.append(cite["marker_raw"])
                elif kind == "ambiguous":
                    cit_rows.append({"ref_id": None, "ref_number": None,
                                     "marker_raw": cite["marker_raw"],
                                     "candidate_ref_ids": [r["id"] for r in res]})
                    kept_raw.append(cite["marker_raw"])
                    grp_amb.append({
                        "marker_raw": cite["marker_raw"], "surname": cite["surname"],
                        "year": cite["year"],
                        "candidates": [{"ref_number": r["ref_number"],
                                        "raw_entry": r["raw_entry"][:160]} for r in res]})
                else:
                    # Suppress forward orphans whose year is already resolved
                    # by a reverse-lookup citation elsewhere in the same
                    # sentence.  Compound surnames (e.g. "Tjong Kim Sang")
                    # produce both a forward orphan for the first word and a
                    # reverse-resolved match for the last word — they share
                    # the same year so we suppress the orphan.
                    if cite["year"] in sentence_resolved_years:
                        continue
                    # Same idea, for a mis-key the year test cannot see because the
                    # two readings of the mention disagree on the year too.
                    if _is_forward_miskey(cite, span_count, resolved_reverse_spans):
                        continue
                    cit_rows.append({"ref_id": None, "ref_number": None,
                                     "marker_raw": cite["marker_raw"], "candidate_ref_ids": []})
                    kept_raw.append(cite["marker_raw"])
                    grp_orph.append({"marker_raw": cite["marker_raw"],
                                     "surname": cite["surname"], "year": cite["year"]})
            # If every cite in this group was suppressed, skip the claim.
            if not matched_nums and not cit_rows:
                continue
            claim = parse_mod._new_claim(
                manuscript_id, sentences, idx, window,
                "; ".join(kept_raw),
                sorted(set(matched_nums)), len(cit_rows) > 1,
                sentence_text=frag if split else None,
                scope="sentence_fragment" if split else "sentence",
                marker_group_index=group_index,
                marker_group_count=len(groups))
            claims.append(claim)
            for row in cit_rows:
                row["claim_id"] = claim["id"]
                citations.append(row)
            for ambiguity in grp_amb:
                ambiguity["claim_id"] = claim["id"]
                ambiguities.append(ambiguity)
            for orphan in grp_orph:
                orphan["claim_id"] = claim["id"]
                orphans.append(orphan)
    # Silent misses: a reference the PDF hyperlinks but whose citation the text never
    # read (a shattered or year-only name).  Link-only and additive; a no-op off.
    citations.extend(_rescue_uncited_via_links(references, citations, by_sy, link_layer))
    table_only = parse_mod._table_only_citations(suppressed, citations, references)
    return claims, citations, rows, {"ambiguities": ambiguities, "orphans": orphans,
                                      "suppressed_markers": suppressed,
                                      "missegmented_recovered": recovered,
                                      "table_only_citations": table_only}
