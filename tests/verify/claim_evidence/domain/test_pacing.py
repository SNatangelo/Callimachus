# tests/verify/claim_evidence/domain/test_pacing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import FrozenInstanceError

import pytest

from core.verify.claim_evidence.domain.pacing import (
    PacingError,
    PacingPolicy,
    PacingState,
    next_eligible_at,
    reserve_start,
)


def test_pace_01_zero_defaults_add_no_artificial_wait():
    state = PacingState()
    starts = []
    for model in ("m1", "m2", "m3"):
        reservation = reserve_start(
            state, PacingPolicy(), provider="p", model=model, now_ms=0
        )
        starts.append(reservation.start_ms)
        state = reservation.state
    assert starts == [0, 0, 0]


def test_pace_02_global_and_provider_model_intervals_are_additive():
    policy = PacingPolicy(100, (("p:m1", 250),))
    state = PacingState()
    starts = []
    for model in ("m1", "m2", "m1"):
        reservation = reserve_start(
            state, policy, provider="p", model=model, now_ms=0
        )
        starts.append(reservation.start_ms)
        state = reservation.state
    assert starts == [0, 100, 250]


def test_key_cooldown_is_a_third_independent_eligibility_constraint():
    policy = PacingPolicy(100, (("p:m1", 250),))
    assert next_eligible_at(
        PacingState(100, (("p:m1", 100),)),
        policy,
        provider="p",
        model="m1",
        now_ms=150,
        key_cooldown_until_ms=500,
    ) == 500


def test_persisted_state_resumes_without_resetting_last_starts():
    policy = PacingPolicy(100, (("p:m1", 250),))
    first = reserve_start(
        PacingState(), policy, provider="p", model="m1", now_ms=0
    )
    resumed = reserve_start(
        first.state, policy, provider="p", model="m1", now_ms=10
    )
    assert resumed.start_ms == 250
    assert resumed.wait_ms == 240


def test_pacing_state_is_immutable_and_invalid_policy_fails_closed():
    with pytest.raises(FrozenInstanceError):
        PacingState().last_global_start_ms = 1  # type: ignore[misc]
    with pytest.raises(PacingError):
        reserve_start(
            PacingState(),
            PacingPolicy(-1),
            provider="p",
            model="m",
            now_ms=0,
        )
