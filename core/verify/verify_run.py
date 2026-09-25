#!/usr/bin/env python3
# core/verify/verify_run.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
verify_run.py — the COMPLETION GATE. Makes "I'm done" a checkable claim, not prose.

The pipeline's whole value is that every (claim, source) pair gets a guarded terminal
written to the typed run ledger. An agent that freelances — reads a few sources, writes
a nice report by hand, and declares success — produces none of those artifacts. This gate
detects exactly that and FAILS loudly, so a skipped verification can never pass as done.

What it enforces (each a hard FAIL unless noted):
  1. a parsed run payload exists and has claims/references.
  2. report.md exists AND carries a provenance signature that matches the one recomputed
     from the parsed payload + deterministic ledgers/artifacts. If report.journal.md exists, its
     latest append-only snapshot must match report.md and its history hash must chain
     correctly (catches a hand-written / stale / rewritten report).
  3. COVERAGE: every (claim, ref) pair that HAS retrievable source text has an
     operational terminal in the typed verification ledger. A pair with text but no terminal = a silently
     skipped verification (the Haiku failure mode) -> FAIL.
  4. Every crediting evidence outcome has grounded representative evidence; the
     typed projection rejects contradictory or incomplete terminal state.
  5. Pairs with NO text are reported as 'no_text' (not a failure: honestly uncheckable),
     but they are listed so they cannot hide.

Exit code 0 = gate passed; 20 = gate failed. Prints a JSON verdict either way.

Usage:
  python run.py verify --run runs/<ts>
  python run.py verify --run runs/<ts> --strict-crediting  # require >=1 crediting result
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from core.invocation import format_run_examples, run_command

try:
    from core.resolve import sources as _sources
    import core.report as _report
    from core.infra.integrity import signing as _signing
    from core.infra.db import RunRepository
    from core.report.verification_projection import (
        project_verification_pairs,
        select_verification_pair_rows,
    )
    from core.report.rollup import latest_terminal_states
    from core.report.source_availability import usable_verification_source_refs
except ImportError:  # direct execution
    import sources as _sources
    import report as _report
    import signing as _signing
    from db import RunRepository
    from verification_projection import project_verification_pairs, select_verification_pair_rows
    from rollup import latest_terminal_states
    from source_availability import usable_verification_source_refs

_PROV_RE = re.compile(
    r"citation-verifier-provenance alg=(\S+) sig=([0-9a-f]{64}) content=([0-9a-f]{64})")
def _terminal_uncertain_by_cause(pair_states):
    """Classify terminal uncertain pairs using the authoritative pair state.
    """
    classified = {}
    for state in latest_terminal_states(pair_states).values():
        if state.get("status") != "uncertain":
            continue
        cause = str(state.get("terminal_cause") or "unknown")
        classified.setdefault(cause, set())
        classified[cause].add((state.get("claim_id"), state.get("ref_id")))

    return classified


def _repo_open(run_dir):
    try:
        return RunRepository.open_readonly(run_dir)
    except RuntimeError as exc:
        if str(exc).startswith("no sqlite run database found"):
            return None
        raise


def _report_summary(run_dir):
    _md, summary = _report.render(run_dir)
    return summary if isinstance(summary, dict) else {}


def _load_run_projection(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        raw = repo.verification_raw_payloads()
        projection = {
            "parse": repo.effective_parse_payload(),
            "manifest": repo.source_manifest_payload(),
            "unreadable": repo.unreadable_payload(),
            "pair_states": repo.verification_pair_state_payloads(),
            "verification_raw": raw,
        }
        projection["verification_projection"] = project_verification_pairs(
            raw["pair_states"], raw["candidates"], raw["candidate_events"]
        )
        return projection
    finally:
        repo.close()


def verify(run_dir, *, strict_crediting=False, require_signature=False,
           _projection=None, _report_summary_val=None):
    """Run the completion gate.

    ``_projection``/``_report_summary_val`` are private, keyword-only
    escape hatches (leading underscore = not part of the stable API) that
    let :func:`signature_status` pass in an already-loaded run projection /
    report summary instead of recomputing them — it calls ``verify()``
    twice (once per ``require_signature`` value) and the two calls would
    otherwise redo the same DB reads and report-summary lookup (finding
    #14). Every other caller omits them and gets the original behavior.
    """
    failures = []   # hard problems -> gate fails
    warnings = []   # surfaced, not fatal
    info = {}

    try:
        projection = (
            _projection
            if _projection is not None
            else _load_run_projection(run_dir)
        )
    except RuntimeError as exc:
        return {
            "ok": False,
            "failures": [f"run database validation failed: {exc}"],
            "warnings": [],
            "info": {},
        }
    parse = projection["parse"]
    if not parse:
        return {"ok": False, "failures": ["parse payload missing — pipeline never ran"],
                "warnings": [], "info": {}}
    claims = parse.get("claims", [])
    refs = {r["id"]: r for r in parse.get("references", [])}
    if not claims:
        failures.append("parse payload has zero claims — nothing was parsed")

    # Citations -> the (claim, ref) pairs that SHOULD be verified. Derive these the SAME
    # way the driver does (core.app.phases.verify._verify_pairs) so the gate's
    # coverage set cannot diverge from what was actually emitted for verification.
    expected_pairs = {
        (ci["claim_id"], ci["ref_id"])
        for ci in parse.get("citations", [])
        # Link-silent resolved markers deliberately have no claim.  They are
        # Parse coverage/provenance, not claim-to-source work for Verify.
        if not (
            ci.get("claim_id") is None
            and ci.get("provenance") == "link_silent_resolved"
        )
        and ci.get("ref_id")
    }
    info["expected_pairs"] = len(expected_pairs)

    # Which refs have retrievable source text (any tier)?
    manifest = projection["manifest"]
    refs_with_text, unavailable_sources = usable_verification_source_refs(run_dir, manifest)
    if unavailable_sources:
        failures.append(
            "persisted source integrity failure: " + "; ".join(unavailable_sources)
        )
    # Unreadable scans pending OCR: text located but NOT readable -> not "skipped".
    unreadable_refs = {e["ref_id"]
                       for e in projection["unreadable"].get("entries", [])
                       if e.get("ref_id") and e.get("ocr_status") != "done"}

    raw_verify = projection.get("verification_raw") or {}
    verification_rows = select_verification_pair_rows(
        projection.get("verification_projection") or []
    )
    operational_pairs = {(row["claim_id"], row["ref_id"]) for row in verification_rows
                         if row["operational_complete"]}
    crediting_projection_pairs = {
        (row["claim_id"], row["ref_id"])
        for row in verification_rows if row["crediting"]
    }
    accepted_lifecycle_pairs = {
        (state.get("claim_id"), state.get("ref_id"))
        for state in raw_verify.get("pair_states", ())
        if state.get("status") == "accepted"
    }
    strict_crediting_pairs = crediting_projection_pairs & accepted_lifecycle_pairs

    # (3) Coverage: a pair WITH text but no typed terminal = silently skipped.
    skipped = sorted(
        p for p in expected_pairs
        if p[1] in refs_with_text and p[1] not in unreadable_refs
        and p not in operational_pairs)
    no_text = sorted(
        p for p in expected_pairs
        if p[1] not in refs_with_text and p[1] not in unreadable_refs)
    pending_ocr = sorted(p for p in expected_pairs if p[1] in unreadable_refs)
    info["pairs_with_text_verified"] = len(
        {p for p in expected_pairs if p[1] in refs_with_text}
        & operational_pairs
    )
    info["pairs_skipped"] = len(skipped)
    info["pairs_no_text"] = len(no_text)
    info["pairs_pending_ocr"] = len(pending_ocr)

    def _label(p):
        rn = refs.get(p[1], {}).get("ref_number", "?")
        return f"{p[0]}·[{rn}]"

    if skipped:
        failures.append(
            f"{len(skipped)} (claim,source) pair(s) HAVE source text but NO typed "
            f"verification terminal — verification was skipped: "
            + ", ".join(_label(p) for p in skipped[:20])
            + (" …" if len(skipped) > 20 else ""))
    if no_text:
        warnings.append(
            f"{len(no_text)} pair(s) have no retrievable text (honestly uncheckable): "
            + ", ".join(_label(p) for p in no_text[:20])
            + (" …" if len(no_text) > 20 else ""))
    if pending_ocr:
        warnings.append(f"{len(pending_ocr)} pair(s) pending OCR (scanned, opt-in).")

    # Terminal uncertain outcomes are classified by their persisted cause.  A
    # cause-specific warning keeps soft disagreement, contested negatives,
    # attribution, identity, and isolated evidence distinct.
    uncertain_by_cause = _terminal_uncertain_by_cause(projection.get("pair_states"))
    uncertain_pairs = sorted({
        pair for pairs in uncertain_by_cause.values() for pair in pairs
    })
    uncertain_counts = {
        cause: len(pairs) for cause, pairs in uncertain_by_cause.items()
    }
    info["uncertain_by_terminal_cause"] = uncertain_counts
    info["pairs_terminal_uncertain"] = len(uncertain_pairs)
    for cause, pairs in uncertain_by_cause.items():
        if not pairs:
            continue
        labels = sorted(_label(pair) for pair in pairs)
        if cause == "isolated_uncertain":
            detail = "isolated-evidence judge"
        else:
            detail = f"terminal cause `{cause}`"
        warnings.append(
            f"{len(pairs)} pair(s) ended uncertain ({detail}) — accepted support "
            "not established: " + ", ".join(labels[:20])
            + (" …" if len(labels) > 20 else ""))

    # Quality is intentionally distinct from the completion gate.  A run can
    # be complete (every pair was attempted and the report is authentic) while
    # still retaining an isolated-evidence uncertainty.  Consumers that need
    # a decision-quality result can inspect these explicit dimensions instead
    # of inferring reliability from an exit code.
    expected_with_text = {
        p for p in expected_pairs
        if p[1] in refs_with_text and p[1] not in unreadable_refs
    }
    evidence_matched_pairs = {
        (row["claim_id"], row["ref_id"]) for row in verification_rows
        if row["evidence_bearing"] and row["evidence"]
    }
    projected_by_pair = {(row["claim_id"], row["ref_id"]): row for row in verification_rows}
    evidence_required_pairs = {
        pair for pair in expected_with_text
        if pair not in projected_by_pair or projected_by_pair[pair]["evidence_bearing"]
    }
    semantic_decision_complete = all(
        pair in projected_by_pair
        and projected_by_pair[pair]["result_class"] != "unresolved"
        for pair in expected_pairs
    )
    terminal_causes_for_quality = _report.latest_terminal_causes(
        projection.get("pair_states"),
    )
    terminal_pairs_for_quality = _report.terminal_pair_counts(
        expected_pairs,
        projection.get("pair_states") or (),
        terminal_causes_for_quality,
        usable_text_refs=refs_with_text,
        unreadable_ref_ids=unreadable_refs,
    )
    health_dimensions = _report.terminal_health_dimensions(
        terminal_pairs_for_quality,
        terminal_causes_for_quality,
        operational_pairs,
        _report.terminal_attempt_causes_by_pair(raw_verify),
    )
    health_eligible = len(health_dimensions["eligible_pairs"])
    mechanical_pairs = health_dimensions["mechanical_pairs"]
    semantic_pairs = health_dimensions["semantic_pairs"]
    deterministic_guard_pairs = health_dimensions["deterministic_guard_pairs"]
    unclassified_pairs = health_dimensions["unclassified_pairs"]
    mechanical_rate = (
        len(mechanical_pairs) / health_eligible if health_eligible else 0.0
    )
    semantic_rate = (
        len(semantic_pairs) / health_eligible if health_eligible else 0.0
    )
    quality = {
        # Assigned after the remaining integrity checks below.
        "pipeline_complete": False,
        "protocol_valid": (
            not mechanical_pairs
            and not unclassified_pairs
        ),
        "mechanical_protocol_failure_pairs": len(mechanical_pairs),
        "mechanical_protocol_failure_rate": round(mechanical_rate, 3),
        "mechanical_protocol_eligible_pairs": health_eligible,
        "semantic_uncertainty_pairs": len(semantic_pairs),
        "semantic_uncertainty_rate": round(semantic_rate, 3),
        "deterministic_guard_pairs": len(deterministic_guard_pairs),
        "unclassified_terminal_pairs": len(unclassified_pairs),
        "evidence_matching_complete": evidence_required_pairs <= evidence_matched_pairs,
        # This says every pair received a terminal semantic *decision*.  It
        # does not claim the decisions are gold-validated; that requires a
        # separate labelled benchmark/human audit.
        "semantic_decision_complete": not uncertain_pairs and semantic_decision_complete,
        "semantic_verified_against_gold": None,
    }
    quality["run_reliable"] = (
        quality["protocol_valid"]
        and quality["evidence_matching_complete"]
        and quality["semantic_decision_complete"]
    )
    info["quality"] = quality
    info["evidence_matched_pairs"] = len(expected_with_text & evidence_matched_pairs)
    info["mechanical_protocol_failure_pairs"] = len(mechanical_pairs)
    info["semantic_uncertainty_pairs"] = len(semantic_pairs)
    info["deterministic_guard_pairs"] = len(deterministic_guard_pairs)
    info["operational_terminal_pairs"] = len(operational_pairs)

    # (2) Report provenance: authentic projection vs hand-written prose.
    report_md_path = os.path.join(run_dir, "report.md")
    report_journal_path = _report.journal_path(run_dir)
    if not os.path.exists(report_md_path):
        failures.append("report.md missing — run core.report.")
    else:
        with open(report_md_path, encoding="utf-8") as f:
            report_text = f.read()
        m = _PROV_RE.search(report_text)
        if not m:
            failures.append(
                "report.md has NO provenance seal — it was not produced by core.report "
                f"(likely hand-written). Regenerate with: {run_command('report', '--run', '<run>')}.")
        else:
            alg, sig, content = m.group(1), m.group(2), m.group(3)
            summary = (_report_summary_val if _report_summary_val is not None
                       else _report_summary(run_dir))
            # Recompute over the SAME payload core.report signed: input fields + a hash of
            # the report body (seal stripped). Catches both stale inputs and edited prose.
            body_sha256 = hashlib.sha256(
                _report.strip_seal(report_text).encode("utf-8")).hexdigest()
            payload = _report.seal_payload(
                _report.provenance_fields(run_dir, summary), body_sha256)
            expected_content = hashlib.sha256(payload).hexdigest()
            info["report_seal_alg"] = alg
            # Content binding (no key needed): catches a stale or hand-edited report.
            if content != expected_content:
                failures.append(
                    "report.md content seal does NOT match the DB-backed deterministic "
                    "artifacts "
                    "(stale or hand-edited report). Regenerate with core.report.")
            if os.path.exists(report_journal_path):
                with open(report_journal_path, encoding="utf-8") as f:
                    journal_text = f.read()
                latest_entry = _report.latest_report_entry(journal_text)
                if not latest_entry:
                    failures.append(
                        "report.journal.md exists but has no valid journal entries.")
                    info["report_journal_mode"] = "append-only"
                    info["report_history_chained"] = False
                else:
                    expected_history = latest_entry["meta"].get("history_sha256")
                    actual_history = (hashlib.sha256(
                        journal_text[:latest_entry["start"]].encode("utf-8")).hexdigest()
                        if latest_entry["start"] else "none")
                    info["report_journal_mode"] = "append-only"
                    info["report_history_chained"] = True
                    info["report_created_at"] = latest_entry["meta"].get("created_at")
                    if (_report.latest_snapshot_text(journal_text).rstrip("\n")
                            != report_text.rstrip("\n")):
                        failures.append(
                            "report.md does NOT match the latest snapshot stored in "
                            "report.journal.md.")
                    if (expected_history or "none") != actual_history:
                        failures.append(
                            "report.journal.md append-only history hash does NOT match the "
                            "earlier journal prefix (prior report text was rewritten or deleted).")
            else:
                info["report_journal_mode"] = "none"
                info["report_history_chained"] = False
            # Signature: the deterministic system's HMAC. Enforced ONLY with
            # --require-signature (CI / the hook's post-sign confirmation), never
            # implicitly — so the trusted context can run a coverage-only pre-check
            # BEFORE it applies the seal (avoids a chicken-and-egg at signing time).
            info["signing_key_present"] = _signing.key_present()
            if require_signature:
                if alg != "hmac-sha256":
                    failures.append(
                        "report carries only a weak sha256 seal, not the HMAC of the "
                        "deterministic system — it was not signed by the trusted signer. "
                        "Re-run core.report in the signing context.")
                elif not _signing.verify(payload, alg, sig):
                    failures.append(
                        "report HMAC signature is INVALID — not produced with the signing "
                        "key (forged or tampered). The run must restart.")

    # Strict delivery requires at least one crediting typed result overall.
    if strict_crediting and not strict_crediting_pairs:
        failures.append("no crediting verification result in the entire run (strict mode).")

    info["verification_terminals"] = len(operational_pairs)
    info["accepted_pairs"] = len(accepted_lifecycle_pairs)
    quality["pipeline_complete"] = not failures
    quality["run_reliable"] = bool(
        quality["pipeline_complete"]
        and quality["protocol_valid"]
        and quality["evidence_matching_complete"]
        and quality["semantic_decision_complete"]
    )

    return {"ok": not failures, "failures": failures,
            "warnings": warnings, "info": info}


def signature_status(run_dir, *, strict_crediting=False):
    """Return a small user-facing certificate for report provenance.

    The operational completion gate proves the report matches the parsed payload + ledger
    + report body. The HMAC gate proves the seal was produced by the trusted signer, but
    only when the verifier is running in a context that can read the signing key.
    """
    # Load the current projection and rendered summary once for both gate checks.
    _projection = _load_run_projection(run_dir)
    _summary = _report_summary(run_dir)
    base = verify(run_dir, strict_crediting=strict_crediting, require_signature=False,
                  _projection=_projection, _report_summary_val=_summary)
    hmac_check = verify(run_dir, strict_crediting=strict_crediting, require_signature=True,
                        _projection=_projection, _report_summary_val=_summary)
    info = base.get("info", {})
    alg = info.get("report_seal_alg")
    key_present = bool(info.get("signing_key_present"))
    strong = bool(base.get("ok") and hmac_check.get("ok") and alg == "hmac-sha256")
    if strong:
        verdict = "SIGNED_OK"
        message = ("Report verified: operational completion gate passed and the HMAC "
                   "signature is valid in this verifier context.")
    elif base.get("ok") and alg == "sha256":
        verdict = "CONTENT_SEAL_ONLY"
        message = ("Report content seal is valid, but it is not HMAC-signed. This proves "
                   "the report matches local artifacts, not that a trusted signer approved it.")
    elif base.get("ok") and alg == "hmac-sha256" and not key_present:
        verdict = "HMAC_KEY_UNAVAILABLE"
        message = ("Report carries an HMAC seal, but this verifier cannot read the signing "
                   "key, so it cannot confirm the trusted signature here.")
    elif base.get("ok"):
        verdict = "NOT_STRONGLY_SIGNED"
        message = ("Report passed the operational completion gate, but it did not pass the "
                   "trusted HMAC signature check.")
    else:
        verdict = "INVALID"
        message = ("Report is not deliverable: the operational completion gate failed. See "
                   "base_gate.failures.")
    return {
        "schema": "citation-verifier.signature-status.v1",
        "verdict": verdict,
        "signed_correctly": strong,
        "base_gate_ok": bool(base.get("ok")),
        "hmac_signature_ok": bool(hmac_check.get("ok")),
        "report_seal_alg": alg,
        "signing_key_present": key_present,
        "message": message,
        "base_gate": base,
        "hmac_gate": hmac_check,
    }


def write_signature_status(run_dir, *, strict_crediting=False):
    """Write opt-in signature and reliability status next to report.md."""
    status = signature_status(run_dir, strict_crediting=strict_crediting)
    md_path = os.path.join(run_dir, "report.signature_status.md")
    quality = (
        ((status.get("base_gate") or {}).get("info") or {}).get("quality")
        or {}
    )
    lines = [
        "# Citation Verifier Signature Status",
        "",
        f"Verdict: **{status['verdict']}**",
        f"Signed correctly: **{'yes' if status['signed_correctly'] else 'no'}**",
        f"Operational completion gate passed: **{'yes' if status['base_gate_ok'] else 'no'}**",
        f"HMAC signature valid: **{'yes' if status['hmac_signature_ok'] else 'no'}**",
        f"Seal algorithm: `{status.get('report_seal_alg') or 'none'}`",
        f"Signing key visible to verifier: **{'yes' if status['signing_key_present'] else 'no'}**",
    ]
    if quality:
        lines.extend([
            f"Run reliable: **{'yes' if quality.get('run_reliable') else 'no'}**",
            f"Pipeline complete: **{'yes' if quality.get('pipeline_complete') else 'no'}**",
            f"Protocol valid: **{'yes' if quality.get('protocol_valid') else 'no'}**",
            "Evidence matching complete: "
            f"**{'yes' if quality.get('evidence_matching_complete') else 'no'}**",
            "Semantic decision complete: "
            f"**{'yes' if quality.get('semantic_decision_complete') else 'no'}**",
        ])
    lines.extend([
        "",
        status["message"],
        "",
        "This file is generated by `core.verify.verify_run`, not by the LLM. Re-run:",
        "",
        "```",
        run_command("verify", "--run", os.path.abspath(run_dir), "--write-status"),
        "```",
        "",
    ])
    if status["base_gate"].get("failures"):
        lines.append("## Failures")
        lines.extend(f"- {x}" for x in status["base_gate"]["failures"])
        lines.append("")
    if status["base_gate"].get("warnings"):
        lines.append("## Warnings")
        lines.extend(f"- {x}" for x in status["base_gate"]["warnings"])
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return status


def main():
    ap = argparse.ArgumentParser(description=format_run_examples(__doc__),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory (runs/<ts>)")
    ap.add_argument("--strict-crediting", action="store_true",
                    help="also fail if zero typed verification results are crediting")
    ap.add_argument("--require-signature", action="store_true",
                    help="require a valid HMAC seal (the deterministic system's "
                         "signature); use in CI / external audit")
    ap.add_argument("--write-status", action="store_true",
                    help="write report.signature_status.md")
    args = ap.parse_args()

    result = verify(args.run, strict_crediting=args.strict_crediting,
                    require_signature=args.require_signature)
    if args.write_status:
        write_signature_status(args.run, strict_crediting=args.strict_crediting)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        sys.stderr.write("\nGATE FAILED — the run is INCOMPLETE or the report is not "
                         "authentic. Do NOT deliver this report.\n")
        sys.exit(20)
    sys.exit(0)


if __name__ == "__main__":
    main()
