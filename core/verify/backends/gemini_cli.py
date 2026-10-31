# core/verify/backends/gemini_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native Google Gemini CLI backend using its existing OAuth login."""
import os
import re

from core.infra._local_executables import find_cli, cli_environment

from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._gemini_cli_runtime import run_gemini_prompt
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import ConfigError


ENV_GEMINI_CLI_MODEL = "GEMINI_CLI_MODEL"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}\Z")
_AUTO_MODEL_ALIASES = {"auto", "default", "latest", "stable"}


def _resolve_cli() -> str:
    executable = find_cli("gemini")
    if executable is None:
        raise ConfigError("Gemini CLI was not found in supported installation locations.", backend="gemini_cli")
    return executable


def _available() -> bool:
    return find_cli("gemini") is not None


def _validate_model(model: str | None) -> str:
    if not isinstance(model, str) or not model or not _MODEL_ID.fullmatch(model):
        raise ConfigError(
            f"Set {ENV_GEMINI_CLI_MODEL} to a pinned Gemini CLI model id.",
            backend="gemini_cli",
        )
    lowered = model.casefold()
    if lowered in _AUTO_MODEL_ALIASES or lowered.startswith("auto-") or lowered.endswith("-latest"):
        raise ConfigError(
            "Gemini CLI requires a pinned model id; automatic model aliases are not allowed.",
            backend="gemini_cli",
        )
    return model


def _call_gemini_cli(
    system: str, user: str, model: str | None, max_tokens: int = 1500
) -> str:
    pinned_model = _validate_model(model)
    timeout = llm_timeout()
    return run_gemini_prompt(
        _resolve_cli(),
        system,
        user,
        pinned_model,
        timeout=timeout,
        environ=cli_environment("gemini"),
    )


register(BackendSpec(
    name="gemini_cli",
    env_key="",  # Native Gemini CLI OAuth login; never pass API credentials.
    env_model=ENV_GEMINI_CLI_MODEL,
    available=_available,
    call=_call_gemini_cli,
    requires_model=True,
    supports_tools=False,
    auto_priority=78,
    codex_priority=92,
    claude_code_priority=96,
    antigravity_priority=15,
))
