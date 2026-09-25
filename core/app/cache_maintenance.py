# core/app/cache_maintenance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted removal of parsed texts from the cross-run reuse cache."""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

from core.fetch.storage import content_store
from core.infra.db import RunRepository
from core.infra.integrity import IntegrityGateError, RunIntegrityGate


def _inactive_run_required(run_dir: str) -> None:
    repo = RunRepository.open_readonly(run_dir)
    try:
        if repo.get_active_session() is not None:
            raise RuntimeError(
                "cache maintenance is unavailable while the selected run is active"
            )
    finally:
        repo.close()


def _environment_gate(
    run_dir: str,
    *,
    environ: dict[str, str] | None = None,
) -> RunIntegrityGate | None:
    values = os.environ if environ is None else environ
    if not str(values.get("CITATION_VERIFIER_INTEGRITY_SOCKET") or "").strip():
        return None
    gate = RunIntegrityGate.from_environment(values)
    gate.activate_worker(run_dir)
    gate.preflight(run_dir)
    gate.preflight_content_store(run_dir)
    return gate


def deactivate_reusable_texts(
    run_dir: str,
    parsed_text_ids: Sequence[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    gate_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Deactivate selected cached texts, or every reusable text when ids is None.

    The operation deliberately keeps cached bytes and user-library originals.
    It changes only reuse eligibility, so existing run evidence and provenance
    remain available. When an integrity authority protects the shared store,
    its coordinated run/content-store transition encloses the mutation.
    """
    run_dir = os.path.realpath(os.path.abspath(run_dir))
    _inactive_run_required(run_dir)
    identifiers = None if parsed_text_ids is None else list(parsed_text_ids)
    gate = (
        gate_factory(run_dir)
        if gate_factory is not None
        else _environment_gate(run_dir, environ=environ)
    )
    lease = None
    if gate is not None:
        lease = gate.begin_pipeline_transition(
            run_dir,
            checkpoint_kind="external_input",
            mutates_content_store=True,
        )
    mutation_completed = False
    try:
        affected = content_store.deactivate_parsed_texts(
            run_dir,
            identifiers,
            environ=environ,
        )
        mutation_completed = True
        if gate is not None:
            gate.commit_pipeline_transition(run_dir, lease)
    except IntegrityGateError:
        # A failed checkpoint after the mutation must remain open for the
        # authority's normal crash recovery. Aborting it would bless the old
        # manifest even though the store already contains the completed update.
        if gate is not None and lease is not None and not mutation_completed:
            gate.abort_pipeline_transition(
                run_dir,
                lease,
                reason="cache maintenance did not start",
            )
        raise
    except BaseException:
        if gate is not None and lease is not None:
            try:
                gate.abort_pipeline_transition(
                    run_dir,
                    lease,
                    reason="cache maintenance did not complete",
                )
            except IntegrityGateError:
                pass
        raise
    return {
        "run_dir": run_dir,
        "deactivated": affected,
        "scope": "all" if identifiers is None else "selected",
        "files_deleted": 0,
        "user_originals_preserved": True,
    }
