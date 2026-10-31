# core/verify/backends/claude_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Claude CLI backend — subprocess via the ``claude`` CLI tool."""
import json
import subprocess

from core.infra._local_executables import cli_argv, cli_environment, find_cli
from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import ConfigError, cli_failure_error, cli_timeout_error


def _resolve_cli() -> str:
    exe = find_cli("claude")
    if exe is None:
        raise ConfigError("Claude CLI was not found in supported installation locations.", backend="claude_cli")
    return exe


def _available() -> bool:
    return find_cli("claude") is not None


def _call_cli(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    cmd = [
        _resolve_cli(),
        "--append-system-prompt",
        system,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--tools",
        "",
    ]
    if model:
        cmd += ["--model", model]
    timeout = llm_timeout()
    try:
        child_env = cli_environment("claude")
        p = subprocess.run(cli_argv(cmd, child_env), input=user, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=child_env)
    except subprocess.TimeoutExpired:
        raise cli_timeout_error("claude_cli", timeout)
    if p.returncode != 0:
        raise cli_failure_error("claude_cli", p.returncode, p.stderr, stdout=p.stdout)
    try:
        obj = json.loads(p.stdout)
        return obj.get("result", "") if isinstance(obj, dict) else str(obj)
    except json.JSONDecodeError:
        return p.stdout


register(BackendSpec(
    name="claude_cli",
    env_key="",  # no API key — relies on claude CLI login
    env_model="CLAUDE_CLI_MODEL",
    available=_available,
    call=_call_cli,
    requires_model=False,
    supports_tools=True,
    auto_priority=70,
    codex_priority=90,
    claude_code_priority=20,
    antigravity_priority=70,
))
