# core/verify/claim_evidence/domain/cooldown.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure immutable key-wide HTTP-429 cooldown transitions."""
from dataclasses import dataclass


COOLDOWN_POLICY_VERSION = "cooldown-policy-v1"


class CooldownError(ValueError):
    """Cooldown policy, state, or observation is invalid."""


@dataclass(frozen=True, slots=True)
class CooldownSettings:
    global_seconds: int = 5
    multiplier: int = 2
    local_max_seconds: int = 300
    stable_successes: int = 2
    version: str = COOLDOWN_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class CooldownState:
    next_eligible_at: int = 0
    last_applied_seconds: int | None = None
    learned_seconds: int | None = None
    post_cooldown_successes: int = 0
    policy_version: str = COOLDOWN_POLICY_VERSION


def effective_baseline(
    settings: CooldownSettings,
    state: CooldownState,
    *,
    configured_override_seconds: int | None = None,
) -> int:
    _validate(settings, state, configured_override_seconds)
    return (
        state.learned_seconds
        or configured_override_seconds
        or settings.global_seconds
    )


def transition(
    settings: CooldownSettings,
    state: CooldownState,
    *,
    now: int,
    status: int | None,
    retry_after_seconds: int | None = None,
    configured_override_seconds: int | None = None,
    post_cooldown: bool = False,
) -> CooldownState:
    _validate(settings, state, configured_override_seconds)
    _nonnegative(now, "cooldown time")
    if (
        status is not None
        and (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        )
    ):
        raise CooldownError("HTTP status is invalid")
    if type(post_cooldown) is not bool:
        raise CooldownError("post-cooldown marker is invalid")
    if status != 429 and retry_after_seconds is not None:
        raise CooldownError("Retry-After requires HTTP 429")
    if status != 429:
        return state
    if retry_after_seconds is not None:
        _nonnegative(retry_after_seconds, "Retry-After")
    baseline = effective_baseline(
        settings,
        state,
        configured_override_seconds=configured_override_seconds,
    )
    if post_cooldown and state.last_applied_seconds is not None:
        local_ceiling = max(settings.local_max_seconds, state.last_applied_seconds)
        local = min(state.last_applied_seconds * settings.multiplier, local_ceiling)
    else:
        local = baseline
    applied = max(local, retry_after_seconds or 0)
    return CooldownState(
        now + applied,
        applied,
        state.learned_seconds,
        0,
        settings.version,
    )


def success(
    settings: CooldownSettings,
    state: CooldownState,
    *,
    post_cooldown: bool,
) -> CooldownState:
    _validate(settings, state, None)
    if type(post_cooldown) is not bool:
        raise CooldownError("post-cooldown marker is invalid")
    if not post_cooldown:
        return state
    if state.last_applied_seconds is None:
        raise CooldownError("post-cooldown success has no cooldown")
    successes = state.post_cooldown_successes + 1
    learned = (
        state.last_applied_seconds
        if successes >= settings.stable_successes
        else state.learned_seconds
    )
    return CooldownState(
        state.next_eligible_at,
        state.last_applied_seconds,
        learned,
        successes,
        settings.version,
    )


def _validate(
    settings: CooldownSettings,
    state: CooldownState,
    override: int | None,
) -> None:
    if not isinstance(settings, CooldownSettings) or not isinstance(state, CooldownState):
        raise CooldownError("cooldown settings and state are required")
    values = (
        settings.global_seconds,
        settings.multiplier,
        settings.local_max_seconds,
        settings.stable_successes,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise CooldownError("cooldown settings are invalid")
    if not isinstance(settings.version, str) or not settings.version:
        raise CooldownError("cooldown policy version is invalid")
    if state.policy_version != settings.version:
        raise CooldownError("cooldown policy version changed")
    state_values = (
        state.next_eligible_at,
        state.post_cooldown_successes,
    )
    optional = (state.last_applied_seconds, state.learned_seconds, override)
    if (
        any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in state_values)
        or any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0) for value in optional)
    ):
        raise CooldownError("cooldown state or override is invalid")


def _nonnegative(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CooldownError(f"{field} is invalid")
