# core/verify/backends/openrouter.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenRouter backend — Chat Completions API with optional attribution headers."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    credential_value,
    get_json_post,
    llm_timeout,
)

ENV_OPENROUTER_KEY = "OPENROUTER_API_KEY"
ENV_OPENROUTER_SITE_URL = "CITATION_VERIFIER_OPENROUTER_SITE_URL"
ENV_OPENROUTER_APP_NAME = "CITATION_VERIFIER_OPENROUTER_APP_NAME"
_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def _available() -> bool:
    return bool(os.environ.get(ENV_OPENROUTER_KEY))


def _call_openrouter_api(system: str, user: str, model: str,
                         max_tokens: int = 1500) -> str:
    key = credential_value(ENV_OPENROUTER_KEY)
    if not key:
        raise RuntimeError(f"{ENV_OPENROUTER_KEY} not set - cannot use the OpenRouter backend")
    headers: dict[str, str] = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    site_url = (os.environ.get(ENV_OPENROUTER_SITE_URL) or "").strip()
    if site_url:
        headers["HTTP-Referer"] = site_url
    app_name = (os.environ.get(ENV_OPENROUTER_APP_NAME) or "").strip()
    if app_name:
        headers["X-OpenRouter-Title"] = app_name
    _jp = get_json_post()
    data = _jp(
        _OPENROUTER_CHAT_URL,
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        },
        headers,
        timeout=llm_timeout(),
    )
    msg = ((((data.get("choices") or [{}])[0]).get("message") or {}).get("content"))
    if isinstance(msg, str):
        return msg.strip()
    if isinstance(msg, list):
        return "\n".join(str(m) for m in msg if m).strip()
    return ""


register(BackendSpec(
    name="openrouter",
    env_key=ENV_OPENROUTER_KEY,
    available=_available,
    call=_call_openrouter_api,
    auto_priority=50,
    codex_priority=30,
))
