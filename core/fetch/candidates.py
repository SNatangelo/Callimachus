#!/usr/bin/env python3
# core/fetch/candidates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate URL/spec generation — provider hooks, priority sorting, and stage merging."""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse

try:
    import core.fetch.transport.http as _http_mod
    import core.fetch.refdata as _refdata_mod
    from core.fetch.transport import transport_telemetry as _transport_telemetry
    from core.resolve import providers as _provider_registry
    from core.resolve import http as _resolve_http
    from core.resolve import sources as _sources
except ImportError:
    import http as _http_mod
    import refdata as _refdata_mod
    import transport_telemetry as _transport_telemetry
    from core.resolve import providers as _provider_registry
    import http as _resolve_http
    from core.resolve import sources as _sources


# Status preference when merging the results of the version-of-record and preprint stages:
# a stored full text beats an abstract, which beats a definite negative finding.
_STAGE_STATUS_RANK = {
    "stored": 7, "abstract_only": 6, "abstract_fallback": 6, "quality_error": 5,
    "wrong_document": 5,
    "identity_inconclusive": 5, "identity_mismatch": 4, "metadata_only": 3, "download_error": 2,
    "not_found": 1, "skipped": 0,
}


class _ProviderCallbackRef(dict):
    """A mapping-preserving carrier for one provider registry invocation."""


class _ProviderSpecs(list):
    """Private candidate-list carrier retaining a deferred provider lookup."""


def _with_provider_callback_context(
    ref: dict,
    *,
    run_dir: str | None,
    ref_id: str | None,
    fetch_context=None,
    cited_ref: dict | None = None,
    deadline: float | None = None,
) -> dict:
    if not ((run_dir and ref_id) or cited_ref is not None):
        return ref
    # Keep the public mapping unchanged while allowing an exact provider to
    # validate against the citation that led to a resolved discovery view.
    scoped = _ProviderCallbackRef(ref)
    if run_dir and ref_id:
        scoped._provider_callback_run_dir = run_dir
        scoped._provider_callback_ref_id = ref_id
        scoped._provider_callback_fetch_context = fetch_context
        scoped._provider_callback_deadline = deadline
    if cited_ref is not None:
        scoped._provider_callback_cited_ref = cited_ref
    return scoped


def _provider_callback_context(
    ref: dict | None,
    run_dir: str | None,
    ref_id: str | None,
) -> tuple[str | None, str | None, object | None]:
    return (
        run_dir or getattr(ref, "_provider_callback_run_dir", None),
        ref_id or getattr(ref, "_provider_callback_ref_id", None),
        getattr(ref, "_provider_callback_fetch_context", None),
    )


def _provider_callback_deadline(ref: dict | None) -> float | None:
    deadline = getattr(ref, "_provider_callback_deadline", None)
    return deadline if isinstance(deadline, (int, float)) else None


def _reference_url_slug_repair(ref: dict, url: str | None) -> str | None:
    """Return one citation-supported missing-hyphen URL repair, or fail closed."""
    url_text = str(url or "").strip()
    raw = str(ref.get("raw_entry") or "")
    if not url_text or raw.count(url_text) != 1 or _refdata_mod._is_doi_url(url_text):
        return None
    try:
        parsed = urllib.parse.urlsplit(url_text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    if (host == "jstor.org" or host.endswith(".jstor.org")) and parsed.path.startswith("/stable/"):
        return None
    # Encoded/opaque paths are identifier-like and must not be rewritten.
    if urllib.parse.unquote(parsed.path) != parsed.path:
        return None

    title = str(ref.get("title") or "").lower()
    if not title:
        return None
    words = list(re.finditer(r"[a-z0-9]+", title))
    repaired_paths: set[str] = set()
    for left_match, right_match in zip(words, words[1:]):
        if not title[left_match.end():right_match.start()].isspace():
            continue
        left = left_match.group(0)
        right = right_match.group(0)
        if len(left) < 4 or len(right) < 4:
            continue
        glued = left + right
        # Only repair a word embedded in an otherwise hyphenated title slug.
        # End tokens and opaque slash-delimited segments remain untouched.
        pattern = re.compile(rf"(?<=-){re.escape(glued)}(?=-)", re.IGNORECASE)
        matches = list(pattern.finditer(parsed.path))
        if len(matches) != 1:
            continue
        match = matches[0]
        matched = match.group(0)
        replacement = matched[:len(left)] + "-" + matched[len(left):]
        repaired_paths.add(parsed.path[:match.start()] + replacement + parsed.path[match.end():])

    if len(repaired_paths) != 1:
        return None
    repaired_path = repaired_paths.pop()
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, repaired_path, parsed.query, parsed.fragment)
    )


def _pure_ws_file_url(url: str | None) -> str | None:
    """Map a public Elsevier Pure portal file URL to its binary route."""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https":
        return None
    match = re.match(r"^/portal/files/([^/]+/.+\.pdf)$", parsed.path, re.IGNORECASE)
    if not match:
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/ws/files/" + match.group(1), parsed.query, parsed.fragment)
    )


def _provider_pdf_url_from_doi(
    doi: str | None,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
    fetch_context=None,
) -> tuple[str, str] | None:
    doi_norm = _refdata_mod._normalize_doi(doi)
    if not doi_norm:
        return None
    for module in _provider_registry.enabled_modules(email=email):
        hook = getattr(module, "pdf_url_from_doi", None)
        if not callable(hook):
            continue
        get_fn = _provider_get_fn(
            run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
        )
        url = hook(
            doi_norm,
            email=email,
            get_fn=get_fn,
            environ=os.environ,
        )
        deferred = get_fn.fetch_admission_deferred()
        if deferred is not None:
            raise deferred
        if url:
            return getattr(module, "NAME", "provider"), url
    return None


def _provider_landing_to_pdf(
    url: str | None,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
    fetch_context=None,
) -> tuple[str, str] | None:
    url_text = str(url or "").strip()
    if not url_text:
        return None
    for module in _provider_registry.enabled_modules(email=email):
        hook = getattr(module, "landing_to_pdf", None)
        if not callable(hook):
            continue
        get_fn = _provider_get_fn(
            run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
        )
        pdf_url = hook(
            url_text,
            email=email,
            get_fn=get_fn,
            environ=os.environ,
        )
        deferred = get_fn.fetch_admission_deferred()
        if deferred is not None:
            raise deferred
        if pdf_url:
            return getattr(module, "NAME", "provider"), pdf_url
    return None


class _FetchDiscoveryBudgetExhausted(RuntimeError):
    """A provider lookup was deliberately not sent after its fetch budget ended."""


class _ProviderRows(list):
    """Private carrier for a provider-discovery budget observation."""

    def __init__(self, rows=(), *, fetch_budget_exhausted: bool = False):
        super().__init__(rows)
        self.fetch_budget_exhausted = bool(fetch_budget_exhausted)


def _provider_get(
    url: str,
    default_timeout: int = _http_mod.TIMEOUT_API,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
    fetch_context=None,
    **kwargs,
):
    timeout = kwargs.pop("timeout", default_timeout)
    include_effective_url = bool(kwargs.pop("include_effective_url", False))
    profile = kwargs.get("profile", "document")
    accept = kwargs.get("accept", "*/*")
    referer = kwargs.get("referer")
    headers_extra = kwargs.get("headers_extra")
    try:
        if fetch_context is not None:
            def fetch(request_url, **request_kwargs):
                response = _http_mod._get(
                    request_url, _return_effective_url=True, **request_kwargs
                )
                status, body = response[:2]
                effective_url = response[2] if len(response) >= 3 else request_url
                return {
                    "status": status,
                    "body": body,
                    "url": effective_url,
                    "challenge_markers": _http_mod._challenge_markers(body),
                }

            result = fetch_context.cached_fetch(
                fetch,
                url,
                timeout=timeout,
                profile=profile,
                strategy="provider_callback",
                accept=accept,
                referer=referer,
                headers_extra=headers_extra,
                ref_id=ref_id,
                # Provider callback URLs can carry API keys in their query.  Keep
                # their exact-request memo process-local while preserving the
                # ordinary fetch response cache for document candidates.
                persist_response=False,
            )
            if not isinstance(result, dict):
                raise RuntimeError("provider callback did not return an HTTP response")
            if include_effective_url:
                return result
            return int(result["status"]), result["body"]
        with _transport_telemetry.provider_callback_request(
            url=url,
            profile=profile,
            accept=accept,
            referer=referer,
            headers_extra=headers_extra,
            run_dir=run_dir,
            ref_id=ref_id,
        ):
            if include_effective_url:
                response = _http_mod._get(
                    url, timeout=timeout, _return_effective_url=True, **kwargs
                )
                status, body = response[:2]
                effective_url = response[2] if len(response) >= 3 else url
                return {"status": status, "body": body, "url": effective_url}
            return _http_mod._get(url, timeout=timeout, **kwargs)
    except _http_mod.FetchAdmissionDeferred as exc:
        # Providers often catch their callback's Exception and return an empty
        # list. Preserve this typed admission fact in the existing per-provider
        # failure scope so the registry can re-raise it after the callback exits.
        _resolve_http._record_provider_failure(exc)
        raise


class _ProviderGetCallback:
    """Provider callback with a thread-safe typed-defer latch."""

    supports_effective_url = True

    def __init__(self, *, run_dir, ref_id, fetch_context, deadline: float | None = None,
                 budget_state: dict | None = None) -> None:
        self._run_dir = run_dir
        self._ref_id = ref_id
        self._fetch_context = fetch_context
        self._lock = threading.Lock()
        self._deferred = None
        self._deadline = deadline
        self._budget_state = budget_state if budget_state is not None else {
            "lock": threading.Lock(), "exhausted": False,
        }

    def __call__(self, request_url, **kwargs):
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                with self._budget_state["lock"]:
                    self._budget_state["exhausted"] = True
                raise _FetchDiscoveryBudgetExhausted(
                    "per-reference fetch deadline exceeded before provider discovery request"
                )
            requested_timeout = kwargs.get("timeout", _http_mod.TIMEOUT_API)
            try:
                kwargs["timeout"] = min(float(requested_timeout), remaining)
            except (TypeError, ValueError):
                kwargs["timeout"] = remaining
        try:
            return _provider_get(
                request_url, _http_mod.TIMEOUT_API, run_dir=self._run_dir,
                ref_id=self._ref_id, fetch_context=self._fetch_context, **kwargs,
            )
        except _http_mod.FetchAdmissionDeferred as exc:
            with self._lock:
                if self._deferred is None:
                    self._deferred = exc
            raise

    def fetch_admission_deferred(self):
        with self._lock:
            return self._deferred

    def fetch_budget_exhausted(self) -> bool:
        with self._budget_state["lock"]:
            return bool(self._budget_state["exhausted"])

    def for_provider(self):
        """Return an admission latch for one provider invocation.

        The transport context remains shared (including its pacing and cached
        responses), but a cooldown seen by one provider must not make another
        provider's already-ready result look deferred.
        """
        callback = _ProviderGetCallback(
            run_dir=self._run_dir,
            ref_id=self._ref_id,
            fetch_context=self._fetch_context,
            deadline=self._deadline,
            budget_state=self._budget_state,
        )
        lookup = getattr(self, "provider_record_lookup", None)
        if callable(lookup):
            callback.provider_record_lookup = lookup
        work_lookup = getattr(self, "provider_work_lookup", None)
        if callable(work_lookup):
            callback.provider_work_lookup = work_lookup
        return callback


def _provider_get_fn(*, run_dir: str | None = None, ref_id: str | None = None,
                     fetch_context=None, deadline: float | None = None):
    """Return a worker-safe provider GET callback bound to one Fetch reference."""
    get_fn = _ProviderGetCallback(
        run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context, deadline=deadline,
    )
    lookup_factory = getattr(fetch_context, "provider_record_lookup", None)
    if callable(lookup_factory):
        lookup = lookup_factory("openalex")
        if callable(lookup):
            get_fn.provider_record_lookup = lookup
    work_lookup = getattr(fetch_context, "provider_work_lookup", None)
    if callable(work_lookup) and ref_id is not None:
        get_fn.provider_work_lookup = lambda provider, lookup_key, compute: work_lookup(
            str(provider), str(ref_id), str(lookup_key), compute,
        )
    return get_fn


def _method_priority(method: str | None) -> int:
    if method == "metadata":
        return 0
    if method == "reference_url":
        return 1
    if method == "doi_landing":
        return 3
    return 2


def _confidence_number(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_official_arxiv_html_candidate(item: dict) -> bool:
    context = item.get("identity_context") or {}
    arxiv_id = (
        context.get("identifiers", {}).get("arxiv_id")
        if isinstance(context, dict) and isinstance(context.get("identifiers"), dict)
        else None
    )
    parsed = urllib.parse.urlsplit(str(item.get("url") or ""))
    html_match = re.fullmatch(
        r"/html/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)",
        parsed.path,
        flags=re.IGNORECASE,
    )
    normalized_id = re.sub(r"v\d+$", "", str(arxiv_id or "").strip(), flags=re.IGNORECASE)
    return bool(
        item.get("official_arxiv_html")
        and item.get("method") == "arxiv"
        and item.get("kind") == "html"
        and parsed.scheme == "https"
        and _refdata_mod._host(item.get("url")) == "arxiv.org"
        and not parsed.query
        and not parsed.fragment
        and isinstance(context, dict) and context.get("canonical_host")
        and html_match is not None
        and re.sub(r"v\d+$", "", html_match.group(1), flags=re.IGNORECASE).casefold()
        == normalized_id.casefold()
    )


def _candidate_priority(item: dict) -> tuple[int, int, int, int, int]:
    from core.fetch.hosts import _challenge_prone_host, _trusted_fetch_host

    method = item.get("method")
    kind = item.get("kind")
    url = item.get("url")
    host = _refdata_mod._host(url)
    context = item.get("identity_context") or {}
    canonical = bool(isinstance(context, dict) and context.get("canonical_host"))
    official = canonical or _trusted_fetch_host(host)
    # The URL explicitly present in the citation is direct evidence and keeps
    # absolute precedence over provider-generated/canonical fallback locations.
    if method == "reference_url" and not _refdata_mod._is_doi_url(url):
        return (-1, 0, 0, 0, 0)
    if _challenge_prone_host(host):
        return (3, 1, 0 if kind == "pdf" else 1, _method_priority(method), 0)
    # A provider's canonical URL or recognised official repository should beat
    # an arbitrary cited URL, while preserving PDF-before-landing preference.
    if canonical:
        return (0, 0, 0 if kind == "pdf" else 1, _method_priority(method), 0)
    if official:
        return (1, 0, 0 if kind == "pdf" else 1, _method_priority(method), 0)
    if method == "doi_landing":
        return (4, 0, 0, 0, 0)
    return (
        2,
        1,
        0 if kind == "pdf" else 1,
        _method_priority(method),
        0,
    )


def _is_unambiguous_native_provider_candidate(item: dict) -> bool:
    """Return whether ``item`` is owned by its registered fetch provider.

    This deliberately invokes only the URL-ownership predicates.  Credential
    hooks stay at the outbound-request boundary, where candidate rows cannot
    retain their headers or tokens.
    """
    method = str(item.get("method") or "").strip().lower()
    if not method or method == "metadata":
        return False
    provider = _provider_registry.load_module(method)
    owns = getattr(provider, "owns_request_url", None) if provider is not None else None
    if not callable(owns):
        return False
    try:
        if not owns(item):
            return False
    except Exception:
        return False
    claims = []
    for candidate_provider in _provider_registry.get_registry().values():
        candidate_owns = getattr(candidate_provider, "owns_request_url", None)
        if not callable(candidate_owns):
            continue
        try:
            if candidate_owns(item):
                claims.append(candidate_provider)
        except Exception:
            return False
    return len(claims) == 1 and claims[0] is provider


def _finalize_specs(out: list[dict]) -> list[dict]:
    """Deduplicate logical URLs while retaining aliases, identity, and provenance."""
    by_url = {}
    dedup = []
    for item in out:
        url = item.get("url")
        if not url:
            continue
        key = _provider_registry.canonical_candidate_key(
            url, normalize_doi=_refdata_mod._normalize_doi,
        )
        if not key:
            continue
        # Request representation and fetch strategy alter HTTP behaviour.  Do
        # not collapse those calls merely because their logical URL is shared.
        scope = (
            item.get("profile") or item.get("fetch_profile") or "",
            item.get("strategy") or item.get("fetch_strategy") or "",
        )
        dedup_key = (key, scope)
        if dedup_key not in by_url:
            row = dict(item)
            row.setdefault("candidate_key", key)
            by_url[dedup_key] = row
            dedup.append(row)
            continue
        current = by_url[dedup_key]
        original_method = current.get("method")
        original_url = current.get("url")
        if current.get("method") == "metadata" and _is_unambiguous_native_provider_candidate(item):
            # Preserve this row's position and accumulated audit fields, but
            # use the provider-owned request representation so the later
            # outbound boundary can select its provider-local credentials.
            for field in ("method", "url", "kind", "content_version"):
                if field in item:
                    current[field] = item[field]
        aliases = list(current.get("url_aliases") or [])
        for alias in (original_url, current.get("url"), url, *(item.get("url_aliases") or [])):
            if alias and alias not in aliases:
                aliases.append(alias)
        if len(aliases) > 1:
            current["url_aliases"] = aliases
        candidate_keys = list(current.get("candidate_keys") or [])
        for candidate_key in (current.get("candidate_key"), item.get("candidate_key"), key):
            if candidate_key and candidate_key not in candidate_keys:
                candidate_keys.append(candidate_key)
        if len(candidate_keys) > 1:
            current["candidate_keys"] = candidate_keys
        contexts = []
        for context in (
            current.get("identity_context"),
            *(current.get("identity_contexts") or []),
            item.get("identity_context"),
            *(item.get("identity_contexts") or []),
        ):
            if isinstance(context, dict) and context not in contexts:
                contexts.append(dict(context))
        if contexts:
            contexts.sort(key=lambda ctx: (
                not bool(ctx.get("canonical_host")),
                -_confidence_number(ctx.get("source_confidence")),
                str(ctx.get("provider") or ""),
            ))
            current["identity_context"] = contexts[0]
            if len(contexts) > 1:
                current["identity_contexts"] = contexts
                titles = {_refdata_mod._title_key(ctx.get("title")) for ctx in contexts if ctx.get("title")}
                current["identity_context_conflict"] = len(titles) > 1
        provenance = list(current.get("provenance") or [])
        for provider in (
            current.get("discovered_via"),
            item.get("discovered_via"),
            original_method,
            current.get("method"),
            item.get("method"),
            *(item.get("provenance") or []),
        ):
            if provider and provider not in provenance:
                provenance.append(provider)
        if provenance:
            current["provenance"] = provenance
        reasons = list(current.get("discovery_reasons") or [])
        for reason in (current.get("discovery_reason"), item.get("discovery_reason")):
            if reason and reason not in reasons:
                reasons.append(reason)
        if reasons:
            current["discovery_reason"] = reasons[0]
            if len(reasons) > 1:
                current["discovery_reasons"] = reasons
    dedup = [dict(item, _order=idx) for idx, item in enumerate(dedup)]
    dedup.sort(key=lambda item: (*_candidate_priority(item), item.get("_order", 0)))
    for item in dedup:
        item.pop("_order", None)
    return dedup


def _provider_candidate_items_from_rows(rows: list[dict] | None) -> list[dict]:
    """Flatten fetchable provider items while leaving diagnostics in the trace rows."""
    out = []
    for row in rows or []:
        for item in row.get("items") or []:
            if isinstance(item, dict) and item.get("url"):
                out.append(item)
    return out


def _split_oa_fallback_specs(specs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep alternate OA locations out of the established primary queue."""
    primary = [item for item in specs if not item.get("fallback_stage")]
    alternates = [
        item for item in specs if item.get("fallback_stage") == "oa_alternate"
    ]
    return primary, alternates


def _oa_alternate_queue(
    oa_alternate_specs: list[dict],
    attempted_stage_one_hosts: set[str],
    *,
    record_only: bool = True,
) -> list[dict]:
    """Throttle stage-2 ("oa_alternate") candidates so the second network round
    stays cheap: a same-host mirror after stage 1 already failed there is close
    to zero marginal reach for full cost, and a landing page rarely earns a
    second round-trip when a PDF didn't.

      - PDF candidates only (no landing pages).
      - only on hosts stage 1 hasn't actually tried.
      - capped at MAX_OA_ALTERNATE_CANDIDATES, highest-priority first.

    ``record_only`` drops non-record (preprint/etc.) content versions, which
    is correct when this stage sits between the version-of-record stage and
    the dedicated preprint stage; pass False when the caller already treats
    every candidate as belonging to a single (e.g. forced-published) queue.
    """
    from core.fetch.hosts import MAX_OA_ALTERNATE_CANDIDATES

    specs = oa_alternate_specs
    if record_only:
        specs = [
            c for c in specs
            if c.get("content_version") not in _sources.NON_RECORD_VERSIONS
        ]
    tried_hosts = {host for host in attempted_stage_one_hosts if host}
    filtered = [
        c for c in specs
        if c.get("kind") == "pdf" and _refdata_mod._host(c.get("url")) not in tried_hosts
    ]
    return _finalize_specs(filtered)[:MAX_OA_ALTERNATE_CANDIDATES]


def _candidate_specs(
    ref: dict,
    resolve_result: dict,
    email: str | None,
    *,
    provider_rows: list[dict] | None = None,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[dict]:
    """Stage-1 candidates: the version-of-record providers (preprint resolvers excluded)
    plus metadata / reference-url / doi-landing routes. A few may still be preprint copies
    surfaced by hybrid providers (e.g. OpenAlex) — those are split into Stage 2 by their
    content_version, not consulted here."""
    out = []
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    effective_ref = _refdata_mod._effective_fetch_ref(ref, resolve_result)
    doi = _refdata_mod._normalize_doi(effective_ref.get("doi"))
    from core.resolve.providers import repository

    def add(method, url, kind=None, content_version=None, source_item=None):
        if not url:
            return
        source_item = source_item if isinstance(source_item, dict) else {}
        row = {
            "method": method,
            "url": url,
            "kind": kind or _refdata_mod._kind_from_url(url),
            "content_version": _sources.content_version_for(url, content_version),
        }
        for key in (
            "identity_context", "identity_contexts", "identity_context_conflict",
            "discovered_via", "provenance", "fallback_stage", "candidate_key",
            "discovery_reason", "profile", "fetch_profile", "strategy", "fetch_strategy",
            "official_arxiv_html",
        ):
            if key in source_item:
                value = source_item[key]
                row[key] = list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        if method == "metadata" and source_item.get("method") == "arxiv":
            candidate = dict(
                row,
                method="arxiv",
                kind=source_item.get("kind") or row["kind"],
            )
            if _is_official_arxiv_html_candidate(candidate):
                row.update(method="arxiv", kind="html")
        out.append(row)
        pure_url = _pure_ws_file_url(url)
        if pure_url and pure_url != url:
            pure_row = dict(row)
            pure_row.update(
                method="pure_ws_repository",
                url=pure_url,
                discovered_via="pure_ws_repository",
                discovery_reason="Elsevier Pure portal file alternate binary route",
                provenance=list(dict.fromkeys(
                    [
                        *(row.get("provenance") or []),
                        row.get("method"),
                        "pure_ws_repository",
                    ]
                )),
            )
            out.append(pure_row)

    for item in _refdata_mod._metadata_link_items(resolve_result):
        # Keep the established host-based version classification for metadata
        # links; explicit provenance remains available in the copied item.
        add("metadata", item["url"], _refdata_mod._kind_from_url(item["url"], item.get("content_type")), None, item)
        # Metadata can classify a bare Handle as PDF from its MIME declaration.
        # Retain that original candidate, then expand only this exact public
        # Handle form through the repository provider with the traced callback.
        if repository.is_bare_handle_url(item["url"]):
            get_fn = _provider_get_fn(
                run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
                deadline=_provider_callback_deadline(ref),
            )
            for landing_item in repository.landing_items(
                item["url"], ref=effective_ref, get_fn=get_fn,
            ):
                add(
                    landing_item["method"], landing_item["url"],
                    landing_item.get("kind"), landing_item.get("content_version"),
                    landing_item,
                )
    provider_items = (
        _provider_candidate_items_from_rows(provider_rows)
        if provider_rows is not None
        else _provider_registry.candidate_items(
            effective_ref,
            email=email,
            normalize_doi=_refdata_mod._normalize_doi,
            kind_from_url=_refdata_mod._kind_from_url,
            get_fn=_provider_get_fn(
                run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
                deadline=_provider_callback_deadline(ref),
            ),
        )
    )
    for item in provider_items:
        add(item["method"], item["url"], item.get("kind"), item.get("content_version"), item)
    ref_url = effective_ref.get("url")
    if ref_url:
        add("reference_url", ref_url, "landing" if _refdata_mod._is_doi_url(ref_url) else _refdata_mod._kind_from_url(ref_url))
        repaired_url = _reference_url_slug_repair(effective_ref, ref_url)
        if repaired_url:
            add(
                "reference_url_repair",
                repaired_url,
                _refdata_mod._kind_from_url(repaired_url),
                source_item={
                    "discovered_via": "reference_url_repair",
                    "provenance": ["reference_url", "deterministic_slug_repair"],
                    "discovery_reason": (
                        "adjacent citation words were concatenated in the cited URL slug"
                    ),
                },
            )
    if doi:
        add("doi_landing", f"https://doi.org/{urllib.parse.quote(doi, safe='')}", "landing")
    return _finalize_specs(out)


def _preprint_resolver_specs(
    ref: dict,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[dict]:
    """Stage-2 candidates: the dedicated preprint-resolver group only. Invoked lazily,
    so their network calls happen ONLY after the version of record could not be fetched."""
    out = []
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    effective_ref = _refdata_mod._effective_fetch_ref(ref, None)
    provider_items = _provider_registry.preprint_candidate_items(
        effective_ref,
        email=email,
        normalize_doi=_refdata_mod._normalize_doi,
        kind_from_url=_refdata_mod._kind_from_url,
        get_fn=_provider_get_fn(
            run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
            deadline=_provider_callback_deadline(ref),
        ),
    )
    for item in provider_items:
        url = item.get("url")
        if not url:
            continue
        row = {
            "method": item["method"],
            "url": url,
            "kind": item.get("kind") or _refdata_mod._kind_from_url(url),
            "content_version": _sources.content_version_for(url, item.get("content_version")),
        }
        for key in (
            "candidate_key", "discovery_reason", "fallback_stage", "identity_context",
            "identity_contexts", "identity_context_conflict", "discovered_via", "provenance",
            "profile", "fetch_profile", "strategy", "fetch_strategy",
            "official_arxiv_html",
        ):
            if key in item:
                value = item[key]
                row[key] = list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        out.append(row)
    specs = _ProviderSpecs(_finalize_specs(out))
    specs.fetch_admission_deferred = getattr(provider_items, "fetch_admission_deferred", None)
    return specs


def _merge_stage_results(primary: dict | None, secondary: dict | None) -> dict | None:
    """Keep the more informative of two stage results; concatenate their failures."""
    if secondary is None:
        return primary
    if primary is None:
        return secondary
    rank = lambda r: _STAGE_STATUS_RANK.get(r.get("status"), -1)
    best = dict(primary if rank(primary) >= rank(secondary) else secondary)
    best["failures"] = list(primary.get("failures") or []) + list(secondary.get("failures") or [])
    return best


def _candidate_urls(
    ref: dict,
    resolve_result: dict,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[tuple[str, str]]:
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    return [
        (c["method"], c["url"])
        for c in _candidate_specs(
            ref, resolve_result, email, run_dir=run_dir, ref_id=ref_id,
        )
    ]


def _provider_candidate_rows(
    ref: dict,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[dict]:
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    get_fn = _provider_get_fn(
        run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
        deadline=_provider_callback_deadline(ref),
    )
    rows = _provider_registry.candidate_rows(
        ref,
        email=email,
        normalize_doi=_refdata_mod._normalize_doi,
        kind_from_url=_refdata_mod._kind_from_url,
        get_fn=get_fn,
    )
    return _ProviderRows(rows, fetch_budget_exhausted=get_fn.fetch_budget_exhausted())


def _provider_direct_text_items(
    ref: dict,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[dict]:
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    get_fn = _provider_get_fn(
        run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
        deadline=_provider_callback_deadline(ref),
    )
    return _provider_registry.direct_text_items(
        ref,
        email=email,
        normalize_doi=_refdata_mod._normalize_doi,
        get_fn=get_fn,
    )


def _provider_direct_text_rows(
    ref: dict,
    email: str | None,
    *,
    run_dir: str | None = None,
    ref_id: str | None = None,
) -> list[dict]:
    run_dir, ref_id, fetch_context = _provider_callback_context(ref, run_dir, ref_id)
    get_fn = _provider_get_fn(
        run_dir=run_dir, ref_id=ref_id, fetch_context=fetch_context,
        deadline=_provider_callback_deadline(ref),
    )
    rows = _provider_registry.direct_text_rows(
        ref,
        email=email,
        normalize_doi=_refdata_mod._normalize_doi,
        get_fn=get_fn,
    )
    return _ProviderRows(rows, fetch_budget_exhausted=get_fn.fetch_budget_exhausted())
