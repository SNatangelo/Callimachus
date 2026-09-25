#!/usr/bin/env python3
"""Read-only projections used by the Callimachus desktop interface.

The desktop layer reads durable run and content-store state.  It does not
interpret terminal output and never opens either database writable.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.app.guided_fetch import build_source_inventory
from core.fetch.storage import content_store
from core.infra.db import RunRepository
from core.report.verification_projection import project_verification_pairs


def load_run_brief(run_dir: str) -> dict[str, Any]:
    """Return one run's lightweight durable state for Resume and report controls.

    This projection intentionally avoids parsing the source inventory and
    verification ledger. It reads only run metadata and the active-session
    heartbeat needed by the desktop controls.
    """
    absolute_run_dir = os.path.abspath(run_dir)
    repo = RunRepository.open_readonly(absolute_run_dir)
    try:
        run = repo.get_run()
        settings = repo.list_run_settings()
        action_required = bool(repo.list_pending_tasks())
        crash_recoverable = repo.detect_interrupted_run(stale_after_seconds=120)
    finally:
        repo.close()

    child = Path(absolute_run_dir)
    report_path = child / "report.md"
    journal_path = child / "report.journal.md"
    report_html_path = child / "report.html"
    if not report_html_path.is_file():
        preview_path = child / "report.preview.html"
        report_html_path = preview_path if preview_path.is_file() else None
    input_label = os.path.basename(run.input_path) or run.input_path
    return {
        "available": True,
        "run_dir": absolute_run_dir,
        "run_id": run.run_id,
        "input": {"label": input_label, "path": run.input_path},
        "paper": input_label,
        "accuracy": run.accuracy,
        "phase": run.phase,
        "run_status": run.status,
        "references_only": bool(settings.get("references_only")),
        "completed": run.status == "completed" and run.phase == "done",
        "action_required": action_required,
        "crash_recoverable": crash_recoverable,
        "report_present": report_path.is_file(),
        "report_journal_present": journal_path.is_file(),
        "report_html_present": report_html_path is not None,
        "report_html_path": (
            str(report_html_path) if report_html_path is not None else None
        ),
        "updated_at": run.updated_at or run.created_at,
    }


def discover_run_briefs(runs_root: str) -> list[dict[str, Any]]:
    """List child-run metadata without loading inventories or verification pairs."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    briefs = []
    for child in root.iterdir():
        if not child.is_dir() or not (child / "run.sqlite").is_file():
            continue
        try:
            briefs.append(load_run_brief(str(child)))
        except Exception as exc:
            briefs.append(_unavailable_run_row(child, exc))
    return sorted(briefs, key=_summary_order, reverse=True)


def load_latest_run_brief(runs_root: str) -> dict[str, Any] | None:
    """Load the newest readable run brief using its durable update timestamp."""
    for brief in discover_run_briefs(runs_root):
        if brief.get("available") is True:
            return brief
    return None


def load_run_snapshot(
    run_dir: str, *, include_fetch_attempts: bool = True,
) -> dict[str, Any]:
    """Return one durable, audit-backed run view for the desktop GUI."""
    absolute_run_dir = os.path.abspath(run_dir)
    inventory = build_source_inventory(
        absolute_run_dir, include_fetch_attempts=include_fetch_attempts,
    )
    repo = RunRepository.open_readonly(absolute_run_dir)
    try:
        run = repo.get_run()
        settings = repo.list_run_settings()
        pending = repo.list_pending_tasks()
        pair_states = repo.verification_pair_state_payloads()
        pairs = _verification_pair_views(repo, pair_states)
        progress_counts = _progress_counts(repo, inventory["references"], pairs)
        snapshot = {
            "run_dir": absolute_run_dir,
            "run_id": run.run_id,
            "phase": run.phase,
            "active": run.status == "active",
            "paused": run.status == "paused",
            "action_required": bool(pending),
            "fetch_paused": bool(settings.get("fetch_paused")),
            "references_only": bool(settings.get("references_only")),
            "run_status": run.status,
            "input": {"label": os.path.basename(run.input_path) or run.input_path,
                      "path": run.input_path},
            "accuracy": run.accuracy,
            "source_inventory": inventory["references"],
            "verification_pairs": pairs,
            "progress_counts": progress_counts,
        }
        snapshot["phase_progress"] = _phase_progress(snapshot)
        return snapshot
    finally:
        repo.close()


def load_run_overview(run_dir: str) -> dict[str, Any]:
    """Return run status and progress without projecting every source or pair."""
    absolute_run_dir = os.path.abspath(run_dir)
    repo = RunRepository.open_readonly(absolute_run_dir)
    try:
        run = repo.get_run()
        settings = repo.list_run_settings()
        pending = repo.list_pending_tasks()
        progress_counts = repo.run_progress_counts()
        overview = {
            "run_dir": absolute_run_dir,
            "run_id": run.run_id,
            "phase": run.phase,
            "active": run.status == "active",
            "paused": run.status == "paused",
            "action_required": bool(pending),
            "fetch_paused": bool(settings.get("fetch_paused")),
            "references_only": bool(settings.get("references_only")),
            "run_status": run.status,
            "input": {
                "label": os.path.basename(run.input_path) or run.input_path,
                "path": run.input_path,
            },
            "accuracy": run.accuracy,
            "progress_counts": progress_counts,
            "updated_at": run.updated_at or run.created_at,
        }
        overview["phase_progress"] = _phase_progress(overview)
        return overview
    finally:
        repo.close()


def load_source_page(
    run_dir: str, *, offset: int, limit: int,
) -> dict[str, Any]:
    """Load one bounded source inventory page and its verification lifecycle."""
    inventory = build_source_inventory(
        os.path.abspath(run_dir),
        include_fetch_attempts=False,
        offset=offset,
        limit=limit,
    )
    references = inventory["references"]
    ref_ids = [row["ref_id"] for row in references]
    repo = RunRepository.open_readonly(os.path.abspath(run_dir))
    try:
        pair_states = repo.verification_pair_state_payloads(ref_ids=ref_ids)
        pairs = _verification_pair_views(repo, pair_states, ref_ids=ref_ids)
        total = repo.count_references()
    finally:
        repo.close()
    return {
        "total": total,
        "phase": inventory["run"].get("phase"),
        "source_inventory": references,
        "verification_pairs": pairs,
    }


def load_source_detail(run_dir: str, ref_id: str) -> dict[str, Any] | None:
    """Load the detailed source summary and verification pairs for one ref."""
    absolute_run_dir = os.path.abspath(run_dir)
    inventory = build_source_inventory(
        absolute_run_dir,
        focus_ref_id=ref_id,
        include_fetch_attempts=True,
    )
    references = inventory.get("references") or []
    if not references:
        return None
    reference = references[0]

    repo = RunRepository.open_readonly(absolute_run_dir)
    try:
        ref_ids = [ref_id]
        pair_states = repo.verification_pair_state_payloads(ref_ids=ref_ids)
        pairs = _verification_pair_views(repo, pair_states, ref_ids=ref_ids)
    finally:
        repo.close()

    return {
        "parsed": reference.get("parsed"),
        "resolved": reference.get("resolve"),
        "fetch": reference.get("fetch"),
        "verification_pairs": pairs,
    }


def load_run_summary(run_dir: str) -> dict[str, Any]:
    """Return the existing full History summary for one run directory."""
    child = Path(run_dir)
    try:
        snapshot = load_run_snapshot(str(child))
        counts = _source_counts(snapshot["source_inventory"])
        verification_counts = _verification_counts(
            snapshot["verification_pairs"]
        )
        report_path = child / "report.md"
        journal_path = child / "report.journal.md"
        report_html_path = child / "report.html"
        if not report_html_path.is_file():
            preview_path = child / "report.preview.html"
            report_html_path = preview_path if preview_path.is_file() else None
        repo = RunRepository.open_readonly(str(child))
        try:
            audit_present = bool(repo.list_phase_events())
            crash_recoverable = repo.detect_interrupted_run(
                stale_after_seconds=120
            )
        finally:
            repo.close()
        return {
            "available": True,
            "run_dir": snapshot["run_dir"],
            "run_id": snapshot["run_id"],
            "input": snapshot["input"],
            "accuracy": snapshot["accuracy"],
            "phase": snapshot["phase"],
            "run_status": snapshot["run_status"],
            "references_only": snapshot["references_only"],
            "completed": (
                snapshot["run_status"] == "completed"
                and snapshot["phase"] == "done"
            ),
            "source_counts": counts,
            "verification_counts": verification_counts,
            "action_required": snapshot["action_required"],
            "crash_recoverable": crash_recoverable,
            "audit_present": audit_present,
            "report_present": report_path.is_file(),
            "report_journal_present": journal_path.is_file(),
            "report_html_present": report_html_path is not None,
            "report_html_path": (
                str(report_html_path) if report_html_path is not None else None
            ),
            "updated_at": _run_timestamp(str(child)),
        }
    except Exception as exc:
        return _unavailable_run_row(child, exc)


def _unavailable_run_row(run_dir: str | Path, exc: Exception) -> dict[str, Any]:
    child = Path(run_dir)
    return {
        "available": False,
        "run_dir": str(child.resolve()),
        "run_id": None,
        "error": f"run unavailable: {type(exc).__name__}",
        "unavailable_reason": (
            "incompatible_schema"
            if _is_incompatible_schema_error(exc)
            else "unavailable"
        ),
        "updated_at": None,
    }


def discover_run_summaries(runs_root: str) -> list[dict[str, Any]]:
    """List child runs without allowing one broken database to hide others."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    summaries = [
        load_run_summary(str(child))
        for child in root.iterdir()
        if child.is_dir() and (child / "run.sqlite").is_file()
    ]
    return sorted(summaries, key=_summary_order, reverse=True)


def load_cache_inventory(
    run_dir: str | None = None, environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return reusable parsed texts only, never user-library originals.

    The content-store module intentionally exposes no public list API.  This
    narrow read-only query therefore validates the v4 schema before selecting
    the parsed-text rows required by the desktop cache tab.
    """
    root = Path(content_store.storage_root(run_dir, environ)).resolve()
    path = root / content_store.DB_FILENAME
    empty = {
        "storage_root": str(root), "available": False, "items": [],
        "totals": {"works": 0, "parsed_texts": 0, "file_present": 0,
                   "reusable": 0,
                   "by_tier": {tier: 0 for tier in content_store.TIERS}},
    }
    if not path.is_file():
        return empty
    try:
        conn = _open_valid_content_store(path)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return empty
    try:
        rows = conn.execute(
            """
            SELECT w.work_id,w.title,w.year,w.doi,w.pmid,w.isbn,
                   p.parsed_text_id,p.tier,p.stored_relpath,p.char_count,
                   p.active,p.missing,p.updated_at
            FROM parsed_texts p JOIN works w ON w.work_id=p.work_id
            WHERE p.active=1
            ORDER BY w.updated_at DESC,w.work_id,p.updated_at DESC,p.parsed_text_id
            """
        ).fetchall()
    finally:
        conn.close()

    works: dict[str, dict[str, Any]] = {}
    totals = empty["totals"]
    for row in rows:
        file_present = _parsed_file_present(root, row["stored_relpath"])
        reusable = bool(row["active"]) and not bool(row["missing"]) and file_present
        content = {
            "parsed_text_id": row["parsed_text_id"], "tier": row["tier"],
            "char_count": row["char_count"], "updated_at": row["updated_at"],
            "file_present": file_present, "reusable": reusable,
        }
        work = works.setdefault(row["work_id"], {
            "work_id": row["work_id"], "title": row["title"], "year": row["year"],
            "doi": row["doi"], "pmid": row["pmid"], "isbn": row["isbn"],
            "contents": [], "availability": "none",
        })
        work["contents"].append(content)
        totals["parsed_texts"] += 1
        totals["file_present"] += int(file_present)
        totals["reusable"] += int(reusable)
        if reusable:
            totals["by_tier"][row["tier"]] += 1
            if content_store.TIER_RANK[row["tier"]] > content_store.TIER_RANK.get(work["availability"], 0):
                work["availability"] = row["tier"]
    empty["available"] = True
    empty["items"] = list(works.values())
    totals["works"] = len(works)
    return empty


def _accepted_jury1_outcomes(verification_raw: dict[str, Any]) -> dict[tuple[Any, Any, Any], str]:
    """Project only candidates accepted by Jury2 from validated ledger facts."""
    rows = project_verification_pairs(
        verification_raw.get("pair_states") or (),
        verification_raw.get("candidates") or (),
        verification_raw.get("candidate_events") or (),
    )
    return {
        (row["claim_id"], row["ref_id"], row["scope"]): row["semantic_outcome"]
        for row in rows
        if row.get("resolution") == "jury2_accepted"
        and row.get("jury2_passed") is True
        and isinstance(row.get("representative_candidate_id"), str)
        and row["representative_candidate_id"]
    }


def _verification_pair_views(
    repo: RunRepository,
    pair_states: list[dict[str, Any]],
    *,
    ref_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Project pair lifecycle facts without inferring verdicts from attempts."""
    jury1_outcomes = (
        _accepted_jury1_outcomes(
            repo.verification_raw_payloads()
            if ref_ids is None
            else repo.verification_pair_projection_payloads(ref_ids=ref_ids)
        )
        if any(
            state.get("status") == "accepted"
            and state.get("terminal_cause") == "jury2_accepted"
            and state.get("winner_call_id")
            for state in pair_states
        )
        else {}
    )
    pairs = []
    for state in pair_states:
        pair = {
            key: state.get(key)
            for key in (
                "claim_id", "ref_id", "scope", "status",
                "terminal_outcome", "terminal_cause", "winner_call_id",
                "opened_at", "terminal_at", "active_elapsed_ms",
            )
        }
        pair["jury1_outcome"] = jury1_outcomes.get(
            (pair["claim_id"], pair["ref_id"], pair["scope"])
        )
        pairs.append(pair)
    return pairs


def _source_counts(references: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"resolved": 0, "fulltext": 0, "abstract": 0, "none": 0}
    for reference in references:
        counts["resolved"] += int(reference.get("resolve") is not None)
        tier = (reference.get("fetch") or {}).get("tier")
        counts[tier if tier in {"fulltext", "abstract"} else "none"] += 1
    return counts


def _verification_counts(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    open_count = 0
    for pair in pairs:
        if pair.get("status") == "open":
            open_count += 1
            continue
        outcome = str(pair.get("terminal_outcome") or "unavailable")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {"total": len(pairs), "open": open_count, "outcomes": outcomes}


def _progress_counts(
    repo: RunRepository,
    references: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
) -> dict[str, int]:
    """Return progress denominators and durable completed units for one run."""
    reference_ids = {
        row.get("ref_id")
        for row in references
        if isinstance(row.get("ref_id"), str) and row["ref_id"]
    }
    citation_pairs = {
        (citation.claim_id, citation.ref_id)
        for citation in repo.list_citations()
        if isinstance(citation.claim_id, str) and citation.claim_id
        and isinstance(citation.ref_id, str) and citation.ref_id
    }
    return {
        "sources_total": len(references),
        "resolve_completed": sum(
            1 for row in references if row.get("resolve") is not None
        ),
        "fetch_completed": len(_completed_fetch_reference_ids(
            repo.list_integrity_unit_completions("fetch_auto"), reference_ids,
        )),
        "citation_pairs_total": len(citation_pairs),
        "verify_total": len(pairs),
        "verify_completed": sum(
            1 for pair in pairs if pair.get("status") != "open"
        ),
    }


def _completed_fetch_reference_ids(
    rows: list[dict[str, Any]], reference_ids: set[str],
) -> set[str]:
    """Project identity-valid automatic Fetch conclusions without double counts.

    The ledger permits a subsequent cycle for a reference using ``ref_id#N``.
    Every terminal conclusion counts, including a documented no-source result;
    only its durable identity, not a best available source, establishes progress.
    """
    completed: set[str] = set()
    completion_counts: dict[str, int] = {}
    for row in rows:
        payload = row.get("payload")
        ref_id = payload.get("ref_id") if isinstance(payload, dict) else None
        unit_id = row.get("unit_id")
        if not isinstance(ref_id, str) or not ref_id or not isinstance(unit_id, str):
            continue
        prior_count = completion_counts.get(ref_id, 0)
        expected_unit_id = ref_id if prior_count == 0 else f"{ref_id}#{prior_count + 1}"
        if unit_id != expected_unit_id:
            continue
        completion_counts[ref_id] = prior_count + 1
        if ref_id in reference_ids:
            completed.add(ref_id)
    return completed


def _phase_progress(snapshot: dict[str, Any]) -> int:
    """Project conservative, durable phase progress (never an ETA)."""
    if snapshot.get("run_status") == "completed" and snapshot.get("phase") == "done":
        return 100
    phase = str(snapshot.get("phase") or "").casefold()
    counts = _snapshot_progress_counts(snapshot)
    if phase == "parse":
        return 0
    if phase == "resolve":
        return _phase_fraction(8, 22, counts["resolve_completed"], counts["sources_total"])
    if phase == "fetch":
        return _phase_fraction(30, 45, counts["fetch_completed"], counts["sources_total"])
    if phase in {"gaps", "style", "verify", "web_research"}:
        return _phase_fraction(75, 21, counts["verify_completed"], counts["verify_total"])
    if phase == "report":
        return 96
    return 0


def _snapshot_progress_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    """Use snapshot counts, with a safe legacy fallback for direct callers."""
    raw = snapshot.get("progress_counts")
    if isinstance(raw, dict):
        return {
            key: _nonnegative_int(raw.get(key))
            for key in (
                "sources_total", "resolve_completed", "fetch_completed",
                "citation_pairs_total", "verify_total", "verify_completed",
            )
        }
    references = snapshot.get("source_inventory") or []
    pairs = snapshot.get("verification_pairs") or []
    return {
        "sources_total": len(references),
        "resolve_completed": sum(1 for row in references if row.get("resolve") is not None),
        "fetch_completed": 0,
        "citation_pairs_total": 0,
        "verify_total": len(pairs),
        "verify_completed": sum(1 for pair in pairs if pair.get("status") != "open"),
    }


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _phase_fraction(floor: int, span: int, completed: int, total: int) -> int:
    if total <= 0:
        return floor
    bounded_completed = min(max(completed, 0), total)
    return min(floor + span, floor + round(span * bounded_completed / total))


def _run_timestamp(run_dir: str) -> str | None:
    repo = RunRepository.open_readonly(run_dir)
    try:
        run = repo.get_run()
        return run.updated_at or run.created_at
    finally:
        repo.close()


def _is_incompatible_schema_error(exc: Exception) -> bool:
    """Classify only the repository's explicit current-schema rejection."""
    return (
        isinstance(exc, RuntimeError)
        and "database schema version" in str(exc)
        and "is unsupported (current:" in str(exc)
    )


def _summary_order(summary: dict[str, Any]) -> tuple[int, float, str]:
    value = summary.get("updated_at")
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        timestamp = float("-inf")
    return (int(value is not None), timestamp, summary["run_dir"])


def _open_valid_content_store(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version is None or version[0] != str(content_store.SCHEMA_VERSION):
        conn.close()
        raise ValueError("content store schema is unavailable")
    required = {
        "works": {"work_id", "title", "year", "doi", "pmid", "isbn"},
        "parsed_texts": {
            "parsed_text_id", "work_id", "tier", "stored_relpath", "char_count",
            "active", "missing", "updated_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns <= actual:
            conn.close()
            raise ValueError("content store schema is unavailable")
    return conn


def _parsed_file_present(root: Path, relpath: str) -> bool:
    try:
        path = (root / relpath).resolve()
        parsed_root = (root / content_store.PARSED_CACHE_SUBDIR).resolve()
        return os.path.commonpath((str(path), str(parsed_root))) == str(parsed_root) and path.is_file()
    except (OSError, ValueError):
        return False
