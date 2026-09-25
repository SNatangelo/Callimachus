# core/app/commands/desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Launch the Callimachus desktop application."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

from core.app.runtime_paths import (
    application_argv,
    is_frozen,
    load_environment_file,
    process_environment_without_dotenv,
    refresh_loaded_environment,
    resource_root,
    show_startup_error,
    user_data_root,
)

from core.app.cache_maintenance import deactivate_reusable_texts
from core.app.commands.configure import (
    default_key_path,
    generate_key,
    write_env_updates,
)
from core.app.desktop import (
    discover_run_briefs,
    discover_run_summaries,
    load_cache_inventory,
    load_latest_run_brief,
    load_run_brief,
    load_run_overview,
    load_run_snapshot,
    load_run_summary,
    load_source_detail,
    load_source_page,
)
from core.app.desktop_config import (
    available_verify_backend_options,
    discover_verify_backend_options,
    effective_environment,
    load_settings_inventory,
    preview_verify_configuration,
    selected_verify_environment,
)
from core.gui.guided_fetch_viewmodel import GuidedFetchViewModel
from core.infra.db import RunRepository


def _root() -> Path:
    return resource_root()


def _new_run_dir(runs_root: Path, manuscript: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(manuscript).stem).strip(".-")
    base = runs_root / f"{stamp}-{stem or 'paper'}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = runs_root / f"{base.name}-{suffix}"
        suffix += 1
    return candidate


def _cache_anchor(runs_root: Path, current_run: str | None) -> str:
    """Select the run-root-local cache before a desktop run exists."""
    if current_run:
        return current_run
    return str(runs_root / ".desktop-cache-anchor")


def _reference_rows(
    snapshot: dict[str, Any], *, include_details: bool = True,
    appearance_offset: int = 0,
) -> list[dict[str, Any]]:
    guided_rows = {
        row.ref_id: row
        for row in GuidedFetchViewModel({
            "references": snapshot.get("source_inventory") or [],
        }, env={}).rows()
    }
    pair_by_ref: dict[str, list[dict[str, Any]]] = {}
    for pair in snapshot.get("verification_pairs") or ():
        pair_by_ref.setdefault(str(pair.get("ref_id") or ""), []).append(pair)
    rows = []
    active_phase = snapshot.get("phase")
    for appearance_order, reference in enumerate(
        snapshot.get("source_inventory") or (), start=appearance_offset + 1
    ):
        ref_id = str(reference.get("ref_id") or "")
        parsed = reference.get("parsed") or {}
        source_number = appearance_order
        for candidate in (reference.get("ref_number"), parsed.get("ref_number")):
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, int) and candidate > 0:
                source_number = candidate
                break
            if (
                isinstance(candidate, float)
                and math.isfinite(candidate)
                and candidate > 0
                and candidate.is_integer()
            ):
                source_number = int(candidate)
                break
        resolved = reference.get("resolve") or {}
        fetched = reference.get("fetch") or {}
        pairs = pair_by_ref.get(ref_id, [])
        guided = guided_rows.get(ref_id)
        terminal = [pair for pair in pairs if pair.get("status") != "open"]
        open_pairs = [pair for pair in pairs if pair.get("status") == "open"]
        if open_pairs or terminal:
            phase = "verify"
            status = "in_corso" if open_pairs else "completato"
        elif fetched.get("pending_tasks"):
            phase, status = "fetch", "richiede_intervento"
        elif fetched.get("tier"):
            phase, status = "fetch", "completato"
        elif resolved:
            phase, status = "resolve", "completato"
        else:
            phase = "parse"
            status = "in_corso" if active_phase == "parse" else "in_attesa"
        outcomes = sorted({
            str(pair.get("terminal_outcome"))
            for pair in terminal if pair.get("terminal_outcome")
        })
        jury1_outcomes = sorted({
            str(pair.get("jury1_outcome"))
            for pair in pairs
            if pair.get("status") == "accepted"
            and pair.get("terminal_cause") == "jury2_accepted"
            and pair.get("winner_call_id")
            and pair.get("jury1_outcome")
        })
        result = ", ".join(
            [f"Jury1: {outcome}" for outcome in jury1_outcomes]
            or outcomes
        ) or ""
        row = {
            "ref_id": ref_id,
            "ref_number": source_number,
            "title": parsed.get("title") or parsed.get("raw_entry") or ref_id,
            "phase": phase,
            "status": status,
            "availability": fetched.get("tier") or "none",
            "risk_signal": guided.risk_label if guided is not None else "",
            "risk_tooltip": guided.risk_tooltip if guided is not None else "",
            "review_labels": guided.review_label if guided is not None else "",
            "review_tooltip": guided.review_tooltip if guided is not None else "",
            "result": result,
        }
        if include_details:
            row["details"] = {
                "parsed": parsed,
                "resolved": resolved,
                "fetch": fetched,
                "verification_pairs": pairs,
            }
        rows.append(row)
    return rows


def _history_rows(runs_root: Path) -> list[dict[str, Any]]:
    rows = []
    for summary in discover_run_summaries(str(runs_root)):
        if not summary.get("available"):
            rows.append({
                **summary,
                "paper": Path(summary["run_dir"]).name,
            })
            continue
        rows.append({
            **summary,
            "paper": (summary.get("input") or {}).get("label") or summary["run_id"],
        })
    return rows


def _history_brief_rows(runs_root: Path) -> list[dict[str, Any]]:
    return [
        {
            **brief,
            "paper": (brief.get("input") or {}).get("label")
            or brief.get("run_id")
            or Path(str(brief["run_dir"])).name,
            "summary_pending": brief.get("available") is True,
        }
        for brief in discover_run_briefs(str(runs_root))
    ]


def _history_detail_row(run_dir: str) -> dict[str, Any]:
    summary = load_run_summary(run_dir)
    return {
        **summary,
        "paper": (summary.get("input") or {}).get("label")
        or summary.get("run_id")
        or Path(run_dir).name,
    }


def _delete_history_rows(
    runs_root: Path,
    rows: list[dict[str, Any]],
    *,
    protected_run_dir: str | None = None,
) -> None:
    """Delete only inactive direct-child historical run directories.

    The GUI confirmation is deliberately not trusted as an authority boundary:
    each requested path is re-derived against the current run inventory before
    the irreversible deletion takes place.
    """
    root = runs_root.resolve()
    protected = Path(protected_run_dir).resolve() if protected_run_dir else None
    current = {
        str(Path(str(summary.get("run_dir") or "")).resolve()): summary
        for summary in discover_run_briefs(str(root))
        if summary.get("run_dir")
    }
    targets: list[Path] = []
    seen: set[Path] = set()
    for row in rows:
        run_dir = str(row.get("run_dir") or "")
        if not run_dir:
            raise ValueError("a selected historical run has no run directory")
        candidate = Path(run_dir).resolve()
        summary = current.get(str(candidate))
        protected_active = bool(
            candidate == protected
            and isinstance(summary, dict)
            and (
                summary.get("run_status") == "active"
                or summary.get("action_required")
            )
        )
        if (
            candidate == root
            or candidate.parent != root
            or not candidate.is_dir()
            or protected_active
        ):
            raise ValueError("selected run is not a direct run directory")
        if candidate in seen:
            continue
        if (
            summary is None
            or summary.get("run_status") == "active"
        ):
            raise ValueError("selected run is no longer an inactive historical run")
        seen.add(candidate)
        targets.append(candidate)
    for target in targets:
        shutil.rmtree(target)


def _cache_rows(run_dir: str | None, environ: dict[str, str]) -> list[dict[str, Any]]:
    inventory = load_cache_inventory(run_dir, environ=environ)
    return [
        {
            **item,
            "tier": item.get("availability") or "none",
            "updated_at": max(
                (
                    str(content.get("updated_at") or "")
                    for content in item.get("contents") or ()
                ),
                default="",
            ),
        }
        for item in inventory.get("items") or ()
    ]


def _maintenance_run(runs_root: Path) -> str:
    for row in discover_run_briefs(str(runs_root)):
        if row.get("available") and row.get("run_status") != "active":
            return str(row["run_dir"])
    raise RuntimeError(
        "cache maintenance requires at least one inactive run for its audit boundary"
    )


def _docs_library(root: Path) -> dict[str, str]:
    """Load the complete bundled guide without exposing arbitrary files."""
    guide = root / "docs" / "guide"
    documents: dict[str, str] = {}
    guide_paths = [guide / "README.md", *(guide / f"{index:02d}-{name}.md" for index, name in enumerate((
        "quickstart", "cli-reference", "configuration", "pipeline", "tasks-and-recovery",
        "verification-and-evidence", "artifacts-and-provenance", "capabilities-and-limits",
        "troubleshooting", "keys-and-credentials", "environment-reference",
        "desktop-packages"), 1))]
    document_paths = {path.name: path for path in guide_paths}
    document_paths.update({
        "../deployment/agent-guide.md": root / "docs" / "deployment" / "agent-guide.md",
        "../deployment/administrator-guide.md": root / "docs" / "deployment" / "administrator-guide.md",
        "../architecture/artifact-integrity.md": root / "docs" / "architecture" / "artifact-integrity.md",
        "../../DEPLOYMENT.md": root / "DEPLOYMENT.md",
    })
    for key, path in document_paths.items():
        try:
            documents[key] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    notice_document = _third_party_notice_document(root)
    if notice_document:
        documents["Third-party notices.md"] = notice_document
    return documents


def _third_party_notice_document(root: Path) -> str | None:
    """Summarize the bundled notice index and point to its full local files."""
    candidates: list[Path] = []
    if is_frozen():
        executable_dir = Path(sys.executable).resolve().parent
        for parent in (executable_dir, *list(executable_dir.parents)[:4]):
            candidates.append(parent / "THIRD-PARTY-NOTICES")
    candidates.append(root / "THIRD-PARTY-NOTICES")

    notice_dir = next(
        (path for path in dict.fromkeys(candidates) if (path / "index.json").is_file()),
        None,
    )
    if notice_dir is None:
        return None
    try:
        inventory = json.loads((notice_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(inventory, list):
        return None

    rows = []
    for item in inventory:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"]).replace("|", "\\|")
        version = str(item.get("version") or "").replace("|", "\\|")
        license_name = str(item.get("license") or "See included notice files")
        license_name = license_name.replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {name} | {version} | {license_name} |")

    if not rows:
        return None
    relative_location = "THIRD-PARTY-NOTICES/"
    return "\n".join((
        "# Third-party notices",
        "",
        "Callimachus includes third-party components with their own license terms. "
        "The full license and notice texts are kept outside the application window "
        "so this page stays quick to open.",
        "",
        f"Open `{relative_location}` and its `index.json` for each component's "
        "version and notice-file path. The notice files are in the component's "
        "subfolder.",
        "",
        "| Component | Version | License metadata |",
        "| --- | --- | --- |",
        *rows,
        "",
        "This inventory is informational and is not a legal opinion.",
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("it", "en"), default=None)
    parser.add_argument("--runs-root", default=None)
    args = parser.parse_args(argv)
    root = _root()
    state_root = user_data_root(source_root=root)
    runs_root = Path(args.runs_root or state_root / "runs").resolve()
    env_path = state_root / ".env"
    if is_frozen():
        try:
            state_root.mkdir(parents=True, exist_ok=True)
            runs_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            show_startup_error(
                "Callimachus could not create its per-user data folder. "
                "Check your account's folder permissions and try again."
            )
            return 2
    def environment() -> dict[str, str]:
        return effective_environment(
            env_path=env_path, environ=process_environment_without_dotenv(),
        )

    def save_settings(updates: dict[str, str]) -> Path:
        external = process_environment_without_dotenv()
        shadowed = sorted(name for name in updates if name in external)
        if shadowed:
            raise ValueError(
                "These settings are controlled by the process environment: "
                + ", ".join(shadowed)
                + ". Remove those overrides before editing them here."
            )
        saved_path = write_env_updates(
            env_path, updates, template_path=root / ".env.example",
        )
        refresh_loaded_environment(updates)
        load_environment_file(env_path)
        return Path(saved_path)

    try:
        from core.gui.desktop import run_desktop
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            if is_frozen():
                show_startup_error(
                    "The desktop interface is missing from this Callimachus release. "
                    "Repair or reinstall the current release and try again."
                )
                return 2
            parser.error(
                "PySide6 is required; install requirements-gui.txt and retry"
            )
        raise

    current_run: dict[str, str | None] = {"value": None}

    def llm_available() -> bool:
        current_environment = environment()
        return bool(
            preview_verify_configuration(
                current_environment,
                jury2_level=str(
                    current_environment.get(
                        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL", "medium"
                    )
                ),
            ).get("available")
        )

    def snapshot_loader(run_dir: str) -> dict[str, Any]:
        current_run["value"] = run_dir
        snapshot = load_run_snapshot(run_dir, include_fetch_attempts=False)
        rows = _reference_rows(snapshot, include_details=False)
        return {
            **{
                key: value for key, value in snapshot.items()
                if key not in {"source_inventory", "verification_pairs"}
            },
            "fetch_paused": (
                snapshot.get("fetch_paused")
                or (
                    snapshot.get("phase") == "fetch"
                    and snapshot.get("action_required")
                )
            ),
            "references": rows,
            "llm_available": llm_available(),
        }

    def overview_loader(run_dir: str) -> dict[str, Any]:
        current_run["value"] = run_dir
        return {
            **load_run_overview(run_dir),
            "llm_available": llm_available(),
        }

    def source_page_loader(run_dir: str, offset: int, limit: int) -> dict[str, Any]:
        page = load_source_page(run_dir, offset=offset, limit=limit)
        return {
            "total": page["total"],
            "references": _reference_rows(
                page, include_details=False, appearance_offset=offset,
            ),
        }

    def command_builder(paper: str, mode: str, level: str) -> dict[str, Any]:
        run_dir = _new_run_dir(runs_root, paper)
        preview = preview_verify_configuration(environment(), jury2_level=level)
        command = application_argv(
            "--input",
            paper,
            "--run",
            str(run_dir),
            "--accuracy",
            mode,
            "--proceed",
            source_root=root,
        )
        if not preview.get("available"):
            command.append("--references-only")
        return {
            "command": command,
            "run_dir": str(run_dir),
            "environment": {
                "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": level,
            },
        }

    def resume_builder(run_dir: str) -> list[str] | dict[str, Any]:
        snapshot = load_run_brief(run_dir)
        if snapshot.get("run_status") == "completed":
            raise ValueError("a completed run cannot be resumed; use History to start an explicit new run")
        return application_argv(
            "--run",
            run_dir,
            "--resume",
            "--proceed",
            source_root=root,
        )

    def skip_manual_tasks(run_dir: str) -> int:
        """Apply only explicit negative manual decisions selected in the GUI."""
        repo = RunRepository.open_readonly(run_dir)
        try:
            pending = repo.list_pending_tasks()
        finally:
            repo.close()
        manual = [
            task for task in pending
            if task.task_kind in {"fetch", "browser_challenge", "source_identity_attestation"}
        ]
        unsupported = sorted(
            task.task_id for task in pending
            if task.task_kind not in {
                "fetch", "browser_challenge", "source_identity_attestation", "claim_evidence",
                "web_research",
            }
        )
        if unsupported:
            raise ValueError(
                "automatic manual-task skip cannot handle these tasks: "
                + ", ".join(unsupported)
            )
        if not manual:
            return 0
        from core.app.commands.tasks import cmd_guided_proceed, cmd_skip_identity

        args = argparse.Namespace(
            run=run_dir,
            agent_identity=None,
            debug_override_artifact_integrity=False,
            debug_override_reason=None,
            reason=(
                "Operator selected automatic completion in the desktop GUI; "
                "source identity was not manually reviewed and remains unverified."
            ),
        )
        try:
            if any(task.task_kind in {"fetch", "browser_challenge"} for task in manual):
                cmd_guided_proceed(args)
            if any(task.task_kind == "source_identity_attestation" for task in manual):
                cmd_skip_identity(args)
        except SystemExit as exc:
            raise ValueError(str(exc) or "manual-task skip was refused") from exc
        return len(manual)

    def report_command(row: dict[str, Any]) -> list[str]:
        return application_argv(
            "report",
            "--run",
            str(row["run_dir"]),
            source_root=root,
        )

    def verify_fork_options() -> list[dict[str, object]]:
        current_environment = environment()
        return available_verify_backend_options(
            current_environment,
            jury2_level=str(
                current_environment.get("CITATION_VERIFIER_VERIFY_JURY2_LEVEL")
                or "medium"
            ),
        )

    def verify_fork_command(
        row: dict[str, Any], selected_lanes: list[str]
    ) -> dict[str, Any]:
        options = verify_fork_options()
        by_selector = {str(option["selector"]): option for option in options}
        selected = list(dict.fromkeys(
            str(item).strip() for item in selected_lanes
            if str(item).strip()
        ))
        if not selected or any(item not in by_selector for item in selected):
            raise ValueError(
                "select at least one available Verify backend/model"
            )
        current_environment = environment()
        level = str(
            current_environment.get("CITATION_VERIFIER_VERIFY_JURY2_LEVEL")
            or "medium"
        )
        preview = selected_verify_environment(
            current_environment,
            {"jury1": selected, "jury2": [] if level == "off" else selected},
            jury2_level=level,
        )
        if not preview.get("available"):
            raise ValueError(
                "the selected Verify backends do not form a valid policy: "
                f"{preview.get('reason') or 'configuration unavailable'}"
            )
        overlay = dict(preview["overlay"])
        parent_run = str(row.get("run_dir") or "")
        if not parent_run:
            raise ValueError("the selected historical run has no run directory")
        label = str(row.get("paper") or row.get("run_id") or "run")
        references_only = bool(row.get("references_only"))
        run_dir = _new_run_dir(
            runs_root, f"{label}-{'fetch-verify' if references_only else 'verify'}"
        )
        return {
            "command": application_argv(
                "--run",
                str(run_dir),
                "--fork-reference-only-fetch" if references_only else "--fork-completed-verify",
                parent_run,
                "--proceed",
                source_root=root,
            ),
            "run_dir": str(run_dir),
            "environment": overlay,
        }

    def delete_cache(rows: list[dict[str, Any]]) -> None:
        identifiers = [
            str(content["parsed_text_id"])
            for row in rows
            for content in row.get("contents") or ()
            if content.get("reusable") and content.get("parsed_text_id")
        ]
        deactivate_reusable_texts(
            _maintenance_run(runs_root),
            identifiers,
            environ=environment(),
        )

    def clear_cache() -> None:
        deactivate_reusable_texts(
            _maintenance_run(runs_root),
            None,
            environ=environment(),
        )

    def generate_signing_key() -> dict[str, object]:
        """Create (or retain) a protected signing key and record only its path."""
        configured_path = environment().get("CITATION_VERIFIER_SIGNING_KEY_FILE")
        requested_path = str(configured_path or default_key_path())
        existed = Path(requested_path).exists()
        key_path = generate_key(requested_path, overwrite=False)
        if "CITATION_VERIFIER_SIGNING_KEY_FILE" not in process_environment_without_dotenv():
            save_settings({"CITATION_VERIFIER_SIGNING_KEY_FILE": key_path})
        return {"path": key_path, "created": not existed}

    try:
        return run_desktop(
            language=args.language,
            command_builder=command_builder,
            resume_command_builder=resume_builder,
            snapshot_loader=snapshot_loader,
            overview_loader=overview_loader,
            source_page_loader=source_page_loader,
            latest_loader=lambda: load_latest_run_brief(str(runs_root)),
            source_detail_loader=load_source_detail,
            history_loader=lambda: _history_rows(runs_root),
            history_brief_loader=lambda: _history_brief_rows(runs_root),
            history_detail_loader=_history_detail_row,
            cache_loader=lambda: _cache_rows(
                _cache_anchor(runs_root, current_run["value"]), environment()
            ),
            settings_loader=lambda: load_settings_inventory(
                env_path=env_path,
                template_path=root / ".env.example",
                environ=process_environment_without_dotenv(),
            ),
            verify_preview_loader=lambda level: preview_verify_configuration(
                environment(), jury2_level=level
            ),
            verify_candidates_loader=lambda: discover_verify_backend_options(environment()),
            verify_selection_preview_loader=lambda selected, level: selected_verify_environment(
                environment(), selected, jury2_level=level
            ),
            settings_saver=save_settings,
            signing_key_generator=generate_signing_key,
            docs_loader=lambda: _docs_library(root),
            report_callback=report_command,
            skip_manual_callback=skip_manual_tasks,
            verify_fork_options_loader=verify_fork_options,
            verify_fork_command_builder=verify_fork_command,
            delete_cache_callback=delete_cache,
            clear_cache_callback=clear_cache,
            delete_history_callback=lambda rows: _delete_history_rows(
                runs_root,
                list(rows),
                protected_run_dir=current_run["value"],
            ),
        )
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            parser.error(
                "PySide6 is required; install requirements-gui.txt and retry"
            )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
