# core/verify/backends/ollama.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Ollama backend — local and cloud chat."""
import os
import ipaddress
from urllib.parse import urlsplit, urlunsplit

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
ENV_OLLAMA_MODEL = "OLLAMA_MODEL"
ENV_LLM_SEED = "CITATION_VERIFIER_LLM_SEED"
_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
_DEFAULT_OLLAMA_CLOUD_HOST = "https://ollama.com"


def normalize_ollama_host(host: str) -> str:
    """Interpret Ollama's native bare/bind host as a client endpoint."""
    if not isinstance(host, str) or not host.strip() or any(char.isspace() or ord(char) < 32 for char in host):
        raise ValueError("Invalid Ollama host")
    bare = "://" not in host
    parsed = urlsplit("http://" + host if bare else host)
    hostname, port = parsed.hostname, parsed.port
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Invalid Ollama host")
    hostname = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(hostname, hostname)
    if bare and port is None:
        port = 11434
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))


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


def _is_loopback_host(host: str) -> bool:
    try:
        parsed = urlsplit(normalize_ollama_host(host))
        if parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname:
            return False
        return parsed.hostname.lower() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _call_ollama_api(system: str, user: str, model: str, max_tokens: int = 3000) -> str:
    return _call_ollama(system, user, model, max_tokens, allow_api_key=True)


def _call_ollama_local(system: str, user: str, model: str, max_tokens: int = 3000) -> str:
    if (credential_value(ENV_OLLAMA_API_KEY) or "").strip():
        from core.verify.backends.errors import ConfigError
        raise ConfigError("ollama_local does not accept an API key")
    host = (os.environ.get(ENV_OLLAMA_HOST) or _DEFAULT_OLLAMA_HOST).strip()
    if not _is_loopback_host(host):
        from core.verify.backends.errors import ConfigError
        raise ConfigError("ollama_local requires a loopback host")
    return _call_ollama(system, user, model, max_tokens, allow_api_key=False)


def _call_ollama(system: str, user: str, model: str, max_tokens: int, *, allow_api_key: bool) -> str:
    api_key = (credential_value(ENV_OLLAMA_API_KEY) or "").strip() if allow_api_key else ""
    host = (os.environ.get(ENV_OLLAMA_HOST) or "").strip()
    if not host:
        host = _DEFAULT_OLLAMA_CLOUD_HOST if api_key else _DEFAULT_OLLAMA_HOST
    try:
        host = normalize_ollama_host(host)
    except ValueError:
        from core.verify.backends.errors import ConfigError
        raise ConfigError("The configured Ollama host is invalid.", backend="ollama") from None
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
    env_model=ENV_OLLAMA_MODEL,
    available=_available,
    call=_call_ollama_api,
    auto_priority=60,
    codex_priority=40,
    claude_code_priority=60,
    antigravity_priority=50,
))


register(BackendSpec(
    name="ollama_local",
    env_key="",
    env_model=ENV_OLLAMA_MODEL,
    available=lambda: (
        not (os.environ.get(ENV_OLLAMA_API_KEY) or "").strip()
        and bool((os.environ.get(ENV_OLLAMA_HOST) or "").strip()
                 or (os.environ.get(ENV_OLLAMA_MODEL) or "").strip())
        and _is_loopback_host((os.environ.get(ENV_OLLAMA_HOST) or _DEFAULT_OLLAMA_HOST).strip())
    ),
    call=_call_ollama_local,
    auto_priority=60,
    codex_priority=45,
    claude_code_priority=65,
    antigravity_priority=55,
))
