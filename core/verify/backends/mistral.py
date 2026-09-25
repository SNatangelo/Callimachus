# core/verify/backends/mistral.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Mistral AI backend — Chat Completions API."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import call_chat_completions

ENV_MISTRAL_KEY = "MISTRAL_API_KEY"
ENV_MISTRAL_MODEL = "MISTRAL_MODEL"


def _available() -> bool:
    return bool(os.environ.get(ENV_MISTRAL_KEY))


SPEC = BackendSpec(
    name="mistral",
    env_key=ENV_MISTRAL_KEY,
    env_model=ENV_MISTRAL_MODEL,
    base_url="https://api.mistral.ai/v1",
    available=_available,
    auto_priority=90,
)


def _call_mistral_api(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    return call_chat_completions(system, user, model, max_tokens, spec=SPEC)


SPEC.call = _call_mistral_api
register(SPEC)
