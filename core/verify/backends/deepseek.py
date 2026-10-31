# core/verify/backends/deepseek.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""DeepSeek's native OpenAI-compatible Chat Completions backend."""
import os

from core.verify.backends._chat_transport import (
    credential_value,
    get_json_post,
    llm_timeout,
    openai_reasoning_fields,
    resolve_model,
)
from core.verify.backends._registry import BackendSpec, register

ENV_DEEPSEEK_KEY = "DEEPSEEK_API_KEY"
ENV_DEEPSEEK_MODEL = "DEEPSEEK_MODEL"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _available() -> bool:
    return bool(os.environ.get(ENV_DEEPSEEK_KEY))


def _call_deepseek_api(
    system: str,
    user: str,
    model: str | None,
    max_tokens: int | None = 1500,
) -> str:
    key = credential_value(ENV_DEEPSEEK_KEY)
    if not key:
        raise RuntimeError(f"{ENV_DEEPSEEK_KEY} not set - cannot use the DeepSeek backend")
    resolved = resolve_model(model, ENV_DEEPSEEK_MODEL)
    deepseek_json = resolved.lower().startswith("deepseek-v4-")
    data = get_json_post()(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        {
            "model": resolved,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
            "temperature": 0,
            "stream": False,
            **openai_reasoning_fields(resolved, structured_output=deepseek_json),
            **({"response_format": {"type": "json_object"}} if deepseek_json else {}),
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=llm_timeout(),
    )
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()
    return ""


register(BackendSpec(
    name="deepseek",
    env_key=ENV_DEEPSEEK_KEY,
    env_model=ENV_DEEPSEEK_MODEL,
    base_url=DEEPSEEK_BASE_URL,
    available=_available,
    call=_call_deepseek_api,
    supports_reasoning=True,
    supports_omitted_max_tokens=True,
))
