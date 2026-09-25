#!/usr/bin/env python3
# core/parse/citation_schemes/mla.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""MLA author-page citation scheme.

MLA names a source in text by its author and a page locator, and no year —
``(Morrison 42)``, ``(Viji and Girdhar 63)``, or narratively "as Morrison writes
(42)".  The sources are an alphabetical Works Cited list, and the in-text citation
carries nothing to match on but the author's surname: the year the entry prints is not
repeated in text.  So the join keys on surname alone.

Two works by one author are told apart in text by a short title —
``(Pun, "Neo-Slave Narratives" 18)`` — matched against the entry's own title.  A bare
``(Pun 18)`` against two Pun entries has nothing to choose by and is surfaced as
ambiguous, never guessed.

The whole scheme rests on the page/year distinction: a trailing number in [1500, 2099]
is a *year*, so ``(Smith 2020)`` is an author-year citation and is not read here — which
is what keeps an author-year manuscript from being mis-routed to this scheme.
"""

from __future__ import annotations

import importlib
import re

NAME = "mla"

# A page locator, never a year: MLA repeats no year in text, and a four-digit number in
# the publication-year range is exactly how an author-year citation ends.  Excluding it
# is what separates "(Morrison 42)" from "(Smith 2020)".
_PAGE_MAX = 1499


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


_NAME = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’\-]+"
# An author part: one surname, "X and Y", or "X et al." — the shapes MLA opens a
# citation with.  Only the first surname keys the citation (as everywhere in this
# project), the co-author narrows an ambiguous pair.
_AUTHORS = _NAME + r"(?:\s+(?:and|&)\s+" + _NAME + r"|\s+et\s+al\.?)?"
# Parenthetical: "(Author[, short title] Page[-Page])".
_PAREN_CITE = re.compile(
    r"\(\s*(?P<authors>" + _AUTHORS + r")"
    r"(?P<title>\s*,\s*[^)]{2,70}?)?"
    r"\s+(?P<page>\d{1,4})(?:\s*[\-–—]\s*\d{1,4})?\s*\)"
)
# Narrative: "Author('s) … (Page)" — the author sits just before a parenthesised page.
_NARR_CITE = re.compile(
    r"(?P<authors>" + _AUTHORS + r")(?:’s|'s)?\s+"
    r"\(\s*(?P<page>\d{1,4})(?:\s*[\-–—]\s*\d{1,4})?\s*\)"
)
_STOP_LEAD = frozenset({"and", "et", "al"})


def _first_surname(authors: str, ay) -> str:
    return ay._surname_key(re.split(r"\s+(?:and|&|et)\s+", authors.strip())[0])


def _coauthor(authors: str, ay) -> str | None:
    parts = re.split(r"\s+and\s+|\s+&\s+", authors.strip())
    return ay._surname_key(parts[1]) if len(parts) > 1 else None


def _index(references, ay) -> dict:
    idx: dict[str, list] = {}
    for r in references:
        sur = r.get("ay_surname")
        if sur:
            idx.setdefault(sur, []).append(r)
    return idx


def _find_cites(sentence: str, ay):
    """Every MLA author-page citation in a sentence, as dicts
    {marker_raw, surname, coauthor, short_title, page, span}."""
    text = ay.detection_text(sentence)
    out, spans = [], []
    for rx, kind in ((_PAREN_CITE, "paren"), (_NARR_CITE, "narr")):
        for m in rx.finditer(text):
            page = int(m.group("page"))
            if page > _PAGE_MAX or page == 0:
                continue
            if any(s < m.end() and m.start() < e for s, e in spans):
                continue
            sur = _first_surname(m.group("authors"), ay)
            if not sur or sur in _STOP_LEAD or sur in ay.stop_names():
                continue
            spans.append((m.start(), m.end()))
            out.append({
                "marker_raw": m.group(0).strip(),
                "surname": sur,
                "coauthor": _coauthor(m.group("authors"), ay),
                "short_title": (m.groupdict().get("title") or "").strip(" ,").lower(),
                "page": page,
                "span": (m.start(), m.end()),
            })
    out.sort(key=lambda c: c["span"][0])
    return out


def _match(cite, idx, ay):
    """('unique', ref) | ('ambiguous', [ref…]) | ('orphan', [])."""
    group = idx.get(cite["surname"], [])
    if not group:
        return "orphan", []
    if len(group) == 1:
        return "unique", group[0]
    # Two+ works by one author: a short title in the marker, matched against the entry;
    # else a co-author named in the marker, present in exactly one entry.
    if cite.get("short_title"):
        st = cite["short_title"]
        narrowed = [r for r in group
                    if st and st in ay._norm(r.get("raw_entry", "")).lower()]
        if len(narrowed) == 1:
            return "unique", narrowed[0]
    if cite.get("coauthor"):
        narrowed = [r for r in group
                    if ay._entry_has_surname(r.get("raw_entry", ""), cite["coauthor"])]
        if len(narrowed) == 1:
            return "unique", narrowed[0]
    return "ambiguous", group


def detect(body: str, sentences: list[str], references: list[dict]) -> float:
    """How many in-text author-page citations name a surname the Works Cited carries.

    Zero for numeric and author-year manuscripts: a numeric marker has no author, and an
    author-year citation ends in a year, which the page guard rejects."""
    del body
    ay = _resolve_authoryear_module()
    idx = _index(references, ay)
    if not idx:
        return 0.0
    n = 0
    for sent in sentences:
        for cite in _find_cites(sent, ay):
            if cite["surname"] in idx:
                n += 1
    return float(n)


def build(sentences, references, window, manuscript_id, fmt=None, link_layer=None,
          boilerplate_refs=None):
    del fmt, link_layer, boilerplate_refs
    parse_mod = _resolve_parse_module()
    ay = _resolve_authoryear_module()
    idx = _index(references, ay)
    claims, citations, rows = [], [], []
    ambiguities, orphans = [], []
    for i, sent in enumerate(sentences):
        cites = _find_cites(sent, ay)
        rows.append((i + 1, len(cites), parse_mod._display(sent)))
        if not cites:
            continue
        # Each citation scopes its own sentence fragment (mirrors the other schemes'
        # per-group claim), so distinct sources become distinct claims.
        spans = [c["span"] for c in cites]
        det = ay.detection_text(sent)
        frags, split = parse_mod._fragment_texts(det, spans)
        for group_index, (cite, frag) in enumerate(zip(cites, frags)):
            kind, res = _match(cite, idx, ay)
            if kind == "unique":
                row = {"ref_id": res["id"], "ref_number": res["ref_number"]}
                matched = [res["ref_number"]]
            elif kind == "ambiguous":
                row = {"ref_id": None, "ref_number": None,
                       "marker_raw": cite["marker_raw"],
                       "candidate_ref_ids": [r["id"] for r in res]}
                matched = []
                ambiguities.append({
                    "marker_raw": cite["marker_raw"], "surname": cite["surname"],
                    "candidates": [{"ref_number": r["ref_number"],
                                    "raw_entry": r["raw_entry"][:160]} for r in res]})
            else:
                row = {"ref_id": None, "ref_number": None,
                       "marker_raw": cite["marker_raw"], "candidate_ref_ids": []}
                matched = []
                orphans.append({"marker_raw": cite["marker_raw"],
                                "surname": cite["surname"], "year": None})
            claim = parse_mod._new_claim(
                manuscript_id, sentences, i, window, cite["marker_raw"], matched,
                False, sentence_text=frag if split else None,
                scope="sentence_fragment" if split else "sentence",
                marker_group_index=group_index,
                marker_group_count=len(cites))
            claims.append(claim)
            row["claim_id"] = claim["id"]
            citations.append(row)
            if kind == "ambiguous":
                ambiguities[-1]["claim_id"] = claim["id"]
            elif kind == "orphan":
                orphans[-1]["claim_id"] = claim["id"]
    return claims, citations, rows, {"ambiguities": ambiguities, "orphans": orphans,
                                     "suppressed_markers": [], "missegmented_recovered": []}
