#!/usr/bin/env python3
# core/app/run.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Callimachus pipeline runtime driver."""
from __future__ import annotations

import _thread
import atexit
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from core.app.runtime_paths import (
    application_argv,
    is_frozen,
    load_environment_file,
    read_build_metadata,
    resource_root,
    user_data_root,
)
from core.invocation import run_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_ACTIVE_RUN_DIR: str | None = None
_ACTIVE_DRIVER_SESSION: tuple[str, str] | None = None
_ACTIVE_HEARTBEAT_STOP: threading.Event | None = None


def _desktop_control_reader(stream, interrupt=_thread.interrupt_main) -> None:
    """Translate the desktop's explicit STOP line into the normal Ctrl+C path."""
    while True:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            return
        if not line:
            return
        if line.rstrip("\r\n") == "STOP":
            interrupt()
            return


def _stdin_is_interactive() -> bool:
    """Windowed frozen builds may not have a Python stdin stream at all."""
    stream = getattr(sys, "stdin", None)
    try:
        return bool(stream is not None and stream.isatty())
    except (OSError, ValueError):
        return False


def _start_desktop_control_reader() -> None:
    """Enable the one-shot control pipe only for the opt-in desktop child."""
    if os.environ.pop("CALLIMACHUS_DESKTOP_CONTROL", "") != "1":
        return
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return
    threading.Thread(
        target=_desktop_control_reader, args=(stream,), daemon=True,
        name="callimachus-desktop-control",
    ).start()


def _resume_command(run_dir: str, platform_name: str | None = None) -> str:
    """Format a shell-safe resume command for the operator's platform."""
    return run_command("--run", run_dir, "--resume", platform_name=platform_name)


def _guided_fetch_command(
    run_dir: str,
    platform_name: str | None = None,
    *,
    agent_identity: str | None = None,
) -> str:
    """Format the explicit human-operated Guided Fetch launch command."""
    args = ["--run", run_dir, "--resume", "--guided-fetch"]
    if agent_identity is not None:
        args.extend(("--agent-identity", agent_identity))
    return run_command(*args, platform_name=platform_name)


def _guided_fetch_pause_eligible(st: dict, result_code: int, *, autonomous: bool) -> bool:
    """Identify a paused Fetch run eligible for the guided workflow."""
    return bool(
        result_code == ACTION_REQUIRED
        and st.get("phase") == "fetch"
        and st.get("fetch_paused")
        and not autonomous
    )


def _guided_fetch_offer_available(st: dict, result_code: int, *, autonomous: bool) -> bool:
    """Offer the desktop workflow when the eligible pause can read a reply."""
    return bool(
        _guided_fetch_pause_eligible(st, result_code, autonomous=autonomous)
        and _stdin_is_interactive()
    )


def _resume_paused_run(
    st: dict,
    *,
    agent_identity: str | None = None,
    resume_runner=subprocess.run,
) -> int:
    root = resource_root()
    command = application_argv(
        "--run", st["run_dir"], "--resume", source_root=root,
    )
    if agent_identity is not None:
        command.extend(("--agent-identity", agent_identity))
    runner_options = {"check": False}
    if is_frozen():
        runner_options["cwd"] = str(user_data_root())
    completed = resume_runner(command, **runner_options)
    return completed.returncode


def _maybe_run_guided_fetch(
    st: dict,
    result_code: int,
    *,
    autonomous: bool,
    guided_fetch_requested: bool = False,
    agent_identity: str | None = None,
    ask=input,
    resume_runner=subprocess.run,
) -> int | None:
    """Ask, launch the opt-in GUI, then resume only after its explicit proceed."""
    if not _guided_fetch_pause_eligible(st, result_code, autonomous=autonomous):
        if guided_fetch_requested:
            print(
                "Guided Fetch was not launched: this run is not paused in Fetch.",
                file=sys.stderr,
            )
        return None
    if guided_fetch_requested:
        from core.gui.launcher import launch_guided_fetch, missing_guided_dependencies

        missing = missing_guided_dependencies()
        if missing:
            print(
                "Guided Fetch was not launched: optional components are missing "
                f"({', '.join(missing)}). Install requirements-gui.txt and retry.",
                file=sys.stderr,
            )
            return None
        from core.app.guided_fetch import GuidedFetchController

        # The agent may request this window. Its identity remains attached to
        # the controller so admission can reject a Guided Fetch answer rather
        # than treating the desktop callback as an operator identity.
        controller = GuidedFetchController(st["run_dir"], agent_identity=agent_identity)
        launched = launch_guided_fetch(
            st["run_dir"],
            submit_source=controller.stage_source,
            proceed=controller.proceed,
            discard_source=controller.discard_source,
            submit_identity_review=controller.submit_identity_decision,
            identity_review_allowed=controller.identity_review_allowed,
        )
        if not launched.started:
            print(f"Guided Fetch was not launched: {launched.reason}", file=sys.stderr)
            return None
        if not controller.proceeded:
            return None
        return _resume_paused_run(
            st, agent_identity=agent_identity, resume_runner=resume_runner
        )
    if not _guided_fetch_offer_available(st, result_code, autonomous=autonomous):
        print(
            "Guided Fetch needs an interactive terminal. To launch it on explicit "
            "request, use:\n"
            f"  {_guided_fetch_command(st['run_dir'], agent_identity=agent_identity)}",
        )
        return None
    try:
        answer = ask(
            "Start Guided Fetch? [y/N/skip] "
            "(skip waives the remaining source retrievals): "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer == "skip":
        from core.app.guided_fetch import GuidedFetchController

        try:
            summary = GuidedFetchController(
                st["run_dir"], agent_identity=agent_identity
            ).proceed()
        except (OSError, ValueError) as exc:
            print(f"Could not waive pending Fetch tasks: {exc}", file=sys.stderr)
            return None
        waived = len(summary.get("waived_ref_ids") or ())
        print(f"Waived {waived} unresolved source retrieval(s); resuming at Verify.")
        return _resume_paused_run(
            st, agent_identity=agent_identity, resume_runner=resume_runner
        )
    if answer not in {"y", "yes", "s", "si", "sì"}:
        return None

    from core.gui.launcher import (
        install_guided_dependencies,
        launch_guided_fetch,
        missing_guided_dependencies,
    )

    missing = missing_guided_dependencies()
    if missing:
        try:
            install = ask(
                "Optional components are missing "
                f"({', '.join(missing)}). Install them now? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if install not in {"y", "yes", "s", "si", "sì"}:
            print("Guided Fetch was not launched: optional components were not installed.")
            return None
        installed, reason = install_guided_dependencies()
        if not installed:
            print(f"Guided Fetch was not launched: {reason}", file=sys.stderr)
            return None

    from core.app.guided_fetch import GuidedFetchController

    controller = GuidedFetchController(st["run_dir"], agent_identity=agent_identity)
    launched = launch_guided_fetch(
        st["run_dir"],
        submit_source=controller.stage_source,
        proceed=controller.proceed,
        discard_source=controller.discard_source,
        submit_identity_review=controller.submit_identity_decision,
        identity_review_allowed=controller.identity_review_allowed,
    )
    if not launched.started:
        print(f"Guided Fetch was not launched: {launched.reason}", file=sys.stderr)
        return None
    if not controller.proceeded:
        return None

    return _resume_paused_run(
        st, agent_identity=agent_identity, resume_runner=resume_runner
    )


def _report_interruption(run_dir: str | None) -> int:
    if run_dir is not None:
        print("Interrupted. Resume this run with:", file=sys.stderr)
        print(f"  {_resume_command(run_dir)}", file=sys.stderr)
    return 130


def _cleanup_interrupted_driver() -> None:
    """Release the active session if Ctrl+C arrives outside the driver finally."""
    global _ACTIVE_DRIVER_SESSION, _ACTIVE_HEARTBEAT_STOP
    if _ACTIVE_HEARTBEAT_STOP is not None:
        try:
            _ACTIVE_HEARTBEAT_STOP.set()
        except BaseException:
            pass
        _ACTIVE_HEARTBEAT_STOP = None
    if _ACTIVE_DRIVER_SESSION is not None:
        run_dir, session_id = _ACTIVE_DRIVER_SESSION
        try:
            _end_driver_session(run_dir, session_id, status="interrupted")
        except BaseException:
            pass
        _ACTIVE_DRIVER_SESSION = None


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith(("export ", "export\t")):
        line = line[len("export"):].lstrip()
    key, value = (piece.strip() for piece in line.split("=", 1))
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    else:
        comment = value.find(" #")
        if comment != -1:
            value = value[:comment].rstrip()
    return key, value


def _load_dotenv(path: str | None = None) -> None:
    """Load the current local environment format without Verify side effects."""
    try:
        if path is None:
            root = resource_root()
            configured_data_root = os.environ.get("CALLIMACHUS_DATA_DIR", "").strip()
            directories = (
                (user_data_root(),)
                if configured_data_root or is_frozen()
                else (
                    root / "core" / "verify", root / "core", root,
                )
            )
            for directory in directories:
                for name in (".env", ".env.local"):
                    candidate = directory / name
                    if candidate.is_file():
                        path = str(candidate)
                        break
                if path is not None:
                    break
        if path is None:
            return
        load_environment_file(path)
    except Exception:
        pass


# Load .env at import time so all startup checks see the configured values.
_load_dotenv()

if __package__:
    from core.fetch.storage import content_store
    from core.fetch.diagnostics import fetch_audit as _fetch_audit
    from core.app import pipeline as _pipeline
    from core.parse import parse_manuscript
    from core.resolve import service as _resolve
    from core.parse.extract import extract_text
    from core.fetch.extraction.pdf import check_deps as _check_pdf_deps
    from core.verify import verify_run
    from core.resolve import sources as _sources
    from core.infra.db import RunRepository
    from core.fetch import service as _fetch
    from core.fetch.fallbacks import fetch_modes as _fetch_modes
    from core.fetch.storage.fetch_store import origin_for_method
    from core.infra import startup_preflight as _startup_preflight
    from core.style import detect as _style_detect
    from core.fetch.transport.http_headers import (
        DEFAULT_HTTP_PROFILE,
        ENV_HTTP_PROFILE,
        HTTP_PROFILE_CHOICES,
    )
    from core.infra.integrity import signing as _signing
    from core.infra import perf as _perf
else:  # direct execution
    import content_store
    import fetch_audit as _fetch_audit
    import pipeline as _pipeline
    import parse_manuscript
    import resolve as _resolve
    from extract import extract_text
    from pdf import check_deps as _check_pdf_deps
    import sources as _sources
    import verify_run
    from db import RunRepository
    import fetch as _fetch
    import fetch_modes as _fetch_modes
    from fetch_store import origin_for_method
    import startup_preflight as _startup_preflight
    from style import detect as _style_detect
    from http_headers import (
        DEFAULT_HTTP_PROFILE,
        ENV_HTTP_PROFILE,
        HTTP_PROFILE_CHOICES,
    )
    import signing as _signing
    import perf as _perf

from core.app.runtime.settings import (
    _MODEL_SUGGESTIONS, ACCURACY_CHOICES, ACTION_REQUIRED, CHALLENGE_MODE_CHOICES,
    DEFAULT_ACCURACY, DEFAULT_CHALLENGE_MODE, DEFAULT_FETCH_WORKERS, DEFAULT_OCR_LANG,
    ENV_ACCURACY, ENV_CHALLENGE_MODE, ENV_DEBUG_RUN, ENV_FETCH_WORKERS, ENV_GBOOKS,
    ENV_MAILTO, ENV_OCR_LANG, ENV_REPORT_HTML, ENV_RESOLVE_WORKERS, GATE_FAILED,
    PHASES, PKG_DIR, RUN_LOCKED, _blocking_fetch_need_items, _config_issues,
    _debug_directives_from_parse, _debug_mode_enabled, _fetch_worker_count,
    _gbooks_config_status, _model_suggestions_for, _module, _print_signing_status,
    _print_startup_check, _progress, _progress_phase, _resolve_worker_count,
)
from core.app.runtime.repository import (
    RUNTIME_SETTING_KEYS, _configure_debug_mode, _fixture_fingerprint, _fresh_start_seed,
    _load_parse_payload, _load_resolve_map, _load_run_refs, _now, _repo_mark_phase,
    _repo_open, _repo_record_resolution_trace, _repo_sync_parse, _repo_sync_resolve_result,
    _run_ref_by_id, _runtime_state_from_repo, _save_state, _sha256_file,
)
from core.app.runtime.sources import (
    ABSTRACT_FALLBACK_SCOPE, ABSTRACT_ONLY_SCOPE, ABSTRACT_SCOPES, TIER_TO_SCOPE,
    _fetch_evidence_payload, _load_manifest_payload, _load_unreadable_payload,
    _materialize_resolve_abstracts, _provide_identity_for_origin,
    _ref_has_materialized_source, _ref_has_source_tier, _register_run_source_text,
    _repair_abstract_payload, _resolve_abstract_origin, _resolve_abstract_source_ref,
    _resolve_map, _scope_for_source, _store_resolve_abstract_if_needed,
    _suppress_repair_abstract_entries,
)
from core.app.runtime.fetch_audit import _record_fetch_attempts
from core.app.runtime.resolve_failures import (
    _exception_payload, _repair_exception_result, _repair_failed_result, _resolve_exception_result,
)
from core.app.runtime.credentials import _print_phase_credential_warnings
from core.infra.credential_catalog import capture_credential_inventory
from core.app.runtime.tasks import (
    STATUS_SLOTS, _answered_tasks, _create_task, _pause, _pending_tasks, _reopen_task_with_error,
    _submit_task_answer, _task_ingest_error, _task_store_mode, _update_task, print_status, status_snapshot,
)
from core.infra.integrity import (
    DEBUG_INTEGRITY_LABEL,
    IntegrityGateError,
    RunIntegrityGate,
)
from core.infra.integrity.execution_assurance import (
    derived_child_assurance,
    downgrade_after_authority_failure,
    new_assurance,
    resolve_existing,
)
from core.verify.claim_evidence import ClaimEvidenceRuntime

# Re-exports of helpers moved to phase modules — tests / internal callers
# reference these via run.<name>.
from core.app.phases.fetch import (  # noqa: E402
    _auto_fetch_fulltexts,
    _auto_run_source_ocr,
    _fetchable_need_items,
    _ingest_fetch_answers,
    _run_source_ocr,
    _seed_reusable_sources,
)
from core.app.phases.resolve import _resolve_reference_pipeline  # noqa: E402
from core.app.phases.verify import (  # noqa: E402
    _emit_verify_tasks,
    _sync_verify_runtime_setting,
    _verify_pairs,
)
from core.app.phases.web_research import (  # noqa: E402
    _emit_web_verify_tasks,
    _has_tier,
    _web_research_triggers,
)


# --------------------------------------------------------------------------- #
#  Phase: parse                                                                #
# --------------------------------------------------------------------------- #

def phase_parse(st):
    from core.app.phases.parse import phase_parse as _phase_parse
    return _phase_parse(st, _progress=_progress,
                        _configure_debug_mode=_configure_debug_mode,
                        _repo_open=_repo_open, _save_state=_save_state)






# --------------------------------------------------------------------------- #
#  Phase: resolve (existence + abstract retrieval, deterministic + web)        #
# --------------------------------------------------------------------------- #

def phase_resolve(st):
    from core.app.phases.resolve import phase_resolve as _phase_resolve
    from core.app.commands.journal_catalog import ensure_for_resolve
    from core.resolve.journal_authority import CATALOG_ENV

    prior_catalog = os.environ.get(CATALOG_ENV)
    ensure_for_resolve(progress=_progress)
    try:
        next_phase = _phase_resolve(st)
        if next_phase == "fetch" and st.get("references_only"):
            return "report"
        return next_phase
    finally:
        if prior_catalog is None:
            os.environ.pop(CATALOG_ENV, None)
        else:
            os.environ[CATALOG_ENV] = prior_catalog










# --------------------------------------------------------------------------- #
#  Phase: fetch (LLM/agent web tools — opt pause for missing full text)        #
# --------------------------------------------------------------------------- #















def _route_reference_only_report(st):
    if not st.get("references_only"):
        return False
    st["fetch_paused"] = False
    return True


def phase_fetch(st):
    from core.app.phases.fetch import phase_fetch as _phase_fetch

    if _route_reference_only_report(st):
        return "report"
    return _phase_fetch(st)















# --------------------------------------------------------------------------- #
#  Phase: gaps + style                                                         #
# --------------------------------------------------------------------------- #

def phase_gaps(st):
    from core.app.phases.gaps_style import phase_gaps as _phase_gaps

    if _route_reference_only_report(st):
        return "report"
    return _phase_gaps(st)


def phase_style(st):
    from core.app.phases.gaps_style import phase_style as _phase_style

    if _route_reference_only_report(st):
        return "report"
    return _phase_style(st)




















def _prepare_verify_backend(st, *, ask=input):
    """Select a per-run backend or explicitly switch to a partial report."""
    env_name = "CITATION_VERIFIER_VERIFY_BACKENDS"
    configured = (os.environ.get(env_name) or st.get("verify_backends") or "").strip()
    if configured:
        os.environ[env_name] = configured
        return "verify"

    if not _stdin_is_interactive():
        print(
            "Verify needs an LLM backend. Set CITATION_VERIFIER_VERIFY_BACKENDS "
            "and resume, or resume with --references-only for a partial "
            "reference-check report.",
            file=sys.stderr,
        )
        return None

    from core.verify.backends import _registry
    import core.verify.backends  # noqa: F401 - registers backend specifications

    known = set(_registry.all_names())
    while True:
        try:
            answer = ask(
                "No LLM backend is configured. Enter backend name(s) as CSV, "
                "type 'report' for a reference-check-only report, or press "
                "Enter to stop: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not answer:
            print(f"Run paused. Resume with: {_resume_command(st['run_dir'])}")
            return None
        if answer.lower() in {"report", "references", "references-only"}:
            st["references_only"] = True
            _save_state(st)
            return "report"

        selected = tuple(item.strip().lower() for item in answer.split(",") if item.strip())
        unknown = sorted(set(selected) - known)
        if not selected or unknown:
            suffix = f": {', '.join(unknown)}" if unknown else ""
            print(f"Unknown LLM backend{suffix}. Available: {', '.join(sorted(known))}")
            continue

        configured = ",".join(selected)
        os.environ[env_name] = configured
        st["verify_backends"] = configured
        _save_state(st)
        print(f"Verify backend(s) selected for this run: {configured}")
        return "verify"


def phase_verify(st):
    from core.app.phases.verify import phase_verify as _phase_verify

    if _route_reference_only_report(st):
        return "report"
    decision = _prepare_verify_backend(st)
    if decision == "report":
        return "report"
    if decision is None:
        return ACTION_REQUIRED
    return _phase_verify(st)


def phase_web_research(st):
    from core.app.phases.web_research import phase_web_research as _phase_web_research

    if _route_reference_only_report(st):
        return "report"
    return _phase_web_research(st)





# Char budget for full-context composition: when the combined full texts of a claim's
# sources fit, the composer sees them whole; otherwise it falls back to the union of the
# already-verified spans. Sized to stay comfortably within a single model context.
_COMPOSE_FULLCTX_BUDGET = 120_000










# --------------------------------------------------------------------------- #
#  Phase: report + gate                                                        #
# --------------------------------------------------------------------------- #

def phase_report(st):
    from core.app.phases.report_gate import (
        phase_reference_report,
        phase_report as _phase_report,
    )

    if st.get("references_only"):
        return phase_reference_report(st)
    return _phase_report(st)


PHASE_FN = {
    "parse": phase_parse, "resolve": phase_resolve,
    "fetch": phase_fetch,
    "gaps": phase_gaps, "style": phase_style, "verify": phase_verify,
    "web_research": phase_web_research,
    "report": phase_report,
}


# --------------------------------------------------------------------------- #
#  Driver loop                                                                 #
# --------------------------------------------------------------------------- #

_CONTENT_STORE_MUTATING_PHASES = frozenset({"fetch", "verify", "web_research"})


class _PipelineTransition:
    """Own one active signed lease and rotate it after completed work units."""

    def __init__(
        self,
        st: dict,
        gate: RunIntegrityGate | None,
        *,
        checkpoint_kind: str,
        mutates_content_store: bool,
    ) -> None:
        self._st = st
        self._gate = gate
        self._checkpoint_kind = checkpoint_kind
        self._mutates_content_store = mutates_content_store
        self._lease = None
        self._lock = st.setdefault("_integrity_lease_lock", threading.Lock())

    def _raise_heartbeat_error(self) -> None:
        error = self._st.get("_integrity_heartbeat_error")
        if error is not None:
            raise IntegrityGateError(f"integrity lease heartbeat failed: {error}")

    def _set_active(self, lease) -> None:
        self._lease = lease
        if lease is None:
            self._st.pop("_active_integrity_lease", None)
        else:
            self._st["_active_integrity_lease"] = lease

    def begin(self) -> None:
        if self._gate is None:
            return
        with self._lock:
            self._raise_heartbeat_error()
            lease = self._gate.begin_pipeline_transition(
                self._st["run_dir"],
                checkpoint_kind=self._checkpoint_kind,
                mutates_content_store=self._mutates_content_store,
            )
            self._set_active(lease)

    def install_unit_callback(self) -> None:
        if self._gate is not None:
            self._st["_integrity_unit_checkpoint"] = self.checkpoint_unit

    def remove_unit_callback(self) -> None:
        if self._st.get("_integrity_unit_checkpoint") == self.checkpoint_unit:
            self._st.pop("_integrity_unit_checkpoint", None)

    def checkpoint_unit(self, _group: str, _unit_id: str) -> None:
        if self._gate is None:
            return
        with self._lock:
            self._raise_heartbeat_error()
            if self._lease is None:
                raise IntegrityGateError("integrity unit checkpoint has no active lease")
            self._gate.commit_pipeline_transition(
                self._st["run_dir"], self._lease
            )
            self._set_active(None)
            lease = self._gate.begin_pipeline_transition(
                self._st["run_dir"],
                checkpoint_kind="unit",
                mutates_content_store=self._mutates_content_store,
            )
            self._set_active(lease)

    def commit(self) -> None:
        if self._gate is None:
            return
        with self._lock:
            self._raise_heartbeat_error()
            if self._lease is None:
                raise IntegrityGateError("integrity transition has no active lease")
            self._gate.commit_pipeline_transition(
                self._st["run_dir"], self._lease
            )
            self._set_active(None)

    def abort(self, *, reason: str) -> None:
        if self._gate is None:
            return
        with self._lock:
            if self._lease is None:
                return
            lease = self._lease
            self._set_active(None)
            self._gate.abort_pipeline_transition(
                self._st["run_dir"], lease, reason=reason
            )


def _abort_preserving_exception(
    transition: _PipelineTransition, *, reason: str, original: BaseException
) -> None:
    """Best-effort abort that cannot hide the exception that triggered it."""
    try:
        transition.abort(reason=reason)
    except BaseException as abort_exc:
        diagnostic = (
            "integrity transition abort failed while handling "
            f"{type(original).__name__}: {type(abort_exc).__name__}: {abort_exc}"
        )
        add_note = getattr(original, "add_note", None)
        if callable(add_note):
            try:
                add_note(diagnostic)
                return
            except BaseException:
                pass
        print(f"[integrity] {diagnostic}", file=sys.stderr)


def drive(st, *, integrity_gate: RunIntegrityGate | None = None):
    """Advance through phases until a PAUSE, DONE, or error."""
    with RunRepository.session(st["run_dir"]):
        return _drive(st, integrity_gate=integrity_gate)


def _drive(st, *, integrity_gate: RunIntegrityGate | None = None):
    """Advance through phases while the run repository session is pinned."""
    if integrity_gate is not None:
        st["_execution_integrity_gate"] = integrity_gate
    else:
        st.pop("_execution_integrity_gate", None)

    if (
        st["phase"] == "fetch"
        and st.get("fetch_paused")
        and not st.get("references_only")
    ):
        from core.app.phases.fetch import _ingest_fetch_answers

        transition = _PipelineTransition(
            st,
            integrity_gate,
            checkpoint_kind="external_input",
            mutates_content_store=True,
        )
        try:
            transition.begin()
            transition.install_unit_callback()
            _ingest_fetch_answers(st)
            transition.commit()
        except IntegrityGateError as exc:
            print(f"[integrity] {exc}", file=sys.stderr)
            return 2
        except BaseException as exc:
            _abort_preserving_exception(
                transition,
                reason=f"fetch answer ingestion raised {type(exc).__name__}",
                original=exc,
            )
            raise
        finally:
            transition.remove_unit_callback()

    while True:
        phase = st["phase"]
        if phase == "done":
            transition = _PipelineTransition(
                st,
                integrity_gate,
                checkpoint_kind="phase_boundary",
                mutates_content_store=False,
            )
            try:
                transition.begin()
                _repo_mark_phase(
                    st["run_dir"],
                    "done",
                    session_id=st.get("db_session_id"),
                    status="completed",
                    event_type="done",
                )
                _perf.persist_summary(st["run_dir"], st.get("db_session_id"))
                transition.commit()
            except IntegrityGateError as exc:
                print(f"[integrity] {exc}", file=sys.stderr)
                return 2
            except BaseException as exc:
                _abort_preserving_exception(
                    transition,
                    reason=f"run completion raised {type(exc).__name__}",
                    original=exc,
                )
                raise
            if _perf.is_enabled():
                print(_perf.format_table())
            report_name = (
                "report.preview.html" if st.get("references_only") else "report.md"
            )
            print(f"\nDONE · report: {os.path.join(st['run_dir'], report_name)}")
            return 0

        fn = PHASE_FN.get(phase)
        if fn is None:
            print(f"unknown phase {phase}", file=sys.stderr)
            return 2
        transition = _PipelineTransition(
            st,
            integrity_gate,
            checkpoint_kind="report" if phase == "report" else "phase_boundary",
            mutates_content_store=phase in _CONTENT_STORE_MUTATING_PHASES,
        )
        try:
            transition.begin()
            transition.install_unit_callback()
        except IntegrityGateError as exc:
            print(f"[integrity] {exc}", file=sys.stderr)
            return 2

        try:
            with _progress_phase(phase), _perf.span("phase", phase):
                nxt = fn(st)
        except BaseException as exc:
            try:
                _abort_preserving_exception(
                    transition,
                    reason=f"phase {phase} raised {type(exc).__name__}",
                    original=exc,
                )
            finally:
                transition.remove_unit_callback()
            raise
        freeze_ready = False
        if nxt == "error":
            _repo_mark_phase(
                st["run_dir"],
                phase,
                session_id=st.get("db_session_id"),
                status="failed",
                event_type="fail",
            )
            result_code = 2
        elif nxt == "_gate_failed":
            _repo_mark_phase(
                st["run_dir"],
                phase,
                session_id=st.get("db_session_id"),
                status="failed",
                event_type="fail",
                payload={"reason": "gate_failed"},
            )
            result_code = GATE_FAILED
        elif nxt == ACTION_REQUIRED:
            _repo_mark_phase(st["run_dir"], phase, status="paused")
            result_code = ACTION_REQUIRED
        else:
            st["phase"] = nxt
            _save_state(st)
            _repo_mark_phase(
                st["run_dir"],
                nxt,
                session_id=st.get("db_session_id"),
                status="active",
                event_type="enter",
            )
            freeze_ready = phase == "fetch" and bool(st.get("freeze_after_fetch"))
            if freeze_ready:
                _repo_mark_phase(st["run_dir"], nxt, status="paused")
                result_code = ACTION_REQUIRED
            else:
                result_code = None

        try:
            if result_code == ACTION_REQUIRED:
                _perf.persist_summary(st["run_dir"], st.get("db_session_id"))
            transition.commit()
        except IntegrityGateError as exc:
            print(f"[integrity] {exc}", file=sys.stderr)
            return 2
        except BaseException as exc:
            _abort_preserving_exception(
                transition,
                reason=f"phase {phase} checkpoint raised {type(exc).__name__}",
                original=exc,
            )
            raise
        finally:
            transition.remove_unit_callback()
        if freeze_ready:
            with _progress_phase("fetch"):
                _progress("Fetch complete; frozen post-fetch baseline is ready for --fork-frozen-fetch-verify.")
        if result_code is not None:
            return result_code


#  Run-dir lock — prevents two concurrent driver invocations on the same run   #
# --------------------------------------------------------------------------- #
_RUN_LOCK_FILENAME = ".run.lock"
_DRIVER_LEASE_STALE_SECONDS = 120
_DRIVER_HEARTBEAT_SECONDS = 15


def _acquire_run_lock(run_dir):
    """Take an exclusive, non-blocking lock on ``<run_dir>/.run.lock`` so two
    `run.py --resume`/`--run` invocations can't process the same run dir at the
    same time (observed failure mode: concurrent juries duplicating work).

    POSIX only, via ``fcntl.flock(LOCK_EX | LOCK_NB)``. On a platform without
    ``fcntl`` (e.g. Windows) this degrades to a no-op — returns ``None`` — rather
    than failing the run; the driver is still safe for the common single-process
    case there, just not concurrency-guarded.

    Returns the open file handle to keep alive for the run's lifetime (the OS
    releases the flock automatically when the handle/process closes — no
    stale-lock file cleanup is needed, and a single sequential run acquires and
    releases without friction). Exits the process with ``RUN_LOCKED`` if the
    lock is already held by another process.
    """
    try:
        import fcntl
    except ImportError:
        return None  # non-POSIX platform: no-op fallback, see docstring
    os.makedirs(run_dir, exist_ok=True)
    lock_path = os.path.join(run_dir, _RUN_LOCK_FILENAME)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print(f"[lock] run already in progress: {run_dir}", file=sys.stderr)
        sys.exit(RUN_LOCKED)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass  # best-effort diagnostic contents; the flock itself is what matters
    return fh


def _release_run_lock(fh):
    """Release a lock taken by :func:`_acquire_run_lock`. Safe to call with
    ``None`` (no-op platform) and safe to register with ``atexit`` — releasing
    twice, or after the fd is already closed, is a no-op."""
    if fh is None:
        return
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def _start_driver_heartbeat(
    run_dir: str,
    session_id: str,
    *,
    state: dict | None = None,
    integrity_gate: RunIntegrityGate | None = None,
):
    """Keep both the DB session and active signed mutation lease alive."""
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(_DRIVER_HEARTBEAT_SECONDS):
            repo = _repo_open(run_dir)
            if repo is None:
                continue
            try:
                repo.heartbeat_session(session_id)
            except Exception:
                # A heartbeat is liveness metadata; it must not interrupt the
                # guarded run.  The file lock remains the same-host guard.
                pass
            finally:
                repo.close()
            if state is None or integrity_gate is None:
                continue
            lock = state.setdefault("_integrity_lease_lock", threading.Lock())
            with lock:
                lease = state.get("_active_integrity_lease")
                if lease is None:
                    continue
                try:
                    integrity_gate.heartbeat_pipeline_transition(run_dir, lease)
                except Exception as exc:
                    # The driver checks this marker before the next signed
                    # checkpoint and stops fail-closed.  A background thread
                    # cannot safely raise into the phase executing on main.
                    state.setdefault(
                        "_integrity_heartbeat_error",
                        f"{type(exc).__name__}: {exc}",
                    )

    thread = threading.Thread(target=_beat, name="citation-verifier-heartbeat", daemon=True)
    thread.start()
    return stop


def _code_revision() -> str | None:
    """Best-effort source revision recorded with a run for reproducibility."""
    if is_frozen():
        metadata = read_build_metadata()
        revision = metadata.get("revision") if metadata else None
        return revision.strip() if isinstance(revision, str) and revision.strip() else None
    try:
        root = str(resource_root())
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


_CODE_SNAPSHOT_PATHS = ("core", "run.py", "schema")


def _debug_code_snapshot_identity(revision: str | None) -> dict:
    """Fingerprint runtime-code changes for explicitly labelled debug runs.

    Normal runs retain the cheap HEAD-only provenance above.  Debug and frozen
    verification runs additionally hash tracked diffs plus untracked runtime
    source files, so two uncommitted experiments cannot masquerade as the same
    checkout merely because they share a commit.
    """
    root = str(resource_root())
    if is_frozen():
        metadata = read_build_metadata(root=root) or {}
        build_revision = metadata.get("revision") or revision or "unknown"
        dirty_note = (
            " The release metadata says its source checkout was modified."
            if metadata.get("dirty") is True else ""
        )
        return {
            "code_dirty": None,
            "code_diff_sha256": None,
            "code_snapshot_id": None,
            "code_snapshot_error": (
                "A packaged runtime has no Git source tree to fingerprint; "
                f"its recorded build revision is {build_revision}.{dirty_note}"
            ),
        }
    try:
        diff = subprocess.run(
            [
                "git", "diff", "--binary", "--no-ext-diff", "HEAD", "--",
                *_CODE_SNAPSHOT_PATHS,
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if diff.returncode != 0:
            raise RuntimeError(
                diff.stderr.decode("utf-8", errors="replace").strip()
                or "git diff failed"
            )
        untracked = subprocess.run(
            [
                "git", "ls-files", "-z", "--others", "--exclude-standard", "--",
                *_CODE_SNAPSHOT_PATHS,
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if untracked.returncode != 0:
            raise RuntimeError(
                untracked.stderr.decode("utf-8", errors="replace").strip()
                or "git ls-files failed"
            )

        digest = hashlib.sha256()
        digest.update(b"tracked-diff\0")
        digest.update(diff.stdout)
        untracked_paths = sorted(
            path for path in untracked.stdout.split(b"\0") if path
        )
        for raw_path in untracked_paths:
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            source_path = os.path.abspath(os.path.join(root, relative))
            if os.path.commonpath((root, source_path)) != root:
                raise RuntimeError(f"untracked code path escapes workspace: {relative}")
            digest.update(b"\0untracked-path\0")
            digest.update(raw_path)
            digest.update(b"\0untracked-content\0")
            with open(source_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)

        diff_sha256 = digest.hexdigest()
        snapshot_payload = json.dumps(
            {
                "revision": revision,
                "runtime_diff_sha256": diff_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "code_dirty": bool(diff.stdout or untracked_paths),
            "code_diff_sha256": diff_sha256,
            "code_snapshot_id": hashlib.sha256(snapshot_payload).hexdigest(),
        }
    except Exception as exc:
        return {
            "code_dirty": None,
            "code_diff_sha256": None,
            "code_snapshot_id": None,
            "code_snapshot_error": f"{type(exc).__name__}: {exc}",
        }


def _code_identity(*, debug_enabled: bool) -> dict:
    """Return cheap provenance normally and a content identity only in debug."""
    revision = _code_revision()
    identity = {"code_revision": revision}
    if debug_enabled:
        identity.update(_debug_code_snapshot_identity(revision))
    return identity


def _store_verify_runtime_setting(repository, runtime: dict) -> None:
    """Record startup labels, retaining a previously frozen Verify policy."""
    repository.set_run_setting("verify_runtime", runtime)
    if repository.get_run_setting("verify_claim_evidence_config") is not None:
        _sync_verify_runtime_setting(repository)


def _verify_table_citations_requested(args) -> bool:
    return bool(args.verify_table_citations) or (
        os.environ.get(
            parse_manuscript.ENV_VERIFY_TABLE_CITATIONS, ""
        ).strip().lower() in ("1", "true", "yes", "on")
    )


def _apply_resume_table_verification(st: dict, requested: bool) -> None:
    """Apply the opt-in only while a resumed run can still parse its input."""
    if not requested or st.get("verify_table_citations"):
        return
    if st.get("phase") != "parse":
        raise RuntimeError(
            "--verify-table-citations cannot change a run after parsing; "
            "start a fresh run (or resume a frozen pre-parse run)"
        )
    st["verify_table_citations"] = True


# A verification fork must retain the deterministic acquisition record, while
# deliberately not inheriting any model work.  Keeping this list here makes the
# boundary auditable and avoids a repository-level cloning API that could be
# accidentally reused for a different lifecycle.
_FROZEN_FETCH_COPY_TABLES = (
    # Parse facts.  Citation projections are Verify-derived and are
    # intentionally regenerated in the child.
    "manuscript_text",
    "manuscript_identity",
    "manuscript_identity_identifiers",
    "claims",
    "claim_marker_members",
    "reference_entries",
    "cited_bibliographic_coordinates",
    "cited_bibliographic_coordinate_spans",
    "operational_references",
    "footnote_notes",
    "footnote_note_sources",
    "footnote_note_parents",
    "claim_footnotes",
    "citations",
    "unresolved_citations",
    "unresolved_citation_candidates",
    "parse_table_citation_state",
    "parse_table_citation_markers",
    "parse_coverage_state",
    "parse_coverage_further_reading",
    "reference_identity",
    # Resolve facts and their typed children, parent before child.
    "resolve_results",
    "resolve_attempt_states",
    "resolve_attempts",
    "resolve_attempt_provider_details",
    "resolve_attempt_identifier_values",
    "resolve_attempt_matched_authors",
    "resolve_attempt_oa_license_urls",
    "resolve_attempt_fulltext_availability",
    "resolve_attempt_identity_searches",
    "resolve_attempt_resolved_identifiers",
    "resolve_attempt_trial_registrations",
    "resolve_attempt_metadata_matches",
    "resolve_attempt_metadata_coordinate_comparisons",
    "resolve_attempt_metadata_hard_conflicts",
    "resolve_attempt_metadata_ordinals",
    "resolve_attempt_fulltext_links",
    "resolve_attempt_link_contexts",
    "resolve_attempt_link_context_authors",
    "resolve_attempt_link_context_identifiers",
    "resolve_attempt_exceptions",
    "resolve_attempt_fetch_repairs",
    "resolve_attempt_fetch_repair_direct",
    "resolve_attempt_fetch_repair_execution",
    "resolve_trace_state",
    "resolve_trace_stages",
    "resolve_trace_identifier_details",
    "resolve_trace_reason_details",
    "resolve_trace_status_details",
    "resolve_trace_confirmed_details",
    "resolve_trace_weak_details",
    "resolve_trace_retraction_details",
    "resolve_trace_final_details",
    "resolve_fulltext_link_sets",
    "resolve_fulltext_links",
    "resolve_fulltext_link_provenance",
    "resolve_fulltext_link_contexts",
    "resolve_fulltext_link_context_authors",
    "resolve_fulltext_link_context_identifiers",
    "resolve_evidence_profile_states",
    "resolve_evidence_profiles",
    "resolve_evidence_journal_authorities",
    "resolve_evidence_journal_alias_assessments",
    "resolve_evidence_journal_alias_candidates",
    "resolve_evidence_bibliographic_suspicions",
    "resolve_evidence_bibliographic_suspicion_providers",
    "resolve_evidence_resolver_coverages",
    "resolve_evidence_resolver_coverage_catalogs",
    "resolve_evidence_resolver_coverage_payloads",
    "resolve_evidence_resolver_coverage_issns",
    "resolve_evidence_resolver_coverage_observations",
    "resolve_evidence_resolver_coverage_article_lookups",
    "resolve_evidence_issue_attestations",
    "resolve_evidence_issue_attestation_members",
    "resolve_evidence_issue_attestation_sources",
    "resolve_evidence_issue_attestation_observations",
    "resolve_evidence_bibliographic_adjudications",
    "resolve_evidence_bibliographic_refutations",
    "resolve_evidence_checks",
    "resolve_evidence_source_type_evidence",
    "resolve_evidence_metadata_matches",
    "resolve_evidence_metadata_coordinate_comparisons",
    "resolve_evidence_metadata_conflicts",
    "resolve_evidence_metadata_hard_conflicts",
    "resolve_evidence_metadata_ordinals",
    "resolve_evidence_best_candidates",
    "resolve_evidence_best_candidate_authors",
    "resolve_evidence_identifier_fallbacks",
    "resolve_evidence_identifier_fallback_authors",
    "resolve_evidence_identifier_fallback_licenses",
    "resolve_evidence_identifier_fallback_links",
    "resolve_evidence_risks",
    "resolve_evidence_risk_signals",
    "resolve_evidence_fulltext_availability",
    "resolve_evidence_exceptions",
    "resolve_evidence_repair_failed",
    "resolve_evidence_fetch_repairs",
    "resolve_evidence_abstract_dispositions",
    # Fetch attempts and their typed trace children.
    "fetch_attempts",
    "fetch_attempt_trace_states",
    # Frozen candidate plans are immutable Fetch facts.  Copy the complete
    # parent-before-child subgraph before traces that reference its candidates.
    "fetch_candidate_plans",
    "fetch_candidate_stage_freezes",
    "fetch_frozen_candidates",
    "fetch_frozen_candidate_strings",
    "fetch_frozen_candidate_contexts",
    "fetch_frozen_candidate_context_authors",
    "fetch_frozen_candidate_context_identifiers",
    "fetch_frozen_candidate_events",
    "fetch_execution_traces",
    "fetch_execution_headers",
    "fetch_execution_markers",
    "fetch_execution_parser_variants",
    "fetch_execution_parser_flags",
    "fetch_direct_text_traces",
    "fetch_provider_diagnostic_traces",
    "fetch_provider_direct_items",
    "fetch_provider_candidate_items",
    # Run-local evidence and its typed extraction-flag state.
    "source_texts",
    "source_text_extraction_flag_states",
    "source_text_extraction_flags",
    "unreadable_sources",
)
_FROZEN_FETCH_COPY_SINGLETON_TABLES = {
    "parse_table_citation_state",
    "parse_coverage_state",
}
_FROZEN_FETCH_SETTING_KEYS = (
    "mailto",
    "max_retries",
    "fetch_workers",
    "no_fetch",
    "autonomous",
    "ocr_lang",
    "style",
    "style_confidence",
    "config_snapshot",
    "verify_table_citations",
    "user_source_ingest",
)
_FROZEN_FETCH_DEBUG_LABEL = "fork:frozen_fetch_verify"
_FROZEN_FETCH_RUN_ORIGIN = "forked_from_frozen_fetch"
_COMPLETED_VERIFY_RUN_ORIGIN = "forked_from_completed_verify"
_REFERENCE_ONLY_FETCH_RUN_ORIGIN = "forked_from_reference_only"
_COMPLETED_REMEDIATION_RUN_ORIGIN = "remediated_from_completed"


_COMPLETED_PARSE_ADJUDICATION_COPY_TABLES = (
    # Only applied Parse-review task records cross this boundary.  In
    # particular, Fetch/Verify tasks and answers never do.
    "tasks",
    "task_manual_parse_review_details",
    "task_manual_parse_review_candidates",
    "task_answers",
    "task_manual_parse_review_answers",
    "task_manual_parse_review_split_sources",
    "task_answer_provenance",
    "task_answer_provenance_files",
    # These are the operational Parse overlays produced by an applied review.
    "manual_reference_identity_overrides",
    "manual_footnote_source_overrides",
    "manual_footnote_source_override_sources",
    "manual_citation_attribution_overrides",
    "manual_parse_review_applications",
)

_COMPLETED_SOURCE_IDENTITY_ATTESTATION_COPY_TABLES = (
    # Source-identity decisions are part of the frozen Fetch evidence layer.
    # Copy only decisions applied to source texts that cross into the child.
    "tasks",
    "task_source_identity_attestation_details",
    "task_answers",
    "task_source_identity_attestation_answers",
    "task_answer_provenance",
    "task_answer_provenance_files",
    "source_identity_attestation_decisions",
)


def _completed_parse_adjudication_select_sql(table: str) -> str:
    """Select exactly the applied Parse-adjudication subgraph for ``table``."""
    applied = """
        JOIN frozen.manual_parse_review_applications p
          ON p.task_id=t.task_id
        WHERE t.task_kind='manual_parse_review' AND t.status='applied'
    """
    if table == "tasks":
        return f"SELECT t.* FROM frozen.tasks t {applied}"
    if table == "task_manual_parse_review_details":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.tasks t ON t.task_id=x.task_id
            {applied}
        """
    if table == "task_manual_parse_review_candidates":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.tasks t ON t.task_id=x.task_id
            {applied}
        """
    if table in {
        "task_answers", "task_manual_parse_review_answers",
        "task_manual_parse_review_split_sources", "task_answer_provenance",
        "task_answer_provenance_files",
    }:
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.manual_parse_review_applications p ON p.answer_id=x.answer_id
            JOIN frozen.tasks t ON t.task_id=p.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
        """
    if table == "manual_parse_review_applications":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.tasks t ON t.task_id=x.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
        """
    if table in {
        "manual_reference_identity_overrides", "manual_footnote_source_overrides",
    }:
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.manual_parse_review_applications p ON p.answer_id=x.answer_id
            JOIN frozen.tasks t ON t.task_id=p.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
        """
    if table == "manual_footnote_source_override_sources":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.manual_footnote_source_overrides o ON o.note_id=x.note_id
            JOIN frozen.manual_parse_review_applications p ON p.answer_id=o.answer_id
            JOIN frozen.tasks t ON t.task_id=p.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
        """
    if table == "manual_citation_attribution_overrides":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.manual_parse_review_applications p
              ON p.task_id=x.task_id AND p.answer_id=x.answer_id
            JOIN frozen.tasks t ON t.task_id=p.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
        """
    raise RuntimeError(f"unsupported completed Parse table: {table}")


def _completed_source_identity_attestation_select_sql(table: str) -> str:
    """Select applied source-identity decisions for copied source texts only."""
    applied = """
        JOIN frozen.source_identity_attestation_decisions d ON d.task_id=t.task_id
        JOIN frozen.task_source_identity_attestation_details td ON td.task_id=t.task_id
        JOIN frozen.source_texts s ON s.source_text_id=td.source_text_id
        WHERE t.task_kind='source_identity_attestation' AND t.slot='fetch'
          AND t.status='applied'
    """
    if table == "tasks":
        return f"SELECT t.* FROM frozen.tasks t {applied}"
    if table == "task_source_identity_attestation_details":
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.tasks t ON t.task_id=x.task_id
            {applied}
        """
    if table in {
        "task_answers", "task_source_identity_attestation_answers",
        "task_answer_provenance", "task_answer_provenance_files",
    }:
        return f"""
            SELECT x.* FROM frozen.{table} x
            JOIN frozen.source_identity_attestation_decisions d
              ON d.answer_id=x.answer_id
            JOIN frozen.tasks t ON t.task_id=d.task_id
            JOIN frozen.task_source_identity_attestation_details td ON td.task_id=t.task_id
            JOIN frozen.source_texts s ON s.source_text_id=td.source_text_id
            WHERE t.task_kind='source_identity_attestation' AND t.slot='fetch'
              AND t.status='applied'
        """
    if table == "source_identity_attestation_decisions":
        return f"""
            SELECT d.* FROM frozen.{table} d
            JOIN frozen.tasks t ON t.task_id=d.task_id
            JOIN frozen.task_source_identity_attestation_details td ON td.task_id=t.task_id
            JOIN frozen.source_texts s ON s.source_text_id=td.source_text_id
            WHERE t.task_kind='source_identity_attestation' AND t.slot='fetch'
              AND t.status='applied'
        """
    raise RuntimeError(f"unsupported completed source identity table: {table}")


def _completed_parse_adjudication_snapshot(
    source_db: str, source_run_dir: str, files: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    """Return a canonical, authenticated snapshot of copied Parse adjudications."""
    del source_run_dir  # Files have already been authenticated by the caller.
    conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS frozen", (Path(source_db).resolve().as_uri() + "?mode=ro",))
        tables: dict[str, list[dict[str, object]]] = {}
        for table in _COMPLETED_PARSE_ADJUDICATION_COPY_TABLES:
            columns = [row[1] for row in conn.execute(
                f'PRAGMA frozen.table_info("{table}")'
            )]
            if not columns:
                raise RuntimeError(f"completed Parse source missing table: {table}")
            rows = []
            for row in conn.execute(_completed_parse_adjudication_select_sql(table)):
                record = dict(zip(columns, row, strict=True))
                for key, value in record.items():
                    if isinstance(value, bytes):
                        record[key] = {"bytes_hex": value.hex()}
                rows.append(record)
            tables[table] = sorted(
                rows,
                key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
            )
    finally:
        conn.close()
    snapshot: dict[str, object] = {
        "tables": tables,
        "authenticated_files": files,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return snapshot, hashlib.sha256(encoded).hexdigest()


def _completed_source_identity_attestation_snapshot(
    source_db: str, files: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    """Return a canonical snapshot of copied applied source attestations."""
    conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS frozen", (Path(source_db).resolve().as_uri() + "?mode=ro",))
        tables: dict[str, list[dict[str, object]]] = {}
        for table in _COMPLETED_SOURCE_IDENTITY_ATTESTATION_COPY_TABLES:
            columns = [row[1] for row in conn.execute(
                f'PRAGMA frozen.table_info("{table}")'
            )]
            if not columns:
                raise RuntimeError(f"completed source identity source missing table: {table}")
            rows = []
            for row in conn.execute(_completed_source_identity_attestation_select_sql(table)):
                record = dict(zip(columns, row, strict=True))
                for key, value in record.items():
                    if isinstance(value, bytes):
                        record[key] = {"bytes_hex": value.hex()}
                rows.append(record)
            tables[table] = sorted(
                rows,
                key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
            )
    finally:
        conn.close()
    snapshot: dict[str, object] = {
        "tables": tables,
        "authenticated_files": files,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return snapshot, hashlib.sha256(encoded).hexdigest()


def _completed_review_snapshot(
    source_db: str,
    parse_files: list[dict[str, object]],
    source_identity_files: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    """Digest every applied review subgraph that a completed Verify fork copies."""
    parse_snapshot, _parse_sha256 = _completed_parse_adjudication_snapshot(
        source_db, "", parse_files,
    )
    identity_snapshot, _identity_sha256 = _completed_source_identity_attestation_snapshot(
        source_db, source_identity_files,
    )
    snapshot: dict[str, object] = {
        "manual_parse": parse_snapshot,
        "source_identity_attestation": identity_snapshot,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return snapshot, hashlib.sha256(encoded).hexdigest()


def _frozen_source_inventory(repo, run_dir: str) -> tuple[list[dict], str]:
    """Return a verified source inventory and its content-addressed digest."""
    inventory = []
    for row in repo.list_source_texts():
        resolved = _run_relative_asset(
            run_dir, row.stored_path, required=True)
        assert resolved is not None
        _relative, source_path = resolved
        actual_sha256 = _sha256_file(source_path)
        if row.sha256 and actual_sha256 != row.sha256:
            raise RuntimeError(
                f"frozen-fetch source asset hash mismatch: {row.stored_path}"
            )
        inventory.append({
            "ref_id": row.ref_id,
            "tier": row.tier,
            "origin": row.origin,
            "stored_path": row.stored_path,
            "source_ref": row.source_ref,
            "sha256": actual_sha256,
            "char_count": row.char_count,
        })
    inventory.sort(key=lambda row: (
        row["ref_id"], row["tier"], row["stored_path"], row["sha256"]
    ))
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return inventory, hashlib.sha256(encoded).hexdigest()


def _completed_parse_adjudication_files(
    source_db: str, source_run_dir: str,
) -> list[dict[str, object]]:
    """Return and authenticate files attached to applied Parse reviews only."""
    conn = sqlite3.connect(Path(source_db).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT f.answer_id,f.file_order,f.original_name,f.stored_path,
                   f.sha256,f.byte_count
            FROM manual_parse_review_applications p
            JOIN tasks t ON t.task_id=p.task_id
            JOIN task_answers a ON a.answer_id=p.answer_id
            JOIN task_answer_provenance q ON q.answer_id=a.answer_id
            JOIN task_answer_provenance_files f ON f.answer_id=q.answer_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
              AND a.accepted_for_processing=1 AND a.generation=t.generation
              AND p.generation=t.generation
            ORDER BY f.answer_id,f.file_order
            """
        ).fetchall()
    finally:
        conn.close()

    assets: dict[str, dict[str, object]] = {}
    for row in rows:
        relative, path = _run_relative_asset(
            source_run_dir, row["stored_path"], required=True,
        ) or (None, None)
        if relative is None or path is None:  # pragma: no cover - required=True raises
            raise RuntimeError("manual Parse provenance file is unavailable")
        actual_sha256 = _sha256_file(path)
        byte_count = os.path.getsize(path)
        if actual_sha256 != row["sha256"] or byte_count != row["byte_count"]:
            raise RuntimeError(
                "manual Parse provenance file does not match its authenticated record"
            )
        prior = assets.get(relative)
        record = {
            "relative": relative,
            "sha256": actual_sha256,
            "byte_count": byte_count,
        }
        if prior is not None and prior != record:
            raise RuntimeError(
                "manual Parse provenance file has inconsistent authenticated records"
            )
        assets[relative] = record
    return [assets[key] for key in sorted(assets)]


def _completed_source_identity_attestation_files(
    source_db: str, source_run_dir: str,
) -> list[dict[str, object]]:
    """Return and authenticate files attached to copied identity attestations."""
    conn = sqlite3.connect(Path(source_db).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT f.answer_id,f.file_order,f.original_name,f.stored_path,
                   f.sha256,f.byte_count
            FROM source_identity_attestation_decisions d
            JOIN tasks t ON t.task_id=d.task_id
            JOIN task_source_identity_attestation_details td ON td.task_id=t.task_id
            JOIN source_texts s ON s.source_text_id=td.source_text_id
            JOIN task_answers a ON a.answer_id=d.answer_id
            JOIN task_answer_provenance q ON q.answer_id=a.answer_id
            JOIN task_answer_provenance_files f ON f.answer_id=q.answer_id
            WHERE t.task_kind='source_identity_attestation' AND t.slot='fetch'
              AND t.status='applied'
              AND a.accepted_for_processing=1 AND a.generation=t.generation
            ORDER BY f.answer_id,f.file_order
            """
        ).fetchall()
    finally:
        conn.close()

    assets: dict[str, dict[str, object]] = {}
    for row in rows:
        relative, path = _run_relative_asset(
            source_run_dir, row["stored_path"], required=True,
        ) or (None, None)
        if relative is None or path is None:  # pragma: no cover - required=True raises
            raise RuntimeError("source identity provenance file is unavailable")
        actual_sha256 = _sha256_file(path)
        byte_count = os.path.getsize(path)
        if actual_sha256 != row["sha256"] or byte_count != row["byte_count"]:
            raise RuntimeError(
                "source identity provenance file does not match its authenticated record"
            )
        record = {
            "relative": relative,
            "sha256": actual_sha256,
            "byte_count": byte_count,
        }
        prior = assets.get(relative)
        if prior is not None and prior != record:
            raise RuntimeError(
                "source identity provenance file has inconsistent authenticated records"
            )
        assets[relative] = record
    return [assets[key] for key in sorted(assets)]


def _regular_file_sha256(path: str, label: str) -> str:
    """Hash one non-symlink regular file, refusing ambiguous filesystem state."""
    if not isinstance(path, str) or not path:
        raise ValueError(f"completed remediation parent {label} path is missing")
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"completed remediation parent {label} must be a regular non-symlink file")
    return _sha256_file(path)


def _completed_remediation_seed(parent_run_dir: str, target_run_dir: str) -> dict:
    """Validate a completed parent before its Parse-only child is created.

    Nothing operational crosses this boundary: the child obtains only immutable
    configuration plus typed hashes proving exactly which completed parent was
    revalidated immediately before its creation.
    """
    parent_run_dir = os.path.abspath(parent_run_dir)
    target_run_dir = os.path.abspath(target_run_dir)
    if parent_run_dir == target_run_dir:
        raise ValueError("completed remediation target must differ from the parent run")
    if os.path.exists(target_run_dir):
        raise ValueError(f"completed remediation target already exists: {target_run_dir}")
    if not os.path.isfile(os.path.join(parent_run_dir, "run.sqlite")):
        raise ValueError(f"completed remediation parent has no run.sqlite: {parent_run_dir}")

    parent_repo = RunRepository.open_readonly(parent_run_dir)
    try:
        parent = parent_repo.get_run()
        if parent.status != "completed" or parent.phase != "done":
            raise ValueError("completed remediation requires a parent run with status completed and phase done")
        input_path = parent.input_path
        input_sha256 = _regular_file_sha256(input_path, "input")
        if input_sha256 != parent.input_sha256:
            raise ValueError("completed remediation parent input SHA-256 does not match its run record")
        report_sha256 = _regular_file_sha256(
            os.path.join(parent_run_dir, "report.md"), "report.md")
        journal_sha256 = _regular_file_sha256(
            os.path.join(parent_run_dir, "report.journal.md"), "report.journal.md")
        for source in parent_repo.list_source_texts():
            if not re.fullmatch(r"[0-9a-f]{64}", str(source.sha256 or "")):
                raise ValueError("completed remediation parent source asset has no valid registered SHA-256")
        inventory, inventory_sha256 = _frozen_source_inventory(parent_repo, parent_run_dir)
        for entry in inventory:
            if not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])):
                raise ValueError("completed remediation parent source inventory has invalid SHA-256")
        gate = verify_run.verify(parent_run_dir)
        if not gate.get("ok"):
            raise ValueError("completed remediation parent failed verify_run gate")
        # A parent is never mutated by this path.  Detect a concurrent edit
        # between the completion gate and child construction instead of
        # attaching provenance to an indeterminate snapshot.
        current_inventory, current_inventory_sha256 = _frozen_source_inventory(
            parent_repo, parent_run_dir)
        if (
            _regular_file_sha256(input_path, "input") != input_sha256
            or _regular_file_sha256(os.path.join(parent_run_dir, "report.md"), "report.md") != report_sha256
            or _regular_file_sha256(os.path.join(parent_run_dir, "report.journal.md"), "report.journal.md") != journal_sha256
            or current_inventory != inventory
            or current_inventory_sha256 != inventory_sha256
        ):
            raise ValueError("completed remediation parent changed during validation")

        settings = parent_repo.list_run_settings()
        config_snapshot = settings.get("config_snapshot")
        if not isinstance(config_snapshot, dict):
            raise ValueError("completed remediation parent has no valid configuration snapshot")
        return {
            "input": input_path,
            "input_sha256": input_sha256,
            "accuracy": parent.accuracy,
            "style": parent.style,
            "mailto": settings.get("mailto"),
            "model": parent.model_id,
            "max_retries": settings.get("max_retries", 2),
            "fetch_workers": settings.get("fetch_workers"),
            "ocr_lang": settings.get("ocr_lang") or DEFAULT_OCR_LANG,
            "http_profile": parent.http_profile or DEFAULT_HTTP_PROFILE,
            "challenge_mode": parent.challenge_mode or DEFAULT_CHALLENGE_MODE,
            "no_fetch": bool(settings.get("no_fetch")),
            "autonomous": bool(settings.get("autonomous")),
            "verify_table_citations": bool(settings.get("verify_table_citations")),
            "config_snapshot": config_snapshot,
            "parent_run_id": parent.run_id,
            "parent_run_dir": parent_run_dir,
            "run_origin": _COMPLETED_REMEDIATION_RUN_ORIGIN,
            "source_inventory_sha256": inventory_sha256,
            "parent_report_sha256": report_sha256,
            "parent_journal_sha256": journal_sha256,
        }
    finally:
        parent_repo.close()


def _run_relative_asset(
    run_dir: str,
    stored_path: str,
    *,
    required: bool,
) -> tuple[str, str] | None:
    """Resolve one regular in-run asset without following symlinks."""
    raw = str(stored_path or "").strip()
    if (
        not raw
        or os.path.isabs(raw)
        or re.match(r"^[A-Za-z]:[\\/]", raw)
        or "://" in raw
    ):
        if required:
            raise RuntimeError(f"frozen-fetch asset is not run-relative: {stored_path}")
        return None
    relative = os.path.normpath(
        raw.replace("\\", os.sep).replace("/", os.sep))
    if relative in ("", ".") or relative == ".." or relative.startswith(
        ".." + os.sep
    ):
        raise RuntimeError(f"frozen-fetch asset escapes the run: {stored_path}")
    root = os.path.abspath(run_dir)
    current = root
    for component in relative.split(os.sep):
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise RuntimeError(
                f"frozen-fetch asset uses a symlink: {stored_path}")
    resolved = os.path.abspath(os.path.join(root, relative))
    try:
        inside_run = os.path.commonpath((root, resolved)) == root
    except ValueError:
        inside_run = False
    if not inside_run or not os.path.isfile(resolved):
        if required:
            raise RuntimeError(
                f"frozen-fetch registered asset is missing: {stored_path}")
        return None
    return relative, resolved


def _copy_frozen_fetch_files(
    source_dir: str,
    target_dir: str,
    inventory: list[dict],
    unreadable_payload: dict,
    manual_parse_provenance_files: list[dict[str, object]] = (),
) -> list[str]:
    """Copy whitelisted acquisition assets, never a directory wholesale."""
    assets: dict[str, str] = {}
    for entry in inventory:
        resolved = _run_relative_asset(
            source_dir, entry.get("stored_path"), required=True)
        assert resolved is not None
        assets[resolved[0]] = resolved[1]
        source_ref = _run_relative_asset(
            source_dir, entry.get("source_ref"), required=False)
        if source_ref is not None:
            assets[source_ref[0]] = source_ref[1]
    for entry in (unreadable_payload or {}).get("entries", []):
        if entry.get("ocr_status") == "done" or not entry.get("kept_as"):
            continue
        resolved = _run_relative_asset(
            source_dir, entry.get("kept_as"), required=True)
        assert resolved is not None
        assets[resolved[0]] = resolved[1]
    for entry in manual_parse_provenance_files:
        resolved = _run_relative_asset(
            source_dir, str(entry["relative"]), required=True)
        assert resolved is not None
        actual_sha256 = _sha256_file(resolved[1])
        if (
            actual_sha256 != entry["sha256"]
            or os.path.getsize(resolved[1]) != entry["byte_count"]
        ):
            raise RuntimeError(
                "manual Parse provenance file changed during child creation"
            )
        assets[resolved[0]] = resolved[1]

    copied = []
    for relative, source_path in sorted(assets.items()):
        target_path = os.path.join(target_dir, relative)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied.append(relative.replace(os.sep, "/"))

    # The fetch response cache is an explicit acquisition cache rather than
    # source evidence. Copy only regular files and reject symlinked content.
    cache_root = os.path.join(source_dir, ".fetch-response-cache")
    if os.path.islink(cache_root):
        raise RuntimeError("frozen-fetch response cache is a symlink")
    if os.path.isdir(cache_root):
        for directory, dirnames, filenames in os.walk(
            cache_root, followlinks=False
        ):
            for name in list(dirnames):
                if os.path.islink(os.path.join(directory, name)):
                    raise RuntimeError(
                        "frozen-fetch response cache contains a symlink")
            for name in filenames:
                source_path = os.path.join(directory, name)
                if os.path.islink(source_path) or not os.path.isfile(source_path):
                    raise RuntimeError(
                        "frozen-fetch response cache contains a non-regular file")
                relative_cache = os.path.relpath(source_path, source_dir)
                target_path = os.path.join(target_dir, relative_cache)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(source_path, target_path)
                copied.append(relative_cache.replace(os.sep, "/"))
    return sorted(copied)


def _copy_frozen_fetch_metadata(
    source_db: str, target_db: str, *, copy_completed_parse_adjudications: bool,
) -> None:
    """Logically copy only parse/resolve/fetch relations into a fresh DB.

    This intentionally uses SQLite's relational copy, rather than copying a
    database file: only applied Parse reviews and source-identity attestations
    may cross the boundary; jury, verdict, pair-state, transition, and
    verification projections never do.
    """
    conn = sqlite3.connect(Path(target_db).resolve().as_uri(), uri=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        source_uri = f"{Path(source_db).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS frozen", (source_uri,))
        has_applied_parse_adjudications = conn.execute(
            """
            SELECT 1
            FROM frozen.manual_parse_review_applications p
            JOIN frozen.tasks t ON t.task_id=p.task_id
            WHERE t.task_kind='manual_parse_review' AND t.status='applied'
            LIMIT 1
            """
        ).fetchone() is not None
        if has_applied_parse_adjudications and not copy_completed_parse_adjudications:
            raise RuntimeError(
                "frozen-fetch fork cannot omit applied manual Parse adjudication"
            )
        has_applied_source_identity_attestations = conn.execute(
            """
            SELECT 1
            FROM frozen.source_identity_attestation_decisions d
            JOIN frozen.tasks t ON t.task_id=d.task_id
            JOIN frozen.task_source_identity_attestation_details td ON td.task_id=t.task_id
            JOIN frozen.source_texts s ON s.source_text_id=td.source_text_id
            WHERE t.task_kind='source_identity_attestation' AND t.slot='fetch'
              AND t.status='applied'
            LIMIT 1
            """
        ).fetchone() is not None
        if has_applied_source_identity_attestations and not copy_completed_parse_adjudications:
            raise RuntimeError(
                "frozen-fetch fork cannot omit applied source identity attestation"
            )
        with conn:
            for table in _FROZEN_FETCH_COPY_TABLES:
                columns = [row[1] for row in conn.execute(
                    f'PRAGMA main.table_info("{table}")'
                )]
                if not columns:
                    raise RuntimeError(f"fork target missing table: {table}")
                column_sql = ", ".join(f'"{column}"' for column in columns)
                if table in _FROZEN_FETCH_COPY_SINGLETON_TABLES:
                    mutable_columns = [column for column in columns if column != "singleton"]
                    assignments = ", ".join(
                        f'"{column}"=(SELECT "{column}" FROM frozen."{table}" WHERE singleton=1)'
                        for column in mutable_columns
                    )
                    cursor = conn.execute(
                        f'UPDATE main."{table}" SET {assignments} WHERE singleton=1 '
                        f'AND EXISTS(SELECT 1 FROM frozen."{table}" WHERE singleton=1)'
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"fork source or target missing singleton row: {table}"
                        )
                else:
                    conn.execute(
                        f'INSERT INTO main."{table}" ({column_sql}) '
                        f'SELECT {column_sql} FROM frozen."{table}"'
                    )
            if has_applied_parse_adjudications:
                for table in _COMPLETED_PARSE_ADJUDICATION_COPY_TABLES:
                    columns = [row[1] for row in conn.execute(
                        f'PRAGMA main.table_info("{table}")'
                    )]
                    if not columns:
                        raise RuntimeError(f"fork target missing table: {table}")
                    column_sql = ", ".join(f'"{column}"' for column in columns)
                    select_sql = _completed_parse_adjudication_select_sql(table)
                    conn.execute(
                        f'INSERT INTO main."{table}" ({column_sql}) '
                        f'SELECT {column_sql} FROM ({select_sql})'
                    )
            if has_applied_source_identity_attestations:
                for table in _COMPLETED_SOURCE_IDENTITY_ATTESTATION_COPY_TABLES:
                    columns = [row[1] for row in conn.execute(
                        f'PRAGMA main.table_info("{table}")'
                    )]
                    if not columns:
                        raise RuntimeError(f"fork target missing table: {table}")
                    column_sql = ", ".join(f'"{column}"' for column in columns)
                    select_sql = _completed_source_identity_attestation_select_sql(table)
                    conn.execute(
                        f'INSERT INTO main."{table}" ({column_sql}) '
                        f'SELECT {column_sql} FROM ({select_sql})'
                    )
        conn.execute("DETACH DATABASE frozen")
    finally:
        conn.close()


def _fork_frozen_fetch_verify(
    source_run_dir: str,
    target_run_dir: str,
    *,
    credential_inventory_at_start: list[dict[str, object]],
    execution_assurance=None,
    completed_parent: bool = False,
    reference_only_parent: bool = False,
) -> dict:
    """Create a provenance-safe child run at ``verify`` from a frozen fetch.

    The child has a new run id, a source-inventory fingerprint, and fresh
    projections/tasks generated by the currently checked-out code.  It is
    intentionally not a filesystem/database copy of a prior verification.
    """
    source_run_dir = os.path.abspath(source_run_dir)
    target_run_dir = os.path.abspath(target_run_dir)
    if completed_parent and reference_only_parent:
        raise ValueError("fork source cannot be both completed Verify and references-only")
    source_label = (
        "completed Verify parent" if completed_parent
        else "completed references-only parent" if reference_only_parent
        else "frozen-fetch baseline"
    )
    if source_run_dir == target_run_dir:
        raise ValueError(f"{source_label} fork target must differ from its source run")
    if not os.path.isfile(os.path.join(source_run_dir, "run.sqlite")):
        raise ValueError(f"{source_label} has no run.sqlite: {source_run_dir}")
    if os.path.exists(target_run_dir):
        raise ValueError(f"fork target already exists: {target_run_dir}")

    source_repo = RunRepository.open_readonly(source_run_dir)
    target_repo = None
    created_target = False
    try:
        source_run = source_repo.get_run()
        baseline_settings = source_repo.list_run_settings()
        if completed_parent:
            if source_run.status != "completed" or source_run.phase != "done":
                raise ValueError(
                    "completed Verify fork requires a parent run with status "
                    "completed and phase done"
                )
            gate = verify_run.verify(source_run_dir)
            if not gate.get("ok"):
                raise ValueError("completed Verify parent failed verify_run gate")
        elif reference_only_parent:
            if source_run.status != "completed" or source_run.phase != "done":
                raise ValueError(
                    "references-only Fetch fork requires a parent run with status "
                    "completed and phase done"
                )
            if not baseline_settings.get("references_only"):
                raise ValueError(
                    "references-only Fetch fork requires a references-only parent"
                )
        elif source_run.phase in ("parse", "resolve", "resolve_repair", "fetch"):
            raise ValueError(
                "frozen-fetch baseline has not completed fetch; choose a post-fetch run"
            )
        if not source_repo.list_claims() or not source_repo.list_references():
            raise ValueError("frozen-fetch baseline has no parsed claims/references")
        non_pending_verify_tasks = [
            task for task in source_repo.list_tasks(slot="verify")
            if task.status != "pending"
        ]
        if not completed_parent and (
            non_pending_verify_tasks
            or source_repo.verification_pair_state_payloads()
        ):
            raise ValueError(
                "frozen-fetch baseline already contains verification work; "
                "use the clean post-fetch snapshot"
            )
        unreadable_payload = source_repo.unreadable_payload()
        inventory, inventory_sha256 = _frozen_source_inventory(
            source_repo, source_run_dir)
        manual_parse_provenance_files = _completed_parse_adjudication_files(
            os.path.join(source_run_dir, "run.sqlite"), source_run_dir,
        ) if (completed_parent or reference_only_parent) else []
        source_identity_provenance_files = _completed_source_identity_attestation_files(
            os.path.join(source_run_dir, "run.sqlite"), source_run_dir,
        ) if (completed_parent or reference_only_parent) else []
        _completed_review, completed_review_sha256 = (
            _completed_review_snapshot(
                os.path.join(source_run_dir, "run.sqlite"),
                manual_parse_provenance_files,
                source_identity_provenance_files,
            )
            if (completed_parent or reference_only_parent) else ({}, "")
        )
        baseline_runtime = baseline_settings.get("verify_runtime") or {}
        baseline_revision = baseline_runtime.get("code_revision")
        fork_debug_mode = (
            bool(baseline_settings.get("debug_mode"))
            if (completed_parent or reference_only_parent) else True
        )
        fork_debug_labels = (
            list(baseline_settings.get("debug_labels") or ())
            if (completed_parent or reference_only_parent)
            else [_FROZEN_FETCH_DEBUG_LABEL]
        )
        fork_code_identity = _code_identity(debug_enabled=fork_debug_mode)
        run_origin = (
            _COMPLETED_VERIFY_RUN_ORIGIN
            if completed_parent else _FROZEN_FETCH_RUN_ORIGIN
        )
        if reference_only_parent:
            run_origin = _REFERENCE_ONLY_FETCH_RUN_ORIGIN

        os.makedirs(target_run_dir, exist_ok=False)
        created_target = True
        target_repo = source_repo.clone_as_fresh_run(
            new_run_dir=target_run_dir,
            new_run_id=os.path.basename(target_run_dir),
            run_origin=run_origin,
            execution_assurance=execution_assurance,
            preserve_model_id=not completed_parent,
        )
        # The fork is a new run, so it must use this process's startup
        # inventory rather than the frozen baseline's historical environment.
        # Persist it before any child work can make keyed transport calls.
        target_repo.write_credential_inventory(credential_inventory_at_start)
        target_repo.close()
        target_repo = None
        _copy_frozen_fetch_metadata(
            os.path.join(source_run_dir, "run.sqlite"),
            os.path.join(target_run_dir, "run.sqlite"),
            copy_completed_parse_adjudications=(
                completed_parent or reference_only_parent
            ),
        )
        copied_assets = _copy_frozen_fetch_files(
            source_run_dir,
            target_run_dir,
            inventory,
            unreadable_payload,
            manual_parse_provenance_files + source_identity_provenance_files,
        )
        current_parent_inventory, current_parent_inventory_sha256 = (
            _frozen_source_inventory(source_repo, source_run_dir)
        )
        if (
            current_parent_inventory != inventory
            or current_parent_inventory_sha256 != inventory_sha256
        ):
            raise RuntimeError(
                "verify fork parent source inventory changed during child creation"
            )
        if completed_parent or reference_only_parent:
            current_parent = source_repo.get_run()
            if (
                current_parent.status != "completed"
                or current_parent.phase != "done"
                or (
                    completed_parent
                    and not verify_run.verify(source_run_dir).get("ok")
                )
                or (
                    reference_only_parent
                    and not source_repo.get_run_setting("references_only")
                )
            ):
                raise RuntimeError(
                    f"{source_label} changed during child creation"
                )
            current_manual_parse_files = _completed_parse_adjudication_files(
                os.path.join(source_run_dir, "run.sqlite"), source_run_dir,
            )
            current_source_identity_files = _completed_source_identity_attestation_files(
                os.path.join(source_run_dir, "run.sqlite"), source_run_dir,
            )
            _current_completed_review, current_completed_review_sha256 = (
                _completed_review_snapshot(
                    os.path.join(source_run_dir, "run.sqlite"),
                    current_manual_parse_files,
                    current_source_identity_files,
                )
            )
            if current_completed_review_sha256 != completed_review_sha256:
                raise RuntimeError(
                    "completed Verify parent review evidence changed during child creation"
                )

        target_repo = RunRepository.open(target_run_dir)
        copied_inventory, copied_inventory_sha256 = _frozen_source_inventory(
            target_repo, target_run_dir)
        if (
            copied_inventory != inventory
            or copied_inventory_sha256 != inventory_sha256
        ):
            raise RuntimeError(
                "frozen-fetch source inventory changed while creating the verify fork"
            )
        if completed_parent or reference_only_parent:
            child_manual_parse_files = _completed_parse_adjudication_files(
                os.path.join(target_run_dir, "run.sqlite"), target_run_dir,
            )
            child_source_identity_files = _completed_source_identity_attestation_files(
                os.path.join(target_run_dir, "run.sqlite"), target_run_dir,
            )
            _child_completed_review, child_completed_review_sha256 = (
                _completed_review_snapshot(
                    os.path.join(target_run_dir, "run.sqlite"),
                    child_manual_parse_files,
                    child_source_identity_files,
                )
            )
            if child_completed_review_sha256 != completed_review_sha256:
                raise RuntimeError(
                    "completed Verify child review evidence does not match its parent"
                )
        copied_settings = {
            key: baseline_settings[key]
            for key in _FROZEN_FETCH_SETTING_KEYS
            if key in baseline_settings
        }
        provenance = {
            "baseline_run_id": source_run.run_id,
            "baseline_run_dir": source_run_dir,
            "baseline_code_revision": baseline_revision,
            "source_inventory": inventory,
            "source_inventory_sha256": inventory_sha256,
            "copied_assets": copied_assets,
            "baseline_verified_clean": not (
                completed_parent or reference_only_parent
            ),
        }
        for key in (
            "code_dirty", "code_diff_sha256", "code_snapshot_id",
            "code_snapshot_error",
        ):
            if key in baseline_runtime:
                provenance[f"baseline_{key}"] = baseline_runtime[key]
        provenance.update({
            f"fork_{key}": value for key, value in fork_code_identity.items()
        })
        child_phase = "fetch" if reference_only_parent else "verify"
        target_repo.update_run_phase(child_phase)
        target_repo.update_run_status("active")
        target_repo.update_run_settings({
            **copied_settings,
            "fetch_paused": False,
            "auto_fetch_attempted": (
                False if reference_only_parent
                else bool(baseline_settings.get("auto_fetch_attempted"))
            ),
            **({
                "no_fetch": False,
                "references_only": False,
                "verify_backends": os.environ.get(
                    "CITATION_VERIFIER_VERIFY_BACKENDS"
                ),
            } if reference_only_parent else {}),
            "debug_mode": fork_debug_mode,
            "debug_labels": fork_debug_labels,
            "frozen_fetch_provenance": provenance,
        })
        target_repo.append_phase_event(
            child_phase, "enter",
            payload={
                "run_origin": run_origin,
                "parent_run_id": source_run.run_id,
                "source_inventory_sha256": inventory_sha256,
            },
        )
        target_repo.close()
        target_repo = None

        # The task emission also writes citation projections, so both are
        # guaranteed to be derived from the code that creates the fork.
        state = _runtime_state_from_repo(target_run_dir) or {}
        state.update({
            "run_dir": target_run_dir,
            "phase": child_phase,
            "debug_mode": fork_debug_mode,
            "debug_labels": fork_debug_labels,
        })
        if reference_only_parent:
            state.update({"no_fetch": False, "references_only": False})
            _save_state(state)
            return {
                "run_dir": target_run_dir,
                "run_id": os.path.basename(target_run_dir),
                "parent_run_id": source_run.run_id,
                "source_inventory_sha256": inventory_sha256,
                "code_snapshot_id": fork_code_identity.get("code_snapshot_id"),
                "fetch_tasks_created": 0,
            }
        emitted = _emit_verify_tasks(state)
        _save_state(state)
        return {
            "run_dir": target_run_dir,
            "run_id": os.path.basename(target_run_dir),
            "parent_run_id": source_run.run_id,
            "source_inventory_sha256": inventory_sha256,
            "code_snapshot_id": fork_code_identity.get("code_snapshot_id"),
            "verify_tasks_created": int(emitted.get("created") or 0),
        }
    except Exception:
        if target_repo is not None:
            target_repo.close()
        if created_target:
            shutil.rmtree(target_run_dir, ignore_errors=True)
        raise
    finally:
        source_repo.close()


def _fork_completed_verify(
    source_run_dir: str,
    target_run_dir: str,
    *,
    credential_inventory_at_start: list[dict[str, object]],
    execution_assurance=None,
) -> dict:
    """Create a fresh Verify child from a completed run's frozen evidence."""
    return _fork_frozen_fetch_verify(
        source_run_dir,
        target_run_dir,
        credential_inventory_at_start=credential_inventory_at_start,
        execution_assurance=execution_assurance,
        completed_parent=True,
    )


def _fork_reference_only_fetch(
    source_run_dir: str,
    target_run_dir: str,
    *,
    credential_inventory_at_start: list[dict[str, object]],
    execution_assurance=None,
) -> dict:
    """Create a fresh Fetch child from completed references-only evidence."""
    return _fork_frozen_fetch_verify(
        source_run_dir,
        target_run_dir,
        credential_inventory_at_start=credential_inventory_at_start,
        execution_assurance=execution_assurance,
        reference_only_parent=True,
    )


def _claim_driver_session(
    st: dict, *, resumed: bool, crash_recovered: bool = False
) -> str:
    """Acquire the DB lease after the filesystem lock has been obtained."""
    repo = _repo_open(st["run_dir"])
    if repo is None:
        print(f"[lock] could not open run database: {st['run_dir']}", file=sys.stderr)
        sys.exit(RUN_LOCKED)
    try:
        session_id = repo.claim_driver_session(
            pid=os.getpid(), host=socket.gethostname(),
            stale_after_seconds=(
                0 if crash_recovered else _DRIVER_LEASE_STALE_SECONDS
            ),
        )
        if session_id is None:
            print(f"[lock] run already has a live DB driver lease: {st['run_dir']}", file=sys.stderr)
            sys.exit(RUN_LOCKED)
        event_type = "resume" if resumed else "enter"
        repo.append_phase_event(
            st.get("phase") or "parse",
            event_type,
            session_id=session_id,
            payload={"resumed_via": "core.run"} if resumed else {"created_via": "core.run"},
        )
    finally:
        repo.close()
    return session_id
    try:
        fh.close()
    except Exception:
        pass


def _ensure_resume_verification_integrity(run_dir: str) -> None:
    """Read-only fail-closed diagnosis for a resumed verification run.

    This deliberately does not change run status or settings: callers invoke
    it only after acquiring the driver lease, and a diagnosis must never turn
    a healthy run into ``failed`` before ownership is established.  The
    explicit recovery path is ``--fresh-start``, which creates a child run
    from the same baseline instead of resuming questionable state.
    """
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        violations = repo.list_verification_lifecycle_violations()
        if not violations:
            return
    finally:
        repo.close()
    labels = ", ".join(
        f"{row['code']}:{row['claim_id']}/{row['ref_id']}/{row['scope']}"
        for row in violations[:8]
    )
    extra = f" (+{len(violations) - 8} more)" if len(violations) > 8 else ""
    raise RuntimeError(
        "verification lifecycle integrity failed; this run cannot be resumed "
        f"safely ({labels}{extra}). Use the explicit safe override: start a "
        "child run with --fresh-start."
    )


def _end_driver_session(run_dir: str, session_id: str | None, *, status: str) -> None:
    """Close a claimed DB lease, including failures before heartbeat startup."""
    if not session_id:
        return
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        repo.end_session(session_id, status=status)
    finally:
        repo.close()


def _record_verification_infrastructure_failure(run_dir: str, exc: Exception) -> None:
    """Persist a fail-closed marker when authoritative terminal writes fail."""
    repo = _repo_open(run_dir)
    if repo is None:
        return
    try:
        repo.set_run_setting("verification_lifecycle_failure", {
            "kind": "infrastructure_error",
            "error_type": type(exc).__name__,
            "reason": str(exc)[:500],
        })
        repo.update_run_status("failed")
    finally:
        repo.close()


def _prompt(label, env_name):
    """Ask for a value only on an interactive terminal; otherwise return None (bypass)."""
    if not _stdin_is_interactive():
        return None
    try:
        v = input(f"  {label} not in env ({env_name}). "
                  "Enter a value, or press Enter to skip: ").strip()
    except EOFError:
        return None
    return v or None


def _preflight_content_store(run_dir: str) -> str:
    try:
        return content_store.preflight(run_dir)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        path = content_store.db_path(run_dir)
        raise RuntimeError(
            f"content store preflight failed for {path}: {exc}"
        ) from exc


def _startup_proceed_command(argv: list[str] | None = None) -> str:
    """Render the current pipeline command with explicit setup acknowledgement."""
    command_args = list(sys.argv[1:] if argv is None else argv)
    if "--proceed" not in command_args:
        command_args.append("--proceed")
    return run_command(*command_args)


def _print_startup_pause_summary(issues: list[dict]) -> None:
    """Repeat actionable preflight results at the end of console output."""
    blocking = [item for item in issues if item.get("blocking", True)]
    notices = [item for item in issues if not item.get("blocking", True)]

    print()
    print("STARTUP PAUSED — no run has been created.")
    if blocking:
        print("Setup items requiring acknowledgement:")
        for index, item in enumerate(blocking, start=1):
            print(f"{index}. {item['summary']}")
            print(f"   Action: {item['fix']}")
    if notices:
        print("Non-blocking notices:")
        for item in notices:
            print(f"- {item['summary']}")
            print(f"  Note: {item['fix']}")
    print("Next steps:")
    print("- Configure the items above, then run the original command again:")
    print(f"  {run_command('configure')}")
    print("- Or continue now with the providers currently available:")
    print(f"  {_startup_proceed_command()}")


def _resolve_config(args):
    """Resolve Phase 0 settings from flags → env → prompt → default. The three knobs —
    verification regime, contact email, Google Books key — live in the environment (a
    git-ignored .env) so they travel with you. Missing ones are asked for on a TTY, or
    bypassed (with a printed notice) when running non-interactively."""
    # Verification regime (accuracy grade).
    accuracy = args.accuracy or os.environ.get(ENV_ACCURACY)
    if accuracy and accuracy not in ACCURACY_CHOICES:
        print(f"Ignoring invalid {ENV_ACCURACY}={accuracy!r}", file=sys.stderr)
        accuracy = None
    if not accuracy:
        entered = _prompt("verification regime (maximum|standard|abstract|standard_web)", ENV_ACCURACY)
        accuracy = entered if entered in ACCURACY_CHOICES else DEFAULT_ACCURACY
    # Contact email for the polite pool (sent to Crossref/Europe PMC/Unpaywall).
    mailto = args.mailto or os.environ.get(ENV_MAILTO)
    cached_key_rows = _startup_preflight.apply_quarantine()
    http_profile = args.http_profile or os.environ.get(ENV_HTTP_PROFILE) or DEFAULT_HTTP_PROFILE
    if http_profile not in HTTP_PROFILE_CHOICES:
        print(f"Ignoring invalid {ENV_HTTP_PROFILE}={http_profile!r}", file=sys.stderr)
        http_profile = DEFAULT_HTTP_PROFILE
    challenge_mode = (args.challenge_mode or os.environ.get(ENV_CHALLENGE_MODE)
                      or DEFAULT_CHALLENGE_MODE)
    if challenge_mode not in CHALLENGE_MODE_CHOICES:
        print(f"Ignoring invalid {ENV_CHALLENGE_MODE}={challenge_mode!r}",
              file=sys.stderr)
        challenge_mode = DEFAULT_CHALLENGE_MODE
    ocr_lang = (getattr(args, "ocr_lang", None) or os.environ.get(ENV_OCR_LANG)
                or DEFAULT_OCR_LANG).strip()

    regime_note = {"standard": "full text preferred; abstract fallback on paywall",
                   "maximum": "full text only", "maximum_fallback": "full text; abstract "
                   "provisional when existence unknown", "abstract": "abstract is final tier",
                   "standard_web": "legacy alias of standard; third-party web pages are "
                   "not citation evidence"
                   }.get(accuracy, "")
    key_rows = []
    cached_envs = {row.get("env") for row in cached_key_rows}
    for row in cached_key_rows + _startup_preflight.key_status_rows():
        if row.get("status") == "absent" and row.get("env") in cached_envs:
            continue
        key_rows.append(row)
    _startup_preflight.quarantine_invalid_rows(key_rows)
    gbooks = bool(os.environ.get(ENV_GBOOKS))
    print(f"Regime: {accuracy} ({regime_note})")
    print(f"Contact email: {mailto or 'none - APIs may rate-limit (bypassed)'}")
    print(f"Google Books key: {_gbooks_config_status(gbooks, key_rows)}")
    print(f"HTTP profile: {http_profile}")
    print(f"Challenge mode: {challenge_mode}")
    print(f"OCR language: {ocr_lang} "
          "(manuscript scans OCR'd automatically; source scans on request)")
    print(
        "Fetch host concurrency: "
        f"{_fetch.fetch_host_concurrency_limit()} per host"
    )
    print(
        "Fetch host min interval: "
        f"{_fetch.fetch_host_min_interval():g}s"
    )
    pdf_deps = _check_pdf_deps()
    print(f"PDF backends: {pdf_deps['message']}")
    for row in key_rows:
        print(f"{row['label']} auth: {_startup_preflight.describe_row(row)}")
    runtime_env = {
        ENV_HTTP_PROFILE: http_profile,
        ENV_CHALLENGE_MODE: challenge_mode,
    }
    profile_rows = []
    try:
        from core.fetch import http_profiles as _http_profiles
        profile_rows = _http_profiles.status_rows(runtime_env)
    except Exception:
        pass
    if profile_rows:
        summary = " · ".join(
            f"{row['name']}={'selected' if row['selected'] else 'available'}"
            + (" (unloadable)" if not row["loadable"] else "")
            for row in profile_rows
        )
        print(f"HTTP profiles: {summary}")
    provider_rows = _fetch.fetch_provider_status_rows(mailto)
    if provider_rows:
        summary = " · ".join(
            f"{row['name']}={'on' if row['enabled'] else 'off'}"
            + (f" ({row['reason']})" if (not row["enabled"] and row.get("reason")) else "")
            for row in provider_rows
        )
        print(f"Fetch providers: {summary}")
    mode_rows = _fetch_modes.status_rows(runtime_env)
    if mode_rows:
        summary = " · ".join(
            f"{row['name']}={'selected' if row['selected'] else 'available'}"
            + (" (unloadable)" if not row["loadable"] else "")
            for row in mode_rows
        )
        print(f"Fetch modes: {summary}")
    issues = _config_issues(args, accuracy, mailto, gbooks, key_rows)
    _print_startup_check(issues)
    blocking_issues = [item for item in issues if item.get("blocking", True)]
    if blocking_issues and not args.proceed:
        if _stdin_is_interactive():
            try:
                ans = input(
                    "Continue with the providers currently available? "
                    "[y/N]: "
                ).strip().lower()
            except EOFError:
                _print_startup_pause_summary(issues)
                raise SystemExit(1)
            if ans not in ("y", "yes"):
                _print_startup_pause_summary(issues)
                raise SystemExit(1)
        else:
            _print_startup_pause_summary(issues)
            raise SystemExit(1)
    return accuracy, mailto, gbooks, http_profile, challenge_mode, ocr_lang


def main():
    """Run the pipeline, translating Ctrl+C into an operator-safe exit."""
    global _ACTIVE_RUN_DIR, _ACTIVE_DRIVER_SESSION, _ACTIVE_HEARTBEAT_STOP
    _ACTIVE_RUN_DIR = None
    _ACTIVE_DRIVER_SESSION = None
    _ACTIVE_HEARTBEAT_STOP = None
    _start_desktop_control_reader()
    try:
        return _main()
    except KeyboardInterrupt:
        _cleanup_interrupted_driver()
        return _report_interruption(_ACTIVE_RUN_DIR)


def _main():
    global _ACTIVE_RUN_DIR, _ACTIVE_DRIVER_SESSION, _ACTIVE_HEARTBEAT_STOP
    from core.app.cli import build_parser

    ap = build_parser(
        accuracy_choices=ACCURACY_CHOICES,
        env_accuracy=ENV_ACCURACY,
        default_accuracy=DEFAULT_ACCURACY,
        http_profile_choices=HTTP_PROFILE_CHOICES,
        env_http_profile=ENV_HTTP_PROFILE,
        default_http_profile=DEFAULT_HTTP_PROFILE,
        challenge_mode_choices=CHALLENGE_MODE_CHOICES,
        env_challenge_mode=ENV_CHALLENGE_MODE,
        default_challenge_mode=DEFAULT_CHALLENGE_MODE,
        env_ocr_lang=ENV_OCR_LANG,
        default_ocr_lang=DEFAULT_OCR_LANG,
    )
    args = ap.parse_args()
    # This is deliberately before configuration/preflight can quarantine a key:
    # it records only what was non-blank in the environment at process start.
    credential_inventory_at_start = capture_credential_inventory(os.environ)
    manual_review_ref_numbers = sorted(set(args.manual_review_ref_number or []))
    if any(number <= 0 for number in manual_review_ref_numbers):
        ap.error("--manual-review-ref-number must be a positive integer")
    if manual_review_ref_numbers:
        args.manual_review = True

    if args.guided_fetch and (not args.run or not args.resume):
        ap.error("--guided-fetch requires --run <run-dir> --resume")
    if args.guided_fetch and args.autonomous:
        ap.error("--guided-fetch cannot be combined with --autonomous")
    if args.remediate_completed and (args.status or args.json_only):
        ap.error("--remediate-completed cannot be combined with --status/--json-only")
    if args.status:
        if not args.run:
            ap.error("--status requires --run")
        snap = status_snapshot(args.run)
        if args.json_only:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print_status(snap)
        sys.exit(0)
    if args.json_only:
        ap.error("--json-only requires --status")
    if args.debug_override_artifact_integrity and not (
        args.debug_override_reason and args.debug_override_reason.strip()
    ):
        ap.error(
            "--debug-override-artifact-integrity requires --debug-override-reason"
        )
    if args.debug_override_reason and not args.debug_override_artifact_integrity:
        ap.error(
            "--debug-override-reason requires --debug-override-artifact-integrity"
        )
    integrity_gate: RunIntegrityGate | None = None
    derived_resolution = None

    activated_integrity_runs: set[str] = set()

    def activate_integrity_worker(run_dir: str) -> None:
        if integrity_gate is None:
            return
        canonical = os.path.realpath(os.path.abspath(run_dir))
        if canonical in activated_integrity_runs:
            return
        try:
            integrity_gate.activate_worker(run_dir)
        except IntegrityGateError:
            raise
        activated_integrity_runs.add(canonical)

    def integrity_preflight(run_dir: str) -> dict:
        if integrity_gate is None:
            return {}
        activate_integrity_worker(run_dir)
        integrity_gate.preflight(
            run_dir,
            debug_override=args.debug_override_artifact_integrity,
            override_reason=args.debug_override_reason,
        )
        return integrity_gate.preflight_content_store(
            run_dir,
            debug_override=args.debug_override_artifact_integrity,
            override_reason=args.debug_override_reason,
        )

    if args.fresh_start and not args.run:
        ap.error("--fresh-start requires --run")
    if args.fresh_start and args.resume:
        ap.error("--fresh-start cannot be combined with --resume")
    completed_remediation_seed = None
    if args.remediate_completed:
        if not args.run:
            ap.error("--remediate-completed requires --run <new-run-dir>")
        disallowed_remediation_flags = (
            "--input", "--resume", "--fresh-start", "--fork-frozen-fetch-verify",
            "--fork-completed-verify",
            "--freeze-after-fetch", "--accuracy", "--style", "--mailto", "--model",
            "--max-retries", "--no-fetch", "--autonomous", "--http-profile",
            "--challenge-mode", "--ocr-lang", "--verify-table-citations", "--proceed",
            "--ignore-missing", "--debug-override-artifact-integrity",
            "--debug-override-reason",
        )
        raw_arguments = tuple(sys.argv[1:])
        if any(
            item == flag or item.startswith(flag + "=")
            for item in raw_arguments for flag in disallowed_remediation_flags
        ):
            ap.error(
                "--remediate-completed accepts only --run, optional --agent-identity, "
                "and optional --manual-review/--manual-review-ref-number controls"
            )
        try:
            derived_resolution = derived_child_assurance(
                args.remediate_completed,
                args.agent_identity,
                mirror_audit_records=False,
            )
            integrity_gate = derived_resolution.gate
            completed_remediation_seed = _completed_remediation_seed(
                args.remediate_completed, args.run,
            )
        except (IntegrityGateError, ValueError, RuntimeError) as exc:
            ap.error(str(exc))
    fork_flags = (
        args.fork_frozen_fetch_verify,
        args.fork_completed_verify,
        args.fork_reference_only_fetch,
    )
    if sum(bool(value) for value in fork_flags) > 1:
        ap.error(
            "--fork-frozen-fetch-verify, --fork-completed-verify, and "
            "--fork-reference-only-fetch are mutually exclusive"
        )
    verify_fork_source = (
        args.fork_completed_verify or args.fork_frozen_fetch_verify
    )
    fork_source = args.fork_reference_only_fetch or verify_fork_source
    if fork_source:
        if args.fork_reference_only_fetch:
            verify_fork_flag = "--fork-reference-only-fetch"
            fork_builder = _fork_reference_only_fetch
        elif args.fork_completed_verify:
            verify_fork_flag = "--fork-completed-verify"
            fork_builder = _fork_completed_verify
        else:
            verify_fork_flag = "--fork-frozen-fetch-verify"
            fork_builder = _fork_frozen_fetch_verify
        if args.freeze_after_fetch:
            ap.error(
                f"--freeze-after-fetch cannot be combined with {verify_fork_flag}"
            )
        if not args.run:
            ap.error(f"{verify_fork_flag} requires --run <new-run-dir>")
        if args.resume or args.fresh_start or args.input:
            ap.error(
                f"{verify_fork_flag} cannot be combined with --resume, "
                "--fresh-start, or --input"
            )
        if args.fork_reference_only_fetch and (
            args.no_fetch or args.references_only
        ):
            ap.error(
                "--fork-reference-only-fetch cannot be combined with "
                "--no-fetch or --references-only"
            )
        try:
            derived_resolution = derived_child_assurance(
                fork_source,
                args.agent_identity,
                debug_override=args.debug_override_artifact_integrity,
                override_reason=args.debug_override_reason,
            )
            integrity_gate = derived_resolution.gate
            fork = fork_builder(
                fork_source,
                args.run,
                credential_inventory_at_start=list(credential_inventory_at_start),
                execution_assurance=derived_resolution.assurance,
            )
        except (IntegrityGateError, ValueError, RuntimeError) as exc:
            ap.error(str(exc))
        try:
            activate_integrity_worker(fork["run_dir"])
            if integrity_gate is not None:
                integrity_gate.enroll(fork["run_dir"])
        except IntegrityGateError as exc:
            try:
                derived_resolution = downgrade_after_authority_failure(
                    fork["run_dir"], derived_resolution.assurance, exc
                )
            except IntegrityGateError as confirmation_exc:
                ap.error(str(confirmation_exc))
            integrity_gate = derived_resolution.gate
        if args.fork_reference_only_fetch:
            _progress(
                "forked references-only evidence into Fetch child "
                f"{fork['run_dir']}"
            )
        else:
            _progress(
                "forked source evidence into verify child "
                f"{fork['run_dir']} ({fork['verify_tasks_created']} task(s))"
            )
        # Continue through the standard resume path so session ownership,
        # debug tracing, and autonomous/manual verification remain identical
        # to every other official run.
        args.resume = True

    parent_run_id = None
    parent_run_dir = None
    run_origin = "fresh"

    if completed_remediation_seed is not None:
        args.input = completed_remediation_seed["input"]
        args.accuracy = completed_remediation_seed["accuracy"]
        args.style = completed_remediation_seed["style"]
        args.mailto = completed_remediation_seed["mailto"]
        args.model = completed_remediation_seed["model"]
        args.max_retries = completed_remediation_seed["max_retries"]
        args.no_fetch = completed_remediation_seed["no_fetch"]
        args.autonomous = completed_remediation_seed["autonomous"]
        args.http_profile = completed_remediation_seed["http_profile"]
        args.challenge_mode = completed_remediation_seed["challenge_mode"]
        args.ocr_lang = completed_remediation_seed["ocr_lang"]
        args.verify_table_citations = completed_remediation_seed["verify_table_citations"]
        parent_run_id = completed_remediation_seed["parent_run_id"]
        parent_run_dir = completed_remediation_seed["parent_run_dir"]
        run_origin = completed_remediation_seed["run_origin"]

    if args.fresh_start:
        try:
            derived_resolution = derived_child_assurance(
                args.run,
                args.agent_identity,
                debug_override=args.debug_override_artifact_integrity,
                override_reason=args.debug_override_reason,
            )
        except IntegrityGateError as exc:
            ap.error(str(exc))
        integrity_gate = derived_resolution.gate
        seed = _fresh_start_seed(args.run)
        if not args.input:
            args.input = seed["input"]
        if args.accuracy is None:
            args.accuracy = seed["accuracy"]
        if args.style is None:
            args.style = seed["style"]
        if args.mailto is None:
            args.mailto = seed["mailto"]
        if args.model is None:
            args.model = seed["model"]
        if args.max_retries == 2:
            args.max_retries = seed["max_retries"]
        if not args.no_fetch:
            args.no_fetch = seed["no_fetch"]
        if not args.references_only:
            args.references_only = seed["references_only"]
        if not args.autonomous:
            args.autonomous = seed["autonomous"]
        if args.http_profile is None:
            args.http_profile = seed["http_profile"]
        if args.challenge_mode is None:
            args.challenge_mode = seed["challenge_mode"]
        if args.ocr_lang is None:
            args.ocr_lang = seed["ocr_lang"]
        parent_run_id = seed["parent_run_id"]
        parent_run_dir = seed["parent_run_dir"]
        run_origin = seed["run_origin"]

    resuming_existing = bool(args.resume or (args.run and not args.input))
    _run_lock_fh = None
    crash_recovered = False
    configure_debug_on_resume = False
    if resuming_existing:
        if not args.run:
            ap.error("--resume requires --run")
        resume_run_dir = os.path.abspath(args.run)
        _run_lock_fh = _acquire_run_lock(resume_run_dir)
        atexit.register(_release_run_lock, _run_lock_fh)
        try:
            resolved_assurance = resolve_existing(
                resume_run_dir, args.agent_identity,
                debug_override=args.debug_override_artifact_integrity,
                override_reason=args.debug_override_reason,
                activation_only=True,
            )
        except IntegrityGateError as exc:
            ap.error(str(exc))
        integrity_gate = resolved_assurance.gate
        if integrity_gate is not None:
            activated_integrity_runs.add(
                os.path.realpath(os.path.abspath(resume_run_dir))
            )
        if integrity_gate is not None:
            try:
                recovery = integrity_gate.recover_pipeline_transition(resume_run_dir)
            except IntegrityGateError as exc:
                try:
                    resolved_assurance = downgrade_after_authority_failure(
                        resume_run_dir, resolved_assurance.assurance, exc
                    )
                except IntegrityGateError as confirmation_exc:
                    ap.error(str(confirmation_exc))
                integrity_gate = resolved_assurance.gate
                recovery = {"status": "none", "recoveries": []}
            crash_recovered = recovery.get("status") == "recovered"
        try:
            integrity_preflight(resume_run_dir)
        except IntegrityGateError as exc:
            try:
                resolved_assurance = downgrade_after_authority_failure(
                    resume_run_dir, resolved_assurance.assurance, exc
                )
            except IntegrityGateError as confirmation_exc:
                ap.error(str(confirmation_exc))
            integrity_gate = resolved_assurance.gate
        st = _runtime_state_from_repo(resume_run_dir)
        if not st:
            ap.error(f"no sqlite run database found in {resume_run_dir}")
        _ACTIVE_RUN_DIR = st["run_dir"]
        # Resume normally restores the frozen run policy from SQLite.  An
        # explicit --no-fetch is a monotonic safety override for this resume
        # and must take effect before Fetch can replay any frozen candidate.
        if args.no_fetch:
            st["no_fetch"] = True
        if args.references_only:
            st["references_only"] = True
        if args.manual_review and not manual_review_ref_numbers and (
            st.get("phase") != "parse" or not st.get("parse_review_paused")
        ):
            ap.error("--manual-review may be resumed only while Parse review is paused")
        if manual_review_ref_numbers:
            if st.get("phase") != "parse" or not st.get("parse_review_paused"):
                ap.error("--manual-review-ref-number may be added only while Parse review is paused")
            st["manual_review"] = True
            st["manual_review_ref_numbers"] = sorted(set(
                (st.get("manual_review_ref_numbers") or []) + manual_review_ref_numbers
            ))
        if st.get("phase") in {"parse", "resolve", "fetch"}:
            try:
                _preflight_content_store(st["run_dir"])
            except RuntimeError as exc:
                ap.error(str(exc))
        # A paused run may have been created before debug was enabled.  Honor
        # the current debug directive on resume as well, so API tracing and
        # phase events are captured when reusing a frozen run baseline.
        if not st.get("debug_mode") and _debug_directives_from_parse()[0]:
            # Updating debug settings mutates the run database; defer it until
            # the signed startup mutation lease is open below.
            configure_debug_on_resume = True
        try:
            _apply_resume_table_verification(
                st, _verify_table_citations_requested(args)
            )
        except RuntimeError as exc:
            ap.error(str(exc))
    else:
        if not args.input:
            ap.error("provide --input <manuscript> to start, or --run --resume")
        if completed_remediation_seed is None:
            accuracy, mailto, gbooks, http_profile, challenge_mode, ocr_lang = _resolve_config(args)
        else:
            accuracy = completed_remediation_seed["accuracy"]
            mailto = completed_remediation_seed["mailto"]
            gbooks = completed_remediation_seed["config_snapshot"]["google_books_key_present"]
            http_profile = completed_remediation_seed["http_profile"]
            challenge_mode = completed_remediation_seed["challenge_mode"]
            ocr_lang = completed_remediation_seed["ocr_lang"]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = os.path.abspath(
            args.run if (args.run and not args.fresh_start) else os.path.join("runs", ts)
        )
        try:
            _preflight_content_store(run_dir)
        except RuntimeError as exc:
            ap.error(str(exc))
        try:
            resolved_assurance = (
                derived_resolution
                if derived_resolution is not None
                else new_assurance(run_dir, args.agent_identity)
            )
        except IntegrityGateError as exc:
            ap.error(str(exc))
        integrity_gate = resolved_assurance.gate
        if completed_remediation_seed is not None:
            try:
                final_seed = _completed_remediation_seed(
                    completed_remediation_seed["parent_run_dir"], run_dir,
                )
            except (ValueError, RuntimeError) as exc:
                ap.error(str(exc))
            immutable_seed_keys = (
                "input", "input_sha256", "accuracy", "style", "mailto", "model",
                "max_retries", "fetch_workers", "ocr_lang", "http_profile",
                "challenge_mode", "no_fetch", "autonomous", "verify_table_citations",
                "config_snapshot", "parent_run_id", "parent_report_sha256",
                "parent_journal_sha256", "source_inventory_sha256",
            )
            if any(final_seed[key] != completed_remediation_seed[key] for key in immutable_seed_keys):
                ap.error("completed remediation parent changed before child creation")
        child_input_sha256 = _sha256_file(os.path.abspath(args.input))
        if (
            completed_remediation_seed is not None
            and child_input_sha256 != completed_remediation_seed["input_sha256"]
        ):
            ap.error("completed remediation parent input changed before child creation")
        # DB-native run: SQLite is the system of record. Only `sources/` holds real
        # files on disk (parsed source .txt + provided originals); resolve results,
        # the verdict ledger and tasks all live in the database, no JSON scaffolding.
        if completed_remediation_seed is not None:
            try:
                # Keep the last target-collision check atomic: no concurrent
                # creator can be mistaken for this child before its DB exists.
                os.makedirs(run_dir, exist_ok=False)
                os.mkdir(os.path.join(run_dir, "sources"))
            except FileExistsError:
                ap.error(f"completed remediation target already exists: {run_dir}")
        else:
            os.makedirs(os.path.join(run_dir, "sources"), exist_ok=True)
        st = {
            "run_dir": run_dir, "input": os.path.abspath(args.input),
            "accuracy": accuracy, "style": args.style, "mailto": mailto,
            "model": args.model, "max_retries": args.max_retries,
            "fetch_workers": (
                completed_remediation_seed["fetch_workers"]
                if completed_remediation_seed is not None
                else (seed.get("fetch_workers") if args.fresh_start else (
                    _fetch_worker_count()
                    if (os.environ.get(ENV_FETCH_WORKERS) or "").strip() else None
                ))
            ),
            "http_profile": http_profile, "challenge_mode": challenge_mode,
            # Full-text retrieval is deterministic (Unpaywall/OA + the web-search provider),
            # so it runs in autonomous mode too — the model only judges retrieved text. Only
            # an explicit --no-fetch skips it.
            "no_fetch": args.no_fetch,
            "references_only": args.references_only,
            "verify_backends": (
                os.environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or None
            ),
            "ocr_lang": ocr_lang,
            # Off by default: a citation inside a benchmark row has no assertion to
            # check against its source. It is still counted for reference coverage
            # and reported — dropped from verification, never from the record.
            "verify_table_citations": (
                completed_remediation_seed["verify_table_citations"]
                if completed_remediation_seed is not None
                else _verify_table_citations_requested(args)
            ),
            "manual_review": bool(args.manual_review),
            "parse_review_paused": False,
            "manual_review_ref_numbers": manual_review_ref_numbers,
            "autonomous": args.autonomous, "phase": "parse", "created_at": _now(),
            "debug_mode": bool(args.debug_override_artifact_integrity),
            "debug_labels": (
                [DEBUG_INTEGRITY_LABEL]
                if args.debug_override_artifact_integrity
                else []
            ),
        }
        config_snapshot = {
            "accuracy": accuracy,
            "style": args.style,
            "mailto_provided": bool(mailto),
            "google_books_key_present": gbooks,
            "http_profile": http_profile,
            "challenge_mode": challenge_mode,
            "fetch_workers": st["fetch_workers"],
            "autonomous": args.autonomous,
            "ocr_lang": ocr_lang,
            "model": args.model,
            "verify_table_citations": st["verify_table_citations"],
            "fresh_start_from": parent_run_dir,
        }
        if completed_remediation_seed is not None:
            # A remediation child is deliberately not reconfigured from the
            # ambient environment: its immutable configuration is the parent
            # snapshot that was just authenticated with the completed run.
            config_snapshot = completed_remediation_seed["config_snapshot"]
        repo = RunRepository.create(
            run_dir,
            run_id=os.path.basename(run_dir),
            input_path=os.path.abspath(args.input),
            input_sha256=child_input_sha256,
            accuracy=accuracy,
            style=args.style,
            model_id=args.model,
            http_profile=http_profile,
            challenge_mode=challenge_mode,
            fixture_fingerprint=_fixture_fingerprint(),
            parent_run_id=parent_run_id,
            run_origin=run_origin,
            execution_assurance=resolved_assurance.assurance,
        )
        _ACTIVE_RUN_DIR = st["run_dir"]
        repo.write_credential_inventory(list(credential_inventory_at_start))
        if completed_remediation_seed is not None:
            repo.write_completed_remediation_provenance({
                "parent_run_id": completed_remediation_seed["parent_run_id"],
                "parent_input_sha256": completed_remediation_seed["input_sha256"],
                "parent_report_sha256": completed_remediation_seed["parent_report_sha256"],
                "parent_journal_sha256": completed_remediation_seed["parent_journal_sha256"],
                "source_inventory_sha256": completed_remediation_seed["source_inventory_sha256"],
                "created_at": _now(),
            })
        repo.update_run_settings({
            "mailto": mailto,
            "max_retries": args.max_retries,
            "fetch_workers": st["fetch_workers"],
            "no_fetch": args.no_fetch,
            "references_only": args.references_only,
            "verify_backends": (
                os.environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or None
            ),
            "autonomous": args.autonomous,
            "ocr_lang": ocr_lang,
            "fetch_paused": False,
            "manual_review": bool(args.manual_review),
            "parse_review_paused": False,
            "manual_review_ref_numbers": manual_review_ref_numbers,
            "auto_fetch_attempted": False,
            "config_snapshot": config_snapshot,
        })
        repo.close()
        _save_state(st)
        content_integrity = {}
        if integrity_gate is not None:
            try:
                activate_integrity_worker(st["run_dir"])
                integrity_gate.enroll(st["run_dir"])
                content_integrity = integrity_gate.preflight_content_store(
                    st["run_dir"],
                    debug_override=args.debug_override_artifact_integrity,
                    override_reason=args.debug_override_reason,
                )
            except IntegrityGateError as exc:
                try:
                    resolved_assurance = downgrade_after_authority_failure(
                        st["run_dir"], resolved_assurance.assurance, exc
                    )
                except IntegrityGateError as confirmation_exc:
                    ap.error(str(confirmation_exc))
                integrity_gate = resolved_assurance.gate
        if content_integrity.get("debug_mode"):
            refreshed = _runtime_state_from_repo(st["run_dir"])
            st["debug_mode"] = bool(refreshed and refreshed.get("debug_mode"))
            st["debug_labels"] = (
                refreshed.get("debug_labels") if refreshed else []
            )

    # Guard against two concurrent driver invocations (start or --resume) processing
    # the same run dir at once — exits with RUN_LOCKED if another process already
    # holds it. Released automatically at process exit (atexit) even on sys.exit()
    # from deeper in main(), e.g. the --autonomous branch below.
    st["freeze_after_fetch"] = bool(args.freeze_after_fetch)
    if _run_lock_fh is None:
        _run_lock_fh = _acquire_run_lock(st["run_dir"])
        atexit.register(_release_run_lock, _run_lock_fh)

    startup_integrity_lease = None
    startup_integrity_pending = {"open": False}
    if integrity_gate is not None:
        try:
            startup_integrity_lease = integrity_gate.begin_transition(
                st["run_dir"], checkpoint_kind="phase_boundary"
            )
            startup_integrity_pending["open"] = True
        except IntegrityGateError as exc:
            try:
                resolved_assurance = downgrade_after_authority_failure(
                    st["run_dir"], resolved_assurance.assurance, exc
                )
            except IntegrityGateError as confirmation_exc:
                ap.error(str(confirmation_exc))
            integrity_gate = resolved_assurance.gate

    def _abort_unfinished_startup_transition() -> None:
        if not startup_integrity_pending["open"]:
            return
        try:
            assert integrity_gate is not None
            integrity_gate.abort_transition(
                st["run_dir"],
                startup_integrity_lease,
                reason="driver startup exited before checkpoint",
            )
        except IntegrityGateError:
            pass

    atexit.register(_abort_unfinished_startup_transition)

    if configure_debug_on_resume:
        _configure_debug_mode(st)

    # The filesystem lock protects normal same-host execution.  Claim the
    # durable lease only *after* that lock succeeds, so a rejected concurrent
    # resume cannot create an apparently-live DB session.  The DB lease also
    # protects us if a client deletes/recreates the lock path by mistake.
    st["db_session_id"] = _claim_driver_session(
        st,
        resumed=resuming_existing,
        crash_recovered=crash_recovered,
    )
    _ACTIVE_DRIVER_SESSION = (st["run_dir"], st["db_session_id"])
    _save_state(st)
    if resuming_existing:
        try:
            _ensure_resume_verification_integrity(st["run_dir"])
        except RuntimeError as exc:
            # The integrity diagnosis runs after ownership is acquired, but
            # before the heartbeat/finalizer exists.  Release that lease
            # explicitly so an immediate safe retry is not reported RUN_LOCKED.
            _end_driver_session(
                st["run_dir"], st.get("db_session_id"), status="interrupted"
            )
            st.pop("db_session_id", None)
            _save_state(st)
            if integrity_gate is not None:
                try:
                    integrity_gate.commit_transition(
                        st["run_dir"], startup_integrity_lease
                    )
                    startup_integrity_pending["open"] = False
                except IntegrityGateError as integrity_exc:
                    ap.error(str(integrity_exc))
            ap.error(str(exc))
    _heartbeat_stop = _start_driver_heartbeat(
        st["run_dir"],
        st["db_session_id"],
        state=st,
        integrity_gate=integrity_gate,
    )
    _ACTIVE_HEARTBEAT_STOP = _heartbeat_stop

    os.environ[ENV_HTTP_PROFILE] = st.get("http_profile") or DEFAULT_HTTP_PROFILE
    os.environ[ENV_CHALLENGE_MODE] = st.get("challenge_mode") or DEFAULT_CHALLENGE_MODE
    persisted_verify_backends = (st.get("verify_backends") or "").strip()
    if persisted_verify_backends and not (
        os.environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or ""
    ).strip():
        os.environ["CITATION_VERIFIER_VERIFY_BACKENDS"] = persisted_verify_backends

    autonomous = args.autonomous or st.get("autonomous")
    repo = _repo_open(st["run_dir"])
    semantic_contract = ClaimEvidenceRuntime.contract_id
    if repo is not None:
        try:
            persisted_semantic_contract = repo.get_run_setting("verify_semantic_contract")
            try:
                semantic_contract = ClaimEvidenceRuntime.select_contract(
                    persisted_semantic_contract, os.environ
                )
            except ValueError as exc:
                ap.error(f"invalid verify semantic contract: {exc}")
            if persisted_semantic_contract is None:
                repo.set_run_setting("verify_semantic_contract", semantic_contract)
            _store_verify_runtime_setting(repo, {
                **_code_identity(debug_enabled=bool(st.get("debug_mode"))),
                "backend": os.environ.get("CITATION_VERIFIER_VERIFY_BACKENDS"),
                "model": st.get("model") or os.environ.get("CITATION_VERIFIER_MODEL"),
                "reasoning": os.environ.get("CITATION_VERIFIER_REASONING"),
                "reasoning_effort": os.environ.get("CITATION_VERIFIER_REASONING_EFFORT"),
                "context_profile": os.environ.get("CITATION_VERIFIER_CONTEXT_PROFILE") or "large",
                "max_source_chars": os.environ.get("CITATION_VERIFIER_MAX_SOURCE_CHARS") or "",
                "require_fulltext": (os.environ.get("CITATION_VERIFIER_REQUIRE_FULLTEXT") or "").strip().lower()
                    in ("1", "true", "yes", "on"),
                "semantic_contract": semantic_contract,
            })
        finally:
            repo.close()

    if integrity_gate is not None:
        try:
            integrity_gate.commit_transition(st["run_dir"], startup_integrity_lease)
            startup_integrity_pending["open"] = False
        except IntegrityGateError as exc:
            _heartbeat_stop.set()
            _end_driver_session(
                st["run_dir"], st.get("db_session_id"), status="failed"
            )
            ap.error(str(exc))

    def _finish_driver_session(result_code):
        global _ACTIVE_DRIVER_SESSION, _ACTIVE_HEARTBEAT_STOP
        _heartbeat_stop.set()
        _ACTIVE_HEARTBEAT_STOP = None
        if result_code == 0:
            final_status = "completed"
        elif result_code == ACTION_REQUIRED:
            final_status = "interrupted"
        elif result_code in (GATE_FAILED, 2):
            final_status = "failed"
        else:
            final_status = "interrupted"
        _end_driver_session(
            st["run_dir"], st.get("db_session_id"), status=final_status
        )
        _ACTIVE_DRIVER_SESSION = None
        if result_code == ACTION_REQUIRED:
            repo = _repo_open(st["run_dir"])
            if repo is not None:
                try:
                    repo.update_run_status("paused")
                finally:
                    repo.close()

    # Verify execution is owned by the final facade inside ``phase_verify`` in
    # every mode. ``--autonomous`` only records intent and changes pause output.
    st["_execution_integrity_gate"] = integrity_gate
    if autonomous:
        rc = None
        try:
            rc = drive(st, integrity_gate=integrity_gate)
        finally:
            _finish_driver_session(rc)
        sys.exit(rc)

    rc = None
    try:
        rc = drive(st, integrity_gate=integrity_gate)
    finally:
        _finish_driver_session(rc)
    guided_result = _maybe_run_guided_fetch(
        st,
        rc,
        autonomous=False,
        guided_fetch_requested=args.guided_fetch,
        agent_identity=args.agent_identity,
    )
    if guided_result is not None:
        rc = guided_result
    sys.exit(rc)


if __name__ == "__main__":
    main()
