#!/usr/bin/env python3
# core/resolve/providers/openalex.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenAlex resolver + fetch provider."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse

try:
    from .. import sources as _sources
except ImportError:
    from resolve import sources as _sources


NAME = "openalex"
CREDENTIAL_SPECS = ({
    "provider": "openalex",
    "env_name": "OPENALEX_API_KEY",
    "channels": ("resolve", "fetch"),
    "label": "OpenAlex",
},)
RESOLVE_NAME = "openalex_search"
MANIFEST = {
    "origin": "openalex",
    "weak_abstract_origin": True,
}
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
JOURNAL_COVERAGE = {"resolver": "openalex", "rule_version": "openalex-source-issn/v1"}
_IDSIA_MIRROR_BASE = "https://sferics.idsia.ch/pub/juergen/"
_IDSIA_ALIAS_MAP = {
    "ch7.ps.gz": "gradientflow.pdf",
    "fki-207-95.ps.gz": "lstm.pdf",
    "fki-207-95r.ps.gz": "lstm.pdf",
    "fki-207-95rev.ps.gz": "lstm.pdf",
    "lstm.ps.gz": "lstm.pdf",
}
MAX_CANDIDATE_ITEMS = 4
_BATCH_ENV = "CITATION_VERIFIER_OPENALEX_BATCH"
_BATCH_LOCAL = threading.local()
_BATCH_SELECT = (
    "id,doi,title,publication_year,authorships,abstract_inverted_index,"
    "open_access,best_oa_location,primary_location,locations,has_fulltext"
)
_FETCH_WORK_FIELDS = (
    "id", "doi", "title", "open_access", "best_oa_location",
    "primary_location", "locations", "has_fulltext",
)


def _http_error_body(exc: urllib.error.HTTPError) -> str | None:
    try:
        body = exc.read()
    except Exception:
        return None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body) if body is not None else None


def probe_journal_coverage(authority: dict) -> dict:
    """Use OpenAlex's exact source-by-ISSN route; title search is inadmissible."""
    from core.resolve import service as resolve_mod
    issns = authority.get("issns") if isinstance(authority, dict) else ()
    if not isinstance(issns, tuple) or not issns:
        raise ValueError("coverage authority has no registered ISSN")
    replies = []
    for issn in issns:
        url = f"https://api.openalex.org/sources/issn:{urllib.parse.quote(issn)}"
        try:
            status, body = resolve_mod._get(url)
            payload = json.loads(body)
            returned_issns = payload.get("issn") if isinstance(payload, dict) else None
            if not isinstance(returned_issns, list):
                returned_issns = []
            if isinstance(payload, dict) and isinstance(payload.get("issn_l"), str):
                returned_issns = [*returned_issns, payload["issn_l"]]
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("id"), str)
                or not payload["id"].strip()
                or issn.casefold() not in {
                    str(value).strip().casefold() for value in returned_issns
                    if isinstance(value, str) and value.strip()
                }
            ):
                raise ValueError("OpenAlex source response is malformed")
            replies.append((issn, int(status), payload, url, None))
        except urllib.error.HTTPError as exc:
            replies.append((issn, exc.code, None, url, _http_error_body(exc)))
        except Exception as exc:
            return {"resolver": "openalex", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": url, "http_status": None, "response": None, "query_contract": "GET /sources/issn:{issn}", "completion": "incomplete", "reason": f"network: {type(exc).__name__}"}
    found = next((item for item in replies if item[1] == 200), None)
    if found:
        issn, status, payload, url, _error_body = found
        return {"resolver": "openalex", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "covered", "provider_journal_id": str(payload["id"]), "work_count": payload.get("works_count") if type(payload.get("works_count")) is int else None, "source_url": url, "http_status": status, "response": payload, "query_contract": "GET /sources/issn:{issn}", "completion": "complete", "reason": "exact OpenAlex ISSN source record exists"}
    if all(item[1] == 404 for item in replies):
        response = {"queries": [
            {"issn": issn, "http_status": status, "source_url": url, "body": error_body}
            for issn, status, _payload, url, error_body in replies
        ]}
        return {"resolver": "openalex", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "not_covered", "provider_journal_id": None, "work_count": None, "source_url": replies[0][3], "http_status": 404, "response": response, "query_contract": "GET /sources/issn:{issn}", "completion": "complete", "reason": "all registered ISSNs returned OpenAlex 404"}
    return {"resolver": "openalex", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": replies[0][3], "http_status": next((item[1] for item in replies if item[1] != 404), None), "response": None, "query_contract": "GET /sources/issn:{issn}", "completion": "incomplete", "reason": "exact OpenAlex ISSN coverage probe did not complete"}


def batch_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether the experimental, run-scoped DOI batch accelerator is enabled."""
    env = environ or os.environ
    return str(env.get(_BATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


class _BatchEntry:
    def __init__(self, doi: str, ref_id: str | None = None):
        self.doi = doi
        self.ref_ids = [ref_id] if ref_id else []
        self.mapped_ref_ids: set[str] = set()
        self.ready = threading.Event()
        self.claimed = False


class OpenAlexBatchSession:
    """Coalesce concurrent declared-DOI lookups within one resolve phase.

    This is deliberately an internal accelerator.  A batch miss or an unusable
    response is indistinguishable from no accelerator: the caller resumes the
    established scalar OpenAlex title search.
    """

    def __init__(self, *, coalesce_delay: float = 0.01, sleep_fn=time.sleep):
        self._coalesce_delay = max(0.0, float(coalesce_delay))
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._pending: dict[str, _BatchEntry] = {}
        self._completed: dict[str, tuple[dict | None, str]] = {}
        self._operation_ids: dict[str, str | None] = {}
        self._normalize_doi = None
        self._dispatching = False
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for doi, entry in self._pending.items():
                self._completed.setdefault(doi, (None, "unavailable"))
                entry.ready.set()

    def prime_many(
        self,
        items,
        *,
        get_fn,
        normalize_doi,
        email=None,
        environ=None,
    ) -> None:
        """Populate this phase's declared DOI cache in supplied order.

        This is deliberately transport-only: callers still perform their normal
        provider discovery and admission checks against the cached record.
        """
        with self._lock:
            if self._closed:
                return
            self._normalize_doi = normalize_doi
            for ref_id, doi in items:
                normalized = normalize_doi(doi)
                if not normalized:
                    continue
                key = normalized.lower()
                entry = self._pending.get(key)
                if entry is None:
                    entry = _BatchEntry(key, ref_id)
                    self._pending[key] = entry
                elif ref_id and ref_id not in entry.ref_ids:
                    entry.ref_ids.append(ref_id)
            if not self._pending or self._dispatching:
                return
            self._dispatching = True
        self._dispatch(
            get_fn=get_fn,
            normalize_doi=normalize_doi,
            email=email,
            environ=environ,
        )

    def lookup(
        self,
        doi: str,
        *,
        get_fn,
        normalize_doi,
        email=None,
        environ=None,
        ref_id: str | None = None,
    ) -> tuple[dict | None, str | None]:
        """Return ``(record, outcome)`` without assigning resolver semantics.

        Outcomes are ``matched``, ``omitted``, and ``unavailable``.  A missing
        declared DOI has no batch outcome at all, so callers retain the scalar
        title-search behaviour without an accelerator diagnostic.
        """
        with self._lock:
            self._normalize_doi = normalize_doi
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
            if completed is None:
                if entry is None:
                    entry = _BatchEntry(key, ref_id)
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
            self._dispatch(
                get_fn=get_fn,
                normalize_doi=normalize_doi,
                email=email,
                environ=environ,
            )
        entry.ready.wait()
        with self._lock:
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

    def _dispatch(self, *, get_fn, normalize_doi, email, environ) -> None:
        # A short wait collects simultaneous phase workers.  It is injectable
        # for deterministic tests; serial callers still become the leader.
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
                for offset in range(0, len(entries), 100):
                    chunk = entries[offset:offset + 100]
                    from core.resolve import transport_telemetry

                    # Consumers may join an already-claimed DOI while its request is
                    # in flight. Snapshot the mutable reference-ID lists under the
                    # session lock;
                    # later consumers add their mapping after the operation completes.
                    with self._lock:
                        ref_ids = [
                            ref_id for entry in chunk for ref_id in entry.ref_ids
                        ]
                        for entry in chunk:
                            entry.mapped_ref_ids.update(entry.ref_ids)
                    operation_id = None
                    if ref_ids:
                        with transport_telemetry.operation(
                            provider="openalex",
                            operation="doi_lookup",
                            mode="batch",
                            ref_ids=ref_ids,
                            item_count=len(chunk),
                            chunk_index=offset // 100,
                        ) as operation_id:
                            records = self._fetch_chunk(
                                [entry.doi for entry in chunk],
                                get_fn=get_fn,
                                normalize_doi=normalize_doi,
                                email=email,
                                environ=environ,
                            )
                    else:
                        records = self._fetch_chunk(
                            [entry.doi for entry in chunk],
                            get_fn=get_fn,
                            normalize_doi=normalize_doi,
                            email=email,
                            environ=environ,
                        )
                    with self._lock:
                        if self._closed:
                            # close() already published unavailable before
                            # waking waiters; never replace that outcome.
                            continue
                        for entry in chunk:
                            self._operation_ids[entry.doi] = operation_id
                            # None deliberately falls through to scalar discovery.
                            if records is None:
                                self._completed[entry.doi] = (None, "unavailable")
                            elif entry.doi in records:
                                self._completed[entry.doi] = (records[entry.doi], "matched")
                            else:
                                self._completed[entry.doi] = (None, "omitted")
                            entry.ready.set()
        except Exception:
            # An accelerator failure must be observationally equivalent to no
            # accelerator.  In particular, no worker can be stranded behind a
            # claimed entry if response validation itself raises unexpectedly.
            with self._lock:
                for doi, entry in self._pending.items():
                    self._completed.setdefault(doi, (None, "unavailable"))
                    entry.ready.set()
                self._dispatching = False

    def predecessor_operation_id(self, doi: str | None) -> str | None:
        return self._operation_ids.get(str(doi or "").lower())

    def matched_record(self, doi: str | None) -> dict | None:
        """Return one completed exact DOI batch match for Fetch runtime reuse."""
        with self._lock:
            normalize_doi = self._normalize_doi
        if not callable(normalize_doi):
            return None
        try:
            normalized = normalize_doi(doi)
        except Exception:
            return None
        if not normalized:
            return None
        with self._lock:
            record, outcome = self._completed.get(normalized.lower(), (None, None))
        return dict(record) if outcome == "matched" and isinstance(record, dict) else None

    @staticmethod
    def _fetch_chunk(
        dois, *, get_fn, normalize_doi, email, environ
    ) -> dict[str, dict] | None:
        params = {
            "filter": "doi:" + "|".join(f"https://doi.org/{doi}" for doi in dois),
            "per_page": str(len(dois)),
            "select": _BATCH_SELECT,
        }
        if email:
            params["mailto"] = email
        api_key = _api_key(environ)
        if api_key:
            params["api_key"] = api_key
        try:
            query = urllib.parse.urlencode(params)
            _status, body = get_fn(f"{OPENALEX_WORKS_URL}?{query}")
            data = json.loads(body)
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                return None
        except Exception:
            return None
        out = {}
        for record in results:
            if not isinstance(record, dict):
                return None
            returned = normalize_doi(record.get("doi"))
            if not returned:
                return None
            returned_key = returned.lower()
            if returned_key in dois:
                out[returned_key] = record
        return out


def new_batch_session(*, coalesce_delay: float = 0.01, sleep_fn=time.sleep) -> OpenAlexBatchSession:
    return OpenAlexBatchSession(coalesce_delay=coalesce_delay, sleep_fn=sleep_fn)


class _BatchBinding:
    def __init__(self, session):
        self._session = session
        self._previous = None

    def __enter__(self):
        self._previous = getattr(_BATCH_LOCAL, "session", None)
        _BATCH_LOCAL.session = self._session
        return self._session

    def __exit__(self, *_exc):
        _BATCH_LOCAL.session = self._previous


def bind_batch_session(session):
    return _BatchBinding(session)


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    return (
        not ref.get("url")
        and resolve_mod._article_like_resolution_candidate(ref)
    )


def _api_key(environ: dict[str, str] | None = None) -> str | None:
    env = environ or os.environ
    value = str(env.get("OPENALEX_API_KEY") or "").strip()
    return value or None


def enabled(*, email=None, environ=None, **kwargs) -> bool:
    return bool((email or "").strip()) or bool(_api_key(environ))


def disabled_reason(*, email=None, environ=None, **kwargs) -> str | None:
    if (email or "").strip() or _api_key(environ):
        return None
    return "missing contact email or API key"


def query_json(params: dict[str, str], *, get_fn, environ: dict[str, str] | None = None) -> dict | None:
    api_key = _api_key(environ)
    if api_key and "api_key" not in params:
        params["api_key"] = api_key
    query = urllib.parse.urlencode(params)
    url = f"{OPENALEX_WORKS_URL}?{query}"
    try:
        _status, body = get_fn(
            url, accept="application/json", profile="api"
        )
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


def work(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    email: str | None = None,
    environ: dict[str, str] | None = None,
) -> dict | None:
    doi = normalize_doi(ref.get("doi"))
    if not doi:
        return None
    lookup = getattr(get_fn, "provider_record_lookup", None)
    if callable(lookup):
        try:
            cached = lookup(doi)
        except Exception:
            cached = None
        cached_doi = normalize_doi(cached.get("doi")) if isinstance(cached, dict) else None
        if cached_doi and str(cached_doi).casefold() == str(doi).casefold():
            # Project the richer Resolve record onto the established scalar
            # Fetch response shape so reuse cannot strengthen identity context.
            return {key: cached[key] for key in _FETCH_WORK_FIELDS if key in cached}
    params = {
        "filter": f"doi:{doi}",
        "per_page": "1",
        "select": ",".join(_FETCH_WORK_FIELDS),
    }
    if email:
        params["mailto"] = email
    api_key = _api_key(environ)
    if api_key:
        params["api_key"] = api_key
    data = query_json(params, get_fn=get_fn, environ=environ)
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not results:
        return None
    candidate = results[0]
    return candidate if isinstance(candidate, dict) else None


def raw_source_pdf_url(raw_source_name: str | None) -> str | None:
    if not raw_source_name:
        return None
    text = str(raw_source_name).strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text)
    basename = (parsed.path or "").rsplit("/", 1)[-1].strip()
    if not basename:
        return None
    basename_lower = basename.lower()
    host = (parsed.netloc or "").lower()
    if basename_lower.endswith(".pdf") and "idsia" in host:
        return urllib.parse.urljoin(_IDSIA_MIRROR_BASE, basename)
    mapped = _IDSIA_ALIAS_MAP.get(basename_lower)
    if mapped and ("idsia" in host or "brauer" in host):
        return urllib.parse.urljoin(_IDSIA_MIRROR_BASE, mapped)
    return None


def pdf_url_from_doi(
    doi: str | None,
    *,
    get_fn,
    email: str | None = None,
    environ: dict[str, str] | None = None,
    **kwargs,
) -> str | None:
    doi_text = str(doi or "").strip()
    if not doi_text:
        return None
    hit = work(
        {"doi": doi_text},
        get_fn=get_fn,
        normalize_doi=lambda value: value,
        email=email,
        environ=environ,
    )
    if not hit:
        return None
    for item in candidate_items(
        {"doi": doi_text},
        get_fn=get_fn,
        normalize_doi=lambda value: value,
        kind_from_url=lambda url: "pdf" if str(url or "").lower().endswith(".pdf") else "landing",
        email=email,
        environ=environ,
    ):
        if item.get("kind") == "pdf" and item.get("url"):
            return item["url"]
    return None


def landing_to_pdf(
    url: str | None,
    **kwargs,
) -> str | None:
    return raw_source_pdf_url(url)


def _reconstruct_abstract(resolve_mod, inv_idx: dict | None) -> str | None:
    if not isinstance(inv_idx, dict) or not inv_idx:
        return None
    positions = {}
    max_pos = -1
    for token, offsets in inv_idx.items():
        if not isinstance(token, str) or not isinstance(offsets, list):
            continue
        for pos in offsets:
            if not isinstance(pos, int) or pos < 0:
                continue
            positions[pos] = token
            if pos > max_pos:
                max_pos = pos
    if max_pos < 0:
        return None
    words = [positions.get(i, "") for i in range(max_pos + 1)]
    text = " ".join(word for word in words if word).strip()
    return text or None


def _fulltext_links(resolve_mod, rec: dict, *, include_doi: bool) -> list[dict]:
    fl_links = []
    seen = set()

    def add(url: str | None, content_type: str, version: str | None = None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        row = {"url": url, "content_type": content_type}
        if version:
            row["content_version"] = resolve_mod._sources.content_version_for(url, version)
        fl_links.append(row)

    if include_doi:
        doi_raw = rec.get("doi") or ""
        doi = re.sub(r"^https?://doi\.org/", "", doi_raw, flags=re.IGNORECASE) or None
        if doi:
            add(f"https://doi.org/{doi}", "doi")
    oa_info = rec.get("open_access") or {}
    if isinstance(oa_info, dict):
        add(oa_info.get("oa_url"), "pdf")
    for key in ("best_oa_location", "primary_location"):
        loc = rec.get(key) or {}
        if not isinstance(loc, dict):
            continue
        version = loc.get("version")
        add(loc.get("pdf_url"), "pdf", version)
        add(resolve_mod._openalex_raw_source_pdf_url(loc.get("raw_source_name")), "pdf", version)
    for loc in rec.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        version = loc.get("version")
        add(loc.get("pdf_url"), "pdf", version)
        add(resolve_mod._openalex_raw_source_pdf_url(loc.get("raw_source_name")), "pdf", version)
    return fl_links


def _official_arxiv_id(url: str | None) -> str | None:
    """Return an arXiv identifier only from an official arxiv.org route."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "arxiv.org":
        return None
    match = re.fullmatch(
        r"/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)(?:\.pdf)?/?",
        urllib.parse.unquote(parsed.path),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def enrich(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    doi = (ref.get("doi") or "").strip()
    if not doi:
        return None

    def result_from_record(rec: dict) -> dict:
        fl_links = _fulltext_links(resolve_mod, rec, include_doi=True)
        return {
            "status": "resolved",
            "via": "openalex",
            "matched_title": rec.get("title"),
            "abstract": _reconstruct_abstract(resolve_mod, rec.get("abstract_inverted_index")),
            "fulltext_links": fl_links,
            "reason": None,
        }

    batch = getattr(_BATCH_LOCAL, "session", None)
    predecessor = None
    if batch is not None and batch_enabled():
        batch_record, batch_outcome = batch.lookup(
            doi,
            get_fn=resolve_mod._get,
            normalize_doi=resolve_mod._normalize_doi_value,
            email=resolve_mod._http._CONTACT_EMAIL,
            ref_id=ref.get("id"),
        )
        if batch_record is not None:
            return result_from_record(batch_record)
        if batch_outcome is not None:
            predecessor = batch.predecessor_operation_id(doi)
    params = {
        "filter": f"doi:{doi}",
        "per_page": "1",
        "select": (
            "doi,title,abstract_inverted_index,open_access,best_oa_location,"
            "primary_location,locations"
        ),
    }
    if resolve_mod._http._CONTACT_EMAIL:
        params["mailto"] = resolve_mod._http._CONTACT_EMAIL
    if resolve_mod._OPENALEX_API_KEY:
        params["api_key"] = resolve_mod._OPENALEX_API_KEY
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    try:
        ref_id = ref.get("id")
        if ref_id:
            from core.resolve import transport_telemetry

            with transport_telemetry.operation(
                provider="openalex",
                operation="doi_lookup",
                mode="scalar",
                ref_ids=[ref_id],
                predecessor_operation_id=predecessor,
            ):
                status, body = resolve_mod._get(url)
        else:
            _status, body = resolve_mod._get(url)
        results = (json.loads(body).get("results") or [])
        if not results:
            return {
                "status": "unverified",
                "via": "openalex",
                "reason": "no match on OpenAlex enrichment",
            }
        rec = results[0] or {}
        return result_from_record(rec)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": "openalex",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {
            "status": "unresolved",
            "via": "openalex",
            "reason": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "unresolved",
            "via": "openalex",
            "reason": f"network: {type(exc).__name__}",
        }


def _to_crossref_like(oa_rec: dict) -> dict:
    authors = oa_rec.get("authorships") or []
    author_list = []
    if authors:
        fa = authors[0].get("author") or {}
        surname = parts[-1] if (parts := fa.get("display_name", "").strip().split()) else None
        if surname:
            author_list.append({"family": surname})
    title = oa_rec.get("title")
    pub_year = oa_rec.get("publication_year")
    venue = None
    loc = oa_rec.get("primary_location") or {}
    source = loc.get("source") or {}
    # A repository is a location/version carrier, not the publication venue.
    # Projecting its display name as ``container-title`` creates a fictitious
    # venue conflict against the journal named by the citation.
    if source and str(source.get("type") or "").casefold() != "repository":
        venue = source.get("display_name")
    return {
        "title": [title] if title else None,
        "author": author_list,
        "published-print": {"date-parts": [[pub_year]]} if pub_year else {},
        "container-title": [venue] if venue else [],
    }


def _filter_safe(text: str) -> str:
    cleaned = (text or "").replace(",", " ").replace("|", " ").replace("?", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _result_from_records(
    ref: dict, results: list[dict], *, resolve_mod, via: str,
    year_filter_applied: bool, reason: str,
    identity_search_complete: bool | None = None,
) -> dict:
    """Apply the scalar OpenAlex admission rules to one or more records."""
    def identity_search(outcome: str) -> dict:
        if identity_search_complete is None:
            return {}
        return {
            "identity_search": {
                "resolver": "openalex",
                "query_contract": "OpenAlex bounded title search",
                "completion": "complete" if identity_search_complete else "incomplete",
                "outcome": outcome if identity_search_complete else "inconclusive",
            },
        }

    year = ref.get("year")
    if not results:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no results on OpenAlex",
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            **identity_search("no_compatible_identity"),
        }
    best_rec = None
    best_score = -1.0
    best_profile = None
    for rec in results:
        msg = _to_crossref_like(rec)
        profile = resolve_mod._metadata_match_profile(ref, msg, rec.get("title") or "")
        if profile["score"] > best_score:
            best_score = profile["score"]
            best_rec = rec
            best_profile = profile

    matched_title = (best_rec or {}).get("title")
    overlap = best_profile["title_overlap"] if best_profile else None
    year_mismatch = bool(
        not year_filter_applied
        and ref.get("year") is not None
        and best_rec is not None
        and best_rec.get("publication_year") is not None
        and int(ref["year"]) != int(best_rec["publication_year"])
        and best_score < 0.60
    )
    if year_mismatch:
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"year mismatch (cited {ref['year']}, "
                f"matched {best_rec['publication_year']}, score {best_score:.3f})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": best_profile,
            **identity_search("candidate_incompatible"),
        }
    if overlap is not None and overlap < resolve_mod.TITLE_MISMATCH_MAX:
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"title too dissimilar (overlap {overlap:.3f} < {resolve_mod.TITLE_MISMATCH_MAX}); "
                "author/year match is not enough to identify the cited work"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": best_profile,
            **identity_search("candidate_incompatible"),
        }
    if best_rec is None or best_score < 0.30:
        return {
            "status": "unverified",
            "via": via,
            "matched_title": matched_title,
            "reason": (
                f"best match below confidence threshold "
                f"(score {best_score:.3f}, overlap {overlap})"
            ),
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "metadata_match": best_profile,
            **identity_search("candidate_incompatible"),
        }

    oa_info = best_rec.get("open_access") or {}
    fl_links = _fulltext_links(resolve_mod, best_rec, include_doi=True)
    has_fetchable_fulltext = resolve_mod._links_have_fulltext(fl_links)
    availability = {
        "status": "available" if has_fetchable_fulltext else "unknown",
        "scope": "provider",
        "observed_by": via,
        "reason": (
            "OpenAlex exposed a fetchable full-text location"
            if has_fetchable_fulltext
            else "provider returned no known full-text location"
        ),
    }
    return {
        "status": "resolved",
        "via": via,
        "matched_title": matched_title,
        "abstract": _reconstruct_abstract(resolve_mod, best_rec.get("abstract_inverted_index")),
        "retracted": False,
        "reason": reason,
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        # A missing OpenAlex location is provider-scoped negative evidence, not
        # proof that the work has no full text elsewhere.
        "fulltext_exists": True if has_fetchable_fulltext else "unknown",
        "fulltext_availability": availability,
        "oa_status": oa_info.get("oa_status") or "unknown",
        "fulltext_links": fl_links,
        "metadata_match": best_profile,
    }


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    """Title+author search on OpenAlex - covers journals, arXiv, conference papers, etc."""
    via = RESOLVE_NAME
    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": via,
            "reason": "no usable title for OpenAlex search",
        }
    batch_note = None

    def with_batch_note(result: dict) -> dict:
        if batch_note is None:
            return result
        out = dict(result)
        reason = str(out.get("reason") or "").strip()
        out["reason"] = f"{reason}; {batch_note}" if reason else batch_note
        return out

    batch = getattr(_BATCH_LOCAL, "session", None)
    if batch is not None and batch_enabled():
        batch_record, batch_outcome = batch.lookup(
            ref.get("doi"),
            get_fn=resolve_mod._get,
            normalize_doi=resolve_mod._normalize_doi_value,
            email=resolve_mod._http._CONTACT_EMAIL,
            ref_id=ref.get("id"),
        )
        if batch_record is not None:
            batch_note = "OpenAlex batch matched declared DOI"
            return with_batch_note(_result_from_records(
                ref, [batch_record], resolve_mod=resolve_mod, via=via,
                year_filter_applied=False, reason="OpenAlex DOI batch match",
                identity_search_complete=None,
            ))
        if batch_outcome == "omitted":
            batch_note = "batch omitted declared DOI; scalar fallback"
        elif batch_outcome == "unavailable":
            batch_note = "batch unavailable; scalar fallback"
    year = ref.get("year")

    def _search_oa(yr: int | None) -> tuple[list[dict], bool]:
        filters = [f"title.search:{_filter_safe(title[:200])}"]
        yr_applied = False
        if yr is not None:
            filters.append(f"publication_year:{yr}")
            yr_applied = True
        params: dict[str, str] = {
            "filter": ",".join(filters), "per_page": "3", "select": _BATCH_SELECT,
        }
        if resolve_mod._http._CONTACT_EMAIL:
            params["mailto"] = resolve_mod._http._CONTACT_EMAIL
        if resolve_mod._OPENALEX_API_KEY:
            params["api_key"] = resolve_mod._OPENALEX_API_KEY
        url = f"{OPENALEX_WORKS_URL}?" + urllib.parse.urlencode(params)
        ref_id = ref.get("id")
        if ref_id:
            from core.resolve import transport_telemetry

            predecessor_doi = resolve_mod._normalize_doi_value(ref.get("doi"))
            predecessor = (
                batch.predecessor_operation_id(predecessor_doi)
                if batch_note
                else None
            )
            with transport_telemetry.operation(
                provider="openalex",
                operation="title_search",
                mode="scalar",
                ref_ids=[ref_id],
                predecessor_operation_id=predecessor,
            ):
                status, body = resolve_mod._get(url)
        else:
            status, body = resolve_mod._get(url)
        payload = json.loads(body)
        results = payload.get("results") if isinstance(payload, dict) else None
        records_valid = isinstance(results, list) and all(
            isinstance(record, dict)
            and isinstance(record.get("title"), str)
            and bool(record["title"].strip())
            for record in results
        )
        if status != 200 or not records_valid:
            raise ValueError("OpenAlex title search response is malformed")
        return results, yr_applied

    year_filter_applied = False
    identity_search_complete = True
    for oa_attempt in range(2):
        try:
            results, year_filter_applied = _search_oa(year)
            if year is not None and len(results) < 3:
                try:
                    results_no_year, _ = _search_oa(None)
                except Exception:
                    identity_search_complete = False
                    results_no_year = []
                seen_ids = set(record.get("id") for record in results)
                for record in results_no_year:
                    if record.get("id") not in seen_ids:
                        results.append(record)
                results = results[:5]
                year_filter_applied = False
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                resolve_mod.time.sleep(1.0 * (oa_attempt + 1))
                continue
            return with_batch_note({"status": "unresolved", "via": via, "reason": f"HTTP {exc.code}"})
        except Exception as exc:
            return with_batch_note({"status": "unresolved", "via": via, "reason": f"network: {type(exc).__name__}"})
    else:
        return with_batch_note({"status": "unresolved", "via": via,
                                "reason": "rate_limited (HTTP 429) - NOT fabrication; falling back to arXiv"})
    return with_batch_note(_result_from_records(
        ref, results, resolve_mod=resolve_mod, via=via,
        year_filter_applied=year_filter_applied, reason="OpenAlex title search match",
        identity_search_complete=identity_search_complete,
    ))


def candidate_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    kind_from_url,
    email: str | None = None,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    hit = work(
        ref,
        get_fn=get_fn,
        normalize_doi=normalize_doi,
        email=email,
        environ=environ,
    )
    if not hit:
        return []
    seen = set()
    out = []
    record_id = hit.get("id")
    doi = normalize_doi(hit.get("doi") or ref.get("doi"))
    identity_context = {
        "provider": NAME,
        "provider_record_id": record_id,
        "title": hit.get("title"),
        "year": hit.get("publication_year"),
        "identifiers": {
            key: value for key, value in (("doi", doi), ("openalex_id", record_id))
            if value
        },
    }
    identity_context = {key: value for key, value in identity_context.items() if value not in (None, "", {})}

    def add(url: str | None, location: dict | None = None, *, source: str = "location"):
        url = str(url or "").strip()
        if not url or url in seen or len(out) >= MAX_CANDIDATE_ITEMS:
            return
        location = location if isinstance(location, dict) else {}
        item_context = dict(identity_context)
        arxiv_id = _official_arxiv_id(url)
        if arxiv_id:
            item_context["identifiers"] = {
                **item_context.get("identifiers", {}), "arxiv_id": arxiv_id,
            }
        out.append({
            "method": NAME,
            "url": url,
            "kind": kind_from_url(url),
            "content_version": _sources.content_version_for(url, location.get("version")),
            "discovered_via": NAME,
            "discovery_reason": f"OpenAlex location source: {source}",
            "provenance": [NAME],
            "identity_context": item_context,
            **({"fallback_stage": "oa_alternate"} if source == "location" else {}),
        })
        seen.add(url)

    # open_access.oa_url is OpenAlex's own best-effort OA pick — add it first
    # so it survives the MAX_CANDIDATE_ITEMS cap as the primary candidate,
    # matching pre-fallback-staging behaviour. It carries no fallback_stage,
    # so it stays in the primary queue like the other primary sources.
    open_access = hit.get("open_access")
    if isinstance(open_access, dict):
        add(open_access.get("oa_url"), {}, source="open_access")

    locations = []
    for key in ("best_oa_location", "primary_location"):
        loc = hit.get(key)
        if isinstance(loc, dict):
            locations.append((key, loc))
    for loc in hit.get("locations") or []:
        if isinstance(loc, dict):
            locations.append(("location", loc))
    direct_urls = []
    landing_urls = []
    for source, loc in locations:
        # Build the complete direct-PDF slice first. This lets a later
        # location's PDF survive the cap even when an earlier location has a
        # landing page.
        for url in (loc.get("pdf_url"), raw_source_pdf_url(loc.get("raw_source_name"))):
            if url:
                direct_urls.append((url, loc, source))
        if loc.get("landing_page_url"):
            landing_urls.append((loc.get("landing_page_url"), loc, source))
    for url, loc, source in direct_urls + landing_urls:
        add(url, loc, source=source)
    return out
