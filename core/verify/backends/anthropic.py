# core/verify/backends/anthropic.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Anthropic Messages API backend.

Supports both the official Anthropic API and Anthropic-compatible local/
third-party endpoints via ``ANTHROPIC_BASE_URL``.  Auth is attempted via
``ANTHROPIC_API_KEY`` first, then ``ANTHROPIC_AUTH_TOKEN`` as a fallback.
"""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    credential_value,
    get_json_post,
    llm_timeout,
    reasoning_mode,
)

_DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_ANTHROPIC_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_ANTHROPIC_BASE = "ANTHROPIC_BASE_URL"


def _resolve_anthropic_key() -> str | None:
    """Return the best available API key/token or None."""
    return (credential_value(ENV_ANTHROPIC_KEY) or "").strip() or \
           (os.environ.get(ENV_ANTHROPIC_TOKEN) or "").strip() or None


def _available() -> bool:
    return bool(_resolve_anthropic_key())


def _call_api(system: str, user: str, model: str, max_tokens: int = 1500) -> str:
    key = _resolve_anthropic_key()
    if not key:
        raise RuntimeError(
            f"Neither {ENV_ANTHROPIC_KEY} nor {ENV_ANTHROPIC_TOKEN} is set"
        )
    base = (os.environ.get(ENV_ANTHROPIC_BASE) or "").rstrip("/")
    url = f"{base}/messages" if base else _DEFAULT_ANTHROPIC_URL
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    mode = reasoning_mode()
    if mode == "on":
        body["thinking"] = {"type": "enabled", "budget_tokens": min(2048, max_tokens // 2)}
        # Anthropic extended thinking rejects temperature=0; the provider
        # requires its default sampling temperature while thinking is enabled.
        body.pop("temperature", None)
    elif mode == "off":
        body["thinking"] = {"type": "disabled"}
    data = get_json_post()(
        url,
        body,
        {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        timeout=llm_timeout(),
    )
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )


register(BackendSpec(
    name="anthropic",
    env_key=ENV_ANTHROPIC_KEY,
    available=_available,
    call=_call_api,
    supports_tools=True,
    supports_reasoning=True,
    auto_priority=30,
    codex_priority=70,
    claude_code_priority=30,
    antigravity_priority=60,
))
