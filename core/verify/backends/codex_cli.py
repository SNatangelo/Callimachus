# core/verify/backends/codex_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Codex CLI backend — subprocess via the ``codex`` CLI tool (non-interactive exec mode).

Lets a user running inside the Codex host (or anywhere the `codex` CLI is
installed and logged in) verify claims without a separate API key. A fresh
`codex exec` process is spawned per call — this is required for clean
context, mirroring core/backends/claude_cli.py.
"""
import os
import subprocess
import tempfile

from core.infra._local_executables import cli_argv, cli_environment, find_cli
from core.verify.backends._chat_transport import llm_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import ConfigError, cli_failure_error, cli_timeout_error


def _resolve_cli() -> str:
    executable = find_cli("codex")
    if executable is None:
        raise ConfigError("Codex CLI was not found in supported installation locations.", backend="codex_cli")
    return executable


def _available() -> bool:
    return find_cli("codex") is not None


def _call_codex_cli(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    prompt = f"{system}\n\n{user}"

    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as tf:
        out_path = tf.name
    try:
        # NOTE: `codex exec` flags are version-dependent; verify with
        # `codex exec --help` on the target machine. Prompt is read from
        # stdin (`-`); the final assistant message is captured via
        # --output-last-message.
        cmd = [
            _resolve_cli(), "exec", "--skip-git-repo-check",
            "--sandbox", "read-only", "--output-last-message", out_path,
        ]
        if model:
            cmd += ["--model", model]
        cmd += ["-"]
        timeout = llm_timeout()
        try:
            child_env = cli_environment("codex")
            p = subprocess.run(cli_argv(cmd, child_env), input=prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               env=child_env)
        except subprocess.TimeoutExpired:
            raise cli_timeout_error("codex_cli", timeout)
        if p.returncode != 0:
            raise cli_failure_error("codex_cli", p.returncode, p.stderr, stdout=p.stdout)
        try:
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                msg = fh.read().strip()
        except OSError:
            msg = ""
        return msg or (p.stdout or "").strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


register(BackendSpec(
    name="codex_cli",
    env_key="",              # uses the codex CLI's own login, no API key
    env_model="CODEX_CLI_MODEL",
    available=_available,
    call=_call_codex_cli,
    requires_model=False,
    supports_tools=False,
    auto_priority=75,
    codex_priority=15,        # preferred inside a Codex host (after `host`)
    claude_code_priority=95,  # deprioritized elsewhere
    antigravity_priority=85,
))
