# tests/verify/claim_evidence/domain/test_cooldown.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import FrozenInstanceError

import pytest

from core.verify.claim_evidence.domain.cooldown import (
    COOLDOWN_POLICY_VERSION,
    CooldownError,
    CooldownSettings,
    CooldownState,
    effective_baseline,
    success,
    transition,
)


def test_only_429_changes_cooldown_and_ordinary_success_is_a_noop():
    state = CooldownState()
    assert transition(CooldownSettings(), state, now=0, status=500) is state
    assert success(CooldownSettings(), state, post_cooldown=False) is state


def test_baseline_precedence_is_learned_then_configured_then_global():
    settings = CooldownSettings(global_seconds=5)
    assert effective_baseline(settings, CooldownState()) == 5
    assert effective_baseline(
        settings, CooldownState(), configured_override_seconds=7
    ) == 7
    learned = CooldownState(learned_seconds=11)
    assert effective_baseline(
        settings, learned, configured_override_seconds=7
    ) == 11


def test_rl_02_repeated_post_cooldown_429_doubles_to_local_cap_without_jitter():
    settings = CooldownSettings()
    state = CooldownState()
    durations = []
    now = 0
    for index in range(8):
        state = transition(
            settings,
            state,
            now=now,
            status=429,
            post_cooldown=index > 0,
        )
        durations.append(state.last_applied_seconds)
        now = state.next_eligible_at
    assert durations == [5, 10, 20, 40, 80, 160, 300, 300]
    assert state.policy_version == COOLDOWN_POLICY_VERSION


def test_retry_after_is_a_lower_bound_and_is_never_truncated():
    state = transition(
        CooldownSettings(),
        CooldownState(),
        now=10,
        status=429,
        retry_after_seconds=600,
    )
    assert state.last_applied_seconds == 600
    assert state.next_eligible_at == 610
    repeated = transition(
        CooldownSettings(),
        state,
        now=610,
        status=429,
        post_cooldown=True,
    )
    assert repeated.last_applied_seconds == 600


def test_two_post_cooldown_successes_stabilize_current_applied_delay():
    settings = CooldownSettings(stable_successes=2)
    state = transition(settings, CooldownState(), now=0, status=429)
    first = success(settings, state, post_cooldown=True)
    second = success(settings, first, post_cooldown=True)
    assert first.learned_seconds is None
    assert second.learned_seconds == 5
    assert effective_baseline(
        settings, second, configured_override_seconds=3
    ) == 5


def test_rl_01_state_is_key_scoped_by_the_caller():
    settings = CooldownSettings()
    key1 = transition(settings, CooldownState(), now=0, status=429)
    key2 = CooldownState()
    assert key1.next_eligible_at == 5
    assert key2.next_eligible_at == 0


def test_cooldown_state_is_immutable_and_version_mismatch_fails_closed():
    with pytest.raises(FrozenInstanceError):
        CooldownState().next_eligible_at = 1  # type: ignore[misc]
    with pytest.raises(CooldownError, match="version"):
        transition(
            CooldownSettings(),
            CooldownState(policy_version="older"),
            now=0,
            status=429,
        )
