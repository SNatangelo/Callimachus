#!/usr/bin/env python3
# core/app/desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only projections used by the Callimachus desktop interface.

The desktop layer reads durable run and content-store state.  It does not
interpret terminal output and never opens either database writable.
"""

from __future__ import annotations

import heapq
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from core.app.guided_fetch import build_source_inventory
from core.fetch.storage import content_store
from core.infra.db import RunRepository
from core.report.verification_projection import project_verification_pairs


def _identity_review_only(pending_tasks: list[Any]) -> bool:
    return bool(pending_tasks) and all(
        task.task_kind == "source_identity_attestation"
        for task in pending_tasks
    )


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
        pending = repo.list_pending_tasks()
        action_required = bool(pending)
        identity_review_only = _identity_review_only(pending)
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
        "identity_review_only": identity_review_only,
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
        if not child.is_dir() or not (
            (child / "run.sqlite").is_file() or (child / "report.html").is_file()
        ):
            continue
        try:
            briefs.append(load_run_brief(str(child)))
        except Exception as exc:
            briefs.append(_unavailable_run_row(child, exc))
    return sorted(briefs, key=_summary_order, reverse=True)


def load_history_page(
    runs_root: str, *, offset: int, limit: int,
) -> dict[str, Any]:
    """Load one bounded History page, opening only selected run databases.

    Run directory names are created with a UTC timestamp prefix, so descending
    name order is the stable inventory order available without opening every
    run database. Older runs whose durable update time diverges from that name
    may appear in a different position than ``discover_run_briefs`` ordering.
    """
    _validate_desktop_page_bounds(offset, limit)
    root = Path(runs_root)
    if not root.is_dir():
        return {"rows": [], "total": 0, "has_more": False, "offset": offset}

    window: list[tuple[str, Path]] = []
    total = 0
    window_size = offset + limit
    for child in root.iterdir():
        if not child.is_dir() or not (
            (child / "run.sqlite").is_file() or (child / "report.html").is_file()
        ):
            continue
        total += 1
        entry = (child.name, child)
        if len(window) < window_size:
            heapq.heappush(window, entry)
        elif entry[0] > window[0][0]:
            heapq.heapreplace(window, entry)
    window.sort(key=lambda entry: entry[0], reverse=True)
    selected = [child for _name, child in window[offset:offset + limit]]
    rows = []
    for child in selected:
        try:
            rows.append(load_run_brief(str(child)))
        except Exception as exc:
            rows.append(_unavailable_run_row(child, exc))
    return {
        "rows": rows, "total": total,
        "has_more": offset + len(rows) < total, "offset": offset,
    }


def _validate_desktop_page_bounds(offset: int, limit: int) -> None:
    if (
        isinstance(offset, bool) or not isinstance(offset, int)
        or offset < 0 or offset > (1 << 63) - 1
        or isinstance(limit, bool) or not isinstance(limit, int)
        or limit < 1 or limit > 100
    ):
        raise ValueError("desktop page bounds are invalid")


def load_latest_run_brief(runs_root: str) -> dict[str, Any] | None:
    """Load the newest readable run brief using its durable update timestamp.

    This potentially scans every child run and is intended for an explicit
    Resume latest request, not initial desktop startup.
    """
    root = Path(runs_root)
    if not root.is_dir():
        return None
    latest = None
    for child in root.iterdir():
        if not child.is_dir() or not (child / "run.sqlite").is_file():
            continue
        try:
            brief = load_run_brief(str(child))
        except Exception:
            continue
        if brief.get("available") is not True:
            continue
        if latest is None or _summary_order(brief) > _summary_order(latest):
            latest = brief
    return latest


def load_latest_run_hint(runs_root: str) -> dict[str, Any] | None:
    """Return a cheap filesystem hint without opening any run database.

    The hint only tells the initial Analysis screen whether a run database is
    present. Resume latest must call :func:`load_latest_run_brief` on demand to
    preserve ordering by each run's durable update timestamp.
    """
    root = Path(runs_root)
    if not root.is_dir():
        return None
    for child in root.iterdir():
        if child.is_dir() and (child / "run.sqlite").is_file():
            return {"available": True, "latest_lookup_pending": True}
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
            "identity_review_only": _identity_review_only(pending),
            "parse_review_pending": sum(
                task.slot == "parse_review" for task in pending
            ),
            "fetch_paused": bool(settings.get("fetch_paused")),
            "institutional_access": settings.get("institutional_access") or {},
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
            "identity_review_only": _identity_review_only(pending),
            "parse_review_pending": sum(
                task.slot == "parse_review" for task in pending
            ),
            "fetch_paused": bool(settings.get("fetch_paused")),
            "institutional_access": settings.get("institutional_access") or {},
            "references_only": bool(settings.get("references_only")),
            "run_status": run.status,
            "input": {
                "label": os.path.basename(run.input_path) or run.input_path,
                "path": run.input_path,
            },
            "accuracy": run.accuracy,
            "progress_counts": progress_counts,
            "updated_at": run.updated_at or run.created_at,
            "verification_activity_revision": repo._conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM llm_dispatch_events"
            ).fetchone()[0],
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
        projection_payloads = (
            repo.verification_pair_projection_payloads(ref_ids=ref_ids)
            if any(state.get("status") != "open" for state in pair_states)
            else None
        )
        pairs = _verification_pair_views(
            repo, pair_states, ref_ids=ref_ids,
            projection_payloads=projection_payloads,
        )
        if projection_payloads is not None:
            _attach_source_verification_records(pairs, pair_states, projection_payloads)
        else:
            for pair in pairs:
                pair["jury1_record"] = None
                pair["jury2_record"] = {"result": "pending", "reason": None}
        claim_labels = {}
        for pair in pairs:
            claim_id = pair.get("claim_id")
            if claim_id not in claim_labels:
                claim = repo._conn.execute(
                    "SELECT sentence, marker_raw FROM claims WHERE claim_id = ?",
                    (claim_id,),
                ).fetchone()
                claim_labels[claim_id] = dict(claim) if claim is not None else {}
            claim = claim_labels[claim_id]
            pair["claim_text"] = claim.get("sentence")
            pair["citation_marker"] = claim.get("marker_raw")
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
            "parse_review_pending": snapshot["parse_review_pending"],
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
    report_path = child / "report.html"
    if not report_path.is_file():
        report_path = child / "report.preview.html"
    report_present = report_path.is_file()
    return {
        "available": False,
        "run_dir": str(child.resolve()),
        "run_id": None,
        "report_html_present": report_present,
        "report_html_path": str(report_path.resolve()) if report_present else None,
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
        if child.is_dir() and (
            (child / "run.sqlite").is_file() or (child / "report.html").is_file()
        )
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
            SELECT w.work_id,w.title,w.year,w.doi,w.pmid,w.isbn,w.normalized_raw_entry,
                   p.parsed_text_id,p.tier,p.stored_relpath,p.char_count,
                   p.active,p.missing,p.updated_at
            FROM parsed_texts p JOIN works w ON w.work_id=p.work_id
            WHERE p.active=1
            ORDER BY w.updated_at DESC,w.work_id,p.updated_at DESC,p.parsed_text_id
            """
        ).fetchall()
    finally:
        conn.close()

    items, totals = _cache_items_from_rows(root, rows)
    empty["available"] = True
    empty["items"] = items
    empty["totals"] = totals
    return empty


def load_cache_page(
    run_dir: str | None = None, environ: dict[str, str] | None = None,
    *, offset: int, limit: int, query: str = "", tier: str = "",
) -> dict[str, Any]:
    """Load one bounded reusable-text page grouped by citation work."""
    _validate_desktop_page_bounds(offset, limit)
    query = str(query or "").strip()
    tier = str(tier or "").strip().lower()
    if tier not in {"", "fulltext", "abstract", "none"}:
        raise ValueError("cache page tier is invalid")
    root = Path(content_store.storage_root(run_dir, environ)).resolve()
    path = root / content_store.DB_FILENAME
    empty = {"rows": [], "total": 0, "has_more": False, "offset": offset}
    if not path.is_file():
        return empty
    try:
        conn = _open_valid_content_store(path)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return empty
    try:
        where_sql, params = _cache_page_filters(query, tier)
        total = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT p.work_id
                FROM parsed_texts p JOIN works w ON w.work_id=p.work_id
                WHERE {where_sql}
                GROUP BY p.work_id,w.updated_at
            )
            """,
            params,
        ).fetchone()[0])
        work_ids = [row[0] for row in conn.execute(
            f"""
            SELECT p.work_id
            FROM parsed_texts p JOIN works w ON w.work_id=p.work_id
            WHERE {where_sql}
            GROUP BY p.work_id,w.updated_at
            ORDER BY w.updated_at DESC,p.work_id
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )]
        if not work_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in work_ids)
            rows = conn.execute(
                f"""
                SELECT w.work_id,w.title,w.year,w.doi,w.pmid,w.isbn,w.normalized_raw_entry,
                       p.parsed_text_id,p.tier,p.stored_relpath,p.char_count,
                       p.active,p.missing,p.updated_at
                FROM parsed_texts p JOIN works w ON w.work_id=p.work_id
                WHERE p.active=1 AND p.work_id IN ({placeholders})
                ORDER BY w.updated_at DESC,w.work_id,p.updated_at DESC,p.parsed_text_id
                """,
                work_ids,
            ).fetchall()
    finally:
        conn.close()
    items, _totals = _cache_items_from_rows(root, rows)
    return {
        "rows": items, "total": total,
        "has_more": offset + len(items) < total, "offset": offset,
    }


def _cache_query_doi(query: str) -> str:
    """Recognize a DOI search without changing stored bibliographic identity."""
    match = re.fullmatch(
        r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)",
        query.strip(), flags=re.IGNORECASE,
    )
    return match.group(1).casefold() if match else ""


def cache_row_matches_query(row: Mapping[str, Any], query: str) -> bool:
    """Retain text search and accept pasted DOI resolver links in either GUI mode."""
    query = query.casefold().strip()
    if query in " ".join(str(value) for value in row.values()).casefold():
        return True
    doi = _cache_query_doi(query)
    return bool(doi and doi == _cache_query_doi(str(row.get("doi") or "")))


def _cache_page_filters(query: str, tier: str) -> tuple[str, tuple[str, ...]]:
    conditions = ["p.active=1"]
    params: list[str] = []
    if query:
        pattern = f"%{query}%"
        search_condition = """(
                LOWER(COALESCE(w.title,'')) LIKE LOWER(?)
                OR LOWER(COALESCE(w.normalized_raw_entry,'')) LIKE LOWER(?)
                OR LOWER(COALESCE(w.doi,'')) LIKE LOWER(?)
                OR LOWER(COALESCE(w.pmid,'')) LIKE LOWER(?)
                OR LOWER(COALESCE(w.isbn,'')) LIKE LOWER(?)
                OR CAST(COALESCE(w.year,'') AS TEXT) LIKE ?
                OR EXISTS (
                    SELECT 1 FROM parsed_texts search_p
                    WHERE search_p.work_id=w.work_id AND search_p.active=1
                    AND (LOWER(search_p.tier) LIKE LOWER(?)
                         OR CAST(search_p.char_count AS TEXT) LIKE ?
                         OR LOWER(COALESCE(search_p.updated_at,'')) LIKE LOWER(?))
                )
            )"""
        params.extend((pattern, pattern, pattern, pattern, pattern, pattern,
                       pattern, pattern, pattern))
        doi = _cache_query_doi(query)
        if doi:
            search_condition = f"({search_condition} OR LOWER(COALESCE(w.doi,''))=?)"
            params.append(doi)
        conditions.append(search_condition)
    if tier in {"fulltext", "abstract"}:
        conditions.append(
            "EXISTS (SELECT 1 FROM parsed_texts tier_p WHERE tier_p.work_id=w.work_id "
            "AND tier_p.active=1 AND tier_p.missing=0 AND tier_p.tier=?)"
        )
        params.append(tier)
        if tier == "abstract":
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM parsed_texts full_p "
                "WHERE full_p.work_id=w.work_id AND full_p.active=1 "
                "AND full_p.missing=0 AND full_p.tier='fulltext')"
            )
    elif tier == "none":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM parsed_texts usable_p "
            "WHERE usable_p.work_id=w.work_id AND usable_p.active=1 "
            "AND usable_p.missing=0)"
        )
    return " AND ".join(conditions), tuple(params)


def _cache_items_from_rows(
    root: Path, rows: list[sqlite3.Row],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:

    works: dict[str, dict[str, Any]] = {}
    totals = {
        "works": 0, "parsed_texts": 0, "file_present": 0, "reusable": 0,
        "by_tier": {tier: 0 for tier in content_store.TIERS},
    }
    from core.app.guided_fetch import source_display_title

    for row in rows:
        file_present = _parsed_file_present(root, row["stored_relpath"])
        reusable = bool(row["active"]) and not bool(row["missing"]) and file_present
        content = {
            "parsed_text_id": row["parsed_text_id"], "tier": row["tier"],
            "char_count": row["char_count"], "updated_at": row["updated_at"],
            "file_present": file_present, "reusable": reusable,
        }
        work = works.setdefault(row["work_id"], {
            "work_id": row["work_id"],
            "title": source_display_title(
                {"title": row["title"], "raw_entry": row["normalized_raw_entry"]},
                ref_id=row["work_id"],
            ),
            "year": row["year"],
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
    totals["works"] = len(works)
    return list(works.values()), totals


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
    projection_payloads: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project pair lifecycle facts without inferring verdicts from attempts."""
    active_pairs: set[tuple[str, str, str]] = set()
    if any(state.get("status") == "open" for state in pair_states) and (
        ref_ids is None or ref_ids
    ):
        scope_filter = ""
        parameters: list[str] = []
        if ref_ids is not None:
            scope_filter = " AND request.ref_id IN (" + ",".join("?" for _ in ref_ids) + ")"
            parameters = ref_ids
        active_pairs = {
            (row["claim_id"], row["ref_id"], row["scope"])
            for row in repo._conn.execute(
                "SELECT DISTINCT request.claim_id, request.ref_id, request.scope "
                "FROM llm_logical_requests request "
                "JOIN llm_dispatch_leases lease USING (logical_request_id) "
                "WHERE lease.status='live' "
                "AND EXISTS (SELECT 1 FROM llm_dispatch_events event "
                "WHERE event.dispatch_attempt_id=lease.dispatch_attempt_id "
                "AND event.event_type='started') "
                "AND NOT EXISTS (SELECT 1 FROM llm_dispatch_events event "
                "WHERE event.dispatch_attempt_id=lease.dispatch_attempt_id "
                "AND event.event_type IN ('completed','failed','abandoned'))"
                + scope_filter,
                parameters,
            )
        }
    jury1_outcomes = {}
    if any(
        state.get("status") == "accepted"
        and state.get("terminal_cause") == "jury2_accepted"
        and state.get("winner_call_id")
        for state in pair_states
    ):
        verification_raw = projection_payloads
        if verification_raw is None:
            verification_raw = (
                repo.verification_raw_payloads()
                if ref_ids is None
                else repo.verification_pair_projection_payloads(ref_ids=ref_ids)
            )
        jury1_outcomes = _accepted_jury1_outcomes(verification_raw)
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
        pair["in_progress"] = pair["status"] == "open" and (
            pair["claim_id"], pair["ref_id"], pair["scope"]
        ) in active_pairs
        pairs.append(pair)
    return pairs


def _attach_source_verification_records(
    pairs: list[dict[str, Any]],
    pair_states: list[dict[str, Any]],
    projection_payloads: dict[str, Any],
) -> None:
    """Attach validated selected-candidate facts to one source's detail DTO."""
    candidates = {
        str(candidate.get("candidate_id")): candidate
        for candidate in projection_payloads.get("candidates") or ()
        if isinstance(candidate, dict) and candidate.get("candidate_id")
    }
    events_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for event in projection_payloads.get("candidate_events") or ():
        if isinstance(event, dict):
            events_by_candidate.setdefault(
                str(event.get("candidate_id") or ""), []
            ).append(event)
    projected = project_verification_pairs(
        projection_payloads.get("pair_states") or pair_states,
        projection_payloads.get("candidates") or (),
        projection_payloads.get("candidate_events") or (),
    )
    projected_by_pair = {
        (row.get("claim_id"), row.get("ref_id"), row.get("scope") or ""): row
        for row in projected
    }
    states_by_pair = {
        (state.get("claim_id"), state.get("ref_id"), state.get("scope") or ""): state
        for state in pair_states
    }
    for pair in pairs:
        key = (pair.get("claim_id"), pair.get("ref_id"), pair.get("scope") or "")
        state = states_by_pair.get(key, {})
        projection = projected_by_pair.get(key, {})
        winner_call_id = state.get("winner_call_id")
        selected_id = projection.get("representative_candidate_id")
        candidate = (
            candidates.get(str(selected_id))
            if selected_id and selected_id == winner_call_id
            else None
        )
        jury1_record = None
        jury2_event = None
        if candidate is not None:
            evidence = candidate.get("evidence") or ()
            grounded = candidate.get("grounded") or ()
            jury1_record = {
                "outcome": candidate.get("outcome"),
                "explanation": candidate.get("explanation"),
                "evidence": [
                    {
                        "quotation": quotation,
                        "source_text": source_span.get("text"),
                        "raw_start": source_span.get("raw_start"),
                        "raw_end": source_span.get("raw_end"),
                    }
                    for quotation, source_span in zip(evidence, grounded)
                    if isinstance(source_span, dict)
                ],
            }
            jury2_event = next((
                event for event in events_by_candidate.get(str(selected_id), ())
                if event.get("event_type") in {"jury2_yes", "jury2_no"}
            ), None)

        event_payload = (
            jury2_event.get("payload")
            if isinstance(jury2_event, dict)
            and isinstance(jury2_event.get("payload"), dict)
            else {}
        )
        strict_jury2_acceptance = bool(
            state.get("status") == "accepted"
            and state.get("terminal_cause") == "jury2_accepted"
            and winner_call_id
            and winner_call_id == selected_id
            and projection.get("resolution") == "jury2_accepted"
            and projection.get("jury2_passed") is True
            and event_payload.get("answer") is True
            and jury2_event.get("event_type") == "jury2_yes"
        )
        if strict_jury2_acceptance:
            jury2_result = "accepted"
        elif (
            (
                projection.get("jury2_passed") is False
                or state.get("terminal_cause") in {
                    "jury2_rejected", "jury2_rejected_nonbinding", "majority_fallback",
                }
            )
            and event_payload.get("answer") is not True
        ):
            jury2_result = "rejected"
        elif (
            state.get("terminal_cause") in {
                "jury2_off", "jury2_not_eligible", "jury1_guard", "jury1_technical",
                "bibliographic_identity_not_corroborated",
                "structural_claim_contamination", "source_integrity_unavailable",
                "source_identity_target_unavailable", "source_identity_check_unavailable",
            }
            and jury2_event is None
        ):
            jury2_result = "not_evaluated"
        else:
            jury2_result = "pending"
        pair["jury1_record"] = jury1_record
        pair["jury2_record"] = {
            "result": jury2_result,
            "reason": event_payload.get("reason"),
        }


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
