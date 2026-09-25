# core/verify/claim_evidence/domain/selection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure deterministic credential-cycle and seeded model selection."""
from dataclasses import dataclass
import hashlib

from .types import ProviderLane, ProviderPolicy


SELECTOR_ALGORITHM = "sha256-counter-v1"


class SelectionError(ValueError):
    """Frozen selection inputs or state are invalid."""


@dataclass(frozen=True, slots=True)
class SelectionState:
    credential_cursor: int = 0
    model_draw_index: int = 0


@dataclass(frozen=True, slots=True)
class LaneControls:
    disabled_credentials: frozenset[tuple[str, str]] = frozenset()
    cooled_credentials: frozenset[tuple[str, str]] = frozenset()
    disabled_lanes: frozenset[tuple[str, str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class LaneSelection:
    provider: str
    lane: ProviderLane
    state: SelectionState
    selector_algorithm: str = SELECTOR_ALGORITHM

    @property
    def lane_id(self) -> tuple[str, str, str]:
        return (self.provider, self.lane.key_alias, self.lane.model)


def select_lane(
    providers: tuple[ProviderPolicy, ...],
    state: SelectionState,
    *,
    stage: str,
    seed: str,
    logical_request_id: str,
    controls: LaneControls = LaneControls(),
) -> LaneSelection | None:
    _validate_inputs(providers, state, stage, seed, logical_request_id, controls)
    credentials = tuple(
        (provider, alias)
        for provider in providers
        for alias in provider.credential_aliases
    )
    for offset in range(len(credentials)):
        position = (state.credential_cursor + offset) % len(credentials)
        provider, alias = credentials[position]
        lanes = _eligible_lanes(provider, alias, stage, controls)
        if not lanes:
            continue
        next_cursor = (position + 1) % len(credentials)
        if provider.pairing_mode == "positional_exclusive":
            lane = lanes[0]
            next_state = SelectionState(next_cursor, state.model_draw_index)
        else:
            lane = _draw(
                lanes,
                seed=seed,
                logical_request_id=logical_request_id,
                provider=provider.name,
                alias=alias,
                counter=state.model_draw_index,
            )
            next_state = SelectionState(next_cursor, state.model_draw_index + 1)
        return LaneSelection(provider.name, lane, next_state)
    return None


def _eligible_lanes(
    provider: ProviderPolicy,
    alias: str,
    stage: str,
    controls: LaneControls,
) -> tuple[ProviderLane, ...]:
    credential = (provider.name, alias)
    if (
        credential in controls.disabled_credentials
        or credential in controls.cooled_credentials
    ):
        return ()
    result = tuple(
        lane
        for lane in provider.lanes
        if lane.key_alias == alias
        and (
            lane.jury1_eligible
            if stage in {
                "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
                "explanation_evidence",
            }
            else lane.jury2_eligible
        )
        and (provider.name, alias, lane.model) not in controls.disabled_lanes
    )
    if provider.pairing_mode == "positional_exclusive" and len(result) > 1:
        raise SelectionError("positional credentials have multiple eligible models")
    return result


def _draw(
    lanes: tuple[ProviderLane, ...],
    *,
    seed: str,
    logical_request_id: str,
    provider: str,
    alias: str,
    counter: int,
) -> ProviderLane:
    material = "\x1f".join(
        (
            SELECTOR_ALGORITHM,
            seed,
            logical_request_id,
            provider,
            alias,
            str(counter),
        )
    )
    number = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest(), "big")
    return lanes[number % len(lanes)]


def _validate_inputs(
    providers: tuple[ProviderPolicy, ...],
    state: SelectionState,
    stage: str,
    seed: str,
    logical_request_id: str,
    controls: LaneControls,
) -> None:
    if (
        not isinstance(providers, tuple)
        or not providers
        or any(not isinstance(provider, ProviderPolicy) for provider in providers)
        or len({provider.name for provider in providers}) != len(providers)
    ):
        raise SelectionError("provider policy is invalid")
    if (
        not isinstance(state, SelectionState)
        or isinstance(state.credential_cursor, bool)
        or isinstance(state.model_draw_index, bool)
        or not isinstance(state.credential_cursor, int)
        or not isinstance(state.model_draw_index, int)
        or state.credential_cursor < 0
        or state.model_draw_index < 0
    ):
        raise SelectionError("selection state is invalid")
    if stage not in {
        "support_gate", "full_support_gate", "contrary_gate", "topic_gate",
        "explanation_evidence", "jury2",
    }:
        raise SelectionError("selection stage is invalid")
    if not isinstance(seed, str) or not seed or not isinstance(logical_request_id, str) or not logical_request_id:
        raise SelectionError("selection identity is invalid")
    if not isinstance(controls, LaneControls):
        raise SelectionError("lane controls are invalid")
    for provider in providers:
        _validate_provider(provider)
    known_credentials = {
        (provider.name, alias)
        for provider in providers
        for alias in provider.credential_aliases
    }
    known_lanes = {
        (provider.name, lane.key_alias, lane.model)
        for provider in providers
        for lane in provider.lanes
    }
    if (
        not controls.disabled_credentials <= known_credentials
        or not controls.cooled_credentials <= known_credentials
        or not controls.disabled_lanes <= known_lanes
    ):
        raise SelectionError("lane controls contain unknown identities")


def _validate_provider(provider: ProviderPolicy) -> None:
    lane_ids = tuple(
        (lane.key_alias, lane.model)
        for lane in provider.lanes
    )
    if (
        provider.pairing_mode not in {"positional_exclusive", "seeded_cross_product"}
        or not provider.name
        or not provider.credential_aliases
        or len(set(provider.credential_aliases)) != len(provider.credential_aliases)
        or len(provider.credential_aliases) != len(provider.credential_fingerprints)
        or not provider.models
        or len(set(provider.models)) != len(provider.models)
        or not provider.lanes
        or len(set(lane_ids)) != len(lane_ids)
        or any(lane.key_alias not in provider.credential_aliases for lane in provider.lanes)
        or any(lane.model not in provider.models for lane in provider.lanes)
        or any(
            type(lane.jury1_eligible) is not bool
            or type(lane.jury2_eligible) is not bool
            for lane in provider.lanes
        )
    ):
        raise SelectionError("provider pairing policy is invalid")
    if provider.pairing_mode == "positional_exclusive":
        expected = tuple(zip(provider.credential_aliases, provider.models))
        if lane_ids != expected:
            raise SelectionError("positional pairing policy is invalid")
    else:
        expected = tuple(
            (alias, model)
            for alias in provider.credential_aliases
            for model in provider.models
        )
        if lane_ids != expected:
            raise SelectionError("cross-product pairing policy is invalid")
