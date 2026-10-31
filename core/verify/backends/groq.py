# core/verify/backends/groq.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Groq's native OpenAI-compatible Chat Completions backend."""
import os

from core.verify.backends._chat_transport import call_chat_completions
from core.verify.backends._registry import BackendSpec, register

ENV_GROQ_KEY = "GROQ_API_KEY"
ENV_GROQ_MODEL = "GROQ_MODEL"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _available() -> bool:
    return bool(os.environ.get(ENV_GROQ_KEY))


def _call_groq_api(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    return call_chat_completions(system, user, model, max_tokens, spec=SPEC)


SPEC = BackendSpec(
    name="groq",
    env_key=ENV_GROQ_KEY,
    env_model=ENV_GROQ_MODEL,
    base_url=GROQ_BASE_URL,
    available=_available,
    call=_call_groq_api,
)
register(SPEC)
