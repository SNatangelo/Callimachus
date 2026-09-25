# core/verify/backends/gemini_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Gemini CLI backend — subprocess via the ``gemini`` CLI tool (Google Gemini CLI).

The practical headless companion for Antigravity users: lets a user running
alongside the Gemini CLI verify claims without a separate API key. A fresh
`gemini` process is spawned per call — this is required for clean context,
mirroring core/backends/claude_cli.py.
"""
import json
import shutil
import subprocess

from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import cli_failure_error, cli_timeout_error


def _resolve_cli() -> str:
    return shutil.which("gemini") or "gemini"


def _available() -> bool:
    return shutil.which("gemini") is not None


def _call_gemini_cli(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    prompt = f"{system}\n\n{user}"

    # NOTE: gemini CLI flags/output vary by version; `-m` (model) is the stable
    # non-interactive model flag. The prompt itself travels via stdin rather
    # than as a `-p` argv value — verifier prompts carry up to ~60k chars of
    # source text, which risks ARG_MAX as a command-line argument. The `gemini`
    # CLI reads a piped prompt from stdin when none is given on argv, mirroring
    # how codex_cli/claude_cli avoid the same limit. Verify with `gemini --help`.
    cmd = [_resolve_cli()]
    if model:
        cmd += ["-m", model]
    timeout = llm_timeout()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise cli_timeout_error("gemini_cli", timeout)
    if p.returncode != 0:
        raise cli_failure_error("gemini_cli", p.returncode, p.stderr, stdout=p.stdout)
    out = (p.stdout or "").strip()
    # Some gemini CLI versions can emit JSON; if so, pull the text field.
    try:
        obj = json.loads(out)
        if isinstance(obj, dict):
            return obj.get("response") or obj.get("text") or out
    except (json.JSONDecodeError, ValueError):
        pass
    return out


register(BackendSpec(
    name="gemini_cli",
    env_key="",
    available=_available,
    call=_call_gemini_cli,
    requires_model=False,
    supports_tools=False,
    auto_priority=78,
    codex_priority=92,
    claude_code_priority=96,
    antigravity_priority=15,   # preferred inside an Antigravity host (after `host`)
))
