#!/usr/bin/env python3
# tools/fetch_corpus_sweep.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run resolve+fetch over the whole corpus and fingerprint the outcome.

Each paper gets its own run directory; the driver is left to stop at its FETCH
pause, which is exactly the point this snapshot describes: what resolve and the
deterministic OA fetch managed to obtain on their own, before any agent help.

The fingerprint records, per reference, the tier reached and how it resolved, so
two sweeps can be diffed reference by reference rather than by totals alone.

The sweep refuses to start if a configured provider would go out unauthenticated.
That guard is not paranoia: the V5 sweep ran from a git worktree, `.env` is
git-ignored so the worktree had none, and every CORE lookup recorded "no CORE API
key configured" instead of failing.  CORE went from 29 stored full texts to zero,
OpenAlex from 310 rate-limit hits to 1408, and the corpus read 75 full texts below
the previous sweep — a result that looked like a code regression for a day.  A
sweep missing a provider is worse than one that does not start, because its
numbers look comparable to earlier sweeps and are not.

Usage:
  python3 tools/fetch_corpus_sweep.py --out "Paper test/resolve_fetch_v4" [--only NAME ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.infra.db import RunRepository
from core.app.runtime.settings import ACTION_REQUIRED
from tools.fetch_fingerprint_store import write_fingerprint

# Short run name -> corpus file.  The names match the earlier sweeps so the
# fingerprints stay comparable.
PAPERS = {
    "2006-23058-006": "Paper test/Nuovo/2006-23058-006.pdf",
    "234003_AIP": "Paper test/Nuovo/234003_1_5.0144385.pdf",
    "GroJIL": "Paper test/Nuovo/260416+-+Flexibility_in_IIL-I_GroJIL_styled+-+v2.3.pdf",
    "5376": "Paper test/Nuovo/5376-Article Text-24692-1-10-20240809.pdf",
    "7-135-krishnamurthy": "Paper test/Nuovo/7-135-krishnamurthy.pdf",
    "MLA_fixture_Mondal": "Paper test/Nuovo/MLA_fixture_Mondal_2026.pdf",
    "corso_2026": "Paper test/Nuovo/corso_2026_oi_260603_1782846613.27059.pdf",
    "fmed": "Paper test/Nuovo/fmed-12-1550047.pdf",
    "hybrid-plasmonic": ("Paper test/Nuovo/hybrid-organic-plasmonic-nanoantennas-"
                         "with-enhanced-third-harmonic-generation.pdf"),
    "icae138": "Paper test/Nuovo/icae138.pdf",
    "jea": "Paper test/Nuovo/jea-08-2024-0278en.pdf",
    "s41467_Nature": "Paper test/Nuovo/s41467-026-72585-4.pdf",
    "manuscript_anonymous": "Paper test/Copia di manuscript anonymous - 2.pdf",
    "attention_is_all_you_need": "Paper test/attention_is_all_you_need.pdf",
    "bert": "Paper test/bert.pdf",
    "consort2010": "Paper test/consort2010_plos_vancouver.pdf",
    "vilt": "Paper test/vilt_ieee_style.pdf",
    "vit": "Paper test/vit_image_worth_16x16.pdf",
}


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Credentials whose absence degrades a provider silently rather than loudly.
# CITATION_VERIFIER_MAILTO gates OpenAlex: without it OpenAlex is disabled outright.
REQUIRED_ENV = ("CORE_API_KEY", "CITATION_VERIFIER_MAILTO", "OPENALEX_API_KEY")


def _child_environment() -> dict:
    """What the child process will see, resolved the way the app resolves it.

    The app reads `.env` from the repository it executes out of, and a shell export
    wins over the file.  Mirroring that here is the only way the preflight can speak
    for the child rather than for this process.
    """
    env = dict(os.environ)
    path = os.path.join(REPO_ROOT, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if value and key not in env:
                    env[key] = value
    return env


def preflight(env: dict) -> list[str]:
    """Credentials the child would start without.  Empty means good to go."""
    return [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]


def environment_diagnostics(run_dir: str) -> dict[str, int]:
    """Count the silent provider failures the V5 post-mortem taught us to watch.

    `core/no-key` must be zero.  A non-zero count means the sweep is not comparable
    to any other sweep, whatever its totals say.
    """
    out = {"core/no-key": 0, "openalex/429": 0, "core/stored": 0}
    db = os.path.join(run_dir, "run.sqlite")
    if not os.path.isfile(db):
        return out
    repo = RunRepository.open_readonly(run_dir)
    try:
        for result in repo.list_resolve_results():
            for item in result.attempts or []:
                if not isinstance(item, dict):
                    continue
                reason = str(item.get("reason") or "")
                if "no CORE API key" in reason:
                    out["core/no-key"] += 1
                if "rate_limited" in reason or "429" in reason:
                    out["openalex/429"] += 1
        out["core/stored"] = repo._conn.execute(
            "select count(*) from fetch_attempts "
            "where method like '%core%' and outcome = 'stored'"
        ).fetchone()[0]
    finally:
        repo.close()
    return out


def fingerprint(run_dir: str) -> dict:
    """Canonical post-Fetch facts, excluding run ids and timestamps."""
    repo = RunRepository.open_readonly(run_dir)
    try:
        parse = repo.parse_payload()
        resolve_map = repo.resolve_payload_map()
        manifest = repo.source_manifest_payload()
        unreadable = repo.unreadable_payload()
        task_rows = repo._conn.execute(
            """
            SELECT t.task_id,t.ref_id,t.status,t.task_kind,t.last_error_stage,t.last_error_type,
                   t.last_error_message,a.answer_id,a.actor_type,a.answer_kind,
                   a.accepted_for_processing,p.producer_class,p.producer_identity,
                   p.ingress_kind,p.authority_id
            FROM tasks t
            LEFT JOIN task_answers a ON a.task_id=t.task_id
            LEFT JOIN task_answer_provenance p ON p.answer_id=a.answer_id
            WHERE t.slot='fetch'
            ORDER BY t.ref_id,t.task_id,a.answer_id
            """
        ).fetchall()
        task_files = repo._conn.execute(
            """
            SELECT a.answer_id,f.file_order,f.original_name,f.sha256,f.byte_count
            FROM tasks t JOIN task_answers a ON a.task_id=t.task_id
            JOIN task_answer_provenance_files f ON f.answer_id=a.answer_id
            WHERE t.slot='fetch' ORDER BY t.ref_id,t.task_id,f.file_order
            """
        ).fetchall()
        candidate_rows = repo._conn.execute(
            """
            SELECT p.ref_id,s.stage,c.candidate_fingerprint,e.event_type,
                   e.reason_code,e.reason_detail
            FROM fetch_candidate_plans p
            JOIN fetch_candidate_stage_freezes s ON s.plan_id=p.plan_id
            JOIN fetch_frozen_candidates c ON c.stage_freeze_id=s.stage_freeze_id
            LEFT JOIN fetch_frozen_candidate_events e ON e.frozen_candidate_id=c.frozen_candidate_id
            ORDER BY p.ref_id,s.stage,c.candidate_order,e.event_type
            """
        ).fetchall()
        attempt_rows = repo._conn.execute(
            """
            SELECT ref_id,method,url,kind,final_url,status_code,content_type,outcome,reason,
                   challenge_blocked,paywalled,origin
            FROM fetch_attempts
            ORDER BY ref_id,method,kind,outcome,reason,url,final_url,status_code,content_type,origin
            """
        ).fetchall()
    finally:
        repo.close()

    tiers: dict[str, set[str]] = {}
    for entry in manifest.get("entries", []):
        tiers.setdefault(entry["ref_id"], set()).add(entry["tier"])

    sources: dict[str, list[dict]] = {}
    for entry in manifest.get("entries", []):
        facts = {key: entry.get(key) for key in (
            "tier", "sha256", "char_count", "origin", "source_ref", "mapping", "identity_status",
            "identity_note", "match_signal", "match_score", "supplied_by", "supplied_via",
            "file_format", "extraction_method", "content_version", "provenance_relation",
            "extraction_flags") if key in entry}
        sources.setdefault(entry["ref_id"], []).append(facts)
    unreadable_by_ref: dict[str, list[dict]] = {}
    for entry in unreadable.get("entries", []):
        if entry.get("ref_id"):
            unreadable_by_ref.setdefault(entry["ref_id"], []).append({
                key: entry.get(key) for key in ("kept_as", "source_ref", "origin", "reason", "ocr_status", "ocr_method")
            })
    tasks_by_ref: dict[str, list[dict]] = {}
    files_by_answer: dict[str, list[dict]] = {}
    for row in task_files:
        files_by_answer.setdefault(row["answer_id"], []).append({
            key: row[key] for key in ("file_order", "original_name", "sha256", "byte_count")
        })
    for row in task_rows:
        if not row["ref_id"]:
            continue
        fact = {"status": row["status"], "kind": row["task_kind"],
                "error_stage": row["last_error_stage"], "error_type": row["last_error_type"],
                "error": row["last_error_message"], "actor": row["actor_type"],
                "answer_kind": row["answer_kind"],
                "accepted_for_processing": (
                    bool(row["accepted_for_processing"])
                    if row["accepted_for_processing"] is not None else None
                ),
                "producer_class": row["producer_class"], "producer_identity": row["producer_identity"],
                "ingress": row["ingress_kind"], "authority_present": row["authority_id"] is not None,
                "files": files_by_answer.get(row["answer_id"], [])}
        tasks_by_ref.setdefault(row["ref_id"], []).append(
            {key: value for key, value in fact.items() if value is not None})
    candidates: dict[str, list[dict]] = {}
    for row in candidate_rows:
        fact = {"stage": row["stage"], "fingerprint": row["candidate_fingerprint"],
                "event": row["event_type"], "reason_code": row["reason_code"],
                "error": row["reason_detail"]}
        candidates.setdefault(row["ref_id"], []).append({key: value for key, value in fact.items() if value is not None})
    for row in attempt_rows:
        fact = {"kind": "attempt", "method": row["method"], "url": row["url"],
                "attempt_kind": row["kind"], "final_url": row["final_url"],
                "status": row["status_code"], "content_type": row["content_type"],
                "outcome": row["outcome"], "reason": row["reason"],
                "challenge": bool(row["challenge_blocked"]), "paywalled": bool(row["paywalled"]),
                "origin": row["origin"]}
        candidates.setdefault(row["ref_id"], []).append(
            {key: value for key, value in fact.items() if value is not None})

    per_ref, totals = {}, {"fulltext": 0, "abstract_only": 0, "neither": 0}
    for ref in parse.get("references", []):
        rid = ref["id"]
        have = tiers.get(rid, set())
        ex = resolve_map.get(rid, {})
        if "fulltext" in have:
            tier = "fulltext"
            totals["fulltext"] += 1
        elif "abstract" in have or ex.get("abstract"):
            tier = "abstract"
            totals["abstract_only"] += 1
        else:
            tier = "niente"
            totals["neither"] += 1
        per_ref[rid] = {
            # Reference ids are regenerated every run; the number is what two
            # sweeps can actually be matched on.
            "ref_number": ref.get("ref_number"),
            "tier": tier,
            "status": ex.get("status"),
            "tag": ex.get("reference_status_tag"),
            "via": ex.get("via"),
            "basis": ex.get("resolution_basis"),
            # A change can rewrite the verdict on a reference without moving it
            # between tiers - dropping a fabrication accusation is exactly that.
            # Recorded here, or such a change diffs as "nothing happened".
            "risk": ex.get("fabrication_risk"),
            "title_flag": ex.get("title_flag"),
            "fulltext_exists": (
                "true" if ex.get("fulltext_exists") is True
                else "false" if ex.get("fulltext_exists") is False
                else "unknown" if ex.get("fulltext_exists") == "unknown"
                else "absent"
            ),
            "sources": sorted(sources.get(rid, []), key=lambda item: json.dumps(item, sort_keys=True)),
            "unreadable": sorted(unreadable_by_ref.get(rid, []), key=lambda item: json.dumps(item, sort_keys=True)),
            "tasks": sorted(tasks_by_ref.get(rid, []), key=lambda item: json.dumps(item, sort_keys=True)),
            "candidates": sorted(candidates.get(rid, []), key=lambda item: json.dumps(item, sort_keys=True)),
        }
    parse_refs = []
    for ref in parse.get("references", []):
        # Run-local ids are intentionally excluded; Parse semantics are preserved.
        parse_refs.append({key: value for key, value in ref.items() if key not in {"id", "ref_id"}})
    parse_blob = json.dumps(sorted(parse_refs, key=lambda item: (item.get("ref_number") or 0, json.dumps(item, sort_keys=True))),
                            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "refs": len(parse.get("references", [])),
        **totals,
        "parse_fingerprint": hashlib.sha256(parse_blob.encode("utf-8")).hexdigest(),
        "snapshot_complete": True,
        "ref_fingerprint": per_ref,
    }


def _snapshot_readiness_error(run_dir: str) -> str | None:
    """Return why a child is not a complete post-Fetch snapshot, if anything."""
    repo = RunRepository.open_readonly(run_dir)
    try:
        run = repo.get_run()
        references = repo.parse_payload().get("references", [])
        resolved = repo.resolve_payload_map()
    finally:
        repo.close()

    if run.status != "paused" or run.phase not in {"fetch", "gaps"}:
        return f"unexpected run state {run.phase}/{run.status}"
    reference_ids = {ref["id"] for ref in references}
    resolved_ids = set(resolved)
    if resolved_ids != reference_ids:
        return (
            "incomplete Resolve state "
            f"({len(resolved_ids)}/{len(reference_ids)} references)"
        )
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="folder to hold one run directory per paper")
    ap.add_argument("--only", nargs="*", default=None, help="run just these short names")
    ap.add_argument("--accuracy", default="standard_web")
    ap.add_argument("--timeout", type=int, default=3600, help="per-paper seconds")
    ap.add_argument("--allow-missing-credentials", action="store_true",
                    help="sweep even with a provider unauthenticated (results are "
                         "NOT comparable to a fully-credentialed sweep)")
    args = ap.parse_args()

    env = _child_environment()
    missing = preflight(env)
    if missing:
        print(f"PREFLIGHT: missing credentials: {', '.join(missing)}", file=sys.stderr)
        print(f"  expected in {os.path.join(REPO_ROOT, '.env')} or the shell", file=sys.stderr)
        if not args.allow_missing_credentials:
            print("  refusing to start: the sweep would produce numbers that look "
                  "comparable to earlier sweeps and are not.", file=sys.stderr)
            print("  pass --allow-missing-credentials to sweep anyway.", file=sys.stderr)
            return 2
        print("  --allow-missing-credentials given: continuing under protest.",
              file=sys.stderr)
    else:
        print(f"preflight OK - {', '.join(REQUIRED_ENV)} present")

    os.makedirs(args.out, exist_ok=True)
    names = args.only or list(PAPERS)
    unknown = [n for n in names if n not in PAPERS]
    if unknown:
        print(f"unknown paper name(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    papers_fp, failures = {}, []
    for i, name in enumerate(names, 1):
        src = PAPERS[name]
        run_dir = os.path.join(args.out, name)
        log_path = os.path.join(args.out, f"{name}.log")
        print(f"[{i}/{len(names)}] {name} ... ", end="", flush=True)
        started = time.time()
        # --proceed: the startup check only warns about a missing report-signing
        # key, which a fetch snapshot never needs (it stops before any report).
        cmd = [sys.executable, "run.py", "--input", src, "--run", run_dir,
               "--accuracy", args.accuracy, "--http-profile", "browser_like",
               "--challenge-mode", "off", "--freeze-after-fetch", "--proceed"]
        with open(log_path, "w", encoding="utf-8") as log:
            try:
                # Explicit env, and cwd pinned to the repo: the app resolves `.env`
                # from the tree it runs out of, so leaving either to the launching
                # shell is what made V5 unauthenticated.
                completed = subprocess.run(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=REPO_ROOT,
                    env=env,
                    timeout=args.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.write("\n[sweep] TIMEOUT\n")
                failures.append((name, "timeout"))
                print(f"TIMEOUT after {args.timeout}s")
                continue
        elapsed = time.time() - started
        if completed.returncode != ACTION_REQUIRED:
            error = f"child exit {completed.returncode}"
            failures.append((name, error))
            print(f"FAILED ({error})")
            continue
        try:
            readiness_error = _snapshot_readiness_error(run_dir)
            if readiness_error:
                raise RuntimeError(readiness_error)
            fp = fingerprint(run_dir)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"NO FINGERPRINT ({exc})")
            continue
        fp["seconds"] = round(elapsed, 1)
        papers_fp[name] = fp
        print(f"{fp['refs']} refs · fulltext {fp['fulltext']} · abstract {fp['abstract_only']} "
              f"· niente {fp['neither']} · {elapsed:.0f}s")

    out_db = os.path.join(args.out, "fetch_fingerprint.sqlite")
    # A --only run describes the papers it swept, not the corpus: merge into any
    # fingerprint already there instead of replacing the other papers with
    # nothing.  Re-reads them from their run databases so the file stays a true
    # picture of the folder.
    for name in sorted(os.listdir(args.out)):
        if name in papers_fp or not os.path.isfile(os.path.join(args.out, name, "run.sqlite")):
            continue
        try:
            run_dir = os.path.join(args.out, name)
            if _snapshot_readiness_error(run_dir):
                continue
            papers_fp[name] = fingerprint(run_dir)
        except Exception:  # noqa: BLE001 - an unreadable neighbour must not stop the write
            continue
    write_fingerprint(out_db, accuracy=args.accuracy, papers=papers_fp)

    total = {k: sum(p[k] for p in papers_fp.values())
             for k in ("refs", "fulltext", "abstract_only", "neither")}
    print(f"\n{total['refs']} refs · fulltext {total['fulltext']} · "
          f"abstract {total['abstract_only']} · niente {total['neither']}")
    print(f"fingerprint -> {out_db}")

    diag = {"core/no-key": 0, "openalex/429": 0, "core/stored": 0}
    for name in papers_fp:
        for key, value in environment_diagnostics(os.path.join(args.out, name)).items():
            diag[key] += value
    print(f"environment · core/no-key={diag['core/no-key']} "
          f"openalex/429={diag['openalex/429']} full text via CORE={diag['core/stored']}")
    if diag["core/no-key"]:
        print("  WARNING: CORE ran unauthenticated for part of this sweep. "
              "These totals are NOT comparable to a credentialed sweep.")
    if failures:
        print(f"\nfailed: {len(failures)}")
        for name, err in failures:
            print(f"  {name}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
