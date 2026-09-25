# core/verify/backends/freetoken.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""FreeToken local OpenAI-compatible Chat Completions backend."""
import os

from core.verify.backends._chat_transport import get_json_post, llm_timeout, resolve_model
from core.verify.backends._registry import BackendSpec, register


ENV_FREETOKEN_HOST = "FREETOKEN_HOST"
ENV_FREETOKEN_MODEL = "FREETOKEN_MODEL"
_DEFAULT_FREETOKEN_HOST = "http://127.0.0.1:1919"


def _available() -> bool:
    return bool(
        (os.environ.get(ENV_FREETOKEN_HOST) or "").strip()
        or (os.environ.get(ENV_FREETOKEN_MODEL) or "").strip()
    )


def _call_freetoken(
    system: str, user: str, model: str | None, max_tokens: int | None = 1500,
) -> str:
    host = (os.environ.get(ENV_FREETOKEN_HOST) or "").strip()
    if not host:
        host = _DEFAULT_FREETOKEN_HOST
    if "://" not in host:
        host = "http://" + host
    resolved = resolve_model(model, ENV_FREETOKEN_MODEL)
    data = get_json_post()(
        host.rstrip("/") + "/v1/chat/completions",
        {
            "model": resolved,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
            "temperature": 0,
            "stream": False,
        },
        {"Content-Type": "application/json"},
        timeout=llm_timeout(),
    )
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()
    return ""


register(BackendSpec(
    name="freetoken",
    env_key="",
    env_model=ENV_FREETOKEN_MODEL,
    base_url=None,
    available=_available,
    call=_call_freetoken,
    supports_omitted_max_tokens=True,
))
