#!/usr/bin/env python3
# core/parse/citation_schemes/inline_doi.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Inline DOI / arXiv citation scheme."""

from __future__ import annotations

import importlib
import re


NAME = "inline-doi"

_DOI_TOKEN = r"10\.\d{4,9}/[^\s;,)\]}]+"
_ARXIV_TOKEN = r"arXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?"
_INLINE_TOKEN_RE = re.compile(rf"(?:{_DOI_TOKEN})|(?:{_ARXIV_TOKEN})", re.IGNORECASE)
_PAREN_GROUP_RE = re.compile(r"\(([^()]*)\)")


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


def canonical_inline_doi(token: str) -> str | None:
    """Canonical DOI for an inline token (DOI as-is, arXiv id -> arXiv DOI)."""
    parse_mod = _resolve_parse_module()
    match = re.search(_DOI_TOKEN, token, re.IGNORECASE)
    if match:
        return match.group(0).rstrip(".,;)]}")
    return parse_mod._extract_arxiv_doi(token)


def find_inline_dois(sentence: str) -> list[dict]:
    """Inline DOI/arXiv citations in a sentence."""
    found = []
    for parenthetical in _PAREN_GROUP_RE.finditer(sentence):
        inner = parenthetical.group(1)
        for token_match in _INLINE_TOKEN_RE.finditer(inner):
            raw = token_match.group(0)
            canon = canonical_inline_doi(raw)
            if not canon:
                continue
            found.append({
                "marker_raw": "(" + inner.strip() + ")",
                "token": raw,
                "canonical": canon,
                "span": parenthetical.span(),
            })
    return found


def detect(body: str, sentences: list[str], references: list[dict]) -> float:
    del body, references
    return float(sum(len(find_inline_dois(sentence)) for sentence in sentences))


def synthesize_references(sentences, manuscript_id, start_num=0):
    """One reference per distinct inline DOI/arXiv id, in first-seen order."""
    parse_mod = _resolve_parse_module()
    authoryear_mod = _resolve_authoryear_module()
    seen: dict[str, dict] = {}
    refs: list[dict] = []
    num = start_num
    for sent in sentences:
        for cite in find_inline_dois(sent):
            canon = cite["canonical"]
            if canon in seen:
                continue
            num += 1
            ref = parse_mod._make_reference(num, cite["token"])
            ref["doi"] = canon
            ref["manuscript_id"] = manuscript_id
            sur, yr, suf = authoryear_mod.entry_key(ref["raw_entry"])
            ref["ay_surname"], ref["ay_year"], ref["ay_suffix"] = sur, yr, suf
            seen[canon] = ref
            refs.append(ref)
    return refs


def build(sentences, references, window, manuscript_id, fmt=None, link_layer=None,
          boilerplate_refs=None):
    # link_layer / boilerplate_refs are accepted for a uniform scheme interface;
    # inline-doi drafts carry their identifiers in-text and use neither.
    del link_layer, boilerplate_refs
    parse_mod = _resolve_parse_module()
    doi_to_ref = {r["doi"]: r for r in references if r.get("doi")}
    claims, citations, rows = [], [], []
    orphans = []
    for idx, sent in enumerate(sentences):
        cites = find_inline_dois(sent)
        rows.append((idx + 1, len(cites), parse_mod._display(sent)))
        if not cites:
            continue
        group_map: dict[tuple, list] = {}
        for cite in cites:
            group_map.setdefault(tuple(cite["span"]), []).append(cite)
        group_spans = sorted(group_map)
        groups = [group_map[k] for k in group_spans]
        frags, split = parse_mod._fragment_texts(sent, group_spans)
        for group_index, (group, frag) in enumerate(zip(groups, frags)):
            matched, cit_rows = [], []
            orphan_rows = []
            for cite in group:
                ref = doi_to_ref.get(cite["canonical"])
                if ref is None:
                    orphan_rows.append({"marker_raw": cite["marker_raw"],
                                        "canonical": cite["canonical"]})
                    continue
                matched.append(ref["ref_number"])
                cit_rows.append({"ref_id": ref["id"], "ref_number": ref["ref_number"]})
            claim = parse_mod._new_claim(
                manuscript_id, sentences, idx, window,
                "; ".join(dict.fromkeys(c["marker_raw"] for c in group)),
                sorted(set(matched)), len(cit_rows) > 1,
                sentence_text=frag if split else None,
                scope="sentence_fragment" if split else "sentence",
                marker_group_index=group_index,
                marker_group_count=len(groups))
            claims.append(claim)
            for orphan in orphan_rows:
                orphan["claim_id"] = claim["id"]
                citations.append(dict(orphan))
                orphans.append(orphan)
            for row in cit_rows:
                row["claim_id"] = claim["id"]
                citations.append(row)
    return claims, citations, rows, {"orphans": orphans}
