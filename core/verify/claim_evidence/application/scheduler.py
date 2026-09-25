# core/verify/claim_evidence/application/scheduler.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrent provider-neutral scheduling with one aggregate physical cap."""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..domain.cooldown import (
    CooldownSettings, CooldownState, success, transition,
)
from ..domain.pacing import PacingPolicy, PacingState, reserve_start
from ..domain.selection import (
    LaneControls, LaneSelection, SelectionState, select_lane,
)
from ..domain.replies import normalise_jury_reply
from ..domain.types import (
    DispatchAssignment, LogicalRequest, ProviderPolicy, SchedulerState,
    TechnicalFailure, logical_request_provenance,
    technical_failure, validate_logical_request,
)
from .ports import (
    AggregateLimiterPort,
    FatalDispatchError,
    JuryReply,
    ProviderTransportPort,
)


class SchedulerError(RuntimeError):
    """Scheduler policy, state, or request provenance is invalid."""


@dataclass(slots=True)
class _LiveAttempt:
    provenance: tuple[str, str, int, str | None, int | None]
    logical_done: threading.Event = field(default_factory=threading.Event)
    physical_done: threading.Event = field(default_factory=threading.Event)
    result: JuryReply | None = None
    assignment: DispatchAssignment | None = None
    owner_active: bool = True
    error: FatalDispatchError | None = None


class ClaimEvidenceScheduler:
    def __init__(
        self, providers: tuple[ProviderPolicy, ...], seed: str,
        transport: ProviderTransportPort, *, aggregate_cap: int,
        limiter: AggregateLimiterPort | None = None,
        pacing: PacingPolicy = PacingPolicy(),
        cooldown: CooldownSettings = CooldownSettings(),
        cooldown_overrides: tuple[tuple[str, int], ...] = (),
        state: SchedulerState | None = None, attempt_timeout_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        cancelled: Callable[[], bool] | None = None, wait_quantum_ms: int = 50,
    ) -> None:
        _validate_init(
            seed, aggregate_cap, state, attempt_timeout_ms, wait_quantum_ms,
            pacing, cooldown, cooldown_overrides,
        )
        state = state or SchedulerState(SelectionState(), PacingState())
        self._providers, self._seed, self._transport = providers, seed, transport
        self._limiter = limiter or threading.BoundedSemaphore(aggregate_cap)
        self._pacing_policy, self._cooldown_policy = pacing, cooldown
        self._overrides, self._selection = dict(cooldown_overrides), state.selection
        self._pacing, self._cooldowns = state.pacing, dict(state.cooldowns)
        self._key_until = dict(state.key_eligibility_ms)
        self._disabled_credentials = set(state.disabled_credentials)
        self._disabled_lanes = set(state.disabled_lanes)
        self._counter, self._timeout = state.dispatch_counter, attempt_timeout_ms
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._sleep, self._cancelled, self._quantum = sleeper, cancelled, wait_quantum_ms
        self._condition = threading.Condition(threading.RLock())
        self._live: dict[str, _LiveAttempt] = {}

    def dispatch(self, request: LogicalRequest) -> JuryReply:
        try:
            validate_logical_request(request)
        except ValueError as exc: raise SchedulerError(str(exc)) from exc
        while True:
            entry, owner, joined = self._claim(request)
            if not owner:
                target = entry.logical_done if joined else entry.physical_done
                if not self._await_event(target, request.deadline_ms):
                    return technical_failure(request, "timeout")
                if joined:
                    if entry.error is not None: raise entry.error
                    if entry.result is None:
                        raise SchedulerError("dispatch completed without result")
                    return entry.result
                continue
            result: JuryReply = technical_failure(request, "transport")
            try:
                result = self._dispatch_owner(request, entry)
            finally:
                self._finish_owner(request, entry, result)
            return result

    def snapshot(self) -> SchedulerState:
        with self._condition:
            return SchedulerState(
                self._selection, self._pacing, tuple(sorted(self._cooldowns.items())),
                tuple(sorted(self._key_until.items())),
                frozenset(self._disabled_credentials),
                frozenset(self._disabled_lanes), self._counter,
            )

    def _claim(self, request: LogicalRequest) -> tuple[_LiveAttempt, bool, bool]:
        with self._condition:
            entry = self._live.get(request.logical_request_id)
            if entry is not None:
                if logical_request_provenance(request) != entry.provenance:
                    raise SchedulerError("logical request has conflicting provenance")
                return entry, False, not entry.logical_done.is_set()
            entry = _LiveAttempt(logical_request_provenance(request))
            self._live[request.logical_request_id] = entry
            return entry, True, False

    def _dispatch_owner(self, request: LogicalRequest, entry: _LiveAttempt) -> JuryReply:
        while True:
            selection, wait_ms, _ = self._prepare(request, False)
            if selection is None and not wait_ms:
                return technical_failure(request, "lane_unavailable")
            if wait_ms:
                if not self._wait_before_slot(wait_ms, request.deadline_ms):
                    return technical_failure(request, "timeout")
                continue
            if not self._acquire_slot(request.deadline_ms):
                return technical_failure(request, "timeout")
            transferred = False
            try:
                selection, wait_ms, prior_pacing = self._prepare(request, True)
                if selection is None or wait_ms:
                    continue
                assignment = self._assignment(selection, prior_pacing)
                entry.assignment = assignment
                worker = threading.Thread(target=self._physical_call, args=(request, entry, assignment), name=f"claim-evidence-{request.stage}", daemon=True)
                worker.start()
                transferred = True
                return self._await_physical(request, entry, assignment)
            finally:
                if not transferred:
                    self._limiter.release()

    def _prepare(
        self, request: LogicalRequest, commit: bool,
    ) -> tuple[LaneSelection | None, int, PacingState | None]:
        with self._condition:
            now = self._clock()
            cooled = frozenset(k for k, until in self._key_until.items() if until > now)
            controls = LaneControls(frozenset(self._disabled_credentials), cooled, frozenset(self._disabled_lanes))
            selection = select_lane(self._providers, self._selection,
                stage=request.stage, seed=self._seed, logical_request_id=request.logical_request_id, controls=controls)
            if selection is None:
                waits = [until - now for until in self._key_until.values() if until > now]
                return None, min(waits) if waits else 0, None
            reservation = reserve_start(self._pacing, self._pacing_policy,
                provider=selection.provider, model=selection.lane.model,
                now_ms=now, key_cooldown_until_ms=self._key_until.get(
                    (selection.provider, selection.lane.key_alias), 0))
            if reservation.wait_ms:
                return selection, reservation.wait_ms, None
            prior = self._pacing
            if commit:
                self._selection, self._pacing = selection.state, reservation.state
            return selection, 0, prior

    def _assignment(
        self,
        selection: LaneSelection,
        prior: PacingState | None,
    ) -> DispatchAssignment:
        with self._condition:
            self._counter += 1
            profile = (selection.provider, selection.lane.key_alias, selection.lane.model)
            state = self._cooldowns.get(profile)
            post = bool(state and state.last_applied_seconds is not None
                and state.post_cooldown_successes < self._cooldown_policy.stable_successes and self._clock() >= state.next_eligible_at * 1000)
            selector = f"{selection.provider}:{selection.lane.model}"
            prior = prior or PacingState()
            return DispatchAssignment(
                selection.provider, selection.lane.key_alias, selection.lane.key_fingerprint,
                selection.lane.model, selection.lane_id,
                f"{selection.provider}:{self._counter}", post, self._clock(),
                selection.state.credential_cursor, selection.state.model_draw_index, prior.last_global_start_ms,
                dict(prior.last_model_starts_ms).get(selector),
                max((self._pacing.last_global_start_ms or 0) + self._pacing_policy.global_interval_ms, dict(self._pacing.last_model_starts_ms).get(selector, 0) + dict(self._pacing_policy.per_model_interval_ms).get(selector, 0)),
                self._pacing_policy.global_interval_ms,
                dict(self._pacing_policy.per_model_interval_ms).get(selector, 0),
                state,
                self._overrides.get(selector),
            )

    def _physical_call(
        self, request: LogicalRequest, entry: _LiveAttempt,
        assignment: DispatchAssignment,
    ) -> None:
        try:
            raw = self._transport.dispatch(request, assignment)
        except FatalDispatchError as exc:
            raw = exc
        except Exception:
            raw = technical_failure(request, "transport", dispatch_id=assignment.dispatch_attempt_id)
        result = None if isinstance(raw, FatalDispatchError) else normalise_jury_reply(request, assignment, raw)
        with self._condition:
            if not entry.logical_done.is_set():
                if isinstance(raw, FatalDispatchError):
                    entry.error = raw
                else:
                    try:
                        self._apply_result(assignment, result)
                    except Exception:
                        entry.error = FatalDispatchError("dispatch state projection failed")
                    entry.result = result
                entry.logical_done.set()
            entry.physical_done.set()
            self._limiter.release()
            if not entry.owner_active:
                self._live.pop(request.logical_request_id, None)
            self._condition.notify_all()

    def _await_physical(
        self, request: LogicalRequest, entry: _LiveAttempt,
        assignment: DispatchAssignment,
    ) -> JuryReply:
        budget = self._timeout
        if request.deadline_ms is not None:
            budget = min(budget, max(0, request.deadline_ms - self._clock()))
        if self._await_event(entry.logical_done, self._clock() + budget):
            if entry.error is not None: raise entry.error
            if entry.result is None:
                raise SchedulerError("physical call completed without result")
            return entry.result
        result = technical_failure(request, "timeout", dispatch_id=assignment.dispatch_attempt_id)
        with self._condition:
            if not entry.logical_done.is_set():
                entry.result = result
                entry.logical_done.set()
                self._condition.notify_all()
            return entry.result or result

    def _apply_result(self, assignment: DispatchAssignment, result: JuryReply) -> None:
        key = (assignment.provider, assignment.credential_alias)
        profile = (*key, assignment.model)
        if isinstance(result, TechnicalFailure):
            if result.cause == "credential_invalid":
                self._disabled_credentials.add(key)
            elif result.cause == "lane_unavailable":
                self._disabled_lanes.add(profile)
            elif result.cause == "rate_limited":
                current = self._cooldowns.get(profile, CooldownState())
                updated = transition(
                    self._cooldown_policy, current,
                    now=((result.observed_at_ms or self._clock()) + 999) // 1000,
                    status=429, retry_after_seconds=result.retry_after_seconds,
                    configured_override_seconds=self._overrides.get(
                        f"{assignment.provider}:{assignment.model}"),
                    post_cooldown=assignment.post_cooldown,
                )
                self._cooldowns[profile] = updated
                self._key_until[key] = updated.next_eligible_at * 1000
            return
        state = self._cooldowns.get(profile)
        if state is not None and assignment.post_cooldown:
            self._cooldowns[profile] = success(
                self._cooldown_policy, state, post_cooldown=True)

    def _finish_owner(
        self, request: LogicalRequest, entry: _LiveAttempt, result: JuryReply,
    ) -> None:
        with self._condition:
            if not entry.logical_done.is_set():
                entry.result = result
                entry.logical_done.set()
                entry.physical_done.set()
            entry.owner_active = False
            if entry.physical_done.is_set():
                self._live.pop(request.logical_request_id, None)
            self._condition.notify_all()

    def _wait_before_slot(self, wait_ms: int, deadline_ms: int | None) -> bool:
        if self._cancelled and self._cancelled():
            return False
        before = self._clock()
        if deadline_ms is not None and before + wait_ms > deadline_ms:
            return False
        self._sleep(wait_ms / 1000)
        if self._clock() < before + wait_ms:
            raise SchedulerError("sleeper returned before eligibility")
        return not self._cancelled or not self._cancelled()

    def _acquire_slot(self, deadline_ms: int | None) -> bool:
        if self._cancelled is None and deadline_ms is None:
            return self._limiter.acquire()
        remaining = None if deadline_ms is None else deadline_ms - self._clock()
        while remaining is None or remaining > 0:
            if self._cancelled and self._cancelled():
                return False
            step = self._quantum if remaining is None else min(self._quantum, remaining)
            if self._limiter.acquire(step / 1000):
                return True
            if remaining is not None:
                remaining -= step
        return False

    def _await_event(self, event: threading.Event, deadline_ms: int | None) -> bool:
        remaining = None if deadline_ms is None else max(0, deadline_ms - self._clock())
        while remaining is None or remaining > 0:
            if self._cancelled and self._cancelled():
                return False
            step = self._quantum if remaining is None else min(self._quantum, remaining)
            if event.wait(step / 1000):
                return True
            if remaining is not None:
                remaining -= step
        return event.is_set()


def _validate_init(
    seed: str, cap: int, state: SchedulerState | None,
    timeout: int, quantum: int, pacing: PacingPolicy,
    cooldown: CooldownSettings, overrides: tuple[tuple[str, int], ...],
) -> None:
    bad_int = lambda value: isinstance(value, bool) or not isinstance(value, int) or value < 1
    if not isinstance(seed, str) or not seed or bad_int(cap) or state is not None and not isinstance(state, SchedulerState) or any(bad_int(value) for value in (timeout, quantum)) or len(dict(overrides)) != len(overrides) or any(not key or bad_int(value) for key, value in overrides):
        raise SchedulerError("scheduler policy or state is invalid")
    reserve_start(PacingState(), pacing, provider="validation", model="validation", now_ms=0)
    transition(cooldown, CooldownState(), now=0, status=None)
