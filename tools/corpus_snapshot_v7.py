#!/usr/bin/env python3
# tools/corpus_snapshot_v7.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the V7 baseline snapshot: parse + deterministic OA fetch over the corpus.

One `run.py` invocation per paper does parse -> resolve -> automatic OA fetch and
then pauses at the FETCH gate; that pause is exactly the baseline this snapshot
records (what the pipeline obtained on its own, before any agent help). Papers are
processed one at a time, so each paper is parsed and then fetched before the next
starts, and its deliverables are materialised immediately.

Debug capture ("the label") is forced on via CITATION_VERIFIER_DEBUG_RUN=1 so the
parse phase keeps its parse_debug.md provenance. The JSONL files below are offline
diagnostic exports materialised from the authoritative SQLite run.

Layout produced under --out (default "Paper test/V7"):

  <out>/parser/<paper>/         parse_debug.md, parse.json, debug_events.parse.jsonl
  <out>/full text/<paper>/      *_fulltext_*.txt, index.json, fetch_manual_sources.md,
                                debug_events.fetch.jsonl
  <out>/_runs/<paper>/          the real run dir (run.sqlite, caches) — source of truth
  <out>/fetch_fingerprint.json  corpus fingerprint, comparable to earlier sweeps

The credential preflight is inherited from fetch_corpus_sweep: the run refuses to
start unauthenticated, because an uncredentialed sweep produces numbers that look
comparable to earlier ones and are not (the V5 post-mortem).

Usage:
  python3 tools/corpus_snapshot_v7.py [--out "Paper test/V7"] [--only NAME ...] [--skip-done]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Reuse the proven sweep building blocks: paper map, credential preflight, the
# exact `.env`-resolving child environment, per-reference fingerprint, and the
# V5-lesson environment diagnostics.
from core.parse.footnotes import cross_reference_map  # noqa: E402
from tools.fetch_corpus_sweep import (  # noqa: E402
    PAPERS,
    _child_environment,
    environment_diagnostics,
    fingerprint,
    preflight,
    REQUIRED_ENV,
)
from core.infra.db import RunRepository  # noqa: E402
from core.fetch.storage import content_store as _content_store  # noqa: E402

DEBUG_ENV = "CITATION_VERIFIER_DEBUG_RUN"


def _write_jsonl(path: str, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False, default=str, sort_keys=True) + "\n")


def _diagnostic_events(repo: RunRepository, parse: dict, resolve_map: dict) -> tuple[list[dict], list[dict]]:
    """Build deterministic offline debug exports from the current SQLite schema."""
    references = parse.get("references")
    if not isinstance(references, list):
        raise RuntimeError("diagnostic export requires a well-formed parse reference list")
    ref_numbers: dict[str, int] = {}
    for reference in references:
        if not isinstance(reference, dict):
            raise RuntimeError("diagnostic export encountered a malformed parse reference")
        ref_id = reference.get("id")
        ref_number = reference.get("ref_number")
        if not isinstance(ref_id, str) or not ref_id or not isinstance(ref_number, int):
            raise RuntimeError("diagnostic export encountered incomplete parse reference identity")
        if ref_id in ref_numbers:
            raise RuntimeError("diagnostic export encountered duplicate parse reference identity")
        ref_numbers[ref_id] = ref_number

    parse_events: list[dict] = []
    fetch_events: list[dict] = []
    for event in repo.list_phase_events():
        record = {"record_type": "phase_event", "event_id": event.event_id, "phase": event.phase,
                  "event_type": event.event_type, "created_at": event.created_at,
                  "session_id": event.session_id, "payload": event.payload}
        if event.phase == "parse":
            parse_events.append(record)
        elif event.phase in ("resolve", "fetch"):
            fetch_events.append(record)
    for ref_id, ref_number in sorted(ref_numbers.items(), key=lambda item: (item[1], item[0])):
        if ref_id not in resolve_map or not isinstance(resolve_map[ref_id], dict):
            raise RuntimeError(f"diagnostic export requires resolve data for {ref_id}")
        for record_type, rows in (("resolver_attempt", repo.list_resolver_attempts(ref_id)),
                                  ("identifier_validation", repo.list_identifier_validations(ref_id)),
                                  ("retraction_check", repo.list_retraction_checks(ref_id))):
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise RuntimeError(f"diagnostic export encountered malformed {record_type} data for {ref_id}")
            for ordinal, row in enumerate(rows):
                fetch_events.append({"record_type": record_type, "phase": "resolve", "ref_id": ref_id,
                                     "ref_number": ref_number, "ordinal": ordinal, "payload": row})
        attempts = repo.list_fetch_attempts(ref_id)
        if not isinstance(attempts, list) or any(not isinstance(attempt, dict) for attempt in attempts):
            raise RuntimeError(f"diagnostic export encountered malformed fetch attempt data for {ref_id}")
        for ordinal, attempt in enumerate(attempts):
            fetch_events.append({"record_type": "fetch_attempt", "phase": "fetch", "ref_id": ref_id,
                                 "ref_number": ref_number, "ordinal": ordinal, "payload": attempt})
    if not parse_events or not fetch_events:
        raise RuntimeError("diagnostic export requires non-empty parse and resolve/fetch event data")
    return parse_events, fetch_events


def _materialise(name: str, run_dir: str, out: str) -> dict:
    """Reshape one finished run dir into the parser/ and full text/ deliverables.

    Returns a small summary dict for the driver's console line.
    """
    parser_dir = os.path.join(out, "parser", name)
    fulltext_dir = os.path.join(out, "full text", name)
    os.makedirs(parser_dir, exist_ok=True)
    os.makedirs(fulltext_dir, exist_ok=True)

    repo = RunRepository.open_readonly(run_dir)
    try:
        parse = repo.parse_payload()
        manifest = repo.source_manifest_payload()
        resolve_map = repo.resolve_payload_map()
        parse_events, fetch_events = _diagnostic_events(repo, parse, resolve_map)
    finally:
        repo.close()

    # --- parser/ : parse output + parse-phase debug ---------------------------
    src_parse_debug = os.path.join(run_dir, "parse_debug.md")
    if os.path.isfile(src_parse_debug):
        shutil.copy2(src_parse_debug, os.path.join(parser_dir, "parse_debug.md"))
    with open(os.path.join(parser_dir, "parse.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "manuscript": parse.get("manuscript") or {},
                "coverage": parse.get("coverage") or {},
                "references": parse.get("references") or [],
                "citations": parse.get("citations") or [],
                "claims": parse.get("claims") or [],
                "table_only_citations": parse.get("table_only_citations") or [],
            },
            fh,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    _write_jsonl(os.path.join(parser_dir, "debug_events.parse.jsonl"), parse_events)

    # --- full text/ : fetched full texts + fetch/resolve debug ----------------
    tiers: dict[str, str] = {}
    entries_by_ref: dict[str, list[dict]] = {}
    for e in manifest.get("entries", []):
        entries_by_ref.setdefault(e["ref_id"], []).append(e)

    fulltext_count = 0
    for entry in manifest.get("entries", []):
        if entry.get("tier") != "fulltext":
            continue
        stored = entry.get("stored_as")  # e.g. "parsed/1_fulltext_arxiv.txt"
        if not stored:
            continue
        src = os.path.join(run_dir, "sources", stored)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(fulltext_dir, os.path.basename(stored)))
            fulltext_count += 1

    def _ref_number_of(refs, ref_id):
        return next((r.get("ref_number") for r in refs if r["id"] == ref_id), None)

    # A back-reference ("Id. at 96.", "X, supra note 10.") is never fetched: it has
    # no document of its own, and verify reads its ANTECEDENT's stored text instead
    # (core/app/phases/verify.py).  Resolve already hands such a note its
    # antecedent's ABSTRACT, so the gap was only ever in retrieved text — but that
    # is the tier the fetch baseline is read off, and it was scoring every one of
    # those notes as having nothing while verify was about to check it against a
    # real document.
    #
    # On the law-review paper: fulltext 36 -> 51, coverage 31.6% -> 37.8%.
    #
    # So the antecedent's text is followed here, exactly as verify follows it.
    references = parse.get("references", [])
    cross_references = cross_reference_map(references)

    # Per-reference index: what we have and how we got it, for auditing the folder.
    index = []
    ft = ab = ni = 0
    inherited = 0
    for ref in references:
        rid = ref["id"]
        have = {e.get("tier") for e in entries_by_ref.get(rid, [])}
        ex = resolve_map.get(rid, {})
        inherited_from = None
        if not have and not ex.get("abstract") and rid in cross_references:
            antecedent_id = cross_references[rid][0]
            antecedent_have = {e.get("tier") for e in entries_by_ref.get(antecedent_id, [])}
            antecedent_abstract = (resolve_map.get(antecedent_id, {}) or {}).get("abstract")
            if antecedent_have or antecedent_abstract:
                have = antecedent_have
                ex = {**ex, "abstract": ex.get("abstract") or antecedent_abstract}
                inherited_from = _ref_number_of(references, antecedent_id)
                inherited += 1
        if "fulltext" in have:
            tier = "fulltext"; ft += 1
        elif "abstract" in have or ex.get("abstract"):
            tier = "abstract"; ab += 1
        else:
            tier = "niente"; ni += 1
        stored_files = [
            os.path.basename(e["stored_as"])
            for e in entries_by_ref.get(rid, [])
            if e.get("tier") == "fulltext" and e.get("stored_as")
        ]
        index.append({
            "ref_number": ref.get("ref_number"),
            "title": ref.get("title"),
            "doi": ref.get("doi"),
            "tier": tier,
            "status": ex.get("status"),
            "via": ex.get("via"),
            "origin": next((e.get("origin") for e in entries_by_ref.get(rid, [])
                            if e.get("tier") == "fulltext"), None),
            "stored_files": stored_files,
            # Which note's text this one is read against, when it is not its own.
            "text_inherited_from_note": inherited_from,
        })
    with open(os.path.join(fulltext_dir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"totals": {"fulltext": ft, "abstract_only": ab, "neither": ni,
                        "inherited_from_antecedent": inherited},
             "references": index},
            fh, ensure_ascii=False, indent=2, default=str,
        )

    for fname in ("fetch_manual_sources.md",):
        src = os.path.join(run_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(fulltext_dir, fname))
    _write_jsonl(os.path.join(fulltext_dir, "debug_events.fetch.jsonl"), fetch_events)

    return {"refs": len(references), "fulltext": ft, "abstract_only": ab,
            "neither": ni, "files": fulltext_count, "inherited": inherited}


def _is_materialised(out: str, name: str) -> bool:
    """True only if this paper finished and its deliverables were written.

    Keyed off the full-text index.json, which `_materialise` writes as its last
    step. A bare run.sqlite is not enough: a run killed mid-parse still leaves a
    fingerprintable-but-empty database, and skipping on that would silently bake a
    half-fetched paper into the baseline.
    """
    return os.path.isfile(os.path.join(out, "full text", name, "index.json"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="Paper test/V7")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--accuracy", default="standard_web")
    ap.add_argument("--timeout", type=int, default=3600, help="per-paper seconds")
    ap.add_argument("--skip-done", action="store_true",
                    help="skip papers whose run dir already fingerprints")
    ap.add_argument("--workers", type=int, default=1,
                    help="how many papers to process concurrently. The shared "
                         "content store is WAL/busy-timeout safe, but every worker "
                         "multiplies request pressure on OpenAlex/CORE/Crossref, so "
                         "a high count risks 429s that make the baseline drift below "
                         "the sequential sweeps. 2 is a safe overlap; keep <=3.")
    ap.add_argument("--allow-missing-credentials", action="store_true")
    args = ap.parse_args()

    env = _child_environment()
    # Force the debug label on for every child, whatever the shell had.
    env[DEBUG_ENV] = "1"

    missing = preflight(env)
    if missing:
        print(f"PREFLIGHT: missing credentials: {', '.join(missing)}", file=sys.stderr)
        print(f"  expected in {os.path.join(REPO_ROOT, '.env')} or the shell", file=sys.stderr)
        if not args.allow_missing_credentials:
            print("  refusing to start: an uncredentialed sweep is not comparable "
                  "to earlier ones (V5).", file=sys.stderr)
            return 2
        print("  --allow-missing-credentials given: continuing under protest.", file=sys.stderr)
    else:
        print(f"preflight OK - {', '.join(REQUIRED_ENV)} present; "
              f"GOOGLE_BOOKS_API_KEY {'present' if (env.get('GOOGLE_BOOKS_API_KEY') or '').strip() else 'MISSING'}; "
              f"debug label {DEBUG_ENV}=1")

    out = args.out
    runs_root = os.path.join(out, "_runs")
    os.makedirs(runs_root, exist_ok=True)

    names = args.only or list(PAPERS)
    unknown = [n for n in names if n not in PAPERS]
    if unknown:
        print(f"unknown paper name(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    papers_fp, failures = {}, []
    print_lock = threading.Lock()
    done = [0]
    total = len(names)

    def process(name: str) -> None:
        src = PAPERS[name]
        run_dir = os.path.join(runs_root, name)
        log_path = os.path.join(runs_root, f"{name}.log")
        line = None
        if args.skip_done and _is_materialised(out, name):
            elapsed_note = "skip (already done) -> "
        else:
            started = time.time()
            # Give every paper its OWN content store, keyed to its run dir. Concurrent
            # workers sharing one content.sqlite thrash on its lock (busy_timeout
            # retries then failed stores), which silently zeroes fulltext even on
            # papers that fetch trivially. Isolation removes the contention entirely;
            # cross-paper dedup is lost, which does not matter for a baseline.
            paper_env = dict(env)
            paper_env[_content_store.ENV_STATE_DIR] = os.path.abspath(run_dir)
            cmd = [sys.executable, "run.py", "--input", src, "--run", run_dir,
                   "--accuracy", args.accuracy, "--http-profile", "browser_like",
                   "--challenge-mode", "off", "--proceed"]
            with open(log_path, "w", encoding="utf-8") as log:
                try:
                    subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=REPO_ROOT, env=paper_env, timeout=args.timeout,
                                   check=False)
                except subprocess.TimeoutExpired:
                    log.write("\n[snapshot] TIMEOUT\n")
                    failures.append((name, "timeout"))
                    line = f"TIMEOUT after {args.timeout}s"
            elapsed_note = None if line else f"{time.time() - started:.0f}s -> "

        if line is None:
            try:
                fp = fingerprint(run_dir)
            except Exception as exc:  # noqa: BLE001
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                line = f"{elapsed_note}NO FINGERPRINT ({exc})"
            else:
                papers_fp[name] = fp
                try:
                    summ = _materialise(name, run_dir, out)
                except Exception as exc:  # noqa: BLE001
                    failures.append((name, f"materialise {type(exc).__name__}: {exc}"))
                    line = f"{elapsed_note}fingerprinted but LAYOUT FAILED ({exc})"
                else:
                    line = (f"{elapsed_note}{summ['refs']} refs · "
                            f"fulltext {summ['fulltext']} ({summ['files']} files, "
                            f"{summ['inherited']} ereditati) · "
                            f"abstract {summ['abstract_only']} · niente {summ['neither']}")
        with print_lock:
            done[0] += 1
            print(f"[{done[0]}/{total}] {name} ... {line}", flush=True)

    workers = max(1, args.workers)
    print(f"processing {total} paper(s) with {workers} worker(s)", flush=True)
    if workers == 1:
        for name in names:
            process(name)
    else:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(process, names))

    # Merge any neighbour already on disk so the fingerprint file is a true picture.
    for name in sorted(os.listdir(runs_root)):
        run_dir = os.path.join(runs_root, name)
        if name in papers_fp or not os.path.isfile(os.path.join(run_dir, "run.sqlite")):
            continue
        try:
            papers_fp[name] = fingerprint(run_dir)
        except Exception:  # noqa: BLE001
            continue

    diagnostics = {n: environment_diagnostics(os.path.join(runs_root, n))
                   for n in papers_fp}
    out_json = os.path.join(out, "fetch_fingerprint.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(
            {"meta": {"generated": time.strftime("%Y-%m-%d"),
                      "accuracy": args.accuracy,
                      "debug_label": f"{DEBUG_ENV}=1",
                      "papers": len(papers_fp)},
             "environment_diagnostics": diagnostics,
             "papers": papers_fp},
            fh, ensure_ascii=False, indent=2, default=str,
        )

    tot = {"refs": 0, "fulltext": 0, "abstract_only": 0, "neither": 0}
    for fp in papers_fp.values():
        for k in tot:
            tot[k] += fp.get(k, 0)
    cov = 100 * tot["fulltext"] / tot["refs"] if tot["refs"] else 0
    no_key = sum(d.get("core/no-key", 0) for d in diagnostics.values())
    print(f"\nDONE {len(papers_fp)} papers · {tot['refs']} refs · "
          f"fulltext {tot['fulltext']} ({cov:.1f}%) · core/no-key {no_key}")
    if no_key:
        print("  WARNING: core/no-key > 0 — this snapshot is NOT comparable (V5).",
              file=sys.stderr)
    if failures:
        print(f"  {len(failures)} issue(s): " +
              "; ".join(f"{n}: {why}" for n, why in failures), file=sys.stderr)
    print(f"  fingerprint -> {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
