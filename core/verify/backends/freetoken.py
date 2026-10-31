# core/verify/backends/freetoken.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""FreeToken local OpenAI-compatible Chat Completions backend."""
import os
import ipaddress
from urllib.parse import urlsplit

from core.verify.backends._chat_transport import credential_value, get_json_post, llm_timeout, resolve_model
from core.verify.backends._registry import BackendSpec, register


ENV_FREETOKEN_HOST = "FREETOKEN_HOST"
ENV_FREETOKEN_MODEL = "FREETOKEN_MODEL"
ENV_FREETOKEN_API_KEY = "FREETOKEN_API_KEY"
_DEFAULT_FREETOKEN_HOST = "http://127.0.0.1:1919"


def _is_loopback_host(host: str) -> bool:
    try:
        parsed = urlsplit(host if "://" in host else "http://" + host)
        if parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname:
            return False
        return parsed.hostname.lower() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _available() -> bool:
    host = (os.environ.get(ENV_FREETOKEN_HOST) or _DEFAULT_FREETOKEN_HOST).strip()
    return (
        not (os.environ.get(ENV_FREETOKEN_API_KEY) or "").strip()
        and _is_loopback_host(host)
        and bool((os.environ.get(ENV_FREETOKEN_HOST) or "").strip()
                 or (os.environ.get(ENV_FREETOKEN_MODEL) or "").strip())
    )


def _authenticated_available() -> bool:
    return bool((credential_value(ENV_FREETOKEN_API_KEY) or "").strip())


def _call_freetoken(
    system: str, user: str, model: str | None, max_tokens: int | None = 1500,
) -> str:
    if (credential_value(ENV_FREETOKEN_API_KEY) or "").strip():
        from core.verify.backends.errors import ConfigError
        raise ConfigError("freetoken is credentialless and forbids key values")
    host = (os.environ.get(ENV_FREETOKEN_HOST) or _DEFAULT_FREETOKEN_HOST).strip()
    if not _is_loopback_host(host):
        from core.verify.backends.errors import ConfigError
        raise ConfigError("freetoken requires a loopback host without an API key")
    return _call_freetoken_request(system, user, model, max_tokens, api_key="")


def _call_freetoken_authenticated(
    system: str, user: str, model: str | None, max_tokens: int | None = 1500,
) -> str:
    api_key = (credential_value(ENV_FREETOKEN_API_KEY) or "").strip()
    if not api_key or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        from core.verify.backends.errors import ConfigError
        raise ConfigError("FREETOKEN_API_KEY is invalid")
    return _call_freetoken_request(system, user, model, max_tokens, api_key=api_key)


def _call_freetoken_request(
    system: str, user: str, model: str | None, max_tokens: int | None, *, api_key: str,
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
        {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
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


register(BackendSpec(
    name="freetoken_authenticated",
    env_key=ENV_FREETOKEN_API_KEY,
    env_model=ENV_FREETOKEN_MODEL,
    base_url=None,
    available=_authenticated_available,
    call=_call_freetoken_authenticated,
    supports_omitted_max_tokens=True,
))
