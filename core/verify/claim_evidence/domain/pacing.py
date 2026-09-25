# core/verify/claim_evidence/domain/pacing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure immutable ordinary dispatch-start pacing reservations."""
from dataclasses import dataclass


class PacingError(ValueError):
    """Pacing policy, state, or reservation input is invalid."""


@dataclass(frozen=True, slots=True)
class PacingPolicy:
    global_interval_ms: int = 0
    per_model_interval_ms: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class PacingState:
    last_global_start_ms: int | None = None
    last_model_starts_ms: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class StartReservation:
    start_ms: int
    wait_ms: int
    state: PacingState


def next_eligible_at(
    state: PacingState,
    policy: PacingPolicy,
    *,
    provider: str,
    model: str,
    now_ms: int,
    key_cooldown_until_ms: int = 0,
) -> int:
    _validate(state, policy, provider, model, now_ms, key_cooldown_until_ms)
    selector = f"{provider}:{model}"
    intervals = dict(policy.per_model_interval_ms)
    starts = dict(state.last_model_starts_ms)
    global_at = (
        now_ms
        if state.last_global_start_ms is None
        else state.last_global_start_ms + policy.global_interval_ms
    )
    model_at = (
        now_ms
        if selector not in starts
        else starts[selector] + intervals.get(selector, 0)
    )
    return max(now_ms, global_at, model_at, key_cooldown_until_ms)


def reserve_start(
    state: PacingState,
    policy: PacingPolicy,
    *,
    provider: str,
    model: str,
    now_ms: int,
    key_cooldown_until_ms: int = 0,
) -> StartReservation:
    start = next_eligible_at(
        state,
        policy,
        provider=provider,
        model=model,
        now_ms=now_ms,
        key_cooldown_until_ms=key_cooldown_until_ms,
    )
    selector = f"{provider}:{model}"
    starts = dict(state.last_model_starts_ms)
    starts[selector] = start
    updated = PacingState(start, tuple(sorted(starts.items())))
    return StartReservation(start, start - now_ms, updated)


def _validate(
    state: PacingState,
    policy: PacingPolicy,
    provider: str,
    model: str,
    now_ms: int,
    cooldown_ms: int,
) -> None:
    if not isinstance(state, PacingState) or not isinstance(policy, PacingPolicy):
        raise PacingError("pacing state and policy are required")
    values = (policy.global_interval_ms, now_ms, cooldown_ms)
    if (
        any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values)
        or not isinstance(provider, str)
        or not provider
        or not isinstance(model, str)
        or not model
    ):
        raise PacingError("pacing inputs are invalid")
    entries = policy.per_model_interval_ms
    state_entries = state.last_model_starts_ms
    if (
        len({key for key, _ in entries}) != len(entries)
        or any(not key or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in entries)
        or len({key for key, _ in state_entries}) != len(state_entries)
        or any(not key or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in state_entries)
        or state.last_global_start_ms is not None
        and (
            isinstance(state.last_global_start_ms, bool)
            or not isinstance(state.last_global_start_ms, int)
            or state.last_global_start_ms < 0
        )
    ):
        raise PacingError("pacing policy or state is invalid")
