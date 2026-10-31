# tests/verify/claim_evidence/domain/test_selection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import FrozenInstanceError

import pytest

from core.verify.claim_evidence.domain.selection import (
    LaneControls,
    SelectionError,
    SelectionState,
    select_lane,
)
from core.verify.claim_evidence.domain.types import ProviderLane, ProviderPolicy


def _provider(
    *,
    keys: tuple[str, ...],
    models: tuple[str, ...],
    jury1_only: frozenset[str] = frozenset(),
    jury2_only: frozenset[str] = frozenset(),
    name: str = "p",
) -> ProviderPolicy:
    positional = len(keys) == len(models)
    pairs = zip(keys, models) if positional else (
        (key, model) for key in keys for model in models
    )
    lanes = tuple(
        ProviderLane(
            key,
            f"fp-{key}",
            model,
            model not in jury2_only,
            model not in jury1_only,
        )
        for key, model in pairs
    )
    return ProviderPolicy(
        name,
        keys,
        tuple(f"fp-{key}" for key in keys),
        models,
        "positional_exclusive" if positional else "seeded_cross_product",
        lanes,
    )


def _sequence(
    provider: ProviderPolicy,
    count: int,
    *,
    stage: str = "support_gate",
    state: SelectionState = SelectionState(),
    controls: LaneControls = LaneControls(),
) -> tuple[list[tuple[str, str, str]], SelectionState]:
    result = []
    for index in range(count):
        selected = select_lane(
            (provider,),
            state,
            stage=stage,
            seed="fixture-seed",
            logical_request_id=f"request-{index}",
            controls=controls,
        )
        assert selected is not None
        result.append(selected.lane_id)
        state = selected.state
    return result, state


def test_mks_01_equal_cardinality_is_positional_and_cyclic():
    provider = _provider(keys=("k1", "k2"), models=("m1", "m2"))
    lanes, state = _sequence(provider, 4)
    assert lanes == [
        ("p", "k1", "m1"),
        ("p", "k2", "m2"),
        ("p", "k1", "m1"),
        ("p", "k2", "m2"),
    ]
    assert state == SelectionState(0, 0)


def test_mks_02_unequal_cardinality_cycles_keys_and_draws_models():
    one_key, one_state = _sequence(
        _provider(keys=("k1",), models=("m1", "m2")), 5
    )
    assert one_key == [
        ("p", "k1", "m2"),
        ("p", "k1", "m1"),
        ("p", "k1", "m2"),
        ("p", "k1", "m1"),
        ("p", "k1", "m2"),
    ]
    assert one_state.model_draw_index == 5
    two_keys, _ = _sequence(
        _provider(keys=("k1", "k2"), models=("m1",)), 4
    )
    assert two_keys == [
        ("p", "k1", "m1"),
        ("p", "k2", "m1"),
        ("p", "k1", "m1"),
        ("p", "k2", "m1"),
    ]
    mixed, _ = _sequence(
        _provider(keys=("k1", "k2"), models=("m1", "m2", "m3")), 6
    )
    assert [key for _, key, _ in mixed] == ["k1", "k2", "k1", "k2", "k1", "k2"]
    assert mixed == [
        ("p", "k1", "m2"),
        ("p", "k2", "m2"),
        ("p", "k1", "m2"),
        ("p", "k2", "m1"),
        ("p", "k1", "m3"),
        ("p", "k2", "m1"),
    ]


@pytest.mark.parametrize("control_name", ["cooled_credentials", "disabled_credentials"])
def test_mks_03_controls_filter_frozen_positional_lanes_without_remapping(control_name):
    provider = _provider(keys=("k1", "k2"), models=("m1", "m2"))
    controls = LaneControls(**{control_name: frozenset({("p", "k1")})})
    lanes, _ = _sequence(provider, 3, controls=controls)
    assert lanes == [("p", "k2", "m2")] * 3


def test_rl_01_cooling_one_credential_removes_all_its_models_only():
    provider = _provider(
        keys=("k1", "k2"),
        models=("m1", "m2", "m3"),
    )
    lanes, _ = _sequence(
        provider,
        6,
        controls=LaneControls(cooled_credentials=frozenset({("p", "k1")})),
    )
    assert {key for _, key, _ in lanes} == {"k2"}
    assert {model for _, _, model in lanes} <= {"m1", "m2", "m3"}


def test_role_01_default_and_exclusive_models_are_filtered_by_stage():
    provider = _provider(
        keys=("kd", "k1", "k2"),
        models=("default", "jury1", "jury2"),
        jury1_only=frozenset({"jury1"}),
        jury2_only=frozenset({"jury2"}),
    )
    jury1, _ = _sequence(provider, 4, stage="support_gate")
    jury2, _ = _sequence(provider, 4, stage="jury2")
    assert {model for _, _, model in jury1} == {"default", "jury1"}
    assert {model for _, _, model in jury2} == {"default", "jury2"}


def test_role_02_filtering_does_not_recompute_positional_pairing():
    provider = _provider(
        keys=("k1", "k2"),
        models=("m1", "m2"),
        jury1_only=frozenset({"m1"}),
        jury2_only=frozenset({"m2"}),
    )
    jury1, _ = _sequence(provider, 3, stage="support_gate")
    jury2, _ = _sequence(provider, 3, stage="jury2")
    assert jury1 == [("p", "k1", "m1")] * 3
    assert jury2 == [("p", "k2", "m2")] * 3


def test_sel_01_persisted_cursors_resume_the_identical_sequence():
    provider = _provider(keys=("k1", "k2"), models=("m1", "m2", "m3"))
    uninterrupted, final_state = _sequence(provider, 8)
    prefix, resume_state = _sequence(provider, 3)
    suffix = []
    state = resume_state
    for index in range(3, 8):
        selected = select_lane(
            (provider,),
            state,
            stage="support_gate",
            seed="fixture-seed",
            logical_request_id=f"request-{index}",
        )
        assert selected is not None
        suffix.append(selected.lane_id)
        state = selected.state
    assert prefix + suffix == uninterrupted
    assert state == final_state


def test_selection_is_immutable_and_fails_closed_when_inputs_are_invalid():
    with pytest.raises(FrozenInstanceError):
        SelectionState().credential_cursor = 2  # type: ignore[misc]
    provider = _provider(keys=("k1",), models=("m1",))
    with pytest.raises(SelectionError):
        select_lane(
            (provider,),
            SelectionState(),
            stage="unknown",
            seed="seed",
            logical_request_id="request",
        )
    unavailable = select_lane(
        (provider,),
        SelectionState(),
        stage="support_gate",
        seed="seed",
        logical_request_id="request",
        controls=LaneControls(disabled_credentials=frozenset({("p", "k1")})),
    )
    assert unavailable is None
