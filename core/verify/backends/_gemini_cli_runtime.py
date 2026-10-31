# core/verify/backends/_gemini_cli_runtime.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, isolated subprocess support for the native Gemini CLI backend."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping

from core.app.providers.cli_discovery import _CatalogError, _Session
from core.verify.backends.errors import (
    AuthError,
    ConfigError,
    RateLimitError,
    TransientError,
    cli_timeout_error,
)


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}\Z")
_AUTO_MODEL_ALIASES = {"auto", "default", "latest", "stable"}
_OAUTH_PERSONAL = "oauth-personal"
_CATALOG_TIMEOUT = 30.0
_API_AND_VERTEX_ENV = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_REGION",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_GEMINI_BASE_URL",
)
_TELEMETRY_ENV = (
    "GEMINI_TELEMETRY_ENABLED",
    "GEMINI_TELEMETRY_TARGET",
    "GEMINI_TELEMETRY_OUTFILE",
)


@dataclass(frozen=True)
class _Runtime:
    cwd: Path
    settings_path: Path
    environ: dict[str, str]


def _settings(context_name: str) -> dict:
    """Return system overrides for a one-turn, no-tool native CLI session."""
    return {
        "general": {
            "maxAttempts": 1,
            "plan": {"enabled": False, "modelRouting": False},
        },
        "model": {"maxSessionTurns": 1},
        "security": {
            "auth": {
                "selectedType": _OAUTH_PERSONAL,
                "enforcedType": _OAUTH_PERSONAL,
            },
        },
        "tools": {"core": [], "discoveryCommand": "", "callCommand": ""},
        "admin": {
            "mcp": {"enabled": False, "config": {}, "requiredConfig": {}},
            "extensions": {"enabled": False},
            "skills": {"enabled": False},
            "secureModeEnabled": True,
        },
        "mcp": {"serverCommand": "", "allowed": [], "excluded": []},
        "mcpServers": {},
        "hooksConfig": {"enabled": False},
        "skills": {"enabled": False},
        "experimental": {
            "enableAgents": False,
            "autoMemory": False,
            "extensionManagement": False,
            "extensionConfig": False,
            "extensionRegistry": False,
            "extensionReloading": False,
            "dynamicModelConfiguration": False,
        },
        "context": {
            "fileName": context_name,
            "includeDirectoryTree": False,
            "includeDirectories": [],
            "loadMemoryFromIncludeDirectories": False,
            "memoryBoundaryMarkers": [],
        },
        "telemetry": {"enabled": False, "logPrompts": False},
        "privacy": {"usageStatisticsEnabled": False},
    }


@contextmanager
def isolated_runtime(
    environ: Mapping[str, str] | None = None, *, temp_root: Path | str | None = None
):
    """Create a fresh cwd and highest-precedence system settings file.

    OAuth state remains in Gemini CLI's native home. The environment copy is
    changed only for the child process, and API-key/Vertex routes are disabled.
    """
    child_env = dict(os.environ)
    if environ is not None:
        child_env.update({str(key): str(value) for key, value in environ.items()})
    for name in _API_AND_VERTEX_ENV:
        child_env[name] = ""
    for name in _TELEMETRY_ENV:
        child_env[name] = ""
    # Gemini CLI otherwise imports IDE workspace folders into its context.
    child_env["GEMINI_CLI_IDE_WORKSPACE_PATH"] = ""

    temp_dir_parent = None
    if temp_root is not None:
        temp_dir_parent = Path(temp_root).resolve(strict=True)
        if not temp_dir_parent.is_dir():
            raise ValueError("Gemini CLI temporary root must be a directory.")

    with tempfile.TemporaryDirectory(
        prefix="callimachus-gemini-",
        dir=str(temp_dir_parent) if temp_dir_parent is not None else None,
    ) as root_name:
        root = Path(root_name)
        cwd = root / "workspace"
        cwd.mkdir()
        settings_path = root / "system-settings.json"
        context_name = f"CALLIMACHUS_NO_CONTEXT_{uuid.uuid4().hex}.md"
        settings_path.write_text(
            json.dumps(_settings(context_name), separators=(",", ":")),
            encoding="utf-8",
        )
        child_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(settings_path)
        yield _Runtime(cwd=cwd, settings_path=settings_path, environ=child_env)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        raise ConfigError(
            "Gemini CLI returned invalid JSON output.", backend="gemini_cli"
        ) from None
    if not isinstance(value, dict):
        raise ConfigError(
            "Gemini CLI returned an invalid JSON response.", backend="gemini_cli"
        )
    return value


def _error_text(error: object) -> str:
    if isinstance(error, str):
        return error.casefold()
    if not isinstance(error, dict):
        return ""
    values = (error.get("type"), error.get("message"))
    return " ".join(value for value in values if isinstance(value, str)).casefold()


def _raise_cli_error(error: object, returncode: int | None = None) -> None:
    text = _error_text(error)
    if any(token in text for token in (
        "authentication", "authrequired", "credential", "not logged in",
        "login required", "please log in", "sign in", "unauthorized", "401",
    )):
        raise AuthError(
            "Gemini CLI is not signed in with its native Google login.",
            backend="gemini_cli",
        )
    if "429" in text or "rate limit" in text or "resource exhausted" in text:
        raise RateLimitError(
            "Gemini CLI was rate limited.", backend="gemini_cli"
        )
    if any(token in text for token in ("badrequest", "invalid model", "unknown model", "unsupported model", "invalid_request", "400", "404")):
        raise ConfigError(
            "Gemini CLI rejected the pinned model or request.",
            backend="gemini_cli",
        )
    if returncode is not None:
        raise TransientError(
            f"Gemini CLI exited with status {returncode}.", backend="gemini_cli"
        )
    raise TransientError(
        "Gemini CLI could not complete the request.", backend="gemini_cli"
    )


def _reported_models(stats: dict) -> set[str] | None:
    models = stats.get("models")
    if models is not None:
        if not isinstance(models, dict):
            raise ConfigError(
                "Gemini CLI returned malformed model usage statistics.",
                backend="gemini_cli",
            )
        if models:
            return {key for key in models if isinstance(key, str)}
    model = stats.get("model")
    if isinstance(model, str) and model:
        return {model}
    if isinstance(model, dict):
        for key in ("modelId", "model", "id", "name"):
            value = model.get(key)
            if isinstance(value, str) and value:
                return {value}
    return None


def _validate_success(payload: dict, model: str) -> str:
    if payload.get("error") is not None:
        _raise_cli_error(payload.get("error"))
    response = payload.get("response")
    if not isinstance(response, str) or not response.strip():
        raise TransientError(
            "Gemini CLI returned an empty response.", backend="gemini_cli"
        )

    stats = payload.get("stats")
    if stats is not None:
        if not isinstance(stats, dict):
            raise ConfigError(
                "Gemini CLI returned malformed usage statistics.",
                backend="gemini_cli",
            )
        tools = stats.get("tools")
        if tools is not None:
            if not isinstance(tools, dict):
                raise ConfigError(
                    "Gemini CLI returned malformed tool usage statistics.",
                    backend="gemini_cli",
                )
            calls = tools.get("totalCalls")
            if calls is not None:
                if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
                    raise ConfigError(
                        "Gemini CLI returned malformed tool usage statistics.",
                        backend="gemini_cli",
                    )
                if calls > 0:
                    raise ConfigError(
                        "Gemini CLI attempted a tool call; tool use is disabled.",
                        backend="gemini_cli",
                    )
        used_models = _reported_models(stats)
        if used_models and used_models != {model}:
            raise ConfigError(
                "Gemini CLI used a model other than the pinned model.",
                backend="gemini_cli",
            )
    return response.strip()


def run_gemini_prompt(
    path: str,
    system: str,
    user: str,
    model: str,
    *,
    timeout: float,
    environ: Mapping[str, str] | None = None,
    temp_root: Path | str | None = None,
) -> str:
    """Run one JSON-mode prompt, sending its full body through stdin."""
    if (
        not isinstance(model, str)
        or not _MODEL_ID.fullmatch(model)
        or model.casefold() in _AUTO_MODEL_ALIASES
        or model.casefold().startswith("auto-")
        or model.casefold().endswith("-latest")
    ):
        raise ConfigError(
            "Gemini CLI requires a pinned model id; automatic aliases are not allowed.",
            backend="gemini_cli",
        )
    prompt = f"{system}\n\n{user}"
    session = None
    try:
        with isolated_runtime(environ, temp_root=temp_root) as runtime:
            with _Session(
                [path, "-m", model, "--output-format", "json"],
                runtime.environ,
                cwd=runtime.cwd,
                timeout=timeout,
            ) as session:
                session.send_text(prompt)
                # Gemini's non-interactive stdin reader consumes until EOF.
                try:
                    session.process.stdin.close()
                except BrokenPipeError:
                    # A fast auth/config rejection may close stdin before the
                    # JSON error or sanitized stderr has been drained.
                    pass
                raw = session.text_document()
                returncode = session.process.wait(
                    timeout=max(0.01, session.deadline - time.monotonic())
                )
            diagnostics = session.diagnostics
    except _CatalogError as exc:
        message = str(exc).casefold()
        if "timed out" in message:
            raise cli_timeout_error("gemini_cli", timeout) from None
        if "request exceeded the safe size limit" in message:
            raise ConfigError(
                "The Gemini CLI prompt exceeded the safe size limit.",
                backend="gemini_cli",
            ) from None
        if "size limit" in message:
            raise ConfigError(
                "Gemini CLI output exceeded the safe size limit.",
                backend="gemini_cli",
            ) from None
        if session is not None and _auth_error(session.diagnostics):
            raise AuthError(
                "Gemini CLI is not signed in with its native Google login.",
                backend="gemini_cli",
            ) from None
        raise TransientError(
            "Gemini CLI closed before returning a response.", backend="gemini_cli"
        ) from None
    except subprocess.TimeoutExpired:
        raise cli_timeout_error("gemini_cli", timeout) from None
    except OSError:
        raise ConfigError(
            "Gemini CLI could not be started.", backend="gemini_cli"
        ) from None

    try:
        payload = _json_object(raw)
    except ConfigError:
        if diagnostics and _auth_error(diagnostics):
            raise AuthError(
                "Gemini CLI is not signed in with its native Google login.",
                backend="gemini_cli",
            ) from None
        raise
    if returncode != 0:
        error = payload.get("error")
        if error is None and diagnostics:
            error = {"message": diagnostics}
        _raise_cli_error(error, returncode)
    return _validate_success(payload, model)


class _CatalogFailure(Exception):
    def __init__(self, message: str, *, authenticated: bool | None = None):
        super().__init__(message)
        self.authenticated = authenticated


def _auth_error(value: object) -> bool:
    if isinstance(value, Mapping):
        text = " ".join(
            str(part) for part in value.values() if isinstance(part, str)
        ).casefold()
    else:
        text = value.casefold() if isinstance(value, str) else ""
    return any(
        token in text
        for token in (
            "auth", "credential", "not logged in", "login required",
            "sign in", "unauthorized", "401",
        )
    )


def _rpc(session: _Session, request_id: int, method: str, params: dict) -> dict:
    session.send({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    })
    for _ in range(64):
        frame = session.frame()
        if frame.get("id") != request_id:
            # Never answer an ACP server request (permission, authentication,
            # filesystem, terminal or tool interaction) from this catalog probe.
            if "id" in frame or "method" in frame:
                raise _CatalogFailure(
                    "Gemini CLI requested interaction during model discovery."
                )
            continue
        if "error" in frame:
            error = frame.get("error")
            authenticated = False if _auth_error(error) else None
            raise _CatalogFailure(
                "Sign in to Gemini CLI with Google OAuth before checking models."
                if authenticated is False
                else "Gemini CLI rejected its model catalog check.",
                authenticated=authenticated,
            )
        result = frame.get("result")
        if not isinstance(result, dict):
            raise _CatalogFailure("Gemini CLI returned an invalid model catalog response.")
        return result
    raise _CatalogFailure("Gemini CLI sent too many model catalog notifications.")


def _catalog_models(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise _CatalogFailure("Gemini CLI did not return a usable model catalog.")
    models = []
    for row in value:
        if not isinstance(row, dict):
            raise _CatalogFailure("Gemini CLI returned a malformed model catalog.")
        model = row.get("modelId")
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
            raise _CatalogFailure("Gemini CLI returned a malformed model catalog.")
        lowered = model.casefold()
        if lowered in _AUTO_MODEL_ALIASES or lowered.startswith("auto-") or lowered.endswith("-latest"):
            continue
        if model not in models:
            models.append(model)
    if not models:
        raise _CatalogFailure("Gemini CLI did not return any pinned model ids.")
    return models


def check_catalog(
    path: str, environ: Mapping[str, str], *, temp_root: Path | str | None = None
) -> dict:
    """Check native Google OAuth and model ids without submitting a prompt."""
    result = {
        "ok": False,
        "models": [],
        "authenticated": None,
        "message": "Gemini CLI model catalog could not be checked.",
    }
    session = None
    try:
        with isolated_runtime(environ, temp_root=temp_root) as runtime:
            with _Session(
                [str(path), "--acp"],
                runtime.environ,
                cwd=runtime.cwd,
                timeout=_CATALOG_TIMEOUT,
            ) as session:
                initialize = _rpc(
                    session,
                    1,
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientInfo": {"name": "callimachus", "version": "1"},
                        "clientCapabilities": {},
                    },
                )
                if initialize.get("protocolVersion") != 1:
                    raise _CatalogFailure("Gemini CLI returned an unsupported ACP protocol version.")
                auth_methods = initialize.get("authMethods")
                if not isinstance(auth_methods, list) or not any(
                    isinstance(method, dict) and method.get("id") == _OAUTH_PERSONAL
                    for method in auth_methods
                ):
                    raise _CatalogFailure(
                        "This Gemini CLI does not expose native Google OAuth login."
                    )
                catalog = _rpc(
                    session,
                    2,
                    "session/new",
                    {"cwd": str(runtime.cwd), "mcpServers": []},
                )
                result["authenticated"] = True
                models_state = catalog.get("models")
                if not isinstance(models_state, dict):
                    raise _CatalogFailure("Gemini CLI returned no ACP model state.")
                models = _catalog_models(models_state.get("availableModels"))
                result.update(
                    ok=True,
                    models=models,
                    message="Gemini CLI Google OAuth and native model catalog detected.",
                )
                return result
    except _CatalogFailure as exc:
        result["authenticated"] = exc.authenticated
        result["message"] = str(exc)
    except _CatalogError as exc:
        message = str(exc).casefold()
        if session is not None and _auth_error(session.diagnostics):
            result["authenticated"] = False
            result["message"] = "Sign in to Gemini CLI with Google OAuth before checking models."
        else:
            result["message"] = (
                "Gemini CLI model catalog check timed out."
                if "timed out" in message
                else "Gemini CLI returned an invalid model catalog response."
            )
    except (OSError, subprocess.SubprocessError, ValueError):
        result["message"] = "Gemini CLI model catalog could not be checked."
    return result
