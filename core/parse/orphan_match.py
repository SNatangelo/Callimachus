# core/parse/orphan_match.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Assisted manual resolution of orphan citations.

An orphan is a citation with no matching bibliography entry.  Most are genuine
defects (a fabricated source, or a mistyped/garbled author name), so the parser
never resolves them automatically — that would risk masking a fabrication.  But
when the intended reference is obviously present under a slightly different name
("Winkleman" cited, "Winkelman" in the list), a human can say so.

This module ranks the references that most resemble each orphan and drives an
optional interactive prompt.  It also asks the inverse: a reference nobody cites,
against the claims whose prose carries a near-miss of its author — a citation the
detector never read, so no orphan was ever raised for it.  The chosen association is
recorded with provenance ``user_selected`` so the report can flag it as a human
decision, distinct from an automatic match.  Nothing here resolves anything on its
own.
"""

from __future__ import annotations

import difflib
import re
import sys


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def rank_candidates(surname, year, references, top_k: int = 5, min_score: float = 0.5):
    """References most similar to an orphan (surname, year), best first.

    Score blends surname similarity (0.7) with a year term (0.3): same year is
    strongest, ±1 year partial, otherwise weak.  Returns ``[(score, ref), …]``
    filtered to ``score >= min_score`` and truncated to ``top_k``."""
    scored = []
    for r in references:
        surname_sim = _ratio(surname, r.get("ay_surname") or "")
        ry = r.get("ay_year")
        if ry == year:
            year_score = 1.0
        elif (isinstance(ry, int) and isinstance(year, int)
              and abs(ry - year) <= 1):   # a no-year sentinel is a string, not an int
            year_score = 0.6
        else:
            year_score = 0.2
        scored.append((round(0.7 * surname_sim + 0.3 * year_score, 3), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(s, r) for s, r in scored if s >= min_score][:top_k]


def apply_choice(result: dict, orphan: dict, ref: dict) -> None:
    """Bind an orphan's citation(s) to the user-chosen reference.

    Mutates *result*: sets the citation rows' ref_id/ref_number, tags them
    ``provenance="user_selected"``, drops the orphan from the debug orphan list,
    and records the decision under ``_debug.user_resolved`` for the report."""
    for c in result.get("citations", []):
        if (c.get("claim_id") == orphan.get("claim_id")
                and c.get("marker_raw") == orphan.get("marker_raw")
                and not c.get("ref_number")):
            c["ref_id"] = ref.get("id")
            c["ref_number"] = ref.get("ref_number")
            c["provenance"] = "user_selected"
    d = result.setdefault("_debug", {})
    d["orphans"] = [o for o in (d.get("orphans") or []) if o is not orphan]
    d.setdefault("user_resolved", []).append({
        "marker_raw": orphan.get("marker_raw"),
        "surname": orphan.get("surname"), "year": orphan.get("year"),
        "ref_number": ref.get("ref_number"), "ref_id": ref.get("id"),
        "raw_entry": (ref.get("raw_entry") or "")[:160],
    })


def doubtful_orphans(result: dict, top_k: int = 5, min_score: float = 0.5):
    """Orphans that have at least one plausible candidate, paired with them."""
    refs = result.get("references") or []
    out = []
    for o in (result.get("_debug") or {}).get("orphans") or []:
        cands = rank_candidates(o.get("surname"), o.get("year"), refs,
                                top_k=top_k, min_score=min_score)
        if cands:
            out.append((o, cands))
    return out


def _uncited_refs(result: dict) -> list:
    """The reference dicts a coverage pass marked as cited by nobody."""
    by_num = {r.get("ref_number"): r for r in (result.get("references") or [])}
    cov = (result.get("_debug") or {}).get("reference_coverage") or {}
    out = []
    for u in cov.get("uncited") or []:
        r = u if isinstance(u, dict) else by_num.get(u)
        if r is not None:
            out.append(r)
    return out


_CLAIM_WORD_RE = re.compile(r"[A-Z][A-Za-zÀ-ÿ'\-]{2,}")


def rank_claims_for_uncited(ref: dict, claims, top_k: int = 3, min_score: float = 0.72):
    """Claims whose prose carries a name resembling an uncited reference's surname —
    the inverse of :func:`rank_candidates`.

    The forward direction starts from an orphan citation and looks for its reference.
    But a reference can go uncited with no orphan raised at all: its citation was a
    silent miss, a name the detector never read as a citation.  Starting from the
    *reference* and looking for a near-miss of its author in the prose is the only way
    to surface that — and, like everything here, it is offered to a human, never
    applied on its own."""
    sur = (ref.get("ay_surname") or "").lower()
    if len(sur) < 3:
        return []
    scored = []
    for cl in claims or []:
        best = max((_ratio(sur, w)
                    for w in _CLAIM_WORD_RE.findall(cl.get("sentence") or "")),
                   default=0.0)
        if best >= min_score:
            scored.append((round(best, 3), cl))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:top_k]


def uncited_with_candidates(result: dict, top_k: int = 3, min_score: float = 0.72):
    """Uncited references paired with the claims that may in fact cite them."""
    claims = result.get("claims") or []
    out = []
    for ref in _uncited_refs(result):
        cands = rank_claims_for_uncited(ref, claims, top_k=top_k, min_score=min_score)
        if cands:
            out.append((ref, cands))
    return out


def apply_claim_choice(result: dict, claim: dict, ref: dict) -> None:
    """Bind an uncited reference to the claim a human says cites it — the inverse of
    :func:`apply_choice`.  Adds its citation, takes it off the uncited list, and
    records the decision as ``user_selected`` for the report."""
    result.setdefault("citations", []).append({
        "claim_id": claim.get("id"), "ref_id": ref.get("id"),
        "ref_number": ref.get("ref_number"), "provenance": "user_selected",
    })
    d = result.setdefault("_debug", {})
    cov = d.get("reference_coverage") or {}
    unc = cov.get("uncited")
    if isinstance(unc, list) and ref.get("ref_number") in unc:
        unc.remove(ref.get("ref_number"))
        cov["cited"] = (cov.get("cited") or 0) + 1
        if cov.get("total"):
            cov["pct"] = round(100 * cov["cited"] / cov["total"], 1)
    d.setdefault("user_resolved", []).append({
        "ref_number": ref.get("ref_number"), "claim_id": claim.get("id"),
        "surname": ref.get("ay_surname"), "year": ref.get("ay_year"), "via": "uncited",
    })


def interactive_resolve(result: dict, *, ask=input, out=sys.stderr,
                        top_k: int = 5, min_score: float = 0.5) -> dict:
    """Offer manual resolution, in both directions.  ``ask`` is injectable for
    testing.  Returns *result* (mutated in place when the user selects).

    First the forward query — an orphan citation offered against the references it
    resembles.  Then, for whatever the forward query left unresolved, the inverse: an
    uncited reference offered against the claims whose prose carries a near-miss of its
    author (a citation the detector never read, so no orphan was ever raised).  Both
    are optional and nothing is applied without the user picking it."""
    _yes = ("y", "yes", "s", "si", "sì")
    doubtful = doubtful_orphans(result, top_k=top_k, min_score=min_score)
    if doubtful:
        print(f"\n{len(doubtful)} orphan citation(s) resemble a listed reference.",
              file=out)
        if (ask("Select matches manually? [y/N] ") or "").strip().lower() in _yes:
            for orphan, cands in doubtful:
                print(f"\nOrphan  {orphan.get('marker_raw')}  "
                      f"({orphan.get('surname')}, {orphan.get('year')})", file=out)
                for i, (score, ref) in enumerate(cands, 1):
                    print(f"  [{i}] {score:.2f}  {ref.get('ay_surname')} "
                          f"{ref.get('ay_year')}  — {(ref.get('raw_entry') or '')[:70]}",
                          file=out)
                answer = (ask("Pick a number (Enter to leave as orphan): ") or "").strip()
                if answer.isdigit() and 0 <= int(answer) - 1 < len(cands):
                    apply_choice(result, orphan, cands[int(answer) - 1][1])
                    print("  → recorded as a user-selected association.", file=out)

    # The inverse query, presented last and only for what the forward pass did not
    # resolve: a reference nobody cites, offered against the claims that may in fact
    # cite it under a name the detector never read.
    doubtful_unc = uncited_with_candidates(result, top_k=top_k)
    if doubtful_unc:
        print(f"\n{len(doubtful_unc)} uncited reference(s) resemble a name in the text.",
              file=out)
        if (ask("Review these too? [y/N] ") or "").strip().lower() in _yes:
            for ref, cands in doubtful_unc:
                print(f"\nUncited  [{ref.get('ref_number')}]  "
                      f"{ref.get('ay_surname')} {ref.get('ay_year')}"
                      f"  — {(ref.get('raw_entry') or '')[:70]}", file=out)
                for i, (score, cl) in enumerate(cands, 1):
                    print(f"  [{i}] {score:.2f}  {(cl.get('sentence') or '')[:78]}",
                          file=out)
                answer = (ask("Pick a number (Enter to leave uncited): ") or "").strip()
                if answer.isdigit() and 0 <= int(answer) - 1 < len(cands):
                    apply_claim_choice(result, cands[int(answer) - 1][1], ref)
                    print("  → recorded as a user-selected citation.", file=out)
    return result
