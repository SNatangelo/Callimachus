# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Scope shared scheduler controls to the configured provider lanes."""
from typing import Any

from ..domain.types import ProviderPolicy


def configured_controls(
    controls: dict[str, Any], providers: tuple[ProviderPolicy, ...],
) -> dict[str, Any]:
    """Exclude controls belonging to providers absent from this run."""
    credentials = frozenset(
        (provider.name, credential_id)
        for provider in providers
        for credential_id in provider.credential_aliases
    )
    lanes = frozenset(
        (provider.name, lane.key_alias, lane.model)
        for provider in providers
        for lane in provider.lanes
    )
    return {
        "disabled_credentials": controls["disabled_credentials"] & credentials,
        "disabled_lanes": controls["disabled_lanes"] & lanes,
        "key_eligibility_ms": tuple(
            item for item in controls["key_eligibility_ms"] if item[0] in credentials
        ),
        "cooldowns": {
            key: value for key, value in controls["cooldowns"].items() if key in lanes
        },
    }
