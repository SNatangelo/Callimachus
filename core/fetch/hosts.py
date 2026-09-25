#!/usr/bin/env python3
# core/fetch/hosts.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Host configuration, rate-limiting, and fetch budget helpers."""

from __future__ import annotations

import os

try:
    from core.fetch.transport import host_limiter as _host_limiter
    from core.resolve import provider_config as _provider_config
    from core.resolve import providers as _provider_registry
    from core.resolve import sources as _sources
except ImportError:
    import host_limiter as _host_limiter
    from core.resolve import provider_config as _provider_config
    from core.resolve import providers as _provider_registry
    from core.resolve import sources as _sources


# ---------------------------------------------------------------------------
# Environment-driven constants
# ---------------------------------------------------------------------------

ENV_FETCH_PDF_TIMEOUT = "CITATION_VERIFIER_FETCH_PDF_TIMEOUT"
DEFAULT_TIMEOUT_PDF = 45
TIMEOUT_PDF = DEFAULT_TIMEOUT_PDF  # fallback constant; live timeout comes from _pdf_timeout()
ENV_FETCH_BUDGET = "CITATION_VERIFIER_FETCH_BUDGET_S"
DEFAULT_FETCH_BUDGET_S = 120
MAX_FETCH_CANDIDATES = 16
MAX_OA_ALTERNATE_CANDIDATES = 3
ENV_OA_ALTERNATES = "CITATION_VERIFIER_OA_ALTERNATES"
ENV_FETCH_CANDIDATE_WORKERS = "CITATION_VERIFIER_FETCH_CANDIDATE_WORKERS"
DEFAULT_FETCH_CANDIDATE_WORKERS = 3
ENV_FETCH_HOST_CONCURRENCY = "CITATION_VERIFIER_FETCH_HOST_CONCURRENCY"
DEFAULT_FETCH_HOST_CONCURRENCY = 2
ENV_FETCH_HOST_MIN_INTERVAL = "CITATION_VERIFIER_FETCH_HOST_MIN_INTERVAL"
DEFAULT_FETCH_HOST_MIN_INTERVAL = 0.0


# ---------------------------------------------------------------------------
# Host / limiter helpers
# ---------------------------------------------------------------------------

def _shared_host_limiter():
    return _host_limiter.get_shared_limiter()


def _trusted_host_config() -> dict[str, object]:
    return _provider_config.trusted_hosts()


def _preprint_marker_config() -> dict[str, tuple[str, ...]]:
    return _provider_config.preprint_markers()


def _trusted_fetch_host(host: str) -> bool:
    if not host:
        return False
    config = _trusted_host_config()
    suffixes = config.get("canonical_hosts") or ()
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
        return True
    if any(marker in host for marker in (config.get("host_markers") or ())):
        return True
    return any(host.endswith(tld) for tld in (config.get("host_tlds") or ()))


def _challenge_prone_host(host: str) -> bool:
    if not host:
        return False
    suffixes = _trusted_host_config().get("challenge_hosts") or ()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


# ---------------------------------------------------------------------------
# Budget / worker count / timeouts
# ---------------------------------------------------------------------------

def _candidate_worker_count(environ: dict[str, str] | None = None) -> int:
    env = environ or os.environ
    raw = str(env.get(ENV_FETCH_CANDIDATE_WORKERS) or "").strip()
    if not raw:
        return DEFAULT_FETCH_CANDIDATE_WORKERS
    try:
        return max(1, min(MAX_FETCH_CANDIDATES, int(raw)))
    except ValueError:
        return DEFAULT_FETCH_CANDIDATE_WORKERS


def _pdf_timeout(environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_FETCH_PDF_TIMEOUT) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_PDF
    try:
        return max(5, min(int(raw), 300))
    except ValueError:
        return DEFAULT_TIMEOUT_PDF


def _fetch_budget_seconds(environ: dict | None = None) -> float:
    """Per-reference fetch wall-clock budget in seconds; 0 (or invalid) = unlimited."""
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_FETCH_BUDGET) or "").strip()
    if not raw:
        return float(DEFAULT_FETCH_BUDGET_S)
    try:
        v = float(raw)
        return v if v > 0 else 0.0
    except ValueError:
        return float(DEFAULT_FETCH_BUDGET_S)


def _oa_alternates_enabled(environ: dict | None = None) -> bool:
    """Whether the stage-2 "oa_alternate" second network round is allowed to run.

    Default is enabled; set CITATION_VERIFIER_OA_ALTERNATES=0/false/no to
    restore the pre-staged-fallback behaviour of never consulting alternates.
    """
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_OA_ALTERNATES) or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Public fetch configuration
# ---------------------------------------------------------------------------

def fetch_host_concurrency_limit(environ: dict[str, str] | None = None) -> int:
    env = environ or os.environ
    raw = str(env.get(ENV_FETCH_HOST_CONCURRENCY) or "").strip()
    if not raw:
        return DEFAULT_FETCH_HOST_CONCURRENCY
    try:
        return max(1, min(MAX_FETCH_CANDIDATES, int(raw)))
    except ValueError:
        return DEFAULT_FETCH_HOST_CONCURRENCY


def fetch_host_min_interval(environ: dict[str, str] | None = None) -> float:
    env = environ or os.environ
    raw = str(env.get(ENV_FETCH_HOST_MIN_INTERVAL) or "").strip()
    if not raw:
        return DEFAULT_FETCH_HOST_MIN_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FETCH_HOST_MIN_INTERVAL


# ---------------------------------------------------------------------------
# Provider status / preprint detection
# ---------------------------------------------------------------------------

def fetch_provider_status_rows(email: str | None = None) -> list[dict]:
    return _provider_registry.status_rows(email=email)


def _citation_declares_preprint(ref: dict, resolve_result: dict | None = None) -> bool:
    if _sources.ref_is_preprint(ref, resolve_result):
        return True
    raw = " ".join(
        str(ref.get(key) or "")
        for key in ("raw_entry", "url", "title")
    ).lower()
    return any(marker in raw for marker in (_preprint_marker_config().get("text_markers") or ()))
