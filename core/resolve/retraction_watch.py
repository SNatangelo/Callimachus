# core/resolve/retraction_watch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
retraction_watch.py — deterministic retraction lookup via the Retraction Watch
Database, downloaded as a public CSV and cached on disk.

The Retraction Watch Database is the most comprehensive registry of retracted
publications. It is made available by The Center for Scientific Integrity and
mirrored by Crossref Labs at:

  https://api.labs.crossref.org/data/retractionwatch

The CSV is cached locally (default 7-day TTL) and loaded into a set of
normalised DOIs for O(1) lookup.

Usage:
    from core.resolve.retraction_watch import is_retracted, load

    retracted_set = load("user@example.com")
    if is_retracted("10.1000/abcd.1234", retracted_set):
        print("retracted")
"""
from __future__ import annotations

import csv
import http.client
import io
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from core.fetch.storage import content_store as _content_store
    from core.fetch.transport.http_headers import configured_user_agent, open_request, request_headers
except ImportError:  # direct execution
    import content_store as _content_store
    from http_headers import configured_user_agent, open_request, request_headers

CACHE_DIR = Path(_content_store.storage_root()) / "external_cache"
CACHE_FILE = CACHE_DIR / "retraction_watch.csv"
CACHE_TTL_DAYS = 7
RW_URL = "https://api.labs.crossref.org/data/retractionwatch"
TIMEOUT = 15  # seconds

_UA = configured_user_agent()
_loaded_set: set[str] | None = None
_loaded_key: str | None = None
_load_lock = threading.Lock()


# ---- cache helpers --------------------------------------------------------

def _cache_stale() -> bool:
    """True if the cache file is missing or older than CACHE_TTL_DAYS."""
    if not CACHE_FILE.is_file():
        return True
    age = time.time() - CACHE_FILE.stat().st_mtime
    return age > CACHE_TTL_DAYS * 86400


def _download_csv(mailto: str) -> None:
    """Download the Retraction Watch CSV from Crossref Labs to the cache file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{RW_URL}?mailto={urllib.parse.quote(mailto)}"
    headers = request_headers(url=url, accept="text/csv", profile="api")
    headers["User-Agent"] = _UA
    req = urllib.request.Request(url, headers=headers)
    try:
        from core.resolve import transport_telemetry

        started = time.monotonic()
        with transport_telemetry.physical_request(method="GET", url=url):
            try:
                with open_request(req, timeout=TIMEOUT) as r:
                    body = r.read()
                    transport_telemetry.record_attempt(
                        method="GET",
                        url=url,
                        attempt_number=1,
                        started=started,
                        status=r.status,
                    )
            except Exception as error:
                transport_telemetry.record_attempt(
                    method="GET",
                    url=url,
                    attempt_number=1,
                    started=started,
                    status=getattr(error, "code", None),
                    error=error,
                )
                raise
        CACHE_FILE.write_bytes(body)
    except (http.client.IncompleteRead, urllib.error.URLError, OSError, ValueError):
        # Network failure: if a stale cache exists, keep using it.
        # If no cache at all, the set stays empty (no false positives).
        pass


def _read_cached_set(path: Path) -> set[str] | None:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _load_csv(data)


def _load_csv(data: str) -> set[str]:
    """Parse CSV text and return a set of normalised DOIs.

    The Retraction Watch CSV has many columns. We look for the first column
    whose header contains 'doi' (case-insensitive).  Values are stored
    lowercased and stripped of any ``https://doi.org/`` or ``doi:`` prefix.
    Empty or whitespace-only cells are skipped.
    """
    do_set: set[str] = set()
    reader = csv.reader(io.StringIO(data))
    header = next(reader, None)
    if not header:
        return do_set

    # Locate the DOI column.
    doi_col = None
    for i, col in enumerate(header):
        if "doi" in col.strip().lower():
            doi_col = i
            break
    if doi_col is None:
        return do_set  # no DOI column found — return empty

    for row in reader:
        if doi_col >= len(row):
            continue
        raw = (row[doi_col] or "").strip()
        if not raw:
            continue
        doi = _normalise(raw)
        if doi:
            do_set.add(doi)
    return do_set


def _normalise(raw: str) -> str:
    """Normalise a DOI string: lowercase, strip URL/doi: prefixes."""
    doi = raw.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "doi.org/"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    doi = doi.strip("/")
    return doi


# ---- public API -----------------------------------------------------------

def load(mailto: str = "user@example.com") -> set[str]:
    """Return the cached set of retracted DOIs.

    The CSV is downloaded at most once per ``CACHE_TTL_DAYS`` days.  The
    result is kept in memory so repeated calls within the same session are
    free (and return the *same* set object).

    If the download fails and no cache exists, an empty set is returned so
    the caller never blocks on a transient network error.
    """
    global _loaded_set, _loaded_key

    # Keep the established hot path lock-free. The guarded check below is the
    # one that closes the cold/stale check-to-download race.
    if _loaded_set is not None and _loaded_key == mailto:
        return _loaded_set

    # Resolve workers can arrive concurrently before the in-memory result is
    # published. Hold one process-local flight across cache recheck, download,
    # parse, and publication so they observe one complete set object.
    with _load_lock:
        if _loaded_set is not None and _loaded_key == mailto:
            return _loaded_set

        if not _cache_stale():
            cached = _read_cached_set(CACHE_FILE)
            if cached is not None:
                _loaded_set = cached
                _loaded_key = mailto
                return _loaded_set

        _download_csv(mailto)
        if CACHE_FILE.is_file():
            _loaded_set = _read_cached_set(CACHE_FILE) or set()
        else:
            _loaded_set = set()
        _loaded_key = mailto
        return _loaded_set


def is_retracted(doi: str, retracted_set: set[str] | None = None) -> bool:
    """Return True if *doi* is in the Retraction Watch database.

    If *retracted_set* is None, the cached set is loaded on demand (requires
    a prior call to ``load()`` or a ``CITATION_VERIFIER_MAILTO`` env var).

    The DOI is normalised before lookup (lowercased, URL prefix stripped), so
    the caller does not need to pre-process.
    """
    if retracted_set is not None:
        return _normalise(doi) in retracted_set
    if _loaded_set is not None:
        return _normalise(doi) in _loaded_set
    # Lazy load: attempt with the env var, fall back to empty set.
    mailto = os.environ.get("CITATION_VERIFIER_MAILTO", "")
    s = load(mailto)
    return _normalise(doi) in s
