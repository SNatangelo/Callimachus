# core/verify/backends/errors.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed exceptions for backend error handling.

Used by ``_chat_transport.py`` and the parallel-verify orchestrator to
distinguish rate limits (retryable), auth failures (permanent disable),
transient network issues, and configuration errors.
"""

from __future__ import annotations

import json


class BackendError(RuntimeError):
    """Base class for all backend errors.

    Attributes:
        backend: Name of the backend that produced the error (e.g. "anthropic").
        retryable: Whether this request can be retried after backoff.
        retry_after: Suggested delay in seconds before retrying (for rate limits).
    """

    def __init__(
        self,
        message: str,
        *,
        backend: str = "",
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.backend = backend
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimitError(BackendError):
    """HTTP 429 — retry after exponential backoff."""

    def __init__(
        self,
        message: str = "Rate limited",
        *,
        backend: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message, backend=backend, retryable=True, retry_after=retry_after
        )


class AuthError(BackendError):
    """Definitive invalid/expired credential — permanently disable the key."""

    def __init__(
        self, message: str = "Authentication failed", *, backend: str = ""
    ) -> None:
        super().__init__(message, backend=backend, retryable=False)


class LaneError(BackendError):
    """The selected model/key lane is unavailable while the key may be valid."""

    def __init__(
        self, message: str = "Model lane unavailable", *, backend: str = ""
    ) -> None:
        super().__init__(message, backend=backend, retryable=False)


class BackendTimeoutError(BackendError):
    """The provider-visible attempt exceeded its transport timeout."""

    def __init__(
        self, message: str = "Backend request timed out", *, backend: str = ""
    ) -> None:
        super().__init__(message, backend=backend, retryable=True)


class TransientError(BackendError):
    """HTTP 5xx or network error — retry after short backoff."""

    def __init__(
        self,
        message: str = "Transient server or network error",
        *,
        backend: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message, backend=backend, retryable=True, retry_after=retry_after
        )


class ConfigError(BackendError):
    """Missing key, missing model, unregistered backend — do not retry."""

    def __init__(
        self, message: str = "Backend not configured", *, backend: str = ""
    ) -> None:
        super().__init__(message, backend=backend, retryable=False)


# ---------------------------------------------------------------------------
# Subprocess/CLI backends (claude_cli, codex_cli, gemini_cli, host) don't get a
# structured error like an HTTP status code — just an exit code and stderr text.
# These helpers classify that into the same typed hierarchy so the orchestrator
# applies one consistent retry policy regardless of backend
# transport.
# ---------------------------------------------------------------------------

# Keyword heuristics for recognizing an auth/login failure in CLI stderr output.
_CLI_AUTH_HINTS = (
    "not logged in", "please log in", "please run", "login required",
    "unauthorized", "authentication", "api key", "permission denied",
    "invalid api key", "401", "403",
)


# Known keys carrying an integer status / a human-readable message in the
# JSON some CLIs (e.g. `claude --output-format json`) write to stdout on
# failure, e.g. {"is_error":true,"api_error_status":429,"result":"..."}.
_STDOUT_STATUS_KEYS = ("api_error_status", "status", "code")
_STDOUT_MESSAGE_KEYS = ("result", "error", "message", "detail")


def _classify_stdout_status(status: int | None, msg: str, backend: str) -> BackendError | None:
    """Map a status extracted from CLI stdout JSON to a typed BackendError.

    Returns None if `status` doesn't tell us anything actionable (caller
    falls back to the stderr heuristic in that case).
    """
    snippet = (msg or "")[:300]
    text = f"`{backend}` CLI failed: {snippet}" if snippet else f"`{backend}` CLI failed"
    low = (msg or "").lower()
    if status == 429:
        return RateLimitError(text, backend=backend, retry_after=None)
    if status == 529 or "overload" in low or "overloaded" in low:
        return TransientError(text, backend=backend)
    if status == 401:
        return AuthError(text, backend=backend)
    if status == 403:
        return (
            LaneError(text, backend=backend)
            if _single_physical_attempt_active()
            else AuthError(text, backend=backend)
        )
    if status in (404, 400, 422):
        return ConfigError(text, backend=backend)
    if status is not None and 500 <= status < 600:
        return TransientError(text, backend=backend)
    if status is not None and 400 <= status < 500:
        return ConfigError(text, backend=backend)
    return None


def cli_failure_error(
    backend: str, returncode: int, stderr: str, *, stdout: str | None = None
) -> BackendError:
    """Classify a nonzero CLI exit code into a typed :class:`BackendError`.

    CLI backends don't give a structured error the way HTTP status codes do.
    Some (e.g. `claude`) write a JSON error payload to stdout instead of
    stderr on failure (rc!=0, stderr empty); when `stdout` is given and
    parses to a dict with a recognizable status, that's classified first
    (rate limit / auth / transient / config). Otherwise this falls back to
    keyword heuristics on stderr to catch the common auth/login-failure case
    (permanently disable the backend); everything else is treated as a
    retryable transient error, mirroring how HTTP 5xx is handled.
    """
    if stdout:
        try:
            obj = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            status = None
            for key in _STDOUT_STATUS_KEYS:
                val = obj.get(key)
                if isinstance(val, int):
                    status = val
                    break
            msg = ""
            for key in _STDOUT_MESSAGE_KEYS:
                val = obj.get(key)
                if isinstance(val, str) and val:
                    msg = val
                    break
            classified = _classify_stdout_status(status, msg, backend)
            if classified is not None:
                return classified

    snippet = (stderr or "")[:300]
    msg = f"`{backend}` CLI failed (rc={returncode}): {snippet}"
    low = (stderr or "").lower()
    ambiguous_forbidden = "403" in low or "permission denied" in low
    definitive_auth = any(
        hint in low
        for hint in ("invalid api key", "unauthorized", "401", "not logged in")
    )
    if (
        ambiguous_forbidden
        and not definitive_auth
        and _single_physical_attempt_active()
    ):
        return LaneError(msg, backend=backend)
    if any(hint in low for hint in _CLI_AUTH_HINTS):
        return AuthError(msg, backend=backend)
    return TransientError(msg, backend=backend)


def cli_timeout_error(backend: str, timeout: float) -> BackendTimeoutError:
    """A CLI subprocess exceeded its timeout — always retryable."""
    return BackendTimeoutError(
        f"`{backend}` CLI timed out after {timeout}s", backend=backend
    )


def _single_physical_attempt_active() -> bool:
    try:
        from ._chat_transport import single_physical_attempt_active
        return single_physical_attempt_active()
    except ImportError:
        return False
