# core/app/runtime/tasks.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Task lifecycle helpers for paused and resumable pipeline phases."""

from __future__ import annotations

import os
import traceback

from core.app.runtime.repository import (
    RUNTIME_SETTING_KEYS,
    _repo_mark_phase,
    _repo_open,
    _runtime_state_from_repo,
)
from core.app.runtime.settings import ACTION_REQUIRED, _progress
from core.fetch.diagnostics import fetch_audit as _fetch_audit
from core.verify import verify_run
from core.invocation import run_command

STATUS_SLOTS = ("fetch", "research", "verify", "parse_review")


def _task_ingest_error(exc, *, stage):
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def _reopen_task_with_error(run_dir, slot, handle, task, *, stage, exc):
    task["status"] = "pending"
    task["last_error"] = _task_ingest_error(exc, stage=stage)
    _update_task(run_dir, slot, handle, task, status="pending")


def _update_task(run_dir, slot, handle, task, *, status=None):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        task_id = handle
        record = repo.get_task(task_id)
        if record is None or record.slot != slot:
            raise ValueError("task update does not match its persisted slot")
        # `_answered_tasks()` returns a presentation view enriched with task
        # identity, database lifecycle timestamps and the latest answer.  Those
        # fields are projections over `tasks`/`task_answers`, not task payload
        # facts, and must never be written back into the authoritative payload.
        payload = dict(record.task_payload or {})
        if status == "pending":
            payload["status"] = "pending"
            if "last_error" in task:
                payload["last_error"] = task["last_error"]
            else:
                payload.pop("last_error", None)
            repo.reopen_task(task_id, task_payload=payload)
        elif status == "applied":
            payload["status"] = "done"
            payload.pop("last_error", None)
            repo.apply_task(task_id, task_payload=payload)
        else:
            raise ValueError("task update status is required")
    finally:
        repo.close()


def _task_store_mode(run_dir):
    repo = _repo_open(run_dir)
    if repo is None:
        return "missing"
    try:
        return "db"
    finally:
        repo.close()


def _create_task(run_dir, slot, task_id, task):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        repo.create_task(
            task_id=task_id,
            slot=slot,
            ref_id=task.get("ref_id"),
            claim_id=task.get("claim_id"),
            scope=task.get("scope"),
            task_payload=task,
        )
    finally:
        repo.close()
    return task_id


def _submit_task_answer(run_dir, slot, handle, answer, *, actor_type="llm"):
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        repo.submit_task_answer(
            task_id=handle,
            actor_type=actor_type,
            raw_payload=answer,
            accepted_for_processing=True,
        )
    finally:
        repo.close()


def _pending_tasks(run_dir, slot):
    """Open tasks that still belong to the current run state."""
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        return [
            (task["task_id"], task)
            for task in repo.list_task_views(slot=slot, statuses=("pending", "answered"))
        ]
    finally:
        repo.close()


def _answered_tasks(run_dir, slot):
    """Open tasks whose answer has been submitted and is waiting to be applied."""
    repo = _repo_open(run_dir)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run_dir}")
    try:
        return [
            (task["task_id"], task)
            for task in repo.list_task_views(slot=slot, statuses=("answered",))
            if task.get("answer") is not None
        ]
    finally:
        repo.close()


def status_snapshot(run_dir):
    """A user-facing snapshot of where a run stands right now."""
    repo = _repo_open(run_dir)
    if repo is not None:
        try:
            snap = repo.status_snapshot()
            settings = repo.list_run_settings()
        finally:
            repo.close()
        status = None
        if os.path.exists(os.path.join(run_dir, "report.signature_status.md")):
            status = verify_run.signature_status(run_dir)
        gate = (status or {}).get("base_gate")
        fetch_summary = _fetch_audit.build_summary(run_dir)
        snap.update({
            "fetch_paused": bool(settings.get("fetch_paused")),
            "autonomous": bool(settings.get("autonomous")),
            "gate_present": gate is not None,
            "gate_ok": None if gate is None else bool(gate.get("ok")),
            "signature_status_present": status is not None,
            "signed_correctly": None if status is None else bool(status.get("signed_correctly")),
            "signature_verdict": None if status is None else status.get("verdict"),
            "fetch_attempt_summary": {
                "references_with_attempts": fetch_summary.get("references_with_attempts", 0),
                "attempt_rows_total": fetch_summary.get("attempt_rows_total", 0),
                "challenge_blocked_references": fetch_summary.get("challenge_blocked_references", 0),
                "paywalled_references": fetch_summary.get("paywalled_references", 0),
                "outcomes": fetch_summary.get("outcomes") or {},
                "latest_outcomes": fetch_summary.get("latest_outcomes") or {},
            },
        })
        return snap

    raise SystemExit(f"no sqlite run database found in {run_dir}")


def print_status(snapshot):
    state = "DONE" if snapshot["done"] else "IN PROGRESS"
    print(f"RUN STATUS: {state}")
    print(f"RUN DIR: {snapshot['run_dir']}")
    print(f"CURRENT PHASE: {snapshot['phase']}")
    print(f"CREATED AT: {snapshot.get('created_at') or 'unknown'}")
    print(f"PENDING TASKS: {snapshot['pending_tasks_total']}")
    print(f"ANSWERED TASKS WAITING RESUME: {snapshot['answered_tasks_waiting_resume_total']}")
    if snapshot["pending_tasks_by_slot"]:
        slots = ", ".join(f"{slot}={n}" for slot, n in snapshot["pending_tasks_by_slot"].items())
        print(f"PENDING BY SLOT: {slots}")
    if snapshot["answered_tasks_waiting_resume_by_slot"]:
        slots = ", ".join(f"{slot}={n}" for slot, n in
                          snapshot["answered_tasks_waiting_resume_by_slot"].items())
        print(f"ANSWERED BY SLOT: {slots}")
    print(f"REPORT PRESENT: {'yes' if snapshot['report_present'] else 'no'}")
    print(f"REPORT JOURNAL PRESENT: {'yes' if snapshot['report_journal_present'] else 'no'}")
    gate_ok = snapshot["gate_ok"]
    print(f"GATE: {'ok' if gate_ok else 'failed' if gate_ok is False else 'not run'}")
    sig = snapshot.get("signature_verdict") or "not written"
    print(f"SIGNATURE STATUS: {sig}")
    fetch_summary = snapshot.get("fetch_attempt_summary") or {}
    if fetch_summary.get("attempt_rows_total"):
        print(
            "FETCH ATTEMPTS: "
            f"{fetch_summary.get('attempt_rows_total')} rows across "
            f"{fetch_summary.get('references_with_attempts')} reference(s)"
        )
        print(
            "FETCH BLOCKERS: "
            f"challenge_refs={fetch_summary.get('challenge_blocked_references', 0)}, "
            f"paywalled_refs={fetch_summary.get('paywalled_references', 0)}"
        )
    print(f"NEXT ACTION: {snapshot['next_action']}")
    if not snapshot["done"]:
        print(f"RESUME COMMAND: {snapshot['resume_command']}")


def _pause(slot, run_dir, n, what):
    st = _runtime_state_from_repo(run_dir) or {}
    _repo_mark_phase(
        run_dir,
        st.get("phase") or slot,
        session_id=st.get("db_session_id"),
        status="paused",
        event_type="pause",
        payload={"slot": slot, "pending_tasks": n},
    )
    if st.get("autonomous"):
        _progress(f"{slot}: {n} task(s) pending · re-batching")
        return ACTION_REQUIRED
    print("\n" + "=" * 72)
    print(f"CITATION-VERIFIER · ACTION REQUIRED · slot: {slot.upper()}")
    print("=" * 72)
    print(f"{n} task(s) need your answer for slot {slot.upper()}.")
    print(f"Inspect them with: {run_command('tasks', 'list', '--run', run_dir, '--slot', slot)}")
    if slot == "fetch":
        print(
            "Skip all ordinary pending FETCH tasks: "
            f"{run_command('tasks', 'skip-fetch', '--run', run_dir)}"
        )
    print(what)
    print("\nWhen every task answer is stored, run:")
    print(f"    {run_command('--run', run_dir, '--resume')}")
    print("=" * 72)
    return ACTION_REQUIRED
