#!/usr/bin/env python3
# tests/test_pipeline_parallel.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Phase 7 regression tests for bounded per-reference parallelism."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests._bootstrap import *  # noqa: F401,F403
from core.fetch import service as fetch_mod
from core.fetch.storage import content_store
from core.fetch.transport import host_limiter
from core.fetch.transport import http_headers as fetch_http_headers_mod
from core.resolve import service as resolve_mod
from core.resolve import transport_telemetry
from core.app import run as driver
from core.app.commands import journal_catalog
from core.app.phases import resolve as resolve_phase
from core.infra.db import RunRepository
from core.infra import perf as perf_mod
from core.resolve import http as http_mod
from tests._resolve_fixtures import current_resolution


class _FakeClock:
    """A fake monotonic clock shared across test threads.

    The limiter serialises requests correctly, but a plain global ``now`` is
    advanced by *every* thread's sleep — so by the time a thread that was
    granted instantly reaches ``open_request()`` other threads have already
    pushed the clock forward.  We fix that by pinning each thread's view at
    the first ``monotonic()`` call it makes, and only advancing that view
    when the thread itself sleeps.  This preserves the true grant time in
    ``host_times`` regardless of thread interleaving.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.now = 0.0
        self.sleeps: list[float] = []
        self._thread_time: dict[int, float] = {}

    def monotonic(self) -> float:
        tid = threading.get_ident()
        with self._lock:
            t = self._thread_time.get(tid)
            if t is None:
                t = self.now
                self._thread_time[tid] = t
            return t

    def sleep(self, seconds: float) -> None:
        tid = threading.get_ident()
        with self._lock:
            self.sleeps.append(round(float(seconds), 3))
            self.now += float(seconds)
            self._thread_time[tid] = self.now


class _FakeResp:
    def __init__(self, url: str, body: bytes = b"{}", status: int = 200):
        self.full_url = url
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return b"{}"

    def geturl(self) -> str:
        return self.full_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class PipelineParallelTests(unittest.TestCase):
    def test_provider_cooldown_preflight_has_no_physical_429(self):
        deferred = http_mod.ProviderCooldownDeferred(not_before=10, provider="p")
        self.assertEqual(deferred.physical_429_count, 0)

    def test_provider_cooldown_carries_physical_429_from_http_scope(self):
        deferred = http_mod.SemanticScholarCooldownDeferred(
            not_before=10, physical_429_count=1,
        )
        self.assertEqual(deferred.physical_429_count, 1)

    def test_openalex_fallback_cooldown_enters_typed_discovery_continuation(self):
        ref = {"id": "r1", "ref_number": 1, "title": "Synthetic work",
               "raw_entry": "Author. Synthetic work.", "source_type": "article",
               "source_kind": "article_like"}

        def resolver(name, *, outcome=None, error=None):
            module = mock.Mock(NAME=name, RESOLVE_NAME=name, LOCAL_ACCELERATOR=False)
            module.supports.return_value = True
            if error is not None:
                module.discover.side_effect = error
            else:
                module.discover.return_value = outcome
            return module

        primary = resolver("openalex_search", outcome={
            "status": "unresolved", "reason": "rate_limited", "retryable": True,
        })
        before = resolver("fallback_before")
        deferred = http_mod.ProviderCooldownDeferred(
            not_before=10, provider="semantic_scholar",
        )
        cooling = resolver("semantic_scholar_search", error=deferred)
        after = resolver("fallback_after")
        attempts = []
        calls = []
        outcomes = [
            {"status": "unresolved", "via": "openalex_search",
             "reason": "rate_limited", "retryable": True},
            None,
            deferred,
        ]

        def call(module, _ref):
            calls.append(module)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(resolve_mod, "_resolver_registry", return_value={
                "openalex_search": primary,
                "fallback_before": before,
                "semantic_scholar_search": cooling,
                "fallback_after": after,
        }), mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[
                "openalex_search", "fallback_before", "semantic_scholar_search",
                "fallback_after",
        ]), mock.patch.object(resolve_mod, "_is_published_preprint_candidate", return_value=False), \
             mock.patch.object(resolve_mod, "_call_registry_resolver", side_effect=call):
            with self.assertRaises(resolve_mod.ProviderDeferredWork) as raised:
                resolve_mod._try_resolvers_with_fallback(
                    "openalex_search", ref, result=None, attempts=attempts, max_rounds=1,
                )

        self.assertEqual(raised.exception.kind, "discovery_fallback")
        self.assertIs(raised.exception.task("semantic_scholar")["exc"], deferred)
        self.assertEqual(raised.exception.state["next_fallback_index"], 2)
        self.assertEqual([attempt["via"] for attempt in attempts], ["openalex_search"])
        self.assertEqual(calls, [primary, before, cooling])
        self.assertEqual(raised.exception.state["stage"], "fallback_resume")

    def test_primary_openalex_cooldown_resumes_same_primary_before_fallbacks(self):
        ref = {"id": "r1", "title": "Synthetic work", "source_type": "article",
               "source_kind": "article_like"}
        primary = mock.Mock(NAME="openalex_search", RESOLVE_NAME="openalex_search")
        fallback = mock.Mock(NAME="fallback", RESOLVE_NAME="fallback")
        primary.LOCAL_ACCELERATOR = False
        fallback.LOCAL_ACCELERATOR = False
        primary.supports.return_value = True
        deferred = http_mod.ProviderCooldownDeferred(not_before=10, provider="openalex")
        resolved = {"status": "resolved", "via": "fallback", "matched_title": "Synthetic work"}
        calls = []
        outcomes = [
            deferred,
            {"status": "unresolved", "via": "openalex_search",
             "reason": "rate_limited", "retryable": True},
            resolved,
        ]

        def call(module, _ref):
            calls.append(module)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(resolve_mod, "_resolver_registry", return_value={
                "openalex_search": primary, "fallback": fallback,
        }), mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[
                "openalex_search", "fallback",
        ]), mock.patch.object(resolve_mod, "_is_published_preprint_candidate", return_value=False), \
             mock.patch.object(resolve_mod, "_call_registry_resolver", side_effect=call), \
             mock.patch.object(
                 resolve_mod, "_complete_discovery_after_openalex",
                 side_effect=lambda ref, result, attempts, **_kwargs: {
                     "result": result, "attempts": attempts,
                 },
             ) as complete:
            with self.assertRaises(resolve_mod.ProviderDeferredWork) as raised:
                resolve_mod._try_resolvers_with_fallback(
                    "openalex_search", ref, result=None, attempts=[], max_rounds=1,
                    discovery_only=True,
                )
            clock = _FakeClock()
            clock.sleep(10)
            self.assertGreaterEqual(clock.monotonic(), deferred.not_before)
            result = resolve_mod.resume_semantic_scholar_cooldown(
                raised.exception, "openalex",
            )

        self.assertEqual(calls, [primary, primary, fallback])
        self.assertEqual(set(result), {"result", "attempts"})
        self.assertEqual(result["result"], resolved)
        self.assertEqual([item["via"] for item in result["attempts"]], [
            "openalex_search", "fallback",
        ])
        self.assertEqual(raised.exception.providers(), ())
        complete.assert_called_once()

    def test_semantic_scholar_fallback_resumes_once_then_continues_saved_suffix(self):
        ref = {"id": "r1", "title": "Synthetic work", "source_type": "article",
               "source_kind": "article_like"}
        primary = mock.Mock(NAME="openalex_search", RESOLVE_NAME="openalex_search")
        before = mock.Mock(NAME="fallback_before", RESOLVE_NAME="fallback_before")
        cooling = mock.Mock(NAME="semantic_scholar_search", RESOLVE_NAME="semantic_scholar_search")
        after = mock.Mock(NAME="fallback_after", RESOLVE_NAME="fallback_after")
        for module in (primary, before, cooling, after):
            module.LOCAL_ACCELERATOR = False
        primary.supports.return_value = True
        deferred = http_mod.SemanticScholarCooldownDeferred(not_before=10)
        unverified = {"status": "unverified", "via": "semantic_scholar_search",
                      "reason": "synthetic weak candidate"}
        resolved = {"status": "resolved", "via": "fallback_after",
                    "matched_title": "Synthetic work"}
        calls = []
        outcomes = [
            {"status": "unresolved", "via": "openalex_search",
             "reason": "rate_limited", "retryable": True},
            None,
            deferred,
            unverified,
            resolved,
        ]

        def call(module, _ref):
            calls.append(module)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(resolve_mod, "_resolver_registry", return_value={
                "openalex_search": primary, "fallback_before": before,
                "semantic_scholar_search": cooling, "fallback_after": after,
        }), mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[
                "openalex_search", "fallback_before", "semantic_scholar_search",
                "fallback_after",
        ]), mock.patch.object(resolve_mod, "_is_published_preprint_candidate", return_value=False), \
             mock.patch.object(resolve_mod, "_call_registry_resolver", side_effect=call), \
             mock.patch.object(
                 resolve_mod, "_complete_discovery_after_openalex",
                 side_effect=lambda ref, result, attempts, **_kwargs: {
                     "result": result, "attempts": attempts,
                 },
             ):
            with self.assertRaises(resolve_mod.ProviderDeferredWork) as raised:
                resolve_mod._try_resolvers_with_fallback(
                    "openalex_search", ref, result=None, attempts=[], max_rounds=1,
                    discovery_only=True,
                )
            work = raised.exception
            clock = _FakeClock()
            clock.sleep(10)
            self.assertGreaterEqual(clock.monotonic(), deferred.not_before)
            result = resolve_mod.resume_semantic_scholar_cooldown(
                work, "semantic_scholar",
            )

        self.assertEqual(calls, [primary, before, cooling, cooling, after])
        self.assertEqual(set(result), {"result", "attempts"})
        self.assertEqual(result["result"], resolved)
        self.assertEqual([item["via"] for item in result["attempts"]], [
            "openalex_search", "semantic_scholar_search", "fallback_after",
        ])

    def test_expired_fallback_cooldown_only_evidence_stays_unresolved_not_not_found(self):
        ref = {"id": "r1", "title": "Synthetic work", "source_type": "article",
               "source_kind": "article_like"}
        primary = mock.Mock(NAME="openalex_search", RESOLVE_NAME="openalex_search")
        before = mock.Mock(NAME="fallback_before", RESOLVE_NAME="fallback_before")
        cooling = mock.Mock(NAME="semantic_scholar_search", RESOLVE_NAME="semantic_scholar_search")
        after = mock.Mock(NAME="fallback_after", RESOLVE_NAME="fallback_after")
        for module in (primary, before, cooling, after):
            module.LOCAL_ACCELERATOR = False
        primary.supports.return_value = True
        deferred = http_mod.ProviderCooldownDeferred(not_before=10, provider="semantic_scholar")
        calls = []
        outcomes = [
            {"status": "unresolved", "via": "openalex_search",
             "reason": "rate_limited", "retryable": True},
            None,
            deferred,
            None,
        ]

        def call(module, _ref):
            calls.append(module)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(resolve_mod, "_resolver_registry", return_value={
                "openalex_search": primary, "fallback_before": before,
                "semantic_scholar_search": cooling, "fallback_after": after,
        }), mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[
                "openalex_search", "fallback_before", "semantic_scholar_search",
                "fallback_after",
        ]), mock.patch.object(resolve_mod, "_is_published_preprint_candidate", return_value=False), \
             mock.patch.object(resolve_mod, "_call_registry_resolver", side_effect=call):
            with self.assertRaises(resolve_mod.ProviderDeferredWork) as raised:
                resolve_mod._try_resolvers_with_fallback(
                    "openalex_search", ref, result=None, attempts=[], max_rounds=1,
                    discovery_only=True,
                )
            work = raised.exception
            clock = _FakeClock()
            clock.sleep(610)
            self.assertGreater(clock.monotonic(), deferred.not_before)
            with mock.patch.object(resolve_mod, "_resolver_registry", return_value={}), \
                 mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[]):
                result = resolve_mod.exhaust_provider_cooldown(
                    work, "semantic_scholar", reason="rate_limit_exhausted: fake clock",
                )

        self.assertEqual(calls, [primary, before, cooling, after])
        self.assertEqual(set(result), {"result", "attempts"})
        self.assertEqual(result["result"]["status"], "unresolved")
        self.assertNotEqual(result["result"]["status"], "not_found")
        self.assertEqual(result["attempts"][-1]["via"], "semantic_scholar_search")
        self.assertEqual(result["attempts"][-1]["error_type"], "rate_limit")

    def test_semantic_scholar_fallback_cooldown_expiry_preserves_attempts_and_suffix(self):
        ref = {"id": "r1", "title": "Synthetic work", "source_type": "article",
               "source_kind": "article_like"}
        primary = mock.Mock(NAME="openalex_search", RESOLVE_NAME="openalex_search")
        before = mock.Mock(NAME="fallback_before", RESOLVE_NAME="fallback_before")
        cooling = mock.Mock(NAME="semantic_scholar_search", RESOLVE_NAME="semantic_scholar_search")
        after = mock.Mock(NAME="fallback_after", RESOLVE_NAME="fallback_after")
        for module in (primary, before, cooling, after):
            module.LOCAL_ACCELERATOR = False
        primary.supports.return_value = True
        deferred = http_mod.SemanticScholarCooldownDeferred(not_before=10)
        resolved = {"status": "resolved", "via": "fallback_after", "matched_title": "Synthetic work"}
        calls = []
        outcomes = [
            {"status": "unresolved", "via": "openalex_search",
             "reason": "rate_limited", "retryable": True},
            None,
            deferred,
            resolved,
        ]

        def call(module, _ref):
            calls.append(module)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(resolve_mod, "_resolver_registry", return_value={
                "openalex_search": primary, "fallback_before": before,
                "semantic_scholar_search": cooling, "fallback_after": after,
        }), mock.patch.object(resolve_mod, "_load_fallback_order", return_value=[
                "openalex_search", "fallback_before", "semantic_scholar_search",
                "fallback_after",
        ]), mock.patch.object(resolve_mod, "_is_published_preprint_candidate", return_value=False), \
             mock.patch.object(resolve_mod, "_call_registry_resolver", side_effect=call), \
             mock.patch.object(
                 resolve_mod, "_complete_discovery_after_openalex",
                 side_effect=lambda ref, result, attempts, **_kwargs: {
                     "result": result, "attempts": attempts,
                 },
             ):
            with self.assertRaises(resolve_mod.ProviderDeferredWork) as raised:
                resolve_mod._try_resolvers_with_fallback(
                    "openalex_search", ref, result=None, attempts=[], max_rounds=1,
                    discovery_only=True,
                )
            work = raised.exception
            self.assertEqual(work.state["next_fallback_index"], 2)
            self.assertIs(work.task("semantic_scholar")["module"], cooling)
            clock = _FakeClock()
            clock.sleep(10)
            self.assertGreaterEqual(clock.monotonic(), deferred.not_before)
            exhausted = resolve_mod.exhaust_provider_cooldown(
                work, "semantic_scholar", reason="rate_limit_exhausted: test clock",
            )

        self.assertEqual(calls, [primary, before, cooling, after])
        self.assertEqual(set(exhausted), {"result", "attempts"})
        self.assertEqual(exhausted["result"], resolved)
        self.assertEqual(
            [item["via"] for item in exhausted["attempts"]],
            ["openalex_search", "semantic_scholar_search", "fallback_after"],
        )
        exhaustion = exhausted["attempts"][1]
        self.assertEqual(exhaustion["status"], "unverified")
        self.assertEqual(exhaustion["error_type"], "rate_limit")
        self.assertFalse(exhaustion["retryable"])
        self.assertNotIn("_provider_deferred_cancelled", exhausted)

    def test_expired_fallback_handoff_retains_typed_suffix_through_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                ref = repo.effective_parse_payload()["references"][0]
            finally:
                repo.close()
            known = {
                "status": "unresolved", "via": "openalex_search",
                "reason": "rate_limited", "retryable": True,
            }
            primary = mock.Mock(NAME="openalex_search", RESOLVE_NAME="openalex_search")
            cooling = mock.Mock(
                NAME="semantic_scholar_search", RESOLVE_NAME="semantic_scholar_search",
            )
            after = mock.Mock(NAME="fallback_after", RESOLVE_NAME="fallback_after")
            attempts = [dict(known)]
            state = {
                "ref": ref,
                "result": dict(known),
                "attempts": attempts,
                "discovery_only": True,
                "publisher_uri_discovered_candidate_selected": False,
                "primary_module": primary,
                "fallback_modules": [cooling, after],
                "max_rounds": 1,
                "round_idx": 0,
                "stage": "fallback_resume",
                "primary_result": dict(known),
                "blocked_preprint": False,
                "next_fallback_index": 1,
                "deferred_fallback_index": 0,
                "finish_current_round": False,
            }
            initial = resolve_mod.ProviderDeferredWork(
                kind="discovery_fallback",
                tasks=[(
                    http_mod.ProviderCooldownDeferred(
                        not_before=0, provider="semantic_scholar", physical_429_count=3,
                    ),
                    cooling,
                )],
                state=state,
            )
            suffix_module = mock.Mock(
                NAME="suffix_provider", RESOLVE_NAME="suffix_provider",
            )
            suffix_work = {}

            def defer_in_suffix(_ref, result, current_attempts, **_kwargs):
                work = resolve_mod.ProviderDeferredWork(
                    kind="discovery_provider",
                    tasks=[(
                        http_mod.ProviderCooldownDeferred(
                            not_before=0, provider="suffix_provider",
                        ),
                        suffix_module,
                    )],
                    state={
                        "ref": dict(ref), "result": dict(result),
                        "attempts": current_attempts, "discovery_only": True,
                        "tail_complete": True,
                    },
                )
                suffix_work["work"] = work
                raise work

            finalized = []

            def finalize(_st, _run, _ref, _context, **kwargs):
                finalized.append(kwargs["discovery_state"])
                return {
                    "result": kwargs["discovery_state"]["result"],
                    "attempts": kwargs["discovery_state"]["attempts"],
                }

            with mock.patch.object(
                    resolve_phase, "_load_parse_payload",
                    return_value={"references": [ref]},
            ), mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=initial), \
                 mock.patch.object(resolve_phase, "_persist_discovery_wave"), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=finalize), \
                 mock.patch.object(resolve_phase._resolve, "_call_registry_resolver", return_value=None), \
                 mock.patch.object(
                     resolve_phase._resolve, "_complete_discovery_after_openalex",
                     side_effect=defer_in_suffix,
                 ), mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase.time, "monotonic", return_value=0.0), \
                 mock.patch.object(resolve_phase.time, "sleep") as sleep, \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(
                     resolve_phase._resolve, "new_semantic_scholar_batch_session",
                     return_value=mock.Mock(close=mock.Mock()),
                 ), mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(
                     resolve_phase._resolve, "new_scholar_archive_circuit_session",
                     return_value=mock.Mock(close=mock.Mock()),
                 ), mock.patch.object(
                     resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None,
                 ):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        sleep.assert_not_called()
        self.assertIn("work", suffix_work)
        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]["result"], known)
        self.assertEqual(
            [(item["via"], item["status"]) for item in finalized[0]["attempts"]],
            [("openalex_search", "unresolved"), ("semantic_scholar_search", "unverified")],
        )

    def test_exhausted_provider_records_unverified_rate_limit_evidence(self):
        ref = {"id": "r1", "title": "Example"}
        sibling = mock.Mock(NAME="q")
        work = resolve_mod.ProviderDeferredWork(
            kind="discovery_provider",
            tasks=[
                (http_mod.ProviderCooldownDeferred(not_before=0, provider="p"), mock.Mock(NAME="p")),
                (http_mod.ProviderCooldownDeferred(not_before=0, provider="q"), sibling),
            ],
            state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
        )
        with self.assertRaises(http_mod.ProviderCooldownDeferred) as raised:
            resolve_mod.exhaust_provider_cooldown(
                work, "p", reason="rate_limit_exhausted: test",
            )
        self.assertIs(raised.exception, work)
        self.assertIsNotNone(work.task("q"))
        self.assertIsNone(work.task("p"))
        self.assertIn("rate_limit_exhausted", work.state["attempts"][-1]["reason"])
        self.assertEqual(work.state["attempts"][-1]["error_type"], "rate_limit")
        self.assertFalse(work.state["attempts"][-1]["retryable"])

        sibling.discover.return_value = {"status": "resolved", "via": "q"}
        with mock.patch.object(
            resolve_mod,
            "_complete_discovery_after_optional",
            return_value={"status": "resolved", "via": "q"},
        ) as complete:
            result = resolve_mod.resume_semantic_scholar_cooldown(work, "q")
        self.assertEqual(result["status"], "resolved")
        self.assertNotEqual(result["status"], "not_found")
        complete.assert_called_once()

        solo = resolve_mod.ProviderDeferredWork(
            kind="discovery_provider",
            tasks=[(http_mod.ProviderCooldownDeferred(
                not_before=0, provider="p",
            ), mock.Mock(NAME="p"))],
            state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
        )
        with mock.patch.object(
            resolve_mod,
            "_complete_discovery_after_optional",
            side_effect=lambda _ref, resolved, _attempts, **_kwargs: resolved,
        ):
            exhausted = resolve_mod.exhaust_provider_cooldown(
                solo, "p", reason="rate_limit_exhausted: test",
            )
        self.assertEqual(exhausted["status"], "unverified")
        self.assertIn("rate_limit_exhausted", exhausted["reason"])

    def test_finalization_exhausts_immediately_after_initial_plus_two_retry_429s(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                ref = repo.effective_parse_payload()["references"][0]
            finally:
                repo.close()
            module = mock.Mock(NAME="p")
            work = resolve_mod.ProviderDeferredWork(
                kind="enrichment_provider",
                tasks=[(http_mod.ProviderCooldownDeferred(
                    not_before=0, provider="p", physical_429_count=1,
                ), module)],
                state={
                    "ref": ref,
                    "result": {"status": "resolved", "via": "doi", "doi": "10.1000/test"},
                    "attempts": [],
                    "enriched": set(),
                },
            )
            resumes = []
            exhausted_resolution = current_resolution(ref, {
                "status": "resolved", "via": "doi", "doi": "10.1000/test",
                "reason": "rate_limit_exhausted: provider returned more than two retry HTTP 429 responses",
                "attempts": [{
                    "status": "unverified", "via": "p",
                    "reason": "rate_limit_exhausted: provider returned more than two retry HTTP 429 responses",
                    "error_type": "rate_limit",
                    "error_reason": "rate_limit_exhausted: provider returned more than two retry HTTP 429 responses",
                    "retryable": False,
                }],
            })
            real_pipeline = resolve_phase._resolve_reference_pipeline

            def pipeline(*args, semantic_scholar_deferred=None, resolution=None, **kwargs):
                if resolution is not None:
                    return real_pipeline(*args, resolution=resolution, **kwargs)
                if semantic_scholar_deferred is None:
                    return {"_semantic_scholar_deferred": work}
                resumes.append("p")
                work.task("p")["exc"] = http_mod.ProviderCooldownDeferred(
                    not_before=0, provider="p", physical_429_count=1,
                )
                return {"_semantic_scholar_deferred": work}

            def exhaust_cooldown(*_args, **_kwargs):
                self.assertIsNotNone(transport_telemetry._LOCAL.repo)
                self.assertEqual(transport_telemetry._LOCAL.ref_id, ref["id"])
                return exhausted_resolution

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(
                     resolve_phase._resolve, "exhaust_provider_cooldown",
                     side_effect=exhaust_cooldown,
                 ) as exhaust, \
                 mock.patch.object(
                     resolve_phase._pipeline, "process_source",
                     side_effect=lambda ref, _run, deps: resolve_phase._pipeline.ProcessSourceResult(
                         resolution=deps.resolve_ref(ref), fetched=None, cached_source=None,
                     ),
                 ), \
                 mock.patch.object(
                     resolve_phase, "_maybe_inline_resolve_repair",
                     side_effect=lambda _st, _ref, result, **_kwargs: result,
                 ), \
                 mock.patch.object(resolve_phase.time, "monotonic", return_value=0.0), \
                 mock.patch.object(resolve_phase.time, "sleep") as sleep, \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

            repo = RunRepository.open(run)
            try:
                persisted = repo.get_resolve_result(ref["id"])
            finally:
                repo.close()

        self.assertEqual(resumes, ["p", "p"])
        exhaust.assert_called_once()
        self.assertIn("more than two retry", exhaust.call_args.kwargs["reason"])
        sleep.assert_not_called()
        self.assertIn("rate_limit_exhausted", persisted.reason)
        self.assertEqual(persisted.attempts[-1]["error_type"], "rate_limit")

    def test_discovery_exhaustion_wakes_at_reference_600_second_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                ref = repo.effective_parse_payload()["references"][0]
            finally:
                repo.close()
            work = resolve_mod.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[(http_mod.ProviderCooldownDeferred(
                    not_before=5000, provider="p",
                ), mock.Mock(NAME="p"))],
                state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
            )
            clock, sleeps = [0.0], []

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            def exhaust_cooldown(*_args, **_kwargs):
                self.assertIsNotNone(transport_telemetry._LOCAL.repo)
                self.assertEqual(transport_telemetry._LOCAL.ref_id, ref["id"])
                return discovered

            discovered = {"result": {"status": "unverified"}, "attempts": []}
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=work), \
                 mock.patch.object(resolve_phase, "_persist_discovery_wave"), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", return_value={}), \
                 mock.patch.object(resolve_phase._resolve, "exhaust_provider_cooldown", side_effect=exhaust_cooldown) as exhaust, \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=sleep), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=mock.Mock(close=mock.Mock())), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(sleeps, [600.0])
        exhaust.assert_called_once()
        self.assertIn("600 seconds", exhaust.call_args.kwargs["reason"])

    def test_late_discovery_inherits_provider_episode_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                refs = repo.effective_parse_payload()["references"][:2]
            finally:
                repo.close()
            works = {
                ref["id"]: resolve_mod.ProviderDeferredWork(
                    kind="discovery_provider",
                    tasks=[(http_mod.ProviderCooldownDeferred(
                        not_before=5000, provider="p",
                    ), mock.Mock(NAME="p"))],
                    state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
                )
                for ref in refs
            }
            clock, sleeps = [0.0], []
            discovered = {"result": {"status": "unverified"}, "attempts": []}

            def discover(_run, ref, **_kwargs):
                if ref["id"] == refs[1]["id"]:
                    clock[0] = 300.0
                return works[ref["id"]]

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=discover), \
                 mock.patch.object(resolve_phase, "_persist_discovery_wave"), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", return_value={}), \
                 mock.patch.object(resolve_phase._resolve, "exhaust_provider_cooldown", return_value=discovered) as exhaust, \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=sleep), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=mock.Mock(close=mock.Mock())), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(sleeps, [300.0])
        self.assertEqual(exhaust.call_count, 2)
        self.assertTrue(all(
            "600 seconds" in call.kwargs["reason"] for call in exhaust.call_args_list
        ))

    def test_shared_batch_429_token_is_counted_once_for_provider_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                refs = repo.effective_parse_payload()["references"][:3]
            finally:
                repo.close()
            token = object()
            deferred = {
                ref["id"]: http_mod.ProviderCooldownDeferred(
                    not_before=0, provider="p", physical_429_count=1,
                    physical_429_tokens=(token,),
                )
                for ref in refs
            }
            resumed = []

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if semantic_scholar_deferred is None:
                    return {"_semantic_scholar_deferred": deferred[ref["id"]]}
                resumed.append(ref["id"])
                return {}

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase._resolve, "exhaust_provider_cooldown") as exhaust, \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(resumed, [ref["id"] for ref in refs])
        exhaust.assert_not_called()

    def test_provider_episode_exhausts_split_429_counts_without_resuming(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                refs = repo.effective_parse_payload()["references"][:3]
            finally:
                repo.close()
            tokens = [object() for _ in range(5)]
            deferred = {
                refs[0]["id"]: http_mod.ProviderCooldownDeferred(
                    not_before=100, provider="p", physical_429_count=2,
                    physical_429_tokens=tuple(tokens[:2]),
                ),
                refs[1]["id"]: http_mod.ProviderCooldownDeferred(
                    not_before=200, provider="p", physical_429_count=2,
                    physical_429_tokens=tuple(tokens[2:4]),
                ),
                refs[2]["id"]: http_mod.ProviderCooldownDeferred(
                    not_before=300, provider="p", physical_429_count=1,
                    physical_429_tokens=(tokens[4],),
                ),
            }
            resumed, persisted, finalized = [], [], []
            clock, sleeps = [0.0], []
            exhausted = {
                "result": {"status": "unverified", "via": "p"},
                "attempts": [],
            }

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, resolution=None, **_kwargs):
                if resolution is not None:
                    self.assertIn("trace", resolution)
                    persisted.append(ref["id"])
                    return {}
                if semantic_scholar_deferred is None:
                    return {"_semantic_scholar_deferred": deferred[ref["id"]]}
                resumed.append(ref["id"])
                return {}

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(
                     resolve_phase,
                     "_finalize_resumed_semantic_scholar",
                     side_effect=lambda ref, outcome: finalized.append(ref["id"]) or {
                         "status": outcome["result"]["status"],
                         "attempts": outcome["attempts"],
                         "trace": [],
                     },
                 ) as finalize, \
                 mock.patch.object(resolve_phase._resolve, "exhaust_provider_cooldown", return_value=exhausted) as exhaust, \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds))):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(resumed, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(persisted, [ref["id"] for ref in refs])
        self.assertEqual(exhaust.call_count, 3)
        self.assertEqual(finalize.call_count, 3)
        self.assertEqual(finalized, [ref["id"] for ref in refs])
        self.assertTrue(all(
            "more than two retry" in call.kwargs["reason"] for call in exhaust.call_args_list
        ))

    def test_simultaneous_deferred_sibling_cancel_does_not_enter_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                ref = repo.effective_parse_payload()["references"][0]
            finally:
                repo.close()
            module = object()
            work = resolve_phase._resolve.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[
                    (http_mod.ProviderCooldownDeferred(not_before=0, provider="p"), module),
                    (http_mod.ProviderCooldownDeferred(not_before=0, provider="q"), module),
                ],
                state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
            )
            resolution = {
                "status": "resolved",
                "via": "p",
                "attempts": [],
                "trace": [],
            }
            resumed = []

            def resume(deferred):
                self.assertIs(deferred, work)
                provider = deferred._resume_provider
                resumed.append(provider)
                if provider == "p":
                    work.terminal = True
                    return resolution
                return {"_provider_deferred_cancelled": True}

            def sync(_run, _ref_id, payload, **_kwargs):
                self.assertNotIn("_provider_deferred_cancelled", payload)

            processed = resolve_phase._pipeline.ProcessSourceResult(
                resolution=resolution,
                fetched=None,
                cached_source=None,
            )
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase._resolve, "resolve", side_effect=work), \
                 mock.patch.object(resolve_phase._resolve, "resume_semantic_scholar_cooldown", side_effect=resume), \
                 mock.patch.object(resolve_phase, "_repo_sync_resolve_result", side_effect=sync) as sync_result, \
                 mock.patch.object(resolve_phase._pipeline, "process_source", return_value=processed) as process, \
                 mock.patch.object(resolve_phase, "_maybe_inline_resolve_repair", side_effect=lambda _st, _ref, res, **_kwargs: res), \
                 mock.patch.object(resolve_phase, "_store_resolve_abstract_if_needed", return_value=(None, None, None, None)), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

            self.assertEqual(resumed, ["p", "q"])
            sync_result.assert_called_once()
            process.assert_called_once()

    def test_parallel_completion_preserves_corpus_order_in_provider_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                refs = parse.effective_parse_payload()["references"][:2]
            finally:
                parse.close()
            second_finished = threading.Event()
            completions, resumes = [], []
            deferred = {
                ref["id"]: http_mod.ProviderCooldownDeferred(not_before=0, provider="p")
                for ref in refs
            }

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if semantic_scholar_deferred is not None:
                    resumes.append(ref["id"])
                    return {}
                if ref["id"] == refs[0]["id"]:
                    self.assertTrue(second_finished.wait(2))
                else:
                    second_finished.set()
                completions.append(ref["id"])
                return {"_semantic_scholar_deferred": deferred[ref["id"]]}

            discovery = {"result": {"status": "unverified"}, "attempts": []}
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=2), \
                 mock.patch.object(resolve_phase, "_discover_reference", return_value=discovery), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=mock.Mock(close=mock.Mock())), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(completions[0], refs[1]["id"])
        self.assertEqual(resumes, [refs[0]["id"], refs[1]["id"]])

    def test_parallel_terminal_completion_reports_progress_before_slow_sibling(self):
        """A completed worker must not wait for its sibling to report progress."""
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                refs = repo.effective_parse_payload()["references"][:2]
            finally:
                repo.close()

            fast_completed = threading.Event()
            slow_released = threading.Event()
            first_progress = threading.Event()
            progress_lines = []

            def pipeline(_st, _run, ref, _context, **_kwargs):
                if ref["id"] == refs[0]["id"]:
                    self.assertTrue(slow_released.wait(2))
                else:
                    fast_completed.set()
                return {}

            def progress(message):
                progress_lines.append(message)
                if message == "resolving reference 1/2":
                    first_progress.set()

            session = mock.Mock(close=mock.Mock())
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=2), \
                 mock.patch.object(resolve_phase, "_discover_reference", return_value={"result": {"status": "unverified"}, "attempts": []}), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase, "_progress", side_effect=progress), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=session), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                phase = threading.Thread(
                    target=resolve_phase.phase_resolve, args=({"run_dir": run},),
                )
                phase.start()
                try:
                    self.assertTrue(fast_completed.wait(1))
                    self.assertTrue(first_progress.wait(1))
                finally:
                    slow_released.set()
                    phase.join(2)

            self.assertFalse(phase.is_alive())
            self.assertEqual(
                [line for line in progress_lines if line.startswith("resolving reference ")],
                ["resolving reference 1/2", "resolving reference 2/2"],
            )

    def test_discovery_success_ignores_simultaneous_cancelled_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                ref = parse.effective_parse_payload()["references"][0]
            finally:
                parse.close()
            module = object()
            work = resolve_phase._resolve.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[
                    (http_mod.ProviderCooldownDeferred(not_before=0, provider="p"), module),
                    (http_mod.ProviderCooldownDeferred(not_before=0, provider="q"), module),
                ],
                state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
            )
            discovered = {"result": {"status": "unverified"}, "attempts": []}
            resumed = []

            def resume(deferred, provider):
                self.assertIs(deferred, work)
                resumed.append(provider)
                if provider == "p":
                    work.terminal = True
                    return discovered
                return {"_provider_deferred_cancelled": True}

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=work), \
                 mock.patch.object(resolve_phase._resolve, "resume_semantic_scholar_cooldown", side_effect=resume), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", return_value={}), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=mock.Mock(close=mock.Mock())), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(resumed, ["p", "q"])

    def test_discovery_success_prunes_cancelled_provider_without_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                ref = parse.effective_parse_payload()["references"][0]
            finally:
                parse.close()
            module = object()
            work = resolve_phase._resolve.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[
                    (http_mod.ProviderCooldownDeferred(not_before=10, provider="p"), module),
                    (http_mod.ProviderCooldownDeferred(not_before=0, provider="q"), module),
                ],
                state={"ref": ref, "result": None, "attempts": [], "discovery_only": True},
            )
            discovered = {"result": {"status": "unverified"}, "attempts": []}
            clock, resumed, sleeps = [0.0], [], []

            def resume(deferred, provider):
                self.assertIs(deferred, work)
                resumed.append(provider)
                self.assertEqual(provider, "q")
                work.terminal = True
                return discovered

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=work), \
                 mock.patch.object(resolve_phase._resolve, "resume_semantic_scholar_cooldown", side_effect=resume), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", return_value={}), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=sleep), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=mock.Mock(close=mock.Mock())), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(resumed, ["q"])
        self.assertEqual(sleeps, [])

    def test_deferred_work_redefer_keeps_provider_head_before_next_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                refs = parse.effective_parse_payload()["references"][:2]
            finally:
                parse.close()
            clock, events, sleeps = [0.0], [], []
            module = object()
            x = resolve_phase._resolve.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[(http_mod.ProviderCooldownDeferred(not_before=0, provider="p"), module)],
                state={"ref": refs[0], "result": None, "attempts": [], "discovery_only": True},
            )
            y = resolve_phase._resolve.ProviderDeferredWork(
                kind="discovery_provider",
                tasks=[(http_mod.ProviderCooldownDeferred(not_before=0, provider="p"), module)],
                state={"ref": refs[1], "result": None, "attempts": [], "discovery_only": True},
            )
            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if semantic_scholar_deferred is None:
                    return {"_semantic_scholar_deferred": x if ref["id"] == "r1" else y}
                work = semantic_scholar_deferred
                events.append(ref["id"])
                if ref["id"] == "r1" and len(events) == 1:
                    work.task("p")["exc"] = http_mod.ProviderCooldownDeferred(not_before=5, provider="p")
                    return {"_semantic_scholar_deferred": work}
                if ref["id"] == "r1":
                    work.tasks.pop("p")
                    return {"_semantic_scholar_deferred": work}
                return {}
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds))), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")
        self.assertEqual(events, ["r1", "r1", "r2"])
        self.assertEqual(sleeps, [5.0])

    def test_provider_local_queues_are_independent_and_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                refs = parse.effective_parse_payload()["references"][:3]
            finally:
                parse.close()
            clock, events, sleeps = [0.0], [], []
            deferred = {
                "r1": http_mod.ProviderCooldownDeferred(not_before=5.0, provider="p"),
                "r2": http_mod.ProviderCooldownDeferred(not_before=5.0, provider="p"),
                "r3": http_mod.ProviderCooldownDeferred(not_before=0.0, provider="q"),
            }

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if semantic_scholar_deferred is None:
                    events.append("defer:" + ref["id"])
                    return {"_semantic_scholar_deferred": deferred[ref["id"]]}
                events.append("resume:" + semantic_scholar_deferred.provider + ":" + ref["id"])
                return {}

            def sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=sleep), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=mock.Mock()):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(
            events,
            ["defer:r1", "defer:r2", "defer:r3", "resume:q:r3", "resume:p:r1", "resume:p:r2"],
        )
        self.assertEqual(sleeps, [5.0])

    def test_phase_resolve_runs_ready_reference_before_semantic_cooldown_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            parse = RunRepository.open(run)
            try:
                payload = parse.effective_parse_payload()
            finally:
                parse.close()
            payload["references"] = payload["references"][:2]
            events = []
            deferred = http_mod.SemanticScholarCooldownDeferred(not_before=0.0)

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if ref["id"] == "r1" and semantic_scholar_deferred is None:
                    events.append("deferred")
                    return {"_semantic_scholar_deferred": deferred}
                events.append("resumed" if ref["id"] == "r1" else "ready")
                return {}

            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value=payload), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase, "_repo_sync_resolve_result") as sync:
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

            self.assertEqual(events, ["deferred", "ready", "resumed"])
            self.assertFalse(sync.called, "a deferred admission must not persist a terminal resolve result")

    def test_batch_discovery_finalizes_ready_cohort_before_earlier_cooldown(self):
        old_perf = os.environ.get(perf_mod.ENV_PERF)
        os.environ[perf_mod.ENV_PERF] = "1"
        perf_mod.reset()
        self.addCleanup(perf_mod.reset)
        self.addCleanup(
            lambda: os.environ.pop(perf_mod.ENV_PERF, None)
            if old_perf is None else os.environ.__setitem__(perf_mod.ENV_PERF, old_perf)
        )
        class Session:
            def close(self):
                pass

        refs = [
            {"id": "r1", "ref_number": 1},
            {"id": "r2", "ref_number": 2},
            {"id": "r3", "ref_number": 3},
        ]
        clock = [0.0]
        deferred = http_mod.SemanticScholarCooldownDeferred(not_before=5.0)
        work_deferred = http_mod.SemanticScholarCooldownDeferred(not_before=300.0)
        discovered = {"result": {"status": "unverified"}, "attempts": []}
        events = []
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)

            def discover(_run, ref, **_kwargs):
                events.append(ref["id"])
                if ref["id"] == "r1":
                    raise deferred
                return discovered

            def sleep(_seconds):
                sleeps.append(_seconds)
                repo = RunRepository.open(run)
                try:
                    snapshots = repo.list_integrity_unit_completions(
                        resolve_phase._DISCOVERY_UNIT_GROUP,
                    )
                    expected = ["r2"] if len(sleeps) == 1 else ["r2", "r1"]
                    self.assertEqual([row["unit_id"] for row in snapshots], expected)
                finally:
                    repo.close()
                if len(sleeps) == 1:
                    self.assertIn("barrier:r2", events)
                    self.assertIn("defer-work:r2", events)
                    self.assertNotIn("final:r1", events)
                clock[0] += _seconds

            def barrier(_run, cohort, **kwargs):
                if kwargs.get("prime_pmc_idconv"):
                    events.append("barrier:" + ",".join(ref["id"] for ref in cohort))

            def pipeline(_st, _run, ref, _context, *, semantic_scholar_deferred=None, **_kwargs):
                if ref["id"] == "r2" and semantic_scholar_deferred is None:
                    events.append("defer-work:r2")
                    return {"_semantic_scholar_deferred": work_deferred}
                if ref["id"] == "r2":
                    events.append("resume:r2")
                    return {}
                events.append("final:" + ref["id"])
                return {}

            checkpoint = mock.Mock()
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_final_resolve_ids", return_value={"r3"}), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=discover), \
                 mock.patch.object(resolve_phase._resolve, "resume_semantic_scholar_cooldown", return_value=discovered), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=pipeline), \
                 mock.patch.object(resolve_phase.time, "sleep", side_effect=sleep), \
                 mock.patch.object(resolve_phase.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=Session()), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=Session()), \
                 mock.patch.object(resolve_phase, "_DISCOVERY_CHECKPOINT_CHUNK_SIZE", 1), \
                 mock.patch.object(resolve_phase, "_prime_phase_batch_sessions", side_effect=barrier), \
                 mock.patch.object(resolve_phase, "_repo_sync_resolve_result"):
                self.assertEqual(resolve_phase.phase_resolve({
                    "run_dir": run, "_integrity_unit_checkpoint": checkpoint,
                }), "fetch")

            repo = RunRepository.open(run)
            try:
                snapshots = repo.list_integrity_unit_completions(resolve_phase._DISCOVERY_UNIT_GROUP)
            finally:
                repo.close()
        self.assertEqual([row["unit_id"] for row in snapshots], ["r2", "r1"])
        self.assertEqual(
            events,
            [
                "r1", "r2", "barrier:r2", "defer-work:r2", "final:r3",
                "barrier:r1", "final:r1", "resume:r2",
            ],
        )
        self.assertEqual(sleeps, [5.0, 295.0])
        waits = [row for row in perf_mod.summary()["spans"] if row["phase"] == "resolve_wait"]
        self.assertEqual(waits[0]["key"], "api.semanticscholar.org")
        self.assertEqual(waits[0]["count"], 2)
        self.assertEqual(
            checkpoint.call_args_list,
            [
                mock.call(resolve_phase._DISCOVERY_UNIT_GROUP, "wave"),
                mock.call(resolve_phase._DISCOVERY_UNIT_GROUP, "wave"),
            ],
        )

    def test_batch_discovery_checkpoints_completed_chunk_before_interruption(self):
        class Session:
            def close(self):
                pass

        refs = [
            {"id": "r1", "ref_number": 1},
            {"id": "r2", "ref_number": 2},
            {"id": "r3", "ref_number": 3},
        ]
        state = {"result": {"status": "unverified"}, "attempts": []}
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)

            def interrupted_discovery(_run, ref, **_kwargs):
                if ref["id"] == "r3":
                    raise RuntimeError("interrupted after first checkpoint")
                return state

            common = [
                mock.patch.object(
                    resolve_phase, "_load_parse_payload",
                    return_value={"references": refs},
                ),
                mock.patch.object(resolve_phase, "cross_reference_map", return_value={}),
                mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()),
                mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1),
                mock.patch.object(resolve_phase, "_prime_phase_batch_sessions"),
                mock.patch.object(resolve_phase, "_resolve_reference_pipeline"),
                mock.patch.object(
                    resolve_phase._resolve, "new_openalex_batch_session", return_value=Session(),
                ),
                mock.patch.object(
                    resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None,
                ),
                mock.patch.object(
                    resolve_phase._resolve, "new_pubmed_batch_session", return_value=None,
                ),
                mock.patch.object(
                    resolve_phase._resolve, "new_pmc_idconv_batch_session", return_value=None,
                ),
                mock.patch.object(
                    resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=Session(),
                ),
                mock.patch.object(resolve_phase, "_DISCOVERY_CHECKPOINT_CHUNK_SIZE", 2),
            ]
            with contextlib.ExitStack() as stack:
                for patch in common:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        resolve_phase, "_discover_reference", side_effect=interrupted_discovery,
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "first checkpoint"):
                    resolve_phase.phase_resolve({"run_dir": run})

            repo = RunRepository.open(run)
            try:
                snapshots = repo.list_integrity_unit_completions(
                    resolve_phase._DISCOVERY_UNIT_GROUP,
                )
            finally:
                repo.close()
            self.assertEqual([row["unit_id"] for row in snapshots], ["r1", "r2"])

            resumed = []
            with contextlib.ExitStack() as stack:
                for patch in common:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        resolve_phase, "_discover_reference",
                        side_effect=lambda _run, ref, **_kwargs: resumed.append(ref["id"]) or state,
                    )
                )
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(resumed, ["r3"])

    def test_phase_resolve_keeps_new_context_only_in_runtime_state(self):
        context = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            st = {"run_dir": run}
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": []}), \
                 mock.patch.object(resolve_phase._fetch, "new_fetch_run_context", return_value=context), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None):
                self.assertEqual(resolve_phase.phase_resolve(st), "fetch")

        self.assertIs(st["_fetch_run_context"], context)
        context.disable_persistence.assert_not_called()

    def _make_run(self, tmp: str) -> str:
        run = os.path.abspath(tmp)
        repo = RunRepository.create(
            run,
            run_id="run-pipeline-parallel",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style="vancouver",
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        references = []
        claims = []
        citations = []
        for idx in range(1, 5):
            ref_id = f"r{idx}"
            claim_id = f"c{idx}"
            references.append(
                {
                    "id": ref_id,
                    "ref_number": idx,
                    "raw_entry": f"Author {idx}. Example paper {idx}. 202{idx}.",
                    "title": f"Example paper {idx}",
                    "source_type": "article",
                    "source_kind": "article_like",
                    "doi": f"10.1000/example-{idx}",
                }
            )
            claims.append({"id": claim_id, "sentence": f"Claim {idx} [{idx}]."})
            citations.append({"claim_id": claim_id, "ref_id": ref_id, "ref_number": idx})
        repo.replace_parse_payload(claims=claims, references=references, citations=citations)
        repo.close()
        return run

    def _normalize(self, value):
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if key in {
                    "created_at",
                    "updated_at",
                    "checked_at",
                    "recorded_at",
                    "answered_at",
                    "applied_at",
                    "submitted_at",
                    "started_at",
                    "heartbeat_at",
                    "ended_at",
                    "fetch_attempt_id",
                    "answer_id",
                }:
                    continue
                if (
                    isinstance(item, str)
                    and key.endswith("_path")
                    and "sources" in item
                ):
                    marker = item.lower().rfind("sources")
                    item = item[marker:].replace("\\", "/") if marker >= 0 else item
                cleaned[key] = self._normalize(item)
            return cleaned
        if isinstance(value, list):
            normalized = [self._normalize(item) for item in value]
            if normalized and all(isinstance(item, dict) for item in normalized):
                normalized = sorted(
                    normalized,
                    key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                )
            return normalized
        return value

    def _export_state(self, run: str) -> dict:
        repo = RunRepository.open(run)
        try:
            exported = repo.export_report_inputs()
            fetch_attempts = {
                ref["id"]: repo.list_fetch_attempts(ref["id"])
                for ref in exported["parse"]["references"]
            }
        finally:
            repo.close()
        return self._normalize(
            {
                "parse": exported["parse"],
                "resolve": exported["resolve"],
                "source_texts": exported["source_texts"],
                "tasks": exported["tasks"],
                "task_answers": exported["task_answers"],
                "verification_raw": exported["verification_raw"],
                "gate": exported["gate"],
                "fetch_attempts": fetch_attempts,
            }
        )

    def _run_phase(self, *, max_workers: int) -> tuple[dict, dict[str, list[float]], list[float]]:
        http_mod.reset_response_memo()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                content_store.ENV_STATE_DIR: os.path.join(tmp, "state"),
                "CALLIMACHUS_JOURNAL_AUTHORITY_DB": "",
            },
        ), mock.patch.object(
            journal_catalog,
            "ensure_for_resolve",
            return_value={"status": "unavailable", "updated": False, "metadata": None},
        ):
            content_store.preflight(tmp)
            run = self._make_run(tmp)
            parse = driver._load_parse_payload(run)
            total = len(parse.get("references") or [])
            resolve_barrier = None if max_workers <= 1 else threading.Barrier(total, timeout=2)
            fetch_barrier = None if max_workers <= 1 else threading.Barrier(total, timeout=2)
            clock = _FakeClock()
            host_times: dict[str, list[float]] = {}
            host_lock = threading.Lock()
            limiter = host_limiter.HostLimiter(
                rules={
                    "openalex.org": host_limiter.HostRate.per_second(2.0),
                    "crossref.org": host_limiter.HostRate.per_second(2.0),
                },
                default_rate=host_limiter.HostRate.per_second(1000.0),
                time_fn=clock.monotonic,
                sleep_fn=clock.sleep,
            )

            saved_resolve = driver._resolve.resolve
            saved_discover = driver._resolve._discover_resolution
            saved_finalize = driver._resolve._finalize_discovery
            saved_fetch = driver._fetch.fetch_fulltext
            saved_resolve_open = resolve_mod.open_request
            saved_fetch_http_open = fetch_http_headers_mod.open_request
            saved_limiter = host_limiter.get_shared_limiter()

            def fake_open(req, timeout=None):
                host = host_limiter.host_for_url(req.full_url)
                with host_lock:
                    host_times.setdefault(host, []).append(clock.monotonic())
                return _FakeResp(req.full_url)

            def fake_resolve(ref):
                resolve_mod._get(f"https://api.openalex.org/works/{ref['id']}")
                if resolve_barrier is not None:
                    resolve_barrier.wait()
                return current_resolution(ref, {
                    "ref_id": ref["id"],
                    "status": "resolved",
                    "via": "openalex_search",
                    "matched_title": ref["title"],
                    "retracted": False,
                    "fulltext_exists": True,
                    "oa_status": "open",
                    "attempts": [{"via": "openalex_search", "status": "resolved"}],
                    "fulltext_links": [{"url": f"https://doi.org/{ref['doi']}", "content_type": "doi"}],
                })

            def fake_fetch(ref, run_dir, resolve_result, **_kwargs):
                fetch_mod._get(f"https://api.crossref.org/works/{ref['id']}", profile="api")
                if fetch_barrier is not None:
                    fetch_barrier.wait()
                entry = driver._sources.store_text(
                    run_dir,
                    ref,
                    "fulltext",
                    "webfetch",
                    (f"Full text for {ref['id']} with enough distinctive tokens. " * 12).strip(),
                    source_ref=f"https://example.org/{ref['id']}.pdf",
                    mapping="auto",
                    signal="doi",
                    score=1.0,
                )
                return {
                    "status": "stored",
                    "stored_as": entry["stored_as"],
                    "method": "webfetch",
                    "pdf_url": f"https://example.org/{ref['id']}.pdf",
                    "corroborate_signal": "doi",
                    "corroborate_score": 1.0,
                    "fetch_trace": {
                        "direct_text": {"attempts": []},
                        "execution": {
                            "attempts": [
                                {
                                    "queue_index": 0,
                                    "batch_index": 0,
                                    "method": "webfetch",
                                    "url": f"https://example.org/{ref['id']}.pdf",
                                    "kind": "pdf",
                                    "final_url": f"https://example.org/{ref['id']}.pdf",
                                    "status": 200,
                                    "content_type": "application/pdf",
                                    "outcome": "stored",
                                }
                            ]
                        },
                    },
                }

            try:
                host_limiter.set_shared_limiter(limiter)
                resolve_mod.open_request = fake_open
                fetch_http_headers_mod.open_request = fake_open
                driver._resolve.resolve = fake_resolve
                driver._resolve._discover_resolution = lambda ref: {
                    "result": fake_resolve(ref), "attempts": [],
                }
                driver._resolve._finalize_discovery = lambda _ref, state: state["result"]
                driver._fetch.fetch_fulltext = fake_fetch
                next_phase = driver.phase_resolve(
                    {
                        "run_dir": run,
                        "phase": "resolve",
                        "accuracy": "standard",
                        "fetch_workers": max_workers,
                    }
                )
                self.assertEqual(next_phase, "fetch")
                snapshot = self._export_state(run)
            finally:
                host_limiter.set_shared_limiter(saved_limiter)
                resolve_mod.open_request = saved_resolve_open
                fetch_http_headers_mod.open_request = saved_fetch_http_open
                driver._resolve.resolve = saved_resolve
                driver._resolve._discover_resolution = saved_discover
                driver._resolve._finalize_discovery = saved_finalize
                driver._fetch.fetch_fulltext = saved_fetch
            return snapshot, host_times, list(clock.sleeps)

    def test_parallel_pipeline_matches_serial_baseline_and_respects_host_rates(self):
        serial_snapshot, _serial_host_times, _serial_sleeps = self._run_phase(max_workers=1)
        parallel_snapshot, host_times, sleeps = self._run_phase(max_workers=4)

        self.assertEqual(parallel_snapshot, serial_snapshot)
        self.assertNotIn("database is locked", json.dumps(parallel_snapshot).lower())
        self.assertTrue(sleeps, "parallel run should have exercised the shared host limiter")

        for host in ("api.openalex.org", "api.crossref.org"):
            times = sorted(host_times.get(host) or [])
            self.assertEqual(len(times), 4)
            self.assertGreaterEqual(round(times[-1] - times[0], 3), 1.5)

    def test_batch_sessions_are_closed_when_phase_setup_raises(self):
        class Session:
            closed = False

            def close(self):
                self.closed = True

        openalex_session = Session()
        semantic_session = Session()
        pubmed_session = Session()
        scholar_archive_session = Session()
        with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": []}), \
             mock.patch.object(resolve_phase, "_resolve_manuscript_title"), \
             mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=openalex_session), \
             mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=semantic_session), \
             mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=pubmed_session), \
             mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=scholar_archive_session), \
             mock.patch.object(resolve_phase, "_resolve_worker_count", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                resolve_phase.phase_resolve({"run_dir": "ignored"})
        self.assertTrue(openalex_session.closed)
        self.assertTrue(semantic_session.closed)
        self.assertTrue(pubmed_session.closed)
        self.assertTrue(scholar_archive_session.closed)

    def test_discovery_worker_binds_scholar_archive_circuit_session(self):
        session = object()
        events = []

        class Binding:
            def __enter__(self):
                events.append(("enter", session))

            def __exit__(self, *_exc):
                events.append(("exit", session))

        with mock.patch.object(
            resolve_phase._resolve,
            "bind_scholar_archive_circuit_session",
            return_value=Binding(),
        ), mock.patch.object(
            resolve_phase._resolve,
            "_discover_resolution",
            side_effect=lambda _ref: (
                events.append(("discover", session))
                or {"result": {"status": "unverified"}, "attempts": []}
            ),
        ):
            result = resolve_phase._discover_reference(
                "unused",
                {"id": "r1"},
                scholar_archive_circuit_session=session,
            )

        self.assertEqual(result["result"]["status"], "unverified")
        self.assertEqual(events, [
            ("enter", session),
            ("discover", session),
            ("exit", session),
        ])

    def test_phase_passes_and_closes_scholar_archive_circuit_session(self):
        class Session:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        circuit_session = Session()
        received = []
        refs = [{"id": "r1", "ref_number": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=lambda *_args, **kwargs: received.append(kwargs)), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=circuit_session):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(received[0]["scholar_archive_circuit_session"], circuit_session)
        self.assertTrue(circuit_session.closed)

    def test_split_phase_passes_scholar_archive_circuit_to_both_worker_stages(self):
        class BatchSession:
            def close(self):
                pass

        class CircuitSession:
            def close(self):
                pass

        circuit_session = CircuitSession()
        discovery_sessions = []
        finalization_sessions = []
        refs = [{"id": "r1", "ref_number": 1}]
        discovered = {"result": {"status": "unverified"}, "attempts": []}
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=lambda *_args, **kwargs: discovery_sessions.append(kwargs["scholar_archive_circuit_session"]) or discovered), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=lambda *_args, **kwargs: finalization_sessions.append(kwargs["scholar_archive_circuit_session"])), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=BatchSession()), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_scholar_archive_circuit_session", return_value=circuit_session), \
                 mock.patch.object(resolve_phase._resolve, "prime_openalex_batch_session"):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(discovery_sessions, [circuit_session])
        self.assertEqual(finalization_sessions, [circuit_session])

    def test_phase_primes_only_real_refs_before_workers_in_manuscript_order(self):
        class Session:
            def close(self):
                pass

        events = []
        refs = [
            {"id": "r1", "ref_number": 1},
            {"id": "pointer", "ref_number": 2},
            {"id": "r2", "ref_number": 3},
            {"id": "xref", "ref_number": 4},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value={"pointer"}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={"xref": ("r1", None)}), \
                 mock.patch.object(resolve_phase, "_record_manuscript_pointer"), \
                 mock.patch.object(resolve_phase, "_inherit_cross_reference", return_value=True), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=lambda _run, ref, **_kw: {"result": {"status": "unverified"}, "attempts": []}), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", side_effect=lambda _st, _run, ref, _ctx, **_kw: events.append(("worker", ref["id"]))), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=Session()), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=Session()), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=Session()), \
                 mock.patch.object(resolve_phase._resolve, "prime_openalex_batch_session", side_effect=lambda _session, prime_refs: events.append(("openalex", [r["id"] for r in prime_refs]))), \
                 mock.patch.object(resolve_phase._resolve, "prime_semantic_scholar_batch_session", side_effect=lambda _session, prime_refs: events.append(("semantic", [r["id"] for r in prime_refs]))), \
                 mock.patch.object(resolve_phase._resolve, "prime_pubmed_batch_session", side_effect=lambda _session, prime_refs: events.append(("pubmed", [r["id"] for r in prime_refs]))):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")
        self.assertEqual(events[:3], [
            ("openalex", ["r1", "r2"]),
            ("semantic", ["r1", "r2"]),
            ("pubmed", ["r1", "r2"]),
        ])
        self.assertEqual(events[3:6], [
            ("openalex", ["r1", "r2"]),
            ("semantic", ["r1", "r2"]),
            ("pubmed", ["r1", "r2"]),
        ])
        self.assertEqual(events[6:], [("worker", "r1"), ("worker", "r2")])

    def test_phase_second_prime_uses_discovered_dois_in_manuscript_order(self):
        class Session:
            def close(self):
                pass

        refs = [
            {"id": "r1", "ref_number": 1},
            {"id": "r2", "ref_number": 2},
        ]
        waves = []
        discovered = {
            "r1": {"result": {
                "status": "resolved",
                "resolved_identifier": {
                    "type": "doi", "value": "10.1/one", "validated_via": "crossref",
                },
            }, "attempts": []},
            "r2": {"result": {
                "status": "resolved",
                "fulltext_links": [{
                    "url": "https://doi.org/10.1/two", "content_type": "doi",
                }],
            }, "attempts": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=lambda _run, ref, **_kw: discovered[ref["id"]]), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline"), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=Session()), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "prime_openalex_batch_session", side_effect=lambda _session, prime_refs: waves.append([r.get("doi") for r in prime_refs])):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")
        self.assertEqual(waves, [[None, None], ["10.1/one", "10.1/two"]])

    def test_pmc_converter_primes_only_after_discovery_with_effective_pmids(self):
        class Session:
            closed = False

            def close(self):
                self.closed = True

        refs = [
            {"id": "r1", "ref_number": 1},
            {"id": "r2", "ref_number": 2},
        ]
        discovered = {
            "r1": {"result": {
                "status": "resolved",
                "resolved_identifier": {
                    "type": "pmid", "value": "101", "validated_via": "pubmed",
                },
            }, "attempts": []},
            "r2": {"result": {
                "status": "resolved",
                "resolved_identifier": {
                    "type": "pmid", "value": "202", "validated_via": "pubmed",
                },
            }, "attempts": []},
        }
        session = Session()
        waves = []
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(
                resolve_phase, "_load_parse_payload",
                return_value={"references": refs},
            ), mock.patch.object(
                resolve_phase, "cross_reference_map", return_value={},
            ), mock.patch.object(
                resolve_phase, "manuscript_pointer_ids", return_value=set(),
            ), mock.patch.object(
                resolve_phase, "_resolve_worker_count", return_value=1,
            ), mock.patch.object(
                resolve_phase, "_discover_reference",
                side_effect=lambda _run, ref, **_kw: discovered[ref["id"]],
            ), mock.patch.object(
                resolve_phase, "_resolve_reference_pipeline",
            ), mock.patch.object(
                resolve_phase._resolve, "new_openalex_batch_session",
                return_value=None,
            ), mock.patch.object(
                resolve_phase._resolve, "new_semantic_scholar_batch_session",
                return_value=None,
            ), mock.patch.object(
                resolve_phase._resolve, "new_pubmed_batch_session",
                return_value=None,
            ), mock.patch.object(
                resolve_phase._resolve, "new_pmc_idconv_batch_session",
                return_value=session,
            ), mock.patch.object(
                resolve_phase._resolve, "prime_pmc_idconv_batch_session",
                side_effect=lambda _session, prime_refs: waves.append(
                    [ref.get("pmid") for ref in prime_refs]
                ),
            ):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertEqual(waves, [["101", "202"]])
        self.assertTrue(session.closed)

    def test_split_resolve_records_additive_discovery_and_finalization_spans(self):
        ref = {"id": "r1", "ref_number": 1, "raw_entry": "Example"}
        state = {"result": {"status": "unverified"}, "attempts": []}
        perf_mod.reset()
        try:
            with mock.patch.dict(os.environ, {perf_mod.ENV_PERF: "1"}), \
                 mock.patch.object(
                     resolve_phase._resolve, "_discover_resolution", return_value=state,
                 ), \
                 mock.patch.object(
                     resolve_phase._resolve, "_finalize_discovery", return_value={"done": True},
                 ):
                discovered = resolve_phase._discover_reference("unused", ref)
                finalized = resolve_phase._finalize_reference_discovery(ref, discovered)
            spans = {
                (row["phase"], row["key"]): row["count"]
                for row in perf_mod.summary()["spans"]
            }
        finally:
            perf_mod.reset()
        self.assertEqual(finalized, {"done": True})
        self.assertEqual(spans, {
            ("resolve", "discovery"): 1,
            ("resolve", "finalization"): 1,
        })

    def test_phase_priming_excludes_persisted_fulltext(self):
        class Row:
            def __init__(self, ref_id, tier):
                self.ref_id, self.tier = ref_id, tier

        repo = mock.Mock()
        repo.list_source_texts.return_value = [
            Row("r2", "fulltext"), Row("r3", "fulltext"),
        ]
        repo.list_resolve_results.return_value = [Row("r3", "resolved")]
        session = object()
        captured = []
        refs = [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]
        with mock.patch.object(resolve_phase, "_repo_open", return_value=repo), \
             mock.patch.object(resolve_phase._resolve, "prime_openalex_batch_session", side_effect=lambda _session, prime_refs: captured.append([r["id"] for r in prime_refs])):
            resolve_phase._prime_phase_batch_sessions(
                "missing-run", refs, openalex_batch_session=session,
            )
        self.assertEqual(captured, [["r1", "r2"]])
        repo.close.assert_called_once()

    def test_phase_resumes_completed_discovery_without_rerunning_it(self):
        ref = {
            "id": "r1", "ref_number": 1, "title": "Recovered title",
            "raw_entry": "Author. Recovered title.", "source_type": "article",
        }
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                repo.append_integrity_unit_completion(
                    "resolve_discovery", "r1",
                    {"state": {"result": {"status": "unverified"}, "attempts": []}},
                )
            finally:
                repo.close()
            pipeline = mock.Mock()
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": [ref]}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase, "_discover_reference", side_effect=AssertionError("reran discovery")), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", pipeline):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")
            self.assertEqual(pipeline.call_args.kwargs["discovery_state"], {
                "result": {"status": "unverified"}, "attempts": [],
            })

    def test_phase_resume_with_all_final_results_skips_resolve_pipeline(self):
        refs = [
            {"id": "r1", "ref_number": 1, "raw_entry": "One"},
            {"id": "r2", "ref_number": 2, "raw_entry": "Two"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                for ref in refs:
                    repo.upsert_resolve_result(
                        ref["id"],
                        current_resolution(ref, {
                            "ref_id": ref["id"],
                            "ref_number": ref["ref_number"],
                            "status": "unverified",
                            "reason": "final result",
                            "reference_status_tag": "unverified",
                            "fabrication_risk": "unknown",
                            "attempts": [],
                        }),
                        trace_state="produced",
                    )
            finally:
                repo.close()

            pipeline = mock.Mock()
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", pipeline), \
                 mock.patch.object(resolve_phase, "_resolve_manuscript_title") as manuscript_title:
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        pipeline.assert_not_called()
        manuscript_title.assert_called_once_with(run)

    def test_phase_resume_with_partial_final_results_resolves_missing_references(self):
        refs = [
            {"id": "r1", "ref_number": 1, "raw_entry": "One"},
            {"id": "r2", "ref_number": 2, "raw_entry": "Two"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                repo.upsert_resolve_result(
                    "r1",
                    current_resolution(refs[0], {
                        "ref_id": "r1",
                        "ref_number": 1,
                        "status": "unverified",
                        "reason": "final result",
                        "reference_status_tag": "unverified",
                        "fabrication_risk": "unknown",
                        "attempts": [],
                    }),
                    trace_state="produced",
                )
            finally:
                repo.close()

            pipeline = mock.Mock()
            with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": refs}), \
                 mock.patch.object(resolve_phase, "cross_reference_map", return_value={}), \
                 mock.patch.object(resolve_phase, "manuscript_pointer_ids", return_value=set()), \
                 mock.patch.object(resolve_phase, "_resolve_worker_count", return_value=1), \
                 mock.patch.object(resolve_phase, "_resolve_reference_pipeline", pipeline), \
                 mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=None), \
                 mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", return_value=None):
                self.assertEqual(resolve_phase.phase_resolve({"run_dir": run}), "fetch")

        self.assertIn("r2", [call.args[2]["id"] for call in pipeline.call_args_list])

    def test_final_resolution_is_authoritative_over_discovery_snapshot(self):
        ref = {
            "id": "r1", "ref_number": 1, "raw_entry": "Author. Final title.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            repo = RunRepository.open(run)
            try:
                repo.append_integrity_unit_completion(
                    "resolve_discovery", "r1",
                    {"state": {
                        "result": {"status": "unverified", "matched_title": "Stale title"},
                        "attempts": [],
                    }},
                )
                repo.upsert_resolve_result(
                    "r1",
                    current_resolution(ref, {
                        "ref_id": "r1",
                        "ref_number": 1,
                        "status": "unverified",
                        "via": None,
                        "matched_title": "Final title",
                        "reason": "final result",
                        "reference_status_tag": "unverified",
                        "fabrication_risk": "unknown",
                        "attempts": [],
                    }),
                    trace_state="produced",
                )
            finally:
                repo.close()

            self.assertEqual(resolve_phase._load_discovery_snapshots(run), {})

    def test_discovery_exception_is_isolated_and_skips_landing_fetch(self):
        failed_ref = {"id": "r1", "ref_number": 1, "raw_entry": "Broken ref"}
        state = {
            "exception_result": {
                "ref_id": "r1",
                "ref_number": 1,
                "status": "unresolved",
                "via": "resolver_exception",
                "reason": "RuntimeError: boom",
                "attempts": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            with mock.patch.object(resolve_phase._pipeline, "process_source") as process_source:
                resolve_phase._resolve_reference_pipeline(
                    {
                        "run_dir": run,
                        "accuracy": "standard",
                        # Resolve exceptions have historically remained eligible
                        # for the explicit repair path.  Disable repair here so
                        # this assertion targets the landing-fetch boundary.
                        "no_fetch": True,
                    },
                    run,
                    failed_ref,
                    fetch_context=object(),
                    discovery_state=state,
                )
            process_source.assert_not_called()
            repo = RunRepository.open(run)
            try:
                result = repo.get_resolve_result("r1")
            finally:
                repo.close()
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.via, "resolver_exception")
        self.assertIsNone(result.trace)

    def test_discovery_wave_rotates_integrity_checkpoint_once_after_persistence(self):
        states = [
            ({"id": "r1"}, {"result": {"status": "unverified"}, "attempts": []}),
            ({"id": "r2"}, {"result": {"status": "unverified"}, "attempts": []}),
        ]
        checkpoint = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            run = self._make_run(tmp)
            resolve_phase._persist_discovery_wave(
                {"_integrity_unit_checkpoint": checkpoint}, run, states,
            )
            repo = RunRepository.open(run)
            try:
                rows = repo.list_integrity_unit_completions("resolve_discovery")
            finally:
                repo.close()
        self.assertEqual([row["unit_id"] for row in rows], ["r1", "r2"])
        checkpoint.assert_called_once_with("resolve_discovery", "wave")

    def test_phase_priming_is_inactive_without_enabled_sessions(self):
        with mock.patch.object(resolve_phase, "_repo_open", side_effect=AssertionError("disabled batch opened repository")):
            resolve_phase._prime_phase_batch_sessions("ignored", [{"id": "r1"}])

    def test_openalex_session_closes_when_semantic_scholar_setup_raises(self):
        class Session:
            closed = False

            def close(self):
                self.closed = True

        openalex_session = Session()
        with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": []}), \
             mock.patch.object(resolve_phase, "_resolve_manuscript_title"), \
             mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=openalex_session), \
             mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                resolve_phase.phase_resolve({"run_dir": "ignored"})
        self.assertTrue(openalex_session.closed)

    def test_earlier_sessions_close_when_pubmed_setup_raises(self):
        class Session:
            closed = False

            def close(self):
                self.closed = True

        openalex_session = Session()
        semantic_session = Session()
        with mock.patch.object(resolve_phase, "_load_parse_payload", return_value={"references": []}), \
             mock.patch.object(resolve_phase, "_resolve_manuscript_title"), \
             mock.patch.object(resolve_phase._resolve, "new_openalex_batch_session", return_value=openalex_session), \
             mock.patch.object(resolve_phase._resolve, "new_semantic_scholar_batch_session", return_value=semantic_session), \
             mock.patch.object(resolve_phase._resolve, "new_pubmed_batch_session", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                resolve_phase.phase_resolve({"run_dir": "ignored"})
        self.assertTrue(openalex_session.closed)
        self.assertTrue(semantic_session.closed)


if __name__ == "__main__":
    unittest.main()
