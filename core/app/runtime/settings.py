# core/app/runtime/settings.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime configuration, environment parsing, and operator diagnostics."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar

from core.app.runtime_paths import application_argv, is_frozen, resource_root, user_data_root
from core.fetch.fallbacks import fetch_modes as _fetch_modes
from core.fetch.transport.http_headers import (
    DEFAULT_HTTP_PROFILE,
    ENV_HTTP_PROFILE,
    HTTP_PROFILE_CHOICES,
)
from core.infra import startup_preflight as _startup_preflight
from core.infra.integrity import signing as _signing
from core.invocation import run_command

PKG_DIR = str(resource_root())
PHASES = [
    "parse",
    "resolve",
    "fetch",
    "gaps",
    "style",
    "verify",
    "web_research",
    "report",
    "done",
]

# Phase 0 configuration read from the environment, so a .env carries it
# across machines.
ACCURACY_CHOICES = ("maximum", "maximum_fallback", "standard", "abstract", "standard_web")
DEFAULT_ACCURACY = "standard"  # full text preferred; abstract fallback on paywall
ENV_ACCURACY = "CITATION_VERIFIER_ACCURACY"
ENV_MAILTO = "CITATION_VERIFIER_MAILTO"
ENV_GBOOKS = "GOOGLE_BOOKS_API_KEY"
ENV_OCR_LANG = "CITATION_VERIFIER_OCR_LANG"
DEFAULT_OCR_LANG = "eng"
ENV_FETCH_WORKERS = "CITATION_VERIFIER_FETCH_WORKERS"
ENV_RESOLVE_WORKERS = "CITATION_VERIFIER_RESOLVE_WORKERS"
ENV_REPORT_HTML = "CITATION_VERIFIER_REPORT_HTML"
ENV_CHALLENGE_MODE = _fetch_modes.ENV_CHALLENGE_MODE
CHALLENGE_MODE_CHOICES = _fetch_modes.mode_choices()
DEFAULT_CHALLENGE_MODE = _fetch_modes.DEFAULT_CHALLENGE_MODE
DEFAULT_FETCH_WORKERS = 4
ENV_DEBUG_RUN = "CITATION_VERIFIER_DEBUG_RUN"

# Suggested (fast/cheap, strong) model ids per backend, offered by the
# interactive picker. Purely advisory: users may enter any id.
_MODEL_SUGGESTIONS = {
    "claude_cli": ("claude-haiku-4-5", "claude-sonnet-5"),
    "anthropic": ("claude-haiku-4-5", "claude-sonnet-5"),
    "codex_cli": ("gpt-5-mini", "gpt-5"),
    "openai": ("gpt-5-mini", "gpt-5"),
    "gemini_cli": ("gemini-2.5-flash", "gemini-2.5-pro"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro"),
}
ACTION_REQUIRED = 10
GATE_FAILED = 20

# A second resume on the same run found the run lock already held and exited
# instead of racing the first process.
RUN_LOCKED = 3

_ACTIVE_PROGRESS_PHASE: ContextVar[str | None] = ContextVar(
    "callimachus_progress_phase", default=None
)


def _model_suggestions_for(backend):
    """Return (fast, quality) suggested model ids for *backend*, or None."""
    return _MODEL_SUGGESTIONS.get(backend)


def _debug_mode_enabled(st: dict) -> bool:
    return bool(st.get("debug_mode"))


def _debug_directives_from_parse() -> tuple[bool, list[str]]:
    labels: set[str] = set()
    env_enabled = str(os.environ.get(ENV_DEBUG_RUN) or "").strip().lower() in ("1", "true", "yes", "on")
    if env_enabled:
        labels.add(f"env:{ENV_DEBUG_RUN}")
    return env_enabled, sorted(labels)


def _progress(msg):
    """Step-by-step progress so a human (or the watching agent) sees work happening."""
    phase = _ACTIVE_PROGRESS_PHASE.get()
    label = str(phase or "run").replace("_", " ").title()
    line = f"[{label}] {msg}"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)


@contextmanager
def _progress_phase(phase: str):
    """Label progress emitted while a pipeline phase is active."""
    token = _ACTIVE_PROGRESS_PHASE.set(phase)
    try:
        yield
    finally:
        _ACTIVE_PROGRESS_PHASE.reset(token)


def _fetch_worker_count(environ=None) -> int:
    env = environ or os.environ
    raw = str(env.get(ENV_FETCH_WORKERS) or "").strip()
    if not raw:
        return DEFAULT_FETCH_WORKERS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FETCH_WORKERS
    return max(1, min(value, 16))


def _resolve_worker_count(st: dict | None = None, environ=None, total: int | None = None) -> int:
    env = environ or os.environ
    # 1) Explicit resolve-specific override wins.
    raw = str(env.get(ENV_RESOLVE_WORKERS) or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 32))
        except ValueError:
            pass
    # 2) A fetch_workers value stored in run state.
    if isinstance(st, dict):
        try:
            value = int(st.get("fetch_workers") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return max(1, min(value, 16))
    # 3) Ref-count-aware default: scale up for large bibliographies, but stay
    #    at the fetch default (4) for small runs so existing behavior/tests are
    #    unchanged. Per-host rate limits bound the actual API pressure.
    if total and total > 0:
        return min(16, max(_fetch_worker_count(environ), (total + 3) // 4))
    return _fetch_worker_count(environ)


def _blocking_fetch_need_items(need, accuracy, refs=None):
    return list(need)


def _print_signing_status():
    """Tell the user up front whether this process can emit a trusted HMAC seal."""
    if _signing.key_present():
        source = ("file" if os.environ.get(_signing.ENV_KEY_FILE)
                  else "env" if os.environ.get(_signing.ENV_KEY)
                  else "resolved")
        print("Signing key: present "
              f"({source} — reports generated in this context can be HMAC-signed)")
    else:
        print("Trusted HMAC is not enabled "
              "(reports generated in this context will get sha256 content seal only, "
              "not a trusted HMAC signature)")


def _config_issues(args, accuracy, mailto, gbooks, key_rows=None):
    """Human-facing startup gaps/advisories to show before the first run starts."""
    issues = []
    key_rows = key_rows or []
    accuracy_explicit = bool(args.accuracy or os.environ.get(ENV_ACCURACY))
    gbooks_row = next((row for row in key_rows if row.get("name") == "googlebooks"
                       and row.get("status") != "absent"), None)
    if not mailto:
        issues.append({
            "code": "mailto_missing",
            "summary": "contact email missing — Crossref / Europe PMC / Unpaywall may rate-limit more aggressively",
            "fix": (f"Set ${ENV_MAILTO} in .env, or pass `--mailto you@example.org` "
                    "when starting the run."),
        })
    if not gbooks and not gbooks_row:
        issues.append({
            "code": "gbooks_missing",
            "summary": "Google Books API key missing — book preview / snippet retrieval is disabled",
            "fix": (f"Set ${ENV_GBOOKS} in .env, or configure it with "
                    f"`{run_command('configure')}`."),
        })
    if not accuracy_explicit:
        issues.append({
            "code": "accuracy_defaulted",
            "summary": f"verification regime not explicitly selected — defaulting to `{accuracy}`",
            "fix": (f"Pass `--accuracy <maximum|maximum_fallback|standard|abstract|standard_web>` "
                    f"or set ${ENV_ACCURACY} in .env."),
        })
    if not _signing.key_present():
        issues.append({
            "code": "signing_missing",
            "summary": "trusted HMAC is not enabled in this process — reports have only a sha256 content seal",
            "fix": ("No action is required for a standalone SHA-256-sealed run. "
                    "To require trusted HMAC, configure the protected trusted Hook/CI process "
                    "with a signing key; keep that key out of .env and the run process."),
            "guidance_label": "note",
            "blocking": False,
        })
    for row in key_rows:
        status = row.get("status")
        label = row.get("label") or row.get("name")
        env_name = row.get("env") or "API key"
        detail = _startup_preflight.describe_row(row)
        if status == "invalid_key":
            issues.append({
                "code": f"{row['name']}_invalid",
                "summary": f"{label} API key is invalid — {detail}",
                "fix": f"Replace ${env_name} with a valid key, then re-run the startup check.",
            })
        elif status == "quota_or_rate_limited":
            issues.append({
                "code": f"{row['name']}_quota",
                "summary": f"{label} API key is currently quota/rate limited — {detail}",
                "fix": (f"Wait for quota reset or replace ${env_name}; pass --proceed to run "
                        "without that optional tier."),
            })
        elif status == "auth_rejected_unknown":
            issues.append({
                "code": f"{row['name']}_auth_rejected",
                "summary": f"{label} API key was rejected by the server — {detail}",
                "fix": (f"Check whether ${env_name} is correct and authorised for this API; "
                        "then re-run the startup check."),
            })
        elif status == "disabled_invalid_cached":
            issues.append({
                "code": f"{row['name']}_disabled_cached",
                "summary": f"{label} API key remains disabled - {detail}",
                "fix": (f"Update ${env_name} in the env to a different value; the preflight "
                        "will automatically re-enable the API check on the next run."),
            })
    return issues


def _print_startup_check(issues):
    if not issues:
        print("Startup check complete — no missing optional setup detected")
        return
    print("Startup check — review these before the first run")
    for item in issues:
        print(f"- {item['summary']}")
        print(f"  {item.get('guidance_label', 'how to fix')}: {item['fix']}")


def _gbooks_config_status(gbooks: bool, key_rows: list[dict]) -> str:
    row = next((r for r in key_rows if r.get("name") == "googlebooks"), None)
    status = row.get("status") if row else None
    if status == "invalid_key":
        return "invalid (book preview tier disabled for this and future runs until the env value changes)"
    if status == "disabled_invalid_cached":
        return "disabled (previously invalid key cached; update env value to re-enable)"
    if gbooks:
        return "present (book preview tier enabled)"
    return "absent (book preview tier disabled)"


def _module(args, capture=True):
    """Run `python run.py <...>` from the repo root. Returns (rc, stdout, stderr)."""
    cmd = application_argv(*args, source_root=PKG_DIR)
    cwd = str(user_data_root()) if is_frozen() else PKG_DIR
    p = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")
