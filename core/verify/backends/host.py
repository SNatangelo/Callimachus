# core/verify/backends/host.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Host bridge backend — delegates to an external command via stdin JSON."""
import json
import os
import shlex
import subprocess

from core.verify.backends._chat_transport import llm_timeout, llm_tools_timeout
from core.verify.backends._registry import BackendSpec, register
from core.verify.backends.errors import cli_failure_error, cli_timeout_error

ENV_HOST_COMMAND = "CITATION_VERIFIER_LLM_HOST_COMMAND"


def _available() -> bool:
    return bool((os.environ.get(ENV_HOST_COMMAND) or "").strip())


def _call_host_command(system: str, user: str, model: str | None, max_tokens: int = 1500,
                       *, task_kind: str = "llm", allow_tools: bool = False) -> str:
    command = (os.environ.get(ENV_HOST_COMMAND) or "").strip()
    if not command:
        raise RuntimeError(f"{ENV_HOST_COMMAND} not set - cannot use the host backend")
    payload = {
        "host": (
            "codex" if (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE"))
            else "claude_code" if (os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
            else "antigravity" if (os.environ.get("ANTIGRAVITY_WORKFLOW") or os.environ.get("ANTIGRAVITY_SESSION_ID"))
            else None
        ),
        "task_kind": task_kind,
        "model": model,
        "max_tokens": max_tokens,
        "allow_tools": allow_tools,
        "system": system,
        "user": user,
    }
    args = shlex.split(command, posix=(os.name != "nt"))
    timeout = llm_tools_timeout() if allow_tools else llm_timeout()
    try:
        p = subprocess.run(
            args,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise cli_timeout_error("host", timeout)
    if p.returncode != 0:
        raise cli_failure_error("host", p.returncode, p.stderr, stdout=p.stdout)
    out = (p.stdout or "").strip()
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return out
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            return obj["text"]
        if isinstance(obj.get("result"), str):
            return obj["result"]
    return out


register(BackendSpec(
    name="host",
    env_key=ENV_HOST_COMMAND,
    available=_available,
    call=_call_host_command,
    requires_model=False,
    supports_tools=True,
    auto_priority=10,
))
