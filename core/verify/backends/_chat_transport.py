# core/verify/backends/_chat_transport.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared OpenAI-compatible Chat Completions helper.

Used by every backend that speaks ``POST /chat/completions`` with Bearer auth
and expects ``choices[0].message.content`` in the response — GLM, Mistral,
OpenCode Zen, OpenAI-compatible, and (with small wrappers) OpenRouter.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from core.verify.backends.errors import (
    AuthError,
    BackendError,
    BackendTimeoutError,
    ConfigError,
    LaneError,
    RateLimitError,
    TransientError,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract C — one unified timeout knob per backend family. Every HTTP/subprocess
# backend reads these instead of hardcoding its own 180/300/600 literal.
# ---------------------------------------------------------------------------
ENV_LLM_TIMEOUT = "CITATION_VERIFIER_LLM_TIMEOUT"
ENV_LLM_TOOLS_TIMEOUT = "CITATION_VERIFIER_LLM_TOOLS_TIMEOUT"
ENV_LLM_REASONING = "CITATION_VERIFIER_REASONING"
ENV_LLM_REASONING_EFFORT = "CITATION_VERIFIER_REASONING_EFFORT"
_DEFAULT_LLM_TIMEOUT = 300
_DEFAULT_LLM_TOOLS_TIMEOUT = 600

# Retry-After is attacker/server-controlled input; never sleep longer than this
# even if a server asks us to.
_MAX_RETRY_AFTER = 300

_TRACE_CONTEXT: ContextVar[dict] = ContextVar("llm_trace_context", default={})
_SINGLE_PHYSICAL_ATTEMPT: ContextVar[bool] = ContextVar("single_physical_attempt", default=False)
_CREDENTIAL_OVERRIDES: ContextVar[dict[str, str | None]] = ContextVar(
    "credential_overrides", default={}
)
_REASONING_OVERRIDES: ContextVar[dict[str, str]] = ContextVar(
    "reasoning_overrides", default={}
)
_RAW_HTTP_ATTEMPT_BUDGET: dict[str, int] | None = None
_RAW_HTTP_ATTEMPT_BUDGET_TOKEN: object | None = None
_RAW_HTTP_ATTEMPT_BUDGET_LOCK = threading.Lock()


def reasoning_mode() -> str:
    value = (
        _REASONING_OVERRIDES.get().get("mode")
        or os.environ.get(ENV_LLM_REASONING)
        or "auto"
    ).strip().lower()
    return value if value in {"auto", "on", "off"} else "auto"


def reasoning_effort() -> str:
    value = (
        _REASONING_OVERRIDES.get().get("effort")
        or os.environ.get(ENV_LLM_REASONING_EFFORT)
        or "medium"
    ).strip().lower()
    return value if value in {"low", "medium", "high", "max", "xhigh"} else "medium"


def openai_reasoning_fields(
    model: str | None = None,
    *,
    structured_output: bool = False,
) -> dict:
    """Return provider-compatible thinking controls."""
    mode = reasoning_mode()
    is_deepseek_v4 = (model or "").strip().lower().startswith("deepseek-v4-")
    if is_deepseek_v4:
        if mode == "off":
            return {"thinking": {"type": "disabled"}}
        if mode == "on":
            effort = reasoning_effort()
            effort = {"low": "high", "medium": "high", "xhigh": "max"}.get(effort, effort)
            return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
        if structured_output:
            return {"thinking": {"type": "disabled"}}
        return {}
    if mode in {"auto", "off"}:
        return {}
    return {"reasoning_effort": reasoning_effort() if mode == "on" else "none"}


def set_trace_context(**values):
    """Set metadata for the raw HTTP attempts nested in one logical call."""
    current = dict(_TRACE_CONTEXT.get() or {})
    current.update({key: value for key, value in values.items() if value is not None})
    return _TRACE_CONTEXT.set(current)


def reset_trace_context(token) -> None:
    _TRACE_CONTEXT.reset(token)


@contextmanager
def single_physical_attempt():
    """Disable local retry/jitter for one scheduler-owned provider call."""
    token = _SINGLE_PHYSICAL_ATTEMPT.set(True)
    try:
        yield
    finally:
        _SINGLE_PHYSICAL_ATTEMPT.reset(token)


def single_physical_attempt_active() -> bool:
    return _SINGLE_PHYSICAL_ATTEMPT.get()


@contextmanager
def reasoning_override(mode: str | None, effort: str | None):
    """Apply reasoning controls to one call context without mutating the environment."""
    current = dict(_REASONING_OVERRIDES.get())
    if mode is not None:
        current["mode"] = mode
    if effort is not None:
        current["effort"] = effort
    token = _REASONING_OVERRIDES.set(current)
    try:
        yield
    finally:
        _REASONING_OVERRIDES.reset(token)


@contextmanager
def credential_override(env_name: str, secret: str | None):
    """Override one backend credential in this call context only."""
    if not isinstance(env_name, str) or not env_name:
        if secret is not None:
            raise ValueError("credentialless backend received a secret")
        yield
        return
    current = dict(_CREDENTIAL_OVERRIDES.get())
    current[env_name] = secret
    token = _CREDENTIAL_OVERRIDES.set(current)
    try:
        yield
    finally:
        _CREDENTIAL_OVERRIDES.reset(token)


def credential_value(env_name: str) -> str | None:
    """Resolve a call-local credential before the process environment value."""
    overrides = _CREDENTIAL_OVERRIDES.get()
    if env_name in overrides:
        return overrides[env_name]
    return os.environ.get(env_name)


def set_raw_http_attempt_budget(maximum: int):
    """Install a process-scoped cap on provider-visible HTTP attempts.

    The shared transport normally retries transient failures up to five times.
    Audit experiments with an explicit data-transfer ceiling can install this
    budget so every ``urlopen`` reservation, including transport retries, draws
    from one fail-closed counter across worker threads. Only one active budget
    scope is permitted at a time.
    """
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise ValueError("raw HTTP attempt maximum must be a positive integer")
    state = {"maximum": maximum, "attempted": 0}
    token = object()
    global _RAW_HTTP_ATTEMPT_BUDGET, _RAW_HTTP_ATTEMPT_BUDGET_TOKEN
    with _RAW_HTTP_ATTEMPT_BUDGET_LOCK:
        if _RAW_HTTP_ATTEMPT_BUDGET is not None:
            raise RuntimeError("raw HTTP attempt budget scope is already active")
        _RAW_HTTP_ATTEMPT_BUDGET = state
        _RAW_HTTP_ATTEMPT_BUDGET_TOKEN = token
    return token, state


def reset_raw_http_attempt_budget(token) -> None:
    global _RAW_HTTP_ATTEMPT_BUDGET, _RAW_HTTP_ATTEMPT_BUDGET_TOKEN
    with _RAW_HTTP_ATTEMPT_BUDGET_LOCK:
        if (
            _RAW_HTTP_ATTEMPT_BUDGET is None
            or token is not _RAW_HTTP_ATTEMPT_BUDGET_TOKEN
        ):
            raise RuntimeError("raw HTTP attempt budget reset token is invalid")
        _RAW_HTTP_ATTEMPT_BUDGET = None
        _RAW_HTTP_ATTEMPT_BUDGET_TOKEN = None


def restore_raw_http_attempt_budget(token, attempted: int) -> None:
    """Advance an active budget to a journaled stable-boundary count."""
    if not isinstance(attempted, int) or isinstance(attempted, bool) or attempted < 0:
        raise ValueError("raw HTTP attempted count must be a non-negative integer")
    with _RAW_HTTP_ATTEMPT_BUDGET_LOCK:
        state = _RAW_HTTP_ATTEMPT_BUDGET
        if state is None or token is not _RAW_HTTP_ATTEMPT_BUDGET_TOKEN:
            raise RuntimeError("raw HTTP attempt budget restore token is invalid")
        if attempted < state["attempted"]:
            raise ValueError("raw HTTP attempted count cannot decrease")
        if attempted > state["maximum"]:
            raise ValueError("raw HTTP attempted count exceeds maximum")
        state["attempted"] = attempted


def _reserve_raw_http_attempt() -> None:
    with _RAW_HTTP_ATTEMPT_BUDGET_LOCK:
        state = _RAW_HTTP_ATTEMPT_BUDGET
        if state is None:
            return
        if state["attempted"] >= state["maximum"]:
            raise BackendError(
                "raw HTTP attempt budget exhausted before network I/O",
                retryable=False,
            )
        state["attempted"] += 1


def _trace_url(url: str) -> str:
    """Keep endpoint identity while dropping query/fragment credentials."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<invalid-url>"


# --------------------------------------------------------------------------- #
#  Raw HTTP attempt telemetry. A verify run points this at its current SQLite  #
#  run via configure_debug_trace(). Secrets are never recorded: _json_post     #
#  stores endpoint identity and attempt metadata, never credentials/headers.    #
# --------------------------------------------------------------------------- #
_HTTP_TRACE_RUN_DIR: str | None = None


def configure_debug_trace(run_dir: str | None, enabled: bool) -> None:
    """Point raw HTTP tracing at *run_dir* (or disable it)."""
    global _HTTP_TRACE_RUN_DIR
    _HTTP_TRACE_RUN_DIR = run_dir if (enabled and run_dir) else None


def _emit_raw_attempt(payload: dict) -> None:
    """Append one raw HTTP attempt to the current run database."""
    run_dir = _HTTP_TRACE_RUN_DIR
    if not run_dir:
        return
    repository = None
    try:
        from core.infra.db import RunRepository

        repository = RunRepository.open(run_dir)
        repository.append_http_attempt(payload)
    except Exception as exc:
        _logger.debug("could not write raw HTTP attempt trace: %s", exc)
    finally:
        if repository is not None:
            repository.close()


def _read_timeout_env(env_name: str, default: int) -> int:
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        val = int(float(raw))
    except ValueError:
        return default
    return val if val > 0 else default


def llm_timeout() -> int:
    """Standard HTTP/subprocess timeout (seconds). Env ``CITATION_VERIFIER_LLM_TIMEOUT``."""
    return _read_timeout_env(ENV_LLM_TIMEOUT, _DEFAULT_LLM_TIMEOUT)


def llm_tools_timeout() -> int:
    """Timeout for tool-equipped calls (web research, ``host`` with allow_tools).

    Env ``CITATION_VERIFIER_LLM_TOOLS_TIMEOUT``.
    """
    return _read_timeout_env(ENV_LLM_TOOLS_TIMEOUT, _DEFAULT_LLM_TOOLS_TIMEOUT)


def _http_error_to_backend_error(
    exc: urllib.error.HTTPError, backend: str = ""
) -> BackendError:
    """Convert an :class:`urllib.error.HTTPError` into a typed
    :class:`BackendError` subclass based on the HTTP status code."""
    code = getattr(exc, "code", None)
    retry_after = None
    try:
        retry_after = int((getattr(exc, "headers", {}) or {}).get("Retry-After", "0")) or None
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None and not _SINGLE_PHYSICAL_ATTEMPT.get():
        retry_after = min(retry_after, _MAX_RETRY_AFTER)

    msg = str(exc)
    if code == 429:
        return RateLimitError(msg, backend=backend, retry_after=retry_after)
    if code == 401:
        return AuthError(msg, backend=backend)
    if code == 403:
        if _SINGLE_PHYSICAL_ATTEMPT.get():
            return LaneError(msg, backend=backend)
        return AuthError(msg, backend=backend)
    if isinstance(code, int) and 500 <= code < 600:
        return TransientError(msg, backend=backend, retry_after=retry_after)
    return BackendError(msg, backend=backend, retryable=False)


def _backoff_delay(attempt: int, retry_after: int | None = None) -> float:
    """Exponential backoff with jitter, capped by *retry_after* when the server gave one.

    Base delay doubles per attempt (5s, 10s, 20s, ...) capped at 60s, or the
    server's Retry-After (itself capped at ``_MAX_RETRY_AFTER``) when present.
    A random 0-25% jitter is added so concurrent workers don't retry in lockstep.
    """
    base = retry_after if retry_after else min(60, 5 * (2 ** attempt))
    return base + random.uniform(0, base * 0.25)


def _json_post(url: str, body: dict, headers: dict[str, str], timeout: int | None = None) -> dict:
    """POST *body* as JSON to *url* with 5-retry exponential back-off on
    transient errors (429 / 5xx / network errors).

    Raises :class:`RateLimitError`, :class:`AuthError`, or
    :class:`TransientError` instead of raw :class:`urllib.error.HTTPError` /
    :class:`urllib.error.URLError` so callers can distinguish error categories
    without inspecting HTTP codes or socket exceptions.
    """
    if timeout is None:
        timeout = llm_timeout()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    last_exc: Exception | None = None
    attempts = 1 if _SINGLE_PHYSICAL_ATTEMPT.get() else 5
    for attempt in range(attempts):
        _reserve_raw_http_attempt()
        started = time.time()
        trace = dict(_TRACE_CONTEXT.get() or {})
        trace.update({
            "network_attempt_id": uuid.uuid4().hex,
            "url": _trace_url(url),
            "attempt": attempt + 1,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result = json.loads(r.read().decode("utf-8"))
                _emit_raw_attempt({
                    **trace, "outcome": "response",
                    "status": getattr(r, "status", None) or getattr(r, "code", None),
                    "duration_ms": round((time.time() - started) * 1000, 3),
                    **_prompt_cache_usage(result),
                })
                return result
        except urllib.error.HTTPError as e:
            last_exc = e
            code = getattr(e, "code", None)
            _emit_raw_attempt({
                **trace, "outcome": "http_error", "status": code,
                "retryable": code in (429, 500, 502, 503, 504) and attempt < attempts - 1,
                "duration_ms": round((time.time() - started) * 1000, 3),
            })
            # Auth errors — never retry
            if code in (401, 403):
                raise _http_error_to_backend_error(e)
            # Transient errors — retry unless exhausted
            if code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise _http_error_to_backend_error(e)
            retry_after = 0
            try:
                retry_after = int((e.headers or {}).get("Retry-After", "0"))
            except (TypeError, ValueError):
                retry_after = 0
            retry_after = min(retry_after, _MAX_RETRY_AFTER) if retry_after and not _SINGLE_PHYSICAL_ATTEMPT.get() else retry_after
            delay = _backoff_delay(attempt, retry_after or None)
            _logger.warning(
                "backend HTTP %s on attempt %d/5, retrying in %.1fs", code, attempt + 1, delay
            )
            time.sleep(delay)
        except ValueError as e:
            # A 2xx response with invalid JSON is still a completed raw
            # attempt; preserve the historical exception while recording only
            # its type (never the response body).
            _emit_raw_attempt({
                **trace, "outcome": "invalid_response", "error_type": type(e).__name__,
                "retryable": False,
                "duration_ms": round((time.time() - started) * 1000, 3),
            })
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            last_exc = e
            _emit_raw_attempt({
                **trace, "outcome": "network_error", "error_type": type(e).__name__,
                "retryable": attempt < attempts - 1,
                "duration_ms": round((time.time() - started) * 1000, 3),
            })
            if attempt == attempts - 1 and isinstance(
                e, (socket.timeout, TimeoutError)
            ):
                raise BackendTimeoutError(
                    f"backend request timed out: {type(e).__name__}"
                ) from e
            if attempt == attempts - 1:
                raise TransientError(f"network error calling backend: {e}") from e
            delay = _backoff_delay(attempt)
            _logger.warning(
                "backend network error on attempt %d/5 (%s), retrying in %.1fs",
                attempt + 1, e, delay,
            )
            time.sleep(delay)
    if last_exc is not None:
        if isinstance(last_exc, urllib.error.HTTPError):
            raise _http_error_to_backend_error(last_exc)
        raise TransientError(f"network error calling backend: {last_exc}") from last_exc
    raise RuntimeError("request failed with no response")


def _prompt_cache_usage(result: object) -> dict[str, int]:
    if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
        return {}
    usage = result["usage"]
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if type(hit) is not int or hit < 0 or type(miss) is not int or miss < 0:
        return {}
    return {
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }


def get_json_post():
    """Return the _json_post function to use.

    Indirection kept so a test can monkeypatch ``_chat_transport._json_post`` and
    have every backend module observe it (they all call ``get_json_post()(...)``).
    """
    return _json_post


def resolve_model(model: str | None, env_model: str | None) -> str:
    """Resolve a model name using the standard backend priority order.

    Priority (first wins):
        1. *model* — the value passed by the caller (e.g. an explicit ``--model``)
        2. Per-backend model env var  (e.g. ``MISTRAL_MODEL``)
        3. ``CITATION_VERIFIER_MODEL`` (global default)

    An explicit model always wins; the env vars are fallbacks for when the
    caller didn't pass one. Every backend should use this same resolution.
    """
    if model:
        return model
    resolved = (os.environ.get(env_model or "") or "").strip()
    if resolved:
        return resolved
    resolved = (os.environ.get("CITATION_VERIFIER_MODEL") or "").strip()
    if resolved:
        return resolved
    raise ConfigError(
        f"Model not set: configure {env_model or 'CITATION_VERIFIER_MODEL'}"
        f" or CITATION_VERIFIER_MODEL, or pass --model",
    )


def call_chat_completions(
    system: str,
    user: str,
    model: str | None,
    max_tokens: int,
    *,
    spec,
) -> str:
    """OpenAI-compatible ``/chat/completions`` call driven by *spec*.

    Reads the API key from ``spec.env_key``, resolves the model via
    :func:`resolve_model`, and POSTs to ``{spec.base_url}/chat/completions``.
    ``temperature`` is pinned to 0 for deterministic verifier output.
    """
    key = credential_value(spec.env_key)
    if not key:
        label = spec.name.capitalize()
        raise ConfigError(
            f"{spec.env_key} not set - cannot use the {label} backend",
            backend=spec.name,
        )

    resolved = resolve_model(model, spec.env_model)

    url = f"{spec.base_url}/chat/completions"
    _jp = get_json_post()
    data = _jp(
        url,
        {
            "model": resolved,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            **(openai_reasoning_fields(resolved) if spec.supports_reasoning else {}),
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=llm_timeout(),
    )
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()
    return ""
