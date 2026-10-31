# core/app/providers/discovery.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, read-only provider checks for the desktop configuration UI."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

import certifi

from core.infra._local_executables import find_cli


_TIMEOUT = 8
_MAX_BODY = 512 * 1024
_MAX_MODELS = 500
_MAX_PAGES = 3
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}$")
_DNS_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?$")
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}
_MODEL_ENVS = {
    "openai": "OPENAI_MODEL",
    "openai_compatible": "OPENAI_MODEL",
    "deepseek": "DEEPSEEK_MODEL",
    "groq": "GROQ_MODEL",
    "xai": "XAI_MODEL",
    "anthropic": "ANTHROPIC_MODEL",
    "gemini": "GEMINI_MODEL",
    "openrouter": "OPENROUTER_MODEL",
    "typesafe": "TYPESAFE_MODEL",
    "glm": "ZHIPUAI_MODEL",
    "mistral": "MISTRAL_MODEL",
    "opencode": "OPENCODE_MODEL",
    "ollama": "OLLAMA_MODEL",
    "ollama_local": "OLLAMA_MODEL",
    "freetoken": "FREETOKEN_MODEL",
    "freetoken_authenticated": "FREETOKEN_MODEL",
    "claude_cli": "CLAUDE_CLI_MODEL",
    "codex_cli": "CODEX_CLI_MODEL",
    "gemini_cli": "GEMINI_CLI_MODEL",
    "antigravity_cli": "ANTIGRAVITY_CLI_MODEL",
    "opencode_cli": "OPENCODE_CLI_MODEL",
}
_API_KEYS = {
    "openai": "OPENAI_API_KEY", "openai_compatible": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "typesafe": "TYPESAFE_API_KEY",
    "glm": "ZHIPUAI_API_KEY", "mistral": "MISTRAL_API_KEY",
    "opencode": "OPENCODE_API_KEY", "ollama": "OLLAMA_API_KEY",
    "freetoken": "FREETOKEN_API_KEY",
}


class _DiscoveryError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ProviderCheckOpener:
    """Use strict, certifi-augmented TLS for HTTPS catalog requests."""

    def __init__(self):
        self._http_opener = urllib.request.build_opener(_NoRedirect)
        self._https_opener = None

    def open(self, request, timeout):
        if request.type.lower() != "https":
            return self._http_opener.open(request, timeout=timeout)
        opener = self._https_opener
        if opener is None:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=certifi.where())
            opener = urllib.request.build_opener(
                _NoRedirect,
                urllib.request.HTTPSHandler(context=context),
            )
            self._https_opener = opener
        return opener.open(request, timeout=timeout)


_OPENER = _ProviderCheckOpener()


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _is_loopback(parsed: urllib.parse.SplitResult) -> bool:
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_endpoint(value: str) -> str:
    """Validate and normalize a user-supplied HTTP(S) API root."""
    if not isinstance(value, str) or not value or len(value) > 2048 or _has_controls(value):
        raise ValueError("invalid endpoint")
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid endpoint") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and port == 0)
    ):
        raise ValueError("invalid endpoint")
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if not _DNS_HOST.fullmatch(hostname):
            raise ValueError("invalid endpoint")
    if parsed.scheme.lower() == "http" and not _is_loopback(parsed):
        raise ValueError("unencrypted remote endpoint")
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _secret(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not isinstance(value, str):
        raise _DiscoveryError("The credential value is invalid.")
    value = value.strip()
    if _has_controls(value):
        raise _DiscoveryError("The credential contains unsupported control characters.")
    return value


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _open_json(url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise _DiscoveryError("The configured endpoint is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _has_controls(url)
        or (parsed.scheme == "http" and not _is_loopback(parsed))
    ):
        raise _DiscoveryError("The configured endpoint is invalid or unsafe.")
    clean_headers = {"Accept": "application/json", "User-Agent": "Callimachus-Provider-Check/1"}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or _has_controls(name) or _has_controls(value):
            raise _DiscoveryError("The request configuration is invalid.")
        clean_headers[name] = value
    request = urllib.request.Request(url, headers=clean_headers, method="GET")
    def connection_message(timed_out: bool = False) -> str:
        if _is_loopback(parsed):
            endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            failure = "timed out" if timed_out else "could not be reached"
            return f"The local service at {endpoint} {failure}. Ensure it is running and check its address."
        return "The provider request timed out." if timed_out else "The provider could not be reached."
    try:
        with _OPENER.open(request, timeout=_TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > _MAX_BODY:
                raise _DiscoveryError("The provider response exceeded the safe size limit.")
            body = response.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise _DiscoveryError("The provider redirected the request; redirects were not followed.", exc.code) from None
        if exc.code == 401:
            raise _DiscoveryError("The API key was rejected or has expired.", 401) from None
        if exc.code == 403:
            raise _DiscoveryError("The API key lacks permission to list models.", 403) from None
        if exc.code in {404, 405}:
            raise _DiscoveryError("The provider does not expose a compatible model catalog.", exc.code) from None
        raise _DiscoveryError(f"The provider returned HTTP {int(exc.code)}.", int(exc.code)) from None
    except (TimeoutError, socket.timeout):
        raise _DiscoveryError(connection_message(True)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise _DiscoveryError(connection_message(True)) from None
        raise _DiscoveryError(connection_message()) from None
    except OSError:
        raise _DiscoveryError(connection_message()) from None
    if len(body) > _MAX_BODY:
        raise _DiscoveryError("The provider response exceeded the safe size limit.")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _DiscoveryError("The provider returned malformed model catalog data.") from None
    if not isinstance(value, dict):
        raise _DiscoveryError("The provider returned malformed model catalog data.")
    return value


def _model_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 200 or not _MODEL_ID.fullmatch(value):
        return None
    return value


def _models_from_rows(rows: object, key: str) -> list[str]:
    if not isinstance(rows, list):
        raise _DiscoveryError("The provider returned malformed model catalog data.")
    if len(rows) > _MAX_MODELS:
        raise _DiscoveryError("The provider model list exceeded the safe limit.")
    models: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise _DiscoveryError("The provider returned malformed model catalog data.")
        model = _model_id(row.get(key))
        if model is None:
            # Ignore unsafe records, but malformed records are not treated as usable models.
            continue
        if model not in models:
            models.append(model)
    if not models:
        raise _DiscoveryError("The provider returned an empty model list.")
    return models


def _base(environ: Mapping[str, str], name: str, default: str) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        return default
    try:
        return normalize_endpoint(value)
    except ValueError:
        raise _DiscoveryError("The configured endpoint is invalid or unsafe.") from None


def _openai_models(base: str, key: str) -> list[str]:
    url = base.rstrip("/") + "/models"
    models: list[str] = []
    for page in range(_MAX_PAGES):
        data = _open_json(url, {"Authorization": f"Bearer {key}"})
        if not isinstance(data.get("data"), list):
            raise _DiscoveryError("The provider returned malformed model catalog data.")
        rows = _models_from_rows(data["data"], "id") if data["data"] else []
        for model in rows:
            if model not in models:
                models.append(model)
        if len(models) > _MAX_MODELS:
            raise _DiscoveryError("The provider model list exceeded the safe limit.")
        if data.get("has_more") is not True:
            break
        last = data.get("last_id") or data.get("after")
        if not isinstance(last, str) or not _model_id(last):
            raise _DiscoveryError("The provider returned malformed pagination data.")
        if page + 1 == _MAX_PAGES:
            raise _DiscoveryError("The provider model list exceeded the safe page limit.")
        url = base.rstrip("/") + "/models?after=" + urllib.parse.quote(last, safe="")
    if not models:
        raise _DiscoveryError("The provider returned an empty model list.")
    return models


def _xai_models(key: str) -> list[str]:
    data = _open_json(
        "https://api.x.ai/v1/language-models",
        {"Authorization": f"Bearer {key}"},
    )
    return _models_from_rows(data.get("models"), "id")


def _anthropic_models(base: str, key: str) -> list[str]:
    models: list[str] = []
    url = base.rstrip("/") + "/models?limit=100"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    for page in range(_MAX_PAGES):
        data = _open_json(url, headers)
        if not isinstance(data.get("data"), list):
            raise _DiscoveryError("The provider returned malformed model catalog data.")
        rows = _models_from_rows(data["data"], "id") if data["data"] else []
        for model in rows:
            if model not in models:
                models.append(model)
        if len(models) > _MAX_MODELS:
            raise _DiscoveryError("The provider model list exceeded the safe limit.")
        if data.get("has_more") is not True:
            break
        last = data.get("last_id")
        if not isinstance(last, str) or not _model_id(last):
            raise _DiscoveryError("The provider returned malformed pagination data.")
        if page + 1 == _MAX_PAGES:
            raise _DiscoveryError("The provider model list exceeded the safe page limit.")
        url = base.rstrip("/") + "/models?limit=100&after_id=" + urllib.parse.quote(last, safe="")
    if not models:
        raise _DiscoveryError("The provider returned an empty model list.")
    return models


def _gemini_models(key: str) -> list[str]:
    data = _open_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        {"x-goog-api-key": key},
    )
    rows = data.get("models")
    if not isinstance(rows, list):
        raise _DiscoveryError("The provider returned malformed model catalog data.")
    models: list[str] = []
    for row in rows[:_MAX_MODELS + 1]:
        if not isinstance(row, dict):
            raise _DiscoveryError("The provider returned malformed model catalog data.")
        methods = row.get("supportedGenerationMethods")
        if not isinstance(methods, list):
            continue
        if "generateContent" not in methods:
            continue
        name = _model_id(row.get("name"))
        if name and name not in models:
            models.append(name if name.startswith("models/") else "models/" + name)
    if len(rows) > _MAX_MODELS:
        raise _DiscoveryError("The provider model list exceeded the safe limit.")
    if not models:
        raise _DiscoveryError("The provider returned an empty model list.")
    return models


def _typesafe_models(key: str) -> list[str]:
    data = _open_json(
        "https://api.typesafe.ai/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    return _models_from_rows(data.get("models"), "name")


def _ollama_models(base: str, key: str) -> list[str]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _open_json(base.rstrip("/") + "/api/tags", headers)
    return _models_from_rows(data.get("models"), "name")


def _freetoken_models(base: str, key: str) -> list[str]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _open_json(base.rstrip("/") + "/v1/models", headers)
    return _models_from_rows(data.get("data"), "id")


def _probe(provider: str, environ: Mapping[str, str]) -> tuple[list[str], str, str | None, dict[str, str]]:
    key_env = _API_KEYS.get(provider)
    key = _secret(environ, key_env) if key_env else ""
    settings: dict[str, str] = {}
    backend = provider
    if provider == "openai":
        base = str(environ.get("OPENAI_BASE_URL") or "").strip()
        if base:
            normalized = _base(environ, "OPENAI_BASE_URL", "")
            parsed = urllib.parse.urlsplit(normalized)
            if parsed.hostname != "api.openai.com":
                if not key:
                    raise _DiscoveryError("Enter an API key to check this compatible endpoint.")
                backend = "openai_compatible"
                models = _openai_models(normalized, key)
                settings["base_url"] = normalized
                return models, backend, "OPENAI_MODEL", settings
        if not key:
            raise _DiscoveryError("Enter an API key to check this provider.")
        return _openai_models("https://api.openai.com/v1", key), backend, "OPENAI_MODEL", settings
    if provider == "openai_compatible":
        base = _base(environ, "OPENAI_BASE_URL", "")
        if not base or not key:
            raise _DiscoveryError("Enter an API key and base URL to check this endpoint.")
        models = _openai_models(base, key)
        settings["base_url"] = base
        return models, provider, "OPENAI_MODEL", settings
    if provider == "anthropic":
        if not key:
            raise _DiscoveryError("Enter an Anthropic API key to check this provider.")
        base = _base(environ, "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        models = _anthropic_models(base, key)
        if str(environ.get("ANTHROPIC_BASE_URL") or "").strip():
            settings["base_url"] = base
        return models, provider, "ANTHROPIC_MODEL", settings
    if provider == "gemini":
        if not key:
            raise _DiscoveryError("Enter an API key to check this provider.")
        return _gemini_models(key), provider, "GEMINI_MODEL", settings
    if provider == "openrouter":
        if not key:
            raise _DiscoveryError("Enter an API key to check this provider.")
        auth = _open_json("https://openrouter.ai/api/v1/auth/key", {"Authorization": f"Bearer {key}"})
        if not isinstance(auth.get("data"), dict):
            raise _DiscoveryError("The provider returned malformed authentication data.")
        return _openai_models("https://openrouter.ai/api/v1", key), provider, "OPENROUTER_MODEL", settings
    if provider == "typesafe":
        if not key:
            raise _DiscoveryError("Enter an API key to check this provider.")
        return _typesafe_models(key), provider, "TYPESAFE_MODEL", settings
    if provider == "xai":
        if not key:
            raise _DiscoveryError("Enter an xAI API key to check this provider.")
        return _xai_models(key), provider, _MODEL_ENVS[provider], settings
    fixed_bases = {
        "glm": "https://open.bigmodel.cn/api/paas/v4",
        "deepseek": "https://api.deepseek.com",
        "groq": "https://api.groq.com/openai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "opencode": "https://opencode.ai/zen/v1",
    }
    if provider in fixed_bases:
        if not key:
            raise _DiscoveryError("Enter an API key to check this provider.")
        return _openai_models(fixed_bases[provider], key), provider, _MODEL_ENVS[provider], settings
    if provider == "ollama":
        from core.verify.backends.ollama import normalize_ollama_host
        try:
            host = normalize_endpoint(normalize_ollama_host(str(environ.get("OLLAMA_HOST") or ("https://ollama.com" if key else "http://127.0.0.1:11434"))))
        except ValueError:
            raise _DiscoveryError("The configured Ollama host is invalid or unsafe.") from None
        if not key and not _is_loopback(urllib.parse.urlsplit(host)):
            raise _DiscoveryError("A key is required to check a non-local Ollama host.")
        backend = "ollama" if key else "ollama_local"
        return _ollama_models(host, key), backend, "OLLAMA_MODEL", {"host": host}
    if provider == "freetoken":
        host = _base(environ, "FREETOKEN_HOST", "http://127.0.0.1:1919")
        if not key and not _is_loopback(urllib.parse.urlsplit(host)):
            raise _DiscoveryError("A key is required to check a non-local FreeToken host.")
        backend = "freetoken_authenticated" if key else "freetoken"
        return _freetoken_models(host, key), backend, "FREETOKEN_MODEL", {"host": host}
    raise _DiscoveryError("Model catalog checks are not supported for this provider.")


def _installed(provider: str, environ: Mapping[str, str] | None = None) -> bool:
    commands = {
        "claude_cli": ("claude",),
        "codex_cli": ("codex",),
        "gemini_cli": ("gemini",),
        "antigravity_cli": ("agy",),
        "antigravity": ("agy", "antigravity", "antigravity-ide"),
        "opencode_cli": ("opencode",),
        "cursor_cli": ("cursor-agent", "agent"),
        "copilot_cli": ("copilot",),
        "ollama": ("ollama",),
        "freetoken": ("ft",),
    }.get(provider, ())
    return any(find_cli(command, environ) is not None for command in commands)


def _catalog_result(
    provider: str,
    environ: Mapping[str, str],
    *,
    ok: bool,
    models: list[str] | None = None,
    model_env: str | None = None,
    backend: str | None = None,
    message: str,
    status_code: int | None = None,
    installed: bool | None = None,
    authenticated: bool | None = None,
    settings: Mapping[str, str] | None = None,
) -> dict[str, object]:
    models = models or []
    model_env = model_env or _MODEL_ENVS.get(backend or provider, "CITATION_VERIFIER_MODEL")
    options: list[dict[str, object]] = []
    if ok and models:
        values = dict(environ)
        values[model_env] = ",".join(models)
        if backend == "openai_compatible" and settings and settings.get("base_url"):
            values["OPENAI_BASE_URL"] = settings["base_url"]
        if backend in {"ollama", "ollama_local"} and settings and settings.get("host"):
            values["OLLAMA_HOST"] = settings["host"]
        if backend in {"freetoken", "freetoken_authenticated"} and settings and settings.get("host"):
            values["FREETOKEN_HOST"] = settings["host"]
        if provider == "anthropic" and settings and settings.get("base_url"):
            values["ANTHROPIC_BASE_URL"] = settings["base_url"]
        try:
            from core.app.settings.config import discover_verify_backend_options
            configured_options = {
                str(item.get("selector") or ""): item
                for item in discover_verify_backend_options(environ)
                if item.get("backend") == backend
            }
            available = discover_verify_backend_options(values)
            for item in available:
                if item.get("backend") != backend or item.get("model") not in models:
                    continue
                selector = str(item.get("selector") or "")
                configured = configured_options.get(selector) or {}
                option = {key: item[key] for key in (
                    "selector", "backend", "model", "model_env", "label",
                    "configured",
                )}
                # The expanded model list describes catalog availability. Keep
                # role-selection flags from the configuration that existed
                # before this check added the newly discovered models.
                option["jury1_selected"] = bool(configured.get("jury1_selected"))
                option["jury2_selected"] = bool(configured.get("jury2_selected"))
                options.append(option)
        except (ImportError, KeyError, TypeError, ValueError):
            options = []
    return {
        "provider": provider,
        "ok": bool(ok),
        "models": list(models),
        "model_env": model_env,
        "options": options,
        "message": str(message),
        **({"status_code": int(status_code)} if status_code is not None else {}),
        **({"installed": bool(installed)} if installed is not None else {}),
        **({"authenticated": bool(authenticated)} if authenticated is not None else {}),
        "settings": {str(key): str(value) for key, value in (settings or {}).items()},
    }


def check_provider(provider: str, environ: Mapping[str, str]) -> dict[str, object]:
    """Check one provider using only a bounded GET or documented local status."""
    aliases = {"ollama_local": "ollama", "openai_compatible": "openai_compatible"}
    provider = aliases.get(str(provider).strip().lower(), str(provider).strip().lower())
    pending_native = {
        "cursor_cli": "Cursor CLI needs a qualified tool-free launch profile; Ask mode still exposes tools.",
        "copilot_cli": "GitHub Copilot CLI isolation is not qualified for native-account juries; an empty CLI tool option still exposes tools.",
    }
    if provider == "host":
        command = str(environ.get("CITATION_VERIFIER_LLM_HOST_COMMAND") or "").strip()
        raw_models = str(environ.get("CITATION_VERIFIER_MODEL") or "").strip()
        try:
            from core.verify.backends.host import parse_host_command
            args = parse_host_command(command)
        except ValueError:
            args = []
        if not args:
            return _catalog_result(provider, environ, ok=False,
                message="Enter a valid Host command and model IDs.")
        executable = args[0]
        if shutil.which(executable, path=environ.get("PATH")) is None:
            return _catalog_result(provider, environ, ok=False,
                message="The configured Host executable was not found.")
        models = [item.strip() for item in raw_models.split(",")]
        if not raw_models or len(models) > _MAX_MODELS or any(
            not model or not _model_id(model) for model in models
        ):
            return _catalog_result(provider, environ, ok=False,
                message="Enter valid comma-separated Host model IDs.")
        return _catalog_result(
            provider, environ, ok=True, backend="host", models=list(dict.fromkeys(models)),
            model_env="CITATION_VERIFIER_MODEL",
            message="Command and model IDs validated. The response is checked before a run.",
        )
    if provider in pending_native:
        return _catalog_result(
            provider, environ, ok=False, installed=_installed(provider, environ), authenticated=None,
            message=pending_native[provider],
        )
    if provider in {"claude_cli", "codex_cli", "gemini_cli", "antigravity", "antigravity_cli", "opencode_cli"}:
        backend = "antigravity_cli" if provider == "antigravity" else provider
        try:
            from core.app.providers.cli_discovery import cli_catalog
            native = cli_catalog(backend, environ)
            native_models = native.get("models", [])
            if not isinstance(native_models, list):
                native_models = []
            models = [model for item in native_models if (model := _model_id(item))]
            ok = native.get("ok") is True and bool(models)
            return _catalog_result(
                provider, environ, ok=ok, models=models, backend=backend,
                model_env=_MODEL_ENVS[backend],
                installed=native.get("installed") is True,
                authenticated=native.get("authenticated") if isinstance(native.get("authenticated"), bool) else None,
                message=str(native.get("message") or "The CLI provider check could not be completed."),
            )
        except Exception:
            return _catalog_result(
                provider, environ, ok=False, installed=_installed(provider, environ),
                authenticated=None, message="The CLI provider check could not be completed safely.",
            )
    try:
        models, backend, model_env, settings = _probe(provider, environ)
        return _catalog_result(
            provider, environ, ok=True, models=models, backend=backend,
            model_env=model_env, message=f"Found {len(models)} available model(s).",
            installed=_installed(provider, environ) if provider in {"ollama", "freetoken"} else None,
            authenticated=True if provider not in {"ollama", "freetoken"} or _secret(environ, _API_KEYS[provider]) else False,
            settings=settings,
        )
    except _DiscoveryError as exc:
        return _catalog_result(
            provider, environ, ok=False, message=str(exc), status_code=exc.status_code,
            installed=_installed(provider, environ) if provider in {"ollama", "freetoken"} else None,
            authenticated=False if exc.status_code in {401, 403} else None,
        )
    except Exception:
        # Error text and exception values may include credentials or provider bodies.
        return _catalog_result(provider, environ, ok=False, message="The provider check could not be completed safely.")


def discover_local_providers(environ: Mapping[str, str]) -> dict[str, list[dict[str, object]]]:
    """Report local CLI installs and probe only configured loopback services."""
    providers: list[dict[str, object]] = []
    for provider in ("claude_cli", "codex_cli", "gemini_cli", "antigravity",
                     "opencode_cli", "cursor_cli", "copilot_cli"):
        providers.append(check_provider(provider, environ))
    for provider, env_name, default in (
        ("ollama", "OLLAMA_HOST", "http://127.0.0.1:11434"),
        ("freetoken", "FREETOKEN_HOST", "http://127.0.0.1:1919"),
    ):
        configured_host = str(environ.get(env_name) or "").strip()
        try:
            if provider == "ollama" and configured_host:
                from core.verify.backends.ollama import normalize_ollama_host
                configured_host = normalize_ollama_host(configured_host)
            host = normalize_endpoint(configured_host or default)
        except ValueError:
            providers.append(_catalog_result(
                provider, environ, ok=False, installed=_installed(provider, environ),
                message="The configured local host is invalid or unsafe.",
            ))
            continue
        if not _is_loopback(urllib.parse.urlsplit(host)) or (environ.get(_API_KEYS[provider]) or "").strip():
            providers.append(_catalog_result(
                provider, environ, ok=False, installed=_installed(provider, environ),
                message="Preflight checks only probe keyless loopback services.",
                settings={"host": host},
            ))
            continue
        probe_values = dict(environ)
        probe_values[env_name] = host
        providers.append(check_provider(provider, probe_values))
    return {"providers": providers}


def catalog_verify_preview(
    environ: Mapping[str, str],
    catalogs: Mapping[str, Mapping[str, object]],
    selected: Mapping[str, Any],
    *,
    jury2_level: str,
) -> dict[str, object]:
    """Re-export the validated, non-persisting Verify catalog preview helper."""
    from core.app.settings.config import catalog_verify_preview as preview

    return preview(environ, catalogs, selected, jury2_level=jury2_level)
