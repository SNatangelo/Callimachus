# core/verify/backends/opencode.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenCode Zen backend — Chat Completions API (free models available)."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import call_chat_completions

ENV_OPENCODE_KEY = "OPENCODE_API_KEY"
ENV_OPENCODE_MODEL = "OPENCODE_MODEL"


def _available() -> bool:
    return bool(os.environ.get(ENV_OPENCODE_KEY))


SPEC = BackendSpec(
    name="opencode",
    env_key=ENV_OPENCODE_KEY,
    env_model=ENV_OPENCODE_MODEL,
    base_url="https://opencode.ai/zen/v1",
    available=_available,
    auto_priority=95,
)


def _call_opencode_api(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    return call_chat_completions(system, user, model, max_tokens, spec=SPEC)


SPEC.call = _call_opencode_api
register(SPEC)
