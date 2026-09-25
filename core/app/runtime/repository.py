# core/app/runtime/repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run repository access and persistence helpers for application orchestration."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from core.app.runtime.settings import (
    DEFAULT_ACCURACY,
    DEFAULT_CHALLENGE_MODE,
    DEFAULT_OCR_LANG,
    _debug_directives_from_parse,
    _fetch_worker_count,
)
from core.infra.db import RunRepository
from core.fetch.transport.http_headers import DEFAULT_HTTP_PROFILE

RUNTIME_SETTING_KEYS = (
    "mailto",
    "max_retries",
    "fetch_workers",
    "no_fetch",
    "references_only",
    "verify_backends",
    "autonomous",
    "ocr_lang",
    "fetch_paused",
    "auto_fetch_attempted",
    "style_confidence",
    "style",
    "debug_mode",
    "debug_labels",
    "manual_review",
    "parse_review_paused",
    "manual_review_ref_numbers",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fixture_fingerprint() -> str:
    return "sqlite-bridge-v1"


def _repo_open(run_dir):
    # Absence is the sole non-exceptional result.  In particular, do not turn a
    # busy, corrupt, or incompatible existing database into an apparent fresh
    # run: callers must preserve that failure rather than bootstrap over it.
    if not os.path.exists(os.path.join(run_dir, "run.sqlite")):
        return None
    return RunRepository.open(run_dir)


def _repo_sync_parse(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        parse = _load_parse_payload(run_dir)
        repo.replace_parse_payload(
            claims=parse.get("claims") or [],
            references=parse.get("references") or [],
            citations=parse.get("citations") or [],
            footnote_notes=parse.get("footnote_notes") or [],
            footnote_note_sources=parse.get("footnote_note_sources") or [],
            claim_footnotes=parse.get("claim_footnotes") or [],
        )
    finally:
        repo.close()


def _runtime_state_from_repo(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        return None
    try:
        run = repo.get_run()
        settings = repo.list_run_settings()
    finally:
        repo.close()
    return {
        "run_dir": os.path.abspath(run_dir),
        "input": run.input_path,
        "accuracy": run.accuracy,
        "style": settings.get("style") or run.style,
        "model": run.model_id,
        "http_profile": run.http_profile or DEFAULT_HTTP_PROFILE,
        "challenge_mode": run.challenge_mode or DEFAULT_CHALLENGE_MODE,
        "phase": run.phase,
        "created_at": run.created_at,
        "run_status": run.status,
        "mailto": settings.get("mailto"),
        "max_retries": int(settings.get("max_retries") or 2),
        "fetch_workers": settings.get("fetch_workers"),
        "no_fetch": bool(settings.get("no_fetch")),
        "references_only": bool(settings.get("references_only")),
        "verify_backends": settings.get("verify_backends"),
        "autonomous": bool(settings.get("autonomous")),
        "ocr_lang": settings.get("ocr_lang") or DEFAULT_OCR_LANG,
        "fetch_paused": bool(settings.get("fetch_paused")),
        "auto_fetch_attempted": bool(settings.get("auto_fetch_attempted")),
        "style_confidence": settings.get("style_confidence"),
        # A frozen-fetch verification fork deliberately enables tracing even
        # when the ambient environment has no debug directive.  Carry the
        # persisted setting on resume instead of silently dropping it.
        "debug_mode": bool(settings.get("debug_mode")),
        "debug_labels": settings.get("debug_labels") or [],
        "manual_review": bool(settings.get("manual_review")),
        "parse_review_paused": bool(settings.get("parse_review_paused")),
        "manual_review_ref_numbers": settings.get("manual_review_ref_numbers") or [],
    }


def _repo_sync_resolve_result(
    run_dir, ref_id, payload, *, trace_state="preserve",
):
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        return repo.upsert_resolve_result(
            ref_id, payload, trace_state=trace_state,
        )
    finally:
        repo.close()


def _repo_record_resolution_trace(
    run_dir, ref_id, trace, *, trace_not_produced=False,
):
    repo = _repo_open(run_dir)
    if repo is None:
        return None
    try:
        if trace is None:
            if not trace_not_produced:
                raise ValueError(
                    "missing resolution trace requires explicit trace_not_produced"
                )
            repo.record_resolution_trace_not_produced(ref_id)
            return {"trace": None}
        return repo.record_resolution_trace(ref_id, trace)
    finally:
        repo.close()


def _repo_mark_phase(
    run_dir,
    phase,
    *,
    session_id=None,
    status=None,
    event_type=None,
    payload=None,
):
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        repo.mark_run_phase(
            phase,
            status=status,
            event_type=event_type,
            session_id=session_id,
            payload=payload,
        )
    finally:
        repo.close()


def _configure_debug_mode(st: dict) -> None:
    env_enabled, env_labels = _debug_directives_from_parse()
    enabled = bool(st.get("debug_mode")) or env_enabled
    labels = sorted({*(st.get("debug_labels") or []), *env_labels})
    st["debug_mode"] = enabled
    st["debug_labels"] = labels
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {st['run_dir']}")
    try:
        repo.update_run_settings({
            "debug_mode": enabled,
            "debug_labels": labels,
        })
    finally:
        repo.close()


def _load_parse_payload(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        return repo.effective_parse_payload()
    finally:
        repo.close()


def _load_resolve_map(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        return repo.resolve_payload_map()
    finally:
        repo.close()


def _fresh_start_seed(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"cannot fresh-start {run_dir}: no sqlite run database found")
    try:
        run = repo.get_run()
    finally:
        repo.close()
    state = _runtime_state_from_repo(run_dir) or {}
    input_path = state.get("input") or run.input_path
    if not input_path:
        raise SystemExit(f"cannot fresh-start {run_dir}: original input path is missing")
    return {
        "input": input_path,
        "accuracy": state.get("accuracy") or run.accuracy or DEFAULT_ACCURACY,
        "style": state.get("style") or run.style,
        "mailto": state.get("mailto"),
        "model": state.get("model") or run.model_id,
        "max_retries": state.get("max_retries", 2),
        "fetch_workers": state.get("fetch_workers", _fetch_worker_count()),
        "ocr_lang": state.get("ocr_lang") or DEFAULT_OCR_LANG,
        "http_profile": (
            state.get("http_profile")
            or run.http_profile
            or DEFAULT_HTTP_PROFILE
        ),
        "challenge_mode": (
            state.get("challenge_mode")
            or run.challenge_mode
            or DEFAULT_CHALLENGE_MODE
        ),
        # A fresh start preserves stable configuration, not the parent's
        # one-run execution choices.  Callers may still request these modes
        # explicitly on the new invocation.
        "no_fetch": False,
        "references_only": False,
        "autonomous": False,
        "parent_run_id": run.run_id,
        "parent_run_dir": os.path.abspath(run_dir),
        "run_origin": "restarted_from_interrupted",
    }


def _load_run_refs(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        return repo.effective_parse_payload().get("references", [])
    finally:
        repo.close()


def _run_ref_by_id(run_dir, ref_id):
    ref = next((r for r in _load_run_refs(run_dir) if r.get("id") == ref_id), None)
    if ref is None:
        raise ValueError(f"reference {ref_id} not found in parse payload")
    return ref


def _save_state(st):
    repo = _repo_open(st["run_dir"])
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {st['run_dir']}")
    try:
        repo.update_run_phase(st.get("phase") or repo.get_run().phase)
        repo.update_run_settings({
            key: st.get(key)
            for key in RUNTIME_SETTING_KEYS
            if key in st
        })
    finally:
        repo.close()
