#!/usr/bin/env python3
# adapters/claude-code/hooks/citation_arm.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
citation_arm.py — Claude Code UserPromptSubmit hook: arm the verification requirement from
the USER's words (which the agent cannot reword), so the Stop hook can later force — or
itself launch — a real run.

Wire it in .claude/settings.json:

  { "hooks": { "UserPromptSubmit": [ { "hooks": [
      { "type": "command",
        "command": "python3 adapters/claude-code/hooks/citation_arm.py" } ] } ] } }

On each user prompt:
  - if it is a citation-verification request (tight regex: an action verb near a
    citation/source noun) AND not an explicit user override ("senza verifica formale",
    "no full run", "just answer") → write a sentinel
    <cwd>/.citation_verifier/pending.json {manuscript, prompt_sha, armed_at} and inject a
    line of context telling the assistant this task MUST go through run.py;
  - otherwise do nothing.

Detection is deliberately on the USER's request (stable input), tight to limit false
positives. The manuscript path is taken from a file-looking token in the prompt that exists
on disk; if none is found the sentinel records manuscript=null and the Stop hook will ask
for the file rather than guess. stdlib-only.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

_INTENT = re.compile(
    r"(verif\w*|check|controll\w*|valid\w*|fact[\s-]?check|audit)\b.{0,40}?"
    r"(citation|cite\b|cited|source|reference|bibliograph\w*|"
    r"citazion\w*|font[ei]\b|riferiment\w*|bibliograf\w*)",
    re.IGNORECASE | re.DOTALL)
_INTENT_REV = re.compile(
    r"(citation|reference|bibliograph\w*|citazion\w*|riferiment\w*|bibliograf\w*)"
    r".{0,40}?(verif\w*|check|controll\w*|valid\w*|fact[\s-]?check)",
    re.IGNORECASE | re.DOTALL)
_OVERRIDE = re.compile(
    r"(senza verifica( formale)?|no full run|just answer|solo rispondi|"
    r"niente run|skip verification|no formal verification)", re.IGNORECASE)
_FILE = re.compile(r"[\w./\\~-]+\.(?:pdf|docx|tex|md|txt)", re.IGNORECASE)


def _run_command(*args):
    """Render the runner command for the OS that executes this hook."""
    package = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    argv = [sys.executable, os.path.join(package, "run.py"), *args]
    if os.name != "nt":
        return shlex.join(argv)
    rendered = " ".join(_windows_argument(value) for value in argv)
    if subprocess.list2cmdline([sys.executable]).startswith('"'):
        rendered = "& " + rendered
    return rendered


def _windows_argument(value):
    rendered = subprocess.list2cmdline([value])
    if ("<" in value or ">" in value) and rendered == value:
        return f'"{value}"'
    return rendered


def _state_dir(cwd):
    """Where to write the armed sentinel. Defaults under the project (agent-writable); a
    deployment can set CITATION_VERIFIER_STATE_DIR to a dir the agent's tools cannot write,
    so the agent cannot delete the sentinel to dodge the Stop hook. citation_verify_stop.py
    resolves this identically."""
    return (os.environ.get("CITATION_VERIFIER_STATE_DIR")
            or os.path.join(cwd, ".citation_verifier"))


def _detect_manuscript(prompt, cwd):
    for tok in _FILE.findall(prompt):
        cand = os.path.expanduser(tok)
        for path in (cand, os.path.join(cwd, cand)):
            if os.path.isfile(path):
                return os.path.abspath(path)
    return None


def _arm_error(cwd, reason):
    """Fail closed when the hook input cannot be trusted."""
    try:
        sentinel_dir = _state_dir(cwd)
        os.makedirs(sentinel_dir, exist_ok=True)
        with open(os.path.join(sentinel_dir, "pending.json"), "w", encoding="utf-8") as f:
            json.dump({"manuscript": None, "error": reason}, f)
    except Exception:
        print("[citation-verifier] Hook error: could not persist a fail-closed sentinel.")
        return False
    else:
        print("[citation-verifier] Hook error: invalid hook input; verification is blocked.")
        return True


def main():
    fallback_cwd = os.getcwd()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0 if _arm_error(fallback_cwd, "invalid hook JSON") else 1)
    if not isinstance(payload, dict):
        sys.exit(0 if _arm_error(fallback_cwd, "hook JSON must be an object") else 1)
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    cwd = payload.get("cwd") or fallback_cwd
    if not isinstance(prompt, str) or not isinstance(cwd, str) or not cwd:
        sys.exit(0 if _arm_error(fallback_cwd, "invalid hook payload") else 1)

    is_task = bool(_INTENT.search(prompt) or _INTENT_REV.search(prompt))
    if not is_task or _OVERRIDE.search(prompt):
        sys.exit(0)   # not a verification task, or the user opted out → no-op

    try:
        sentinel_dir = _state_dir(cwd)
        os.makedirs(sentinel_dir, exist_ok=True)
        manuscript = _detect_manuscript(prompt, cwd)
        with open(os.path.join(sentinel_dir, "pending.json"), "w", encoding="utf-8") as f:
            json.dump({"manuscript": manuscript,
                       "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                       "armed_at": time.time()}, f)
    except Exception:
        sys.exit(0 if _arm_error(fallback_cwd, "could not arm verification sentinel") else 1)

    # Injected context (UserPromptSubmit stdout is added to the model's context).
    print("[citation-verifier] This is a citation-verification task. It MUST be done by the "
          "deterministic runner — run `"
          f"{_run_command('--input', '<manuscript>', '--autonomous')}`. Do NOT "
          "verify by reading the sources yourself, and present results with "
          f"`{_run_command('present')}` (never paste the report). The turn cannot end until a "
          "signed run exists"
          + (f" for {os.path.basename(manuscript)}." if manuscript else "."))
    sys.exit(0)


if __name__ == "__main__":
    main()
