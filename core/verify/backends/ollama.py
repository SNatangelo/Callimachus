# core/verify/backends/ollama.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Ollama backend — local and cloud chat."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import (
    ENV_LLM_TIMEOUT, _read_timeout_env, credential_value, get_json_post,
)


def _ollama_timeout() -> int:
    """Verification timeout for Ollama, defaulting high because a local model is slow.

    Still overridable by ``CITATION_VERIFIER_LLM_TIMEOUT`` (the shared knob), but its
    floor is 900s rather than the 300s the cloud backends default to: codex found a
    local model routinely needs the longer wall for a single verification call."""
    return _read_timeout_env(ENV_LLM_TIMEOUT, 900)

ENV_OLLAMA_HOST = "OLLAMA_HOST"
ENV_OLLAMA_API_KEY = "OLLAMA_API_KEY"
ENV_LLM_SEED = "CITATION_VERIFIER_LLM_SEED"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
_DEFAULT_OLLAMA_CLOUD_HOST = "https://ollama.com"


def _sampling_options(max_tokens: int) -> dict:
    """Deterministic sampling options shared by /api/chat and /api/generate."""
    options = {"num_predict": max_tokens, "temperature": 0}
    seed = (os.environ.get(ENV_LLM_SEED) or "").strip()
    if seed:
        try:
            options["seed"] = int(seed)
        except ValueError:
            pass  # non-numeric seed: skip rather than fail the whole call
    return options


def _available() -> bool:
    return bool((os.environ.get(ENV_OLLAMA_HOST) or "").strip()
                or (os.environ.get(ENV_OLLAMA_API_KEY) or "").strip())


def _call_ollama_api(system: str, user: str, model: str, max_tokens: int = 3000) -> str:
    api_key = (credential_value(ENV_OLLAMA_API_KEY) or "").strip()
    host = (os.environ.get(ENV_OLLAMA_HOST) or "").strip()
    if not host:
        host = _DEFAULT_OLLAMA_CLOUD_HOST if api_key else _DEFAULT_OLLAMA_HOST
    # Normalise bare host:port to http://host:port so urlopen can parse it
    if "://" not in host:
        host = "http://" + host
    host = host.rstrip("/")
    direct_cloud = bool(api_key and host.lower().startswith(_DEFAULT_OLLAMA_CLOUD_HOST))
    model_name = model
    if direct_cloud and model_name:
        if model_name.endswith("-cloud"):
            model_name = model_name[:-6]
        elif model_name.endswith(":cloud"):
            model_name = model_name[:-6]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    _jp = get_json_post()
    if direct_cloud:
        data = _jp(
            host + "/api/chat",
            {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": _sampling_options(max_tokens),
            },
            headers,
            timeout=_ollama_timeout(),
        )
        message = data.get("message") or {}
        return (message.get("content") or data.get("response") or "").strip()
    data = _jp(
        host + "/api/chat",
        {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "options": _sampling_options(max_tokens),
        },
        headers,
        timeout=_ollama_timeout(),
    )
    message = data.get("message") or {}
    return (message.get("content") or "").strip()


register(BackendSpec(
    name="ollama",
    env_key=ENV_OLLAMA_API_KEY,
    available=_available,
    call=_call_ollama_api,
    auto_priority=60,
    codex_priority=40,
    claude_code_priority=60,
    antigravity_priority=50,
))
