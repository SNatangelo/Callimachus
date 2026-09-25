# core/verify/backends/transport.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Neutral direct transport for registered backend specifications."""

from __future__ import annotations

from core.verify.backends._chat_transport import (
    credential_override,
    reasoning_override,
    single_physical_attempt,
)
from core.verify.backends._registry import all_specs
from core.verify.claim_evidence.config import ConfigError


DEFAULT_MAX_TOKENS = 4000
REASONING_MODES = frozenset({"auto", "on", "off"})
REASONING_EFFORTS = frozenset({"low", "medium", "high", "max", "xhigh"})


def call_registered_provider(
    provider: str,
    system: str,
    user: str,
    model: str,
    secret: str | None,
    *,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
    reasoning: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Call one explicitly registered provider once, with no fallback."""
    matching = [
        spec for spec in all_specs()
        if provider == spec.name
    ]
    if len(matching) != 1 or matching[0].call is None:
        raise ConfigError("requested provider is not registered")
    spec = matching[0]
    if not isinstance(model, str) or not model:
        raise ConfigError("provider model is required")
    token_limit = max_tokens
    if token_limit is None and not spec.supports_omitted_max_tokens:
        raise ConfigError("provider requires a max_tokens budget")
    if token_limit is not None and (
        type(token_limit) is not int or token_limit <= 0
    ):
        raise ConfigError("provider max_tokens must be positive")
    if reasoning is not None and reasoning not in REASONING_MODES:
        raise ConfigError("provider reasoning mode is invalid")
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ConfigError("provider reasoning effort is invalid")
    with reasoning_override(reasoning, reasoning_effort), credential_override(
        spec.env_key, secret
    ):
        with single_physical_attempt():
            return spec.call(system, user, model, token_limit)
