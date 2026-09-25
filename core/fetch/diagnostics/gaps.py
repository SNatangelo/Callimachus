#!/usr/bin/env python3
# core/fetch/diagnostics/gaps.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
gaps.py — Phase 2.5: the missing-text gap list, deterministic.

After resolve+ingest, reports for each reference the highest reliability tier
reachable and what is needed to go higher. Availability is read from the stored
source provenance, not inferred from file names: provenance (who provided what)
is a recorded fact, not an inference.

Workflow (see PLAYBOOK Phase 2.5):
  1. verify EVERYTHING that already has a full text registered;
  2. gaps.py lists what is missing -> authenticated FETCH task answers provide texts;
  3. what remains -> downgrade to the allowed tier (abstract).

Usage:
  python run.py gaps --run runs/<ts> --accuracy standard
"""
import argparse
import json
import os

try:
    from core.resolve import sources
    from core.infra.db import RunRepository
    from core.parse.footnotes import cross_reference_map
except ImportError:
    import sources
    from db import RunRepository
    from footnotes import cross_reference_map

TIER_ORDER = ["fulltext", "abstract"]

# Phase 0 accuracy grade -> (floor tier, chase_fulltext, provisional_on_unknown).
#   maximum          : full text only, never settle for the abstract (a missing full
#                      text leaves the source UNVERIFIED, never silently downgraded).
#   maximum_fallback : like maximum (full text only when it is KNOWN to exist), BUT when
#                      we cannot even tell whether a fuller text exists
#                      (fulltext_exists == "unknown"), the abstract is accepted as a
#                      PROVISIONAL basis — flagged, never green — while the gap stays
#                      open so the user can still provide the full text. If a full text
#                      is known to exist (fulltext_exists == True) the strict rule holds.
#   standard         : full text preferred and still chased; abstract is a fallback whose
#                      negative is inconclusive when a full text exists (default).
#   abstract         : abstract+title is acceptable as the final tier; the full text is
#                      NOT chased (faster; no nagging for PDFs).
#   standard_web     : retained as an alias of standard; generic third-party web pages
#                      are not source-attributed evidence and cannot enter Verify.
ACCURACY = {
    "maximum":          ("fulltext", True, False),
    "maximum_fallback": ("fulltext", True, True),
    "standard":         ("abstract", True, False),
    "abstract":         ("abstract", False, False),
    "standard_web":     ("abstract", True, False),
}

# Existence statuses that are RED FINDINGS (possible fabrication). Strict:
# only a unique identifier that is absent or that points to a different work.
FAB_STATUSES = ("not_found", "identifier_mismatch")


def _repo_open(run_dir):
    try:
        return RunRepository.open(run_dir)
    except Exception:
        return None


def _risk_band(ratio: float) -> str:
    if ratio >= 0.50:
        return "severe"
    if ratio >= 0.25:
        return "high"
    if ratio >= 0.10:
        return "moderate"
    return "low"


def _abstract_status(have: dict, ex: dict) -> tuple[str, str | None]:
    stored = have.get("abstract") or {}
    if stored:
        origin = stored.get("origin")
        return "available", f"{origin} stored abstract" if origin else "stored abstract"
    if ex.get("abstract"):
        via = ex.get("via") or "metadata"
        suffix = "catalog" if via in ("openlibrary", "googlebooks") else "metadata"
        return "available", f"{via} {suffix}"
    return "absent", None


def build_gap_report(run_dir: str, *, accuracy: str) -> dict:
    min_tier, chase_fulltext, provisional_on_unknown = ACCURACY[accuracy]

    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        parse = repo.effective_parse_payload()
        manual_parse_adjudication = repo.manual_parse_review_projection()
        resolve_map = repo.resolve_payload_map()
        man = repo.source_manifest_payload()
        unreadable_payload = repo.unreadable_payload()
        fetch_attempts_by_ref = {
            ref["id"]: repo.list_fetch_attempts(ref["id"])
            for ref in parse["references"]
            if resolve_map.get(ref["id"], {}).get("abstract")
        }
    finally:
        repo.close()
    tiers_by_ref = {}
    for e in man.get("entries", []):
        tiers_by_ref.setdefault(e["ref_id"], {})[e["tier"]] = e

    # A back-reference note ("Id. at 96.", "X, supra note 10, at 331.") never
    # stores a source of its own: resolve inherits the antecedent's result but
    # not its storage, and verify reads the antecedent's stored text instead
    # (see _verify_pairs in core/app/phases/verify.py).  Read its tiers from the
    # antecedent here too, so it is reported as already covered rather than as a
    # gap.  Otherwise the fetch phase re-downloads and re-extracts the
    # antecedent's document once per citing note — 198 times over on a law
    # review whose apparatus is half back-references.
    # Merge only tiers absent from the child, mirroring verify's per-tier
    # fallback. Child provenance always wins; an antecedent with no text leaves
    # the note a genuine gap.
    for rid, (antecedent_id, _pinpoint) in cross_reference_map(parse["references"]).items():
        antecedent_tiers = tiers_by_ref.get(antecedent_id)
        if antecedent_tiers:
            child_tiers = tiers_by_ref.setdefault(rid, {})
            for tier, entry in antecedent_tiers.items():
                child_tiers.setdefault(tier, entry)

    floor = TIER_ORDER.index(min_tier)
    gaps = []
    ready = []
    unverified = []   # existence unconfirmed: report suggests "search online?" (opt-in)
    n_unresolved = 0  # network/HTTP failures (transient): drives the network_blocked signal
    for ref in parse["references"]:
        rid = ref["id"]
        ex = resolve_map.get(rid, {})
        abstract_invalidated = sources.weak_metadata_abstract_invalidated(
            ref, ex, fetch_attempts_by_ref.get(rid, [])
        )
        status = ex.get("status", "n/d")
        have = tiers_by_ref.get(rid, {})
        if abstract_invalidated:
            abstract_via = ex.get("abstract_via") or ex.get("via") or ""
            abstract_source_ref = ex.get("url") or f"resolve:{abstract_via}"
            have = {
                tier: entry
                for tier, entry in have.items()
                if not (
                    tier == "abstract"
                    and entry.get("source_ref") == abstract_source_ref
                )
            }
            ex = {key: value for key, value in ex.items() if key != "abstract"}
        full_entry = have.get("fulltext")
        has_full = full_entry is not None
        full_is_preprint = (
            bool(full_entry)
            and full_entry.get("content_version") in sources.NON_RECORD_VERSIONS
        )
        require_record = (min_tier == "fulltext")
        has_abs = "abstract" in have or bool(ex.get("abstract"))
        has_web = "web" in have
        origin = (have.get("fulltext") or have.get("abstract") or have.get("web") or {}).get("origin")
        abstract_status, abstract_source = _abstract_status(have, ex)
        fulltext_exists = ex.get("fulltext_exists", "unknown")

        if status == "unresolved":
            n_unresolved += 1

        if status in ("unverified", "unresolved"):
            unverified.append({
                "ref_id": rid, "ref_number": ref["ref_number"],
                "existence_status": status, "existence_reason": ex.get("reason"),
                "reference_status_tag": ex.get("reference_status_tag", "unverified"),
                "fabrication_risk": ex.get("fabrication_risk", "unknown"),
                "text_identity_status": (have.get("fulltext") or have.get("abstract")
                                         or have.get("web") or {}).get("identity_status"),
                "suggested_action": "online search (opt-in) to confirm existence",
            })

        if has_full and not (full_is_preprint and require_record):
            item = {"ref_id": rid, "ref_number": ref["ref_number"],
                    "tier": "fulltext", "origin": full_entry["origin"]}
            if full_is_preprint:
                item["content_version"] = full_entry.get("content_version")
                if full_entry.get("provenance_relation"):
                    item["provenance_relation"] = full_entry.get("provenance_relation")
                item["provisional"] = True
                if full_entry.get("provenance_relation") == sources.OFFICIALLY_SURFACED_COPY:
                    item["note"] = (
                        f"full text is a {full_entry.get('content_version')} surfaced as an "
                        "official citation-path copy; it is not the formal version of record, "
                        "so it is accepted provisionally under the accuracy grade, never green"
                    )
                else:
                    item["note"] = (f"full text is a {full_entry.get('content_version')} "
                                    "(not the version of record): accepted provisionally "
                                    "under the accuracy grade, never green")
            ready.append(item)
            continue

        if has_full and full_is_preprint:
            gaps.append({
                "ref_id": rid,
                "ref_number": ref["ref_number"],
                "existence_status": status,
                "existence_reason": ex.get("reason"),
                "has_abstract": has_abs,
                "has_web": has_web,
                "has_preprint_fulltext": True,
                "content_version": full_entry.get("content_version"),
                "provenance_relation": full_entry.get("provenance_relation"),
                "fulltext_exists": fulltext_exists,
                "oa_status": ex.get("oa_status", "unknown"),
                "available_origin": full_entry["origin"],
                "suggested_tier": "fulltext",
                "needs": (
                    "published full text (version of record) needed; only a "
                    f"{full_entry.get('content_version')} is available - verified "
                    "PROVISIONALLY, never green"
                ) if full_entry.get("provenance_relation") != sources.OFFICIALLY_SURFACED_COPY else (
                    "published full text (version of record) needed; only a "
                    f"{full_entry.get('content_version')} surfaced as an official "
                    "citation-path copy is available - verified PROVISIONALLY, never green"
                ),
                "provisional": True,
                "book_availability": ex.get("book_availability"),
                "availability_note": ex.get("availability_note"),
                "fabrication_warning": status in FAB_STATUSES,
                "reference_status_tag": ex.get("reference_status_tag"),
                "fabrication_risk": ex.get("fabrication_risk"),
                "evidence_profile": ex.get("evidence_profile"),
            })
            continue

        if fulltext_exists is False and has_abs and TIER_ORDER.index("abstract") <= floor:
            ready.append({"ref_id": rid, "ref_number": ref["ref_number"],
                          "tier": "abstract", "origin": origin, "ceiling": "abstract",
                          "note": "full text does not exist (e.g. conference abstract): "
                                  "abstract is the ceiling, nothing to provide"})
            continue

        reachable = None
        if has_abs and TIER_ORDER.index("abstract") <= floor:
            reachable = "abstract"
        provisional = False
        if (reachable is None and provisional_on_unknown
                and fulltext_exists == "unknown" and has_abs):
            reachable = "abstract"
            provisional = True

        if reachable and not chase_fulltext:
            ready.append({"ref_id": rid, "ref_number": ref["ref_number"],
                          "tier": reachable,
                          "origin": origin, "ceiling": "accuracy",
                          "note": f"accuracy grade '{accuracy}' accepts {reachable}; "
                                  "full text not chased"})
            continue

        if reachable:
            suggested = reachable
            if provisional:
                need = ("abstract verified PROVISIONALLY (full-text existence unknown); "
                        "provide the full text to make the verdict conclusive")
            elif fulltext_exists is True:
                need = ("full text needed to reach high reliability"
                        + (" (paywalled: standard may verify on the abstract if needed, "
                           "but still keeps trying to obtain the full text)"
                           if ex.get("oa_status") == "paywalled" else ""))
            else:
                need = "full text needed to reach high reliability"
        else:
            if TIER_ORDER.index("abstract") <= floor:
                suggested = "abstract"
                need = "full text or at least the abstract"
            else:
                suggested = None
                need = "full text (no downgrade authorised)"

        gaps.append({
            "ref_id": rid,
            "ref_number": ref["ref_number"],
            "existence_status": status,
            "existence_reason": ex.get("reason"),
            "has_abstract": has_abs,
            "abstract_status": abstract_status,
            "abstract_source": abstract_source,
            "has_web": has_web,
            "fulltext_exists": fulltext_exists,
            "oa_status": ex.get("oa_status", "unknown"),
            "available_origin": origin,
            "suggested_tier": suggested,
            "needs": need,
            "provisional": provisional,
            "book_availability": ex.get("book_availability"),
            "availability_note": ex.get("availability_note"),
            "fabrication_warning": status in FAB_STATUSES,
            "reference_status_tag": ex.get("reference_status_tag"),
            "fabrication_risk": ex.get("fabrication_risk"),
            "evidence_profile": ex.get("evidence_profile"),
        })

    unreadable = [e for e in unreadable_payload.get("entries", [])
                  if e.get("ocr_status") != "done"]
    any_usable = bool(ready) or any(
        g["has_abstract"] or g["has_web"] or g.get("has_preprint_fulltext") for g in gaps
    )
    network_blocked = (not any_usable) and n_unresolved > 0
    suspected = [
        r for r in resolve_map.values()
        if r.get("reference_status_tag") == "suspected_fabricated"
    ]
    low_index = [
        r for r in resolve_map.values()
        if r.get("reference_status_tag") == "unverified_low_indexability"
    ]
    high_risk_weak_metadata = [
        r for r in resolve_map.values()
        if r.get("reference_status_tag") == "high_risk_weak_metadata"
    ]
    externally_corroborated = sorted({
        e.get("ref_number") for e in man.get("entries", [])
        if e.get("identity_status")
        and resolve_map.get(e.get("ref_id"), {}).get("status") != "resolved"
    })
    identifier_errors = [
        r for r in resolve_map.values()
        if (
            str(r.get("reference_status_tag", "")).startswith("identifier_error")
            or r.get("reference_status_tag") == "verified_with_identifier_error"
        )
    ]
    suspected_ratio = (len(suspected) / len(parse["references"])) if parse["references"] else 0.0

    out = {
        "ready_fulltext": ready,
        "gaps": gaps,
        "unverified": unverified,
        "unreadable_pdfs": unreadable,
        "network_blocked": network_blocked,
        "manual_parse_adjudication": manual_parse_adjudication,
        "summary": {
            "references": len(parse["references"]),
            "ready_fulltext": len(ready),
            "gaps": len(gaps),
            "unverified": len(unverified),
            "unresolved_network": n_unresolved,
            "manual_no_sources": sum(1 for row in manual_parse_adjudication
                                     if row["subject_type"] == "footnote_note" and row["status"] == "no_sources"),
            "manual_ambiguous": sum(1 for row in manual_parse_adjudication
                                    if row["subject_type"] == "footnote_note" and row["status"] == "ambiguous"),
            "manual_split_sources": sum(1 for row in manual_parse_adjudication
                                        if row.get("action") == "split_sources"),
            "manual_identity_overrides": sum(1 for row in manual_parse_adjudication
                if row["subject_type"] == "reference_identity"
                and row.get("action") == "correct_identity"),
            "unreadable_pdfs": len(unreadable),
            "gaps_with_abstract_available": sum(
                1 for g in gaps if g.get("abstract_status") == "available"
            ),
            "gaps_with_abstract_absent": sum(
                1 for g in gaps if g.get("abstract_status") == "absent"
            ),
            "ready_paywalled_abstract_fallbacks": (
                sum(1 for r in ready if r.get("ceiling") == "paywalled_abstract_fallback")
                + sum(1 for g in gaps
                      if g.get("oa_status") == "paywalled"
                      and g.get("abstract_status") == "available")
            ),
            "fabrication_warnings": sum(1 for g in gaps if g["fabrication_warning"]),
            "suspected_fabricated": len(suspected),
            "suspected_fabricated_ratio": round(suspected_ratio, 4),
            "fabrication_risk_band": _risk_band(suspected_ratio),
            "unverified_low_indexability": len(low_index),
            "high_risk_weak_metadata": len(high_risk_weak_metadata),
            "externally_corroborated_outside_resolve": len(externally_corroborated),
            "externally_corroborated_outside_resolve_numbers": externally_corroborated,
            "identifier_errors": len(identifier_errors),
            "provisional_gaps": sum(1 for g in gaps if g.get("provisional")),
            "preprint_only_fulltext": (
                sum(1 for r in ready if r.get("content_version") in sources.NON_RECORD_VERSIONS)
                + sum(1 for g in gaps if g.get("has_preprint_fulltext"))
            ),
            "accuracy": accuracy,
            "min_tier": min_tier,
            "chase_fulltext": chase_fulltext,
            "provisional_on_unknown": provisional_on_unknown,
            "network_blocked": network_blocked,
        },
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument(
        "--accuracy", required=True, choices=sorted(ACCURACY),
        help="Phase 0 accuracy grade; sets the floor tier and full-text policy.",
    )
    args = ap.parse_args()

    out = build_gap_report(args.run, accuracy=args.accuracy)
    print(json.dumps(out["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
