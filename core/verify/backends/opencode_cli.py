# core/verify/backends/opencode_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated, no-tools OpenCode CLI backend using its saved first-party auth."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager

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


_BACKEND = "opencode_cli"
ENV_OPENCODE_CLI_MODEL = "OPENCODE_CLI_MODEL"
_VERSION = "1.19.3"
_AGENT = "callimachus-jury"
_PROVIDERS = frozenset({"opencode-go"})
_AUTO_MODEL_ALIASES = frozenset({"auto", "default", "latest", "stable"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}\Z")
_MAX_AUTH_BYTES = 128 * 1024
_MAX_PROMPT_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_MODELS = 500
_CATALOG_TIMEOUT = 12.0
_AUTH_HINTS = (
    "auth", "credential", "not logged in", "login required", "sign in",
    "unauthorized", "401",
)
_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "quota exceeded", "429", "too many requests")
_MODEL_ERROR_HINTS = ("model not found", "unknown model", "unsupported model", "invalid model")


def _resolve_cli() -> str | None:
    return find_cli("opencode")


def _available() -> bool:
    return _resolve_cli() is not None


def _merged_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    result = {str(key).casefold(): str(value) for key, value in os.environ.items()}
    if environ is not None:
        result.update({str(key).casefold(): str(value) for key, value in environ.items()})
    return result


def _native_auth_path(environ: Mapping[str, str]) -> Path:
    data_home = environ.get("xdg_data_home", "").strip()
    if data_home:
        base = Path(data_home).expanduser()
    else:
        home = environ.get("userprofile", "").strip() or environ.get("home", "").strip()
        base = Path(home).expanduser() if home else Path.home()
        base = base / ".local" / "share"
    if not base.is_absolute():
        raise _CatalogError("OpenCode's native account location is not an absolute path.")
    return base / "opencode" / "auth.json"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate property")
        result[key] = value
    return result


def _load_native_auth(environ: Mapping[str, str], provider: str) -> dict:
    if provider not in _PROVIDERS:
        raise _CatalogError("Only OpenCode's native Go account is qualified for this backend.")
    path = _native_auth_path(environ)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise _CatalogError("Connect the native OpenCode Go account first.") from None
    except OSError:
        raise _CatalogError("OpenCode's native account file could not be checked safely.") from None
    if path.is_symlink() or not path.is_file() or info.st_size > _MAX_AUTH_BYTES:
        raise _CatalogError("OpenCode's native account file is ambiguous or oversized.")
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_AUTH_BYTES:
            raise ValueError("oversized")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError):
        raise _CatalogError("OpenCode's native account file is malformed or unreadable.") from None
    if not isinstance(data, dict):
        raise _CatalogError("OpenCode's native account file has an unknown shape.")
    entry = data.get(provider)
    if not isinstance(entry, dict):
        raise _CatalogError("Sign in to OpenCode with the selected native account first.")
    kind = entry.get("type")
    if kind == "api" and provider == "opencode-go":
        if set(entry) - {"type", "key"}:
            raise _CatalogError("OpenCode's saved Go account has unsupported fields.")
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 8192:
            raise _CatalogError("OpenCode's saved Go account is malformed.")
        return {"type": "api", "key": key}
    raise _CatalogError("The selected OpenCode account is not a supported first-party saved Go key.")


def _managed_paths(environ: Mapping[str, str]) -> tuple[Path, ...]:
    if sys.platform == "win32":
        program_data = environ.get("programdata", "").strip() or r"C:\ProgramData"
        base = Path(program_data)
        if not base.is_absolute():
            raise _CatalogError("OpenCode's managed configuration location is ambiguous.")
        directory = base / "opencode"
        return directory, directory / "opencode.json", directory / "opencode.jsonc"
    if sys.platform == "darwin":
        return (
            Path("/Library/Application Support/opencode"),
            Path("/Library/Application Support/opencode/opencode.json"),
            Path("/Library/Application Support/opencode/opencode.jsonc"),
            Path("/Library/Managed Preferences") / f"{_username()}/ai.opencode.managed.plist",
            Path("/Library/Managed Preferences/ai.opencode.managed.plist"),
        )
    directory = Path("/etc/opencode")
    return directory, directory / "opencode.json", directory / "opencode.jsonc"


def _username() -> str:
    try:
        import getpass
        return getpass.getuser() or "user"
    except (OSError, KeyError):
        return "user"


def _check_managed_config(environ: Mapping[str, str]) -> None:
    """Reject machine policy before OpenCode has an opportunity to merge it."""
    paths = _managed_paths(environ)
    directory, *files = paths
    try:
        info = directory.lstat()
    except FileNotFoundError:
        info = None
    except OSError:
        raise _CatalogError("OpenCode's managed configuration could not be checked safely.") from None
    if info is not None and (directory.is_symlink() or not directory.is_dir()):
        raise _CatalogError("OpenCode's managed configuration directory is ambiguous.")
    for path in files:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise _CatalogError("OpenCode's managed configuration could not be checked safely.") from None
        if path.is_symlink() or not path.is_file() or info.st_size > _MAX_AUTH_BYTES:
            raise _CatalogError("OpenCode's managed configuration is ambiguous or oversized.")
        try:
            if path.read_bytes().strip():
                raise _CatalogError("Nonempty OpenCode machine-managed configuration is not allowed.")
        except _CatalogError:
            raise
        except OSError:
            raise _CatalogError("OpenCode's managed configuration could not be read safely.") from None


def _set_env(child: dict[str, str], key: str, value: str) -> None:
    existing = next((item for item in child if item.casefold() == key.casefold()), key)
    child[existing] = value


def _isolated_environment(
    inherited: Mapping[str, str], root: Path, auth: dict, config: dict,
) -> dict[str, str]:
    # _Session merges overrides with os.environ, so explicitly shadow every
    # inherited variable before restoring only process basics and our settings.
    child = {}
    seen = set()
    for key in os.environ:
        folded = str(key).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        child[str(key)] = ""
    for key in inherited:
        folded = str(key).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        child[str(key).upper()] = ""
    source = _merged_environment(inherited)
    for name in ("path", "systemroot", "windir", "comspec", "pathext"):
        value = source.get(name)
        if value:
            _set_env(child, name, value)
    if source.get("programdata"):
        _set_env(child, "ProgramData", source["programdata"])
    home = root / "home"
    dirs = {
        "home": home,
        "config": root / "xdg-config",
        "data": root / "xdg-data",
        "cache": root / "xdg-cache",
        "state": root / "xdg-state",
        "tmp": root / "tmp",
        "appdata": root / "appdata",
        "localappdata": root / "localappdata",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    for name, path in (
        ("HOME", home), ("USERPROFILE", home),
        ("XDG_CONFIG_HOME", dirs["config"]), ("XDG_DATA_HOME", dirs["data"]),
        ("XDG_CACHE_HOME", dirs["cache"]), ("XDG_STATE_HOME", dirs["state"]),
        ("TMP", dirs["tmp"]), ("TEMP", dirs["tmp"]), ("TMPDIR", dirs["tmp"]),
        ("APPDATA", dirs["appdata"]), ("LOCALAPPDATA", dirs["localappdata"]),
        ("OPENCODE_CONFIG_DIR", dirs["config"] / "opencode"),
        ("OPENCODE_DB", root / "opencode.sqlite"),
    ):
        _set_env(child, name, str(path))
    if os.name == "nt":
        _set_env(child, "HOMEDRIVE", home.drive)
        _set_env(child, "HOMEPATH", str(home)[len(home.drive):])
    for key, value in {
        "OPENCODE_CONFIG": "",
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, ensure_ascii=False, separators=(",", ":")),
        "OPENCODE_AUTH_CONTENT": json.dumps(auth, ensure_ascii=False, separators=(",", ":")),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_PURE": "1",
        "OPENCODE_TEST_MANAGED_CONFIG_DIR": "",
        "OPENCODE_CONSOLE_TOKEN": "",
        "NO_COLOR": "1",
        "CI": "1",
        "TERM": "dumb",
    }.items():
        _set_env(child, key, value)
    return child


@contextmanager
def _isolated_runtime(
    environ: Mapping[str, str], provider: str, auth: dict, config: dict,
    *, temp_root: Path | str | None = None,
):
    source = _merged_environment(environ)
    _check_managed_config(source)
    parent = None
    if temp_root is not None:
        try:
            parent = Path(temp_root).resolve(strict=True)
        except OSError:
            raise _CatalogError("An isolated OpenCode workspace could not be created safely.") from None
        if not parent.is_dir():
            raise _CatalogError("An isolated OpenCode workspace could not be created safely.")
    try:
        with tempfile.TemporaryDirectory(
            prefix=".callimachus-opencode-", dir=str(parent) if parent is not None else None,
        ) as name:
            root = Path(name).resolve()
            cwd = root / "workspace"
            cwd.mkdir()
            yield cwd, _isolated_environment(environ, root, {provider: auth}, config), root
    except _CatalogError:
        raise
    except OSError:
        raise _CatalogError("An isolated OpenCode workspace could not be prepared safely.") from None


def _validate_model(model: str | None) -> tuple[str, str, str]:
    if not isinstance(model, str) or not _ID.fullmatch(model) or "/" not in model:
        raise ConfigError(f"Set {ENV_OPENCODE_CLI_MODEL} to a pinned OpenCode provider/model id.", backend=_BACKEND)
    provider, model_id = model.split("/", 1)
    if provider not in _PROVIDERS or not model_id or not _ID.fullmatch(model_id):
        raise ConfigError("Choose a pinned model from the OpenCode native catalog.", backend=_BACKEND)
    lowered = model_id.casefold()
    if lowered in _AUTO_MODEL_ALIASES or lowered.startswith("auto-") or lowered.endswith("-latest"):
        raise ConfigError("OpenCode CLI requires a pinned model id; automatic aliases are not allowed.", backend=_BACKEND)
    return provider, model_id, f"{provider}/{model_id}"


def _check_cli_version(path: str, env: Mapping[str, str], cwd: Path, timeout: float) -> None:
    try:
        with _Session([path, "--pure", "--version"], env, cwd=cwd, timeout=min(timeout, 8.0)) as session:
            if session.process.stdin is not None:
                session.process.stdin.close()
            output = session.text_document().strip()
            code = session.process.wait(timeout=max(0.01, session.deadline - time.monotonic()))
    except (_CatalogError, subprocess.TimeoutExpired, OSError):
        raise ConfigError("The installed OpenCode CLI could not be qualified safely.", backend=_BACKEND) from None
    if code != 0 or output != _VERSION:
        raise ConfigError(f"OpenCode CLI v{_VERSION} is required for this isolated backend.", backend=_BACKEND)


def _event_object(raw: str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        raise ConfigError("OpenCode returned malformed JSON events.", backend=_BACKEND) from None
    if not isinstance(value, dict):
        raise ConfigError("OpenCode returned a malformed event stream.", backend=_BACKEND)
    return value


def _model_fields_match(value: dict, expected: str) -> bool:
    short = expected.split("/", 1)[1]
    for container in (value, value.get("part")):
        if not isinstance(container, dict):
            continue
        for key in ("model", "modelID", "modelId"):
            model = container.get(key)
            if model is None:
                continue
            if not isinstance(model, str) or model not in {expected, short}:
                return False
    return True


def _raise_event_error(event: dict) -> None:
    error = event.get("error")
    name = ""
    message = ""
    status = None
    if isinstance(error, str):
        message = error
    elif isinstance(error, dict):
        name = error.get("name", "") if isinstance(error.get("name"), str) else ""
        data = error.get("data")
        if isinstance(data, dict):
            message = data.get("message", "") if isinstance(data.get("message"), str) else ""
            candidate = data.get("statusCode") or data.get("status")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                status = candidate
        if not message:
            message = error.get("message", "") if isinstance(error.get("message"), str) else ""
    low = f"{name} {message}".casefold().replace("_", " ").replace("-", " ")
    if status == 401 or any(hint in low for hint in _AUTH_HINTS):
        raise AuthError("OpenCode native account authentication failed.", backend=_BACKEND)
    if status == 429 or any(hint in low for hint in _RATE_LIMIT_HINTS):
        raise RateLimitError("OpenCode account quota or rate limit prevented the request.", backend=_BACKEND)
    if status in {400, 404, 422} or any(hint in low for hint in _MODEL_ERROR_HINTS):
        raise ConfigError("The selected OpenCode model or request is unavailable.", backend=_BACKEND)
    raise TransientError("OpenCode reported an unsuccessful jury request.", backend=_BACKEND)


def _parse_response(raw: str, expected_model: str) -> str:
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        events.append(_event_object(line))
        if len(events) > 2048:
            raise ConfigError("OpenCode returned too many events.", backend=_BACKEND)
    for event in events:
        if event.get("type") == "error":
            _raise_event_error(event)
    if len(events) < 3:
        raise ConfigError("OpenCode returned an incomplete jury response.", backend=_BACKEND)
    first = events[0]
    if first.get("type") != "step_start" or not isinstance(first.get("sessionID"), str):
        raise ConfigError("OpenCode did not start a valid jury response.", backend=_BACKEND)
    first_part = first.get("part")
    if not isinstance(first_part, dict) or first_part.get("type") != "step-start":
        raise ConfigError("OpenCode returned an invalid jury startup event.", backend=_BACKEND)
    session_id = first["sessionID"]
    message_id = first_part.get("messageID")
    if not isinstance(message_id, str) or not message_id or first_part.get("sessionID") != session_id:
        raise ConfigError("OpenCode returned an invalid jury event identity.", backend=_BACKEND)
    text_parts = []
    finished = False
    for index, event in enumerate(events):
        if not _model_fields_match(event, expected_model):
            raise ConfigError("OpenCode changed the pinned model during the jury request.", backend=_BACKEND)
        if event.get("sessionID") != session_id:
            raise ConfigError("OpenCode returned an event from another session.", backend=_BACKEND)
        part = event.get("part")
        if not isinstance(part, dict) or part.get("sessionID") != session_id or part.get("messageID") != message_id:
            raise ConfigError("OpenCode returned an event for another jury message.", backend=_BACKEND)
        kind = event.get("type")
        if index == 0:
            continue
        if finished:
            raise ConfigError("OpenCode returned events after the jury response finished.", backend=_BACKEND)
        if kind == "text" and part.get("type") == "text":
            value = part.get("text")
            if not isinstance(value, str):
                raise ConfigError("OpenCode returned malformed jury text.", backend=_BACKEND)
            text_parts.append(value)
        elif kind == "step_finish" and part.get("type") == "step-finish":
            if part.get("reason") != "stop":
                raise ConfigError("OpenCode did not finish the jury response normally.", backend=_BACKEND)
            finished = True
        else:
            # In particular, tool_use, tool_result and error events are refused.
            raise ConfigError("OpenCode emitted a tool, error, or unsupported jury event.", backend=_BACKEND)
    response = "".join(text_parts)
    if not finished or not response.strip():
        raise ConfigError("OpenCode returned an empty or incomplete jury response.", backend=_BACKEND)
    return response


def _failure_from_diagnostics(diagnostics: str, returncode: int) -> Exception:
    low = diagnostics.casefold().replace("_", " ").replace("-", " ")
    if any(hint in low for hint in _AUTH_HINTS):
        return AuthError("OpenCode native account authentication failed.", backend=_BACKEND)
    if any(hint in low for hint in _RATE_LIMIT_HINTS):
        return RateLimitError("OpenCode account quota or rate limit prevented the request.", backend=_BACKEND)
    if any(hint in low for hint in _MODEL_ERROR_HINTS):
        return ConfigError("The selected OpenCode model is unavailable.", backend=_BACKEND)
    if returncode:
        return TransientError("OpenCode CLI failed before completing the jury request.", backend=_BACKEND)
    return ConfigError("OpenCode CLI returned an unsuccessful jury response.", backend=_BACKEND)


def run_opencode_prompt(
    path: str, system: str, user: str, model: str, *, timeout: float,
    environ: Mapping[str, str] | None = None, temp_root: Path | str | None = None,
) -> str:
    provider, model_id, pinned_model = _validate_model(model)
    if not isinstance(system, str) or not isinstance(user, str):
        raise ConfigError("OpenCode jury prompts must be text.", backend=_BACKEND)
    if len(system.encode("utf-8")) > _MAX_PROMPT_BYTES or len(user.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ConfigError("The OpenCode jury prompt exceeds the safe size limit.", backend=_BACKEND)
    source = _merged_environment(environ)
    try:
        auth = _load_native_auth(source, provider)
        config = {
            "enabled_providers": [provider],
            "model": pinned_model,
            "small_model": pinned_model,
            "default_agent": _AGENT,
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "subagent_depth": 0,
            "plugin": [],
            "agent": {
                _AGENT: {
                    "mode": "primary",
                    "model": pinned_model,
                    "permission": "deny",
                    "prompt": system,
                },
            },
        }
        with _isolated_runtime(source, provider, auth, config, temp_root=temp_root) as (cwd, child_env, _root):
            _check_cli_version(path, child_env, cwd, timeout)
            argv = [
                path, "--pure", "run", "--dir", str(cwd), "--agent", _AGENT,
                "--model", pinned_model, "--format", "json",
                "--title", "Callimachus citation verification",
            ]
            try:
                with _Session(argv, child_env, cwd=cwd, timeout=timeout) as session:
                    session.send_text(user)
                    try:
                        session.process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    output = session.text_document()
                    code = session.process.wait(timeout=max(0.01, session.deadline - time.monotonic()))
                    diagnostics = session.diagnostics
            except _CatalogError as exc:
                if "timed out" in str(exc).casefold():
                    raise BackendTimeoutError("OpenCode CLI jury request timed out.", backend=_BACKEND) from None
                raise TransientError("OpenCode CLI closed before returning a complete jury response.", backend=_BACKEND) from None
            except subprocess.TimeoutExpired:
                raise BackendTimeoutError("OpenCode CLI jury request timed out.", backend=_BACKEND) from None
            except OSError:
                raise ConfigError("OpenCode CLI could not be started.", backend=_BACKEND) from None
        if code != 0:
            raise _failure_from_diagnostics(diagnostics, code)
        return _parse_response(output, pinned_model)
    except _CatalogError as exc:
        if "timeout" in str(exc).casefold():
            raise BackendTimeoutError("OpenCode CLI jury request timed out.", backend=_BACKEND) from None
        raise ConfigError(str(exc), backend=_BACKEND) from None


def _parse_catalog(output: str, provider: str) -> list[str]:
    result = []
    for line in output.splitlines():
        row = line.strip()
        if not row:
            continue
        prefix = provider + "/"
        if not row.startswith(prefix):
            raise _CatalogError("OpenCode returned a model outside the selected first-party provider.")
        model = row[len(prefix):]
        if not model or not _ID.fullmatch(model):
            raise _CatalogError("OpenCode returned an invalid native model identifier.")
        if row not in result:
            result.append(row)
        if len(result) > _MAX_CATALOG_MODELS:
            raise _CatalogError("OpenCode's native model catalog is oversized.")
    if not result:
        raise _CatalogError("OpenCode returned no native models for the selected account.")
    return result


def check_catalog(
    path: str, environ: Mapping[str, str], *, temp_root: Path | str | None = None,
) -> dict:
    """Return actual native first-party model IDs without sending a prompt."""
    result = {
        "ok": False, "models": [], "authenticated": False,
        "message": "OpenCode's native model catalog could not be checked safely.",
    }
    if not isinstance(path, str) or not path or Path(path).name.casefold() not in {"opencode", "opencode.exe"}:
        result["message"] = "The executable is not the supported OpenCode CLI."
        return result
    source = _merged_environment(environ)
    providers = []
    for provider in sorted(_PROVIDERS):
        try:
            providers.append((provider, _load_native_auth(source, provider)))
        except _CatalogError:
            continue
    if not providers:
        result["message"] = "Connect the native OpenCode Go account first."
        return result
    result["authenticated"] = None
    models = []
    session = None
    try:
        for provider, auth in providers:
            config = {
                "enabled_providers": [provider],
                "autoupdate": False,
                "share": "disabled",
                "plugin": [],
            }
            with _isolated_runtime(
                source, provider, auth, config, temp_root=temp_root,
            ) as (cwd, child_env, _root):
                _check_cli_version(path, child_env, cwd, _CATALOG_TIMEOUT)
                with _Session(
                    [path, "--pure", "models", provider], child_env,
                    cwd=cwd, timeout=_CATALOG_TIMEOUT,
                ) as session:
                    if session.process.stdin is not None:
                        session.process.stdin.close()
                    output = session.text_document()
                    code = session.process.wait(timeout=max(0.01, session.deadline - time.monotonic()))
                    diagnostics = session.diagnostics
                if code != 0:
                    raise _CatalogError("OpenCode's native model catalog command failed.")
                models.extend(_parse_catalog(output, provider))
        result.update(
            ok=True,
            models=list(dict.fromkeys(models)),
            authenticated=None,
            message="OpenCode native Go saved credential and first-party model catalog detected; account access is checked when used.",
        )
    except (_CatalogError, ConfigError) as exc:
        if session is not None and any(hint in session.diagnostics.casefold() for hint in _AUTH_HINTS):
            result["authenticated"] = False
            result["message"] = "Connect the native OpenCode Go account first."
        else:
            result["message"] = str(exc)
    except (OSError, subprocess.SubprocessError, ValueError):
        result["message"] = "OpenCode's native model catalog could not be checked safely."
    return result


def _call_opencode_cli(system: str, user: str, model: str | None, max_tokens: int | None = 1500) -> str:
    selected = model or os.environ.get(ENV_OPENCODE_CLI_MODEL)
    path = _resolve_cli()
    if path is None:
        raise ConfigError("OpenCode CLI is not installed or is not on PATH.", backend=_BACKEND)
    timeout = llm_timeout()
    return run_opencode_prompt(path, system, user, selected, timeout=timeout, environ=os.environ)


register(BackendSpec(
    name=_BACKEND,
    env_key="",  # Reuse only a selected native OpenCode saved account.
    env_model=ENV_OPENCODE_CLI_MODEL,
    available=_available,
    call=_call_opencode_cli,
    requires_model=True,
    supports_tools=False,
    auto_priority=77,
))
