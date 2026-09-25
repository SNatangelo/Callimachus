#!/usr/bin/env python3
# core/fetch/storage/fetch_cache.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run-scoped caches for deterministic fetches."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

_MISSING = object()
_CACHE_SCHEMA_VERSION = 2
_DEFAULT_MAX_PERSIST_BODY_BYTES = 25 * 1024 * 1024
_ENV_MAX_PERSIST_BODY_BYTES = "CITATION_VERIFIER_FETCH_CACHE_MAX_BODY_BYTES"
_SENSITIVE_RESPONSE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "set-cookie",
}
_CHALLENGE_BODY = b"Just a moment... Enable JavaScript and cookies to continue"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _signed_url_expiry(url: str | None) -> datetime | None:
    """Return a signed URL's unambiguous expiry, otherwise ``None``.

    Only established AWS v4, AWS/CloudFront legacy, and Azure SAS parameter
    sets are recognized. Conflicting duplicate parameters and malformed or
    timezone-less values deliberately remain unclassified.
    """
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        values: dict[str, set[str]] = {}
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            values.setdefault(key.lower(), set()).add(value)
        if any(len(items) != 1 for items in values.values()):
            return None
        params = {key: next(iter(items)) for key, items in values.items()}

        if all(params.get(key) for key in ("x-amz-date", "x-amz-expires", "x-amz-signature")):
            issued = datetime.strptime(params["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc,
            )
            lifetime = int(params["x-amz-expires"])
            return issued + timedelta(seconds=lifetime) if lifetime >= 0 else None

        if (
            params.get("expires")
            and params.get("signature")
            and (params.get("awsaccesskeyid") or params.get("key-pair-id"))
        ):
            return datetime.fromtimestamp(int(params["expires"]), tz=timezone.utc)

        if all(params.get(key) for key in ("se", "sig", "sv")):
            expiry = datetime.fromisoformat(params["se"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                return None
            return expiry.astimezone(timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    return None


def _signed_url_is_expired(url: str | None, *, now: datetime | None = None) -> bool:
    expiry = _signed_url_expiry(url)
    if expiry is None:
        return False
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expiry <= current.astimezone(timezone.utc)


def _stale_signed_failure(result, *, now: datetime | None = None) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        status = int(result.get("status"))
    except (TypeError, ValueError):
        return False
    if 200 <= status < 300:
        return False
    return _signed_url_is_expired(result.get("url"), now=now)


def _expired_signed_result(url: str, expiry: datetime) -> dict:
    return {
        "url": url,
        "signed_url_expired": True,
        "signed_url_expires_at": expiry.isoformat(),
    }


def _host(url: str | None) -> str | None:
    if not url:
        return None
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return host or None


def _normalized_url(url: str) -> str:
    """Return the stable URL identity used for fetch de-duplication.

    Fragments never affect an HTTP response; host/scheme casing and default ports
    do not either.  Keep the path and query byte-for-byte otherwise: reordering a
    query can be semantically significant for repository download endpoints.
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        # An invalid authority is not safe to canonicalise.  Preserve it as a
        # distinct identity rather than accidentally merging requests.  Do not
        # copy possible userinfo into the persisted key.
        userinfo, separator, hostport = parsed.netloc.rpartition("@")
        authority = (
            f"credential-{hashlib.sha256(userinfo.encode('utf-8')).hexdigest()}@{hostport}"
            if separator else parsed.netloc
        )
        return urllib.parse.urlunsplit((
            parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, "",
        ))
    # Keep userinfo and www: both can select a different HTTP origin or
    # representation.  Userinfo is a credential, so key persistence retains
    # only a stable non-reversible identity component.
    userinfo, separator, _ = parsed.netloc.rpartition("@")
    authority = (
        f"credential-{hashlib.sha256(userinfo.encode('utf-8')).hexdigest()}@"
        if separator else ""
    )
    authority += f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or
                     (parsed.scheme.lower() == "http" and port == 80)):
        authority = f"{authority}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), authority, path, parsed.query, ""))


class FetchRunContext:
    """Shared fetch cache for one run.

    It deduplicates identical document/PDF requests across references.  Entries can
    also be reconstructed by a later phase of the same run.
    """

    def __init__(self, *, host_concurrency: int = 2, host_min_interval: float = 0.0,
                 max_persist_body_bytes: int | None = None):
        self._lock = threading.Lock()
        self._responses: dict[tuple[str, str, str, str], dict] = {}
        self._challenge_hosts: set[tuple[str, str, str]] = set()
        self._host_concurrency = max(1, int(host_concurrency))
        self._host_min_interval = max(0.0, float(host_min_interval))
        self._host_semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._host_next_allowed_at: dict[str, float] = {}
        self._provider_record_lookups: dict[str, object] = {}
        self._provider_work_lookups: dict[tuple[str, str, str], dict] = {}
        # Archive.org protects itself aggressively.  These facts deliberately
        # live only for this Fetch run: the shared limiter remains the durable
        # authority for host cooldowns across runs.
        self._internet_archive_item_lookups: set[tuple[str, str]] = set()
        self._archive_http_429_count = 0
        self._archive_circuit_open = False
        # This is deliberately process-local.  It records completed candidate
        # handling during Resolve inline repair so formal Fetch can continue the
        # ladder rather than parse the same terminal candidate again.
        self._terminal_candidates: dict[str, set[tuple[str, str, str, str]]] = {}
        # Unlike ``_terminal_candidates``, this ledger is populated only by
        # automatic Fetch.  A provider-discovery cooldown can retry the whole
        # reference in the same formal phase; candidates which already issued
        # a request must not be admitted and executed again on that retry.
        self._formal_fetch_completed_candidates: dict[
            str, set[tuple[str, str, str, str]]
        ] = {}
        self._formal_fetch_phase = False
        self._cache_dir: str | None = None
        self._telemetry_run_dir: str | None = None
        self._persistence_enabled = True
        if max_persist_body_bytes is None:
            raw_limit = os.environ.get(_ENV_MAX_PERSIST_BODY_BYTES)
            try:
                max_persist_body_bytes = int(raw_limit) if raw_limit is not None else None
            except (TypeError, ValueError):
                max_persist_body_bytes = None
        self._max_persist_body_bytes = max(
            0,
            _DEFAULT_MAX_PERSIST_BODY_BYTES
            if max_persist_body_bytes is None
            else int(max_persist_body_bytes),
        )

    def claim_internet_archive_item_lookup(self, ref_id: str, lookup_key: str) -> bool:
        """Claim one catalogue lookup; followers must record a no-I/O memo skip."""
        key = (str(ref_id), str(lookup_key))
        with self._lock:
            if key in self._internet_archive_item_lookups:
                return False
            self._internet_archive_item_lookups.add(key)
            return True

    def archive_circuit_is_open(self) -> bool:
        with self._lock:
            return self._archive_circuit_open

    def record_archive_http_429(self, host: str) -> bool:
        """Record a physical canonical archive.org 429 and open on the second."""
        if str(host).casefold() != "archive.org":
            return False
        with self._lock:
            self._archive_http_429_count += 1
            if self._archive_http_429_count >= 2:
                self._archive_circuit_open = True
            return self._archive_circuit_open

    @staticmethod
    def _representation_fingerprint(
        *, accept: str, referer: str | None, headers_extra: dict[str, str] | None,
    ) -> str:
        """Hash representation-affecting request inputs without retaining them."""
        extra = {
            str(name).lower(): str(value)
            for name, value in (headers_extra or {}).items()
        }
        canonical = json.dumps(
            {"accept": str(accept), "referer": referer, "headers_extra": extra},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def request_identity(cls, url: str, profile: str, strategy: str, *, accept: str = "*/*",
                         referer: str | None = None,
                         headers_extra: dict[str, str] | None = None) -> tuple[str, str, str, str]:
        # Profile controls the representation request; strategy distinguishes a
        # browser/repository retry from the same URL.
        return (
            _normalized_url(url), profile, strategy,
            cls._representation_fingerprint(
                accept=accept, referer=referer, headers_extra=headers_extra,
            ),
        )

    def _key(self, url: str, profile: str, strategy: str, *, accept: str = "*/*",
             referer: str | None = None,
             headers_extra: dict[str, str] | None = None) -> tuple[str, str, str, str]:
        return self.request_identity(
            url, profile, strategy, accept=accept, referer=referer,
            headers_extra=headers_extra,
        )

    def attach_run(self, run_dir: str | None):
        """Make response entries reconstructible by later fetch phases.

        This intentionally stores only cacheable HTTP responses.  Network errors
        are not durable state: a later phase must be allowed to retry them.
        """
        if not run_dir:
            return
        self._telemetry_run_dir = run_dir
        if not self._persistence_enabled:
            return
        cache_dir = os.path.join(run_dir, ".fetch-response-cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            return
        self._cache_dir = cache_dir

    def disable_persistence(self) -> None:
        """Make this context an in-memory-only memo for its current process."""
        with self._lock:
            self._persistence_enabled = False
            self._cache_dir = None

    def _transport_run_dir(self) -> str | None:
        return self._telemetry_run_dir

    def register_provider_record_lookup(self, provider: str, lookup) -> None:
        """Register process-local provider metadata produced during Resolve."""
        if not isinstance(provider, str) or not provider or not callable(lookup):
            return
        with self._lock:
            self._provider_record_lookups[provider] = lookup

    def provider_record_lookup(self, provider: str):
        """Return a safe process-local lookup callable, or ``None``."""
        with self._lock:
            lookup = self._provider_record_lookups.get(provider)
        if not callable(lookup):
            return None

        def read(identifier):
            try:
                record = lookup(identifier)
            except Exception:
                return None
            return dict(record) if isinstance(record, dict) else None

        return read

    def provider_work_lookup(self, provider: str, ref_id: str, lookup_key: str, compute):
        """Return one run-local provider work lookup, including a cached miss.

        This is deliberately scoped to one provider, reference, and deterministic
        lookup key.  It never persists and a fresh Fetch run always retries.
        """
        if not callable(compute):
            return None
        key = (str(provider), str(ref_id), str(lookup_key))
        with self._lock:
            entry = self._provider_work_lookups.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "result": _MISSING}
                self._provider_work_lookups[key] = entry
                leader = True
            else:
                leader = False
        if leader:
            try:
                result = compute()
                entry["result"] = dict(result) if isinstance(result, dict) else None
            except BaseException:
                # Deferred and transient work is not a completed miss.  Evict it
                # before waking followers so a later scheduler retry can lead.
                with self._lock:
                    if self._provider_work_lookups.get(key) is entry:
                        self._provider_work_lookups.pop(key, None)
                raise
            finally:
                entry["event"].set()
        else:
            entry["event"].wait()
        result = entry["result"]
        return dict(result) if isinstance(result, dict) else None

    def _disk_path(self, key) -> str | None:
        if not self._cache_dir:
            return None
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
        return os.path.join(self._cache_dir, f"{digest}.json")

    def _load_disk(self, key):
        path = self._disk_path(key)
        if not path:
            return _MISSING
        try:
            with open(path, encoding="utf-8") as f:
                row = json.load(f)
            if row.get("schema_version") != _CACHE_SCHEMA_VERSION:
                return _MISSING
            if tuple(row.get("key") or ()) != key:
                return _MISSING
            result = dict(row.get("result") or {})
            if isinstance(result.get("body"), str):
                result["body"] = base64.b64decode(result["body"].encode("ascii"))
            if not self._positive_response(result):
                return _MISSING
            return result
        except (OSError, ValueError, TypeError):
            return _MISSING

    def _save_disk(self, key, result):
        path = self._disk_path(key)
        if not path or not self._positive_response(result):
            return
        row = dict(result)
        body = row.get("body")
        body_size = len(body) if isinstance(body, bytes) else len(str(body or "").encode("utf-8"))
        if body_size > self._max_persist_body_bytes:
            return
        if isinstance(body, bytes):
            row["body"] = base64.b64encode(body).decode("ascii")
        headers = row.get("headers")
        if isinstance(headers, dict):
            row["headers"] = {
                name: value
                for name, value in headers.items()
                if str(name).lower() not in _SENSITIVE_RESPONSE_HEADERS
            }
        payload = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "key": list(key),
            "result": row,
        }
        tmp = f"{path}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _positive_response(result) -> bool:
        if not isinstance(result, dict):
            return False
        # Only complete 2xx bytes may survive an exact-request replay.  A status
        # proves transport success, not source identity; later stages still parse
        # and validate the replayed bytes independently.
        try:
            status = int(result.get("status"))
        except (TypeError, ValueError):
            return False
        return (
            200 <= status < 300
            and bool(result.get("body"))
            and not result.get("content_decoding_error")
            and not result.get("challenge_markers")
            and not _stale_signed_failure(result)
        )

    @staticmethod
    def _cacheable(result) -> bool:
        """Whether a result may be reused within this paper's process."""
        if FetchRunContext._positive_response(result):
            return True
        # A stable not-found response avoids repeating the exact same request
        # across Resolve and formal Fetch. It is deliberately memory-only, so a
        # fresh process/paper starts without negative state.
        if not isinstance(result, dict):
            return False
        try:
            status = int(result.get("status"))
        except (TypeError, ValueError):
            return False
        return (
            status == 404
            and not result.get("content_decoding_error")
            and not result.get("challenge_markers")
            and not _stale_signed_failure(result)
        )

    def begin_fetch_phase(self) -> None:
        """Keep exact complete responses at the Resolve -> Fetch boundary.

        This context is process-local.  Clearing host-wide challenge and pacing
        state ensures formal Fetch may attempt a different URL on the same host.
        """
        with self._lock:
            self._responses = {
                key: entry
                for key, entry in self._responses.items()
                if entry["event"].is_set() and self._cacheable(entry["result"])
            }
            self._challenge_hosts.clear()
            self._host_next_allowed_at.clear()
            self._formal_fetch_completed_candidates.clear()
            self._formal_fetch_phase = True

    @staticmethod
    def _candidate_key(
        candidate: dict,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> tuple[str, str, str, str] | None:
        if not isinstance(candidate, dict):
            return None
        # Document/landing candidates can enqueue child links while they are
        # parsed. Replaying their cached response is cheap and preserves that
        # residual discovery; only leaf candidates are safe to omit entirely.
        if candidate.get("kind") not in {"pdf", "cran_archive"}:
            return None
        url = candidate.get("url")
        if not isinstance(url, str) or not url:
            return None
        identity = request_identity
        if isinstance(identity, tuple) and len(identity) == 4:
            return identity
        # Callers which have not admitted a request cannot prove an exact
        # representation identity, so they must not be skipped.
        return None

    def record_terminal_candidate(
        self,
        ref_id: str,
        candidate: dict,
        *,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> None:
        """Remember one completed candidate for this process and reference."""
        key = self._candidate_key(candidate, request_identity)
        if not isinstance(ref_id, str) or not ref_id or key is None:
            return
        with self._lock:
            response = self._responses.get(key)
            if not (
                response is not None
                and response["event"].is_set()
                and self._cacheable(response["result"])
            ):
                return
            self._terminal_candidates.setdefault(ref_id, set()).add(key)

    def terminal_candidate_seen(
        self,
        ref_id: str,
        candidate: dict,
        *,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> bool:
        key = self._candidate_key(candidate, request_identity)
        if not isinstance(ref_id, str) or key is None:
            return False
        with self._lock:
            return self._formal_fetch_phase and key in self._terminal_candidates.get(ref_id, set())

    def record_formal_fetch_completed_candidate(
        self,
        ref_id: str,
        candidate: dict,
        *,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> None:
        """Remember a request-bearing, non-deferred automatic-Fetch attempt.

        This deliberately does not depend on HTTP-cacheability: an attempted
        5xx response is still a completed candidate execution for the current
        formal phase and must not be replayed merely because provider discovery
        was deferred afterwards.
        """
        key = self._formal_fetch_candidate_key(candidate, request_identity)
        if not isinstance(ref_id, str) or not ref_id or key is None:
            return
        with self._lock:
            self._formal_fetch_completed_candidates.setdefault(ref_id, set()).add(key)

    def formal_fetch_completed_candidate_seen(
        self,
        ref_id: str,
        candidate: dict,
        *,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> bool:
        """Whether automatic Fetch already physically completed this candidate."""
        key = self._formal_fetch_candidate_key(candidate, request_identity)
        if not isinstance(ref_id, str) or key is None:
            return False
        with self._lock:
            return key in self._formal_fetch_completed_candidates.get(ref_id, set())

    @staticmethod
    def _formal_fetch_candidate_key(
        candidate: dict,
        request_identity: tuple[str, str, str, str] | None = None,
    ) -> tuple[str, str, str, str] | None:
        """Return an exact request key for any automatic-Fetch candidate.

        Resolve-to-Fetch terminal reuse intentionally excludes landing pages,
        because its later parse can discover residual children.  The formal
        Fetch ledger is narrower in time: it is recorded only after
        ``process_queue`` has handled that landing page and frozen any dynamic
        children in the same formal phase, so an exact landing representation
        may safely be omitted on a provider-only retry.
        """
        if not isinstance(candidate, dict):
            return None
        url = candidate.get("url")
        if not isinstance(url, str) or not url:
            return None
        if isinstance(request_identity, tuple) and len(request_identity) == 4:
            return request_identity
        return None

    def _ready_entry(self, result):
        ev = threading.Event()
        ev.set()
        return {"event": ev, "result": result}

    def _challenge_result(self, url: str) -> dict:
        return {
            "status": 403,
            "body": _CHALLENGE_BODY,
            "content_type": "text/plain; charset=utf-8",
            "url": url,
            "cached_challenge": True,
            "body_head": _CHALLENGE_BODY.decode("utf-8", errors="replace"),
            "challenge_markers": [
                "just a moment",
                "enable javascript and cookies to continue",
            ],
        }

    def _host_semaphore(self, host: str | None):
        if not host:
            return None
        with self._lock:
            gate = self._host_semaphores.get(host)
            if gate is None:
                gate = threading.BoundedSemaphore(self._host_concurrency)
                self._host_semaphores[host] = gate
            return gate

    def _wait_for_host_interval(self, host: str | None):
        if not host or self._host_min_interval <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                next_allowed = self._host_next_allowed_at.get(host, 0.0)
                if now >= next_allowed:
                    self._host_next_allowed_at[host] = now + self._host_min_interval
                    return
                delay = next_allowed - now
            if delay > 0:
                time.sleep(delay)

    def remember_challenge_host(self, url: str | None, *, profile: str = "document",
                                strategy: str = "auto"):
        host = _host(url)
        if not host:
            return
        with self._lock:
            self._challenge_hosts.add((host, profile, strategy))

    def cached_fetch(self, fetch_fn, url: str, *, accept: str = "*/*", timeout: int = 12,
                     profile: str = "document", strategy: str = "auto", referer: str | None = None,
                     headers_extra: dict[str, str] | None = None, ref_id: str | None = None,
                     persist_response: bool = True):
        from core.fetch.transport import transport_telemetry

        key = self._key(
            url, profile, strategy, accept=accept, referer=referer,
            headers_extra=headers_extra,
        )
        while True:
            disk_hit = False
            with self._lock:
                entry = self._responses.get(key)
                if entry is None:
                    disk_result = self._load_disk(key) if persist_response else _MISSING
                    entry = {"event": threading.Event(), "result": disk_result}
                    if disk_result is not _MISSING:
                        entry["event"].set()
                    self._responses[key] = entry
                    owner = disk_result is _MISSING
                    disk_hit = not owner
                else:
                    ready = entry["event"].is_set()
                    if not ready:
                        entry["waiters"] = entry.get("waiters", 0) + 1
                    owner = False
            if owner:
                break
            entry["event"].wait()
            result = entry["result"]
            try:
                from core.fetch.transport.http import FetchAdmissionDeferred
            except ImportError:
                FetchAdmissionDeferred = ()
            if isinstance(result, FetchAdmissionDeferred):
                # A same-process follower shares the leader's authoritative
                # defer until its not-before point. It is neither a response
                # nor a persistent cache entry; after expiry one caller may
                # elect a fresh admission attempt.
                if time.monotonic() < result.not_before:
                    raise result
                with self._lock:
                    if self._responses.get(key) is entry:
                        self._responses.pop(key, None)
                continue
            if result is _MISSING:
                # A leader failed after this caller joined its single-flight.
                # Re-enter election so this request may be retried.
                continue
            if result is not _MISSING and not self._cacheable(result):
                with self._lock:
                    if self._responses.get(key) is entry:
                        self._responses.pop(key, None)
                continue
            outcome = "disk_hit" if disk_hit else (
                "memory_hit" if ready else "coalesced_wait"
            )
            with transport_telemetry.logical_request(
                run_dir=self._transport_run_dir(), ref_id=ref_id, url=url,
                profile=profile, strategy=strategy, cache_outcome=outcome,
                method="GET", accept=accept, referer=referer,
                headers_extra=headers_extra,
            ):
                return None if result is _MISSING else dict(result)

        try:
            signed_expiry = _signed_url_expiry(url)
            if signed_expiry is not None and signed_expiry <= _utc_now():
                with transport_telemetry.logical_request(
                    run_dir=self._transport_run_dir(), ref_id=ref_id, url=url,
                    profile=profile, strategy=strategy, cache_outcome="signed_expired",
                    method="GET", accept=accept, referer=referer,
                    headers_extra=headers_extra,
                ):
                    result = _expired_signed_result(url, signed_expiry)
            else:
                host = _host(url)
                with self._lock:
                    challenged = (host, profile, strategy) in self._challenge_hosts
                if challenged:
                    with transport_telemetry.logical_request(
                        run_dir=self._transport_run_dir(), ref_id=ref_id, url=url,
                        profile=profile, strategy=strategy, cache_outcome="challenge_shortcut",
                        method="GET", accept=accept, referer=referer,
                        headers_extra=headers_extra,
                    ):
                        result = self._challenge_result(url)
                else:
                    with transport_telemetry.logical_request(
                        run_dir=self._transport_run_dir(), ref_id=ref_id, url=url,
                        profile=profile, strategy=strategy, cache_outcome="miss",
                        method="GET", accept=accept, referer=referer,
                        headers_extra=headers_extra,
                    ):
                        gate = self._host_semaphore(host) if profile in {"document", "pdf"} else None
                        if gate is None:
                            result = fetch_fn(url, accept=accept, timeout=timeout, profile=profile,
                                              referer=referer, headers_extra=headers_extra)
                        else:
                            with gate:
                                self._wait_for_host_interval(host)
                                result = fetch_fn(url, accept=accept, timeout=timeout, profile=profile,
                                                  referer=referer, headers_extra=headers_extra)
        except BaseException as exc:
            # Never strand followers on a leader failure.  Failure is deliberately
            # not memoized, so the next caller may retry the request.
            with self._lock:
                try:
                    from core.fetch.transport.http import FetchAdmissionDeferred
                except ImportError:
                    FetchAdmissionDeferred = ()
                if isinstance(exc, FetchAdmissionDeferred):
                    entry["result"] = exc
                else:
                    if self._responses.get(key) is entry:
                        self._responses.pop(key, None)
                    entry["result"] = _MISSING
                entry["event"].set()
            raise
        with self._lock:
            entry["result"] = result
            entry["event"].set()
            if self._cacheable(result) and result.get("url"):
                final_key = self._key(
                    result["url"], profile, strategy, accept=accept,
                    referer=referer, headers_extra=headers_extra,
                )
                self._responses.setdefault(final_key, self._ready_entry(result))
                if persist_response and self._positive_response(result):
                    self._save_disk(key, result)
                if persist_response and self._positive_response(result) and final_key != key:
                    # A later phase often starts directly from the redirect target.
                    # Persist that alias too, otherwise only this in-memory context
                    # can reuse the response under the final URL.
                    self._save_disk(final_key, result)
            else:
                # Let any concurrent waiters observe this result, but remove it
                # immediately afterwards so a later phase/request can retry.
                self._responses.pop(key, None)
        return None if result is None else dict(result)
