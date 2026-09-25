#!/usr/bin/env python3
# core/resolve/provide.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
provide.py — deterministic source supply and mapping helpers.

The public CLI keeps the read-only ``path`` lookup. Direct mutating commands
are rejected: external sources enter integrity-protected runs through an
authenticated FETCH task answer and are copied into the controlled inbox.

Sub-commands:
  ingest  --run R --dir FOLDER
          Bulk-maps user-supplied files to references. AUTO-matching only when
          strongly corroborated (DOI/PMID, or almost all entry tokens). Ambiguous
          files are NOT guessed: their review payload is stored by the script
          (DB-backed run setting when available)
          for confirmation via 'map'.

  map     --run R --ref REF_ID --file PATH [--tier fulltext]
          [--origin user] [--force]
          Confirms a single file<->reference mapping (e.g. after a model proposal).
          Without --force, requires corroboration.

  record  --run R --ref REF_ID --origin webfetch|websearch|crossref|...
          --tier fulltext|abstract|web [--url U] [--text-file F | stdin]
          Records a text fetched from the web (full text/abstract/search). Same
          destination and same labels.

  path    --run R --ref REF_ID [--tier fulltext]
          Prints the path of the registered .txt (to pass to preprocess/guard).
"""
import argparse
import glob
import json
import os
import sys

from core.invocation import run_command

try:
    from core.fetch.storage import content_store
    from core.fetch.extraction import fetch_html
    from core.parse import extract as extract_mod
    from . import sources
    from . import user_sources
    from core.infra.db import RunRepository
except ImportError:
    import content_store
    import fetch_html
    import extract as extract_mod
    from resolve import sources
    from resolve import user_sources
    from db import RunRepository


# An HTML file's authoritative identity (DOI/PMID/title/year) usually lives in
# <meta citation_*> tags / JSON-LD, not necessarily in the canonical body text —
# and a paywalled landing page can look enough like a short article to otherwise
# slip through. Both concerns are HTML-specific; every other supported format
# keeps the original token/DOI-in-body corroboration path untouched.
_HTML_EXTS = {".html", ".htm", ".xhtml"}


def _identity_for_origin(origin: str, *, traceable_url: bool, forced: bool) -> tuple[str | None, str | None]:
    if origin == "browser_session":
        return (
            "browser_session_cleared",
            ("retrieved in a visible browser session after the user cleared a "
             "publisher security challenge; the script recorded the resulting text"),
        )
    if forced:
        return (
            "externally_corroborated_text",
            ("mapped from a traceable supplied file and usable for claim "
             "verification, but separate from bibliographic resolve"),
        )
    if origin in ("webfetch", "websearch", "manual") and traceable_url:
        return (
            "externally_corroborated_text",
            ("recorded from a traceable external text source and usable for "
             "claim verification, but separate from bibliographic resolve"),
        )
    return None, None


def _repo_open(run_dir):
    try:
        return RunRepository.open(run_dir)
    except Exception:
        return None


def _repo_open_readonly(run_dir):
    try:
        return RunRepository.open_readonly(run_dir)
    except Exception:
        return None


def _load_refs(run: str):
    repo = _repo_open_readonly(run)
    if repo is None:
        raise SystemExit(f"provide requires a DB-backed run: no sqlite run database found in {run}")
    try:
        return repo.effective_parse_payload().get("references", [])
    finally:
        repo.close()


def _ref_by_id(refs, ref_id):
    return next((r for r in refs if r["id"] == ref_id), None)


def _ingest_identity_refs(run: str, refs: list[dict]) -> list[dict]:
    """Overlay resolver-validated identity onto parsed bibliography entries."""
    repo = _repo_open_readonly(run)
    if repo is None:
        return refs
    try:
        payloads = repo.resolve_payload_map()
    finally:
        repo.close()
    return [user_sources.identity_view(ref, payloads.get(ref.get("id"))) for ref in refs]


def cmd_ingest(args):
    if args.tier == "abstract":
        refs = _ingest_identity_refs(args.run, _load_refs(args.run))
        results = []
        for p in sorted(glob.glob(os.path.join(args.dir, "**", "*"), recursive=True)):
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in {".html", ".htm", ".txt", ".md", ".markdown"}:
                parsed = user_sources.read_user_source(p, tier="abstract")
                if parsed.get("outcome") != "ok":
                    results.append({"file": p, "outcome": parsed.get("outcome", "unreadable"),
                                    "ref_id": None, "ref_number": None})
                    continue
                choices = []
                for ref in refs:
                    signal, score, failure = user_sources.corroborate_user(
                        ref, parsed["text"], parsed.get("identity"))
                    if failure != "identity_mismatch":
                        choices.append((score, signal, failure, ref))
                choices.sort(key=lambda item: item[0], reverse=True)
                top_score = choices[0][0] if choices else 0.0
                top_tie = len(choices) > 1 and choices[1][0] == top_score
                if not choices or top_score < args.auto_threshold or top_tie:
                    results.append({"file": p, "outcome": "needs_manual_confirmation",
                                    "best_score": top_score,
                                    "ref_id": choices[0][3].get("id") if choices else None,
                                    "ref_number": choices[0][3].get("ref_number") if choices else None,
                                    **({"reason": "top_score_tie"} if top_tie else {})})
                    continue
                score, signal, failure, ref = choices[0]
                results.append(user_sources.ingest_file(args.run, ref, p, tier="abstract"))
        out = {"results": results, "tier": args.tier, "auto_threshold": args.auto_threshold}
        repo = _repo_open(args.run)
        if repo:
            try: repo.set_run_setting("ingest_review", out)
            finally: repo.close()
        accepted = sum(1 for result in results if str(result.get("outcome", "")).startswith("accepted_"))
        unreadable = sum(1 for result in results if result.get("outcome") == "unreadable")
        print(json.dumps({"accepted": accepted, "to_review": len(results) - accepted,
                          "unreadable": unreadable}, ensure_ascii=False))
        return
    refs = _load_refs(args.run)
    supported = extract_mod.supported_extensions()
    files = [p for p in sorted(glob.glob(os.path.join(args.dir, "**", "*"), recursive=True))
             if os.path.isfile(p) and os.path.splitext(p)[1].lower() in supported]
    # Score (file, ref) for every readable pair.
    scored = []   # (score, signal, file, ref, text, fmt)
    unreadable = []
    landing_review = []  # HTML-only: abstract/landing pages and identity conflicts,
                          # never eligible for auto-map even when a title/token score
                          # would otherwise clear the threshold.
    for p in files:
        try:
            text, _fmt, _meta = extract_mod.extract_text(p)
        except Exception as e:
            # Unreadable (e.g. scan without OCR). KEEP the file and queue it for OCR;
            # it cannot be associated to a reference yet (association needs the text).
            kept = sources.park_unreadable(
                args.run, None, p, origin="user",
                reason=f"unreadable on ingest: {type(e).__name__}: {e}", move=False)
            unreadable.append({"file": p, "kept_as": kept,
                               "error": f"{type(e).__name__}: {e}",
                               "next": "OCR (opt-in) then map the resulting .txt"})
            continue
        is_html = os.path.splitext(p)[1].lower() in _HTML_EXTS
        if is_html and not fetch_html.html_fulltext_ok(text):
            # A landing/abstract-only page must never be silently registered as
            # full text. Still try to name a likely reference (best effort, using
            # the page's own declared identity) so the review entry is useful.
            identity = user_sources.html_meta_identity(p)
            hint = None
            for r in refs:
                sig, score, failure = user_sources.corroborate_user(r, text, identity)
                if failure == "identity_mismatch":
                    continue
                if hint is None or score > hint[0]:
                    hint = (score, sig, r)
            landing_review.append({
                "file": p, "reason": "abstract_only_page",
                "ref_id": hint[2].get("id") if hint else None,
                "ref_number": hint[2].get("ref_number") if hint else None,
                "signal": hint[1] if hint else None, "score": hint[0] if hint else None,
                "note": "this HTML looks like an abstract/landing page, not full "
                        "text; register it at --tier abstract",
            })
            continue
        identity = user_sources.html_meta_identity(p) if is_html else None
        candidates = []
        for r in refs:
            if is_html:
                sig, score, failure = user_sources.corroborate_user(r, text, identity)
                if failure == "identity_mismatch":
                    continue
            else:
                sig, score = sources.corroborate(r, text)
            candidates.append((score, sig, r))
        if is_html and refs and not candidates:
            # Every reference conflicts with this file's declared DOI/PMID: block
            # auto-map outright rather than falling through to a token-only guess.
            landing_review.append({
                "file": p, "reason": "identity_conflict",
                "ref_id": None, "ref_number": None, "signal": "doi", "score": 0.0,
                "note": "declared identifier in the HTML conflicts with every "
                        "reference; not auto-mapped",
            })
            continue
        if candidates:
            best = max(candidates, key=lambda item: item[0])
            scored.append((best[0], best[1], p, best[2], text, _fmt))

    # Greedy 1:1 assignment above auto threshold.
    scored.sort(key=lambda x: -x[0])
    taken_ref, taken_file = set(), set()
    mapped, review = [], list(landing_review)
    for score, sig, p, r, text, fmt in scored:
        if score >= args.auto_threshold and r["id"] not in taken_ref and p not in taken_file:
            # Keep the raw original in sources/provided/ (audit), store the parsed text.
            provided = sources.store_provided_raw(args.run, p, ref=r)
            archived = content_store.archive_user_original(
                args.run,
                p,
                ref=r,
                supplied_via=f"user_{fmt}",
                file_format=fmt,
                move=True,
            )
            entry = sources.store_text(args.run, r, "fulltext", "user", text,
                                       source_ref=provided or os.path.abspath(p),
                                       mapping="deterministic", signal=sig, score=score,
                                       supplied_by="user", supplied_via=f"user_{fmt}",
                                       file_format=fmt,
                                       library_item_id=(archived or {}).get("library_item_id"))
            mapped.append({"file": p, "ref_number": r["ref_number"],
                           "ref_id": r["id"], "signal": sig, "score": score,
                           "stored_as": entry["stored_as"], "provided_as": provided,
                           "library_as": (archived or {}).get("stored_relpath")})
            taken_ref.add(r["id"]); taken_file.add(p)
    # Ambiguous: unassigned files, with the best candidates for manual confirmation.
    for score, sig, p, r, text, _fmt in scored:
        if p in taken_file:
            continue
        review.append({"file": p, "best_ref_number": r["ref_number"],
                       "best_ref_id": r["id"], "signal": sig, "score": score,
                       "note": "below threshold or reference already taken: confirm with 'map'"})
    out = {"mapped": mapped, "review": review, "unreadable": unreadable,
           "auto_threshold": args.auto_threshold}
    repo = _repo_open(args.run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {args.run}")
    try:
        repo.set_run_setting("ingest_review", out)
    finally:
        repo.close()
    print(json.dumps({"mapped": len(mapped), "to_review": len(review),
                      "unreadable": len(unreadable)}, ensure_ascii=False))


def cmd_map(args):
    refs = _load_refs(args.run)
    ref = _ref_by_id(refs, args.ref)
    if not ref:
        raise SystemExit(f"reference {args.ref} not found")
    if args.tier == "abstract":
        ext = os.path.splitext(args.file)[1].lower()
        supported = {".html", ".htm", ".txt", ".md", ".markdown"}
        if ext not in supported:
            raise SystemExit(
                f"unsupported abstract input format {ext or '<none>'}; "
                "expected .html, .htm, .txt, .md, or .markdown"
            )
        ref = _ingest_identity_refs(args.run, [ref])[0]
        result = user_sources.ingest_file(args.run, ref, args.file, tier="abstract")
        print(json.dumps({**result, "ref_number": ref["ref_number"]}, ensure_ascii=False))
        return
    try:
        text, _fmt, _meta = extract_mod.extract_text(args.file)
    except Exception as e:
        # Unreadable scan: DO NOT crash. The reference is known (the user told us),
        # so park it ASSOCIATED to this ref and queue it for OCR. After OCR, map the
        # resulting .txt to the same ref and it will corroborate normally.
        kept = sources.park_unreadable(
            args.run, ref, args.file, origin=args.origin,
            reason=f"unreadable on map: {type(e).__name__}: {e}", move=False)
        print(json.dumps({
            "ok": False, "reason": "unreadable_pdf",
            "ref_number": ref["ref_number"], "kept_as": kept,
            "hint": "scanned PDF with no text layer: run `"
                    f"{run_command('ocr', '--pdf', kept, '--out', '<txt>')}` (opt-in), then map the .txt to this ref"},
            ensure_ascii=False))
        return
    is_html = os.path.splitext(args.file)[1].lower() in _HTML_EXTS
    if args.tier == "fulltext" and is_html and not fetch_html.html_fulltext_ok(text):
        # A landing/abstract-only page must never be registered as full text,
        # forced or not: --force overrides a WEAK corroboration score, not the
        # basic fact that this page is not the article body.
        print(json.dumps({
            "ok": False, "reason": "abstract_only_page",
            "ref_number": ref["ref_number"],
            "hint": "this HTML looks like an abstract/landing page, not full "
                    "text; register it at --tier abstract"}, ensure_ascii=False))
        return
    if is_html:
        identity = user_sources.html_meta_identity(args.file)
        sig, score, failure = user_sources.corroborate_user(ref, text, identity)
        if failure == "identity_mismatch":
            # A declared DOI/PMID that conflicts with the reference is terminal
            # for this (file, ref) pair, exactly as for the abstract tier — not
            # something --force can talk its way past.
            raise SystemExit(json.dumps({
                "ok": False, "reason": "identity_mismatch",
                "signal": sig, "score": score,
                "hint": "this HTML declares a DOI/PMID that conflicts with the "
                        "reference; map it to the correct reference instead"},
                ensure_ascii=False))
    else:
        sig, score = sources.corroborate(ref, text)
    corroborated = sig in ("doi", "pmid") or score >= sources.CORROBORATE_THRESHOLD
    if not corroborated and not args.force:
        raise SystemExit(json.dumps({
            "ok": False, "reason": "not_corroborated",
            "signal": sig, "score": score,
            "hint": "DOI/PMID or entry tokens not found in the file; "
                    "if you are certain, repeat with --force"}, ensure_ascii=False))
    mapping = ("deterministic" if sig in ("doi", "pmid")
               else "manual" if args.force else "model_corroborated")
    identity_status, identity_note = _identity_for_origin(
        args.origin, traceable_url=False, forced=args.force
    )
    # origin 'ocr' = the .txt produced from a parked scan: store the parsed text, then
    # retire the queued PDF (delete it, mark the queue entry done). Any other origin is
    # a user-supplied original → keep the raw in sources/provided/ for audit.
    if args.origin == "ocr":
        provided, retired = None, sources.resolve_unreadable(
            args.run, ref_id=ref["id"], method="ocr")
        archived = None
    else:
        provided, retired = sources.store_provided_raw(
            args.run, args.file, ref=ref), False
        archived = content_store.archive_user_original(
            args.run,
            args.file,
            ref=ref,
            supplied_via=f"user_{_fmt}",
            file_format=_fmt,
            move=True,
        )
    entry = sources.store_text(args.run, ref, args.tier, args.origin, text,
                               source_ref=provided or os.path.abspath(args.file),
                               mapping=mapping, signal=sig, score=score,
                               identity_status=identity_status,
                               identity_note=identity_note,
                               supplied_by="user" if args.origin in ("user", "ocr") else "script",
                               supplied_via="user_ocr" if args.origin == "ocr" else f"user_{_fmt}",
                               file_format=_fmt,
                               library_item_id=(archived or {}).get("library_item_id"))
    print(json.dumps({"ok": True, "ref_number": ref["ref_number"],
                      "stored_as": entry["stored_as"], "mapping": mapping,
                      "provided_as": provided, "ocr_queue_retired": retired,
                      "signal": sig, "score": score,
                      "identity_status": identity_status}, ensure_ascii=False))


def cmd_record(args):
    refs = _load_refs(args.run)
    ref = _ref_by_id(refs, args.ref)
    if not ref:
        raise SystemExit(f"reference {args.ref} not found")
    if args.text_file:
        with open(args.text_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    sig, score = sources.corroborate(ref, text)
    identity_status, identity_note = _identity_for_origin(
        args.origin, traceable_url=bool(args.url), forced=False
    )
    entry = sources.store_text(args.run, ref, args.tier, args.origin, text,
                               source_ref=args.url, mapping="web", signal=sig, score=score,
                               identity_status=identity_status,
                               identity_note=identity_note,
                               supplied_by="script",
                               supplied_via=f"{args.origin}_{args.tier}")
    print(json.dumps({"ok": True, "ref_number": ref["ref_number"],
                      "tier": args.tier, "origin": args.origin,
                      "stored_as": entry["stored_as"], "signal": sig,
                      "score": score, "identity_status": identity_status},
                     ensure_ascii=False))


def cmd_path(args):
    repo = _repo_open_readonly(args.run)
    if repo is not None:
        try:
            man = repo.source_manifest_payload()
        finally:
            repo.close()
    else:
        man = sources.load_manifest(args.run)
    if args.tier:
        e = next((x for x in man.get("entries", [])
                  if x.get("ref_id") == args.ref and x.get("tier") == args.tier), None)
    else:
        e = sources.best_for(man, args.ref)
    if not e:
        raise SystemExit(f"no text registered for {args.ref}"
                         + (f" at tier {args.tier}" if args.tier else ""))
    print(os.path.join(args.run, "sources", e["stored_as"]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest"); pi.set_defaults(fn=cmd_ingest)
    pi.add_argument("--run", required=True)
    pi.add_argument("--dir", required=True)
    pi.add_argument("--auto-threshold", dest="auto_threshold", type=float,
                    default=sources.AUTO_THRESHOLD)
    pi.add_argument("--tier", choices=("fulltext", "abstract"), default="fulltext")

    pm = sub.add_parser("map"); pm.set_defaults(fn=cmd_map)
    pm.add_argument("--run", required=True)
    pm.add_argument("--ref", required=True); pm.add_argument("--file", required=True)
    pm.add_argument("--tier", default="fulltext", choices=sources.TIERS)
    pm.add_argument("--origin", default="user", choices=sorted(sources.ORIGINS))
    pm.add_argument("--force", action="store_true")

    pr = sub.add_parser("record"); pr.set_defaults(fn=cmd_record)
    pr.add_argument("--run", required=True)
    pr.add_argument("--ref", required=True)
    pr.add_argument("--tier", required=True, choices=sources.TIERS)
    pr.add_argument("--origin", required=True, choices=sorted(sources.ORIGINS))
    pr.add_argument("--url"); pr.add_argument("--text-file", dest="text_file")

    pp = sub.add_parser("path"); pp.set_defaults(fn=cmd_path)
    pp.add_argument("--run", required=True); pp.add_argument("--ref", required=True)
    pp.add_argument("--tier", choices=sources.TIERS)

    args = ap.parse_args()
    if args.cmd in {"ingest", "map", "record"}:
        raise SystemExit(
            "direct source mutation is disabled; answer a generated FETCH task "
            "through `run.py tasks answer-fetch` so the trusted authority can "
            "authenticate and checkpoint the input"
        )
    args.fn(args)


if __name__ == "__main__":
    main()
