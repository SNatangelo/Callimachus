# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed provider capability policy validation."""
import re
from typing import Mapping, Sequence

from .context_config import ConfigError


def resolve_confidence_policy(
    env: Mapping[str, str],
    providers: Sequence[object],
    specs: Mapping[str, object],
    context_mode: str,
) -> tuple[float | None, str | None]:
    """Validate configured provider capabilities before dispatch."""
    threshold: float | None = None
    policy_id: str | None = None
    for provider in providers:
        capability = getattr(specs[provider.name], "claim_evidence_capabilities")
        if not capability:
            continue
        env_name = capability.get("confidence_env")
        configured_policy_id = capability.get("policy_id")
        label = capability.get("provider_label")
        pattern = capability.get("pinned_model_pattern")
        required_context = capability.get("jury1_context_mode")
        context_help = capability.get("jury1_context_help")
        fields = (env_name, configured_policy_id, label, pattern, required_context)
        if not all(isinstance(value, str) and value for value in fields):
            raise ConfigError("provider claim-evidence capability is invalid")
        try:
            value = float(env[env_name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{env_name} is required") from exc
        if not 0 < value <= 1:
            raise ConfigError(f"{env_name} must be in (0,1]")
        if threshold is not None:
            raise ConfigError("multiple confidence-policy providers are not supported")
        if any(not re.fullmatch(pattern, lane.model) for lane in provider.lanes):
            raise ConfigError(f"{label} requires a pinned model")
        if any(lane.jury1_eligible for lane in provider.lanes) and context_mode != required_context:
            suffix = f". {context_help}" if isinstance(context_help, str) and context_help else ""
            raise ConfigError(f"{label} Jury1 requires {required_context} context{suffix}")
        threshold, policy_id = value, configured_policy_id
    return threshold, policy_id
