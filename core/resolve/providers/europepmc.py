#!/usr/bin/env python3
# core/resolve/providers/europepmc.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Europe PMC fetch + identifier-resolution helpers."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

try:
    from core.fetch.extraction import fetch_html as _fetch_html
except ImportError:
    import fetch_html as _fetch_html


NAME = "europepmc"
MANIFEST = {
    "origin": "europepmc",
    "canonical_hosts": ["europepmc.org", "ebi.ac.uk", "ncbi.nlm.nih.gov"],
    "via_aliases": ["europepmc"],
}
SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_XML_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
OPEN_AVAILABILITIES = {"open access", "free"}
XML_STYLE_MARKERS = ("xml", "jats", "nxml")
_ABSTRACT_ONLY_TYPES = (
    "meeting abstract",
    "meeting abstracts",
    "conference abstract",
    "conference paper abstract",
    "abstract",
    "published erratum",
)
_PUBMED_BATCH_ENV = "CITATION_VERIFIER_PUBMED_BATCH"
_PUBMED_BATCH_LOCAL = threading.local()
_PMC_IDCONV_BATCH_LOCAL = threading.local()
_PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
_PUBMED_ECITMATCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/ecitmatch.cgi?"
_PMC_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?"
JOURNAL_COVERAGE = {"resolver": "pubmed", "rule_version": "pubmed-issn-count/v1"}
ARTICLE_LOOKUP = {"resolver": "pubmed", "rule_version": "pubmed-ecitmatch/v1"}
_PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
_NCBI_AUDIT_QUERY_KEYS = frozenset({"db", "retmode", "rettype", "retmax", "term", "bdata"})


def _ncbi_audit_url(value: object) -> str:
    """Retain the bibliographic query while excluding credentials and contact data."""
    parsed = urllib.parse.urlsplit(str(value or ""))
    query = [
        (key, item) for key, item in urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True,
        )
        if key.casefold() in _NCBI_AUDIT_QUERY_KEYS
    ]
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((
        parsed.scheme, host, parsed.path, urllib.parse.urlencode(query), "",
    ))


def supports_article_lookup(ref: dict, authority: dict) -> bool:
    if not isinstance(ref, dict) or not isinstance(authority, dict) or not authority.get("canonical_title"):
        return False
    from core.resolve.service import _article_coordinate_eligible
    return _article_coordinate_eligible(ref)


def lookup_article(ref: dict, authority: dict) -> dict:
    """One canonical-title authorless ECitMatch lookup; never a ranked search."""
    from core.resolve.service import _coordinate_value, _pubmed_citation_match
    journal = authority.get("canonical_title") if isinstance(authority, dict) else None
    volume = _coordinate_value(ref, "volume")
    locator = next((_coordinate_value(ref, kind) for kind in ("article_page_range", "elocator", "article_number", "article_locator") if _coordinate_value(ref, kind)), None)
    first = re.split(r"[-–—]", str(locator or ""), maxsplit=1)[0].strip()
    year = str(ref.get("year") or "").strip()
    base = {"resolver": "pubmed", "rule_version": ARTICLE_LOOKUP["rule_version"], "query_contract": "ECitMatch authorless canonical-journal coordinates", "scope": "canonical journal/year/volume/first locator", "source_url": None, "http_status": None, "media_type": None, "body": None, "candidate": None}
    if not all((journal, volume, first, year)):
        return {**base, "completion": "incomplete", "match_status": "incomplete", "reason": "canonical coordinate tuple is incomplete"}
    result = _pubmed_citation_match(journal=journal, year=year, volume=str(volume), first_page=first, author="")
    body, url, status = result.pop("ecitmatch_body", None), result.pop("ecitmatch_url", None), result.pop("ecitmatch_http_status", None)
    complete = result.get("status") == "not_found" and status == 200 and isinstance(body, str)
    return {**base, "source_url": url, "http_status": status, "media_type": "text/plain" if isinstance(body, str) else None, "body": body, "completion": "complete" if complete else "incomplete", "match_status": "no_compatible_article" if complete else ("ambiguous" if result.get("status") == "ambiguous" else "incomplete"), "reason": result.get("reason") or "ECitMatch incomplete", "candidate": result if result.get("status") == "resolved" else None}


def probe_journal_coverage(authority: dict) -> dict:
    """Count an exact PubMed ISSN field query; title/ranked routes are excluded."""
    resolve_mod = _resolve_module()
    issns = authority.get("issns") if isinstance(authority, dict) else ()
    if not isinstance(issns, tuple) or not issns:
        raise ValueError("coverage authority has no registered ISSN")
    replies = []
    for issn in issns:
        url = resolve_mod._ncbi_url(_PUBMED_ESEARCH_URL, {
            "db": "pubmed", "retmode": "json", "term": f'"{issn}"[ISSN]', "retmax": "0",
        })
        audit_url = _ncbi_audit_url(url)
        try:
            status, body = resolve_mod._get(url)
            payload = json.loads(body).get("esearchresult")
            count = int(payload["count"]) if isinstance(payload, dict) and str(payload.get("count", "")).isdigit() else None
            error_list = payload.get("errorlist") if isinstance(payload, dict) else None
            warning_list = payload.get("warninglist") if isinstance(payload, dict) else None
            has_query_error = bool(payload.get("ERROR")) if isinstance(payload, dict) else False
            if isinstance(error_list, dict):
                has_query_error = has_query_error or any(bool(value) for value in error_list.values())
            elif error_list is not None:
                has_query_error = True
            if isinstance(warning_list, dict):
                has_query_error = has_query_error or any(
                    bool(warning_list.get(field))
                    for field in ("phrasesignored", "quotedphrasesnotfound")
                )
            elif warning_list is not None:
                has_query_error = True
            if count is None or has_query_error:
                raise ValueError("PubMed exact ISSN count response is malformed")
            replies.append((issn, int(status), count, payload, audit_url))
        except urllib.error.HTTPError as exc:
            replies.append((issn, exc.code, None, None, audit_url))
        except Exception as exc:
            return {"resolver": "pubmed", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": audit_url, "http_status": None, "response": None, "query_contract": "PubMed ISSN field count", "completion": "incomplete", "reason": f"network: {type(exc).__name__}"}
    found = next((item for item in replies if item[1] == 200 and item[2] > 0), None)
    if found:
        issn, status, count, payload, url = found
        return {"resolver": "pubmed", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "covered", "provider_journal_id": issn, "work_count": count, "source_url": url, "http_status": status, "response": payload, "query_contract": "PubMed ISSN field count", "completion": "complete", "reason": "exact PubMed ISSN query has indexed works"}
    if all(item[1] == 200 and item[2] == 0 for item in replies):
        response = {"queries": [
            {"issn": issn, "http_status": status, "source_url": source_url, "payload": payload}
            for issn, status, _count, payload, source_url in replies
        ]}
        return {"resolver": "pubmed", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "not_covered", "provider_journal_id": None, "work_count": 0, "source_url": replies[0][4], "http_status": 200, "response": response, "query_contract": "PubMed ISSN field count", "completion": "complete", "reason": "all exact PubMed ISSN queries returned zero"}
    return {"resolver": "pubmed", "rule_version": JOURNAL_COVERAGE["rule_version"], "status": "incomplete", "provider_journal_id": None, "work_count": None, "source_url": replies[0][4], "http_status": next((item[1] for item in replies if item[1] != 200), None), "response": None, "query_contract": "PubMed ISSN field count", "completion": "incomplete", "reason": "exact PubMed ISSN coverage probe did not complete"}


def pubmed_batch_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ or os.environ
    return str(env.get(_PUBMED_BATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


class _PubMedBatchEntry:
    def __init__(self, pmid: str, ref_id: str | None = None):
        self.pmid = pmid
        self.ref_ids = [ref_id] if ref_id else []
        self.mapped_ref_ids: set[str] = set()
        self.ready = threading.Event()
        self.claimed = False


class PubMedBatchSession:
    """Run-local EFetch coalescer for already-declared PubMed identifiers."""

    def __init__(self, *, coalesce_delay: float = 0.01, sleep_fn=time.sleep, chunk_size: int = 200):
        self._coalesce_delay = max(0.0, float(coalesce_delay))
        self._sleep = sleep_fn
        self._chunk_size = max(1, min(200, int(chunk_size)))
        self._lock = threading.Lock()
        self._pending: dict[str, _PubMedBatchEntry] = {}
        self._completed: dict[str, tuple[ET.Element | None, str]] = {}
        self._operation_ids: dict[str, str | None] = {}
        self._dispatching = False
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for pmid, entry in self._pending.items():
                self._completed.setdefault(pmid, (None, "unavailable"))
                entry.ready.set()

    def prime_many(self, items, *, get_fn, ncbi_url) -> None:
        """Populate declared PMID results before resolve workers start."""
        with self._lock:
            if self._closed:
                return
            for ref_id, pmid in items:
                key = str(pmid or "").strip()
                if not key:
                    continue
                entry = self._pending.get(key)
                if entry is None:
                    entry = _PubMedBatchEntry(key, ref_id)
                    self._pending[key] = entry
                elif ref_id and ref_id not in entry.ref_ids:
                    entry.ref_ids.append(ref_id)
            if not self._pending or self._dispatching:
                return
            self._dispatching = True
        self._dispatch(get_fn=get_fn, ncbi_url=ncbi_url)

    def lookup(
        self,
        pmid: str,
        *,
        get_fn,
        ncbi_url,
        ref_id: str | None = None,
    ) -> tuple[ET.Element | None, str | None]:
        key = str(pmid or "").strip()
        if not key:
            return None, None
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
                    entry = _PubMedBatchEntry(key, ref_id)
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
            self._dispatch(get_fn=get_fn, ncbi_url=ncbi_url)
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

    def _dispatch(self, *, get_fn, ncbi_url) -> None:
        try:
            while True:
                if self._coalesce_delay:
                    self._sleep(self._coalesce_delay)
                with self._lock:
                    if self._closed:
                        self._dispatching = False
                        return
                    entries = [entry for entry in self._pending.values() if not entry.claimed]
                    for entry in entries:
                        entry.claimed = True
                    if not entries:
                        self._dispatching = False
                        return
                for offset in range(0, len(entries), self._chunk_size):
                    chunk = entries[offset:offset + self._chunk_size]
                    from core.resolve import transport_telemetry

                    # A duplicate PMID can join a claimed entry concurrently. Copy
                    # ordered reference IDs while holding the lookup() lock.
                    with self._lock:
                        ref_ids = [
                            ref_id for entry in chunk for ref_id in entry.ref_ids
                        ]
                        for entry in chunk:
                            entry.mapped_ref_ids.update(entry.ref_ids)
                    operation_id = None
                    if ref_ids:
                        with transport_telemetry.operation(
                            provider="pubmed",
                            operation="efetch",
                            mode="batch",
                            ref_ids=ref_ids,
                            item_count=len(chunk),
                            chunk_index=offset // self._chunk_size,
                        ) as operation_id:
                            records = self._fetch_chunk(
                                [entry.pmid for entry in chunk],
                                get_fn=get_fn,
                                ncbi_url=ncbi_url,
                            )
                    else:
                        records = self._fetch_chunk(
                            [entry.pmid for entry in chunk],
                            get_fn=get_fn,
                            ncbi_url=ncbi_url,
                        )
                    with self._lock:
                        if self._closed:
                            continue
                        for entry in chunk:
                            self._operation_ids[entry.pmid] = operation_id
                            if records is None:
                                self._completed[entry.pmid] = (None, "unavailable")
                            elif entry.pmid in records:
                                self._completed[entry.pmid] = (records[entry.pmid], "matched")
                            else:
                                self._completed[entry.pmid] = (None, "omitted")
                            entry.ready.set()
        except Exception:
            with self._lock:
                for pmid, entry in self._pending.items():
                    self._completed.setdefault(pmid, (None, "unavailable"))
                    entry.ready.set()
                self._dispatching = False

    def predecessor_operation_id(self, pmid: str) -> str | None:
        return self._operation_ids.get(str(pmid or "").strip())

    @staticmethod
    def _fetch_chunk(pmids, *, get_fn, ncbi_url) -> dict[str, ET.Element] | None:
        try:
            url = ncbi_url(_PUBMED_EFETCH_URL, {
                "db": "pubmed", "retmode": "xml", "id": ",".join(pmids),
            })
            status, body = get_fn(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.8")
            if not isinstance(status, int) or not 200 <= status < 300:
                return None
            root = ET.fromstring(body)
            if _xml_local_name(root.tag) != "PubmedArticleSet":
                return None
            expected = set(pmids)
            records: dict[str, ET.Element] = {}
            for article in root:
                if _xml_local_name(article.tag) != "PubmedArticle":
                    return None
                pmid = _article_pmid(article)
                if (not pmid or pmid not in expected or pmid in records
                        or _pubmed_batch_metadata_from_article(article, pmid) is None):
                    return None
                records[pmid] = article
            return records
        except Exception:
            return None


def new_pubmed_batch_session(**kwargs) -> PubMedBatchSession:
    return PubMedBatchSession(**kwargs)


class _PubMedBatchBinding:
    def __init__(self, session):
        self._session, self._previous = session, None

    def __enter__(self):
        self._previous = getattr(_PUBMED_BATCH_LOCAL, "session", None)
        _PUBMED_BATCH_LOCAL.session = self._session
        return self._session

    def __exit__(self, *_exc):
        _PUBMED_BATCH_LOCAL.session = self._previous


def bind_pubmed_batch_session(session):
    return _PubMedBatchBinding(session)


class _PmcIdConverterBatchEntry:
    def __init__(self, pmid: str, ref_id: str | None = None):
        self.pmid = pmid
        self.ref_ids = [ref_id] if ref_id else []
        self.mapped_ref_ids: set[str] = set()
        self.ready = threading.Event()
        self.claimed = False


class PmcIdConverterBatchSession:
    """Run-local PMID-only PMC converter coalescer."""

    def __init__(
        self,
        *,
        coalesce_delay: float = 0.01,
        sleep_fn=time.sleep,
        chunk_size: int = 200,
    ):
        self._coalesce_delay = max(0.0, float(coalesce_delay))
        self._sleep = sleep_fn
        self._chunk_size = max(1, min(200, int(chunk_size)))
        self._lock = threading.Lock()
        self._pending: dict[str, _PmcIdConverterBatchEntry] = {}
        self._completed: dict[str, tuple[str | None, str]] = {}
        self._operation_ids: dict[str, str | None] = {}
        self._dispatching = False
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for pmid, entry in self._pending.items():
                self._completed.setdefault(pmid, (None, "unavailable"))
                entry.ready.set()

    @staticmethod
    def _pmid(value) -> str | None:
        text = str(value or "").strip()
        return text if re.fullmatch(r"[0-9]+", text) else None

    def prime_many(self, items, *, get_fn, ncbi_url) -> None:
        with self._lock:
            if self._closed:
                return
            for ref_id, value in items:
                pmid = self._pmid(value)
                if not pmid:
                    continue
                entry = self._pending.get(pmid)
                if entry is None:
                    entry = _PmcIdConverterBatchEntry(pmid, ref_id)
                    self._pending[pmid] = entry
                elif ref_id and ref_id not in entry.ref_ids:
                    entry.ref_ids.append(ref_id)
            if not self._pending or self._dispatching:
                return
            self._dispatching = True
        self._dispatch(get_fn=get_fn, ncbi_url=ncbi_url)

    def lookup(
        self,
        pmid,
        *,
        get_fn,
        ncbi_url,
        ref_id=None,
    ) -> tuple[str | None, str | None]:
        key = self._pmid(pmid)
        if not key:
            return None, None
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
                    entry = _PmcIdConverterBatchEntry(key, ref_id)
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
            self._dispatch(get_fn=get_fn, ncbi_url=ncbi_url)
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

    def _dispatch(self, *, get_fn, ncbi_url) -> None:
        try:
            while True:
                if self._coalesce_delay:
                    self._sleep(self._coalesce_delay)
                with self._lock:
                    if self._closed:
                        self._dispatching = False
                        return
                    entries = [
                        entry for entry in self._pending.values()
                        if not entry.claimed
                    ]
                    for entry in entries:
                        entry.claimed = True
                    if not entries:
                        self._dispatching = False
                        return
                for offset in range(0, len(entries), self._chunk_size):
                    chunk = entries[offset:offset + self._chunk_size]
                    from core.resolve import transport_telemetry

                    with self._lock:
                        ref_ids = [
                            ref_id for entry in chunk for ref_id in entry.ref_ids
                        ]
                        for entry in chunk:
                            entry.mapped_ref_ids.update(entry.ref_ids)
                    operation_id = None
                    if ref_ids:
                        with transport_telemetry.operation(
                            provider="pubmed",
                            operation="pmc_idconv",
                            mode="batch",
                            ref_ids=ref_ids,
                            item_count=len(chunk),
                            chunk_index=offset // self._chunk_size,
                        ) as operation_id:
                            records = self._fetch_chunk(
                                [entry.pmid for entry in chunk],
                                get_fn=get_fn,
                                ncbi_url=ncbi_url,
                            )
                    else:
                        records = self._fetch_chunk(
                            [entry.pmid for entry in chunk],
                            get_fn=get_fn,
                            ncbi_url=ncbi_url,
                        )
                    with self._lock:
                        if self._closed:
                            continue
                        for entry in chunk:
                            self._operation_ids[entry.pmid] = operation_id
                            if records is None:
                                outcome = (None, "unavailable")
                            elif entry.pmid in records:
                                outcome = (records[entry.pmid], "matched")
                            else:
                                outcome = (None, "omitted")
                            self._completed[entry.pmid] = outcome
                            entry.ready.set()
        except Exception:
            with self._lock:
                for pmid, entry in self._pending.items():
                    self._completed.setdefault(pmid, (None, "unavailable"))
                    entry.ready.set()
                self._dispatching = False

    def predecessor_operation_id(self, pmid) -> str | None:
        return self._operation_ids.get(str(pmid or "").strip())

    @staticmethod
    def _fetch_chunk(pmids, *, get_fn, ncbi_url) -> dict[str, str] | None:
        try:
            url = ncbi_url(_PMC_IDCONV_URL, {
                "ids": ",".join(pmids),
                "format": "json",
                "idtype": "pmid",
            })
            status, body = get_fn(url)
            if not isinstance(status, int) or not 200 <= status < 300:
                return None
            payload = json.loads(body)
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                return None
            expected = set(pmids)
            seen: set[str] = set()
            out: dict[str, str] = {}
            for record in records:
                if not isinstance(record, dict):
                    return None
                pmid = PmcIdConverterBatchSession._pmid(record.get("pmid"))
                if pmid is None:
                    if record.get("pmcid"):
                        return None
                    continue
                if pmid not in expected or pmid in seen:
                    return None
                seen.add(pmid)
                pmcid = _normalize_pmcid(record.get("pmcid"))
                if pmcid:
                    out[pmid] = pmcid
            return out
        except Exception:
            return None


def new_pmc_idconv_batch_session(**kwargs):
    return PmcIdConverterBatchSession(**kwargs)


class _PmcIdconvBinding:
    def __init__(self, session):
        self._session, self._previous = session, None

    def __enter__(self):
        self._previous = getattr(_PMC_IDCONV_BATCH_LOCAL, "session", None)
        _PMC_IDCONV_BATCH_LOCAL.session = self._session
        return self._session

    def __exit__(self, *_exc):
        _PMC_IDCONV_BATCH_LOCAL.session = self._previous


def bind_pmc_idconv_batch_session(session):
    return _PmcIdconvBinding(session)


def _normalize_pmcid(value: object) -> str | None:
    """Return the canonical PMC identifier, or ``None`` for an invalid one."""
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"(?:https?://(?:(?:www\.)?ncbi\.nlm\.nih\.gov/pmc|pmc\.ncbi\.nlm\.nih\.gov)/articles/)?"
        r"(?:PMC)?(\d+)(?:/)?",
        text,
        re.I,
    )
    if not match:
        return None
    return f"PMC{match.group(1)}"


def _resolve_module():
    try:
        from core.resolve import service as resolve_mod
    except ImportError:
        import resolve as resolve_mod
    return resolve_mod


def _queries(ref: dict, normalize_doi) -> list[str]:
    out = []
    pmcid = _normalize_pmcid(ref.get("pmcid"))
    if pmcid:
        out.append(f"PMCID:{pmcid}")
    pmid = str(ref.get("pmid") or "").strip()
    if pmid:
        out.append(f"EXT_ID:{pmid} AND SRC:MED")
    doi = normalize_doi(ref.get("doi"))
    if doi:
        out.append(f'DOI:"{doi}"')
    return out


def _query_json(query: str, *, get_fn) -> dict | None:
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": "3",
    }
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        _status, body = get_fn(url, accept="application/json", profile="api")
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _results(ref: dict, *, get_fn, normalize_doi) -> list[dict]:
    for query in _queries(ref, normalize_doi):
        data = _query_json(query, get_fn=get_fn)
        if not isinstance(data, dict):
            continue
        results = [
            item for item in (data.get("resultList", {}).get("result") or [])
            if isinstance(item, dict)
        ]
        if results:
            return results
    return []


def _best_hit(ref: dict, results: list[dict], normalize_doi):
    pmcid = _normalize_pmcid(ref.get("pmcid"))
    pmid = str(ref.get("pmid") or "").strip()
    doi = normalize_doi(ref.get("doi"))
    if pmcid:
        for hit in results:
            if _normalize_pmcid(hit.get("pmcid")) == pmcid:
                return hit
        return None
    if pmid:
        for hit in results:
            if str(hit.get("pmid") or "").strip() == pmid:
                return hit
        return None
    if doi:
        for hit in results:
            if normalize_doi(hit.get("doi")) == doi:
                return hit
        return None
    return results[0] if results else None


def work(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    **kwargs,
) -> dict | None:
    if ref.get("pmcid") and not _normalize_pmcid(ref.get("pmcid")):
        return None
    results = _results(ref, get_fn=get_fn, normalize_doi=normalize_doi)
    if not results:
        return None
    return _best_hit(ref, results, normalize_doi)


def _fulltext_items(hit: dict) -> list[dict]:
    return [
        item for item in (hit.get("fullTextUrlList", {}).get("fullTextUrl") or [])
        if isinstance(item, dict) and item.get("url")
    ]


def _is_open_hit(hit: dict) -> bool:
    if hit.get("isOpenAccess") == "Y" or hit.get("inEPMC") == "Y" or hit.get("inPMC") == "Y":
        return True
    availabilities = {
        str(item.get("availability") or "").strip().lower()
        for item in _fulltext_items(hit)
    }
    return bool(availabilities & OPEN_AVAILABILITIES)


def _xml_url(hit: dict) -> str | None:
    pmcid = _normalize_pmcid(hit.get("pmcid"))
    if pmcid:
        return FULLTEXT_XML_URL.format(pmcid=urllib.parse.quote(pmcid, safe=""))
    for item in _fulltext_items(hit):
        style = str(item.get("documentStyle") or "").strip().lower()
        if any(marker in style for marker in XML_STYLE_MARKERS):
            return item.get("url")
    return None


def _style_kind(item: dict, kind_from_url) -> str:
    style = str(item.get("documentStyle") or "").strip().lower()
    if style:
        return "pdf" if "pdf" in style else "landing"
    # Europe PMC links can end in .pdf while returning an HTML redirect.
    # Without an explicit provider content type, keep them as landing pages.
    return "landing"


def _identity_context(hit: dict, pmcid: str, *, xml_root=None) -> dict:
    identifiers = {"pmcid": pmcid}
    doi = hit.get("doi")
    pmid = hit.get("pmid")
    title = hit.get("title")
    if xml_root is not None:
        for node in xml_root.findall(".//article-id"):
            id_type = (node.attrib.get("pub-id-type") or "").lower()
            if id_type == "doi" and node.text:
                doi = node.text.strip()
            if id_type in {"pmid", "pubmed"} and node.text:
                pmid = node.text.strip()
            if id_type in {"pmc", "pmcid"} and node.text:
                xml_pmcid = _normalize_pmcid(node.text)
                if xml_pmcid:
                    identifiers["pmcid"] = xml_pmcid
        title_node = xml_root.find(".//article-title")
        xml_title = _xml_text(title_node)
        if xml_title:
            title = xml_title
    if doi:
        identifiers["doi"] = str(doi).strip()
    if pmid:
        identifiers["pmid"] = str(pmid).strip()
    context = {"provider": NAME, "identifiers": identifiers, "canonical_host": True}
    if title:
        context["title"] = str(title).strip()
    return context


def direct_text_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    **kwargs,
) -> list[dict]:
    hit = work(ref, get_fn=get_fn, normalize_doi=normalize_doi)
    if not hit or not _is_open_hit(hit):
        return []
    xml_url = _xml_url(hit)
    if not xml_url:
        return []
    pmcid = _normalize_pmcid(hit.get("pmcid"))
    if not pmcid:
        return []
    try:
        _status, body = get_fn(
            xml_url,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
            profile="api",
        )
    except Exception:
        return []
    try:
        if not 200 <= int(_status) < 300:
            return []
    except (TypeError, ValueError):
        return []
    raw_xml = body.decode("utf-8", errors="replace")
    try:
        xml_root = ET.fromstring(body)
    except ET.ParseError:
        return []
    for node in xml_root.findall(".//article-id"):
        if (node.attrib.get("pub-id-type") or "").lower() in {"pmc", "pmcid"} and node.text:
            if _normalize_pmcid(node.text) != pmcid:
                return []
    text = _fetch_html.strip_tags(raw_xml)
    if not text:
        return []
    return [
        {
            "method": NAME,
            "text": text,
            "source_ref": xml_url,
            "extract_method": "api_xml",
            "identity_context": _identity_context(hit, pmcid, xml_root=xml_root),
        }
    ]


def candidate_items(
    ref: dict,
    *,
    get_fn,
    normalize_doi,
    kind_from_url,
    **kwargs,
) -> list[dict]:
    hit = work(ref, get_fn=get_fn, normalize_doi=normalize_doi)
    if not hit or not _is_open_hit(hit):
        return []
    pmcid = _normalize_pmcid(hit.get("pmcid"))
    if not pmcid:
        return []
    out = []
    seen = set()
    identity_context = _identity_context(hit, pmcid)
    items = [
        {"url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/", "documentStyle": "pdf"},
        {"url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", "documentStyle": "html"},
        *_fulltext_items(hit),
    ]
    for item in items:
        url = item.get("url")
        if not url or url in seen:
            continue
        style = str(item.get("documentStyle") or "").lower()
        if "xml" in style or "jats" in style or "nxml" in style:
            continue
        seen.add(url)
        out.append(
            {
                "method": NAME,
                "url": url,
                "kind": _style_kind(item, kind_from_url),
                "content_type": item.get("documentStyle"),
                "identity_context": identity_context,
            }
        )
    return out


def _epmc_fulltext_meta(rec: dict) -> dict:
    """Interpret Europe PMC full-text metadata deterministically."""
    pub_types = [pt.lower() for pt in (rec.get("pubTypeList", {}).get("pubType") or [])]
    work_type = pub_types[0] if pub_types else None
    is_abstract_only = any(any(token in pt for token in _ABSTRACT_ONLY_TYPES) for pt in pub_types)

    urls = rec.get("fullTextUrlList", {}).get("fullTextUrl") or []
    availabilities = {(url.get("availability") or "").lower() for url in urls}
    out_links = []
    seen_urls = set()
    for item in urls:
        url = item.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        out_links.append({
            "url": url,
            "content_type": item.get("documentStyle"),
            "availability": item.get("availability"),
            "site": item.get("site"),
        })
    in_epmc = rec.get("inEPMC") == "Y" or rec.get("inPMC") == "Y"
    is_oa = rec.get("isOpenAccess") == "Y"

    if is_abstract_only:
        return {
            "fulltext_exists": False,
            "oa_status": "unknown",
            "work_type": work_type,
            "fulltext_links": out_links,
        }
    if is_oa or in_epmc or {"open access", "free"} & availabilities:
        return {
            "fulltext_exists": True,
            "oa_status": "open",
            "work_type": work_type,
            "fulltext_links": out_links,
        }
    if "subscription required" in availabilities:
        return {
            "fulltext_exists": True,
            "oa_status": "paywalled",
            "work_type": work_type,
            "fulltext_links": out_links,
        }
    return {
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": work_type,
        "fulltext_links": out_links,
    }


def _epmc_record_richness(rec: dict) -> int:
    """Rank a Europe PMC record by identifier / open-access strength."""
    score = 0
    if _normalize_pmcid(rec.get("pmcid")):
        score += 4
    if rec.get("isOpenAccess") == "Y" or rec.get("inEPMC") == "Y" or rec.get("inPMC") == "Y":
        score += 2
    if rec.get("doi"):
        score += 1
    return score


def _prefer_richest_epmc_record(results: list[dict], title: str | None, resolve_mod) -> dict:
    """Pick the strongest same-work record from a title search.

    Europe PMC federates several sources (MED, PMC, AGR, PPR, …) and can rank a
    bare bibliographic record (e.g. AGRICOLA — no DOI, no PMCID, no OA link) ahead
    of the open-access PMC record for the same paper. Among the results whose
    title matches the citation, prefer the one carrying a PMCID / open-access /
    DOI signal so the full text stays reachable.
    """
    scorer = getattr(resolve_mod, "_title_match_score", lambda _a, _b: None)
    matching = [r for r in results if (scorer(title, r.get("title")) or 0) >= 0.90]
    return max(matching or results[:1], key=_epmc_record_richness)


def _europepmc(
    pmid: str | None,
    doi: str | None,
    title: str | None,
    year: int | str | None = None,
) -> dict:
    """Resolve via Europe PMC using PMID, DOI, or title."""
    resolve_mod = _resolve_module()
    if pmid:
        query, strong = f"EXT_ID:{pmid} AND SRC:MED", True
    elif doi:
        query, strong = f'DOI:"{doi}"', True
    elif title:
        query, strong = f'TITLE:"{title}"', False
    else:
        return {"status": "unverified", "via": "europepmc", "reason": "no identifier for lookup"}
    # A title search needs a few results so the richest same-work record (not just
    # the first federated hit) can be chosen; a strong id resolves to one record.
    page_size = 1 if strong else 6
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={urllib.parse.quote(query)}&format=json&resultType=core&pageSize={page_size}"
    )
    try:
        status, body = resolve_mod._get(url)
        payload = json.loads(body)
        result_list = payload.get("resultList") if isinstance(payload, dict) else None
        results = result_list.get("result") if isinstance(result_list, dict) else None
        records_valid = isinstance(results, list) and all(
            isinstance(record, dict)
            and bool(record)
            and (
                strong
                or (
                    isinstance(record.get("title"), str)
                    and bool(record["title"].strip())
                )
            )
            for record in results
        )
        if status != 200 or not isinstance(result_list, dict) or not records_valid:
            raise ValueError("Europe PMC title search response is malformed")
        if not results:
            if strong:
                return {
                    "status": "_empty_strong",
                    "via": "europepmc",
                    "reason": "no match on Europe PMC (strong ID)",
                }
            return {
                "status": "unverified",
                "via": "europepmc",
                "reason": "no title match on Europe PMC (indirect verification recommended)",
                "identity_search": {"resolver": "europepmc", "query_contract": "Europe PMC bounded title search", "completion": "complete", "outcome": "no_compatible_identity"},
            }
        rec = results[0] if strong else _prefer_richest_epmc_record(results, title, resolve_mod)
        matched_title = rec.get("title")
        rec_pmid = str(rec.get("pmid") or "").strip()
        rec_doi = str(rec.get("doi") or "").strip()
        normalize_doi = getattr(resolve_mod, "_normalize_doi_value", lambda value: str(value or "").strip())
        title_score = None
        if pmid and rec_pmid != str(pmid).strip():
            return {
                "status": "unverified", "via": "europepmc",
                "reason": "Europe PMC record did not confirm the requested PMID",
            }
        if doi and str(normalize_doi(rec_doi) or "").casefold() != str(
                normalize_doi(doi) or "").casefold():
            return {
                "status": "unverified", "via": "europepmc",
                "reason": "Europe PMC record did not confirm the requested DOI",
            }
        if not pmid and not doi:
            title_score = getattr(resolve_mod, "_title_match_score", lambda _a, _b: None)(title, matched_title)
            year_conflict = year is not None and rec.get("pubYear") is not None and str(year) != str(rec["pubYear"])
            if title_score is None or title_score < 0.90 or year_conflict:
                return {
                    "status": "unverified", "via": "europepmc",
                    "reason": "Europe PMC title result is not a strong same-work match",
                    "identity_search": {"resolver": "europepmc", "query_contract": "Europe PMC bounded title search", "completion": "complete", "outcome": "candidate_incompatible"},
                }
        retracted = bool(matched_title and matched_title.strip().lower().startswith("retracted"))
        if not retracted:
            epmc_doi = rec.get("doi") or doi
            if epmc_doi:
                retracted = resolve_mod._rw.is_retracted(epmc_doi)
        out = {
            "status": "resolved",
            "via": "europepmc",
            "matched_title": matched_title,
            "retracted": retracted,
            "abstract": rec.get("abstractText"),
            "reason": None,
            "matched_year": rec.get("pubYear"),
        }
        if title_score is not None:
            try:
                matched_year = int(rec["pubYear"])
            except (KeyError, TypeError, ValueError):
                matched_year = None
            out.update({
                "resolution_basis": "metadata_search",
                "existence_confidence": "medium",
                "metadata_match": {
                    "title_overlap": float(title_score),
                    "matched_year": matched_year,
                },
            })
        identifiers = {
            key: value
            for key, value in {
                "doi": rec.get("doi"),
                "pmid": rec.get("pmid"),
                "pmcid": _normalize_pmcid(rec.get("pmcid")),
            }.items()
            if value not in (None, "")
        }
        if identifiers:
            # Retain the record's identifiers on the resolve result.  Fetch can
            # then use an EPMC-confirmed PMCID without revisiting a publisher.
            out["identifiers"] = identifiers
            out.update(identifiers)
        out.update(_epmc_fulltext_meta(rec))
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": "europepmc",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": "europepmc", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": "europepmc", "reason": f"network: {type(exc).__name__}"}


def pubmed_exists(pmid: str) -> str:
    """Return ``exists``, ``absent``, or ``unknown`` for a PubMed identifier."""
    resolve_mod = _resolve_module()
    url = resolve_mod._ncbi_url(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?",
        {"db": "pubmed", "retmode": "json", "id": str(pmid)},
    )
    try:
        _status, body = resolve_mod._get(url)
        res = json.loads(body).get("result", {})
        uids = res.get("uids") or []
        if str(pmid) in uids:
            rec = res.get(str(pmid), {})
            return "absent" if rec.get("error") else "exists"
        return "absent"
    except urllib.error.HTTPError:
        return "unknown"
    except Exception:
        return "unknown"


def _pubmed_search_pmid(doi: str | None) -> str | None:
    pmid, _status = _pubmed_search_pmid_with_status(doi)
    return pmid


def _pubmed_search_pmid_with_status(doi: str | None) -> tuple[str | None, str]:
    """Return the PMID and the deterministic outcome of a DOI lookup."""
    resolve_mod = _resolve_module()
    doi = (doi or "").strip()
    if not doi:
        return None, "not_looked_up"
    url = resolve_mod._ncbi_url(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?",
        {"db": "pubmed", "retmode": "json", "term": f"{doi}[aid]", "retmax": "1"},
    )
    try:
        _status, body = resolve_mod._get(url)
        ids = (json.loads(body).get("esearchresult") or {}).get("idlist") or []
        pmid = str(ids[0]).strip() if ids else None
        return pmid, "resolved" if pmid else "not_found"
    except Exception:
        return None, "unresolved"


def _xml_text(node) -> str | None:
    if node is None:
        return None
    text = " ".join(part.strip() for part in node.itertext() if part and part.strip())
    return text or None


def _xml_local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _direct_children(node, name: str):
    return [child for child in node if _xml_local_name(child.tag) == name]


def _article_pmid(article) -> str | None:
    # Only the primary citation and primary PubmedData list establish the
    # record identity.  Corrections/references can legitimately name other
    # PMIDs and must never make this article ambiguous.
    medline = _direct_children(article, "MedlineCitation")
    pubmed_data = _direct_children(article, "PubmedData")
    if len(medline) != 1 or len(pubmed_data) != 1:
        return None
    id_lists = _direct_children(pubmed_data[0], "ArticleIdList")
    if len(id_lists) != 1:
        return None
    primary_pmids = _direct_children(medline[0], "PMID")
    if len(primary_pmids) != 1:
        return None
    primary_article_ids = [
        node for node in _direct_children(id_lists[0], "ArticleId")
        if (node.attrib.get("IdType") or "").strip().lower() == "pubmed"
    ]
    if len(primary_article_ids) > 1:
        return None
    medline_pmids = [
        (node.text or "").strip()
        for node in primary_pmids
    ]
    article_id_pmids = [
        (node.text or "").strip()
        for node in primary_article_ids
    ]
    if any(not value for value in medline_pmids + article_id_pmids):
        return None
    medline_pmid = medline_pmids[0]
    if article_id_pmids and article_id_pmids[0] != medline_pmid:
        return None
    return medline_pmid


def _pubmed_metadata_from_article(
    article, pmid: str, *, include_citation_fields: bool = False,
) -> dict | None:
    """Build scalar EFetch metadata using the same primary-record rules as batch."""
    return _pubmed_batch_metadata_from_article(
        article, pmid, include_citation_fields=include_citation_fields,
    )


def _pubmed_batch_metadata_from_article(
    article, pmid: str, *, include_citation_fields: bool = False,
) -> dict | None:
    """Build a namespace-aware payload only from the primary PubMed record."""
    if _article_pmid(article) != str(pmid):
        return None
    medline = _direct_children(article, "MedlineCitation")
    pubmed_data = _direct_children(article, "PubmedData")
    if len(medline) != 1 or len(pubmed_data) != 1:
        return None
    article_nodes = _direct_children(medline[0], "Article")
    id_lists = _direct_children(pubmed_data[0], "ArticleIdList")
    if len(article_nodes) != 1 or len(id_lists) != 1:
        return None
    title_nodes = _direct_children(article_nodes[0], "ArticleTitle")
    if len(title_nodes) > 1:
        return None
    title = _xml_text(title_nodes[0]) if title_nodes else None
    abstracts = _direct_children(article_nodes[0], "Abstract")
    if len(abstracts) > 1:
        return None
    abstract_parts = [
        _xml_text(node) for parent in abstracts
        for node in _direct_children(parent, "AbstractText")
    ]
    abstract = "\n".join(part for part in abstract_parts if part) or None
    ids = {}
    for node in _direct_children(id_lists[0], "ArticleId"):
        id_type = (node.attrib.get("IdType") or "").strip().lower()
        value = (node.text or "").strip()
        if id_type and value:
            if id_type in ids:
                return None
            ids[id_type] = value
    if not (title or abstract or ids):
        return None
    out = {
        "status": "resolved",
        "via": "pubmed",
        "matched_title": title,
        "abstract": abstract,
        "pmid": str(pmid),
        "article_ids": ids,
    }
    if not include_citation_fields:
        return out

    journal_nodes = _direct_children(article_nodes[0], "Journal")
    journal = journal_nodes[0] if len(journal_nodes) == 1 else None
    journal_title = None
    volume = issue = matched_year = None
    if journal is not None:
        titles = _direct_children(journal, "Title")
        abbreviations = _direct_children(journal, "ISOAbbreviation")
        journal_title = _xml_text(titles[0]) if len(titles) == 1 else None
        if journal_title is None and len(abbreviations) == 1:
            journal_title = _xml_text(abbreviations[0])
        journal_issues = _direct_children(journal, "JournalIssue")
        if len(journal_issues) == 1:
            volumes = _direct_children(journal_issues[0], "Volume")
            issues = _direct_children(journal_issues[0], "Issue")
            volume = _xml_text(volumes[0]) if len(volumes) == 1 else None
            issue = _xml_text(issues[0]) if len(issues) == 1 else None
            pub_dates = _direct_children(journal_issues[0], "PubDate")
            if len(pub_dates) == 1:
                years = _direct_children(pub_dates[0], "Year")
                matched_year = _xml_text(years[0]) if len(years) == 1 else None
                if matched_year is None:
                    medline_dates = _direct_children(pub_dates[0], "MedlineDate")
                    medline_date = _xml_text(medline_dates[0]) if len(medline_dates) == 1 else None
                    year_match = re.search(r"\b(?:18|19|20)\d{2}\b", medline_date or "")
                    matched_year = year_match.group(0) if year_match else None
    pagination = _direct_children(article_nodes[0], "Pagination")
    pages = _direct_children(pagination[0], "MedlinePgn") if len(pagination) == 1 else []
    page = _xml_text(pages[0]) if len(pages) == 1 else None
    author_lists = _direct_children(article_nodes[0], "AuthorList")
    matched_authors: list[str] = []
    if len(author_lists) == 1:
        for author in _direct_children(author_lists[0], "Author"):
            last_names = _direct_children(author, "LastName")
            collectives = _direct_children(author, "CollectiveName")
            name = (
                _xml_text(last_names[0]) if len(last_names) == 1
                else _xml_text(collectives[0]) if len(collectives) == 1
                else None
            )
            if name:
                matched_authors.append(name)
    for key, value in {
        "matched_authors": matched_authors or None,
        "matched_year": matched_year,
        "matched_venue": journal_title,
        "matched_volume": volume,
        "matched_issue": issue,
        "matched_page": page,
    }.items():
        if value is not None:
            out[key] = value
    return out


def _pubmed_citation_match(
    *, journal: str, year: str, volume: str, first_page: str, author: str,
) -> dict:
    """Resolve PubMed coordinates through NCBI ECitMatch without accepting identity."""
    resolve_mod = _resolve_module()
    bdata = "|".join((journal, year, volume, first_page, author, "callimachus", ""))
    url = resolve_mod._ncbi_url(
        _PUBMED_ECITMATCH_URL,
        {"db": "pubmed", "retmode": "ref", "bdata": bdata},
    )
    try:
        status, body = resolve_mod._get(url, accept="text/plain,*/*;q=0.8")
        audit = {"ecitmatch_url": _ncbi_audit_url(url), "ecitmatch_http_status": int(status), "ecitmatch_body": str(body)}
        if not 200 <= int(status) < 300:
            return {"status": "unresolved", "via": "pubmed_citation_match", "reason": f"HTTP {status}", **audit}
        lines = [line.strip() for line in str(body).splitlines() if line.strip()]
        if len(lines) != 1:
            return {
                "status": "unresolved", "via": "pubmed_citation_match",
                "reason": "unexpected ECitMatch response cardinality", **audit,
            }
        fields = lines[0].split("|")
        expected = (journal, year, volume, first_page, author, "callimachus")
        echoed = tuple(" ".join(item.split()).casefold() for item in fields[:6])
        wanted = tuple(" ".join(item.split()).casefold() for item in expected)
        if len(fields) != 7 or echoed != wanted:
            return {
                "status": "unresolved", "via": "pubmed_citation_match",
                "reason": (
                    "ECitMatch response does not echo the submitted citation tuple "
                    "- NOT fabrication"
                ),
                **audit,
            }
        outcome = fields[-1].strip()
        if outcome.isdigit():
            metadata = _pubmed_fetch_metadata(outcome, include_citation_fields=True)
            if metadata is None:
                return {
                    "status": "unresolved", "via": "pubmed_citation_match",
                    "reason": "ECitMatch returned a PMID whose metadata could not be fetched", **audit,
                }
            return {**metadata, "via": "pubmed_citation_match", "pmid": outcome, **audit}
        if outcome == "NOT_FOUND":
            return {
                "status": "not_found", "via": "pubmed_citation_match",
                "reason": "NCBI ECitMatch found no candidate for the supplied coordinates", **audit,
            }
        if outcome == "AMBIGUOUS":
            return {
                "status": "ambiguous", "via": "pubmed_citation_match",
                "reason": "NCBI ECitMatch returned ambiguous candidates", **audit,
            }
        return {
            "status": "unresolved", "via": "pubmed_citation_match",
            "reason": "unrecognized ECitMatch response - NOT fabrication", **audit,
        }
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved", "via": "pubmed_citation_match",
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": "pubmed_citation_match", "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {
            "status": "unresolved", "via": "pubmed_citation_match",
            "reason": f"network: {type(exc).__name__}",
        }


def _pubmed_fetch_metadata(
    pmid: str, *, include_citation_fields: bool = False,
) -> dict | None:
    resolve_mod = _resolve_module()
    url = resolve_mod._ncbi_url(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?",
        {"db": "pubmed", "retmode": "xml", "id": str(pmid)},
    )
    try:
        _status, body = resolve_mod._get(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.8")
        root = ET.fromstring(body)
    except Exception:
        return None
    article = root.find(".//PubmedArticle")
    if article is None:
        return None
    return _pubmed_metadata_from_article(
        article, pmid, include_citation_fields=include_citation_fields,
    )


def _pmc_links_from_ids(*, pmid: str | None = None) -> list[dict]:
    resolve_mod = _resolve_module()
    pmid = str(pmid or "").strip()
    if not pmid.isdigit():
        return []
    url = resolve_mod._ncbi_url(
        _PMC_IDCONV_URL,
        {"ids": pmid, "format": "json", "idtype": "pmid"},
    )
    try:
        _status, body = resolve_mod._get(url)
        records = json.loads(body).get("records") or []
    except Exception:
        return []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("pmid") or "").strip() != pmid:
            continue
        pmcid = _normalize_pmcid(rec.get("pmcid"))
        if not pmcid:
            continue
        landing = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        pdf = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
        return [
            {"url": landing, "content_type": "text/html", "site": "pmc"},
            {"url": pdf, "content_type": "application/pdf", "site": "pmc"},
        ]
    return []


def _pubmed_enrich(ref: dict) -> dict | None:
    pmid = str(ref.get("pmid") or "").strip() or None
    doi = ref.get("doi")
    looked_up_by_doi = False
    if not pmid:
        if not doi:
            return None
        pmid, search_status = _pubmed_search_pmid_with_status(doi)
        looked_up_by_doi = True
        if search_status == "not_found":
            return {
                "status": "not_found",
                "via": "pubmed",
                "reason": "no PMID found for DOI",
            }
        if search_status == "unresolved":
            return {
                "status": "unresolved",
                "via": "pubmed",
                "reason": "PMID lookup unavailable",
            }
    if not pmid:
        return None
    batch_note = None
    batch = getattr(_PUBMED_BATCH_LOCAL, "session", None)
    if batch is not None and pubmed_batch_enabled():
        article, outcome = batch.lookup(
            pmid,
            get_fn=_resolve_module()._get,
            ncbi_url=_resolve_module()._ncbi_url,
            ref_id=ref.get("id"),
        )
        if article is not None:
            meta = _pubmed_batch_metadata_from_article(article, pmid)
            if meta is not None:
                meta["reason"] = "PubMed EFetch batch matched declared PMID"
            else:
                batch_note = "batch unavailable; scalar fallback"
        else:
            meta = None
            if outcome == "omitted":
                batch_note = "batch omitted declared PMID; scalar fallback"
            elif outcome == "unavailable":
                batch_note = "batch unavailable; scalar fallback"
    else:
        meta = None
    if meta is None:
        ref_id = ref.get("id")
        if ref_id:
            from core.resolve import transport_telemetry

            predecessor = (
                batch.predecessor_operation_id(pmid) if batch_note else None
            )
            with transport_telemetry.operation(
                provider="pubmed",
                operation="efetch",
                mode="scalar",
                ref_ids=[ref_id],
                predecessor_operation_id=predecessor,
            ):
                meta = _pubmed_fetch_metadata(pmid)
        else:
            meta = _pubmed_fetch_metadata(pmid)
    if meta is None:
        reason = "PubMed metadata unavailable"
        if batch_note:
            reason = f"{reason}; {batch_note}"
        return {
            "status": "unresolved",
            "via": "pubmed",
            "reason": reason,
        }
    if looked_up_by_doi:
        normalize_doi = _resolve_module()._normalize_doi_value
        queried_doi = normalize_doi(doi)
        returned_doi = normalize_doi((meta.get("article_ids") or {}).get("doi"))
        if not queried_doi or returned_doi != queried_doi:
            return {
                "status": "unresolved",
                "via": "pubmed",
                "reason": "PubMed metadata DOI does not confirm queried DOI",
            }
    if batch_note:
        reason = str(meta.get("reason") or "").strip()
        meta["reason"] = f"{reason}; {batch_note}" if reason else batch_note
    pmc_batch = getattr(_PMC_IDCONV_BATCH_LOCAL, "session", None)
    if pmc_batch is not None:
        pmcid, _pmc_outcome = pmc_batch.lookup(
            pmid,
            get_fn=_resolve_module()._get,
            ncbi_url=_resolve_module()._ncbi_url,
            ref_id=ref.get("id"),
        )
        links = [
            {
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                "content_type": "text/html",
                "site": "pmc",
            },
            {
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
                "content_type": "application/pdf",
                "site": "pmc",
            },
        ] if pmcid else []
    else:
        links = _pmc_links_from_ids(pmid=pmid)
    if links:
        meta["fulltext_links"] = links
        meta["fulltext_exists"] = True
        meta["oa_status"] = "open"
        meta["work_type"] = "journal-article"
        meta["fulltext_exists_refined_by"] = "pubmed_pmc"
    return meta
