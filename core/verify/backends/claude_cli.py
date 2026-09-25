# core/verify/backends/claude_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Claude CLI backend — subprocess via the ``claude`` CLI tool."""
import json
import os
import shutil
import subprocess

from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import cli_failure_error, cli_timeout_error


def _resolve_cli() -> str:
    nt = os.name == "nt"
    if nt:
        exe = shutil.which("claude.exe")
        if exe:
            return exe
    exe = shutil.which("claude")
    if exe and nt and exe.upper().endswith(".CMD"):
        base = os.path.dirname(exe)
        candidate = os.path.join(
            base, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe",
        )
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
        fallback = shutil.which("claude.exe")
        if fallback:
            return fallback
    if exe:
        return exe
    if nt:
        exe = shutil.which("claude.cmd")
    return exe or "claude"


def _available() -> bool:
    return shutil.which("claude") is not None


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
        p = subprocess.run(cmd, input=user, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
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
    available=_available,
    call=_call_cli,
    requires_model=False,
    supports_tools=True,
    auto_priority=70,
    codex_priority=90,
    claude_code_priority=20,
    antigravity_priority=70,
))
