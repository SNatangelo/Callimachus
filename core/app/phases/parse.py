#!/usr/bin/env python3
# core/app/phases/parse.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: parse — extract claims, references, and citations from the manuscript."""

import hashlib
import os
import sys

from core.parse import parse_manuscript
from core.parse import orphan_match
from core.style import detect as _style_detect
from core.app.runtime.settings import ACTION_REQUIRED
from core.invocation import run_command


def _review_task_id(review_kind, target_id):
    digest = hashlib.sha256(f"{review_kind}:{target_id}".encode("utf-8")).hexdigest()
    return f"parse-review-{digest[:24]}"


def _review_task_matches(repo, task_id, review_kind, *, ref_id=None, note_id=None):
    with repo._connection_lock:
        row = repo._conn.execute(
            "SELECT task_kind,ref_id,note_id FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
    return row is None or (
        row["task_kind"] == "manual_parse_review"
        and ((ref_id is not None and row["ref_id"] == ref_id)
             or (note_id is not None and row["note_id"] == note_id))
        and (repo.get_task(task_id).task_payload.get("review_kind") == review_kind)
    )


def _identity_review_targets(st, repo):
    selectors = st.get("manual_review_ref_numbers") or []
    by_number = {ref.ref_number: ref.ref_id for ref in repo.list_references()}
    missing = [number for number in selectors if number not in by_number]
    if missing:
        raise ValueError("manual Parse review reference number unavailable: " + ", ".join(map(str, missing)))
    return [(number, by_number[number]) for number in selectors]


def _emit_identity_review_tasks(st, repo):
    for number, ref_id in _identity_review_targets(st, repo):
        task_id = _review_task_id("reference_identity_review", ref_id)
        existing = repo.get_task(task_id)
        if existing is not None:
            if not _review_task_matches(
                repo, task_id, "reference_identity_review", ref_id=ref_id,
            ):
                raise ValueError("manual Parse review task identity collision")
            continue
        repo.create_manual_parse_review(
            task_id=task_id,
            review_kind="reference_identity_review",
            target_id=ref_id,
            instructions="Choose correct_identity with a title and/or DOI, or keep_ambiguous.",
        )


def _resume_manual_parse_review(st, *, _repo_open, _save_state, _progress):
    """Apply accepted Parse-review answers without replaying Parse facts."""
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {st['run_dir']}")
    try:
        try:
            _emit_manual_parse_review_tasks(st, repo, repo.parse_payload())
        except ValueError as exc:
            print(f"manual Parse review task creation failed: {exc}", file=sys.stderr)
            return "error"
        _save_state(st)
        reviews = [task for task in repo.list_tasks(slot="parse_review")
                   if task.task_kind == "manual_parse_review"]
        cancelled = [task.task_id for task in reviews if task.status == "cancelled"]
        if cancelled:
            print("manual Parse review cancelled: " + ", ".join(cancelled), file=sys.stderr)
            return "error"
        pending = [task.task_id for task in reviews if task.status == "pending"]
        if pending:
            _progress(f"manual Parse review: {len(pending)} task(s) still pending")
            return ACTION_REQUIRED
        for task in reviews:
            if task.status == "answered":
                try:
                    repo.apply_manual_parse_review(task.task_id)
                except (RuntimeError, ValueError) as exc:
                    print(f"manual Parse review application failed for {task.task_id}: {exc}", file=sys.stderr)
                    return "error"
            elif task.status != "applied":
                print(f"manual Parse review has invalid task status for {task.task_id}", file=sys.stderr)
                return "error"
        st["parse_review_paused"] = False
        _save_state(st)
        _progress("manual Parse review complete; continuing to Resolve")
        return "resolve"
    finally:
        repo.close()


def _emit_manual_parse_review_tasks(st, repo, parse):
    """Emit deterministic review tasks after immutable Parse facts are stored."""
    selected_ref_ids = {
        ref_id for _number, ref_id in _identity_review_targets(st, repo)
    }
    with repo._connection_lock:
        selected_note_ids = {
            row["note_id"]
            for row in repo._conn.execute(
                "SELECT note_id,ref_id FROM footnote_note_parents ORDER BY note_id"
            )
            if row["ref_id"] in selected_ref_ids
        }
    for note in parse.get("footnote_notes") or []:
        note_id = note.get("note_id")
        if (
            note.get("extraction_status") != "ambiguous"
            and note_id not in selected_note_ids
        ):
            continue
        if not isinstance(note_id, str) or not note_id:
            raise ValueError("ambiguous footnote has no stable note identity")
        task_id = _review_task_id("footnote_source_review", note_id)
        existing = repo.get_task(task_id)
        if existing is not None:
            if not _review_task_matches(
                repo, task_id, "footnote_source_review", note_id=note_id,
            ):
                raise ValueError("manual Parse review task identity collision")
            continue
        repo.create_manual_parse_review(
            task_id=task_id,
            review_kind="footnote_source_review",
            target_id=note_id,
            instructions="Choose no_sources, split_sources with exact source text, or keep_ambiguous.",
        )
    _emit_identity_review_tasks(st, repo)
    # Persisted raw candidates are facts; fuzzy rankings are only closed suggestions.
    unresolved_targets = list(parse.get("citations") or []) + list(parse.get("ambiguities") or []) + [
        item for item in (parse.get("_debug") or {}).get("orphans", [])
        if item.get("occurrence_id") not in {x.get("occurrence_id") for x in (parse.get("citations") or [])}
    ]
    for citation in unresolved_targets:
        occurrence_id = citation.get("occurrence_id")
        if not occurrence_id or citation.get("ref_id") is not None:
            continue
        raw_candidates = citation.get("candidate_ref_ids") or [
            ref.ref_id for item in citation.get("candidates", []) for ref in repo.list_references()
            if item.get("ref_number") == ref.ref_number and ref.raw_entry[:160] == item.get("raw_entry")
        ]
        candidates = [{"id": ref_id, "origin": "parser", "score": 1.0} for ref_id in raw_candidates]
        if not candidates:
            orphan = next((x for x in (parse.get("_debug") or {}).get("orphans", []) if x.get("occurrence_id") == occurrence_id or x.get("marker_raw") == citation.get("marker_raw")), citation)
            candidates = [{"id": ref.get("id") or ref.get("ref_id"), "origin": "orphan_match", "score": score} for score, ref in orphan_match.rank_candidates(orphan.get("surname"), orphan.get("year"), parse.get("references") or [])]
        if not candidates:
            continue
        task_id = _review_task_id("citation_reference_review", occurrence_id)
        existing = repo.get_task(task_id)
        if existing is not None and (existing.task_kind != "manual_parse_review" or existing.claim_id != citation.get("claim_id") or existing.scope != occurrence_id or existing.task_payload.get("review_kind") != "citation_reference_review"):
            raise ValueError("manual citation review task identity collision")
        if existing is None:
            repo.create_manual_parse_review(task_id=task_id, review_kind="citation_reference_review", target_id=occurrence_id, candidates=candidates, instructions="Choose select_reference from the listed candidates, or keep_unresolved.")
    for ref, claims in orphan_match.uncited_with_candidates(parse):
        ref_id = ref.get("id") or ref.get("ref_id")
        candidates = [{"id": claim.get("id") or claim.get("claim_id"), "origin": "orphan_match_inverse", "score": score} for score, claim in claims]
        if not ref_id or not candidates:
            continue
        task_id = _review_task_id("reference_claim_review", ref_id)
        existing = repo.get_task(task_id)
        if existing is not None and (existing.task_kind != "manual_parse_review" or existing.ref_id != ref_id or existing.task_payload.get("review_kind") != "reference_claim_review"):
            raise ValueError("manual inverse citation review task identity collision")
        if existing is None:
            repo.create_manual_parse_review(task_id=task_id, review_kind="reference_claim_review", target_id=ref_id, candidates=candidates, instructions="Choose select_claim from the listed candidates, or keep_unresolved.")
    return len(repo.list_tasks(slot="parse_review", status="pending"))


def _report_coverage(cov, table_only, table_verified, _progress):
    """Say out loud what the parse actually read.

    The orphan count cannot catch a silent parse failure — a citation we never
    detected can never become an orphan — so reference coverage is the one number
    that tells the operator whether the markers made it out of the file.  It is
    cheap to print and it is the difference between a run that verified the paper
    and a run that verified the 40% of it we managed to read."""
    if not cov.get("total"):
        return
    _progress(f"reference coverage: {cov['cited']}/{cov['total']} ({cov['pct']:.0f}%) "
              f"of the bibliography is cited in the manuscript "
              f"({cov.get('in_prose', 0)} in prose"
              + (f", {cov['in_tables']} only in a table" if cov.get("in_tables") else "")
              + ")")
    if cov.get("warning"):
        _progress(f"WARNING: {cov['warning']}")
    if table_only:
        nums = ", ".join(f"[{t.get('ref_number')}]" for t in table_only[:15])
        _progress(f"{len(table_only)} reference(s) cited ONLY inside a table — "
                  f"the source is resolved and fetched like any other, but the row "
                  f"makes no claim to check it against (a table row asserts nothing): "
                  f"{nums}{' …' if len(table_only) > 15 else ''}. "
                  f"Pass --verify-table-citations to turn those rows into claims.")
    elif table_verified:
        _progress("table citations are being verified (--verify-table-citations): "
                  "markers inside table rows produce claims like any other")
    further = cov.get("further_reading") or []
    if further:
        nums = ", ".join(f"[{n}]" for n in further)
        _progress(f"{len(further)} entry(ies) belong to a 'Further reading' list — "
                  f"works the authors recommend and never cite, so they are not "
                  f"counted as references left uncited: {nums} (they are still "
                  f"resolved and fetched)")
    uncited = cov.get("uncited") or []
    if uncited:
        shown = ", ".join(f"[{n}]" for n in uncited[:15])
        _progress(f"{len(uncited)} reference(s) cited NOWHERE — not in prose, not in a "
                  f"table: {shown}{' …' if len(uncited) > 15 else ''} "
                  f"(either a marker we failed to read, or one the authors never cited)")


def phase_parse(st, *, _progress, _configure_debug_mode, _repo_open, _save_state):
    if st.get("parse_review_paused"):
        return _resume_manual_parse_review(
            st, _repo_open=_repo_open, _save_state=_save_state, _progress=_progress,
        )
    _progress("parsing manuscript text (PDF extraction and reference detection)")
    run = st["run_dir"]
    # The run's choice outlives the process it was made in: a resume re-parses in a
    # shell that never saw the flag, so re-assert it from the state.
    if st.get("verify_table_citations"):
        os.environ[parse_manuscript.ENV_VERIFY_TABLE_CITATIONS] = "1"

    def _announce_ocr(_method_hint):
        _progress("manuscript appears to be a scanned PDF with no text layer — "
                  "running OCR automatically (this can take a while)")

    try:
        parse, debug_md = parse_manuscript.parse(
            st["input"], window=1,
            ocr_lang=st.get("ocr_lang") or "eng",
            ocr_notice=_announce_ocr)
    except parse_manuscript.CitationMarkersLost as e:
        # Not a parser failure: the markers were destroyed before the file reached us,
        # and no re-run of ours can bring them back.  Never advise a .txt here — that
        # is the very conversion that loses superscript citations.
        print(f"Refusing to verify a manuscript whose citations are missing.\n{e}",
              file=sys.stderr)
        return "error"
    except Exception as e:
        print("parse_manuscript failed. PDF best-effort: extract the text yourself and "
              "re-run with --input <file>. Prefer DOCX over plain text — a paper that "
              "cites by superscript loses its citation markers in .txt.\n"
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return "error"
    ms = parse.get("manuscript") or {}
    extract = (parse.get("_debug") or {}).get("extract") or {}
    if (ms.get("format") == "pdf"
            and extract.get("pdf_method") in {"stdlib", "fallback_stdlib"}
            and parse.get("claims")
            and not parse.get("references")):
        print("Refusing to verify PDF: the stdlib PDF extractor found citation claims but "
              "no bibliography references. Install a declared PDF backend (pymupdf or "
              "pdfminer.six) and rerun.", file=sys.stderr)
        return "error"
    if ms.get("ocr"):
        _progress(f"manuscript OCR complete ({ms.get('ocr_method')}); "
                  "verifying against machine-OCR'd text (recorded degradation)")
    _configure_debug_mode(st)
    with open(os.path.join(run, "parse_debug.md"), "w", encoding="utf-8") as f:
        f.write(debug_md)
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    cov = (parse.get("_debug") or {}).get("reference_coverage") or {}
    table_only = parse.get("table_only_citations") or []
    try:
        repo.replace_parse_payload(
            claims=parse.get("claims") or [],
            references=parse.get("references") or [],
            citations=parse.get("citations") or [],
            manuscript_text=ms.get("full_text"),
            manuscript_identity=ms.get("title_evidence"),
            table_only_citations=table_only,
            coverage=cov,
            parse_extract=(parse.get("_debug") or {}).get("extract") or {},
            footnote_notes=parse.get("footnote_notes") or [],
            footnote_note_sources=parse.get("footnote_note_sources") or [],
            claim_footnotes=parse.get("claim_footnotes") or [],
            footnote_note_parents=parse.get("footnote_note_parents") or [],
            ambiguities=parse.get("ambiguities") or [],
            orphan_diagnostics=(parse.get("_debug") or {}).get("orphans") or [],
        )
        repo.update_run_settings({
            "verify_table_citations": bool(parse.get("table_citations_verified")),
        })
    finally:
        repo.close()
    _report_coverage(cov, table_only, bool(parse.get("table_citations_verified")),
                     _progress)
    if not parse.get("claims"):
        print("No claims with citation markers found — nothing to verify.", file=sys.stderr)
        return "error"
    # Style auto-detection (advisory): used only if the user did not pass --style.
    if not st.get("style"):
        det = _style_detect.detect(
            parse.get("references", []),
            (parse.get("manuscript") or {}).get("citation_mode"),
        )
        st["style"] = det.get("suggested") or "vancouver"
        st["style_confidence"] = det.get("confidence")
        _save_state(st)
    if st.get("manual_review"):
        repo = _repo_open(run)
        if repo is None:
            raise SystemExit(f"no sqlite run database found in {run}")
        try:
            count = _emit_manual_parse_review_tasks(st, repo, repo.parse_payload())
        finally:
            repo.close()
        st["parse_review_paused"] = True
        _save_state(st)
        _progress(f"manual Parse review paused: {count} task(s) emitted")
        print("Manual Parse review required. Inspect with: "
              f"{run_command('tasks', 'list', '--run', st['run_dir'], '--slot', 'parse_review')}")
        return ACTION_REQUIRED
    ambiguous = [note for note in parse.get("footnote_notes") or []
                 if note.get("extraction_status") == "ambiguous"]
    if ambiguous:
        _progress("warning: ambiguous footnotes are excluded from Resolve/Fetch; "
                  "rerun with --manual-review to adjudicate them")
    return "resolve"
