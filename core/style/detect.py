#!/usr/bin/env python3
# core/style/detect.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
detect.py — Phase 0 helper: guess the citation STYLE deterministically.

The orchestrator should not assume a style. This script inspects the parsed
references (their formatting) plus the detected citation_mode and ranks the
available styles, returning a confidence. The PLAYBOOK uses it like this:

  - confidence == "high"   -> use the suggested style (still announce it);
  - confidence != "high"   -> ASK the user which of `available_styles` to force.

It NEVER silently picks a low-confidence style: an unverified guess on the style
axis would be a hidden degradation. The set of styles is read from
`core.style.check.STYLES`, so when new styles are added they appear here for free.

Signals are HEURISTIC (declared as such), aggregated across all reference entries:
  - citation_mode 'numeric'      -> strong prior for Vancouver
  - citation_mode 'author-year'  -> suppresses Vancouver, favours APA/Chicago/MLA
  - year in parentheses '(2020)' -> APA
  - 'Surname, A. B.' dotted init  -> APA
  - DOI as https://doi.org/ URL   -> APA
  - 'Surname AB' initials no dots -> Vancouver
  - 'Surname, Firstname' full     -> Chicago / MLA
  - article title in quotes       -> Chicago / MLA
  - 'vol. N' / 'no. N' labels     -> MLA
  - 'Accessed <date>'             -> MLA

Usage:
  python run.py style-detect --run runs/<ts>
"""
import argparse
import json
import re

try:
    from core.infra.db import RunRepository
    from core.style import common as c
    from core.style.check import STYLES
except ImportError:  # direct execution
    from db import RunRepository
    from . import common as c
    from .check import STYLES

# Distinctive markers not already in common.py.
YEAR_PARENS = re.compile(r"\(\s*(1[5-9]\d{2}|20\d{2})[a-z]?\s*\)")
FULL_FIRSTNAME = re.compile(r"[A-Z][a-zà-ÿ]+,\s+[A-Z][a-zà-ÿ]{2,}")  # "Surname, Firstname"
TITLE_QUOTES = re.compile(r'["“”]')
VOL_LABEL = re.compile(r"\bvol\.\s*\d+", re.IGNORECASE)
NO_LABEL = re.compile(r"\bno\.\s*\d+", re.IGNORECASE)
VOL_ISSUE_NUM = re.compile(r"\b\d+\s*\(\s*\d+\s*\)")  # "12(3)"
# IEEE: initial-dot + surname pattern
IEEE_AUTHOR_DETECT = re.compile(r"^(?:\[\d+\]\s*)?(?:[A-Z]\.\s+)+[A-Z][a-z]+")
IEEE_VOL_NO_PP = re.compile(r"vol\.\s*\d+.*no\.\s*\d+.*pp?\.\s*\d+", re.IGNORECASE)


def _load_parse_payload(run: str) -> dict:
    repo = RunRepository.open_readonly(run)
    try:
        return repo.parse_payload()
    finally:
        repo.close()


def _entry_scores(entry: str) -> dict[str, float]:
    """Per-entry weighted score for each style. Weights are relative, not calibrated."""
    s = {k: 0.0 for k in STYLES}

    has_year_parens = bool(YEAR_PARENS.search(entry))
    has_dotted = bool(c.INITIALS_DOTTED.search(entry))
    has_doi_url = bool(c.DOI_URL.search(entry))
    has_vanc_initials = bool(c.INITIALS_VANC.search(entry)) and not has_dotted
    has_full_first = bool(FULL_FIRSTNAME.search(entry))
    has_quotes = bool(TITLE_QUOTES.search(entry))
    has_vol_label = bool(VOL_LABEL.search(entry))
    has_no_label = bool(NO_LABEL.search(entry))
    has_vol_issue_num = bool(VOL_ISSUE_NUM.search(entry))
    has_accessed = bool(c.ACCESSED.search(entry))

    if "apa7" in s:
        if has_year_parens:
            s["apa7"] += 3.0
        if has_dotted:
            s["apa7"] += 1.5
        if has_doi_url:
            s["apa7"] += 1.0
        if has_vol_issue_num:
            s["apa7"] += 0.5
    if "vancouver" in s:
        if has_vanc_initials:
            s["vancouver"] += 2.5
        if not has_year_parens and c.has_year(entry):
            s["vancouver"] += 0.5
        if has_vol_issue_num:
            s["vancouver"] += 0.5
    if "chicago" in s:
        if has_full_first and not has_year_parens:
            s["chicago"] += 2.0
        if has_quotes:
            s["chicago"] += 1.0
        if has_vol_issue_num:
            s["chicago"] += 0.5
    if "mla9" in s:
        # MLA uses both "vol. N" and "no. N"; keep them equivalent here so the
        # split that introduced has_no_label (for chicago_nb) does not silently
        # drop MLA's "no."-only entries.
        if has_vol_label or has_no_label:
            s["mla9"] += 3.0
        if has_full_first:
            s["mla9"] += 1.0
        if has_quotes:
            s["mla9"] += 0.5
        if has_accessed:
            s["mla9"] += 0.5
    if "chicago_nb" in s:
        # Chicago NB: full author names + titles in quotes are strongest signals
        if has_full_first:
            s["chicago_nb"] += 2.5
        if has_quotes:
            s["chicago_nb"] += 1.5
        if has_vol_label or has_no_label:
            # "no. N" pattern common in NB bibliographies
            s["chicago_nb"] += 1.0
        if has_vol_issue_num:
            s["chicago_nb"] += 0.5
    if "ama" in s:
        # AMA shares the Vancouver author format; the strongest AMA signal is
        # the exclusive use of "doi:10." prefix (never doi.org URLs).
        has_doi_prefix = bool(re.search(r"\bdoi:\s*10\.", entry, re.I))
        if has_doi_prefix:
            s["ama"] += 2.0
        if has_vanc_initials and has_vol_issue_num:
            s["ama"] += 0.5
        if not has_year_parens and c.has_year(entry):
            s["ama"] += 0.5
    if "ieee" in s:
        has_ieee_author = bool(IEEE_AUTHOR_DETECT.search(entry))
        has_ieee_vol_no_pp = bool(IEEE_VOL_NO_PP.search(entry))
        has_doi_prefix = bool(re.search(r"\bdoi:\s*10\.", entry, re.I))
        if has_ieee_author:
            s["ieee"] += 2.5
        if has_ieee_vol_no_pp:
            s["ieee"] += 2.0
        if has_quotes and has_vol_label:
            s["ieee"] += 1.0
        if has_doi_prefix:
            s["ieee"] += 0.5
    return s


def detect(references: list[dict], citation_mode: str | None) -> dict:
    available = sorted(STYLES)
    if not references:
        return {
            "suggested": None, "confidence": "low", "decision": "ask",
            "reason": "no references to analyse", "scores": {k: 0.0 for k in available},
            "available_styles": available, "citation_mode": citation_mode,
        }

    totals = {k: 0.0 for k in STYLES}
    for r in references:
        entry = r.get("raw_entry", "") or ""
        for k, v in _entry_scores(entry).items():
            totals[k] += v
    n = len(references)
    scores = {k: round(totals[k] / n, 3) for k in totals}

    # citation_mode gate: numeric is incompatible with author-year styles and vice
    # versa. Apply it AFTER scoring so the raw signals stay inspectable.
    if citation_mode == "numeric":
        for k in ("apa7", "chicago", "mla9"):
            if k in scores:
                scores[k] = round(scores[k] * 0.25, 3)
        for k in ("vancouver", "ama", "ieee"):
            if k in scores:
                scores[k] = round(scores[k] + 1.0, 3)
        # Chicago NB uses superscript numeric citations — it should NOT be
        # penalised in numeric mode; boost it when there is supporting evidence.
        if "chicago_nb" in scores and scores["chicago_nb"] >= 1.0:
            scores["chicago_nb"] = round(scores["chicago_nb"] + 1.5, 3)
    elif citation_mode == "author-year":
        for k in ("vancouver", "ama", "ieee"):
            if k in scores:
                scores[k] = round(scores[k] * 0.25, 3)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = round(best_score - second_score, 3)

    # Confidence: needs both an absolute floor and a clear margin over the runner-up.
    if best_score >= 1.5 and margin >= 0.6:
        confidence = "high"
    elif best_score >= 0.8 and margin >= 0.3:
        confidence = "medium"
    else:
        confidence = "low"

    decision = "use" if confidence == "high" else "ask"
    return {
        "suggested": best if best_score > 0 else None,
        "confidence": confidence,
        "decision": decision,
        "margin": margin,
        "scores": scores,
        "ranked": [k for k, _ in ranked],
        "available_styles": available,
        "citation_mode": citation_mode,
        "reason": ("clear winner" if confidence == "high"
                   else "ambiguous or weak signal: ask the user to confirm/force a style"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run directory (DB-backed)")
    args = ap.parse_args()

    data = _load_parse_payload(args.run)
    refs = data.get("references", [])
    mode = (data.get("manuscript") or {}).get("citation_mode")
    res = detect(refs, mode)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # Exit 0 always: a low-confidence guess is a normal outcome (ask the user),
    # not an error. The decision field drives the orchestrator.


if __name__ == "__main__":
    main()
