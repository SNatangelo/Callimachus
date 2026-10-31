# tests/verify/claim_evidence/test_phase8_conformance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused offline Phase 8 conformance checks."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict

import pytest

import core.verify.claim_evidence.runtime as runtime_module
from core.infra.db import RunRepository
from core.report.render import render
from core.verify.backends.errors import BackendError
from core.verify.claim_evidence import ClaimEvidenceRuntime
from core.verify.claim_evidence.adapters.llm.credentials import Credential, CredentialResolver
from core.verify.claim_evidence.adapters.llm.registry import ProviderAdapter, ProviderRegistry
from core.verify.claim_evidence.adapters.llm.transport import ClaimEvidenceTransport
from core.verify.claim_evidence.adapters.runtime_repository import RuntimeRepository
from core.verify.claim_evidence.application.scheduler import ClaimEvidenceScheduler
from core.verify.claim_evidence.domain.pacing import PacingPolicy
from core.verify.claim_evidence.domain.jury1_decisions import (
    SupportGateDecision,
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    TopicGateDecision,
)
from core.verify.claim_evidence.domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    TopicGateAnswer,
)
from core.verify.claim_evidence.domain.types import (
    LogicalRequest, ProviderLane, ProviderPolicy,
    TechnicalFailure,
)
from core.verify.claim_evidence.domain.fingerprint import provider_prompt_fingerprint


ENV = {
    "CITATION_VERIFIER_VERIFY_BACKENDS": "alpha",
    "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
    "ALPHA_KEYS": "key-one",
    "ALPHA_MODELS": "model-one",
}
PROVIDERS = (("alpha", "ALPHA_KEYS", "ALPHA_MODELS", False),)


class _Clock:
    def __init__(self):
        self.now, self.sleeps = 0, []
        self._lock = threading.Lock()

    def clock(self):
        with self._lock:
            return self.now

    def sleep(self, seconds):
        with self._lock:
            milliseconds = round(seconds * 1000)
            self.sleeps.append(milliseconds)
            self.now += milliseconds


class _Limiter:
    def __init__(self, cap):
        self._semaphore, self._lock = threading.BoundedSemaphore(cap), threading.Lock()
        self.active = self.maximum = 0

    def acquire(self, timeout=None):
        got = self._semaphore.acquire(timeout=timeout)
        if got:
            with self._lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
        return got

    def release(self):
        with self._lock:
            self.active -= 1
        self._semaphore.release()


def _provider():
    alias, fingerprint, model = "alpha:1", "f" * 64, "model-one"
    lane = ProviderLane(alias, fingerprint, model, True, True)
    return (ProviderPolicy("alpha", (alias,), (fingerprint,), (model,), "positional_exclusive", (lane,)),)


def _request(name):
    payload = json.dumps({"request": name}).encode()
    return LogicalRequest("support_gate", name, payload, provider_prompt_fingerprint("test system prompt", payload), 1, "test system prompt")


def _answer(request):
    if request.stage == "support_gate":
        return SupportGateAnswer(
            request.logical_request_id, request.payload_fingerprint,
            SupportGateDecision(False),
        )
    if request.stage == "contrary_gate":
        return ContraryGateAnswer(
            request.logical_request_id, request.payload_fingerprint,
            ContraryGateDecision(False),
        )
    if request.stage == "topic_gate":
        return TopicGateAnswer(
            request.logical_request_id, request.payload_fingerprint,
            TopicGateDecision(True),
        )
    if request.stage == "explanation_evidence":
        return ExplanationEvidenceAnswer(
            request.logical_request_id,
            request.payload_fingerprint,
            ExplanationEvidenceDecision(
                "The source is related but supplies no usable support.",
                None,
                None,
                None,
                (),
                None,
            ),
        )
    raise AssertionError(f"unexpected stage: {request.stage}")


def _repo(path, run_id):
    path.mkdir()
    repository = RunRepository.create(
        str(path), run_id=run_id, input_path="input.pdf",
        input_sha256="a" * 64, accuracy="standard", style=None,
        model_id=None, http_profile=None, challenge_mode=None,
        fixture_fingerprint="fixture",
    )
    repository.replace_parse_payload(
        claims=[], references=[], citations=[],
    )
    return repository


def _task(claim_id):
    payload, context = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Claim", "context_window": "Manuscript context.", "marker_raw": "[1]"}, "Source text",
    )
    return {"kind": "claim_evidence", "semantic_contract": ClaimEvidenceRuntime.contract_id,
            "claim_id": claim_id, "ref_id": "ref-1", "scope": "fulltext_complete",
            "claim_evidence_payload": payload, "effective_context": context}


def test_phase8_concurrency_cap_dedup_and_waits_are_slot_free():
    """More than four concurrent logical requests never exceed the physical cap."""
    clock, limiter = _Clock(), _Limiter(3)
    wait_started, release_wait, release_calls = (threading.Event() for _ in range(3))

    class Transport:
        def __init__(self):
            self.calls, self._lock = [], threading.Lock()

        def dispatch(self, request, assignment):
            with self._lock:
                self.calls.append((request.logical_request_id, assignment))
                number = len(self.calls)
            if number == 1:
                return TechnicalFailure(request.stage, request.logical_request_id,
                                        request.payload_fingerprint, "rate_limited",
                                        retry_after_seconds=5, http_status=429)
            release_calls.wait(1)
            return _answer(request)

    transport = Transport()

    def sleeper(seconds):
        if round(seconds * 1000) >= 5_000 and not wait_started.is_set():
            assert len(transport.calls) == 1
            wait_started.set()
            assert release_wait.wait(1)
        clock.sleep(seconds)

    scheduler = ClaimEvidenceScheduler(_provider(), "seed", transport,
        aggregate_cap=3, limiter=limiter, pacing=PacingPolicy(100),
        clock_ms=clock.clock, sleeper=sleeper, attempt_timeout_ms=2_000)
    scheduler.dispatch(_request("rate"))
    cooling = threading.Thread(target=lambda: scheduler.dispatch(_request("cooling")))
    cooling.start()
    assert wait_started.wait(1)
    assert limiter.active == 0
    assert len(transport.calls) == 1
    release_wait.set()
    logical = [_request(f"request-{i}") for i in range(8)]
    replies = []
    workers = [threading.Thread(target=lambda item=item: replies.append(scheduler.dispatch(item)))
               for item in logical for _ in range(2)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 1
    while len(transport.calls) < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert limiter.maximum == 3
    release_calls.set()
    cooling.join(1)
    for worker in workers:
        worker.join(1)
    assert len(replies) == 16
    assert all(isinstance(reply, SupportGateAnswer) for reply in replies)
    assert len(transport.calls) == 10
    assert {name for name, _ in transport.calls} == {"rate", "cooling", *(r.logical_request_id for r in logical)}
    assert clock.sleeps[0] == 5_000


def test_phase8_second_fresh_run_reuses_persisted_cooldown_profile(tmp_path, monkeypatch):
    """A new run reconstructs learned shared cooldown state before dispatch."""
    clock = _Clock()
    real_scheduler = ClaimEvidenceScheduler

    class FakeTimeScheduler(real_scheduler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, clock_ms=clock.clock, sleeper=clock.sleep, **kwargs)

    monkeypatch.setattr(runtime_module, "ClaimEvidenceScheduler", FakeTimeScheduler)
    runtime_path = tmp_path / "llm_runtime.sqlite"

    class Transport:
        def __init__(self):
            self.calls, self.assignments = 0, []

        def dispatch(self, request, assignment):
            self.calls += 1
            self.assignments.append(assignment)
            if self.calls == 1:
                return TechnicalFailure(request.stage, request.logical_request_id,
                    request.payload_fingerprint, "rate_limited", retry_after_seconds=5,
                    http_status=429)
            return _answer(request)

    first_repo, first_transport = _repo(tmp_path / "run-one", "run-one"), Transport()
    first_shared = RuntimeRepository(first_repo._conn, runtime_conn=sqlite3.connect(str(runtime_path), check_same_thread=False))
    first = ClaimEvidenceRuntime.for_run(first_repo, ENV, run_id="run-one", transport=first_transport,
        runtime_repository=first_shared, provider_metadata=PROVIDERS)
    try:
        assert first.execute(_task("claim-one"), source_text="Source text")["status"] == "accepted"
        assert first.execute(_task("claim-two"), source_text="Source text")["status"] == "accepted"
        controls = first_shared.scheduler_controls()
        persisted = controls["cooldowns"][("alpha", "alpha:1", "model-one")]
        assert persisted["learned_seconds"] == 5
        assert persisted["post_cooldown_successes"] == 2
        assert first_shared._repo.debug_snapshot()["profiles"][0]["baseline_seconds"] == 5
    finally:
        first.close()
        first_repo.close()

    second_repo, second_transport = _repo(tmp_path / "run-two", "run-two"), Transport()
    second_shared = RuntimeRepository(second_repo._conn, runtime_conn=sqlite3.connect(str(runtime_path), check_same_thread=False))
    second = ClaimEvidenceRuntime.for_run(second_repo, ENV, run_id="run-two", transport=second_transport,
        runtime_repository=second_shared, provider_metadata=PROVIDERS)
    try:
        assert second.execute(_task("claim-three"), source_text="Source text")["status"] == "accepted"
        assert second_transport.assignments[0].prior_cooldown.learned_seconds == 5
        assert second_transport.assignments[0].post_cooldown is False
    finally:
        second.close()
        second_repo.close()


def test_phase8_secret_canary_is_absent_from_authoritative_artifacts(tmp_path):
    """Raw credential material never crosses the runtime's persisted boundary."""
    canary = "sk-phase8-raw-secret-canary"
    digest = hashlib.sha256(canary.encode()).hexdigest()
    repo = _repo(tmp_path / "run", "run-secret")
    runtime_path = tmp_path / "llm_runtime.sqlite"
    shared = RuntimeRepository(repo._conn, runtime_conn=sqlite3.connect(str(runtime_path), check_same_thread=False))
    registry = ProviderRegistry()
    registry.register(ProviderAdapter("alpha", lambda *_: (_ for _ in ()).throw(BackendError(canary))))
    transport = ClaimEvidenceTransport(registry, CredentialResolver({
        "alpha:1": Credential("alpha:1", digest, canary)}))
    captured = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())
    logging.getLogger().addHandler(handler)
    runtime = ClaimEvidenceRuntime.for_run(repo, ENV, run_id="run-secret", transport=transport,
        runtime_repository=shared, provider_metadata=PROVIDERS)
    try:
        terminal = runtime.execute(_task("claim-secret"), source_text="Source text")
        assert terminal["status"] == "exhausted"
        serialized = [str(terminal), json.dumps(asdict(runtime._scheduler.snapshot()), default=str, sort_keys=True),
                      json.dumps(shared._repo.debug_snapshot(), sort_keys=True),
                      *captured]
        report, summary = render(str(tmp_path / "run"))
        serialized.extend((report, json.dumps(summary, sort_keys=True)))
        for path in (tmp_path / "run" / "run.sqlite", runtime_path,
                     tmp_path / "run" / "run.sqlite-wal", runtime_path.with_name(runtime_path.name + "-wal")):
            if path.exists():
                serialized.append(path.read_bytes().decode("latin1"))
        assert all(canary not in artifact for artifact in serialized)
        assert '"credential_fingerprint"' in serialized[2]
    finally:
        runtime.close()
        logging.getLogger().removeHandler(handler)
        repo.close()
