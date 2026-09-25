# core/verify/backends/openai_compat.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenAI-compatible Chat Completions backend (DeepSeek, Together, vLLM, …).

Driven by the shared ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL``
environment variables.  Does NOT use the generic ``call_chat_completions`` helper
because the URL is resolved at call time via ``OPENAI_BASE_URL`` with a fallback.
"""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    credential_value, get_json_post, llm_timeout, resolve_model,
    openai_reasoning_fields,
)

ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_OPENAI_MODEL = "OPENAI_MODEL"
_OPENAI_COMPAT_DEFAULT_URL = "https://api.deepseek.com"


def _available() -> bool:
    return bool((os.environ.get(ENV_OPENAI_BASE_URL) or "").strip()
                and (os.environ.get(ENV_OPENAI_KEY) or "").strip())


def _call_openai_compatible(system: str, user: str, model: str | None,
                            max_tokens: int | None = 1500) -> str:
    base = (os.environ.get(ENV_OPENAI_BASE_URL) or "").rstrip("/")
    if not base:
        base = _OPENAI_COMPAT_DEFAULT_URL
    key = credential_value(ENV_OPENAI_KEY)
    if not key:
        raise RuntimeError(f"{ENV_OPENAI_KEY} not set - cannot use openai_compatible backend")
    resolved = resolve_model(model, ENV_OPENAI_MODEL)
    deepseek_json = resolved.lower().startswith("deepseek-v4-")
    url = f"{base}/chat/completions"
    data = get_json_post()(
        url,
        {
            "model": resolved,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
            "temperature": 0,
            "stream": False,
            **openai_reasoning_fields(
                resolved,
                structured_output=deepseek_json,
            ),
            # DeepSeek V4 supports JSON mode.  This backend is also used for
            # compatible endpoints, so keep it opt-in for those providers.
            **({"response_format": {"type": "json_object"}}
               if deepseek_json else {}),
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=llm_timeout(),
    )
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()
    return ""


register(BackendSpec(
    name="openai_compatible",
    env_key=ENV_OPENAI_KEY,
    env_model=ENV_OPENAI_MODEL,
    base_url=None,  # resolved at call time via OPENAI_BASE_URL
    available=_available,
    call=_call_openai_compatible,
    auto_priority=80,
    supports_reasoning=True,
    supports_omitted_max_tokens=True,
))
