#!/usr/bin/env python3
# core/resolve/providers/semantic_scholar.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Semantic Scholar resolver module."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse

from core.fetch.transport import host_limiter
from core.resolve import http as resolve_http

NAME = "semantic_scholar_search"
CREDENTIAL_SPECS = ({
    "provider": "semantic_scholar",
    "env_name": "SEMANTIC_SCHOLAR_API_KEY",
    "channels": ("resolve",),
    "label": "Semantic Scholar",
},)
MANIFEST = {
    "origin": "semantic_scholar",
}
OPTIONAL_STAGE = True
_BATCH_ENV = "CITATION_VERIFIER_SEMANTIC_SCHOLAR_BATCH"
_BATCH_LOCAL = threading.local()
_BATCH_FIELDS = "title,abstract,year,authors,venue,externalIds,openAccessPdf,isOpenAccess,publicationTypes"
_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
_BATCH_RATE_LIMITED = object()
_BATCH_DEFERRED = object()


def _raise_if_preflight_cooldown(exc: BaseException) -> None:
    """Turn only a pre-send Semantic Scholar cooldown into phase deferral."""
    if not isinstance(exc, host_limiter.HostCooldownExceeded):
        return
    # The shared SQLite pacer uses wall time while ordinary limiters use a
    # monotonic clock. The exception is the only portable representation of
    # the authoritative remaining wait at this boundary.
    not_before = time.monotonic() + exc.wait_seconds
    raise resolve_http.SemanticScholarCooldownDeferred(
        not_before=not_before,
        physical_429_count=getattr(exc, "physical_429_count", 0),
        physical_429_tokens=getattr(exc, "physical_429_tokens", ()),
    ) from exc


def _semantic_scholar_batch_id(doi: str) -> str:
    """Return the Semantic Scholar batch namespace for a normalized DOI."""
    if doi.lower().startswith("10.48550/arxiv."):
        return "ARXIV:" + doi.split(".", 2)[2]
    return "DOI:" + doi


def batch_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ or os.environ
    return str(env.get(_BATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


class _BatchEntry:
    def __init__(self, doi: str, ref_id: str | None = None):
        self.doi = doi
        self.ref_ids = [ref_id] if ref_id else []
        self.mapped_ref_ids: set[str] = set()
        self.ready = threading.Event()
        self.claimed = False


class SemanticScholarBatchSession:
    """Run-local DOI enricher accelerator; scalar enrichment remains authoritative."""

    def __init__(self, *, coalesce_delay: float = 0.01, sleep_fn=time.sleep, chunk_size: int = 500):
        self._coalesce_delay = max(0.0, float(coalesce_delay))
        self._sleep = sleep_fn
        self._chunk_size = max(1, min(500, int(chunk_size)))
        self._lock = threading.Lock()
        self._pending: dict[str, _BatchEntry] = {}
        self._completed: dict[str, tuple[dict | None, str]] = {}
        self._operation_ids: dict[str, str | None] = {}
        self._deferred_until: float | None = None
        self._deferred_physical_429_count = 0
        self._deferred_physical_429_tokens: tuple[object, ...] = ()
        self._deferred_keys: set[str] = set()
        self._deferred_ref_ids_by_key: dict[str, set[str]] = {}
        self._deferred_reported_refs: set[tuple[str, str]] = set()
        self._dispatching = False
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for doi, entry in self._pending.items():
                self._completed.setdefault(doi, (None, "unavailable"))
                entry.ready.set()

    def prime_many(self, items, *, post_fn, normalize_doi, headers) -> None:
        """Populate declared DOI results before resolve workers start."""
        with self._lock:
            if self._closed:
                return
            for ref_id, doi in items:
                normalized = normalize_doi(doi)
                if not normalized:
                    continue
                key = normalized.lower()
                entry = self._pending.get(key)
                if entry is None:
                    entry = _BatchEntry(normalized, ref_id)
                    self._pending[key] = entry
                elif ref_id and ref_id not in entry.ref_ids:
                    entry.ref_ids.append(ref_id)
            if not self._pending or self._dispatching:
                return
            self._dispatching = True
        self._dispatch(post_fn=post_fn, normalize_doi=normalize_doi, headers=headers)

    def lookup(
        self,
        doi: str,
        *,
        post_fn,
        normalize_doi,
        headers,
        ref_id: str | None = None,
    ) -> tuple[dict | None, str | None]:
        normalized = normalize_doi(doi)
        if not normalized:
            return None, None
        key = normalized.lower()
        completed = None
        operation_id = None
        with self._lock:
            if self._closed:
                return None, "unavailable"
            entry = self._pending.get(key)
            if key in self._completed:
                completed = self._completed[key]
                operation_id = self._operation_ids.get(key)
            if self._deferred_until is not None:
                if completed is not None:
                    pass
                elif time.monotonic() < self._deferred_until:
                    # A batch POST is one physical episode.  Each DOI/ref
                    # which was actually in that POST may report it once to
                    # its provider-local scheduler; a repeated observation of
                    # the same ref and a DOI added after the POST consume none.
                    report_key = (key, str(ref_id or ""))
                    physical = 0
                    if (
                        key in self._deferred_keys
                        and report_key[1] in self._deferred_ref_ids_by_key.get(key, set())
                        and report_key not in self._deferred_reported_refs
                    ):
                        self._deferred_reported_refs.add(report_key)
                        physical = self._deferred_physical_429_count
                    raise resolve_http.SemanticScholarCooldownDeferred(
                        not_before=self._deferred_until,
                        physical_429_count=physical,
                        physical_429_tokens=(
                            self._deferred_physical_429_tokens if physical else ()
                        ),
                    )
                elif completed is None:
                    self._deferred_until = None
                    self._deferred_physical_429_count = 0
                    self._deferred_physical_429_tokens = ()
                    for pending_key, pending_entry in self._pending.items():
                        if pending_key in self._completed:
                            continue
                        pending_entry.claimed = False
                        pending_entry.ready.clear()
                    self._deferred_keys.clear()
                    self._deferred_ref_ids_by_key.clear()
                    self._deferred_reported_refs.clear()
            if completed is None:
                if entry is None:
                    entry = _BatchEntry(normalized, ref_id)
                    self._pending[key] = entry
                elif ref_id and ref_id not in entry.ref_ids:
                    entry.ref_ids.append(ref_id)
                leader = not self._dispatching
                if leader:
                    self._dispatching = True
        if completed is not None:
            self._append_ref_mapping_once(entry, operation_id, ref_id)
            return completed
        if leader:
            self._dispatch(post_fn=post_fn, normalize_doi=normalize_doi, headers=headers)
        entry.ready.wait()
        with self._lock:
            if self._deferred_until is not None:
                report_key = (key, str(ref_id or ""))
                physical = 0
                if (
                    key in self._deferred_keys
                    and report_key[1] in self._deferred_ref_ids_by_key.get(key, set())
                    and report_key not in self._deferred_reported_refs
                ):
                    self._deferred_reported_refs.add(report_key)
                    physical = self._deferred_physical_429_count
                raise resolve_http.SemanticScholarCooldownDeferred(
                    not_before=self._deferred_until,
                    physical_429_count=physical,
                    physical_429_tokens=(
                        self._deferred_physical_429_tokens if physical else ()
                    ),
                )
            completed = self._completed.get(key, (None, "unavailable"))
            operation_id = self._operation_ids.get(key)
        self._append_ref_mapping_once(entry, operation_id, ref_id)
        return completed

    def _append_ref_mapping_once(self, entry, operation_id, ref_id) -> None:
        if not ref_id or not operation_id:
            return
        with self._lock:
            if ref_id in entry.mapped_ref_ids:
                return
            entry.mapped_ref_ids.add(ref_id)
        from core.resolve import transport_telemetry

        transport_telemetry.append_ref_mapping(operation_id, ref_id)

    def _dispatch(self, *, post_fn, normalize_doi, headers) -> None:
        active_chunk: list[_BatchEntry] = []
        active_chunk_ref_ids_by_key: dict[str, set[str]] = {}
        try:
            while True:
                if self._coalesce_delay:
                    self._sleep(self._coalesce_delay)
                with self._lock:
                    if self._closed:
                        self._dispatching = False
                        return
                    entries = [
                        entry
                        for entry in self._pending.values()
                        if not entry.claimed
                    ]
                    for entry in entries:
                        entry.claimed = True
                    if not entries:
                        self._dispatching = False
                        return
                for offset in range(0, len(entries), self._chunk_size):
                    chunk = entries[offset:offset + self._chunk_size]
                    active_chunk = chunk
                    from core.resolve import transport_telemetry

                    # A duplicate DOI can join a claimed entry concurrently. Copy
                    # ordered reference IDs while holding the lookup() lock.
                    with self._lock:
                        ref_ids = [
                            ref_id for entry in chunk for ref_id in entry.ref_ids
                        ]
                        active_chunk_ref_ids_by_key = {
                            entry.doi.lower(): set(entry.ref_ids)
                            for entry in chunk
                        }
                        for entry in chunk:
                            entry.mapped_ref_ids.update(entry.ref_ids)
                    operation_id = None
                    if ref_ids:
                        with transport_telemetry.operation(
                            provider="semantic_scholar",
                            operation="doi_lookup",
                            mode="batch",
                            ref_ids=ref_ids,
                            item_count=len(chunk),
                            chunk_index=offset // self._chunk_size,
                        ) as operation_id:
                            records = self._fetch_chunk(
                                chunk,
                                post_fn=post_fn,
                                normalize_doi=normalize_doi,
                                headers=headers,
                            )
                    else:
                        records = self._fetch_chunk(
                            chunk,
                            post_fn=post_fn,
                            normalize_doi=normalize_doi,
                            headers=headers,
                        )
                    with self._lock:
                        if self._closed:
                            continue
                        for entry in chunk:
                            key = entry.doi.lower()
                            self._operation_ids[key] = operation_id
                            if records is _BATCH_DEFERRED:
                                # The same batch remains pending.  Wake the
                                # current callers with the typed continuation;
                                # the first resumed caller becomes the next
                                # leader after the shared admission time.
                                continue
                            if records is _BATCH_RATE_LIMITED:
                                outcome = "rate_limited"
                                record = None
                            elif records is None:
                                outcome = "unavailable"
                                record = None
                            elif key in records:
                                outcome = "matched"
                                record = records[key]
                            else:
                                outcome = "omitted"
                                record = None
                            self._completed[key] = (record, outcome)
                            entry.ready.set()
        except resolve_http.SemanticScholarCooldownDeferred as exc:
            with self._lock:
                self._deferred_until = exc.not_before
                self._deferred_physical_429_count = exc.physical_429_count
                self._deferred_physical_429_tokens = exc.physical_429_tokens
                self._deferred_keys = {entry.doi.lower() for entry in active_chunk}
                self._deferred_ref_ids_by_key = active_chunk_ref_ids_by_key
                self._deferred_reported_refs.clear()
                self._dispatching = False
                for entry in self._pending.values():
                    entry.ready.set()
        except Exception:
            with self._lock:
                for key, entry in self._pending.items():
                    self._completed.setdefault(key, (None, "unavailable"))
                    entry.ready.set()
                self._dispatching = False

    def predecessor_operation_id(self, doi: str) -> str | None:
        return self._operation_ids.get(str(doi or "").lower())

    @staticmethod
    def _fetch_chunk(entries, *, post_fn, normalize_doi, headers) -> dict[str, dict] | object | None:
        dois = [entry.doi for entry in entries]
        try:
            query = urllib.parse.urlencode({"fields": _BATCH_FIELDS})
            status, body = post_fn(
                f"{_BATCH_URL}?{query}",
                {"ids": [_semantic_scholar_batch_id(doi) for doi in dois]},
                headers_extra=headers,
            )
            if status == 429:
                return _BATCH_RATE_LIMITED
            if not isinstance(status, int) or not 200 <= status < 300:
                return None
            data = json.loads(body)
            if not isinstance(data, list) or len(data) != len(dois):
                return None
            expected = {doi.lower() for doi in dois}
            out = {}
            for record in data:
                if record is None:
                    continue
                if not isinstance(record, dict):
                    return None
                returned = normalize_doi((record.get("externalIds") or {}).get("DOI") or (record.get("externalIds") or {}).get("doi"))
                if not returned or returned.lower() not in expected or returned.lower() in out:
                    return None
                out[returned.lower()] = record
            return out
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return _BATCH_RATE_LIMITED
            return None
        except host_limiter.HostCooldownExceeded as exc:
            _raise_if_preflight_cooldown(exc)
            raise AssertionError("unreachable")
        except Exception:
            return None


def new_batch_session(**kwargs) -> SemanticScholarBatchSession:
    return SemanticScholarBatchSession(**kwargs)


class _BatchBinding:
    def __init__(self, session): self._session, self._previous = session, None
    def __enter__(self):
        self._previous = getattr(_BATCH_LOCAL, "session", None)
        _BATCH_LOCAL.session = self._session
        return self._session
    def __exit__(self, *_exc): _BATCH_LOCAL.session = self._previous


def bind_batch_session(session):
    return _BatchBinding(session)


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def _headers(resolve_mod) -> dict[str, str] | None:
    key = resolve_mod._configured_key(resolve_mod.ENV_SEMANTIC_SCHOLAR_API_KEY)
    if not key:
        return None
    return {"x-api-key": key}


def _to_crossref_like(rec: dict) -> dict:
    authors = rec.get("authors") or []
    author_list = []
    if authors:
        surname = str((authors[0] or {}).get("name") or "").strip().split()[-1:]
        if surname:
            author_list.append({"family": surname[0]})
    title = rec.get("title")
    pub_year = rec.get("year")
    venue = rec.get("venue")
    return {
        "title": [title] if title else None,
        "author": author_list,
        "published-print": {"date-parts": [[pub_year]]} if pub_year else {},
        "container-title": [venue] if venue else [],
    }


def _doi(resolve_mod, rec: dict) -> str | None:
    ext = rec.get("externalIds") or {}
    doi = ext.get("DOI") or ext.get("doi") or rec.get("doi")
    return resolve_mod._normalize_doi_value(doi)


def _fulltext_meta(resolve_mod, rec: dict) -> dict:
    out_links = []
    doi = _doi(resolve_mod, rec)
    if doi:
        out_links.append({"url": f"https://doi.org/{doi}", "content_type": "doi"})
    pdf = rec.get("openAccessPdf") or {}
    pdf_url = pdf.get("url")
    if pdf_url:
        out_links.append({"url": pdf_url, "content_type": "pdf", "availability": pdf.get("status")})
    if out_links:
        return {
            "fulltext_exists": True,
            "oa_status": "open" if rec.get("isOpenAccess") or pdf_url else "unknown",
            "work_type": ((rec.get("publicationTypes") or [None])[0] or "").lower() or None,
            "fulltext_links": out_links,
        }
    return {
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": ((rec.get("publicationTypes") or [None])[0] or "").lower() or None,
        "fulltext_links": out_links,
    }


def _enrichment_result(resolve_mod, rec: dict, *, reason=None) -> dict:
    out = {"status": "resolved", "via": "semantic_scholar", "matched_title": rec.get("title"),
           "abstract": rec.get("abstract"), "reason": reason}
    out.update(_fulltext_meta(resolve_mod, rec))
    return out


def enrich(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    headers = _headers(resolve_mod)
    if headers is None:
        return None
    doi = resolve_mod._normalize_doi_value(ref.get("doi"))
    if not doi:
        return None
    batch_note = None
    batch = getattr(_BATCH_LOCAL, "session", None)
    if batch is not None and batch_enabled():
        record, outcome = batch.lookup(
            doi,
            post_fn=lambda url, payload, **kwargs: resolve_mod._post_json(
                url, payload, preserve_cooldown_after_429=True, **kwargs
            ),
            normalize_doi=resolve_mod._normalize_doi_value,
            headers=headers,
            ref_id=ref.get("id"),
        )
        if record is not None:
            return _enrichment_result(resolve_mod, record, reason="Semantic Scholar DOI batch match")
        if outcome == "rate_limited":
            return {
                "status": "unresolved",
                "via": "semantic_scholar",
                "reason": "batch rate_limited (HTTP 429) - NOT fabrication",
                "error_type": "rate_limit",
                "http_status": 429,
                "retryable": True,
            }
        if outcome == "omitted":
            batch_note = "batch omitted declared DOI; scalar fallback"
        elif outcome == "unavailable":
            batch_note = "batch unavailable; scalar fallback"
    def with_batch_note(result: dict) -> dict:
        if batch_note is None:
            return result
        out = dict(result)
        reason = str(out.get("reason") or "").strip()
        out["reason"] = f"{reason}; {batch_note}" if reason else batch_note
        return out
    params = {"fields": _BATCH_FIELDS}
    try:
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/DOI:"
            + urllib.parse.quote(doi, safe="")
            + "?"
            + urllib.parse.urlencode(params)
        )
        ref_id = ref.get("id")
        if ref_id:
            from core.resolve import transport_telemetry

            predecessor = (
                batch.predecessor_operation_id(doi) if batch_note else None
            )
            with transport_telemetry.operation(
                provider="semantic_scholar",
                operation="doi_lookup",
                mode="scalar",
                ref_ids=[ref_id],
                predecessor_operation_id=predecessor,
            ):
                _status, body = resolve_mod._get(
                    url, headers_extra=headers, preserve_cooldown_after_429=True,
                )
        else:
            _status, body = resolve_mod._get(
                url, headers_extra=headers, preserve_cooldown_after_429=True,
            )
        rec = json.loads(body) or {}
        if not isinstance(rec, dict) or not rec:
            return with_batch_note({
                "status": "unverified",
                "via": "semantic_scholar",
                "reason": "no match on Semantic Scholar enrichment",
            })
        return with_batch_note(_enrichment_result(resolve_mod, rec))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return with_batch_note({
                "status": "unverified",
                "via": "semantic_scholar",
                "reason": "no match on Semantic Scholar enrichment",
            })
        if exc.code == 429:
            return with_batch_note({
                "status": "unresolved",
                "via": "semantic_scholar",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            })
        return with_batch_note({
            "status": "unresolved",
            "via": "semantic_scholar",
            "reason": f"HTTP {exc.code}",
        })
    except host_limiter.HostCooldownExceeded as exc:
        _raise_if_preflight_cooldown(exc)
        raise AssertionError("unreachable")
    except resolve_http.SemanticScholarCooldownDeferred:
        raise
    except Exception as exc:
        return with_batch_note({
            "status": "unresolved",
            "via": "semantic_scholar",
            "reason": f"network: {type(exc).__name__}",
        })


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    headers = _headers(resolve_mod)
    if headers is None:
        return None
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for Semantic Scholar search",
        }
    raw = ref.get("raw_entry") or ""
    author = resolve_mod._first_author_key(raw)
    year = ref.get("year")
    query = title[:250]
    if author:
        query += f" {author}"
    params = {
        "query": query,
        "limit": "5",
        "fields": "title,abstract,year,authors,venue,externalIds,openAccessPdf,isOpenAccess,publicationTypes",
    }
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
        _status, body = resolve_mod._get(
            url, headers_extra=headers, preserve_cooldown_after_429=True,
        )
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("title"):
            results = [payload]
        else:
            results = (payload.get("data") or []) if isinstance(payload, dict) else []
        if not results:
            return {
                "status": "unverified",
                "via": NAME,
                "reason": "no match on Semantic Scholar",
            }
        best_rec = None
        best_profile = None
        best_score = -1.0
        for rec in results:
            if not isinstance(rec, dict):
                continue
            msg = _to_crossref_like(rec)
            profile = resolve_mod._metadata_match_profile(ref, msg, rec.get("title"))
            if profile["score"] > best_score:
                best_score = profile["score"]
                best_rec = rec
                best_profile = profile
        rec = best_rec or {}
        profile = best_profile
        if profile is None:
            return {
                "status": "unverified",
                "via": NAME,
                "reason": "no match on Semantic Scholar",
            }
        overlap = profile["title_overlap"]
        if overlap is not None and overlap < resolve_mod.TITLE_MISMATCH_MAX:
            return {
                "status": "unverified",
                "via": NAME,
                "matched_title": rec.get("title"),
                "reason": (
                    f"title too dissimilar (overlap {overlap:.3f} < {resolve_mod.TITLE_MISMATCH_MAX}); "
                    "author/year match is not enough to identify the cited work"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
            }
        if profile["score"] < 0.30:
            return {
                "status": "unverified",
                "via": NAME,
                "matched_title": rec.get("title"),
                "reason": (
                    f"best match below confidence threshold "
                    f"(score {profile['score']:.3f}, overlap {overlap})"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
            }
        if (
            year is not None
            and rec.get("year") is not None
            and int(year) != int(rec["year"])
            and profile["score"] < 0.60
        ):
            return {
                "status": "unverified",
                "via": NAME,
                "matched_title": rec.get("title"),
                "reason": (
                    f"year mismatch (cited {year}, matched {rec['year']}, "
                    f"score {profile['score']:.3f})"
                ),
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
                "metadata_match": profile,
            }
        out = {
            "status": "resolved",
            "via": NAME,
            "matched_title": rec.get("title"),
            "abstract": rec.get("abstract"),
            "retracted": False,
            "reason": "Semantic Scholar title search match",
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium",
            "metadata_match": profile,
        }
        out.update(_fulltext_meta(resolve_mod, rec))
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "status": "unverified",
                "via": NAME,
                "reason": "no match on Semantic Scholar",
            }
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"HTTP {exc.code}",
        }
    except host_limiter.HostCooldownExceeded as exc:
        _raise_if_preflight_cooldown(exc)
        raise AssertionError("unreachable")
    except resolve_http.SemanticScholarCooldownDeferred:
        raise
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"network: {type(exc).__name__}",
        }
