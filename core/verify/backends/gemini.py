# core/verify/backends/gemini.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Gemini GenerateContent API backend."""
import os
import urllib.parse

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    credential_value,
    get_json_post,
    llm_timeout,
    resolve_model,
)

ENV_GEMINI_KEY = "GEMINI_API_KEY"
ENV_GEMINI_MODEL = "GEMINI_MODEL"
_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/{model}:generateContent"


def _available() -> bool:
    return bool(os.environ.get(ENV_GEMINI_KEY))


def _call_gemini_api(system: str, user: str, model: str, max_tokens: int = 1500) -> str:
    key = credential_value(ENV_GEMINI_KEY)
    if not key:
        raise RuntimeError(f"{ENV_GEMINI_KEY} not set - cannot use the Gemini backend")
    resolved = resolve_model(model, ENV_GEMINI_MODEL)
    model_name = resolved if resolved.startswith("models/") else f"models/{resolved}"
    url = _GEMINI_API_URL.format(model=urllib.parse.quote(model_name, safe="/"))
    data = get_json_post()(
        url,
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
        },
        # The key travels in a header, not the query string, so it doesn't end up
        # in server access logs or any URL captured for debugging.
        {"Content-Type": "application/json", "x-goog-api-key": key},
        timeout=llm_timeout(),
    )
    texts = []
    for cand in data.get("candidates", []):
        parts = (((cand or {}).get("content") or {}).get("parts") or [])
        for part in parts:
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n".join(texts).strip()


register(BackendSpec(
    name="gemini",
    env_key=ENV_GEMINI_KEY,
    env_model=ENV_GEMINI_MODEL,
    available=_available,
    call=_call_gemini_api,
    auto_priority=40,
    claude_code_priority=70,
    antigravity_priority=20,
))
