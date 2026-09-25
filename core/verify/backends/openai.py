# core/verify/backends/openai.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenAI Responses API backend."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    credential_value, get_json_post, llm_timeout, reasoning_mode,
    reasoning_effort,
)

_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_LLM_SEED = "CITATION_VERIFIER_LLM_SEED"


def _available() -> bool:
    base = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if base and "api.openai.com" not in base:
        return False  # non-official base → openai_compatible, not this backend
    return bool(os.environ.get(ENV_OPENAI_KEY))


def _call_openai_api(system: str, user: str, model: str, max_tokens: int = 1500) -> str:
    key = credential_value(ENV_OPENAI_KEY)
    if not key:
        raise RuntimeError(f"{ENV_OPENAI_KEY} not set - cannot use the OpenAI backend")
    body = {
        "model": model,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_tokens,
        "temperature": 0,
    }
    seed = (os.environ.get(ENV_LLM_SEED) or "").strip()
    if seed:
        try:
            body["seed"] = int(seed)
        except ValueError:
            pass  # non-numeric seed: skip rather than fail the whole call
    mode = reasoning_mode()
    if mode == "on":
        body["reasoning"] = {
            "effort": reasoning_effort(),
        }
    data = get_json_post()(
        _OPENAI_RESPONSES_URL,
        body,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=llm_timeout(),
    )

    # _first_text_line equivalent inline
    def _collect_text(obj) -> list[str]:
        out = []
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, list):
            for item in obj:
                out.extend(_collect_text(item))
            return out
        if not isinstance(obj, dict):
            return out
        if isinstance(obj.get("output_text"), str):
            out.append(obj["output_text"])
        typ = obj.get("type")
        if typ in ("output_text", "text") and isinstance(obj.get("text"), str):
            out.append(obj["text"])
        for value in obj.values():
            out.extend(_collect_text(value))
        return out

    seen = []
    for text in _collect_text(data.get("output", data)):
        if text and text not in seen:
            seen.append(text)
    return "\n".join(seen).strip()


register(BackendSpec(
    name="openai",
    env_key=ENV_OPENAI_KEY,
    available=_available,
    call=_call_openai_api,
    supports_tools=True,
    auto_priority=20,
    codex_priority=20,
    claude_code_priority=50,
    antigravity_priority=30,
))
