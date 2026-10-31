# core/verify/backends/xai.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""xAI's native OpenAI-compatible Chat Completions backend."""
import os

from core.verify.backends._chat_transport import call_chat_completions
from core.verify.backends._registry import BackendSpec, register

ENV_XAI_KEY = "XAI_API_KEY"
ENV_XAI_MODEL = "XAI_MODEL"
XAI_BASE_URL = "https://api.x.ai/v1"


def _available() -> bool:
    return bool(os.environ.get(ENV_XAI_KEY))


def _call_xai_api(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    return call_chat_completions(system, user, model, max_tokens, spec=SPEC)


SPEC = BackendSpec(
    name="xai",
    env_key=ENV_XAI_KEY,
    env_model=ENV_XAI_MODEL,
    base_url=XAI_BASE_URL,
    available=_available,
    call=_call_xai_api,
)
register(SPEC)
