#!/usr/bin/env python3
# core/fetch/service.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
fetch.py - deterministic OA full-text fetcher (per reference).

For a reference with a DOI and a non-paywalled OA status, ask deterministic
sources for candidate PDF URLs, download the first viable one, extract its text,
and store the parsed text in the run.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Internal imports (core.fetch.* sub-modules and resolve)
# ---------------------------------------------------------------------------

if not __package__:
    _REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
    _SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
    # ``queue.py`` is a Fetch module.  Leaving this script directory first would
    # shadow the standard-library ``queue`` needed by concurrent.futures.
    sys.path = [path for path in sys.path if path != _SCRIPT_DIRECTORY]
    if _REPOSITORY_ROOT not in sys.path:
        sys.path.insert(0, _REPOSITORY_ROOT)

from core.fetch.storage import fetch_cache as _fetch_cache
from core.fetch.extraction import fetch_html as _fetch_html
from core.fetch import queue as _fetch_pipeline
from core.fetch import hosts as _fetch_hosts
from core.fetch.fallbacks import wayback as _wayback
from core.fetch.fallbacks import perma as _perma
from core.fetch.fallbacks import internet_archive_items as _internet_archive_items
from core.resolve import provider_config as _provider_config
from core.fetch.extraction import pdf as _pdf
from core.fetch.storage import fetch_store as _fetch_store
from core.resolve import sources as _sources
from core.resolve import providers as _provider_registry
from core.infra import perf as _perf

# http.py
from core.fetch.transport.http import (
    FetchAdmissionDeferred,
    TIMEOUT_API,
    TRACE_BODY_HEAD_LIMIT,
    TRACE_HEADER_VALUE_LIMIT,
    TRACE_HEADER_EXCLUDE,
    _body_head_text,
    _body_looks_html,
    _challenge_markers,
    _download,
    _fetch_url,
    _get,
    _response_headers_snapshot,
    _retry_after_seconds,
)

# refdata.py
from core.fetch.refdata import (
    _effective_fetch_ref,
    _extract_doi_from_resolve,
    _extract_title_from_resolve,
    _extract_url_from_resolve,
    _extract_year_from_resolve,
    _host,
    _is_doi_url,
    _kind_from_url,
    _looks_pdf_url,
    _metadata_link_items,
    _metadata_urls,
    _normalize_doi,
    _origin_for_method,
    _title_key,
)

# hosts.py
from core.fetch.hosts import (
    DEFAULT_FETCH_BUDGET_S,
    DEFAULT_FETCH_CANDIDATE_WORKERS,
    DEFAULT_FETCH_HOST_CONCURRENCY,
    DEFAULT_FETCH_HOST_MIN_INTERVAL,
    DEFAULT_TIMEOUT_PDF,
    ENV_FETCH_BUDGET,
    ENV_FETCH_CANDIDATE_WORKERS,
    ENV_FETCH_HOST_CONCURRENCY,
    ENV_FETCH_HOST_MIN_INTERVAL,
    ENV_FETCH_PDF_TIMEOUT,
    ENV_OA_ALTERNATES,
    MAX_FETCH_CANDIDATES,
    MAX_OA_ALTERNATE_CANDIDATES,
    TIMEOUT_PDF,
    _candidate_worker_count,
    _challenge_prone_host,
    _citation_declares_preprint,
    _fetch_budget_seconds,
    _oa_alternates_enabled,
    _pdf_timeout,
    _preprint_marker_config,
    _shared_host_limiter,
    _trusted_fetch_host,
    _trusted_host_config,
    fetch_host_concurrency_limit,
    fetch_host_min_interval,
    fetch_provider_status_rows,
)

# candidates.py
from core.fetch.candidates import (
    _STAGE_STATUS_RANK,
    _candidate_priority,
    _candidate_specs,
    _candidate_urls,
    _finalize_specs,
    _merge_stage_results,
    _method_priority,
    _oa_alternate_queue,
    _preprint_resolver_specs,
    _provider_candidate_items_from_rows,
    _provider_candidate_rows,
    _provider_direct_text_items,
    _provider_direct_text_rows,
    _provider_get_fn,
    _provider_landing_to_pdf,
    _provider_pdf_url_from_doi,
    _with_provider_callback_context,
    _split_oa_fallback_specs,
)


# validate.py
from core.fetch.extraction.validate import _html_fulltext_ok, _text_ok

# ---------------------------------------------------------------------------
# HTML classification constants
# ---------------------------------------------------------------------------

HTML_METADATA_SHELL_MARKERS = (
    "export citation",
    "copy citation",
    "bibtex",
    "mods xml",
    "endnote",
    "refworks",
    "cc0 version of this metadata",
    "publication status:",
    "bibliographic details",
    "copy apa style",
    "copy mla style",
    "copy chicago style",
    "source identifiers:",
    "local pid:",
    "copy to clipboard",
    "download as file",
    "correct metadata",
    "create github issue",
    "fix data",
    "anthology id:",
    "cite (acl):",
)
HTML_DISCOVERY_WRAPPER_MARKERS = (
    "search results",
    "research work",
    "research output",
    "similar works",
    "related works",
    "download statistics",
    "view full record",
    "view full item",
    "institutional repository",
)
HTML_METADATA_DISCOVERY_MARKERS = (
    "the wayback machine",
    "captures",
    "about this capture",
    "collection:",
    "timestamps",
    "documents",
    "authors",
    "tables",
    "log in",
    "hosted content",
    "collections",
    "basic edition",
    "add to library",
    "readers",
    "citation style",
    "register to see more suggestions",
    "index terms",
    "comments",
    "review history",
)
HTML_ARTICLE_BODY_SECTION_MARKERS = (
    "introduction",
    "background",
    "methods",
    "materials and methods",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "experiments",
    "evaluation",
    "references",
)

# ---------------------------------------------------------------------------
# HTML classification helpers
# ---------------------------------------------------------------------------


def _normalized_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _html_has_pdf_link(base_url: str, html_text: str, meta: dict[str, list[str]]) -> bool:
    pdf_meta = _first_meta(meta, "citation_pdf_url", "pdf_url", "dc.identifier.pdf")
    if pdf_meta and _looks_pdf_url(pdf_meta):
        return True
    for link in _extract_links(base_url, html_text)[:128]:
        if _looks_pdf_url(link):
            return True
    return False


def _html_paragraph_lengths(html_text: str) -> list[int]:
    lengths = []
    for match in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html_text or ""):
        text = re.sub(r"\s+", " ", _fetch_html.strip_tags(match)).strip()
        if text:
            lengths.append(len(text))
    return lengths


def _html_article_paragraph_lengths(html_text: str) -> list[int]:
    article_html = " ".join(
        re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", html_text or "")
    )
    return _html_paragraph_lengths(article_html)


def _marker_hits(normalized_text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in normalized_text]


def _body_section_hits(normalized_text: str) -> list[str]:
    return [
        marker
        for marker in HTML_ARTICLE_BODY_SECTION_MARKERS
        if re.search(rf"\b{re.escape(marker)}\b", normalized_text)
    ]


def _metadata_shell_profile(
    base_url: str,
    html_text: str,
    page_text: str,
    abstract_text: str | None,
    meta: dict[str, list[str]],
) -> dict:
    effective_url = _fetch_html.wayback_original_url(base_url) or base_url
    parsed_effective_url = urllib.parse.urlparse(effective_url)
    host = _host(effective_url)
    path = (parsed_effective_url.path or "").lower()
    normalized_text = _normalized_space(page_text).lower()
    trusted_config = _trusted_host_config()
    repository_markers = trusted_config.get("host_markers") or ()
    repository_tlds = trusted_config.get("host_tlds") or ()
    acl_hosts = _provider_config.acl_canonical_hosts()
    jmlr_hosts = trusted_config.get("jmlr_hosts") or ()
    markers = [marker for marker in HTML_METADATA_SHELL_MARKERS if marker in normalized_text]
    wrapper_markers = [
        marker for marker in HTML_DISCOVERY_WRAPPER_MARKERS if marker in normalized_text
    ]
    discovery_markers = _marker_hits(normalized_text, HTML_METADATA_DISCOVERY_MARKERS)
    body_section_markers = _body_section_hits(normalized_text)
    cinii_shell = _fetch_html.is_cinii_metadata_shell(effective_url, page_text, html_text)
    paragraph_lengths = _html_paragraph_lengths(html_text)
    long_paragraphs_200 = sum(1 for length in paragraph_lengths if length >= 200)
    long_paragraphs_400 = sum(1 for length in paragraph_lengths if length >= 400)
    article_paragraph_lengths = _html_article_paragraph_lengths(html_text)
    semantic_article_with_prose = bool(
        len(article_paragraph_lengths) >= 2
        and sum(1 for length in article_paragraph_lengths if length >= 200) >= 2
        and any(length >= 400 for length in article_paragraph_lengths)
    )
    has_pdf_link = _html_has_pdf_link(base_url, html_text, meta)
    page_chars = len(page_text or "")
    abstract_chars = len(abstract_text or "")
    repository_host = any(marker in host for marker in repository_markers) or any(
        host.endswith(tld) for tld in repository_tlds
    )
    acl_shell = (
        any(host == acl_host or host.endswith(f".{acl_host}") for acl_host in acl_hosts)
        and has_pdf_link
        and ("anthology id:" in normalized_text or "cite (acl):" in normalized_text)
    )
    generic_shell = (
        has_pdf_link
        and len(markers) >= 3
        and page_chars < 12000
        and (not abstract_chars or page_chars <= max(abstract_chars * 12, 8000))
    )
    dominant_wrapper_abstract = (
        len(wrapper_markers) >= 2
        and page_chars >= 1500
        and abstract_chars >= 1000
        and abstract_chars / max(page_chars, 1) >= 0.75
        and (
            repository_host
            or ("research work" in wrapper_markers or "research output" in wrapper_markers)
        )
    )
    discovery_wrapper = (
        len(wrapper_markers) >= 2
        and (
            page_chars < 16000
            or dominant_wrapper_abstract
        )
        and (
            has_pdf_link
            or repository_host
            or ("research work" in wrapper_markers or "research output" in wrapper_markers)
        )
        and (
            dominant_wrapper_abstract
            or (not abstract_chars or page_chars <= max(abstract_chars * 14, 10000))
        )
    )
    metadata_record_kind = _fetch_html.metadata_record_kind(effective_url)
    arxiv_abs_landing = metadata_record_kind == "arxiv abstract page"
    pubmed_record_landing = metadata_record_kind == "pubmed metadata record"
    ssrn_record_landing = metadata_record_kind == "ssrn metadata record"
    jmlr_article_landing = (
        any(host == suffix or host.endswith(f".{suffix}") for suffix in jmlr_hosts)
        and "/papers/" in path
        and path.endswith(".html")
        and has_pdf_link
        and (
            (
                abstract_chars >= 300
                and page_chars <= max(abstract_chars * 10, 18000)
            )
            or (
                page_chars >= 1200
                and page_chars <= 8000
                and "abstract" in normalized_text
            )
        )
    )
    all_shell_markers = markers + [
        marker for marker in wrapper_markers + discovery_markers if marker not in markers
    ]
    strong_metadata_chrome = (
        len(all_shell_markers) >= 5
        or (
            "the wayback machine" in normalized_text
            and ("captures" in normalized_text or "about this capture" in normalized_text)
        )
    )
    weak_article_body = (
        page_chars < 8000
        and long_paragraphs_400 <= 2
        and len(body_section_markers) <= 2
    )
    metadata_page_without_prose = (
        strong_metadata_chrome
        and long_paragraphs_400 <= 2
        and long_paragraphs_200 <= 4
        and page_chars < 30000
    )
    abstract_dominant_landing = (
        abstract_chars >= 300
        and page_chars < 8000
        and abstract_chars / max(page_chars, 1) >= 0.20
    )
    thin_linked_article_landing = (
        has_pdf_link
        and page_chars < 4000
        and long_paragraphs_400 <= 2
        and "abstract" in normalized_text
    )
    # Keep the terminal recovery probe out of classification.  A semantic
    # article body protects generic routes and shell cues; DOI-specific abstract
    # routes remain authoritative about their abstract-only scope.
    no_structured_article_prose = not semantic_article_with_prose
    doi_abstract_route = bool(
        re.search(r"/doi/abs/[^/]+", path)
        or re.search(r"/abstract/10\.\d{4,9}(?:/|%2f)", path)
    )
    explicit_abstract_route = doi_abstract_route or (
        no_structured_article_prose
        and bool(re.search(r"/abstract/[^/]+", path))
    )
    export_formats = (
        "apa",
        "bibtex",
        "endnote",
        "harvard",
        "mods",
        "refworks",
        "ris",
        "vancouver",
    )
    export_count = sum(
        bool(re.search(rf"\b{re.escape(token)}\b", normalized_text))
        for token in export_formats
    )
    export_cues = (
        "export citation",
        "cite this",
        "@article{",
        "ty -",
        "title =",
        "further data",
    )
    export_only_record = (
        no_structured_article_prose
        and export_count >= 3
        and any(cue in normalized_text for cue in export_cues)
    )
    explicit_fulltext_unavailable = no_structured_article_prose and (
        bool(re.search(r"full text not (?:currently )?available", normalized_text))
        or bool(re.search(r"document.{0,80}not available here", normalized_text))
    )
    abstract_access_purchase_shell = no_structured_article_prose and all(
        marker in normalized_text
        for marker in ("abstract", "get access", "article purchase")
    )
    record_fields = (
        "authors",
        "document type",
        "publication date",
        "date deposited",
        "item type",
        "departments",
        "centres",
        "research units",
        "downloads",
        "included in",
        "disciplines",
    )
    record_field_count = sum(label in normalized_text for label in record_fields)
    repository_catalogue_record = (
        no_structured_article_prose
        and bool(re.search(r"\b(?:recommended )?citation\b|\bcite this\b", normalized_text))
        and record_field_count >= 2
    )
    repository_upload_placeholder = (
        no_structured_article_prose
        and "upload full text" in normalized_text
        and bool(
            re.search(
                r"upload a file.{0,120}processing by (?:the )?repository team",
                normalized_text,
            )
        )
    )
    multiple_resolution_chooser = (
        no_structured_article_prose
        and "multiple resolution" in normalized_text
        and bool(
            re.search(
                r"available from (?:the )?following locations",
                normalized_text,
            )
        )
    )
    deferred_fulltext_placeholder = (
        no_structured_article_prose
        and "full text loading" in normalized_text
        and bool(
            re.search(
                r"/(?:deliver/fulltext|delivery|download)/",
                html_text,
                re.IGNORECASE,
            )
        )
    )
    link_labels = [
        _normalized_space(_fetch_html.strip_tags(label)).lower()
        for label in re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", html_text)
    ]
    chapter_links = sum(
        "chapter" in label or "pdf" in label for label in link_labels
    )
    book_toc_landing = (
        no_structured_article_prose
        and "table of contents" in normalized_text
        and chapter_links >= 2
    )
    metadata_discovery_shell = (
        metadata_page_without_prose
        or (strong_metadata_chrome and (weak_article_body or abstract_dominant_landing))
        or thin_linked_article_landing
    )
    reason = None
    if arxiv_abs_landing:
        reason = "arXiv abstract page exposes only the abstract; follow the linked PDF for article text"
    elif pubmed_record_landing:
        reason = "PubMed record page exposes bibliographic metadata, not the article body"
    elif ssrn_record_landing:
        reason = "SSRN record page exposes metadata or abstract, not the paper body"
    elif cinii_shell:
        reason = "CiNii record page exposes bibliographic metadata, not the article body"
    elif jmlr_article_landing:
        reason = "JMLR article page exposes abstract/metadata and a linked PDF, not the article body"
    elif acl_shell:
        reason = "ACL Anthology landing page with citation-export metadata and linked PDF"
    elif metadata_discovery_shell:
        reason = "metadata/discovery page exposes metadata or abstract, not the article body"
    elif generic_shell:
        reason = "metadata landing page with citation-export controls and linked PDF"
    elif discovery_wrapper:
        reason = "repository/search wrapper page with discovery controls, not article body"
    elif explicit_abstract_route:
        reason = "explicit abstract route without sustained source body"
    elif export_only_record:
        reason = "citation-export record without sustained source body"
    elif explicit_fulltext_unavailable:
        reason = "page explicitly states full text is not available"
    elif abstract_access_purchase_shell:
        reason = "abstract page exposes access and article-purchase controls without source body"
    elif repository_catalogue_record:
        reason = "citation catalogue record without sustained source body"
    elif repository_upload_placeholder:
        reason = "repository record requests a file upload because no full text is deposited"
    elif multiple_resolution_chooser:
        reason = "multiple-resolution chooser without source body"
    elif deferred_fulltext_placeholder:
        reason = "deferred full-text loading placeholder"
    elif book_toc_landing:
        reason = "book table-of-contents landing without source body"
    shell_markers = all_shell_markers
    if arxiv_abs_landing:
        shell_markers.append("arxiv abstract page")
    if pubmed_record_landing:
        shell_markers.append("pubmed metadata record")
    if ssrn_record_landing:
        shell_markers.append("ssrn metadata record")
    if jmlr_article_landing:
        shell_markers.append("jmlr article page")
    if metadata_discovery_shell:
        shell_markers.append("metadata/discovery shell")
    if cinii_shell:
        shell_markers.append("CiNii metadata shell")
    new_shell_markers = (
        (explicit_abstract_route, "explicit abstract route"),
        (export_only_record, "export-only record"),
        (explicit_fulltext_unavailable, "fulltext unavailable"),
        (abstract_access_purchase_shell, "abstract/access/purchase shell"),
        (repository_catalogue_record, "catalogue record"),
        (repository_upload_placeholder, "repository upload placeholder"),
        (multiple_resolution_chooser, "multiple-resolution chooser"),
        (deferred_fulltext_placeholder, "deferred-fulltext placeholder"),
        (book_toc_landing, "book toc landing"),
    )
    shell_markers.extend(marker for active, marker in new_shell_markers if active)
    return {
        "is_metadata_shell": (
            arxiv_abs_landing
            or pubmed_record_landing
            or ssrn_record_landing
            or jmlr_article_landing
            or metadata_discovery_shell
            or cinii_shell
            or acl_shell
            or generic_shell
            or discovery_wrapper
            or explicit_abstract_route
            or export_only_record
            or explicit_fulltext_unavailable
            or abstract_access_purchase_shell
            or repository_catalogue_record
            or repository_upload_placeholder
            or multiple_resolution_chooser
            or deferred_fulltext_placeholder
            or book_toc_landing
        ),
        "shell_reason": reason,
        "shell_markers": shell_markers,
        "has_pdf_link": has_pdf_link,
        "page_chars": page_chars,
        "abstract_chars": abstract_chars,
        "long_paragraphs_200": long_paragraphs_200,
        "long_paragraphs_400": long_paragraphs_400,
        "body_section_markers": body_section_markers,
        "force_metadata_shell": (
            dominant_wrapper_abstract
            or cinii_shell
            or repository_upload_placeholder
        ),
    }


def classify_html_content(
    base_url: str,
    html_text: str,
    meta: dict[str, list[str]] | None = None,
) -> dict:
    meta = meta or _meta_map(html_text)
    page_text = _extract_page_text(html_text)
    abstract_text, abstract_source = _fetch_html.extract_abstract_with_source(html_text, meta)
    shell = _metadata_shell_profile(base_url, html_text, page_text, abstract_text, meta)
    # These pages can contain enough navigation/help copy to clear the generic
    # prose threshold, but they are never source full text. Keep this separate
    # from paywall/challenge detection: a login or search response is neither.
    normalized_page = re.sub(r"\s+", " ", page_text).strip().lower()
    effective_url = _fetch_html.wayback_original_url(base_url) or base_url
    parsed_url = urllib.parse.urlsplit(effective_url or "")
    path = (parsed_url.path or "").lower()
    auth_marker_count = sum(
        normalized_page.count(marker) for marker in ("sign in", "log in", "login")
    )
    login_or_search_shell = bool(
        re.search(r"/(?:login|sign[-_]?in|auth|search)(?:/|$)", path)
        or "no results found" in normalized_page
        or (auth_marker_count >= 2 and len(normalized_page) < 4000)
    )
    if login_or_search_shell:
        shell["is_metadata_shell"] = True
        shell["force_metadata_shell"] = True
        shell["shell_markers"] = list(shell["shell_markers"]) + ["login/search shell"]
        shell["shell_reason"] = "login or search page did not expose source text"
    if _html_fulltext_ok(page_text) and not shell["is_metadata_shell"]:
        content_kind = "fulltext_html"
    elif shell.get("force_metadata_shell"):
        content_kind = "metadata_shell"
    elif shell["is_metadata_shell"]:
        # Metadata/discovery shells may expose an abstract, but visible page
        # sections are often catalogue summaries. Only explicit abstract
        # metadata is safe to promote from this kind of landing.
        content_kind = "abstract_html" if abstract_source == "metadata" else "metadata_shell"
    elif abstract_text:
        content_kind = "abstract_html"
    else:
        content_kind = "landing_page"
    return {
        "content_kind": content_kind,
        "page_text": page_text,
        "abstract_text": abstract_text,
        "abstract_source": abstract_source,
        "has_pdf_link": shell["has_pdf_link"],
        "shell_reason": shell["shell_reason"],
        "shell_markers": shell["shell_markers"],
        "page_chars": shell["page_chars"],
        "abstract_chars": shell["abstract_chars"],
        "long_paragraphs_200": shell.get("long_paragraphs_200"),
        "long_paragraphs_400": shell.get("long_paragraphs_400"),
        "body_section_markers": shell.get("body_section_markers"),
        "force_metadata_shell": shell.get("force_metadata_shell"),
    }


def _abstract_ok(text: str) -> bool:
    return _fetch_html.abstract_ok(text)


def _decode_html(body: bytes) -> str:
    return _fetch_html.decode_html(body)


def _decode_textual_body(body: bytes, content_type: str | None = None) -> str | None:
    return _fetch_html.decode_textual_body(body, content_type)


def _attrs(fragment: str) -> dict:
    return _fetch_html.attrs(fragment)


def _meta_map(html_text: str) -> dict[str, list[str]]:
    return _fetch_html.meta_map(html_text)


def _first_meta(meta: dict[str, list[str]], *keys: str) -> str | None:
    return _fetch_html.first_meta(meta, *keys)


def _extract_links(base_url: str, html_text: str) -> list[str]:
    return _fetch_html.extract_links(base_url, html_text)


def _strip_tags(html_text: str) -> str:
    return _fetch_html.strip_tags(html_text)


def _extract_page_text(html_text: str) -> str:
    # Use the same structural HTML converter as the manuscript parser.  Unlike a
    # manuscript, a fetched source does not need citation markers or its reference
    # list, so this returns only readable article prose for identity/verify.
    try:
        from core.parse import html_text as _canonical_html
        result = _canonical_html.to_canonical_text(html_text, preserve_markers=False)
        if result.get("outcome") in {"ok", "abstract_only"} and result.get("text"):
            return result["text"]
    except Exception:
        pass
    return _fetch_html.extract_page_text(html_text)


def _extract_abstract_text(html_text: str, meta: dict[str, list[str]]) -> str | None:
    return _fetch_html.extract_abstract_text(html_text, meta)


def _is_paywalled_html(html_text: str) -> bool:
    return _fetch_html.is_paywalled_html(html_text)


def _is_challenge_html(html_text: str) -> bool:
    return _fetch_html.is_challenge_html(html_text)


def _is_challenge_url(url: str | None) -> bool:
    return _fetch_html.is_challenge_url(url)


def _identity_corroboration_text(text: str, meta: dict[str, list[str]]) -> str:
    return _fetch_html.identity_corroboration_text(text, meta)


def _abstract_corroborate(ref: dict, text: str, resolve_result: dict | None = None):
    """Pipeline wrapper for the standard source-identity corroborator."""
    signal, score = _sources.corroborate(ref, text, resolve_result)
    return signal in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD


def _landing_identity_profile(
    ref: dict,
    resolve_result: dict,
    meta: dict[str, list[str]],
    candidate_context: dict | None = None,
    requested_url: str | None = None,
    final_url: str | None = None,
) -> dict:
    return _fetch_html.landing_identity_profile(
        ref, resolve_result, meta, candidate_context, requested_url, final_url
    )


# ---------------------------------------------------------------------------
# Store / pipeline wrappers
# ---------------------------------------------------------------------------


def _store_if_new_abstract(
    run_dir: str, ref: dict, origin: str, text: str, source_ref: str
) -> dict | None:
    return _fetch_store.store_if_new_abstract(run_dir, ref, origin, text, source_ref)


def new_fetch_run_context(environ: dict[str, str] | None = None):
    return _fetch_cache.FetchRunContext(
        host_concurrency=fetch_host_concurrency_limit(environ),
        host_min_interval=fetch_host_min_interval(environ),
    )


def _enqueue_landing_candidates(
    queue: list[dict],
    seen: set[str],
    base_url: str,
    html_text: str,
    meta: dict[str, list[str]],
    *,
    ref: dict | None = None,
    email: str | None = None,
    run_dir: str | None = None,
    fetch_context=None,
):
    queue_size = len(queue)
    _fetch_html.enqueue_landing_candidates(
        queue,
        seen,
        base_url,
        html_text,
        meta,
        kind_from_url=_kind_from_url,
        looks_pdf_url=_looks_pdf_url,
    )
    # Standard HTML metadata and links are cheaper and more portable.  Invoke
    # platform APIs only when that normal path exposed no downloadable file.
    if len(queue) != queue_size:
        return None
    items = _provider_registry.landing_items(
        base_url,
        ref=ref,
        email=email,
        kind_from_url=_kind_from_url,
        get_fn=_provider_get_fn(
            run_dir=run_dir,
            ref_id=(ref or {}).get("id"),
            fetch_context=fetch_context,
        ),
        environ=os.environ,
    )
    for item in items:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        candidate = dict(item)
        candidate.setdefault("kind", _kind_from_url(url))
        queue.append(candidate)
    deferred = getattr(items, "fetch_admission_deferred", None)
    if deferred is None:
        return None
    return {
        "deferred_host": getattr(deferred, "host", None),
        "deferred_until": getattr(deferred, "not_before", None),
    }


def _try_store_fulltext(
    run_dir: str,
    ref: dict,
    resolve_result: dict,
    text: str,
    *,
    method: str,
    source_ref: str,
    extract_method: str | None = None,
    identity_extract_text: str | None = None,
    corroborate_text: str | None = None,
    content_version: str | None = None,
    candidate_context: dict | None = None,
    identity_extract_method: str | None = None,
    extraction_flags: list[str] | None = None,
    redirect_chain: list | None = None,
) -> dict:
    return _fetch_store.try_store_fulltext(
        run_dir,
        ref,
        resolve_result,
        text,
        method=method,
        source_ref=source_ref,
        extract_method=extract_method,
        identity_extract_text=identity_extract_text,
        corroborate_text=corroborate_text,
        content_version=content_version,
        candidate_context=candidate_context,
        identity_extract_method=identity_extract_method,
        extraction_flags=extraction_flags,
        redirect_chain=redirect_chain,
    )


def _trusted_springer_jats_identity_extract(
    item: dict,
    cited_ref: dict,
    *,
    resolved_doi: str | None = None,
) -> tuple[str | None, str | None]:
    """Accept a separate identity extract only from the exact Springer JATS path."""
    context = item.get("identity_context")
    if (
        item.get("_direct_provider") != "springer_openaccess"
        or item.get("method") != "springer_openaccess"
        or item.get("extract_method") != "api_jats"
        or item.get("identity_extract_method") != "api_jats_front"
        or not isinstance(context, dict)
        or context.get("provider") != "springer_openaccess"
    ):
        return None, None
    identity_text = item.get("identity_extract_text")
    if not isinstance(identity_text, str) or not identity_text:
        return None, None
    identifiers = context.get("identifiers")
    if not isinstance(identifiers, dict):
        return None, None
    context_doi = _normalize_doi(identifiers.get("doi"))
    expected_doi = _normalize_doi(resolved_doi) or _normalize_doi(cited_ref.get("doi"))
    declared_doi = _normalize_doi(cited_ref.get("doi"))
    if (
        not context_doi
        or not expected_doi
        or context_doi != expected_doi
        or (declared_doi and declared_doi != expected_doi)
    ):
        return None, None
    surname = _sources._first_author_surname(cited_ref)
    if surname and not re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", identity_text, re.I):
        return None, None
    try:
        parsed = urllib.parse.urlsplit(str(item.get("source_ref") or item.get("url") or ""))
        port = parsed.port
    except ValueError:
        return None, None
    expected_query = {"q": [f"doi:{context_doi}"], "p": ["1"], "s": ["1"]}
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "api.springernature.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/openaccess/jats"
        or parsed.fragment
        or urllib.parse.parse_qs(parsed.query, keep_blank_values=True) != expected_query
    ):
        return None, None
    return identity_text, "api_jats_front"


def _with_fetch_trace(result: dict, trace: dict) -> dict:
    result["fetch_trace"] = trace
    return result


def _record_wayback_skip(
    trace: dict,
    *,
    url: str,
    kind: str,
    reason: str,
    reason_code: str,
) -> None:
    """Record a Wayback snapshot fetch that the deterministic gate did not issue."""
    execution = trace["execution"]
    execution["attempts"].append({
        "queue_index": len(execution["attempts"]),
        "batch_index": len(execution["batches"]),
        "method": "wayback",
        "url": url,
        "kind": kind,
        "outcome": "skipped",
        "request": "none",
        "reason": reason,
        "reason_code": reason_code,
    })


def _record_internet_archive_items_event(
    trace: dict, *, url: str, outcome: str, request: str | None, reason: str,
    reason_code: str, status: int | None = None,
) -> None:
    """Persist a catalogue lookup decision without conflating it with Wayback."""
    execution = trace["execution"]
    event = {
        "queue_index": len(execution["attempts"]),
        "batch_index": len(execution["batches"]),
        "method": "internet_archive_item",
        "url": url,
        "kind": "pdf",
        "outcome": outcome,
        "reason": reason,
        "reason_code": reason_code,
    }
    if request is not None:
        event["request"] = request
    else:
        # A lookup response has the same closed trace shape as any other
        # attempted Fetch request, even though it never enters the download queue.
        event.update({"status": status, "final_url": url, "content_type": "application/json"})
    execution["attempts"].append(event)


def _archive_circuit_deferred(url: str) -> FetchAdmissionDeferred:
    exc = FetchAdmissionDeferred("archive.org", 0)
    exc.reason_code = "archive_circuit_open"
    exc.url = url
    return exc


def _archive_circuit_fetch_url(fetch_context, fetch_url):
    """Apply the per-Fetch-run Archive.org admission circuit to fallback I/O."""
    if fetch_context is None:
        return fetch_url

    def guarded_fetch_url(url, **kwargs):
        host = (urllib.parse.urlsplit(url).hostname or "").casefold()
        # The shared limiter normalizes only ``www.`` aliases.  In particular,
        # web.archive.org is a distinct host with its own cooldown authority.
        if host == "archive.org" and fetch_context.archive_circuit_is_open():
            raise _archive_circuit_deferred(url)
        response = fetch_url(url, **kwargs)
        if host == "archive.org" and isinstance(response, dict) and response.get("status") == 429:
            fetch_context.record_archive_http_429(host)
            until = _fetch_hosts._shared_host_limiter().cooldown_until(host)
            if (
                isinstance(until, bool)
                or not isinstance(until, (int, float))
                or not math.isfinite(until)
                or until <= 0
            ):
                raise RuntimeError("Archive.org HTTP 429 did not produce a finite host cooldown")
            exc = FetchAdmissionDeferred(host, max(0.0, until - time.monotonic()))
            exc.reason_code = "rate_limit_response"
            exc.http_status = 429
            exc.url = url
            raise exc
        return response

    return guarded_fetch_url


def _fetch_frozen_candidate(
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    candidate: dict,
    candidate_id: int,
    *,
    mailto: str | None = None,
    fetch_context=None,
    evidence_payload: dict | None = None,
    queue_persistence_hooks=None,
    stage: str | None = None,
) -> dict:
    """Resume one frozen candidate and any children it discovers."""
    resolve_result = _resolve_with_evidence_payload(resolve_result, evidence_payload)
    cited_ref = dict(ref or {})
    email = mailto or os.environ.get("CITATION_VERIFIER_MAILTO")
    candidate = dict(candidate)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)
    trace = {"execution": {"candidate_workers": 1, "batches": [], "attempts": []}}
    try:
        deps = _pipeline_deps(
            email=email,
            fetch_context=fetch_context,
            enqueue_landing_candidates=(
                None if queue_persistence_hooks is not None
                else (lambda *args, **kwargs: None)
            ),
            ref=cited_ref,
            run_dir=run_dir,
        )
        deps = replace(
            deps,
            fetch_url=_archive_circuit_fetch_url(fetch_context, deps.fetch_url),
        )
        result = _fetch_pipeline.process_queue(
            ref=cited_ref,
            run_dir=run_dir,
            resolve_result=resolve_result,
            queue=[candidate],
            seen={str(candidate.get("url") or "")},
            tmp_path=tmp_path,
            max_fetch_candidates=(
                MAX_FETCH_CANDIDATES if queue_persistence_hooks is not None else 1
            ),
            candidate_workers=1,
            deps=deps,
            trace=trace["execution"],
            deadline=None,
            replay_frozen_candidate_ids=[candidate_id],
            persistence_hooks=queue_persistence_hooks,
            stage=stage,
            allow_replay_queue_growth=queue_persistence_hooks is not None,
        )
        return _with_fetch_trace(result, trace)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _pipeline_deps(
    *, email: str | None, fetch_context=None, fetch_url=None,
    enqueue_landing_candidates=None, ref: dict | None = None,
    run_dir: str | None = None,
):
    """Build the standard pipeline boundary, optionally with injected response I/O."""
    if fetch_url is None:
        if fetch_context is None:
            fetch_context = new_fetch_run_context()
        fetch_url = lambda url, **kwargs: fetch_context.cached_fetch(
            _fetch_url,
            url,
            timeout=_pdf_timeout() if kwargs.get("profile") == "pdf" else TIMEOUT_API,
            ref_id=(ref or {}).get("id"),
            **kwargs,
        )
    if enqueue_landing_candidates is None:
        enqueue_landing_candidates = (
            lambda queue, seen, base_url, html_text, meta: _enqueue_landing_candidates(
                queue, seen, base_url, html_text, meta, ref=ref, email=email,
                run_dir=run_dir, fetch_context=fetch_context,
            )
        )
    return _fetch_pipeline.FetchPipelineDeps(
        fetch_url=fetch_url,
        pdf_extract=_pdf.extract,
        text_ok=_text_ok,
        looks_pdf_url=_looks_pdf_url,
        body_looks_html=_body_looks_html,
        decode_html=_decode_html,
        decode_textual_body=_decode_textual_body,
        meta_map=_meta_map,
        enqueue_landing_candidates=enqueue_landing_candidates,
        extract_page_text=_extract_page_text,
        is_paywalled_html=_is_paywalled_html,
        is_challenge_html=_is_challenge_html,
        is_challenge_url=_is_challenge_url,
        html_fulltext_ok=_html_fulltext_ok,
        classify_html_content=classify_html_content,
        identity_corroboration_text=_identity_corroboration_text,
        landing_identity_profile=_landing_identity_profile,
        abstract_corroborate=_abstract_corroborate,
        sustained_paragraph_profile=_fetch_html._sustained_paragraph_profile,
        explicit_cited_route_ok=_fetch_store._explicit_cited_route_ok,
        origin_for_method=_origin_for_method,
        extract_abstract_text=_extract_abstract_text,
        store_if_new_abstract=_store_if_new_abstract,
        try_store_fulltext=_try_store_fulltext,
        park_unreadable=_sources.park_unreadable,
        remember_challenge_host=(
            getattr(fetch_context, "remember_challenge_host", None)
            if fetch_context is not None else None
        ),
        pdf_extract_variants=_pdf.extract_variants,
        pdf_extract_with_quality=_pdf.extract_with_quality,
        pdf_structure_flags=_pdf.structure_flags,
        pdf_extract_native=(getattr(_pdf.extract, "__module__", "") == _pdf.__name__),
        candidate_headers=lambda candidate: _provider_registry.request_headers_for_candidate(
            candidate, environ=os.environ
        ),
    )


def process_browser_responses(
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    responses: list[dict],
    *,
    fetch_url=None,
) -> dict:
    """Classify captured browser bytes through the normal fetch pipeline.

    ``fetch_url`` is an explicit test seam.  By default it serves only the bytes
    captured by Playwright; links discovered in captured HTML are therefore not
    fetched outside that browser session.
    """
    cited_ref = dict(ref or {})
    captured = {
        item.get("requested_url") or item.get("url"): item
        for item in (responses or [])
        if item.get("requested_url") or item.get("url")
    }
    queue = []
    for requested_url, item in captured.items():
        queue.append({
            "url": requested_url,
            "method": "browser_session",
            "strategy": "browser_session",
            "kind": _kind_from_url(requested_url, item.get("content_type")),
        })

    def captured_fetch(url, **_kwargs):
        item = captured.get(url)
        if item is None:
            return None
        return {
            "status": item.get("status", 200),
            "body": item.get("body") or b"",
            "content_type": item.get("content_type") or "",
            "url": item.get("url") or url,
        }

    def captured_landing_candidates(queue, seen, base_url, html_text, meta):
        # Discover normal links exactly as the pipeline does, but never invoke a
        # provider/network fallback: only Playwright-captured bytes may satisfy
        # this injected fetch boundary.
        _fetch_html.enqueue_landing_candidates(
            queue,
            seen,
            base_url,
            html_text,
            meta,
            kind_from_url=_kind_from_url,
            looks_pdf_url=_looks_pdf_url,
        )

    trace = {"candidate_workers": 1, "batches": [], "attempts": []}
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        result = _fetch_pipeline.process_queue(
            ref=cited_ref,
            run_dir=run_dir,
            resolve_result=resolve_result,
            queue=queue,
            seen={item["url"] for item in queue},
            tmp_path=tmp_path,
            max_fetch_candidates=MAX_FETCH_CANDIDATES,
            candidate_workers=1,
            deps=_pipeline_deps(
                email=os.environ.get("CITATION_VERIFIER_MAILTO"),
                fetch_url=fetch_url or captured_fetch,
                enqueue_landing_candidates=captured_landing_candidates,
            ),
            trace=trace,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    status = result.get("status")
    if status == "stored":
        outcome = "stored"
    elif status in {"abstract_fallback", "abstract_only"}:
        outcome = "abstract_fallback"
    elif status == "metadata_only":
        outcome = "metadata_shell"
    elif status in {"identity_mismatch", "identity_inconclusive", "wrong_document"}:
        outcome = "identity_mismatch"
    else:
        outcome = status or "not_found"
    return {**result, "outcome": outcome, "fetch_trace": {"execution": trace}}


def _resolve_with_evidence_payload(resolve_result: dict | None, evidence_payload: dict | None) -> dict:
    merged = dict(resolve_result or {})
    payload = evidence_payload or {}
    for key in (
        "fulltext_links",
        "auxiliary_fulltext_links",
        "oa_status",
        "fulltext_exists",
        "abstract",
        "matched_title",
        "work_type",
        "via",
        "fulltext_availability",
    ):
        if key in payload:
            value = payload.get(key)
            if isinstance(value, list):
                merged[key] = list(value)
            elif isinstance(value, dict):
                merged[key] = dict(value)
            else:
                merged[key] = value
    return merged


def _frozen_fetch_context(
    ref: dict,
    resolve_result: dict | None,
    evidence_payload: dict | None = None,
    environ: dict[str, str] | None = None,
) -> dict:
    """Closed execution contract for one durable, deadline-skipped candidate."""
    env = os.environ if environ is None else environ
    effective_resolution = _resolve_with_evidence_payload(resolve_result, evidence_payload)
    return {
        "version": 1,
        "reference": dict(ref or {}),
        "resolution": effective_resolution,
        "execution": {
            "contract": "frozen-single-candidate-v1",
            "max_candidates": 1,
            "candidate_workers": 1,
            "landing_discovery": False,
            "provider_generation": False,
            "perma": False,
            "wayback": False,
            "pdf_timeout": _pdf_timeout(env),
            "ocr_auto": _fetch_pipeline._auto_ocr_enabled(env),
            "ocr_max_pages": _fetch_pipeline._auto_ocr_page_limit(env),
            "ocr_language": (env.get("CITATION_VERIFIER_OCR_LANG") or "eng").strip(),
        },
    }


# ---------------------------------------------------------------------------
# Main fetch orchestration
# ---------------------------------------------------------------------------


def _primary_stage_deadline(
    deadline: float | None,
    *,
    started_at: float,
) -> float | None:
    """Reserve a bounded final interval for version-of-record preprint fallback."""
    if deadline is None:
        return None
    available = deadline - started_at
    # Tiny budgets keep their historical all-or-nothing primary attempt.
    if available < 20.0:
        return deadline
    return deadline - min(30.0, available * 0.25)


def _preprint_fallback_eligible(ref: dict, email: str | None) -> bool:
    """Whether an enabled preprint adapter can handle this reference.

    A provider without an explicit ``supports`` predicate remains eligible: it
    owns its admission inside ``candidate_items``.  A failing predicate cannot
    justify shortening the primary fetch stage, so it is treated as ineligible.
    """
    modules = _provider_registry.enabled_modules(
        ref=ref, email=email, preprint_resolvers=True,
    )
    for module in modules:
        supports = getattr(module, "supports", None)
        if not callable(supports):
            return True
        try:
            if supports(ref):
                return True
        except Exception:
            continue
    return False


_FETCH_STALL_DIAGNOSTIC_GRACE_S = 60.0
_FETCH_STALL_DIAGNOSTIC_MIN_DELAY_S = 60.0


def _fetch_stall_diagnostic_delay(
    *,
    deadline: float | None,
    budget: float,
    now: float,
) -> float | None:
    """Return when to capture stacks, relative to now, or None for no budget.

    An explicit absolute deadline is authoritative. Without one, a positive
    configured budget is measured from entry to ``fetch_fulltext``. A zero or
    negative configured budget means unlimited work and does not arm a timer.
    """
    if deadline is None:
        if not math.isfinite(budget) or budget <= 0:
            return None
        remaining = budget
    else:
        if not math.isfinite(deadline):
            return None
        remaining = deadline - now
    return max(
        _FETCH_STALL_DIAGNOSTIC_MIN_DELAY_S,
        remaining + _FETCH_STALL_DIAGNOSTIC_GRACE_S,
    )


def fetch_fulltext(
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    mailto: str | None = None,
    fetch_context=None,
    evidence_payload: dict | None = None,
    queue_persistence_hooks: _fetch_pipeline.FetchQueuePersistenceHooks | None = None,
    deadline: float | None = None,
) -> dict:
    """Fetch and store the OA full text for a single reference.

    Status values:
      already_stored - fulltext already in manifest
      skipped        - no fetchable identifier/URL, or fulltext_exists=False
      not_found      - no deterministic OA URL
      download_error - HTTP download failed for all candidates
      quality_error  - a PDF downloaded but extracted text was below threshold
      ocr_needed     - a PDF was parked in the OCR queue (OCR backends are available)
      ocr_backend_unavailable - a PDF was parked in the OCR queue (no OCR backends
                               installed; retryable degradation, not an accusation)
      wrong_document - a PDF/HTML was readable enough to inspect but identified as a different work
      identity_mismatch - a candidate text was fetched but did not match the cited source
      metadata_only - only a metadata landing page was reachable (no abstract/full text)
      abstract_only  - the source itself is abstract-only (no full text exists)
      abstract_fallback - only an abstract was reachable, but a fuller text may exist
      stored         - downloaded, extracted, and stored successfully
    """
    with _perf.span("fetch", ref.get("id")):
        started_at = time.monotonic()
        configured_budget = _fetch_budget_seconds()
        effective_deadline = deadline
        if effective_deadline is None and configured_budget > 0:
            effective_deadline = started_at + configured_budget
        capture_delay = _fetch_stall_diagnostic_delay(
            deadline=effective_deadline,
            budget=configured_budget,
            now=time.monotonic(),
        )
        capture_finished = threading.Event()
        timer: threading.Timer | None = None

        if capture_delay is not None:
            ref_digest = hashlib.sha256(
                str(ref.get("id", "")).encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            diagnostic_budget = (
                max(0.0, deadline - started_at)
                if deadline is not None
                else configured_budget
            )

            def capture_stacks() -> None:
                if capture_finished.is_set():
                    return
                descriptor: int | None = None
                try:
                    diagnostics_dir = Path(run_dir) / "diagnostics"
                    diagnostics_dir.mkdir(parents=True, exist_ok=True)
                    captured_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z")
                    descriptor, _diagnostic_path = tempfile.mkstemp(
                        prefix=f"fetch-stall-{ref_digest}-",
                        suffix=".txt",
                        dir=diagnostics_dir,
                    )
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        descriptor = None
                        elapsed = max(0.0, time.monotonic() - started_at)
                        handle.write(
                            "Fetch stall diagnostic\n"
                            f"captured_at_utc={captured_at}\n"
                            f"ref_id_sha256_16={ref_digest}\n"
                            f"elapsed_s={elapsed:.3f}\n"
                            f"budget_s={diagnostic_budget:.3f}\n"
                            f"grace_s={_FETCH_STALL_DIAGNOSTIC_GRACE_S:.3f}\n"
                            "\nThread stacks:\n"
                        )
                        handle.flush()
                        faulthandler.dump_traceback(file=handle, all_threads=True)
                except Exception:
                    # A diagnostic is best-effort and must never affect Fetch.
                    return
                finally:
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass

            try:
                timer = threading.Timer(capture_delay, capture_stacks)
                timer.daemon = True
                timer.start()
            except Exception:
                # Thread creation or timer setup failure is also diagnostic-only.
                timer = None

        try:
            return _fetch_fulltext_impl(
                ref,
                run_dir,
                resolve_result,
                mailto,
                fetch_context,
                evidence_payload,
                queue_persistence_hooks=queue_persistence_hooks,
                deadline=deadline,
            )
        finally:
            capture_finished.set()
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass


def _fetch_fulltext_impl(
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    mailto: str | None = None,
    fetch_context=None,
    evidence_payload: dict | None = None,
    queue_persistence_hooks: _fetch_pipeline.FetchQueuePersistenceHooks | None = None,
    deadline: float | None = None,
) -> dict:
    resolve_result = _resolve_with_evidence_payload(resolve_result, evidence_payload)
    rid = ref["id"]
    email = mailto or os.environ.get("CITATION_VERIFIER_MAILTO")
    trace = {
        "provider_status": fetch_provider_status_rows(email),
        "direct_text": {"providers": [], "attempts": []},
        "candidate_generation": {
            "metadata_links": [],
            "provider_candidates": [],
            "reference_url": ref.get("url"),
            "doi_landing": None,
            "final_queue": [],
        },
        "execution": {
            "candidate_workers": _candidate_worker_count(),
            "batches": [],
            "attempts": [],
        },
    }

    man = _sources.load_manifest(run_dir)
    existing = [
        e
        for e in man.get("entries", [])
        if e.get("ref_id") == rid and e.get("tier") == "fulltext"
    ]
    if existing:
        existing_url = str(ref.get("url") or "").strip()
        if not existing_url:
            doi = _normalize_doi(ref.get("doi"))
            existing_url = (
                f"https://doi.org/{urllib.parse.quote(doi, safe='')}"
                if doi else "urn:callimachus:wayback:no-source-url"
            )
        _record_wayback_skip(
            trace,
            url=existing_url,
            kind=_kind_from_url(existing_url),
            reason="full text already registered; Wayback snapshot request not sent",
            reason_code="already_stored",
        )
        return _with_fetch_trace({
            "status": "already_stored",
            "stored_as": existing[0].get("stored_as"),
            "method": "cached",
            "reason": "fulltext already registered in manifest",
        }, trace)

    raw_ref_url = str(ref.get("url") or "").strip()
    auxiliary_links = [
        item for item in (resolve_result.get("auxiliary_fulltext_links") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    cited_ref = dict(ref or {})
    discovery_ref = _effective_fetch_ref(ref, resolve_result)
    doi = discovery_ref.get("doi")
    ref_url = discovery_ref.get("url")

    fulltext_exists = resolve_result.get("fulltext_exists", "unknown")
    availability = resolve_result.get("fulltext_availability") or {}
    unavailable_for_work = (
        availability.get("status") == "not_available"
        and availability.get("scope") == "work"
    )
    unscoped_work_negative = fulltext_exists is False and not availability
    primary_fetchable = any(
        isinstance(item, dict)
        and item.get("url")
        and not _is_doi_url(item.get("url"))
        for item in (resolve_result.get("fulltext_links") or [])
    )
    contradictory_candidate = bool(raw_ref_url or auxiliary_links or primary_fetchable)
    if (unavailable_for_work or unscoped_work_negative) and not contradictory_candidate:
        return _with_fetch_trace({
            "status": "skipped",
            "method": None,
            "reason": "work-scoped availability says no full text exists",
        }, trace)
    fetch_context = fetch_context or new_fetch_run_context()
    # Resolve-inline and the later fetch phase create separate in-memory
    # contexts.  Attach both to the run-scoped response cache so an inconclusive
    # identity check can be reparsed without downloading the same bytes again.
    attach_run = getattr(fetch_context, "attach_run", None)
    if callable(attach_run):
        attach_run(run_dir)
    _budget = _fetch_budget_seconds()
    _started_at = time.monotonic()
    _deadline = deadline if deadline is not None else (
        _started_at + _budget if _budget > 0 else None
    )
    cited_is_preprint = _citation_declares_preprint(cited_ref, resolve_result)
    preprint_fallback_eligible = _preprint_fallback_eligible(discovery_ref, email)
    # A cited preprint is itself the requested record.  Reserve a fallback tail
    # only when an enabled preprint adapter can handle the cited work.
    _primary_deadline = (
        _primary_stage_deadline(_deadline, started_at=_started_at)
        if not cited_is_preprint and preprint_fallback_eligible
        else _deadline
    )
    provider_ref = _with_provider_callback_context(
        discovery_ref, run_dir=run_dir, ref_id=rid, fetch_context=fetch_context,
        cited_ref=cited_ref, deadline=_primary_deadline,
    )
    direct_failures = []
    direct_rows = _provider_direct_text_rows(provider_ref, email)
    discovery_budget_exhausted = bool(
        (_deadline is not None and time.monotonic() > _deadline)
        or getattr(direct_rows, "fetch_budget_exhausted", False)
    )
    trace["direct_text"]["providers"] = [
        {
            "provider": row.get("provider"),
            "status": row.get("status"),
            "item_count": len([item for item in (row.get("items") or []) if isinstance(item, dict)]),
            "error": row.get("error"),
            "reason": row.get("reason"),
            "error_type": row.get("error_type"),
            "error_reason": row.get("error_reason"),
            "retryable": row.get("retryable"),
            "http_status": row.get("http_status"),
            "items": [
                {
                    "method": item.get("method"),
                    "source_ref": item.get("source_ref") or item.get("url"),
                    "extract_method": item.get("extract_method"),
                    "candidate_key": item.get("candidate_key"),
                    "discovery_reason": item.get("discovery_reason"),
                    "chars": len(item.get("text") or ""),
                }
                for item in (row.get("items") or [])
                if isinstance(item, dict)
            ],
        }
        for row in direct_rows
    ]
    direct_items = []
    seen_direct = set()
    for row in direct_rows:
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            source_ref = item.get("source_ref") or item.get("url") or item.get("method")
            key = (item.get("method"), source_ref, text)
            if not text or key in seen_direct:
                continue
            seen_direct.add(key)
            direct_item = dict(item)
            direct_item["_direct_provider"] = row.get("provider")
            direct_items.append(direct_item)
    for item in direct_items:
        text = item.get("text") or ""
        identity_extract_text, identity_extract_method = (
            _trusted_springer_jats_identity_extract(
                item,
                cited_ref,
                resolved_doi=discovery_ref.get("doi"),
            )
        )
        if not _text_ok(text):
            direct_failures.append(
                {
                    "method": item.get("method") or "api",
                    "reason": "direct API fulltext below quality threshold",
                    "chars": len(text),
                    "source_ref": item.get("source_ref"),
                }
            )
            trace["direct_text"]["attempts"].append(
                {
                    "method": item.get("method") or "api",
                    "source_ref": item.get("source_ref"),
                    "chars": len(text),
                    "outcome": "quality_below_threshold",
                }
            )
            continue
        stored = _try_store_fulltext(
            run_dir,
            cited_ref,
            resolve_result,
            text,
            method=item.get("method") or "api",
            source_ref=item.get("source_ref") or item.get("url") or "api",
            extract_method=item.get("extract_method") or "api",
            identity_extract_text=identity_extract_text,
            candidate_context=(
                dict(item["identity_context"])
                if isinstance(item.get("identity_context"), dict) else None
            ),
            identity_extract_method=identity_extract_method,
        )
        if stored["status"] == "stored":
            stored["failures"] = direct_failures
            attempt = {
                "method": item.get("method") or "api",
                "source_ref": item.get("source_ref") or item.get("url") or "api",
                "chars": len(text),
                "outcome": "stored",
                "extract_method": item.get("extract_method") or "api",
            }
            if identity_extract_method:
                attempt["identity_extract_method"] = identity_extract_method
            trace["direct_text"]["attempts"].append(attempt)
            return _with_fetch_trace(stored, trace)
        direct_failures.append(
            {
                "method": item.get("method") or "api",
                "reason": stored.get("reason") or "direct API fulltext identity mismatch",
                "corroborate_signal": stored.get("corroborate_signal"),
                "corroborate_score": stored.get("corroborate_score"),
                "source_ref": item.get("source_ref"),
            }
        )
        attempt = {
            "method": item.get("method") or "api",
            "source_ref": item.get("source_ref") or item.get("url") or "api",
            "chars": len(text),
            "outcome": stored.get("status") or "identity_mismatch",
            "reason": stored.get("reason"),
            "corroborate_signal": stored.get("corroborate_signal"),
            "corroborate_score": stored.get("corroborate_score"),
        }
        if identity_extract_method:
            attempt["identity_extract_method"] = identity_extract_method
        trace["direct_text"]["attempts"].append(attempt)

    metadata_items = _metadata_link_items(resolve_result)
    trace["candidate_generation"]["metadata_links"] = [
        {
            "url": item.get("url"),
            "content_type": item.get("content_type"),
            "kind": _kind_from_url(item.get("url"), item.get("content_type")),
        }
        for item in metadata_items
    ]
    provider_rows = _provider_candidate_rows(provider_ref, email)
    discovery_budget_exhausted = discovery_budget_exhausted or bool(
        getattr(provider_rows, "fetch_budget_exhausted", False)
    )
    trace["candidate_generation"]["provider_candidates"] = [
        {
            "provider": row.get("provider"),
            "status": row.get("status"),
            "error": row.get("error"),
            "reason": row.get("reason"),
            "error_type": row.get("error_type"),
            "error_reason": row.get("error_reason"),
            "retryable": row.get("retryable"),
            "http_status": row.get("http_status"),
            "execution_attempts": [
                dict(attempt)
                for attempt in (row.get("execution_attempts") or [])
                if isinstance(attempt, dict)
            ],
            "items": [
                {
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "kind": item.get("kind"),
                    "candidate_key": item.get("candidate_key"),
                    "discovery_reason": item.get("discovery_reason"),
                    "fallback_stage": item.get("fallback_stage"),
                }
                for item in (row.get("items") or [])
                if isinstance(item, dict)
            ],
        }
        for row in provider_rows
    ]
    provider_attempt_index = 0
    for row in provider_rows:
        for attempt in row.get("execution_attempts") or []:
            if not isinstance(attempt, dict):
                continue
            # These coordinates are local to the explicit candidate-generation
            # stage; they do not claim positions in the later download queue.
            trace["execution"]["attempts"].append({
                "queue_index": provider_attempt_index,
                "batch_index": 0,
                **attempt,
            })
            provider_attempt_index += 1
    for row in direct_rows + provider_rows:
        # A provider-local admission defer is a Fetch observation, rather than
        # a provider diagnostic: no provider request was issued and the phase
        # scheduler must receive the typed deferral.  Keep its scheduler-only
        # fields out of the persisted trace while recording the no-request
        # fact in the same closed execution format used for callback defers.
        if row.get("status") == "rate_limit_deferred":
            host = row.get("deferred_host")
            if isinstance(host, str) and host:
                trace["execution"]["attempts"].append({
                    "queue_index": provider_attempt_index,
                    "batch_index": 0,
                    "method": row.get("provider") or "provider_callback",
                    "url": f"https://{host}/",
                    "kind": "document",
                    "outcome": "rate_limit_deferred",
                    "request": "none",
                    "reason_code": "host_cooldown",
                    "reason": row.get("reason") or (
                        f"host cooldown deferred {host}"
                    ),
                })
                provider_attempt_index += 1
    if discovery_budget_exhausted:
        trace["execution"]["attempts"].append({
            "queue_index": provider_attempt_index,
            "batch_index": 0,
            "method": "provider_discovery",
            "url": "urn:callimachus:provider-discovery",
            "kind": "api",
            "outcome": "deadline_exceeded",
            "request": "none",
            "reason_code": "fetch_budget_exhausted",
            "reason": "per-reference fetch deadline exceeded before provider discovery request",
        })
        provider_attempt_index += 1
    earliest_deferred = None
    deferred_failures = []

    def _retain_earliest_deferred(result: dict) -> None:
        nonlocal earliest_deferred
        due = result.get("deferred_until")
        if (
            not isinstance(due, (int, float))
            or isinstance(due, bool)
            or not math.isfinite(due)
        ):
            raise ValueError("invalid fetch stage admission deferral")
        deferred_failures.extend(result.get("failures") or [])
        if earliest_deferred is None or due < earliest_deferred["deferred_until"]:
            earliest_deferred = dict(result)

    for row in direct_rows + provider_rows:
        due = row.get("deferred_until")
        if row.get("deferred_host") is None and due is None:
            continue
        _retain_earliest_deferred({
            "status": "rate_limit_deferred",
            "method": "provider_discovery",
            "reason": row.get("reason") or "provider discovery admission deferred",
            "deferred_host": row.get("deferred_host"),
            "deferred_until": due,
        })
    doi_norm = _normalize_doi(discovery_ref.get("doi"))
    if doi_norm:
        trace["candidate_generation"]["doi_landing"] = (
            f"https://doi.org/{urllib.parse.quote(doi_norm, safe='')}"
        )
    all_specs = _candidate_specs(
        provider_ref,
        resolve_result,
        email,
        provider_rows=provider_rows,
    )
    preprint_resolvers_enabled = bool(_provider_registry.enabled_modules(
        ref=provider_ref, email=email, preprint_resolvers=True,
    ))
    if not doi and not ref_url and not direct_items and not all_specs and not preprint_resolvers_enabled:
        provider_errors = [
            f"{row.get('provider')}: {row.get('error_reason') or row.get('reason')}"
            for row in direct_rows + provider_rows
            if row.get("status") == "error"
        ]
        reason = "no DOI, reference URL, or provider-derived candidate URL"
        if provider_errors:
            reason += "; provider failures: " + "; ".join(provider_errors)
        result = {
            "status": "not_found" if discovery_budget_exhausted else "skipped",
            "method": None,
            "reason": (
                "fetch budget exhausted during provider discovery"
                if discovery_budget_exhausted else reason
            ),
        }
        if earliest_deferred is not None:
            result = dict(earliest_deferred)
            result["failures"] = deferred_failures
        return _with_fetch_trace(result, trace)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)
    try:
        deps = _pipeline_deps(
            email=email, fetch_context=fetch_context, ref=discovery_ref, run_dir=run_dir,
        )

        # This adapter sees only Archive.org fallback I/O.  It preserves the
        # limiter's durable cooldown while making the per-paper circuit a
        # deterministic no-request admission decision.
        deps = replace(
            deps,
            fetch_url=_archive_circuit_fetch_url(fetch_context, deps.fetch_url),
        )

        def _terminal_candidate_attempt(attempt: dict) -> bool:
            """Whether this candidate completed a stable, reusable request.

            A candidate outcome is narrower than a reference verdict.  In
            particular, a stable 404 closes only this exact candidate; it never
            means that the cited work does not exist.
            """
            if not isinstance(attempt, dict) or attempt.get("request") == "none":
                return False
            if attempt.get("challenge_markers") or attempt.get("cached_challenge"):
                return False
            # A candidate declared as PDF can return an HTML landing page.
            # Its parsed children are residual work, so only the handler's
            # actual leaf outcome may make it skippable in formal Fetch.
            if attempt.get("html_content_kind") is not None:
                return False
            try:
                status = int(attempt.get("status"))
            except (TypeError, ValueError):
                return False
            return (200 <= status < 300) or status == 404

        def _record_completed_candidates(attempt_offset: int, stage_queue: list[dict]) -> None:
            remember_terminal = getattr(fetch_context, "record_terminal_candidate", None)
            remember_formal = getattr(
                fetch_context, "record_formal_fetch_completed_candidate", None,
            )
            for attempt in trace["execution"].get("attempts", [])[attempt_offset:]:
                if not isinstance(attempt, dict) or attempt.get("request") == "none":
                    continue
                queue_index = attempt.get("queue_index")
                if (
                    type(queue_index) is not int
                    or not 0 <= queue_index < len(stage_queue)
                ):
                    continue
                candidate = stage_queue[queue_index]
                try:
                    request_identity = _fetch_pipeline.candidate_request_identity(
                        deps, candidate,
                    )
                except Exception:
                    continue
                if queue_persistence_hooks is not None:
                    # A provider-only retry stays inside this formal Fetch
                    # phase.  Record every completed request except an
                    # admission deferral, which has its own durable lifecycle
                    # and remains eligible for its scheduled retry.
                    if (
                        callable(remember_formal)
                        and attempt.get("outcome") != "rate_limit_deferred"
                    ):
                        remember_formal(
                            rid, candidate, request_identity=request_identity,
                        )
                elif (
                    callable(remember_terminal)
                    and _terminal_candidate_attempt(attempt)
                ):
                    remember_terminal(rid, candidate, request_identity=request_identity)

        def _residual_stage_queue(stage_queue):
            seen_terminal = getattr(fetch_context, "terminal_candidate_seen", None)
            seen_formal = getattr(
                fetch_context, "formal_fetch_completed_candidate_seen", None,
            )
            if not callable(seen_terminal) and not callable(seen_formal):
                return stage_queue
            residual = []
            for candidate in stage_queue:
                try:
                    request_identity = _fetch_pipeline.candidate_request_identity(
                        deps, candidate,
                    )
                except Exception:
                    # Without a complete representation identity the candidate
                    # remains runnable; fail closed rather than over-skipping.
                    residual.append(candidate)
                    continue
                if callable(seen_terminal) and seen_terminal(
                    rid, candidate, request_identity=request_identity,
                ):
                    trace["execution"]["attempts"].append({
                        "queue_index": len(trace["execution"]["attempts"]),
                        "batch_index": len(trace["execution"]["batches"]),
                        "method": candidate.get("method"),
                        "url": candidate.get("url"),
                        "kind": candidate.get("kind"),
                        "outcome": "reused_resolve_path",
                        "request": "none",
                        "reason": "candidate completed during Resolve inline fetch",
                        "reason_code": "resolve_path_reused",
                    })
                    continue
                if callable(seen_formal) and seen_formal(
                    rid, candidate, request_identity=request_identity,
                ):
                    trace["execution"]["attempts"].append({
                        "queue_index": len(trace["execution"]["attempts"]),
                        "batch_index": len(trace["execution"]["batches"]),
                        "method": candidate.get("method"),
                        "url": candidate.get("url"),
                        "kind": candidate.get("kind"),
                        "outcome": "reused_formal_fetch_candidate",
                        "request": "none",
                        "reason": "candidate completed earlier in this automatic Fetch phase; request not sent",
                        "reason_code": "formal_fetch_candidate_reused",
                    })
                    continue
                residual.append(candidate)
            return residual

        def _run_stage(
            stage: str,
            stage_queue,
            *,
            stage_deadline: float | None = _deadline,
        ):
            stage_queue = _residual_stage_queue(stage_queue)
            if not stage_queue:
                return None
            attempt_offset = len(trace["execution"].get("attempts") or [])
            if queue_persistence_hooks is None:
                result = _fetch_pipeline.process_queue(
                    ref=cited_ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=stage_queue,
                    seen={c["url"] for c in stage_queue},
                    tmp_path=tmp_path,
                    max_fetch_candidates=MAX_FETCH_CANDIDATES,
                    candidate_workers=_candidate_worker_count(),
                    deps=deps,
                    trace=trace["execution"],
                    deadline=stage_deadline,
                )
            else:
                result = _fetch_pipeline.process_queue(
                    ref=cited_ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=stage_queue,
                    seen={c["url"] for c in stage_queue},
                    tmp_path=tmp_path,
                    max_fetch_candidates=MAX_FETCH_CANDIDATES,
                    candidate_workers=_candidate_worker_count(),
                    deps=deps,
                    trace=trace["execution"],
                    deadline=stage_deadline,
                    stage=stage,
                    persistence_hooks=queue_persistence_hooks,
                )
            _record_completed_candidates(attempt_offset, stage_queue)
            if isinstance(result, dict) and result.get("status") == "rate_limit_deferred":
                _retain_earliest_deferred(result)
                return None
            return result

        def _attempted_hosts_since(attempt_offset: int) -> set[str]:
            """Return hosts for requests actually issued by one queue stage.

            Candidate-generation operations share the execution trace with fetch
            attempts, so callers snapshot its offset before the stage. Queue
            attempts which record ``request=none`` were intentionally not sent
            (deadline, cooldown, expired signed URL, or missing provider auth)
            and must not suppress a same-host OA alternate.
            """
            attempts = trace["execution"].get("attempts") or []
            hosts = set()
            for attempt in attempts[attempt_offset:]:
                if not isinstance(attempt, dict):
                    continue
                if (
                    attempt.get("request") == "none"
                    or attempt.get("outcome") == "provider_auth_unavailable"
                    or attempt.get("reason_code") == "provider_auth_unavailable"
                ):
                    continue
                for url_key in ("url", "final_url"):
                    host = _host(attempt.get(url_key))
                    if host:
                        hosts.add(host)
            return hosts

        def _queue_trace(specs):
            return [
                {"queue_index": idx, "method": item.get("method"),
                 "url": item.get("url"), "kind": item.get("kind"),
                 "candidate_key": item.get("candidate_key"),
                 "discovery_reason": item.get("discovery_reason"),
                 "fallback_stage": item.get("fallback_stage")}
                for idx, item in enumerate(specs)
            ]

        def _run_oa_alternate_stage(oa_alternate_specs, attempted_stage_one_hosts, *, record_only, force_content_version=None):
            """Stage 2 of the OA fallback ladder: a deliberately throttled second
            network round (see ``_oa_alternate_queue`` for the PDF-only/new-host/
            capped filtering), consulted only after stage 1 (the queue just run)
            came up empty. Honors CITATION_VERIFIER_OA_ALTERNATES (disables the
            stage entirely, restoring pre-staged-fallback behaviour) and the
            per-reference deadline. Returns None — leaving the running result
            untouched — when the stage is skipped outright.
            """
            if not _oa_alt_enabled:
                trace["candidate_generation"]["oa_alternate_stage_skipped"] = "disabled"
                return None
            if _primary_deadline is not None and time.monotonic() > _primary_deadline:
                trace["candidate_generation"]["oa_alternate_stage_skipped"] = "fetch_budget_exhausted"
                return None
            oa_alternate_stage_queue = _oa_alternate_queue(
                oa_alternate_specs, attempted_stage_one_hosts, record_only=record_only
            )
            if force_content_version:
                for spec in oa_alternate_stage_queue:
                    spec["content_version"] = force_content_version
            trace["candidate_generation"]["oa_alternate_queue"] = _queue_trace(oa_alternate_stage_queue)
            return _run_stage(
                "oa_alternate", oa_alternate_stage_queue,
                stage_deadline=_primary_deadline,
            )

        _oa_alt_enabled = _oa_alternates_enabled()

        if cited_is_preprint:
            # The CITED source is itself a preprint: resolving the preprint is faithful to
            # the citation, not a degraded fallback. The preprint resolvers are a PRIMARY
            # route, and the result is NOT a non-record mismatch — relative to the citation
            # this preprint IS the record, so it is stored as a normal (green-eligible) text.
            #
            # Alternate OA locations get the same stage-2 throttling as the version-of-record
            # ladder below (PDF-only, new hosts only, capped, deadline/flag aware); everything
            # that stage stores is still forced to PUBLISHED_VERSION, same as the primary queue.
            primary_specs, oa_alternate_specs = _split_oa_fallback_specs(all_specs)
            preprint_specs = _preprint_resolver_specs(
                provider_ref, email,
            )
            preprint_deferred = getattr(preprint_specs, "fetch_admission_deferred", None)
            if preprint_deferred is not None:
                _retain_earliest_deferred({
                    "status": "rate_limit_deferred",
                    "method": "provider_preprint_discovery",
                    "reason": str(preprint_deferred),
                    "deferred_host": getattr(preprint_deferred, "host", None),
                    "deferred_until": getattr(preprint_deferred, "not_before", None),
                })
            primary = _finalize_specs(primary_specs + preprint_specs)
            for spec in primary:
                spec["content_version"] = _sources.PUBLISHED_VERSION
            trace["candidate_generation"]["final_queue"] = _queue_trace(primary)
            stage_one_attempt_offset = len(trace["execution"]["attempts"])
            result = _run_stage("published", primary, stage_deadline=_primary_deadline)
            if not (result and result.get("status") == "stored"):
                alt_result = _run_oa_alternate_stage(
                    oa_alternate_specs,
                    _attempted_hosts_since(stage_one_attempt_offset),
                    record_only=False,
                    force_content_version=_sources.PUBLISHED_VERSION,
                )
                if alt_result is not None:
                    result = _merge_stage_results(result, alt_result)
        else:
            # The deterministic ladder for a cited VERSION OF RECORD:
            #   Stage 1 — do I have the version of record? yes -> done.
            #   Stage 2 — no? consult the preprint resolvers (a separate group); their
            #             network runs ONLY now. Found one? keep it, but labelled
            #             (gaps/report treat the preprint as provisional, never green).
            #   Stage 3 — abstract fallback is whatever a stage surfaced (merged below).
            primary_specs, oa_alternate_specs = _split_oa_fallback_specs(all_specs)
            published_queue = [c for c in primary_specs
                               if c.get("content_version") not in _sources.NON_RECORD_VERSIONS]
            preprint_from_hybrid = [c for c in primary_specs
                                    if c.get("content_version") in _sources.NON_RECORD_VERSIONS]
            trace["candidate_generation"]["final_queue"] = _queue_trace(published_queue)
            stage_one_attempt_offset = len(trace["execution"]["attempts"])
            result = _run_stage(
                "published", published_queue, stage_deadline=_primary_deadline,
            )
            if not (result and result.get("status") == "stored"):
                alt_result = _run_oa_alternate_stage(
                    oa_alternate_specs,
                    _attempted_hosts_since(stage_one_attempt_offset),
                    record_only=True,
                )
                if alt_result is not None:
                    result = _merge_stage_results(result, alt_result)
            if not (result and result.get("status") == "stored"):
                if _deadline is not None and time.monotonic() > _deadline:
                    # Budget exhausted after stage 1 — skip the preprint stage.
                    # The reference stays retryable (never an accusation).
                    trace["candidate_generation"]["preprint_stage_skipped"] = "fetch_budget_exhausted"
                    if result is None:
                        result = {
                            "status": "not_found",
                            "method": "auto",
                            "reason": "fetch_budget_exhausted before preprint stage",
                        }
                else:
                    # When alternates are disabled, drop everything tagged
                    # "oa_alternate" entirely — no alternate stage above, and no
                    # confluence of the non-record ones into the preprint queue
                    # either. For Unpaywall this matches pre-staged-fallback
                    # behaviour (only the best location was consulted); for
                    # OpenAlex it is stricter, since generic `locations` entries
                    # were primary candidates before a8379e0 but carry the
                    # alternate tag under the current taxonomy and so disappear
                    # with the flag off.
                    non_record_alternates = (
                        [c for c in oa_alternate_specs
                         if c.get("content_version") in _sources.NON_RECORD_VERSIONS]
                        if _oa_alt_enabled else []
                    )
                    preprint_provider_ref = _with_provider_callback_context(
                        discovery_ref, run_dir=run_dir, ref_id=rid,
                        fetch_context=fetch_context, cited_ref=cited_ref,
                        deadline=_deadline,
                    )
                    preprint_specs = _preprint_resolver_specs(
                        preprint_provider_ref, email,
                    )
                    preprint_deferred = getattr(preprint_specs, "fetch_admission_deferred", None)
                    if preprint_deferred is not None:
                        _retain_earliest_deferred({
                            "status": "rate_limit_deferred",
                            "method": "provider_preprint_discovery",
                            "reason": str(preprint_deferred),
                            "deferred_host": getattr(preprint_deferred, "host", None),
                            "deferred_until": getattr(preprint_deferred, "not_before", None),
                        })
                    preprint_queue = _finalize_specs(
                        preprint_from_hybrid
                        + non_record_alternates
                        + preprint_specs
                    )
                    trace["candidate_generation"]["preprint_queue"] = _queue_trace(preprint_queue)
                    result = _merge_stage_results(
                        result,
                        _run_stage("preprint", preprint_queue, stage_deadline=_deadline),
                    )

        # Perma.cc backup link, before Wayback: a law-review footnote carries its
        # own author-designated archive code in brackets, and the viewer states the
        # original URL it captured. That original is worth a fetch of its own —
        # often it is not the URL our parse holds, because PDF extraction broke the
        # citation's URL across a line. Runs first because it needs no availability
        # API and because its recovered URL is the better input to Wayback below.
        #
        # The trigger is only "nothing was stored", not Wayback's access-wall test.
        # Wayback needs that test because it costs a rate-limited archive.org lookup
        # for every candidate URL whether or not an archive is likely; perma acts
        # only when the footnote literally names an archive code, so a bare dead
        # link (a 404 that records no wall) — the very link rot the backup exists
        # to answer — must not be gated out.
        perma_recovered_urls: list[str] = []
        if (_perma.enabled()
                and not (result and result.get("status") == "stored")
                and (_deadline is None or time.monotonic() <= _deadline)):
            perma_specs = _perma.build_candidates(
                cited_ref.get("raw_entry") or "", fetch_url=deps.fetch_url,
                seen={spec.get("url") for spec in all_specs if spec.get("url")})
            if perma_specs:
                perma_recovered_urls = [spec["url"] for spec in perma_specs]
                for spec in perma_specs:
                    spec["content_version"] = _sources.PUBLISHED_VERSION
                perma_queue = _finalize_specs(perma_specs)
                trace["candidate_generation"]["perma_queue"] = _queue_trace(perma_queue)
                result = _merge_stage_results(result, _run_stage("perma", perma_queue))

        # Archive.org catalogue items are a separate, late full-text source.  A
        # uniquely matched public item can be downloaded directly; this must run
        # before Wayback, whose snapshots are only a last resort for access walls.
        if not (result and result.get("status") == "stored"):
            if str(resolve_result.get("status") or "").lower() != "resolved":
                _record_internet_archive_items_event(
                    trace, url="https://archive.org/advancedsearch.php", outcome="skipped",
                    request="none", reason="Internet Archive item lookup requires a resolved work identity",
                    reason_code="unresolved_identity",
                )
            elif _deadline is not None and time.monotonic() > _deadline:
                _record_internet_archive_items_event(
                    trace, url="https://archive.org/advancedsearch.php", outcome="skipped",
                    request="none", reason="per-reference fetch deadline exceeded before Internet Archive item lookup",
                    reason_code="budget_expired",
                )
            else:
                item_lookup_key = _internet_archive_items.lookup_key(discovery_ref)
                claim_lookup = getattr(fetch_context, "claim_internet_archive_item_lookup", None)
                if item_lookup_key and callable(claim_lookup) and not claim_lookup(rid, item_lookup_key):
                    _record_internet_archive_items_event(
                        trace, url="https://archive.org/advancedsearch.php", outcome="skipped",
                        request="none", reason="equivalent Internet Archive item lookup already decided in this Fetch run",
                        reason_code="internet_archive_item_memoized",
                    )
                    internet_archive_specs = []
                elif fetch_context.archive_circuit_is_open():
                    _record_internet_archive_items_event(
                        trace, url="https://archive.org/advancedsearch.php", outcome="skipped",
                        request="none", reason="Archive.org circuit is open for this Fetch run",
                        reason_code="archive_circuit_open",
                    )
                    internet_archive_specs = []
                else:
                    try:
                        internet_archive_specs = _internet_archive_items.build_candidates(
                            discovery_ref, fetch_url=deps.fetch_url,
                        )
                    except FetchAdmissionDeferred as exc:
                        defer_reason_code = getattr(exc, "reason_code", "host_cooldown")
                        if defer_reason_code == "archive_circuit_open":
                            _record_internet_archive_items_event(
                                trace, url=getattr(exc, "url", None) or f"https://{exc.host}/",
                                outcome="skipped", request="none", reason="Archive.org circuit is open for this Fetch run",
                                reason_code=defer_reason_code,
                            )
                            internet_archive_specs = []
                            exc = None
                        if exc is not None:
                            response_received = defer_reason_code == "rate_limit_response"
                            defer_reason = (
                                "HTTP 429; Internet Archive item lookup requeued at limiter cooldown expiry"
                                if response_received else str(exc)
                            )
                            _record_internet_archive_items_event(
                                trace,
                                url=getattr(exc, "url", None) or f"https://{exc.host}/",
                                outcome="rate_limit_deferred",
                                request=None if response_received else "none",
                                reason=defer_reason, reason_code=defer_reason_code,
                                status=getattr(exc, "http_status", None) if response_received else None,
                            )
                            _retain_earliest_deferred({
                                "status": "rate_limit_deferred", "method": "internet_archive_item",
                                "reason": defer_reason, "deferred_host": exc.host,
                                "deferred_until": exc.not_before,
                            })
                            internet_archive_specs = []
                    except _internet_archive_items.LookupUnresolved as exc:
                        trace["candidate_generation"]["internet_archive_items_unresolved"] = {
                            "url": exc.url, "reason": exc.reason,
                        }
                        _record_internet_archive_items_event(
                            trace, url=exc.url, outcome="lookup_unresolved", request=None,
                            reason=exc.reason, reason_code="lookup_unresolved", status=exc.status,
                        )
                        internet_archive_specs = []
                if internet_archive_specs:
                    for spec in internet_archive_specs:
                        spec["content_version"] = _sources.PUBLISHED_VERSION
                    internet_archive_queue = _finalize_specs(internet_archive_specs)
                    trace["candidate_generation"]["internet_archive_item_queue"] = _queue_trace(internet_archive_queue)
                    result = _merge_stage_results(
                        result, _run_stage("internet_archive_item", internet_archive_queue),
                    )
                else:
                    reason_code = getattr(internet_archive_specs, "reason_code", None)
                    if reason_code:
                        lookup_url = getattr(internet_archive_specs, "lookup_url", None)
                        if lookup_url:
                            _record_internet_archive_items_event(
                                trace, url=lookup_url, outcome="no_candidate", request=None,
                                reason=getattr(internet_archive_specs, "reason", None) or reason_code,
                                reason_code=reason_code,
                                status=getattr(internet_archive_specs, "http_status", None),
                            )
                        else:
                            _record_internet_archive_items_event(
                                trace, url="https://archive.org/advancedsearch.php", outcome="skipped",
                                request="none",
                                reason=getattr(internet_archive_specs, "reason", None) or reason_code,
                                reason_code=reason_code,
                            )

        # Wayback last resort: when no full text was stored and the live fetch was
        # blocked or empty (challenge, paywall, dead link) — not merely "no OA copy
        # exists" — try archived snapshots of the candidate URLs. An OA page open at
        # capture yields full text; a closed landing yields its public abstract; a
        # dead web citation yields its page. Rate-limited and capped, because
        # archive.org throttles hard.
        wayback_trace_url = (
            ref_url
            or next((spec.get("url") for spec in all_specs if spec.get("url")), None)
            or trace["candidate_generation"].get("doi_landing")
            or "urn:callimachus:wayback:no-source-url"
        )
        if not _wayback.enabled():
            _record_wayback_skip(
                trace, url=wayback_trace_url, kind=_kind_from_url(wayback_trace_url),
                reason="Wayback fallback disabled; snapshot request not sent",
                reason_code="wayback_disabled",
            )
        elif result and result.get("status") == "stored":
            _record_wayback_skip(
                trace, url=wayback_trace_url, kind=_kind_from_url(wayback_trace_url),
                reason="full text already stored; Wayback snapshot request not sent",
                reason_code="already_stored",
            )
        elif not _wayback.result_hit_access_wall(result):
            _record_wayback_skip(
                trace, url=wayback_trace_url, kind=_kind_from_url(wayback_trace_url),
                reason="no access-wall result; Wayback snapshot request not sent",
                reason_code="no_access_wall",
            )
        elif _deadline is not None and time.monotonic() > _deadline:
            _record_wayback_skip(
                trace, url=wayback_trace_url, kind=_kind_from_url(wayback_trace_url),
                reason="per-reference fetch deadline exceeded before Wayback snapshot request",
                reason_code="budget_expired",
            )
        else:
            soft_dead_link_url = _wayback.soft_dead_link_original_url(result)
            if soft_dead_link_url:
                # An unrelated HTTP-200 landing only proves this original URL is
                # archive-eligible; do not fan the marker out to other candidates.
                wayback_source_urls = [soft_dead_link_url]
            else:
                # The perma-recovered original leads: it is the canonical spelling of
                # the citation's URL, so it is the one most likely to have a snapshot.
                wayback_source_urls = perma_recovered_urls + ([ref_url] if ref_url else []) + [
                    spec.get("url") for spec in all_specs if spec.get("url")
                ]
            wayback_deferred = False
            try:
                wayback_specs = _wayback.build_candidates(
                    wayback_source_urls, fetch_url=deps.fetch_url, seen=set())
            except FetchAdmissionDeferred as exc:
                reason_code = getattr(exc, "reason_code", "host_cooldown")
                _record_wayback_skip(
                    trace, url=getattr(exc, "url", None) or wayback_trace_url,
                    kind=_kind_from_url(wayback_trace_url),
                    reason=("Archive.org circuit is open for this Fetch run" if reason_code == "archive_circuit_open" else str(exc)),
                    reason_code=reason_code,
                )
                if reason_code != "archive_circuit_open":
                    _retain_earliest_deferred({
                        "status": "rate_limit_deferred", "method": "wayback", "reason": str(exc),
                        "deferred_host": exc.host, "deferred_until": exc.not_before,
                    })
                wayback_specs = []
                wayback_deferred = True
            if not wayback_specs and not wayback_deferred:
                _record_wayback_skip(
                    trace, url=wayback_trace_url, kind=_kind_from_url(wayback_trace_url),
                    reason="Wayback availability lookup did not yield a snapshot candidate; snapshot request not sent",
                    reason_code="wayback_lookup_unresolved",
                )
            elif wayback_specs:
                for spec in wayback_specs:
                    spec["content_version"] = _sources.PUBLISHED_VERSION
                wayback_queue = _finalize_specs(wayback_specs)
                trace["candidate_generation"]["wayback_queue"] = _queue_trace(wayback_queue)
                result = _merge_stage_results(result, _run_stage("wayback", wayback_queue))

        if result is None:
            provider_errors = [
                f"{row.get('provider')}: {row.get('error_reason') or row.get('reason')}"
                for row in direct_rows + provider_rows
                if row.get("status") == "error"
            ]
            reason = "no deterministic candidate URL found"
            if provider_errors:
                reason += "; provider failures: " + "; ".join(provider_errors)
            result = {
                "status": "not_found",
                "method": "auto",
                "reason": reason,
            }
        if earliest_deferred is not None and result.get("status") != "stored":
            retained = dict(earliest_deferred)
            retained["failures"] = deferred_failures + list(result.get("failures") or [])
            retained["fetch_deadline"] = _deadline
            result = retained
        if direct_failures:
            result["failures"] = direct_failures + list(result.get("failures") or [])
        return _with_fetch_trace(result, trace)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    from core.app.runtime.fetch_audit import _record_fetch_attempts
    from core.infra.db import RunRepository

    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run directory (runs/<ts>)")
    ap.add_argument("--ref-id", required=True, help="reference id from the run database")
    ap.add_argument(
        "--mailto",
        help="contact email for Unpaywall (or CITATION_VERIFIER_MAILTO env)",
    )
    args = ap.parse_args()

    repo = RunRepository.open_readonly(args.run)
    try:
        matches = [
            ref for ref in repo.effective_parse_payload().get("references", [])
            if ref.get("id") == args.ref_id
        ]
        resolve_result = repo.resolve_payload_map().get(args.ref_id)
    finally:
        repo.close()
    if len(matches) != 1:
        raise SystemExit(
            f"reference {args.ref_id!r} is not present exactly once in the run"
        )
    if not isinstance(resolve_result, dict):
        raise SystemExit(f"reference {args.ref_id!r} has no persisted resolve result")
    ref = matches[0]
    result = fetch_fulltext(
        ref,
        args.run,
        resolve_result,
        mailto=args.mailto or os.environ.get("CITATION_VERIFIER_MAILTO"),
    )
    _record_fetch_attempts(args.run, args.ref_id, result, resolve_result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
