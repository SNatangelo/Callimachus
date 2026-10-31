# tests/verify/claim_evidence/application/test_scheduler.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import threading
import time

import pytest

from core.verify.claim_evidence.application.scheduler import (
    ClaimEvidenceScheduler,
    SchedulerError,
)
from core.verify.claim_evidence.application.ports import FatalDispatchError
from core.verify.claim_evidence.domain.cooldown import CooldownSettings
from core.verify.claim_evidence.domain.pacing import PacingPolicy
from core.verify.claim_evidence.domain.jury1_decisions import SupportGateDecision
from core.verify.claim_evidence.domain.replies import SupportGateAnswer
from core.verify.claim_evidence.domain.types import (
    LogicalRequest,
    ProviderLane,
    ProviderPolicy,
    TechnicalFailure,
)
from core.verify.claim_evidence.domain.fingerprint import provider_prompt_fingerprint


def _provider(
    keys=("p:1",),
    models=("m1",),
    *,
    pairing="positional_exclusive",
):
    fingerprints = tuple(f"fingerprint-{index}" for index, _ in enumerate(keys))
    pairs = (
        tuple(zip(keys, fingerprints, models))
        if pairing == "positional_exclusive"
        else tuple(
            (key, fingerprint, model)
            for key, fingerprint in zip(keys, fingerprints)
            for model in models
        )
    )
    lanes = tuple(ProviderLane(key, fingerprint, model, True, True)
                  for key, fingerprint, model in pairs)
    return (
        ProviderPolicy("p", keys, fingerprints, models, pairing, lanes),
    )


def _request(name, stage="support_gate", *, deadline_ms=None):
    payload = f'{{"request":"{name}","stage":"{stage}"}}'.encode()
    system_prompt = "test system prompt"
    return LogicalRequest(
        stage,
        name,
        payload,
        provider_prompt_fingerprint(system_prompt, payload),
        1,
        system_prompt,
        "candidate" if stage == "jury2" else None,
        deadline_ms,
    )


def _failure(request, cause="transport", *, retry_after=None):
    return TechnicalFailure(
        request.stage,
        request.logical_request_id,
        request.payload_fingerprint,
        cause,
        retry_after_seconds=retry_after,
    )


def _answer(request):
    return SupportGateAnswer(
        request.logical_request_id,
        request.payload_fingerprint,
        SupportGateDecision(False),
    )


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        milliseconds = round(seconds * 1000)
        self.sleeps.append(milliseconds)
        self.now += milliseconds


class InstrumentedLimiter:
    def __init__(self, cap):
        self._semaphore = threading.BoundedSemaphore(cap)
        self._lock = threading.Lock()
        self.active = 0
        self.maximum = 0

    def acquire(self, timeout=None):
        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
        return acquired

    def release(self):
        with self._lock:
            self.active -= 1
        self._semaphore.release()


class BlockingTransport:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.started = threading.Event()
        self.gate = threading.Event()
        self._lock = threading.Lock()

    def dispatch(self, request, assignment):
        with self._lock:
            self.calls.append((request, assignment))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.started.set()
        self.gate.wait(2)
        with self._lock:
            self.active -= 1
        return _failure(request, "timeout")


def test_dis_01_same_logical_request_has_one_live_physical_dispatch():
    transport = BlockingTransport()
    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", transport, aggregate_cap=2, attempt_timeout_ms=1_000
    )
    request = _request("same")
    replies = []
    workers = [
        threading.Thread(target=lambda: replies.append(scheduler.dispatch(request)))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    assert transport.started.wait(1)
    time.sleep(0.02)
    assert len(transport.calls) == 1
    transport.gate.set()
    for worker in workers:
        worker.join(1)
    assert len(replies) == 2
    assert len(transport.calls) == 1


def test_persistence_failure_is_not_reclassified_as_technical_retry():
    class Transport:
        def dispatch(self, request, assignment):
            raise FatalDispatchError("audit persistence failed")

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1
    )
    with pytest.raises(FatalDispatchError, match="audit persistence failed"):
        scheduler.dispatch(_request("fatal"))


def test_con_01_aggregate_cap_includes_jury1_and_jury2():
    transport = BlockingTransport()
    limiter = InstrumentedLimiter(2)
    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", transport, aggregate_cap=2,
        limiter=limiter, attempt_timeout_ms=1_000,
    )
    requests = [
        _request("j1-a"),
        _request("j2-a", "jury2"),
        _request("j1-b"),
        _request("j2-b", "jury2"),
    ]
    workers = [
        threading.Thread(target=scheduler.dispatch, args=(request,))
        for request in requests
    ]
    for worker in workers:
        worker.start()
    deadline = time.time() + 1
    while len(transport.calls) < 2 and time.time() < deadline:
        time.sleep(0.005)
    assert transport.maximum == limiter.maximum == 2
    transport.gate.set()
    for worker in workers:
        worker.join(1)
    assert len(transport.calls) == 4


def test_pace_01_zero_default_adds_no_delay():
    clock = FakeClock()
    starts = []

    class Transport:
        def dispatch(self, request, assignment):
            starts.append(clock.now)
            return _failure(request)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=3,
        clock_ms=clock.clock, sleeper=clock.sleep,
    )
    for index in range(3):
        scheduler.dispatch(_request(f"request-{index}"))
    assert starts == [0, 0, 0]
    assert clock.sleeps == []


def test_pace_02_global_and_model_reservations_are_atomic_and_persistable():
    clock = FakeClock()
    starts = []

    class Transport:
        def dispatch(self, request, assignment):
            starts.append((assignment.model, clock.now))
            return _failure(request)

    scheduler = ClaimEvidenceScheduler(
        _provider(("p:1", "p:2"), ("m1", "m2")),
        "seed",
        Transport(),
        aggregate_cap=3,
        pacing=PacingPolicy(100, (("p:m1", 250),)),
        clock_ms=clock.clock,
        sleeper=clock.sleep,
    )
    for name in ("first", "second", "third"):
        scheduler.dispatch(_request(name))
    assert starts == [("m1", 0), ("m2", 100), ("m1", 250)]
    assert clock.sleeps == [100, 150]
    resumed = ClaimEvidenceScheduler(
        _provider(("p:1", "p:2"), ("m1", "m2")),
        "seed",
        Transport(),
        aggregate_cap=3,
        pacing=PacingPolicy(100, (("p:m1", 250),)),
        state=scheduler.snapshot(),
        clock_ms=clock.clock,
        sleeper=clock.sleep,
    )
    resumed.dispatch(_request("fourth"))
    assert starts[-1] == ("m2", 350)


def test_concurrent_pacing_reservations_cannot_share_one_start():
    class AtomicClock(FakeClock):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()

        def clock(self):
            with self._lock:
                return self.now

        def sleep(self, seconds):
            with self._lock:
                milliseconds = round(seconds * 1000)
                self.sleeps.append(milliseconds)
                self.now += milliseconds

    clock = AtomicClock()
    starts = []
    gate = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()

    class Transport:
        def dispatch(self, request, assignment):
            with lock:
                starts.append(assignment.started_at_ms)
                if len(starts) == 2:
                    both_started.set()
            gate.wait(1)
            return _failure(request)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=2,
        pacing=PacingPolicy(100), clock_ms=clock.clock, sleeper=clock.sleep,
        attempt_timeout_ms=1_000,
    )
    workers = [
        threading.Thread(target=scheduler.dispatch, args=(_request(name),))
        for name in ("first", "second")
    ]
    for worker in workers:
        worker.start()
    assert both_started.wait(1)
    gate.set()
    for worker in workers:
        worker.join(1)
    assert sorted(starts) == [0, 100]


def test_wait_01_cooldown_wait_holds_no_slot_and_creates_no_attempt():
    clock = FakeClock()
    limiter = InstrumentedLimiter(1)
    calls = []

    class Transport:
        def dispatch(self, request, assignment):
            calls.append(assignment)
            if len(calls) == 1:
                return _failure(request, "rate_limited", retry_after=9)
            return _failure(request)

    def sleep(seconds):
        assert limiter.active == 0
        assert len(calls) == 1
        clock.sleep(seconds)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1, limiter=limiter,
        clock_ms=clock.clock, sleeper=sleep,
    )
    scheduler.dispatch(_request("rate"))
    scheduler.dispatch(_request("after"))
    assert clock.sleeps == [9_000]
    assert len(calls) == 2


def test_rl_02_doubles_and_learns_only_same_profile_post_cooldown():
    clock = FakeClock()
    outcomes = ["rate_limited", "rate_limited", "answer", "answer", "rate_limited"]

    class Transport:
        def dispatch(self, request, assignment):
            outcome = outcomes.pop(0)
            return _answer(request) if outcome == "answer" else _failure(request, outcome)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1,
        cooldown=CooldownSettings(5, 2, 300, 2),
        clock_ms=clock.clock, sleeper=clock.sleep,
    )
    for index in range(5):
        scheduler.dispatch(_request(f"request-{index}"))
    state = scheduler.snapshot()
    cooldown = dict(state.cooldowns)[("p", "p:1", "m1")]
    assert clock.sleeps == [5_000, 10_000]
    assert cooldown.learned_seconds == 10
    assert cooldown.post_cooldown_successes == 0
    assert dict(state.key_eligibility_ms)[("p", "p:1")] == 25_000


def test_retry_after_600_is_not_truncated():
    clock = FakeClock()
    calls = 0

    class Transport:
        def dispatch(self, request, assignment):
            nonlocal calls
            calls += 1
            return _failure(
                request,
                "rate_limited" if calls == 1 else "transport",
                retry_after=600 if calls == 1 else None,
            )

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1,
        clock_ms=clock.clock, sleeper=clock.sleep,
    )
    scheduler.dispatch(_request("rate"))
    scheduler.dispatch(_request("after"))
    assert clock.sleeps == [600_000]


def test_auth_disables_key_while_lane_failure_disables_only_model():
    auth_calls = []

    class AuthTransport:
        def dispatch(self, request, assignment):
            auth_calls.append(assignment)
            return _failure(
                request,
                "credential_invalid" if len(auth_calls) == 1 else "transport",
            )

    auth = ClaimEvidenceScheduler(
        _provider(("p:1", "p:2"), ("m1", "m2"), pairing="seeded_cross_product"),
        "seed", AuthTransport(), aggregate_cap=1,
    )
    auth.dispatch(_request("auth"))
    auth.dispatch(_request("healthy"))
    auth.dispatch(_request("healthy-again"))
    assert auth_calls[0].credential_alias == "p:1"
    assert all(call.credential_alias == "p:2" for call in auth_calls[1:])

    lane_calls = []

    class LaneTransport:
        def dispatch(self, request, assignment):
            lane_calls.append(assignment)
            return _failure(
                request, "lane_unavailable" if len(lane_calls) == 1 else "transport"
            )

    lane = ClaimEvidenceScheduler(
        _provider(("p:1",), ("m1", "m2"), pairing="seeded_cross_product"),
        "seed", LaneTransport(), aggregate_cap=1,
    )
    lane.dispatch(_request("lane"))
    lane.dispatch(_request("other-lane"))
    assert lane_calls[0].credential_alias == lane_calls[1].credential_alias
    assert lane_calls[0].model != lane_calls[1].model


def test_ret_01_timeout_discards_late_answer_and_retains_physical_slot():
    limiter = InstrumentedLimiter(1)
    gate = threading.Event()
    calls = []

    class Transport:
        def dispatch(self, request, assignment):
            calls.append((request, assignment))
            if len(calls) == 1:
                gate.wait(1)
                return _answer(request)
            return _failure(request)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1, limiter=limiter,
        attempt_timeout_ms=20, wait_quantum_ms=5,
    )
    request = _request("retry")
    first = scheduler.dispatch(request)
    assert first.cause == "timeout"
    assert len(calls) == limiter.active == 1
    retried = []
    retry_worker = threading.Thread(
        target=lambda: retried.append(scheduler.dispatch(request))
    )
    retry_worker.start()
    time.sleep(0.02)
    assert len(calls) == 1
    gate.set()
    retry_worker.join(1)
    assert retried[0].cause == "transport"
    assert len(calls) == 2
    assert calls[0][1].dispatch_attempt_id != calls[1][1].dispatch_attempt_id
    assert limiter.maximum == 1


def test_deadline_and_cancellation_stop_before_provider_attempt():
    clock = FakeClock()
    calls = []

    class Transport:
        def dispatch(self, request, assignment):
            calls.append(assignment)
            return _failure(request)

    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1,
        pacing=PacingPolicy(100), clock_ms=clock.clock, sleeper=clock.sleep,
    )
    scheduler.dispatch(_request("first"))
    reply = scheduler.dispatch(_request("deadline", deadline_ms=50))
    assert reply.cause == "timeout"
    assert len(calls) == 1

    cancelled = ClaimEvidenceScheduler(
        _provider(), "seed", Transport(), aggregate_cap=1,
        cancelled=lambda: True,
    )
    reply = cancelled.dispatch(_request("cancelled"))
    assert reply.cause == "timeout"
    assert len(calls) == 1


def test_conflicting_live_logical_provenance_fails_closed():
    transport = BlockingTransport()
    scheduler = ClaimEvidenceScheduler(
        _provider(), "seed", transport, aggregate_cap=1, attempt_timeout_ms=1_000
    )
    original = _request("same")
    worker = threading.Thread(target=scheduler.dispatch, args=(original,))
    worker.start()
    assert transport.started.wait(1)
    conflict = _request("different")
    conflict = LogicalRequest(
        conflict.stage,
        original.logical_request_id,
        conflict.payload,
        conflict.payload_fingerprint,
        1,
        conflict.system_prompt,
    )
    with pytest.raises(SchedulerError, match="conflicting provenance"):
        scheduler.dispatch(conflict)
    transport.gate.set()
    worker.join(1)
