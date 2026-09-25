#!/usr/bin/env python3
# core/app/phases/resolve.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase: resolve — existence + abstract retrieval, deterministic + web.

Resolve references (DOI, PubMed, ISBN lookup) and optionally perform inline
fetch repair for weak / unresolved references.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import math
import os
import time

from core.app.runtime.credentials import _print_phase_credential_warnings
from core.app.runtime.fetch_audit import _record_fetch_attempts
from core.app.runtime.repository import (
    _load_parse_payload,
    _now,
    _repo_open,
    _repo_sync_resolve_result,
)
from core.app.runtime.resolve_failures import (
    _repair_exception_result,
    _repair_failed_result,
    _resolve_exception_result,
)
from core.app.runtime.settings import _progress, _resolve_worker_count
from core.app.runtime.sources import (
    _fetch_evidence_payload,
    _ref_has_source_tier,
    _repair_abstract_payload,
    _store_resolve_abstract_if_needed,
    _suppress_repair_abstract_entries,
)

from core.resolve import service as _resolve
from core.fetch import service as _fetch
from core.app import pipeline as _pipeline
from core.infra import perf as _perf
from core.parse.footnotes import cross_reference_map, manuscript_pointer_ids
from core.resolve import manuscript_identity as _manuscript_identity
from core.resolve import sources as _resolve_sources
from core.resolve import transport_telemetry


_DISCOVERY_UNIT_GROUP = "resolve_discovery"
# Keep a durable boundary within the observed OneDrive process window.  Five
# references retain provider batching while allowing a checkpoint before a
# short-lived runner terminates a large resolve phase.
_DISCOVERY_CHECKPOINT_CHUNK_SIZE = 5


def _resolve_manuscript_title(run: str) -> None:
    """Resolve the uploaded manuscript's title once from Parse-owned evidence."""
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        identity = repo.manuscript_identity_payload()
        manuscript_text = repo.get_manuscript_text() or ""
    finally:
        repo.close()
    if (
        identity is None
        or identity.get("resolution_status") != "not_attempted"
        or not identity.get("identifiers")
    ):
        return
    from core.resolve import transport_telemetry

    with transport_telemetry.bind_run(
        run, manuscript_input_sha256=identity["input_sha256"],
    ):
        update = _manuscript_identity.resolve_manuscript_identity(
            identity,
            manuscript_text,
            resolve_ref=_resolve.resolve,
            probe_document=_resolve_sources.manuscript_title_identity_probe,
        )
    if update is None:
        return
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        # Re-read the state at the write boundary so a resumed invocation never
        # overwrites a completed manuscript identity decision.
        current = repo.manuscript_identity_payload()
        if current is not None and current.get("resolution_status") == "not_attempted":
            repo.set_manuscript_identity_resolution(update)
    finally:
        repo.close()


def _phase_resolve_progress(total: int, completed: int, tick: int) -> None:
    if total <= 0:
        return
    if completed == 1 or completed == total or completed % tick == 0:
        _progress(f"resolving reference {completed}/{total}")


def _progress_deferred_wait(wait: float, ref: dict, *, pending: int, stage: str) -> None:
    """Make an intentional provider cooldown visible instead of looking hung."""
    seconds = max(1, math.ceil(wait))
    label = ref.get("ref_number") or ref.get("id") or "unknown"
    _progress(
        f"Semantic Scholar cooldown: reference {label} deferred during {stage}; "
        f"retrying in {seconds}s ({pending} deferred)"
    )


def _prime_phase_batch_sessions(
    run: str,
    real_refs: list[dict],
    *,
    openalex_batch_session=None,
    semantic_scholar_batch_session=None,
    pubmed_batch_session=None,
    pmc_idconv_batch_session=None,
    prime_pmc_idconv=False,
) -> None:
    """Prime declared identifiers for this pass before worker resolution.

    Priming only fills provider-local transport caches.  It intentionally does
    not call Resolve or produce a logical provider attempt for any reference.
    """
    if not real_refs or not any((
        openalex_batch_session,
        semantic_scholar_batch_session,
        pubmed_batch_session,
        pmc_idconv_batch_session,
    )):
        return
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        fulltext_ids = {
            row.ref_id for row in repo.list_source_texts()
            if row.tier == "fulltext"
        }
        resolved_ids = {row.ref_id for row in repo.list_resolve_results()}
    finally:
        repo.close()
    refs = [
        ref for ref in real_refs
        if ref["id"] not in fulltext_ids or ref["id"] not in resolved_ids
    ]
    if not refs:
        return
    from core.resolve import transport_telemetry

    with transport_telemetry.bind_run(run):
        if openalex_batch_session is not None:
            _resolve.prime_openalex_batch_session(openalex_batch_session, refs)
        if semantic_scholar_batch_session is not None:
            _resolve.prime_semantic_scholar_batch_session(
                semantic_scholar_batch_session, refs,
            )
        if pubmed_batch_session is not None:
            _resolve.prime_pubmed_batch_session(pubmed_batch_session, refs)
        if prime_pmc_idconv and pmc_idconv_batch_session is not None:
            _resolve.prime_pmc_idconv_batch_session(pmc_idconv_batch_session, refs)


def _bind_batch_sessions(
    stack: contextlib.ExitStack,
    *,
    openalex_batch_session=None,
    semantic_scholar_batch_session=None,
    pubmed_batch_session=None,
    scholar_archive_circuit_session=None,
    pmc_idconv_batch_session=None,
) -> None:
    if openalex_batch_session is not None:
        stack.enter_context(_resolve.bind_openalex_batch_session(openalex_batch_session))
    if semantic_scholar_batch_session is not None:
        stack.enter_context(_resolve.bind_semantic_scholar_batch_session(semantic_scholar_batch_session))
    if pubmed_batch_session is not None:
        stack.enter_context(_resolve.bind_pubmed_batch_session(pubmed_batch_session))
    if scholar_archive_circuit_session is not None:
        stack.enter_context(_resolve.bind_scholar_archive_circuit_session(scholar_archive_circuit_session))
    if pmc_idconv_batch_session is not None:
        stack.enter_context(_resolve.bind_pmc_idconv_batch_session(pmc_idconv_batch_session))


def _validate_discovery_state(state: dict, *, ref_id: str | None = None) -> str:
    if type(state) is not dict:
        raise ValueError("invalid resolve discovery state")
    if set(state) == {"result", "attempts"}:
        if (
            type(state["result"]) is not dict
            or type(state["attempts"]) is not list
            or any(type(attempt) is not dict for attempt in state["attempts"])
        ):
            raise ValueError("invalid resolve discovery state")
        return "discovered"
    if set(state) == {"exception_result"}:
        result = state["exception_result"]
        if (
            type(result) is not dict
            or result.get("status") != "unresolved"
            or result.get("via") != "resolver_exception"
            or (ref_id is not None and result.get("ref_id") != ref_id)
        ):
            raise ValueError("invalid resolve discovery exception state")
        return "exception"
    raise ValueError("invalid resolve discovery state")


def _discovery_payload(state: dict) -> dict:
    _validate_discovery_state(state)
    return {"state": state}


def _load_discovery_snapshots(run: str) -> dict[str, dict]:
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        rows = repo.list_integrity_unit_completions(_DISCOVERY_UNIT_GROUP)
        final_ids = {row.ref_id for row in repo.list_resolve_results()}
    finally:
        repo.close()
    snapshots = {}
    for row in rows:
        ref_id, payload = row["unit_id"], row["payload"]
        if ref_id in final_ids:
            # Final resolve output is authoritative; an earlier phase snapshot
            # must never override it on a subsequent invocation.
            continue
        if type(payload) is not dict or set(payload) != {"state"}:
            raise RuntimeError("invalid persisted resolve discovery payload")
        _validate_discovery_state(payload["state"], ref_id=ref_id)
        snapshots[ref_id] = payload["state"]
    return snapshots


def _final_resolve_ids(run: str) -> set[str]:
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        return {row.ref_id for row in repo.list_resolve_results()}
    finally:
        repo.close()


def _persist_discovery_wave(st: dict, run: str, states: list[tuple[dict, dict]]) -> None:
    """Persist a completed discovery wave after every worker has finished.

    A checkpoint is deliberately not written from worker threads: completion
    rotates the run lease, and doing that while sibling transport activity is
    in flight would split its audit boundary.
    """
    if not states:
        return
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        for ref, state in states:
            repo.append_integrity_unit_completion(
                _DISCOVERY_UNIT_GROUP, ref["id"], _discovery_payload(state),
            )
    finally:
        repo.close()
    checkpoint = st.get("_integrity_unit_checkpoint")
    if callable(checkpoint):
        checkpoint(_DISCOVERY_UNIT_GROUP, "wave")


def _discover_reference(
    run: str, ref: dict, *,
    openalex_batch_session=None, semantic_scholar_batch_session=None,
    pubmed_batch_session=None,
    scholar_archive_circuit_session=None,
    pmc_idconv_batch_session=None,
) -> dict:
    from core.resolve import transport_telemetry
    with _perf.span("resolve", "discovery"):
        try:
            with transport_telemetry.bind_run(run, ref_id=ref["id"]):
                with contextlib.ExitStack() as stack:
                    _bind_batch_sessions(
                        stack,
                        openalex_batch_session=openalex_batch_session,
                        semantic_scholar_batch_session=semantic_scholar_batch_session,
                        pubmed_batch_session=pubmed_batch_session,
                        scholar_archive_circuit_session=scholar_archive_circuit_session,
                        pmc_idconv_batch_session=pmc_idconv_batch_session,
                    )
                    return _resolve._discover_resolution(ref)
        except _resolve._http.ProviderCooldownDeferred:
            raise
        except Exception as exc:
            # Preserve the old per-reference failure boundary.  One unexpected
            # resolver error must not abort discovery for the whole manuscript.
            return {"exception_result": _resolve_exception_result(ref, exc)}


def _finalize_reference_discovery(ref: dict, state: dict) -> dict:
    # These two keyed spans are additive.  Together they replace the single
    # public resolve span on the split batch path without pretending that the
    # barrier wait belongs to an individual reference.
    with _perf.span("resolve", "finalization"):
        return _resolve._finalize_discovery(ref, state)


def _finalize_resumed_semantic_scholar(ref: dict, resumed: dict) -> dict:
    """Normalize a cooldown continuation to a final Resolve payload.

    Discovery continuations intentionally resume to the discovery boundary
    (``{result, attempts}``).  The phase persistence boundary accepts only a
    finalized resolution carrying both attempts and a trace.  Enrichment and
    serial continuations already return that final payload and pass through.
    """
    if type(resumed) is dict and set(resumed) == {"result", "attempts"}:
        _validate_discovery_state(resumed, ref_id=ref["id"])
        return _finalize_reference_discovery(ref, resumed)
    return resumed


def _resolve_reference_pipeline(
    st: dict,
    run: str,
    ref: dict,
    fetch_context,
    *,
    openalex_batch_session=None,
    semantic_scholar_batch_session=None,
    pubmed_batch_session=None,
    scholar_archive_circuit_session=None,
    pmc_idconv_batch_session=None,
    resolution: dict | None = None,
    discovery_state: dict | None = None,
    semantic_scholar_deferred=None,
) -> dict:
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        existing_resolution = repo.get_resolve_result(ref["id"])
        if existing_resolution is not None:
            existing_sources = repo.list_source_texts(ref["id"])
            has_fulltext = any(s.tier == "fulltext" for s in existing_sources)
            if has_fulltext:
                return {}
            # Resolution exists but no fulltext stored — conditions may have
            # changed since the last run (OCR installed, new OA copies, resolver
            # may have been wrong, user may have provided sources, etc.).
            # Fall through to re-resolve.  Downstream gates
            # (_cache_tiers_for_resolution, _should_fetch, evidence_payload)
            # will naturally avoid wasted fetches when fulltext is genuinely
            # unavailable.
    finally:
        repo.close()
    processed = None
    persisted_resolution = None
    persisted_trace_sections = None
    discovery_kind = (
        _validate_discovery_state(discovery_state, ref_id=ref["id"])
        if discovery_state is not None else None
    )
    if discovery_kind == "exception":
        res = dict(discovery_state["exception_result"])
    else:
        try:
            if resolution is None:
                from core.resolve import transport_telemetry
                with transport_telemetry.bind_run(run, ref_id=ref["id"]):
                    # Thread-local binding keeps phase-scoped accelerators and
                    # circuits confined to this paper while workers run concurrently.
                    with contextlib.ExitStack() as stack:
                        _bind_batch_sessions(
                            stack,
                            openalex_batch_session=openalex_batch_session,
                            semantic_scholar_batch_session=semantic_scholar_batch_session,
                            pubmed_batch_session=pubmed_batch_session,
                            scholar_archive_circuit_session=scholar_archive_circuit_session,
                            pmc_idconv_batch_session=pmc_idconv_batch_session,
                        )
                        if semantic_scholar_deferred is not None:
                            resolution = _resolve.resume_semantic_scholar_cooldown(
                                semantic_scholar_deferred
                            )
                            if (
                                isinstance(resolution, dict)
                                and resolution.get("_provider_deferred_cancelled") is True
                            ):
                                return resolution
                            resolution = _finalize_resumed_semantic_scholar(ref, resolution)
                        else:
                            resolution = (
                                _finalize_reference_discovery(ref, discovery_state)
                                if discovery_state is not None else _resolve.resolve(ref)
                            )
        except _resolve._http.ProviderCooldownDeferred as deferred:
            # No terminal result, trace, or checkpoint is emitted for an
            # admission that did not send HTTP. The phase scheduler owns this
            # paper-local continuation.
            return {"_semantic_scholar_deferred": deferred}
        except Exception as e:
            res = _resolve_exception_result(ref, e)
        else:
            # Resolve is a completed, independently valuable phase. Persist it
            # before entering the long/retryable fetch window so KeyboardInterrupt,
            # SystemExit, or process termination cannot erase completed work.
            if resolution:
                persisted_trace_sections = _repo_sync_resolve_result(
                    run, ref["id"], resolution, trace_state="produced",
                )
                persisted_resolution = resolution
            try:
                with _perf.span("fetch", "landing"):
                    processed = _pipeline.process_source(
                        ref,
                        run,
                        deps=_pipeline.ProcessSourceDeps(
                            resolve_ref=lambda _ref, _resolution=resolution: _resolution,
                            fetch_fulltext=_fetch.fetch_fulltext,
                            mailto=st.get("mailto"),
                            fetch_context=fetch_context,
                            allow_inline_fulltext=not (
                                st.get("no_fetch") or st.get("accuracy") == "abstract"
                            ),
                        ),
                    )
            except Exception as e:
                processed = _pipeline.ProcessSourceResult(
                    resolution=resolution,
                    fetched={
                        "status": "error",
                        "method": "auto",
                        "reason": f"{type(e).__name__}: {e}",
                    },
                    cached_source=None,
                )
            res = processed.resolution
    if not res:
        return {}
    if res != persisted_resolution:
        trace_state = (
            "trace_not_produced"
            if processed is None and res.get("via") == "resolver_exception"
            else "produced"
        )
        trace_sections = _repo_sync_resolve_result(
            run, ref["id"], res, trace_state=trace_state,
        )
    else:
        trace_sections = persisted_trace_sections
    if processed is not None and processed.fetched is not None:
        fetched = dict(processed.fetched)
        fetched["ref_id"] = ref["id"]
        fetched["ref_number"] = ref.get("ref_number")
        _record_fetch_attempts(run, ref["id"], fetched, res)
    with _perf.span("fetch", "repair"):
        res = _maybe_inline_resolve_repair(
            st,
            ref,
            res,
            fetch_context=fetch_context,
        )
    stored_abstract, abstract_origin, abstract_signal, abstract_score = _store_resolve_abstract_if_needed(run, ref, res)
    # Verification tasks are emitted by phase_verify. Keeping this phase free
    # of task creation means an interrupted resolve cannot expose verify work
    # before the state machine has actually entered VERIFY.
    return {}


def _record_manuscript_pointer(st: dict, run: str, ref: dict) -> None:
    """Record a note that points at the manuscript's own prose, without a lookup.

    `status` stays "unverified" — nothing has been verified, and this must never
    read as a verdict.  What changes is the REASON: the note is not an external
    source that went missing, so `fabrication_risk` is "low" rather than the
    "unknown" a failed search leaves behind, and no missing work is implied.
    """
    _repo_sync_resolve_result(run, ref["id"], {
        "status": "unverified",
        "resolution_basis": "manuscript_pointer",
        "via": "manuscript_pointer",
        "reason": ("points at a section of this manuscript, not at an external "
                   "source; nothing to resolve"),
        "reference_status_tag": "manuscript_pointer",
        "fabrication_risk": "low",
        "existence_confidence": "not_applicable",
        "fulltext_exists": False,
        "attempts": [],
        "checked_at": _now(),
    }, trace_state="trace_not_produced")


def _inherit_cross_reference(
    st: dict, run: str, ref: dict, antecedent_id: str, antecedent_number
) -> bool:
    """Copy the antecedent's persisted resolve result onto a back-reference note.

    Returns True when the inheritance was performed (a resolve result now
    exists for *ref*, with resolution_basis="cross_reference"). Returns False
    when the antecedent has no persisted resolve result at all, in which case
    the caller must fall back to resolving *ref* through the normal pipeline
    -- never fabricate a resolution.
    """
    repo = _repo_open(run)
    if repo is None:
        raise SystemExit(f"no sqlite run database found in {run}")
    try:
        rec = repo.get_resolve_result(antecedent_id)
    finally:
        repo.close()
    if rec is None:
        return False
    inherited = {
        "status": rec.status,
        "matched_title": rec.matched_title,
        "abstract": rec.abstract,
        "abstract_via": rec.abstract_via,
        "retracted": rec.retracted,
        "fulltext_exists": rec.fulltext_exists,
        "oa_status": rec.oa_status,
        "work_type": rec.work_type,
        "existence_confidence": rec.existence_confidence,
        "reference_status_tag": rec.reference_status_tag,
        "fabrication_risk": rec.fabrication_risk,
        "fulltext_links": rec.fulltext_links,
        "auxiliary_fulltext_links": rec.auxiliary_fulltext_links,
        "evidence_profile": rec.evidence_profile,
        "attempts": rec.attempts,
        "resolution_basis": "cross_reference",
        "via": f"cross_reference:note_{antecedent_number}",
        "reason": f"back-reference to note {antecedent_number}; resolution inherited from it",
    }
    _repo_sync_resolve_result(
        run, ref["id"], inherited, trace_state="trace_not_produced",
    )
    return True


# --------------------------------------------------------------------------- #
#  Phase: resolve (existence + abstract retrieval, deterministic + web)        #
# --------------------------------------------------------------------------- #

def phase_resolve(st):
    _progress("resolving references (DOI, PubMed, ISBN lookup)")
    phase_started_at = _now()
    run = st["run_dir"]
    _resolve_sources.reconcile_materialized_texts(run)
    parse = _load_parse_payload(run)
    refs_list = parse.get("references", [])
    if "debug_mode" not in st:
        repo = _repo_open(run)
        if repo is not None:
            try:
                st["debug_mode"] = bool(repo.get_run_setting("debug_mode", False))
                st["debug_labels"] = repo.get_run_setting("debug_labels", []) or []
            finally:
                repo.close()
    _resolve.set_contact(st.get("mailto") or os.environ.get("CITATION_VERIFIER_MAILTO"))
    _resolve_manuscript_title(run)
    # A completed Resolve phase is durable.  On resume, Fetch consumes this
    # persisted map directly, so do not re-enter provider resolution (and its
    # inline repair work) merely because none of the references has fulltext
    # yet.  The check is deliberately phase-wide: a partial result must still
    # follow the existing path so every missing parsed reference is resolved.
    parsed_ref_ids = {ref["id"] for ref in refs_list}
    if parsed_ref_ids and parsed_ref_ids.issubset(_final_resolve_ids(run)):
        st["_fetch_run_context"] = _fetch.new_fetch_run_context()
        return "fetch"
    total = len(refs_list)
    # Back-references ("Id. at 96.", "X, supra note 10, at 331.") are not
    # standalone sources -- they inherit their antecedent's resolution
    # (Phase 2). Partition so antecedents resolve first; the map already
    # flattens chains to a real note, so a single serial pass over xref_refs
    # after all real_refs are done is enough to guarantee ordering.
    xmap = cross_reference_map(refs_list)
    # A note pointing at one of the manuscript's own sections ("See supra Section
    # II.B.3.a.") names no work at all.  Resolving it means searching the web for a
    # string that cannot exist: the lookup always fails, the note is left looking
    # like a source that went missing, and the request still counts against the
    # rate limits the real references depend on -- sixteen such notes in one
    # law-review paper, whose removal took its OpenAlex 429s from 155 to 10.
    pointer_ids = manuscript_pointer_ids(refs_list)
    num_by_id = {r["id"]: r.get("ref_number") for r in refs_list}
    real_refs = [r for r in refs_list
                 if r["id"] not in xmap and r["id"] not in pointer_ids]
    xref_refs = [r for r in refs_list if r["id"] in xmap]
    fetch_context = _fetch.new_fetch_run_context()
    session_stack = contextlib.ExitStack()
    try:
        openalex_batch_session = _resolve.new_openalex_batch_session()
        if openalex_batch_session is not None:
            session_stack.callback(openalex_batch_session.close)
            register_lookup = getattr(fetch_context, "register_provider_record_lookup", None)
            matched_record = getattr(openalex_batch_session, "matched_record", None)
            if callable(register_lookup) and callable(matched_record):
                register_lookup("openalex", matched_record)
        semantic_scholar_batch_session = _resolve.new_semantic_scholar_batch_session()
        if semantic_scholar_batch_session is not None:
            session_stack.callback(semantic_scholar_batch_session.close)
        pubmed_batch_session = _resolve.new_pubmed_batch_session()
        if pubmed_batch_session is not None:
            session_stack.callback(pubmed_batch_session.close)
        scholar_archive_circuit_session = _resolve.new_scholar_archive_circuit_session()
        session_stack.callback(scholar_archive_circuit_session.close)
        pmc_idconv_batch_session = _resolve.new_pmc_idconv_batch_session()
        if pmc_idconv_batch_session is not None:
            session_stack.callback(pmc_idconv_batch_session.close)
        batch_sessions_active = any((
            openalex_batch_session,
            semantic_scholar_batch_session,
            pubmed_batch_session,
            pmc_idconv_batch_session,
        ))
        _prime_phase_batch_sessions(
            run,
            real_refs,
            openalex_batch_session=openalex_batch_session,
            semantic_scholar_batch_session=semantic_scholar_batch_session,
            pubmed_batch_session=pubmed_batch_session,
            pmc_idconv_batch_session=pmc_idconv_batch_session,
        )
        snapshots = _load_discovery_snapshots(run) if real_refs else {}
        final_ids = (
            _final_resolve_ids(run)
            if real_refs and batch_sessions_active else set()
        )
        discovery_refs = [
            ref for ref in real_refs
            if ref["id"] not in snapshots and ref["id"] not in final_ids
        ] if batch_sessions_active else []
        discovered = []
        # Each provider has an independent FIFO.  A Semantic Scholar cooldown
        # must never make another provider's continuation ineligible.
        deferred_discovery: dict[str, list[tuple[float, int, dict, object]]] = {}
        discovery_order = 0
        # Process-local only: a restart starts a new Resolve attempt.  The
        # provider endpoint, not a reference, owns its cooldown episode: a
        # late reference inherits the initial deadline and cannot extend it.
        deferred_first_suspended_at: dict[str, float] = {}
        deferred_physical_429s: dict[str, int] = {}
        deferred_physical_429_tokens: dict[str, set[object]] = {}

        def track_cooldown(provider, exc):
            now = time.monotonic()
            first = deferred_first_suspended_at.setdefault(provider, now)
            physical = getattr(exc, "physical_429_count", 0)
            tokens = getattr(exc, "physical_429_tokens", ())
            if isinstance(tokens, tuple) and tokens:
                seen = deferred_physical_429_tokens.setdefault(provider, set())
                physical = sum(token not in seen for token in tokens)
                seen.update(tokens)
            if isinstance(physical, int) and not isinstance(physical, bool) and physical > 0:
                deferred_physical_429s[provider] = deferred_physical_429s.get(provider, 0) + physical
            if deferred_physical_429s.get(provider, 0) >= 3:
                return now
            return min(exc.not_before, first + 600.0)

        def cooldown_exhaustion_reason(provider, now):
            first = deferred_first_suspended_at.get(provider, now)
            if now - first >= 600.0:
                return "rate_limit_exhausted: Resolve cooldown exceeded 600 seconds"
            if deferred_physical_429s.get(provider, 0) >= 3:
                return (
                    "rate_limit_exhausted: provider returned more than two retry "
                    "HTTP 429 responses"
                )
            return None

        def provider_wake_at(provider, queue, now):
            """Return the FIFO head wake-up, advancing a spent episode now."""
            if cooldown_exhaustion_reason(provider, now) is not None:
                return now
            return queue[0][0]

        def deferred_entries(deferred):
            if isinstance(deferred, _resolve.ProviderDeferredWork):
                return tuple((provider, task["exc"]) for provider, task in deferred.tasks.items())
            return ((deferred.provider, deferred),)

        def remember_discovery(ref, state):
            nonlocal discovery_order
            if isinstance(state, _resolve._http.ProviderCooldownDeferred):
                for provider, exc in deferred_entries(state):
                    deferred_discovery.setdefault(provider, []).append(
                        (track_cooldown(provider, exc), discovery_order, ref, state)
                    )
                    discovery_order += 1
            else:
                discovered.append((ref, state))

        def resume_discovery_due():
            """Resume only continuations whose shared admission is due."""
            due = []
            now = time.monotonic()
            for provider, queue in list(deferred_discovery.items()):
                if queue and provider_wake_at(provider, queue, now) <= now:
                    due.append((provider, queue[0]))
            for provider, (_not_before, _order, ref, deferred) in due:
                reason = cooldown_exhaustion_reason(provider, now)
                if reason is not None:
                    try:
                        with transport_telemetry.bind_run(run, ref_id=ref["id"]):
                            state = _resolve.exhaust_provider_cooldown(
                                deferred, provider, reason=reason,
                            )
                    except _resolve._http.ProviderCooldownDeferred:
                        deferred_discovery[provider].pop(0)
                        if not deferred_discovery[provider]:
                            deferred_discovery.pop(provider, None)
                        continue
                    deferred_discovery[provider].pop(0)
                    if not deferred_discovery[provider]:
                        deferred_discovery.pop(provider, None)
                    remember_discovery(ref, state)
                    continue
                with transport_telemetry.bind_run(run, ref_id=ref["id"]):
                    with contextlib.ExitStack() as stack:
                        _bind_batch_sessions(
                            stack, openalex_batch_session=openalex_batch_session,
                            semantic_scholar_batch_session=semantic_scholar_batch_session,
                            pubmed_batch_session=pubmed_batch_session,
                            scholar_archive_circuit_session=scholar_archive_circuit_session,
                            pmc_idconv_batch_session=pmc_idconv_batch_session,
                        )
                        try:
                            state = _resolve.resume_semantic_scholar_cooldown(deferred, provider)
                        except _resolve._http.ProviderCooldownDeferred as next_deferred:
                            if next_deferred is deferred and isinstance(deferred, _resolve.ProviderDeferredWork):
                                task = deferred.task(provider)
                                if task is not None:
                                    _old, order, queued_ref, queued_work = deferred_discovery[provider][0]
                                    due_at = track_cooldown(provider, task["exc"])
                                    deferred_discovery[provider][0] = (
                                        due_at, order, queued_ref, queued_work,
                                    )
                                else:
                                    deferred_discovery[provider].pop(0)
                                    if not deferred_discovery[provider]:
                                        deferred_discovery.pop(provider, None)
                            else:
                                deferred_discovery[provider].pop(0)
                                if not deferred_discovery[provider]:
                                    deferred_discovery.pop(provider, None)
                                remember_discovery(ref, next_deferred)
                        else:
                            deferred_discovery[provider].pop(0)
                            if not deferred_discovery[provider]:
                                deferred_discovery.pop(provider, None)
                            if isinstance(state, dict) and state.get("_provider_deferred_cancelled"):
                                continue
                            remember_discovery(ref, state)
        worker_count = _resolve_worker_count(st, total=total)
        tick = max(1, total // 10) if total else 1
        # The count spans every reference in the list, not only the ones looked up. A
        # manuscript pointer is recorded outright and a cross-reference inherits its
        # antecedent's result, but both are references this phase owes the
        # bibliography -- counting only real_refs left the phase reporting "386/418"
        # and looking as though it had stopped short of the list.
        done = 0
        for ref in (r for r in refs_list if r["id"] in pointer_ids):
            _record_manuscript_pointer(st, run, ref)
            done += 1
            _phase_resolve_progress(total, done, tick)
        deferred_work: dict[str, list[tuple[float, int, dict, object]]] = {}
        deferred_order = 0

        def remember_deferred(ref, outcome):
            nonlocal deferred_order
            deferred = outcome.get("_semantic_scholar_deferred") if isinstance(outcome, dict) else None
            if deferred is None:
                return
            if not isinstance(deferred, _resolve._http.ProviderCooldownDeferred):
                return
            for provider, exc in deferred_entries(deferred):
                deferred_work.setdefault(provider, []).append(
                    (track_cooldown(provider, exc), deferred_order, ref, deferred)
                )
                deferred_order += 1

        def terminal(outcome) -> bool:
            return not (isinstance(outcome, dict) and isinstance(
                outcome.get("_semantic_scholar_deferred"), _resolve._http.ProviderCooldownDeferred,
            ))

        def prune_deferred_heads(queues):
            for provider, queue in list(queues.items()):
                while queue:
                    work = queue[0][3]
                    if not isinstance(work, _resolve.ProviderDeferredWork):
                        break
                    if not work.terminal and work.task(provider) is not None:
                        break
                    queue.pop(0)
                if not queue:
                    queues.pop(provider, None)

        def resume_deferred_work_due():
            nonlocal done
            due = []
            prune_deferred_heads(deferred_work)
            now = time.monotonic()
            for provider, queue in list(deferred_work.items()):
                if queue and provider_wake_at(provider, queue, now) <= now:
                    due.append((provider, queue[0]))
            for provider, (_not_before, _order, ref, deferred) in due:
                reason = cooldown_exhaustion_reason(provider, now)
                if reason is not None:
                    try:
                        with transport_telemetry.bind_run(run, ref_id=ref["id"]):
                            outcome = _resolve.exhaust_provider_cooldown(
                                deferred, provider, reason=reason,
                            )
                    except _resolve._http.ProviderCooldownDeferred:
                        deferred_work[provider].pop(0)
                        if not deferred_work[provider]:
                            deferred_work.pop(provider, None)
                        continue
                    deferred_work[provider].pop(0)
                    if not deferred_work[provider]:
                        deferred_work.pop(provider, None)
                    if (
                        isinstance(outcome, dict)
                        and outcome.get("_provider_deferred_cancelled") is True
                    ):
                        continue
                    if terminal(outcome):
                        # Exhaustion is a completed Resolve outcome, not a
                        # scheduler-only shortcut.  Send it through the same
                        # persistence/materialisation path as every other
                        # terminal resolution before reporting progress.
                        outcome = _finalize_resumed_semantic_scholar(ref, outcome)
                        _resolve_reference_pipeline(
                            st, run, ref, fetch_context,
                            openalex_batch_session=openalex_batch_session,
                            semantic_scholar_batch_session=semantic_scholar_batch_session,
                            pubmed_batch_session=pubmed_batch_session,
                            scholar_archive_circuit_session=scholar_archive_circuit_session,
                            pmc_idconv_batch_session=pmc_idconv_batch_session,
                            resolution=outcome,
                        )
                        done += 1
                        _phase_resolve_progress(total, done, tick)
                    continue
                if isinstance(deferred, _resolve.ProviderDeferredWork):
                    deferred._resume_provider = provider
                outcome = _resolve_reference_pipeline(
                    st, run, ref, fetch_context,
                    openalex_batch_session=openalex_batch_session,
                    semantic_scholar_batch_session=semantic_scholar_batch_session,
                    pubmed_batch_session=pubmed_batch_session,
                    scholar_archive_circuit_session=scholar_archive_circuit_session,
                    pmc_idconv_batch_session=pmc_idconv_batch_session,
                    semantic_scholar_deferred=deferred,
                )
                if isinstance(outcome, dict) and outcome.get("_provider_deferred_cancelled"):
                    deferred_work[provider].pop(0)
                    if not deferred_work[provider]:
                        deferred_work.pop(provider, None)
                    continue
                next_deferred = outcome.get("_semantic_scholar_deferred") if isinstance(outcome, dict) else None
                if next_deferred is deferred and isinstance(deferred, _resolve.ProviderDeferredWork):
                    task = deferred.task(provider)
                    if task is not None:
                        _old_not_before, order, queued_ref, _queued_work = deferred_work[provider][0]
                        due_at = track_cooldown(provider, task["exc"])
                        deferred_work[provider][0] = (
                            due_at, order, queued_ref, deferred,
                        )
                        continue
                    deferred_work[provider].pop(0)
                    if not deferred_work[provider]:
                        deferred_work.pop(provider, None)
                    continue
                deferred_work[provider].pop(0)
                if not deferred_work[provider]:
                    deferred_work.pop(provider, None)
                remember_deferred(ref, outcome)
                if terminal(outcome):
                    done += 1
                    _phase_resolve_progress(total, done, tick)

        def drain_deferred_work():
            """Preserve the old terminal drain when no batch cohort exists."""
            while deferred_work:
                prune_deferred_heads(deferred_work)
                if not deferred_work:
                    return
                now = time.monotonic()
                _provider, queue = min(
                    deferred_work.items(),
                    key=lambda item: provider_wake_at(item[0], item[1], now),
                )
                wait = max(0.0, provider_wake_at(_provider, queue, now) - now)
                if wait:
                    _progress_deferred_wait(
                        wait,
                        queue[0][2],
                        pending=sum(len(items) for items in deferred_work.values()),
                        stage="finalization",
                    )
                    with _perf.span("resolve_wait", "api.semanticscholar.org"):
                        time.sleep(wait)
                resume_deferred_work_due()

        def finalize_work_items(work_items):
            """Finalize a ready cohort without waiting on discovery cooldowns."""
            nonlocal done
            if worker_count <= 1 or len(work_items) <= 1:
                for ref, discovery_state in work_items:
                    outcome = _resolve_reference_pipeline(
                        st, run, ref, fetch_context,
                        openalex_batch_session=openalex_batch_session,
                        semantic_scholar_batch_session=semantic_scholar_batch_session,
                        pubmed_batch_session=pubmed_batch_session,
                        scholar_archive_circuit_session=scholar_archive_circuit_session,
                        pmc_idconv_batch_session=pmc_idconv_batch_session,
                        discovery_state=discovery_state,
                    )
                    remember_deferred(ref, outcome)
                    if terminal(outcome):
                        done += 1
                        _phase_resolve_progress(total, done, tick)
                return
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(worker_count, len(work_items))
            ) as pool:
                positions_by_future = {
                    pool.submit(
                        _resolve_reference_pipeline, st, run, ref, fetch_context,
                        openalex_batch_session=openalex_batch_session,
                        semantic_scholar_batch_session=semantic_scholar_batch_session,
                        pubmed_batch_session=pubmed_batch_session,
                        scholar_archive_circuit_session=scholar_archive_circuit_session,
                        pmc_idconv_batch_session=pmc_idconv_batch_session,
                        discovery_state=discovery_state,
                    ): (index, ref)
                    for index, (ref, discovery_state) in enumerate(work_items)
                }
                outcomes = [None] * len(work_items)
                for future in concurrent.futures.as_completed(positions_by_future):
                    index, _ref = positions_by_future[future]
                    outcome = future.result()
                    outcomes[index] = outcome
                    if terminal(outcome):
                        done += 1
                        _phase_resolve_progress(total, done, tick)
                # Provider queues are deterministic corpus-order FIFOs even
                # though independent finalization work completes concurrently.
                for (ref, _discovery_state), outcome in zip(work_items, outcomes):
                    remember_deferred(ref, outcome)

        def finalize_discovery_cohort(cohort, *, persist):
            """Checkpoint, enrich and finalize one complete ready cohort."""
            if not cohort:
                return
            if persist:
                # Only completed workers enter this append-only boundary.  A
                # deferred admission has not produced a state yet.
                _persist_discovery_wave(st, run, cohort)
                snapshots.update({ref["id"]: state for ref, state in cohort})
            enrichment_refs = [
                _resolve._effective_enrichment_ref(ref, state["result"])
                for ref, state in cohort
                if _validate_discovery_state(state, ref_id=ref["id"]) == "discovered"
            ]
            _prime_phase_batch_sessions(
                run,
                enrichment_refs,
                openalex_batch_session=openalex_batch_session,
                semantic_scholar_batch_session=semantic_scholar_batch_session,
                pubmed_batch_session=pubmed_batch_session,
                pmc_idconv_batch_session=pmc_idconv_batch_session,
                prime_pmc_idconv=True,
            )
            finalize_work_items(cohort)

        def discover_batch(refs):
            """Discover one ordered, bounded cohort before checkpointing it."""
            if worker_count <= 1 or len(refs) <= 1:
                for ref in refs:
                    try:
                        state = _discover_reference(
                            run, ref,
                            openalex_batch_session=openalex_batch_session,
                            semantic_scholar_batch_session=semantic_scholar_batch_session,
                            pubmed_batch_session=pubmed_batch_session,
                            scholar_archive_circuit_session=scholar_archive_circuit_session,
                            pmc_idconv_batch_session=pmc_idconv_batch_session,
                        )
                    except _resolve._http.ProviderCooldownDeferred as deferred:
                        state = deferred
                    remember_discovery(ref, state)
                return
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(worker_count, len(refs))
            ) as pool:
                futures = {
                    pool.submit(
                        _discover_reference, run, ref,
                        openalex_batch_session=openalex_batch_session,
                        semantic_scholar_batch_session=semantic_scholar_batch_session,
                        pubmed_batch_session=pubmed_batch_session,
                        scholar_archive_circuit_session=scholar_archive_circuit_session,
                        pmc_idconv_batch_session=pmc_idconv_batch_session,
                    ): ref for ref in refs
                }
                for future, ref in futures.items():
                    try:
                        state = future.result()
                    except _resolve._http.ProviderCooldownDeferred as deferred:
                        state = deferred
                    remember_discovery(ref, state)

        def finalize_ready_discovery():
            nonlocal discovered
            if discovered:
                cohort, discovered = discovered, []
                finalize_discovery_cohort(cohort, persist=True)

        if batch_sessions_active:
            saved_snapshot_ids = set(snapshots)
            first_batch = discovery_refs[:_DISCOVERY_CHECKPOINT_CHUNK_SIZE]
            discover_batch(first_batch)
            finalize_ready_discovery()
            # Keep discovery states bounded in memory and checkpoint each
            # deterministic input-order cohort before starting the next one.
            # A process interruption can therefore resume from the last
            # completed cohort without redoing an entire manuscript's discovery.
            for start in range(
                _DISCOVERY_CHECKPOINT_CHUNK_SIZE,
                len(discovery_refs),
                _DISCOVERY_CHECKPOINT_CHUNK_SIZE,
            ):
                discover_batch(
                    discovery_refs[start:start + _DISCOVERY_CHECKPOINT_CHUNK_SIZE]
                )
                finalize_ready_discovery()

            # Existing snapshots are ready at phase entry, but are already
            # durable and therefore must not be appended a second time.  Delay
            # them until all new discovery cohorts have reached their own
            # durable final boundary, so an interruption leaves a resume with
            # the largest possible complete resolve map.
            saved_cohort = [
                (ref, snapshots[ref["id"]]) for ref in real_refs
                if ref["id"] in saved_snapshot_ids
            ]
            finalize_discovery_cohort(saved_cohort, persist=False)
            finalize_work_items([
                (ref, None) for ref in real_refs if ref["id"] in final_ids
            ])

            # Finish each ready discovery cohort before sleeping for an optional
            # Semantic Scholar continuation.  This releases useful Resolve and
            # inline-Fetch work even while another reference is rate-limited.
            while discovered or deferred_discovery or deferred_work:
                # A shared reference continuation becomes terminal as soon as
                # any provider resolves it.  Remove its sibling queue heads
                # before choosing the next wake-up, otherwise a cancelled
                # provider can keep the whole phase asleep until its old
                # cooldown expires.
                prune_deferred_heads(deferred_discovery)
                prune_deferred_heads(deferred_work)
                if discovered:
                    finalize_ready_discovery()
                    continue
                candidates = []
                now = time.monotonic()
                candidates.extend(
                    (provider_wake_at(provider, queue, now), queue[0][2], "discovery")
                    for provider, queue in deferred_discovery.items()
                )
                candidates.extend(
                    (provider_wake_at(provider, queue, now), queue[0][2], "finalization")
                    for provider, queue in deferred_work.items()
                )
                not_before, next_ref, wait_stage = min(
                    candidates, key=lambda item: item[0]
                )
                wait = max(0.0, not_before - now)
                if wait:
                    _progress_deferred_wait(
                        wait,
                        next_ref,
                        pending=sum(
                            len(queue)
                            for queues in (deferred_discovery, deferred_work)
                            for queue in queues.values()
                        ),
                        stage=wait_stage,
                    )
                    with _perf.span("resolve_wait", "api.semanticscholar.org"):
                        time.sleep(wait)
                resume_discovery_due()
                # A finalization continuation must not block a discovery
                # continuation that becomes due earlier.  When both are due,
                # discovery creates the next durable cohort before its sibling
                # finalization work is resumed.
                if not discovered:
                    resume_deferred_work_due()

            drain_deferred_work()
        else:
            # Preserve the pre-batch serial/parallel behaviour exactly.
            work_items = [
                (ref, snapshots[ref["id"]]) for ref in real_refs
                if ref["id"] in snapshots
            ]
            work_items.extend(
                (ref, None) for ref in real_refs if ref["id"] not in snapshots
            )
            finalize_work_items(work_items)
            drain_deferred_work()
        # Cross-references are always resolved serially, after every real_refs
        # entry has landed -- a cross-ref must never race its antecedent.
        for ref in xref_refs:
            antecedent_id, _pinpoint = xmap[ref["id"]]
            antecedent_number = num_by_id.get(antecedent_id)
            if not _inherit_cross_reference(st, run, ref, antecedent_id, antecedent_number):
                _resolve_reference_pipeline(
                    st, run, ref, fetch_context,
                    openalex_batch_session=openalex_batch_session,
                    semantic_scholar_batch_session=semantic_scholar_batch_session,
                    pubmed_batch_session=pubmed_batch_session,
                    scholar_archive_circuit_session=scholar_archive_circuit_session,
                    pmc_idconv_batch_session=pmc_idconv_batch_session,
                )
            done += 1
            _phase_resolve_progress(total, done, tick)
    finally:
        session_stack.close()
    # This is intentionally process-local runtime state, never a run setting:
    # Fetch may replay only exact successful bytes obtained during this invocation.
    st["_fetch_run_context"] = fetch_context
    _print_phase_credential_warnings(
        run,
        "resolve",
        since=phase_started_at,
    )
    return "fetch"


def _resolve_repair_has_hard_identity_conflict(resolve_result: dict) -> bool:
    """Return whether weak metadata contradicts the cited work.

    Fetch repair may recover availability, but it must not replace the citation's
    identity with the resolver candidate that happened to expose a downloadable
    document.  A single year discrepancy can be legitimate publication-version
    noise; explicit metadata-conflict flags, author/venue conflicts, or multiple
    hard conflicts are fatal.
    """
    evidence = resolve_result.get("evidence_profile") or {}
    profiles = [
        resolve_result.get("metadata_match"),
        evidence.get("metadata_match"),
        (evidence.get("best_candidate") or {}).get("metadata_match"),
    ]
    if resolve_result.get("identity_conflict") or resolve_result.get("metadata_conflict"):
        return True
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        hard_conflicts = set(profile.get("hard_conflicts") or [])
        if (
            profile.get("metadata_conflict")
            or profile.get("author_conflict")
            or profile.get("venue_conflict")
            or hard_conflicts.intersection({"author", "venue"})
            or len(hard_conflicts.intersection({"author", "year", "venue"})) >= 2
        ):
            return True
    return False


def _resolve_repair_trigger(resolve_result: dict | None) -> str | None:
    """Return a trigger reason string when *resolve_repair* should fire, or None.

    A strong resolution must never enter repair; repair is reserved for genuinely
    weak / unresolved references.
    """
    if not isinstance(resolve_result, dict):
        return None
    via = str(resolve_result.get("via") or "")
    status = str(resolve_result.get("status") or "")
    tag = str(resolve_result.get("reference_status_tag") or "")
    # Existing early-outs ---------------------------------------------------
    if via.startswith("fetch_repair:") or tag == "suspected_fabricated":
        return None

    # Block when any strong-identifier signal is present --------------------
    # 1) resolution_basis is a strong identifier
    basis = str(resolve_result.get("resolution_basis") or "")
    _strong_bases = {"doi", "pmid", "isbn", "pubmed", "arxiv"}
    if basis in _strong_bases:
        return None

    # 2) reference_status_tag is verified
    if tag == "verified":
        return None

    # 3) existence_confidence is high and a resolved_identifier exists
    confidence = str(resolve_result.get("existence_confidence") or "")
    resolved_id = resolve_result.get("resolved_identifier")
    if confidence == "high" and isinstance(resolved_id, dict) and resolved_id:
        return None

    # A fetch can improve availability but cannot erase a bibliographic
    # contradiction already established by Resolve.
    if _resolve_repair_has_hard_identity_conflict(resolve_result):
        return None

    # Genuinely unresolved references may still use repair after the strong
    # identity and conflict guards above.
    if status == "unresolved":
        return "status=unresolved"

    # Weak signal: crossref_metadata with no strong identifiers --------------
    if via == "crossref_metadata":
        evidence = resolve_result.get("evidence_profile") or {}
        if evidence.get("minimum_checks_completed") is not True:
            return None
        return "via=crossref_metadata"

    return None


def _resolve_repair_attempt_summary(fetched: dict) -> dict:
    trace = fetched.get("fetch_trace") or {}
    direct_attempts = list((trace.get("direct_text") or {}).get("attempts") or [])
    execution_attempts = list((trace.get("execution") or {}).get("attempts") or [])
    return {
        "status": fetched.get("status"),
        "via": f"fetch_repair:{fetched.get('method') or 'unknown'}",
        "method": fetched.get("method"),
        "reason": fetched.get("reason"),
        "pdf_url": fetched.get("pdf_url"),
        "content_version": fetched.get("content_version"),
        "corroborate_signal": fetched.get("corroborate_signal"),
        "corroborate_score": fetched.get("corroborate_score"),
        "direct_attempts": [
            {
                "method": row.get("method"),
                "source_ref": row.get("source_ref"),
                "outcome": row.get("outcome"),
                "reason": row.get("reason"),
            }
            for row in direct_attempts
            if isinstance(row, dict)
        ],
        "execution_attempts": [
            {
                "method": row.get("method"),
                "url": row.get("url"),
                "final_url": row.get("final_url"),
                "kind": row.get("kind"),
                "outcome": row.get("outcome"),
                "reason": row.get("reason"),
                "status": row.get("status"),
                "content_type": row.get("content_type"),
            }
            for row in execution_attempts
            if isinstance(row, dict)
        ],
    }


def _resolve_repair_payload(
    ref: dict,
    resolve_result: dict,
    fetched: dict,
    *,
    trigger: str,
) -> dict:
    method = fetched.get("method") or "unknown"
    original_status = resolve_result.get("status") or "unknown"
    original_via = resolve_result.get("via") or "unknown"
    attempts = list(resolve_result.get("attempts") or [])
    attempts.append(_resolve_repair_attempt_summary(fetched))
    evidence = dict(resolve_result.get("evidence_profile") or {})
    repaired_abstract, repaired_abstract_via, abstract_disposition = _repair_abstract_payload(
        ref,
        resolve_result,
        trigger=trigger,
    )
    evidence["fetch_repair"] = {
        "trigger": trigger,
        "original_status": original_status,
        "original_via": original_via,
        "stored_via": method,
        "stored_source_ref": fetched.get("pdf_url") or fetched.get("url"),
        "content_version": fetched.get("content_version"),
        "corroborate_signal": fetched.get("corroborate_signal"),
        "corroborate_score": fetched.get("corroborate_score"),
        "abstract_disposition": abstract_disposition,
    }
    return {
        "ref_id": ref["id"],
        "ref_number": ref.get("ref_number"),
        "status": "resolved",
        "via": f"fetch_repair:{method}",
        # Falling back on the raw title field would carry an unusable one into the
        # manifest as the work's identity - the entry whose title field held its
        # own DOI link arrived here that way.  The validated candidate applies the
        # same guards the search key does, and yields None rather than a locator.
        "matched_title": (resolve_result.get("matched_title")
                          or _resolve._article_title_candidate(ref)),
        "abstract": repaired_abstract,
        "abstract_via": repaired_abstract_via,
        "retracted": bool(resolve_result.get("retracted")),
        "fulltext_exists": True,
        "oa_status": resolve_result.get("oa_status"),
        "work_type": resolve_result.get("work_type"),
        "resolution_basis": "fetch_repair",
        "existence_confidence": "high",
        "reason": (
            f"fetch repair stored corroborated full text via {method}; "
            f"original resolve was {original_status} via {original_via}"
        ),
        "reference_status_tag": "fetch_repaired",
        "fabrication_risk": "low",
        "evidence_profile": evidence,
        "attempts": attempts,
        "checked_at": _now(),
        "resolved_identifier": resolve_result.get("resolved_identifier"),
    }


def _maybe_inline_resolve_repair(
    st: dict,
    ref: dict,
    resolve_result: dict,
    *,
    fetch_context: object | None,
) -> dict:
    run = st["run_dir"]
    if st.get("no_fetch") or st.get("accuracy") == "abstract":
        return resolve_result
    trigger = _resolve_repair_trigger(resolve_result)
    if trigger is None:
        return resolve_result
    ref_id = ref["id"]
    if _ref_has_source_tier(run, ref_id, "fulltext"):
        return resolve_result

    try:
        fetched = _fetch.fetch_fulltext(
            ref,
            run,
            resolve_result,
            mailto=st.get("mailto"),
            fetch_context=fetch_context,
            evidence_payload=_fetch_evidence_payload(resolve_result),
        )
    except Exception as exc:
        fetched = {
            "status": "error",
            "method": "resolve_repair",
            "reason": f"{type(exc).__name__}: {exc}",
        }
        # Persist the structured exception so the failure is visible in the
        # DB without requiring CITATION_VERIFIER_DEBUG_RUN=1.
        _repo_sync_resolve_result(
            run,
            ref_id,
            _repair_exception_result(ref, resolve_result, exc, trigger),
            trace_state="trace_not_produced",
        )

    fetched["ref_id"] = ref_id
    fetched["ref_number"] = ref.get("ref_number")
    _record_fetch_attempts(run, ref_id, fetched, resolve_result)
    if fetched.get("status") == "rate_limit_deferred":
        # Admission did not issue a request and formal Fetch will re-admit the
        # frozen plan; it is neither a repair result nor a repair failure.
        return resolve_result
    if fetched.get("status") == "error":
        # The exception handler already persisted a _repair_exception_result
        # in resolve_results -- do not fall through to the non-stored branch
        # which would overwrite it with a _repair_failed_result.
        return resolve_result
    if fetched.get("status") != "stored":
        # Record the repair failure in resolve_results so a reader can see
        # that repair was attempted and why it didn't improve the result.
        _repo_sync_resolve_result(
            run,
            ref_id,
            _repair_failed_result(resolve_result, fetched, trigger),
            trace_state="trace_not_produced",
        )
        return resolve_result

    repaired = _resolve_repair_payload(ref, resolve_result, fetched, trigger=trigger)
    _repo_sync_resolve_result(
        run, ref_id, repaired, trace_state="trace_not_produced",
    )
    removed_abstracts = _suppress_repair_abstract_entries(
        run,
        ref_id,
        ((repaired.get("evidence_profile") or {}).get("fetch_repair") or {}).get("abstract_disposition") or {},
    )
    return repaired
