# core/search/transport.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared HTTP transport for search backends."""

from __future__ import annotations

import logging
import contextlib
import urllib.error
import urllib.request
import uuid
import threading

try:
    import core.fetch.hosts as _hosts_mod
    import core.fetch.transport.http_headers as _http_headers_mod
except ImportError:  # flat-layout import fallback, as elsewhere in the codebase
    import hosts as _hosts_mod
    import http_headers as _http_headers_mod

_TIMEOUT = 20
_LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def capture_observations():
    """Collect non-secret outcomes for one router invocation."""
    previous = getattr(_LOCAL, "observations", None)
    observations: list[dict] = []
    _LOCAL.observations = observations
    try:
        yield observations
    finally:
        _LOCAL.observations = previous


_LOCAL = threading.local()


def _observe(status: int | None, error: BaseException | None) -> None:
    observations = getattr(_LOCAL, "observations", None)
    if observations is None:
        return
    outcome = "response" if error is None else (
        "http_error" if isinstance(error, urllib.error.HTTPError) else "network_error"
    )
    observations.append({
        "transport_outcome": outcome,
        # A response body can fail after headers arrive.  Preserve the same
        # transport contract used by credential telemetry: network failures
        # do not carry a misleading HTTP status.
        "http_status": status if outcome != "network_error" else None,
    })


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             timeout: int = _TIMEOUT, run_dir: str | None = None,
             credential: tuple[str, str] | None = None) -> tuple[int, bytes] | None:
    """Make one rate-limited request; return ``None`` if it did not complete."""
    _hosts_mod._shared_host_limiter().acquire_for_url(url)
    base = _http_headers_mod.request_headers(
        url=url, accept="application/json,text/html,*/*", profile="document")
    base.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=base,
                                 method="POST" if data is not None else "GET")
    status = None
    error = None
    try:
        with _http_headers_mod.open_request(req, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return status, response.read()
    except urllib.error.HTTPError as exc:
        status, error = exc.code, exc
        return None
    except Exception as exc:
        error = exc
        return None
    finally:
        _observe(status, error)
        if run_dir and credential:
            repo = None
            try:
                outcome = "response" if error is None else (
                    "http_error" if isinstance(error, urllib.error.HTTPError)
                    else "network_error"
                )
                from core.infra.db import RunRepository
                repo = RunRepository.open(run_dir)
                repo.append_credential_transport_observation({
                    "observation_id": str(uuid.uuid4()),
                    "provider": credential[0], "env_name": credential[1],
                    "channel": "search",
                    "outcome": outcome,
                    # A body read can fail after response headers were received.
                    # It remains a network error, whose persisted contract has no
                    # HTTP status, rather than an invalid mixed outcome.
                    "http_status": status if outcome != "network_error" else None,
                })
            except Exception as exc:
                # Search remains best-effort, but a telemetry write failure is
                # never silently hidden from the audit log.
                _LOG.warning("Search credential telemetry unavailable: %s", type(exc).__name__)
            finally:
                if repo is not None:
                    try:
                        repo.close()
                    except Exception as exc:
                        _LOG.warning("Search credential telemetry close failed: %s", type(exc).__name__)


def _answered(status: int | None) -> bool:
    """Whether a status means the engine actually served results.

    HTTP 202 is excluded deliberately: DuckDuckGo uses it for an anti-bot
    interstitial, which must be treated as refusal rather than an empty answer.
    """
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        return False
    return 200 <= code < 300 and code != 202
