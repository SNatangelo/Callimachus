# core/verify/backends/antigravity_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native Antigravity CLI backend with a fail-closed, no-tools jury agent."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping

from core.app.providers.cli_discovery import _CatalogError, _Session
from core.infra._local_executables import find_cli
from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import (
    AuthError,
    BackendTimeoutError,
    ConfigError,
    RateLimitError,
    TransientError,
)


_BACKEND = "antigravity_cli"
_AGENT = "callimachus-jury"
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_PLUGIN_ENTRIES = 256
_MAX_MODELS = 500
_MAX_PROMPT_BYTES = 2 * 1024 * 1024
_CATALOG_TIMEOUT = 8.0
_MODEL_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_MODEL_ROW = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})\s{2,}(.+?)\s*$")
_MAX_SETTINGS_NODES = 4096
_MAX_SETTINGS_DEPTH = 32
_HOOK_EVENTS = frozenset({
    "SessionStart", "PreToolUse", "PostToolUse", "PreInvocation",
    "PostInvocation", "Stop",
})
_HOOK_FIELDS = _HOOK_EVENTS | {"enabled"}
_HOOK_EVENT_NAMES = frozenset(event.casefold() for event in _HOOK_EVENTS)
_AUTH_HINTS = (
    "sign in", "signed in", "log in", "logged in", "login", "authentication",
    "authenticate", "auth required", "unauthorized", "credential", "session expired",
)
_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "quota exceeded", "resource exhausted", "too many requests", "429")
_CONFIG_HINTS = ("model not found", "unsupported model", "unknown model", "invalid model")


def _resolve_cli() -> str | None:
    """Resolve only the Antigravity CLI executable, never the IDE command."""
    return find_cli("agy")


def _available() -> bool:
    return _resolve_cli() is not None


def _effective_home(environ: Mapping[str, str]) -> Path:
    raw = environ.get("USERPROFILE") if os.name == "nt" else environ.get("HOME")
    raw = raw or environ.get("HOME") or environ.get("USERPROFILE")
    raw = raw or os.environ.get("USERPROFILE") or os.environ.get("HOME")
    return Path(raw).expanduser() if raw else Path.home()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _CatalogError("A hook configuration contains duplicate keys.")
        result[key] = value
    return result


def _read_config(path: Path) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise _CatalogError("A documented hook configuration could not be checked.") from None
    if path.is_symlink() or not path.is_file() or info.st_size > _MAX_CONFIG_BYTES:
        raise _CatalogError("A documented hook configuration is ambiguous or oversized.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise _CatalogError("A documented hook configuration is oversized.")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except _CatalogError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise _CatalogError("A documented hook configuration is malformed or unreadable.") from None
    if not isinstance(value, dict):
        raise _CatalogError("A documented hook configuration has an unknown shape.")
    return value


def _hook_definition_present(definitions: object) -> bool:
    if not isinstance(definitions, dict):
        raise _CatalogError("A hook configuration has an unknown shape.")
    for definition in definitions.values():
        if not isinstance(definition, dict) or set(definition) - _HOOK_FIELDS:
            raise _CatalogError("A hook configuration has ambiguous fields.")
        enabled = definition.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _CatalogError("A hook configuration has an ambiguous enabled state.")
        for event in _HOOK_EVENTS:
            handlers = definition.get(event, [])
            if not isinstance(handlers, list):
                raise _CatalogError("A hook configuration has an ambiguous event shape.")
            if enabled and handlers:
                return True
    return False


def _settings_hook_present(settings: dict) -> bool:
    # settings.json does not document a single stable nesting shape. Walk
    # bounded JSON structures and reject any nonempty hook/event subtree.
    pending = [(settings, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > _MAX_SETTINGS_NODES or depth > _MAX_SETTINGS_DEPTH:
            raise _CatalogError("The CLI settings hook configuration is oversized or too deep.")
        if isinstance(value, dict):
            for key, child in value.items():
                hook_field = key.casefold() in _HOOK_EVENT_NAMES or "hook" in key.casefold()
                if hook_field and child not in ({}, [], "", None, False):
                    return True
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return False


def _check_config_file(path: Path, *, settings: bool = False) -> None:
    config = _read_config(path)
    if settings:
        found = _settings_hook_present(config)
    else:
        found = _hook_definition_present(config)
    if found:
        raise _CatalogError("Configured Antigravity lifecycle hooks are not allowed for this backend.")


def _check_empty_plugin_root(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise _CatalogError("A documented plugin location could not be checked.") from None
    if path.is_symlink() or not path.is_dir():
        raise _CatalogError("A documented plugin location is ambiguous.")
    try:
        with os.scandir(path) as entries:
            for index, _entry in enumerate(entries, start=1):
                if index > _MAX_PLUGIN_ENTRIES:
                    raise _CatalogError("A documented plugin location is oversized.")
                # Conservatively reject every installed plugin bundle. Plugin
                # activation and bundled hook discovery are CLI-managed.
                raise _CatalogError("Installed Antigravity plugins are not allowed for this backend.")
    except _CatalogError:
        raise
    except OSError:
        raise _CatalogError("A documented plugin location could not be checked.") from None


def _workspace_ancestors(cwd: Path):
    try:
        current = cwd.resolve()
    except OSError:
        raise _CatalogError("The Antigravity workspace path could not be validated.") from None
    for _ in range(64):
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent
    raise _CatalogError("The Antigravity workspace path is too deep to validate.")


def _check_hook_guard(cwd: Path, environ: Mapping[str, str]) -> None:
    """Reject configured lifecycle hooks/plugins before any ``agy`` process."""
    home = _effective_home(environ)
    for ancestor in _workspace_ancestors(cwd):
        agents = ancestor / ".agents"
        _check_config_file(agents / "hooks.json")
        _check_empty_plugin_root(agents / "plugins")
    _check_config_file(home / ".gemini" / "config" / "hooks.json")
    _check_config_file(
        home / ".gemini" / "antigravity-cli" / "settings.json", settings=True,
    )
    _check_empty_plugin_root(home / ".gemini" / "config" / "plugins")
    _check_empty_plugin_root(home / ".gemini" / "antigravity-cli" / "plugins")


def _require_hook_guard(cwd: Path, environ: Mapping[str, str]) -> None:
    try:
        _check_hook_guard(cwd, environ)
    except _CatalogError as exc:
        raise ConfigError(str(exc), backend=_BACKEND) from None
    except OSError:
        raise ConfigError("Antigravity hook and plugin settings could not be checked safely.", backend=_BACKEND) from None


def _clean_cli_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Keep the CLI on its saved Antigravity account, not API/Vertex modes."""
    clean = dict(environ)
    # _Session merges these overrides with os.environ. Empty values shadow
    # inherited API keys, custom endpoints, and Vertex route selectors without
    # changing the caller's environment or global settings.
    for key in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_VERTEX_AI",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION",
        "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
        "GCLOUD_PROJECT",
    ):
        clean[key] = ""
    return clean


def _parse_model_catalog(output: str) -> list[str]:
    models = []
    for line in output.splitlines():
        row = line.strip()
        if not row:
            continue
        if row.casefold() == "available models:":
            continue
        match = _MODEL_ROW.fullmatch(row)
        if not match:
            raise _CatalogError("The Antigravity model catalog contains an invalid row.")
        slug, label = match.groups()
        if not _MODEL_SLUG.fullmatch(slug) or "-" not in slug or not label.strip():
            raise _CatalogError("The Antigravity model catalog contains an invalid model row.")
        lowered = f"{slug} {label}".casefold()
        normalized = lowered.replace("-", " ").replace("_", " ")
        if any(hint in normalized for hint in (*_AUTH_HINTS, *_RATE_LIMIT_HINTS, *_CONFIG_HINTS)) or normalized.startswith(("model ", "models ", "status ")):
            raise _CatalogError("The Antigravity model catalog contains status text, not model rows.")
        if slug not in models:
            models.append(slug)
        if len(models) > _MAX_MODELS:
            raise _CatalogError("The Antigravity model catalog is oversized.")
    if not models:
        raise _CatalogError("The Antigravity CLI returned no usable model slugs.")
    return models


def check_catalog(path: str, environ: Mapping[str, str]) -> dict:
    """Read real model slugs from ``agy models``; this does not prove login."""
    result = {
        "ok": False, "models": [], "authenticated": None,
        "message": "The Antigravity model catalog could not be checked safely.",
    }
    try:
        if not isinstance(path, str) or not path:
            raise _CatalogError("The Antigravity CLI path is not the supported `agy` executable.")
        executable_name = Path(path).name.casefold()
        if os.name == "nt" and executable_name.endswith((".cmd", ".bat")):
            raise _CatalogError("Windows batch launchers are not supported for the Antigravity CLI.")
        if executable_name not in {"agy", "agy.exe"}:
            raise _CatalogError("The Antigravity CLI path is not the supported `agy` executable.")
        cwd = Path.cwd()
        _check_hook_guard(cwd, environ)
        with _Session(
            [path, "models"], _clean_cli_environment(environ), cwd=cwd,
            timeout=_CATALOG_TIMEOUT,
        ) as session:
            if session.process.stdin is not None:
                session.process.stdin.close()
            output = session.text_document()
            session.process.wait(timeout=max(0.001, session.deadline - time.monotonic()))
            if session.process.returncode != 0:
                raise _CatalogError("The Antigravity model catalog command failed.")
        result.update(
            ok=True,
            models=_parse_model_catalog(output),
            authenticated=None,
            message="Antigravity model catalog detected; signed-in account access is not confirmed.",
        )
    except _CatalogError as exc:
        result["message"] = str(exc)
    except subprocess.TimeoutExpired:
        result["message"] = "The Antigravity model catalog check timed out."
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        result["message"] = "The Antigravity model catalog could not be checked safely."
    return result


def _require_slug(model: str | None) -> str:
    if not isinstance(model, str) or not _MODEL_SLUG.fullmatch(model) or "-" not in model:
        raise ConfigError("Choose a model from the Antigravity CLI catalog.", backend=_BACKEND)
    return model


def _validate_init(event: dict, *, model: str, cwd: Path) -> None:
    payload = event.get("init")
    if event.get("event") != "init" or not isinstance(payload, dict):
        raise ConfigError("Antigravity CLI did not provide a valid startup handshake.", backend=_BACKEND)
    if payload.get("agent") != _AGENT:
        raise ConfigError("Antigravity CLI did not activate the required jury agent.", backend=_BACKEND)
    if payload.get("model") != model:
        raise ConfigError("Antigravity CLI did not activate the pinned model.", backend=_BACKEND)
    if payload.get("tools") != []:
        raise ConfigError("Antigravity jury startup exposed tools; the run was refused.", backend=_BACKEND)
    reported_cwd = payload.get("cwd")
    try:
        if not isinstance(reported_cwd, str) or Path(reported_cwd).resolve() != cwd.resolve():
            raise ConfigError("Antigravity CLI did not use the isolated jury workspace.", backend=_BACKEND)
    except OSError:
        raise ConfigError("Antigravity CLI returned an invalid workspace path.", backend=_BACKEND) from None


def _validate_event_model(event: dict, model: str) -> None:
    for container in (event, event.get("init"), event.get("step_update"), event.get("result")):
        if isinstance(container, dict) and "model" in container and container["model"] != model:
            raise ConfigError("Antigravity CLI changed the pinned model during the run.", backend=_BACKEND)


def _parse_event(session: _Session) -> dict:
    try:
        event = session.frame()
    except _CatalogError as exc:
        if "timed out" in str(exc).lower():
            raise BackendTimeoutError("Antigravity CLI session timed out.", backend=_BACKEND) from None
        raise ConfigError("Antigravity CLI returned an invalid or incomplete event stream.", backend=_BACKEND) from None
    if not isinstance(event, dict):
        raise ConfigError("Antigravity CLI returned an invalid event stream.", backend=_BACKEND)
    return event


def _result_failure(result: dict) -> Exception:
    status = result.get("status")
    error = result.get("error")
    low = error.lower().replace("_", " ").replace("-", " ") if isinstance(error, str) else ""
    if any(hint in low for hint in _AUTH_HINTS):
        return AuthError("Antigravity CLI account authentication failed.", backend=_BACKEND)
    if any(hint in low for hint in _RATE_LIMIT_HINTS):
        return RateLimitError("Antigravity CLI request exceeded its account quota or rate limit.", backend=_BACKEND)
    if any(hint in low for hint in _CONFIG_HINTS):
        return ConfigError("The selected Antigravity model is unavailable.", backend=_BACKEND)
    if status in {"CANCELED", "INTERRUPTED", "RUNNING", "WAITING"}:
        return BackendTimeoutError("Antigravity CLI did not complete the jury request.", backend=_BACKEND)
    return ConfigError("Antigravity CLI returned an unsuccessful jury result.", backend=_BACKEND)


def _startup_failure(session: _Session, fallback: Exception) -> Exception:
    """Classify early CLI failures without ever returning diagnostic text."""
    low = getattr(session, "diagnostics", "").casefold().replace("_", " ").replace("-", " ")
    if session.process.poll() is not None:
        error_reader = getattr(session, "error_reader", None)
        if error_reader is not None:
            error_reader.join(timeout=max(0.001, min(0.2, session.deadline - time.monotonic())))
            low = getattr(session, "diagnostics", "").casefold().replace("_", " ").replace("-", " ")
    if any(hint in low for hint in _AUTH_HINTS):
        return AuthError("Antigravity CLI account authentication failed before jury startup.", backend=_BACKEND)
    if any(hint in low for hint in _RATE_LIMIT_HINTS):
        return RateLimitError("Antigravity CLI account quota or rate limit prevented jury startup.", backend=_BACKEND)
    if any(hint in low for hint in _CONFIG_HINTS):
        return ConfigError("The selected Antigravity model is unavailable.", backend=_BACKEND)
    if session.process.returncode not in (None, 0):
        return TransientError("Antigravity CLI failed before jury startup.", backend=_BACKEND)
    return fallback


def _call_antigravity_cli(system: str, user: str, model: str | None, max_tokens: int | None = 1500) -> str:
    model_slug = _require_slug(model)
    if not isinstance(system, str) or not isinstance(user, str):
        raise ConfigError("Antigravity jury prompts must be text.", backend=_BACKEND)
    if len(system.encode("utf-8")) > _MAX_PROMPT_BYTES or len(user.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ConfigError("The Antigravity jury prompt exceeds the safe size limit.", backend=_BACKEND)
    cli = _resolve_cli()
    if not cli:
        raise ConfigError("Antigravity CLI (`agy`) is not installed or is not on PATH.", backend=_BACKEND)

    timeout = llm_timeout()
    timeout_arg = f"{max(1, math.ceil(timeout / 60))}m"
    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="callimachus-agy-")
    except OSError:
        raise ConfigError("An isolated Antigravity workspace could not be created safely.", backend=_BACKEND) from None
    with temporary_directory as temporary:
        cwd = Path(temporary).resolve()
        _require_hook_guard(cwd, os.environ)
        agent_dir = cwd / ".agents" / "agents"
        try:
            agent_dir.mkdir(parents=True)
        except OSError:
            raise ConfigError("The isolated Antigravity jury agent could not be prepared safely.", backend=_BACKEND) from None
        agent_file = agent_dir / f"{_AGENT}.md"
        try:
            agent_file.write_text(
                "---\n"
                f"name: {_AGENT}\n"
                "description: Callimachus citation verification jury.\n"
                "mainAgent: true\n"
                "tools: []\n"
                "inheritCustomizations: false\n"
                "excludeDefaultComponents: true\n"
                "---\n"
                f"{system}",
                encoding="utf-8",
                newline="\n",
            )
        except OSError:
            raise ConfigError("The isolated Antigravity jury agent could not be prepared safely.", backend=_BACKEND) from None
        _require_hook_guard(cwd, os.environ)
        argv = [
            cli, "--agent", _AGENT, "--model", model_slug,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--print-timeout", timeout_arg,
        ]
        try:
            with _Session(
                argv, _clean_cli_environment(os.environ), cwd=cwd, timeout=timeout,
            ) as session:
                try:
                    startup = _parse_event(session)
                except (ConfigError, BackendTimeoutError) as exc:
                    classified = _startup_failure(session, exc)
                    if classified is not exc:
                        raise classified from None
                    raise
                _validate_init(startup, model=model_slug, cwd=cwd)
                # Evidence is sent only after the CLI proves the agent, pinned
                # model, isolated cwd, and empty tool list in its init event.
                session.send({
                    "event": "user",
                    "message": {"content": user},
                })
                user_input_seen = 0
                terminal = None
                while terminal is None:
                    event = _parse_event(session)
                    _validate_event_model(event, model_slug)
                    event_name = event.get("event")
                    if event_name == "step_update":
                        step = event.get("step_update")
                        if not isinstance(step, dict):
                            raise ConfigError("Antigravity CLI returned an invalid step event.", backend=_BACKEND)
                        if "tool_info" in step or "subagent_info" in step or step.get("step_type") == "tool":
                            raise ConfigError("Antigravity jury attempted a tool or subagent action.", backend=_BACKEND)
                        if step.get("step_type") not in {"user_input", "agent_response", "checkpoint"}:
                            raise ConfigError("Antigravity CLI returned an unsupported jury step.", backend=_BACKEND)
                        if step.get("state") not in {"ACTIVE", "DONE"}:
                            raise ConfigError("Antigravity CLI returned an invalid jury step state.", backend=_BACKEND)
                        if step.get("step_type") == "user_input":
                            user_input_seen += 1
                            if user_input_seen > 1 or step.get("state") != "DONE":
                                raise ConfigError("Antigravity CLI reported an unexpected user event.", backend=_BACKEND)
                        if "text_delta" in step and not isinstance(step["text_delta"], str):
                            raise ConfigError("Antigravity CLI returned malformed response text.", backend=_BACKEND)
                        continue
                    if event_name != "result" or not isinstance(event.get("result"), dict):
                        raise ConfigError("Antigravity CLI returned an unexpected stream event.", backend=_BACKEND)
                    terminal = event["result"]

                if user_input_seen != 1:
                    raise ConfigError("Antigravity CLI returned a result without the submitted user event.", backend=_BACKEND)
                if terminal.get("status") != "SUCCESS":
                    raise _result_failure(terminal)
                response = terminal.get("response")
                if not isinstance(response, str) or not response.strip():
                    raise ConfigError("Antigravity CLI returned an empty jury response.", backend=_BACKEND)

                if session.process.stdin is not None:
                    session.process.stdin.close()
                try:
                    trailing = session.text_document()
                    session.process.wait(timeout=max(0.001, session.deadline - time.monotonic()))
                except _CatalogError as exc:
                    if "timed out" in str(exc).lower():
                        raise BackendTimeoutError("Antigravity CLI session timed out after its result.", backend=_BACKEND) from None
                    raise ConfigError("Antigravity CLI returned an invalid trailing stream.", backend=_BACKEND) from None
                except subprocess.TimeoutExpired:
                    raise BackendTimeoutError("Antigravity CLI session timed out after its result.", backend=_BACKEND) from None
                if trailing.strip() or session.process.returncode != 0:
                    raise ConfigError("Antigravity CLI returned trailing data or a failed exit status.", backend=_BACKEND)
                return response
        except ConfigError:
            raise
        except BackendTimeoutError:
            raise
        except AuthError:
            raise
        except RateLimitError:
            raise
        except _CatalogError as exc:
            if "timed out" in str(exc).lower():
                raise BackendTimeoutError("Antigravity CLI session timed out.", backend=_BACKEND) from None
            raise ConfigError("Antigravity CLI session failed safely.", backend=_BACKEND) from None
        except OSError:
            raise ConfigError("Antigravity CLI could not be started safely.", backend=_BACKEND) from None
        except subprocess.SubprocessError:
            raise TransientError("Antigravity CLI session failed.", backend=_BACKEND) from None


register(BackendSpec(
    name=_BACKEND,
    env_key="",  # Uses the local Antigravity CLI login; API-key mode is shadowed.
    env_model="ANTIGRAVITY_CLI_MODEL",
    available=_available,
    call=_call_antigravity_cli,
    requires_model=True,
    supports_tools=False,
    auto_priority=100,
    antigravity_priority=15,
))
