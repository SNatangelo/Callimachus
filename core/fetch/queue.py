#!/usr/bin/env python3
# core/fetch/queue.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Queue-processing pipeline for deterministic source fetches."""

from __future__ import annotations

import os as _os
import math
import re
import time
import urllib.parse

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

try:
    from core.resolve import sources as _sources
    from core.infra import perf as _perf
    from core.fetch import hosts as _hosts
    from core.fetch.transport import host_limiter as _host_limiter
except ImportError:
    import sources as _sources
    import perf as _perf
    import hosts as _hosts
    import host_limiter as _host_limiter


def _auto_ocr_enabled(environ: dict | None = None) -> bool:
    """Check if automatic OCR of scanned PDFs is enabled (env-var gated).

    CITATION_VERIFIER_OCR_AUTO=1 (default) means low-text PDFs are auto-OCR'd
    without creating a manual task. Set to 0 to restore the opt-in-only behaviour.
    """
    env = environ or _os.environ
    val = (env.get("CITATION_VERIFIER_OCR_AUTO") or "1").strip()
    return val.lower() in ("1", "true", "yes", "on")


AUTO_OCR_MAX_PAGES = 50
ENV_AUTO_OCR_MAX_PAGES = "CITATION_VERIFIER_OCR_AUTO_MAX_PAGES"


def _auto_ocr_page_limit(environ: dict | None = None) -> int:
    env = environ or _os.environ
    try:
        return max(1, int((env.get(ENV_AUTO_OCR_MAX_PAGES) or AUTO_OCR_MAX_PAGES)))
    except (TypeError, ValueError):
        return AUTO_OCR_MAX_PAGES


def _automatic_ocr_limit_reason(ocr_module, pdf_path: str) -> str | None:
    """Protect automatic fallback from unexpectedly OCR'ing whole books.

    This limit applies only to unattended fallback. Explicit/manual OCR remains
    unrestricted, and the environment variable can raise the ceiling for a run.
    """
    pages = ocr_module.pdf_page_count(pdf_path)
    limit = _auto_ocr_page_limit()
    if pages is not None and pages > limit:
        return (
            f"auto-OCR skipped: PDF has {pages} pages, exceeding the automatic "
            f"limit of {limit}; use manual OCR or raise {ENV_AUTO_OCR_MAX_PAGES}"
        )
    return None


def _ocr_backends_available() -> bool:
    """Check whether any OCR backends are installed on this system.

    Independent of ``CITATION_VERIFIER_OCR_AUTO`` — that env-var controls
    *automatic* OCR, not whether OCR is possible at all.  When backends are
    present but auto is disabled, the PDF is ``ocr_needed`` (tools exist);
    when no backend is installed at all it is ``ocr_backend_unavailable``
    (retryable degradation, not an accusation).
    """
    try:
        from core.fetch.extraction import ocr as _ocr
    except ImportError:
        try:
            from extraction import ocr as _ocr
        except ImportError:
            return False
    return bool(_ocr.available_backends())


def _maybe_auto_ocr(
    *,
    deps: FetchPipelineDeps,
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    pdf_path: str,
    method: str,
    final_url: str,
    failures: list[dict],
    candidate_context: dict | None = None,
) -> dict | None:
    """Try automatic OCR on a scanned PDF and store the result as fulltext.

    Returns the stored dict on success, or None if OCR is unavailable, disabled,
    failed, or the result didn't pass the identity check.
    """
    if not _auto_ocr_enabled():
        return None

    try:
        from core.fetch.extraction import ocr as _ocr
    except ImportError:
        try:
            from extraction import ocr as _ocr
        except ImportError:
            return None

    limit_reason = _automatic_ocr_limit_reason(_ocr, pdf_path)
    if limit_reason:
        failures.append(_failure(method, final_url, limit_reason, page_count=_ocr.pdf_page_count(pdf_path)))
        return None

    if not _ocr.available_backends():
        failures.append(
            _failure(
                method,
                final_url,
                "auto-OCR skipped: no OCR backends available",
            )
        )
        return None

    ocr_lang = (_os.environ.get("CITATION_VERIFIER_OCR_LANG") or "eng").strip()
    try:
        with _perf.span("fetch_ocr", ref.get("id")):
            ocr_text, ocr_backend = _ocr.ocr_pdf(pdf_path, lang=ocr_lang)
    except Exception as exc:
        failures.append(
            _failure(
                method,
                final_url,
                f"auto-OCR failed: {exc}",
            )
        )
        return None

    if not deps.text_ok(ocr_text):
        failures.append(
            _failure(
                method,
                final_url,
                f"auto-OCR output below quality threshold (len={len(ocr_text)})",
                chars=len(ocr_text),
            )
        )
        return None

    content_version = f"ocr:{ocr_backend}:{int(time.time())}"
    stored = deps.try_store_fulltext(
        run_dir,
        ref,
        resolve_result,
        ocr_text,
        method=f"{method}+ocr",
        source_ref=final_url,
        extract_method=f"pdf+ocr/{ocr_backend}",
        content_version=content_version,
        candidate_context=candidate_context,
        identity_extract_method=f"pdf+ocr/{ocr_backend}",
    )
    if stored.get("status") == "stored":
        return stored
    # OCR produced usable text but the store rejected it (typically an identity
    # mismatch). Record it so the audit shows OCR ran and was rejected on store,
    # rather than the PDF silently ending up parked as "likely a scan without OCR".
    reason = stored.get("reason") or stored.get("reason_code") or stored.get("status") or "unknown"
    failures.append(
        _failure(
            method,
            final_url,
            f"auto-OCR text rejected on store: {reason}",
        )
    )
    return None


def _maybe_retry_identity_with_ocr(
    *,
    deps: "FetchPipelineDeps",
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    pdf_path: str,
    method: str,
    final_url: str,
    failures: list[dict],
    original_text: str,
    extract_method: str | None,
    content_version: str | None,
    candidate_context: dict | None = None,
    redirect_chain: list | None = None,
) -> dict | None:
    """Use a bounded OCR front-matter probe to retry a PDF identity mismatch.

    Some historical PDFs extract as high-entropy garbage with plain text tools while
    still being the correct cited paper.  OCR only the first three pages, extending
    through page six when inconclusive.  If that confirms identity, store the complete
    original extraction; probe text is never materialized as the source full text.
    """
    if extract_method and str(extract_method).startswith("pdf+ocr/"):
        return None
    try:
        from core.fetch.extraction import ocr as _ocr
    except ImportError:
        try:
            from extraction import ocr as _ocr
        except ImportError:
            return None

    if not _ocr.available_backends():
        failures.append(
            _failure(
                method,
                final_url,
                "auto-OCR skipped after identity mismatch: no OCR backends available",
            )
        )
        return None

    ocr_lang = (_os.environ.get("CITATION_VERIFIER_OCR_LANG") or "eng").strip()
    try:
        with _perf.span("fetch_ocr", ref.get("id")):
            ocr_text, ocr_backend = _ocr.ocr_pdf(
                pdf_path, lang=ocr_lang, pages=range(1, 4)
            )
        probe_range = "1_3"
        sig, score = _sources.corroborate(ref, ocr_text, resolve_result)
        identity_probe = _sources.document_identity_probe(
            ref, ocr_text, resolve_result, candidate_context
        )
        confirmed = (
            (sig in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD)
            and identity_probe.get("decision") == "confirmed"
        )
        if not confirmed:
            with _perf.span("fetch_ocr", ref.get("id")):
                extra_text, extra_backend = _ocr.ocr_pdf(
                    pdf_path, lang=ocr_lang, pages=range(4, 7)
                )
            ocr_text = f"{ocr_text}\n\n{extra_text}"
            ocr_backend = extra_backend or ocr_backend
            probe_range = "1_6"
            sig, score = _sources.corroborate(ref, ocr_text, resolve_result)
            identity_probe = _sources.document_identity_probe(
                ref, ocr_text, resolve_result, candidate_context
            )
            confirmed = (
                (sig in ("doi", "pmid") or score >= _sources.CORROBORATE_THRESHOLD)
                and identity_probe.get("decision") == "confirmed"
            )
    except Exception as exc:
        failures.append(
            _failure(
                method,
                final_url,
                f"partial identity OCR failed: {exc}",
            )
        )
        return None

    if not confirmed:
        failures.append(
            _failure(
                method,
                final_url,
                "partial identity OCR did not corroborate the cited work",
                chars=len(ocr_text),
                corroborate_signal=sig,
                corroborate_score=score,
                identity_decision=identity_probe.get("decision"),
            )
        )
        return None

    stored = deps.try_store_fulltext(
        run_dir,
        ref,
        resolve_result,
        original_text,
        method=method,
        source_ref=final_url,
        extract_method=extract_method,
        identity_extract_text=ocr_text,
        content_version=content_version,
        candidate_context=candidate_context,
        identity_extract_method=f"ocr_identity_pages_{probe_range}/{ocr_backend}",
        redirect_chain=redirect_chain,
    )
    if stored.get("status") == "stored":
        return stored

    failures.append(
        _failure(
            method,
            final_url,
            stored.get("reason") or "partial identity OCR still failed identity verification",
            extract_method=extract_method,
            identity_extract_method=f"ocr_identity_pages_{probe_range}/{ocr_backend}",
            corroborate_signal=stored.get("corroborate_signal"),
            corroborate_score=stored.get("corroborate_score"),
        )
    )
    return None


@dataclass(frozen=True)
class FetchPipelineDeps:
    fetch_url: object
    pdf_extract: object
    text_ok: object
    looks_pdf_url: object
    body_looks_html: object
    decode_html: object
    decode_textual_body: object
    meta_map: object
    enqueue_landing_candidates: object
    extract_page_text: object
    is_paywalled_html: object
    is_challenge_html: object
    html_fulltext_ok: object
    classify_html_content: object
    identity_corroboration_text: object
    origin_for_method: object
    extract_abstract_text: object
    store_if_new_abstract: object
    try_store_fulltext: object
    park_unreadable: object
    remember_challenge_host: object | None = None
    pdf_extract_variants: object | None = None
    pdf_extract_with_quality: object | None = None
    pdf_structure_flags: object | None = None
    pdf_extract_native: bool = False
    is_challenge_url: object | None = None
    landing_identity_profile: object | None = None
    abstract_corroborate: object | None = None
    candidate_headers: object | None = None
    sustained_paragraph_profile: object | None = None
    explicit_cited_route_ok: object | None = None


@dataclass(frozen=True)
class FetchQueuePersistenceHooks:
    """Optional durable queue hooks, kept outside the fetch core's DB boundary."""

    freeze_stage_segment: object
    admit_batch: object

    def __post_init__(self) -> None:
        if not callable(self.freeze_stage_segment):
            raise TypeError("freeze_stage_segment must be callable")
        if not callable(self.admit_batch):
            raise TypeError("admit_batch must be callable")


def _looks_meaningful_low_text(text: str) -> bool:
    if not text:
        return False
    alpha = sum(c.isalpha() for c in text)
    if len(text) < 180 or alpha / max(len(text), 1) < 0.45:
        return False
    return len(re.findall(r"[A-Za-zÀ-ÿ]{3,}", text)) >= 20


def _title_after_reference_marker(ref: dict, resolve_result: dict, text: str) -> bool:
    expected_title = (resolve_result or {}).get("matched_title") or ref.get("title") or ""
    if not expected_title:
        return False
    title_words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", expected_title.lower())
    if len(title_words) < 3:
        return False
    phrase = " ".join(title_words)
    full = " ".join(re.findall(r"[A-Za-zÀ-ÿ0-9]+", text.lower()))
    title_pos = full.find(phrase)
    if title_pos < 0:
        return False
    refs_pos = full.find("references")
    bib_pos = full.find("bibliography")
    cutoff = min([p for p in (refs_pos, bib_pos) if p >= 0], default=-1)
    return cutoff >= 0 and title_pos > cutoff


def _low_quality_wrong_document(stored: dict | None, text: str) -> bool:
    if not isinstance(stored, dict) or stored.get("status") != "identity_mismatch":
        return False
    probe = stored.get("identity_probe") or {}
    if not probe or probe.get("ok") is True:
        return False
    if not _looks_meaningful_low_text(text):
        return False
    title_position = probe.get("title_position")
    if title_position is not None:
        low = text.lower()
        refs_pos = low.find("references")
        bib_pos = low.find("bibliography")
        cutoff = min([p for p in (refs_pos, bib_pos) if p >= 0], default=-1)
        if cutoff >= 0 and title_position > cutoff:
            return True
    return title_position is not None or probe.get("author_ok") is False


def _low_quality_verdict_earned() -> bool:
    """Whether ``_low_quality_wrong_document`` may close a PDF as the wrong work.

    That check reads identity off text the quality gate already rejected.  With no
    OCR backend installed, a scan of the *cited* paper and a scan of a *different*
    paper produce the same unreadable output, so ``wrong_document`` is an accusation
    the pipeline has not earned: the honest outcome is the retryable
    ``ocr_backend_unavailable`` degradation the caller falls through to.

    With OCR present the verdict stands — but only *after* ``_maybe_auto_ocr`` has
    run and failed to recover the document.  Callers must defer this verdict until
    then: full-document OCR is stronger evidence than a probe of the rejected text
    layer, and three scans in the corpus (LeCun 1989, Ruderman & Bialek 1994,
    Hochreiter 2001) OCR to an exact title match while their raw text layer does not
    corroborate at all.
    """
    return _ocr_backends_available()


def _suspicious_text_identity_fail(text: str, stored: dict | None) -> bool:
    """Check if text that passed ``_quality()`` but failed identity corroboration
    looks like garbled / corrupt PDF output rather than a genuine document.

    When True, the pipeline should park the PDF as ``unreadable_pdf`` (route to
    the OCR queue) instead of closing with a definitive ``identity_mismatch``.

    A definitive ``identity_mismatch`` means "the text is real English prose, but
    it belongs to a *different* document than the one we expected."  Reporting
    that for garbled PDF output is misleading — the extracted bytes are not
    prose at all, even though they happened to pass the coarse quality gate.
    """
    if not text or not isinstance(stored, dict):
        return False
    if stored.get("status") != "identity_mismatch":
        return False

    # Strong corroboration signal (e.g. DOI match that still failed identity
    # at a deeper probe level) means the text IS real — just wrong document.
    sig = str(stored.get("corroborate_signal") or "")
    score = stored.get("corroborate_score")
    if sig and sig not in ("text", "none", "") and score is not None and score > 0:
        return False

    # ── garbled-text hallmarks ──────────────────────────────────────────
    words = re.findall(r"[A-Za-zÀ-ÿ]{3,}", text)
    words_lower = [w.lower() for w in words]

    # Very high unique-to-total ratio: garbled text has few repeated tokens.
    if len(words_lower) >= 20:
        unique_ratio = len(set(words_lower)) / len(words_lower)
        if unique_ratio > 0.92:
            return True

    # Unusually long average word length signals encoding corruption.
    if words:
        avg_len = sum(len(w) for w in words) / len(words)
        if avg_len > 10:
            return True

    # Lines with excessive non-alpha content suggest interleaved binary.
    lines = text.splitlines()
    if len(lines) > 5:
        garbage_lines = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            alpha_ratio = sum(c.isalpha() for c in stripped) / max(len(stripped), 1)
            if alpha_ratio < 0.4 and len(stripped) > 20:
                garbage_lines += 1
        if garbage_lines > len(lines) * 0.3:
            return True

    return False


def _failure(method: str, url: str, reason: str, **extra) -> dict:
    row = {"method": method, "url": url, "reason": reason}
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    return row


def _transport_request_failure(method: str, url: str, fetched: dict) -> dict | None:
    """Turn the internal no-response envelope into an audit-safe failure."""
    error = fetched.get("transport_error")
    if not isinstance(error, dict):
        return None
    exception_type = error.get("exception_type")
    if not isinstance(exception_type, str) or not exception_type:
        exception_type = "Exception"
    reason = f"transport request failed ({exception_type})"
    return {
        "failure": _failure(method, url, reason, reason_code="transport_error"),
        "attempt": {
            "outcome": "request_failed",
            "reason": reason,
            "reason_code": "transport_error",
        },
    }


def _provider_auth_unavailable(method: str, url: str, fetched: dict) -> dict | None:
    if fetched.get("provider_auth_unavailable") is not True:
        return None
    reason = "provider credential unavailable for persisted candidate; request not sent"
    return {
        "failure": _failure(method, url, reason, reason_code="provider_auth_unavailable"),
        "attempt": {
            "outcome": "provider_auth_unavailable",
            "reason": reason,
            "reason_code": "provider_auth_unavailable",
        },
    }


def _content_decoding_rejection(
    *,
    method: str,
    final_url: str,
    fetched: dict,
    failures: list[dict],
) -> dict | None:
    """Stop a declared encoding failure before any content parser runs."""
    error = fetched.get("content_decoding_error")
    if not isinstance(error, dict):
        return None
    encoding = str(error.get("encoding") or "unknown")
    reason_code = str(error.get("reason_code") or "content_encoding_decode_failed")
    if reason_code not in {
        "content_encoding_decode_failed",
        "content_encoding_unsupported",
    }:
        reason_code = "content_encoding_decode_failed"
    reason = (
        f"response used unsupported Content-Encoding {encoding!r}"
        if reason_code == "content_encoding_unsupported"
        else f"response body could not be decoded from Content-Encoding {encoding!r}"
    )
    failures.append(
        _failure(
            method,
            final_url,
            reason,
            status=fetched.get("status"),
            content_type=fetched.get("content_type") or None,
            reason_code=reason_code,
            content_encoding=encoding,
        )
    )
    return {
        "return": None,
        "attempt": {
            "outcome": "content_decode_failed",
            "reason": reason,
            "reason_code": reason_code,
            "content_encoding": encoding,
        },
    }


def _abstract_fetch_status(resolve_result: dict) -> str:
    """``abstract_only`` when the source genuinely has no full text, otherwise
    ``abstract_fallback`` (an abstract was reached but a fuller text may exist)."""
    return "abstract_only" if resolve_result.get("fulltext_exists") is False else "abstract_fallback"


def _abstract_identity_ok(
    identity_profile: dict | None,
    *,
    abstract_corroborated: bool = False,
) -> bool:
    """Keep landing abstracts tied to a verified work identity.

    A matched DOI/PMID/PMCID is decisive.  Identifierless landings may also be
    recovered through an exact title plus author/year, or an exact title on the
    explicitly cited route.  Any remaining landing needs corroboration from its
    extracted abstract; a shared host alone is intentionally not a route match.
    """
    profile = identity_profile or {}
    if profile.get("status") == "conflict":
        return False
    return bool(
        profile.get("status") == "matched"
        or (
            profile.get("title_match") is True
            and (
                profile.get("author_year_match") is True
                or profile.get("cited_route_match") is True
            )
        )
        or abstract_corroborated
    )


def _metadata_shell_abstract_ok(identity_profile: dict | None, abstract_source: str | None) -> bool:
    """Allow visible shell abstracts only for authoritative exact ID matches."""
    if abstract_source == "metadata":
        return True
    profile = identity_profile or {}
    scheme = profile.get("matched_scheme")
    identifier_sources = profile.get("expected_identifier_sources") or {}
    return bool(
        abstract_source == "visible"
        and profile.get("status") == "matched"
        and scheme in {"doi", "pmid", "pmcid"}
        and identifier_sources.get(scheme) in {"reference", "resolver"}
    )


def _is_access_denied_status(status: int | None) -> bool:
    return status in (401, 403)


_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "applications/vnd.pdf",
}


def _pdf_response_rejection(
    status: int | None, content_type: str, body: bytes,
) -> tuple[str, str] | None:
    """Return a persisted rejection for bytes that must not enter PDF tooling."""
    if not isinstance(status, int) or not 200 <= status < 300:
        shown_status = str(status) if status is not None else "unknown"
        return (
            "http_non_success",
            f"PDF response returned HTTP {shown_status}, not a successful 2xx response",
        )
    media_type = content_type.split(";", 1)[0].strip().lower()
    # Some repositories serve PDFs with no type or generic binary. The magic
    # check below still makes those safe while rejecting declared JSON/XML/etc.
    if media_type and media_type not in _PDF_CONTENT_TYPES and media_type not in {
        "application/octet-stream", "binary/octet-stream",
    }:
        return (
            "incompatible_content_type",
            f"PDF response content type {content_type!r} is not compatible with PDF",
        )
    if not (body or b"")[:1024].lstrip().startswith(b"%PDF"):
        return ("missing_pdf_magic", "PDF response body lacks PDF magic")
    return None


def _reject_pdf_response(
    *, failures: list[dict], method: str, final_url: str,
    status: int | None, content_type: str, body: bytes,
) -> dict | None:
    rejection = _pdf_response_rejection(status, content_type, body)
    if rejection is None:
        return None
    outcome, reason = rejection
    failures.append(_failure(
        method, final_url, reason, status=status, content_type=content_type or None,
    ))
    return {"return": None, "attempt": {"outcome": outcome, "reason": reason}}


def _trace_attempt(
    trace: dict | None,
    *,
    queue_index: int,
    batch_index: int,
    candidate: dict,
    fetched: dict | None,
    outcome: str,
    reason: str | None = None,
    frozen_candidate_id: int | None = None,
    **extra,
):
    if trace is None:
        return
    row = {
        "queue_index": queue_index,
        "batch_index": batch_index,
        "method": candidate.get("method"),
        "url": candidate.get("url"),
        "kind": candidate.get("kind"),
        "outcome": outcome,
    }
    if frozen_candidate_id is not None:
        if type(frozen_candidate_id) is not int or frozen_candidate_id <= 0:
            raise ValueError("invalid frozen candidate ID")
        row["frozen_candidate_id"] = frozen_candidate_id
    if fetched is None:
        row["request"] = "none"
    else:
        row["status"] = fetched.get("status")
        row["final_url"] = fetched.get("url")
        row["content_type"] = fetched.get("content_type")
        if fetched.get("headers"):
            row["headers"] = fetched.get("headers")
        if fetched.get("body_head"):
            row["body_head"] = fetched.get("body_head")
        if fetched.get("challenge_markers"):
            row["challenge_markers"] = list(fetched.get("challenge_markers") or [])
        if fetched.get("cached_challenge"):
            row["cached_challenge"] = True
    if reason is not None:
        row["reason"] = reason
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    trace.setdefault("attempts", []).append(row)


def _extract_pdf_text(
    deps: FetchPipelineDeps,
    tmp_path: str,
    pdf_bytes: bytes,
    *,
    ref_id: str | None = None,
):
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    with _perf.span("fetch_pdf_extract", ref_id):
        text, method = deps.pdf_extract(tmp_path)
    flags = (
        list(deps.pdf_structure_flags(text) or [])
        if callable(deps.pdf_structure_flags)
        else []
    )
    # Preserve the established extraction seam for callers that supply their
    # own extractor, while upgrading native PDF extraction when its first
    # readable result is structurally suspicious.
    if flags and deps.pdf_extract_native and callable(deps.pdf_extract_with_quality):
        with _perf.span("fetch_pdf_extract", ref_id):
            return deps.pdf_extract_with_quality(tmp_path)
    return text, method, flags


def _handle_cran_archive_candidate(**kwargs) -> dict:
    """Extract the one audited PDF from a versioned CRAN source archive."""
    candidate = kwargs["candidate"]
    fetched = kwargs["fetched"]
    failures = kwargs["failures"]
    method = candidate["method"]
    url = candidate["url"]
    if not fetched:
        failures.append(_failure(method, url, "download failed"))
        return {"return": None, "attempt": {"outcome": "download_failed", "reason": "download failed"}}
    transport_failure = _transport_request_failure(method, url, fetched)
    if transport_failure is not None:
        failures.append(transport_failure["failure"])
        return {"return": None, "attempt": transport_failure["attempt"]}
    status = fetched.get("status")
    final_url = fetched.get("url") or url
    if not isinstance(status, int) or not 200 <= status < 300:
        failures.append(_failure(method, final_url, "CRAN archive response was not a successful 2xx response", status=status))
        return {"return": None, "attempt": {"outcome": "http_non_success", "reason": "CRAN archive response was not a successful 2xx response"}}
    try:
        from core.fetch.extraction.cran_archive import CranArchiveError, extract_vegan_2_5_3_archive
    except ImportError:
        from extraction.cran_archive import CranArchiveError, extract_vegan_2_5_3_archive
    try:
        pdf_bytes, description = extract_vegan_2_5_3_archive(fetched.get("body") or b"")
    except CranArchiveError as exc:
        failures.append(_failure(method, final_url, str(exc), reason_code=exc.code))
        return {"return": None, "attempt": {"outcome": exc.code, "reason": str(exc), "reason_code": exc.code}}
    pdf_candidate = dict(candidate, kind="pdf", content_type="application/pdf")
    pdf_fetched = dict(fetched, body=pdf_bytes, content_type="application/pdf")
    identity_extract_prefix = "\n".join(
        f"{key}: {value}" for key, value in description.items()
    )
    return _handle_pdf_candidate(candidate=pdf_candidate, fetched=pdf_fetched, **{
        key: value for key, value in kwargs.items() if key not in {"candidate", "fetched"}
    }, identity_extract_prefix=identity_extract_prefix)


def _candidate_context(candidate: dict | None) -> dict | None:
    """Selected per-link identity plus conflict metadata retained by URL dedup."""
    candidate = candidate or {}
    raw = candidate.get("identity_context")
    context = dict(raw) if isinstance(raw, dict) else {}
    if candidate.get("identity_context_conflict"):
        context["context_conflict"] = True
    if candidate.get("cited_landing_pdf") is True:
        parent = candidate.get("cited_landing_url")
        if isinstance(parent, str) and parent:
            context["cited_landing_pdf"] = True
            context["cited_landing_url"] = parent
    return context or None


def _retry_pdf_parser_variants(
    *, deps: FetchPipelineDeps, ref: dict, run_dir: str, resolve_result: dict,
    tmp_path: str, initial_text: str, initial_method: str, method: str,
    final_url: str, candidate: dict, redirect_chain: list | None = None,
    identity_extract_prefix: str | None = None,
) -> tuple[dict | None, list[dict]]:
    """Try alternate native parsers after the preferred parser cannot decide identity."""
    if not callable(deps.pdf_extract_variants):
        return None, []
    try:
        with _perf.span("fetch_pdf_extract", ref.get("id")):
            variants = deps.pdf_extract_variants(tmp_path) or []
    except Exception as exc:
        return None, [{"method": "variants", "error": f"{type(exc).__name__}: {exc}"}]
    trace = []
    for variant in variants:
        parser = variant.get("method") or "unknown"
        text = variant.get("text") or ""
        row = {
            "method": parser,
            "quality_ok": bool(variant.get("quality_ok")),
            "quality_metrics": variant.get("quality_metrics") or {},
            "structure_flags": variant.get("structure_flags") or [],
            "error": variant.get("error"),
        }
        if (parser == initial_method and text == initial_text) or not row["quality_ok"]:
            trace.append(row)
            continue
        stored = deps.try_store_fulltext(
            run_dir, ref, resolve_result, text,
            method=method, source_ref=final_url, extract_method=parser,
            content_version=candidate.get("content_version"),
            candidate_context=_candidate_context(candidate),
            identity_extract_text=(
                f"{identity_extract_prefix}\n\n{text}"
                if identity_extract_prefix else None
            ),
            identity_extract_method=(
                f"cran_description+{parser}"
                if identity_extract_prefix else parser
            ),
            extraction_flags=row["structure_flags"],
            redirect_chain=redirect_chain,
        )
        row["identity_decision"] = (stored.get("identity_probe") or {}).get("decision")
        row["identity_status"] = stored.get("status")
        trace.append(row)
        if stored.get("status") == "stored":
            stored["parser_variants"] = trace
            stored["parser_disagreement"] = True
            return stored, trace
        if (stored.get("identity_probe") or {}).get("reason_code") == "identifier_conflict":
            break
    return None, trace


def _aggregate_parser_identity(initial: dict, variants: list[dict]) -> dict:
    """A parser-local rejection is definitive only when no usable parser is unsure."""
    statuses = [str(initial.get("status") or "")]
    statuses.extend(
        str(row.get("identity_status") or "")
        for row in variants
        if row.get("quality_ok")
    )
    if "identity_inconclusive" not in statuses:
        return initial
    if initial.get("status") == "identity_inconclusive":
        return initial
    aggregated = dict(initial)
    aggregated.update({
        "status": "identity_inconclusive",
        "reason": "PDF parser results did not agree on a definitive identity rejection",
        "reason_code": "parser_identity_inconclusive",
        "parser_disagreement": True,
    })
    return aggregated


_HEADERS_UNRESOLVED = object()


def candidate_request_identity(
    deps: FetchPipelineDeps,
    candidate: dict,
    *,
    headers_extra=_HEADERS_UNRESOLVED,
):
    """Return the exact cache identity used for this candidate's GET."""
    url = candidate["url"]
    kind = candidate.get("kind")
    referer = candidate.get("referer")
    if headers_extra is _HEADERS_UNRESOLVED:
        headers_extra = (
            deps.candidate_headers(candidate)
            if callable(deps.candidate_headers) else None
        )
    if callable(deps.candidate_headers) and headers_extra == {}:
        return None
    profile = "pdf" if kind == "pdf" else "document"
    accept = (
        "application/pdf,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8" if kind == "pdf" else
        "application/gzip,application/x-gzip,application/octet-stream;q=0.9,*/*;q=0.8" if kind == "cran_archive" else
        "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"
    )
    strategy = candidate.get("strategy") or candidate.get("method") or "auto"
    from core.fetch.storage.fetch_cache import FetchRunContext
    identity = FetchRunContext.request_identity(
        url, profile, strategy, accept=accept, referer=referer,
        headers_extra=headers_extra,
    )
    return identity


def _fetch_candidate(deps: FetchPipelineDeps, candidate: dict) -> dict:
    url = candidate["url"]
    kind = candidate.get("kind")
    referer = candidate.get("referer")
    headers_extra = (
        deps.candidate_headers(candidate)
        if callable(deps.candidate_headers) else None
    )
    # ``None`` means no provider hook owns this candidate. An empty mapping
    # means a provider-owned persisted candidate cannot be authenticated, so
    # it must not be sent as an unauthenticated ordinary request.
    if callable(deps.candidate_headers) and headers_extra == {}:
        return {
            "candidate": candidate,
            "fetched": {
                "status": None,
                "url": url,
                "body": b"",
                "provider_auth_unavailable": True,
            },
        }
    header_kwargs = {"headers_extra": headers_extra} if headers_extra else {}
    strategy = candidate.get("strategy") or candidate.get("method") or "auto"
    try:
        if kind == "pdf":
            fetched = deps.fetch_url(
                url,
                accept="application/pdf,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                profile="pdf",
                strategy=strategy,
                referer=referer,
                **header_kwargs,
            )
        elif kind == "cran_archive":
            fetched = deps.fetch_url(
                url,
                accept="application/gzip,application/x-gzip,application/octet-stream;q=0.9,*/*;q=0.8",
                profile="document",
                strategy=strategy,
                referer=referer,
                **header_kwargs,
            )
        else:
            fetched = deps.fetch_url(
                url,
                accept="text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
                profile="document",
                strategy=strategy,
                referer=referer,
                **header_kwargs,
            )
    except Exception as exc:
        try:
            from core.fetch.transport.http import FetchAdmissionDeferred
        except ImportError:
            from http import FetchAdmissionDeferred
        if not isinstance(exc, FetchAdmissionDeferred):
            raise
        return {
            "candidate": candidate,
            "fetched": {
                "fetch_deferred": True,
                "deferred_host": exc.host,
                "deferred_until": exc.not_before,
                "deferred_wait_seconds": exc.wait_seconds,
                "deferred_reason_code": getattr(exc, "reason_code", "host_cooldown"),
            },
        }
    return {"candidate": candidate, "fetched": fetched}


def _prefetch_batch(
    deps: FetchPipelineDeps, batch: list[dict], candidate_workers: int
) -> list[dict]:
    workers = max(1, min(candidate_workers, len(batch)))
    if workers <= 1:
        return [_fetch_candidate(deps, candidate) for candidate in batch]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch_candidate, deps, candidate) for candidate in batch]
        return [future.result() for future in futures]


def _redirect_route(url: str | None) -> tuple[str, str, str] | None:
    """Normalize only harmless URL spelling changes for redirect comparison."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname.lower().removeprefix("www.").rstrip(".")
    port = parsed.port
    if port not in (None, 80, 443):
        host = f"{host}:{port}"
    path = urllib.parse.unquote(parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    return host, path, query


def _is_soft_dead_link_redirect(
    *,
    requested_url: str,
    final_url: str,
    redirect_chain: list | None,
    status: int | None,
    content_kind: str,
    identity_profile: dict,
    abstract_entry: dict | None,
    paywalled: bool,
) -> bool:
    """Recognize an unrelated HTTP-200 redirect worth a bounded archive lookup.

    This is deliberately not a finding that the cited page no longer exists.
    It only distinguishes a materially different, identity-empty landing from
    harmless URL canonicalization so Wayback may try the original cited URL.
    """
    if (
        status != 200
        or not redirect_chain
        or content_kind != "landing_page"
        or abstract_entry is not None
        or paywalled
        or identity_profile.get("status") != "unconfirmed"
        or identity_profile.get("title_match")
        or identity_profile.get("author_year_match")
    ):
        return False
    requested_route = _redirect_route(requested_url)
    final_route = _redirect_route(final_url)
    return bool(requested_route and final_route and requested_route != final_route)


def _maybe_handle_textual_response(
    *,
    deps: FetchPipelineDeps,
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    queue: list[dict],
    seen: set[str],
    failures: list[dict],
    method: str,
    strategy: str,
    requested_url: str,
    final_url: str,
    html_text: str,
    status: int | None,
    from_pdf_candidate: bool = False,
    content_version: str | None = None,
    candidate_context: dict | None = None,
    fallback_stage: str | None = None,
    redirect_chain: list | None = None,
) -> dict:
    with _perf.span("fetch_html_extract", ref.get("id")):
        meta = deps.meta_map(html_text)
        html_profile = deps.classify_html_content(final_url, html_text, meta)
    page_text = html_profile.get("page_text") or ""
    abstract_text = html_profile.get("abstract_text")
    abstract_source = html_profile.get("abstract_source")
    content_kind = html_profile.get("content_kind") or "landing_page"
    paywalled = deps.is_paywalled_html(html_text)
    challenged = deps.is_challenge_html(html_text) or bool(
        callable(deps.is_challenge_url) and deps.is_challenge_url(final_url)
    )

    if challenged:
        if deps.remember_challenge_host is not None:
            deps.remember_challenge_host(
                final_url,
                profile="pdf" if from_pdf_candidate else "document",
                strategy=strategy,
            )
        failures.append(
            _failure(
                method,
                final_url,
                "publisher challenge page blocked automated retrieval",
                status=status,
            )
        )
        return {
            "result": None,
            "paywalled": paywalled,
            "abstract_entry": None,
            "abstract_reason": None,
            "last_identity": None,
            "trace_outcome": "challenge_blocked",
            "content_kind": content_kind,
            "html_profile": html_profile,
        }

    source_type = str(ref.get("source_type") or ref.get("source_kind") or "").strip().lower()
    explicit_webpage = (
        method == "reference_url"
        and bool(ref.get("url"))
        and source_type in {"webpage", "website", "web", "blog", "news", "documentation"}
    )
    if explicit_webpage and status != 200:
        failures.append(_failure(
            method, final_url,
            "explicit cited webpage did not return HTTP 200",
            status=status,
        ))
        return {
            "result": None,
            "paywalled": paywalled,
            "abstract_entry": None,
            "abstract_reason": None,
            "last_identity": {
                "status": "identity_inconclusive",
                "reason": "explicit cited webpage did not return HTTP 200",
                "reason_code": "cited_url_http_non_success",
            },
            "trace_outcome": "identity_inconclusive",
            "content_kind": content_kind,
            "html_profile": html_profile,
        }

    identity_profile = (
        deps.landing_identity_profile(
            ref, resolve_result, meta, candidate_context, requested_url, final_url
        )
        if callable(deps.landing_identity_profile)
        else {"status": "unconfirmed"}
    )
    if identity_profile.get("status") == "conflict":
        scheme = identity_profile.get("conflict_scheme") or "identifier"
        failures.append(_failure(
            method,
            final_url,
            f"landing page declared a conflicting {scheme}; not the cited work",
            status=status,
        ))
        return {
            "result": None,
            "paywalled": paywalled,
            "abstract_entry": None,
            "abstract_reason": None,
            "last_identity": {"status": "identity_mismatch", "reason": "landing page identifier conflict"},
            "trace_outcome": "identity_mismatch",
            "content_kind": content_kind,
            "html_profile": html_profile,
        }

    landing_deferred = deps.enqueue_landing_candidates(queue, seen, final_url, html_text, meta)
    last_identity = None
    if content_kind == "fulltext_html" and not paywalled:
        stored = deps.try_store_fulltext(
            run_dir,
            ref,
            resolve_result,
            page_text,
            method=method,
            source_ref=final_url,
            extract_method="html",
            corroborate_text=deps.identity_corroboration_text(page_text, meta),
            content_version=content_version,
            candidate_context=candidate_context,
            identity_extract_method="html",
            redirect_chain=redirect_chain,
        )
        if stored["status"] == "stored":
            aliases = list(dict.fromkeys(url for url in (requested_url, final_url) if url))
            if len(aliases) > 1:
                stored["source_aliases"] = aliases
            if identity_profile.get("canonical_doi"):
                stored["canonical_doi"] = identity_profile["canonical_doi"]
            stored["failures"] = failures
            return {
                "result": stored,
                "paywalled": paywalled,
                "abstract_entry": None,
                "abstract_reason": None,
                "last_identity": None,
                "trace_outcome": "stored",
                "content_kind": content_kind,
                "html_profile": html_profile,
                "landing_deferred": landing_deferred,
            }
        last_identity = stored
        failures.append(
            _failure(
                method,
                final_url,
                stored["reason"],
                corroborate_signal=stored.get("corroborate_signal"),
                corroborate_score=stored.get("corroborate_score"),
                html_content_kind=content_kind,
            )
        )

    if (
        content_kind == "landing_page"
        and explicit_webpage
        and status == 200
        and not paywalled
        and not html_profile.get("force_metadata_shell")
        and callable(deps.sustained_paragraph_profile)
        and callable(deps.explicit_cited_route_ok)
        and deps.explicit_cited_route_ok(ref, final_url, redirect_chain)
    ):
        recovery = deps.sustained_paragraph_profile(html_text)
        recovery_text = None
        extract_method = None
        if isinstance(recovery, dict) and recovery.get("eligible") is True:
            recovery_text = recovery.get("text")
            extract_method = "html:explicit-webpage-paragraph-fallback"
        elif isinstance(recovery, dict) and recovery.get("short_article_eligible") is True:
            cited_year = str(ref.get("year") or "").strip()
            article_metadata = any(
                str(value).strip().lower() == "article"
                for value in meta.get("og:type", [])
            )
            publication_year_match = bool(
                re.fullmatch(r"\d{4}", cited_year)
                and any(
                    re.match(rf"{re.escape(cited_year)}(?:-|$)", str(value).strip())
                    for value in meta.get("article:published_time", [])
                )
            )
            if (
                article_metadata
                and identity_profile.get("title_match") is True
                and identity_profile.get("cited_route_match") is True
                and publication_year_match
            ):
                recovery_text = recovery.get("candidate_text")
                extract_method = "html:explicit-webpage-short-article"
        if recovery_text and extract_method:
            stored = deps.try_store_fulltext(
                run_dir,
                ref,
                resolve_result,
                recovery_text,
                method=method,
                source_ref=final_url,
                extract_method=extract_method,
                corroborate_text=deps.identity_corroboration_text(recovery_text, meta),
                content_version=content_version,
                candidate_context=candidate_context,
                identity_extract_method=extract_method,
                redirect_chain=redirect_chain,
            )
            if stored["status"] == "stored":
                aliases = list(dict.fromkeys(
                    url for url in (requested_url, final_url) if url
                ))
                if len(aliases) > 1:
                    stored["source_aliases"] = aliases
                if identity_profile.get("canonical_doi"):
                    stored["canonical_doi"] = identity_profile["canonical_doi"]
                stored["failures"] = failures
                return {
                    "result": stored,
                    "paywalled": paywalled,
                    "abstract_entry": None,
                    "abstract_reason": None,
                    "last_identity": None,
                    "trace_outcome": "stored",
                    "content_kind": "fulltext_html",
                    "html_profile": html_profile,
                    "landing_deferred": landing_deferred,
                }
            last_identity = stored
            failures.append(
                _failure(
                    method,
                    final_url,
                    stored["reason"],
                    corroborate_signal=stored.get("corroborate_signal"),
                    corroborate_score=stored.get("corroborate_score"),
                    html_content_kind=content_kind,
                )
            )

    abstract_entry = None
    abstract_reason = None
    abstract_metadata = None
    abstract_method = None
    effective_content_kind = content_kind
    # A conflicting declared identifier is terminal. For otherwise unconfirmed
    # landings, use the existing corroborator on the explicit abstract itself.
    # Keep this dependency injected so callers/tests can preserve their source
    # policy without coupling the pipeline to resolve.sources.
    abstract_corroborated = False
    if abstract_text and callable(deps.abstract_corroborate):
        abstract_corroborated = bool(deps.abstract_corroborate(
            ref, abstract_text, resolve_result
        ))
    abstract_identity_ok = _abstract_identity_ok(
        identity_profile, abstract_corroborated=abstract_corroborated
    )
    abstract_allowed_on_shell = (
        content_kind != "metadata_shell"
        or _metadata_shell_abstract_ok(identity_profile, abstract_source)
    )
    if (
        abstract_text
        and abstract_identity_ok
        and abstract_allowed_on_shell
        and not html_profile.get("force_metadata_shell")
    ):
        abstract_entry = deps.store_if_new_abstract(
            run_dir,
            ref,
            deps.origin_for_method(method, resolve_result),
            abstract_text,
            final_url,
        )
        if (
            abstract_entry is not None
            and content_kind == "metadata_shell"
            and abstract_source == "visible"
        ):
            # Preserve the conservative raw classification in ``html_profile``.
            # Once its visible abstract has passed the authoritative identity
            # gate and has actually been stored, report the accepted tier in
            # the fetch trace rather than leaving it labelled as a shell.
            effective_content_kind = "abstract_html"
        abstract_reason = (
            "landing page exposed only an abstract"
            if paywalled
            else (
                "landing page yielded abstract but no corroborated OA full text"
                if content_kind != "metadata_shell"
                else "metadata landing page exposed an abstract but not the article body"
            )
        )
        # Name the fallback stage the abstract came from, when it came from one.
        # "landing" stays the label for a page reached the ordinary way; only a
        # stage that reads something other than the live publisher page — an
        # archived snapshot — needs to say so, because a report that calls that
        # "landing" misstates what was actually read.
        abstract_method = fallback_stage or None
        abstract_metadata = {
            "source_aliases": list(dict.fromkeys(
                url for url in (requested_url, final_url) if url
            )),
            "canonical_doi": identity_profile.get("canonical_doi"),
            "article_id": identity_profile.get("article_id"),
            "citation_pdf_url": identity_profile.get("pdf_url"),
        }
    elif content_kind == "metadata_shell":
        failures.append(
            _failure(
                method,
                final_url,
                html_profile.get("shell_reason")
                or "metadata landing page did not expose article text",
                status=status,
                html_content_kind=content_kind,
                has_pdf_link=html_profile.get("has_pdf_link"),
                shell_markers=html_profile.get("shell_markers"),
                page_chars=html_profile.get("page_chars"),
                abstract_chars=html_profile.get("abstract_chars"),
            )
        )

    if from_pdf_candidate:
        failures.append(
            _failure(
                method,
                final_url,
                "pdf candidate resolved to HTML landing page",
                status=status,
                html_content_kind=content_kind,
            )
        )

    if _is_soft_dead_link_redirect(
        requested_url=requested_url,
        final_url=final_url,
        redirect_chain=redirect_chain,
        status=status,
        content_kind=content_kind,
        identity_profile=identity_profile,
        abstract_entry=abstract_entry,
        paywalled=paywalled,
    ):
        failures.append(
            _failure(
                method,
                final_url,
                "live cited URL redirected to an unrelated landing page",
                status=status,
                reason_code="soft_dead_link_redirect",
                requested_url=requested_url,
            )
        )

    return {
        "result": None,
        "paywalled": paywalled,
        "abstract_entry": abstract_entry,
        "abstract_reason": abstract_reason,
        "abstract_metadata": abstract_metadata,
        "abstract_method": abstract_method,
        "last_identity": last_identity,
        "content_kind": effective_content_kind,
        "html_profile": html_profile,
        "identity_profile": identity_profile,
        "metadata_shell": (
            {
                "method": method,
                "url": final_url,
                "reason": html_profile.get("shell_reason")
                or "metadata landing page did not expose article text",
                "has_pdf_link": html_profile.get("has_pdf_link"),
                "shell_markers": html_profile.get("shell_markers"),
            }
            if content_kind == "metadata_shell" and abstract_entry is None
            else None
        ),
        "landing_deferred": landing_deferred,
        "trace_outcome": (
            _abstract_fetch_status(resolve_result)
            if abstract_entry is not None
            # The store already decided *why* the candidate was refused — an
            # identity mismatch and a cited-URL route mismatch are different
            # verdicts.  Flattening both to "identity_mismatch" here loses that
            # in the recorded trace, which is the only evidence a later reader
            # has, so carry the store's own status through.
            else (last_identity.get("status") or "identity_mismatch")
            if last_identity is not None
            else "metadata_shell"
            if content_kind == "metadata_shell"
            else "landing_no_store"
        ),
    }


def _handle_pdf_candidate(
    *,
    deps: FetchPipelineDeps,
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    queue: list[dict],
    seen: set[str],
    failures: list[dict],
    candidate: dict,
    fetched: dict | None,
    tmp_path: str,
    identity_extract_prefix: str | None = None,
) -> dict:
    method = candidate["method"]
    url = candidate["url"]
    if not fetched:
        failures.append(_failure(method, url, "download failed"))
        return {"return": None, "attempt": {"outcome": "download_failed", "reason": "download failed"}}

    transport_failure = _transport_request_failure(method, url, fetched)
    if transport_failure is not None:
        failures.append(transport_failure["failure"])
        return {"return": None, "attempt": transport_failure["attempt"]}
    auth_unavailable = _provider_auth_unavailable(method, url, fetched)
    if auth_unavailable is not None:
        failures.append(auth_unavailable["failure"])
        return {"return": None, "attempt": auth_unavailable["attempt"]}

    final_url = fetched.get("url") or url
    status = fetched.get("status")
    content_type = fetched.get("content_type") or ""
    decoding_rejection = _content_decoding_rejection(
        method=method,
        final_url=final_url,
        fetched=fetched,
        failures=failures,
    )
    if decoding_rejection is not None:
        return decoding_rejection
    body = fetched.get("body") or b""
    redirect_chain = fetched.get("redirect_chain")
    html_text = deps.decode_textual_body(body, content_type)
    challenged = html_text is not None and (
        deps.is_challenge_html(html_text)
        or bool(callable(deps.is_challenge_url) and deps.is_challenge_url(final_url))
    )
    paywalled = html_text is not None and deps.is_paywalled_html(html_text)
    if _is_access_denied_status(status) and not (challenged or paywalled):
        failures.append(
            _failure(
                method,
                final_url,
                "access denied",
                status=status,
                content_type=content_type or None,
            )
        )
        return {
            "return": None,
            "attempt": {"outcome": "access_denied", "reason": "access denied"},
        }
    if (not isinstance(status, int) or not 200 <= status < 300) and not (challenged or paywalled):
        rejected = _reject_pdf_response(
            failures=failures, method=method, final_url=final_url,
            status=status, content_type=content_type, body=body,
        )
        if rejected is not None:
            return rejected
    # A PDF-declared response must prove it is a PDF even when the body looks
    # like HTML; publisher error pages are often mislabeled application/pdf.
    if "pdf" in content_type.lower():
        rejected = _reject_pdf_response(
            failures=failures, method=method, final_url=final_url,
            status=status, content_type=content_type, body=body,
        )
        if rejected is not None:
            return rejected
    if html_text and (
        deps.body_looks_html(body, content_type)
        or challenged
        or paywalled
    ):
        landing = _maybe_handle_textual_response(
            deps=deps,
            ref=ref,
            run_dir=run_dir,
            resolve_result=resolve_result,
            queue=queue,
            seen=seen,
            failures=failures,
            method=method,
            strategy=candidate.get("strategy") or method or "auto",
            requested_url=url,
            final_url=final_url,
            html_text=html_text,
            status=status,
            from_pdf_candidate=True,
            content_version=candidate.get("content_version"),
            candidate_context=_candidate_context(candidate),
            fallback_stage=candidate.get("fallback_stage"),
            redirect_chain=redirect_chain,
        )
        attempt_reason = landing.get("abstract_reason") or (landing.get("metadata_shell") or {}).get("reason")
        if attempt_reason is None and landing["trace_outcome"] == "challenge_blocked":
            attempt_reason = "publisher challenge page blocked automated retrieval"
        if attempt_reason is None:
            attempt_reason = (landing.get("last_identity") or {}).get("reason")
        attempt = {
            "outcome": landing["trace_outcome"],
            "reason": attempt_reason,
            "reason_code": (landing.get("last_identity") or {}).get("reason_code"),
            "html_content_kind": landing.get("content_kind"),
            "has_pdf_link": (landing.get("html_profile") or {}).get("has_pdf_link"),
            "shell_reason": (landing.get("html_profile") or {}).get("shell_reason"),
            "shell_markers": (landing.get("html_profile") or {}).get("shell_markers"),
            "page_chars": (landing.get("html_profile") or {}).get("page_chars"),
            "abstract_chars": (landing.get("html_profile") or {}).get("abstract_chars"),
        }
        if landing["result"] is not None:
            return {
                "return": landing["result"], "attempt": attempt,
                "landing_deferred": landing.get("landing_deferred"),
            }
        return {
            "return": None,
            "attempt": attempt,
            "paywalled": landing["paywalled"],
            "abstract_entry": landing["abstract_entry"],
            "abstract_reason": landing["abstract_reason"],
            "abstract_metadata": landing.get("abstract_metadata"),
            "abstract_method": landing.get("abstract_method"),
            "last_identity": landing["last_identity"],
            "metadata_shell": landing.get("metadata_shell"),
            "landing_deferred": landing.get("landing_deferred"),
        }

    rejected = _reject_pdf_response(
        failures=failures, method=method, final_url=final_url,
        status=status, content_type=content_type, body=body,
    )
    if rejected is not None:
        return rejected

    text, extract_method, extraction_flags = _extract_pdf_text(
        deps, tmp_path, body, ref_id=ref.get("id")
    )
    identity_extract_text = (
        f"{identity_extract_prefix}\n\n{text}" if identity_extract_prefix else None
    )
    identity_extract_method = (
        f"cran_description+{extract_method}"
        if identity_extract_prefix else extract_method
    )
    if not deps.text_ok(text):
        if _looks_meaningful_low_text(text) and _title_after_reference_marker(ref, resolve_result, text):
            failures.append(
                _failure(
                    method,
                    final_url,
                    "expected title found only after references marker; downloaded PDF was not the cited work",
                    chars=len(text),
                    extract_method=extract_method,
                )
            )
            return {
                "return": {
                    "status": "wrong_document",
                    "method": extract_method,
                    "reason": "expected title found only after references marker; downloaded PDF was not the cited work",
                    "pdf_url": url,
                    "content_type": content_type,
                    "failures": failures,
                },
                "attempt": {
                    "outcome": "wrong_document",
                    "reason": "expected title found only after references marker; downloaded PDF was not the cited work",
                    "chars": len(text),
                    "extract_method": extract_method,
                },
            }
        low_quality_identity = deps.try_store_fulltext(
            run_dir,
            ref,
            resolve_result,
            text,
            method=method,
            source_ref=final_url,
            extract_method=extract_method,
            content_version=candidate.get("content_version"),
            candidate_context=_candidate_context(candidate),
            identity_extract_text=identity_extract_text,
            identity_extract_method=identity_extract_method,
            extraction_flags=extraction_flags,
            redirect_chain=redirect_chain,
        )
        wrong_document_verdict = (
            _low_quality_wrong_document(low_quality_identity, text)
            and _low_quality_verdict_earned()
        )
        failures.append(
            _failure(
                method,
                final_url,
                f"text below quality threshold via {extract_method}",
                chars=len(text),
            )
        )

        # Auto-OCR: try to recover a scanned PDF before giving up.  This runs even
        # when the identity probe above pointed at a different work: that probe read
        # text the quality gate had already rejected, so OCR of the whole document is
        # the stronger evidence and has to be allowed to overturn it.
        ocr_stored = _maybe_auto_ocr(
            deps=deps,
            ref=ref,
            run_dir=run_dir,
            resolve_result=resolve_result,
            pdf_path=tmp_path,
            method=method,
            final_url=final_url,
            failures=failures,
            candidate_context=_candidate_context(candidate),
        )
        if ocr_stored is not None:
            ocr_stored["failures"] = failures
            return {
                "return": ocr_stored,
                "attempt": {
                    "outcome": "stored",
                    "reason": "fulltext stored via auto-OCR of scanned PDF",
                    "extract_method": ocr_stored.get("extract_method", "ocr"),
                    "corroborate_signal": ocr_stored.get("corroborate_signal"),
                    "corroborate_score": ocr_stored.get("corroborate_score"),
                },
            }

        if wrong_document_verdict:
            failures.append(
                _failure(
                    method,
                    final_url,
                    low_quality_identity.get("reason") or "downloaded PDF was not the cited work",
                    chars=len(text),
                    extract_method=extract_method,
                    corroborate_signal=low_quality_identity.get("corroborate_signal"),
                    corroborate_score=low_quality_identity.get("corroborate_score"),
                )
            )
            return {
                "return": {
                    "status": "wrong_document",
                    "method": extract_method,
                    "reason": low_quality_identity.get("reason")
                    or "downloaded PDF was not the cited work",
                    "pdf_url": url,
                    "content_type": content_type,
                    "identity_probe": low_quality_identity.get("identity_probe"),
                    "failures": failures,
                },
                "attempt": {
                    "outcome": "wrong_document",
                    "reason": low_quality_identity.get("reason"),
                    "chars": len(text),
                    "extract_method": extract_method,
                    "corroborate_signal": low_quality_identity.get("corroborate_signal"),
                    "corroborate_score": low_quality_identity.get("corroborate_score"),
                },
            }

        kept = deps.park_unreadable(
            run_dir,
            ref,
            origin=deps.origin_for_method(method, resolve_result),
            reason=(
                "unreadable_pdf: extracted text below quality threshold "
                f"(len={len(text)}); likely a scan without OCR"
            ),
            src_bytes=body,
            src_name=final_url,
        )
        ocr_outcome = (
            "ocr_needed" if _ocr_backends_available() else "ocr_backend_unavailable"
        )
        ocr_reason = (
            "unreadable_pdf: likely a scan — OCR backends are available; "
            "the PDF is parked in the OCR queue for processing."
            if ocr_outcome == "ocr_needed"
            else "unreadable_pdf: likely a scan — no OCR backends are installed. "
            "This is a retryable degradation: install OCR tools "
            "(pip install -r requirements-ocr.txt) and rerun."
        )
        return {
            "return": {
                "status": "quality_error",
                "method": extract_method,
                "reason": ocr_reason,
                "kept_pdf": kept,
                "pdf_url": url,
                "content_type": content_type,
                "failures": failures,
            },
            "attempt": {
                "outcome": ocr_outcome,
                "reason": "text below quality threshold",
                "chars": len(text),
                "extract_method": extract_method,
            },
        }

    stored = deps.try_store_fulltext(
        run_dir,
        ref,
        resolve_result,
        text,
        method=method,
        source_ref=final_url,
        extract_method=extract_method,
        content_version=candidate.get("content_version"),
        candidate_context=_candidate_context(candidate),
        identity_extract_text=identity_extract_text,
        identity_extract_method=identity_extract_method,
        extraction_flags=extraction_flags,
        redirect_chain=redirect_chain,
    )
    if stored["status"] == "stored":
        if extraction_flags:
            stored["extraction_flags"] = extraction_flags
        stored["failures"] = failures
        return {
            "return": stored,
            "attempt": {
                "outcome": "stored",
                "reason": "fulltext stored",
                "extract_method": extract_method,
                "corroborate_signal": stored.get("corroborate_signal"),
                "corroborate_score": stored.get("corroborate_score"),
            },
        }

    alternate, parser_variants = _retry_pdf_parser_variants(
        deps=deps, ref=ref, run_dir=run_dir, resolve_result=resolve_result,
        tmp_path=tmp_path, initial_text=text, initial_method=extract_method,
        method=method, final_url=final_url, candidate=candidate,
        redirect_chain=redirect_chain,
        identity_extract_prefix=identity_extract_prefix,
    )
    if alternate is not None:
        alternate["failures"] = failures
        return {
            "return": alternate,
            "attempt": {
                "outcome": "stored",
                "reason": "fulltext stored after alternate PDF parser confirmed identity",
                "extract_method": alternate.get("extract_method"),
                "identity_extract_method": alternate.get("identity_extract_method"),
                "parser_variants": parser_variants,
            },
        }

    stored = _aggregate_parser_identity(stored, parser_variants)

    failures.append(
        _failure(
            method,
            final_url,
            stored["reason"],
            corroborate_signal=stored.get("corroborate_signal"),
            corroborate_score=stored.get("corroborate_score"),
        )
    )
    ocr_stored = _maybe_retry_identity_with_ocr(
        deps=deps,
        ref=ref,
        run_dir=run_dir,
        resolve_result=resolve_result,
        pdf_path=tmp_path,
        method=method,
        final_url=final_url,
        failures=failures,
        original_text=text,
        extract_method=extract_method,
        content_version=candidate.get("content_version"),
        candidate_context=_candidate_context(candidate),
        redirect_chain=redirect_chain,
    )
    if ocr_stored is not None:
        ocr_stored["failures"] = failures
        return {
            "return": ocr_stored,
            "attempt": {
                "outcome": "stored",
                "reason": "fulltext stored after partial OCR confirmed document identity",
                "extract_method": ocr_stored.get("extract_method", extract_method),
                "identity_extract_method": ocr_stored.get("identity_extract_method"),
                "corroborate_signal": ocr_stored.get("corroborate_signal"),
                "corroborate_score": ocr_stored.get("corroborate_score"),
            },
        }

    if _suspicious_text_identity_fail(text, stored):
        kept = deps.park_unreadable(
            run_dir,
            ref,
            origin=deps.origin_for_method(method, resolve_result),
            reason=(
                "unreadable_pdf: extracted text passed the coarse quality gate "
                "but looks garbled after identity corroboration failed; "
                "likely a scan or corrupt encoding — queued for OCR"
            ),
            src_bytes=body,
            src_name=final_url,
        )
        ocr_outcome = (
            "ocr_needed" if _ocr_backends_available() else "ocr_backend_unavailable"
        )
        ocr_reason = (
            "unreadable_pdf: identity corroboration failed on "
            "suspicious extracted text — OCR backends are available; "
            "parked in the OCR queue for processing."
            if ocr_outcome == "ocr_needed"
            else "unreadable_pdf: identity corroboration failed on "
            "suspicious extracted text — no OCR backends are installed. "
            "This is a retryable degradation: install OCR tools "
            "(pip install -r requirements-ocr.txt) and rerun."
        )
        return {
            "return": {
                "status": "quality_error",
                "method": extract_method,
                "reason": ocr_reason,
                "kept_pdf": kept,
                "pdf_url": url,
                "content_type": content_type,
                "failures": failures,
            },
            "attempt": {
                "outcome": ocr_outcome,
                "reason": "text passed quality gate but looks garbled after identity fail",
                "chars": len(text),
                "extract_method": extract_method,
                "corroborate_signal": stored.get("corroborate_signal"),
                "corroborate_score": stored.get("corroborate_score"),
            },
        }
    return {
        "return": None,
        "attempt": {
            "outcome": stored.get("status") or "identity_inconclusive",
            "reason": stored.get("reason"),
            "extract_method": extract_method,
            "corroborate_signal": stored.get("corroborate_signal"),
            "corroborate_score": stored.get("corroborate_score"),
            "parser_variants": parser_variants,
        },
        "last_identity": stored,
    }


def _handle_document_candidate(
    *,
    deps: FetchPipelineDeps,
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    queue: list[dict],
    seen: set[str],
    failures: list[dict],
    candidate: dict,
    fetched: dict | None,
    tmp_path: str,
) -> dict:
    method = candidate["method"]
    url = candidate["url"]
    if not fetched:
        failures.append(_failure(method, url, "request failed"))
        return {"return": None, "attempt": {"outcome": "request_failed", "reason": "request failed"}}

    transport_failure = _transport_request_failure(method, url, fetched)
    if transport_failure is not None:
        failures.append(transport_failure["failure"])
        return {"return": None, "attempt": transport_failure["attempt"]}
    auth_unavailable = _provider_auth_unavailable(method, url, fetched)
    if auth_unavailable is not None:
        failures.append(auth_unavailable["failure"])
        return {"return": None, "attempt": auth_unavailable["attempt"]}

    final_url = fetched.get("url") or url
    status = fetched.get("status")
    content_type = fetched.get("content_type") or ""
    decoding_rejection = _content_decoding_rejection(
        method=method,
        final_url=final_url,
        fetched=fetched,
        failures=failures,
    )
    if decoding_rejection is not None:
        return decoding_rejection
    redirect_chain = fetched.get("redirect_chain")
    if _is_access_denied_status(status):
        html_text = deps.decode_textual_body(fetched.get("body") or b"", content_type)
        if html_text is not None:
            challenged = deps.is_challenge_html(html_text) or bool(
                callable(deps.is_challenge_url) and deps.is_challenge_url(final_url)
            )
            paywalled = deps.is_paywalled_html(html_text)
            if challenged or paywalled:
                landing = _maybe_handle_textual_response(
                    deps=deps,
                    ref=ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=queue,
                    seen=seen,
                    failures=failures,
                    method=method,
                    strategy=candidate.get("strategy") or method or "auto",
                    requested_url=url,
                    final_url=final_url,
                    html_text=html_text,
                    status=status,
                    content_version=candidate.get("content_version"),
                    candidate_context=_candidate_context(candidate),
                    fallback_stage=candidate.get("fallback_stage"),
                    redirect_chain=redirect_chain,
                )
                attempt_reason = landing.get("abstract_reason") or (landing.get("metadata_shell") or {}).get("reason")
                if attempt_reason is None and landing["trace_outcome"] == "challenge_blocked":
                    attempt_reason = "publisher challenge page blocked automated retrieval"
                attempt = {
                    "outcome": landing["trace_outcome"],
                    "reason": attempt_reason,
                    "html_content_kind": landing.get("content_kind"),
                    "has_pdf_link": (landing.get("html_profile") or {}).get("has_pdf_link"),
                    "shell_reason": (landing.get("html_profile") or {}).get("shell_reason"),
                    "shell_markers": (landing.get("html_profile") or {}).get("shell_markers"),
                    "page_chars": (landing.get("html_profile") or {}).get("page_chars"),
                    "abstract_chars": (landing.get("html_profile") or {}).get("abstract_chars"),
                }
                if landing["result"] is not None:
                    return {
                        "return": landing["result"], "attempt": attempt,
                        "landing_deferred": landing.get("landing_deferred"),
                    }
                return {
                    "return": None,
                    "attempt": attempt,
                    "paywalled": landing["paywalled"],
                    "abstract_entry": landing["abstract_entry"],
                    "abstract_reason": landing["abstract_reason"],
                    "abstract_metadata": landing.get("abstract_metadata"),
                    "abstract_method": landing.get("abstract_method"),
                    "last_identity": landing["last_identity"],
                    "metadata_shell": landing.get("metadata_shell"),
                    "landing_deferred": landing.get("landing_deferred"),
                }
        failures.append(
            _failure(
                method,
                final_url,
                "access denied",
                status=status,
                content_type=content_type or None,
            )
        )
        return {
            "return": None,
            "attempt": {"outcome": "access_denied", "reason": "access denied"},
        }
    body = fetched.get("body") or b""
    is_pdf_response = "pdf" in content_type.lower() or deps.looks_pdf_url(final_url)
    # A direct URL ending in .pdf can legitimately redirect to an HTML landing
    # page. Keep that established HTML path, but never send XML/error text to a
    # PDF parser merely because the URL looked like a PDF.
    if is_pdf_response and (
        "pdf" in content_type.lower() or not deps.body_looks_html(body, content_type)
    ):
        rejected = _reject_pdf_response(
            failures=failures, method=method, final_url=final_url,
            status=status, content_type=content_type, body=body,
        )
        if rejected is not None:
            return rejected
        text, extract_method, extraction_flags = _extract_pdf_text(
            deps,
            tmp_path,
            body,
            ref_id=ref.get("id"),
        )
        if not deps.text_ok(text):
            if _looks_meaningful_low_text(text) and _title_after_reference_marker(ref, resolve_result, text):
                failures.append(
                    _failure(
                        method,
                        final_url,
                        "expected title found only after references marker; downloaded PDF was not the cited work",
                        chars=len(text),
                        extract_method=extract_method,
                    )
                )
                return {
                    "return": {
                        "status": "wrong_document",
                        "method": extract_method,
                        "reason": "expected title found only after references marker; downloaded PDF was not the cited work",
                        "pdf_url": url,
                        "content_type": content_type,
                        "failures": failures,
                    },
                    "attempt": {
                        "outcome": "wrong_document",
                        "reason": "expected title found only after references marker; downloaded PDF was not the cited work",
                        "chars": len(text),
                        "extract_method": extract_method,
                    },
                }
            low_quality_identity = deps.try_store_fulltext(
                run_dir,
                ref,
                resolve_result,
                text,
                method=method,
                source_ref=final_url,
                extract_method=extract_method,
                content_version=candidate.get("content_version"),
                candidate_context=_candidate_context(candidate),
                identity_extract_method=extract_method,
                extraction_flags=extraction_flags,
                redirect_chain=redirect_chain,
            )
            wrong_document_verdict = (
                _low_quality_wrong_document(low_quality_identity, text)
                and _low_quality_verdict_earned()
            )
            failures.append(
                _failure(
                    method,
                    final_url,
                    f"text below quality threshold via {extract_method}",
                    chars=len(text),
                )
            )
            # Auto-OCR: try to recover a scanned PDF before giving up.  This runs
            # even when the identity probe above pointed at a different work: that
            # probe read text the quality gate had already rejected, so OCR of the
            # whole document is the stronger evidence and may overturn it.
            ocr_stored = _maybe_auto_ocr(
                deps=deps,
                ref=ref,
                run_dir=run_dir,
                resolve_result=resolve_result,
                pdf_path=tmp_path,
                method=method,
                final_url=final_url,
                failures=failures,
                candidate_context=_candidate_context(candidate),
            )
            if ocr_stored is not None:
                ocr_stored["failures"] = failures
                return {
                    "return": ocr_stored,
                    "attempt": {
                        "outcome": "stored",
                        "reason": "fulltext stored via auto-OCR of scanned PDF",
                        "extract_method": ocr_stored.get("extract_method", "ocr"),
                        "corroborate_signal": ocr_stored.get("corroborate_signal"),
                        "corroborate_score": ocr_stored.get("corroborate_score"),
                    },
                }
            if wrong_document_verdict:
                failures.append(
                    _failure(
                        method,
                        final_url,
                        low_quality_identity.get("reason") or "downloaded PDF was not the cited work",
                        chars=len(text),
                        extract_method=extract_method,
                        corroborate_signal=low_quality_identity.get("corroborate_signal"),
                        corroborate_score=low_quality_identity.get("corroborate_score"),
                    )
                )
                return {
                    "return": {
                        "status": "wrong_document",
                        "method": extract_method,
                        "reason": low_quality_identity.get("reason")
                        or "downloaded PDF was not the cited work",
                        "pdf_url": url,
                        "content_type": content_type,
                        "identity_probe": low_quality_identity.get("identity_probe"),
                        "failures": failures,
                    },
                    "attempt": {
                        "outcome": "wrong_document",
                        "reason": low_quality_identity.get("reason"),
                        "chars": len(text),
                        "extract_method": extract_method,
                        "corroborate_signal": low_quality_identity.get("corroborate_signal"),
                        "corroborate_score": low_quality_identity.get("corroborate_score"),
                    },
                }
            return {
                "return": None,
                "attempt": {
                    "outcome": "quality_below_threshold",
                    "reason": "text below quality threshold",
                    "chars": len(text),
                    "extract_method": extract_method,
                },
            }
        stored = deps.try_store_fulltext(
            run_dir,
            ref,
            resolve_result,
            text,
            method=method,
            source_ref=final_url,
            extract_method=extract_method,
            content_version=candidate.get("content_version"),
            candidate_context=_candidate_context(candidate),
            identity_extract_method=extract_method,
            extraction_flags=extraction_flags,
            redirect_chain=redirect_chain,
        )
        if stored["status"] == "stored":
            if extraction_flags:
                stored["extraction_flags"] = extraction_flags
            stored["failures"] = failures
            return {
                "return": stored,
                "attempt": {
                    "outcome": "stored",
                    "reason": "fulltext stored",
                    "extract_method": extract_method,
                    "corroborate_signal": stored.get("corroborate_signal"),
                    "corroborate_score": stored.get("corroborate_score"),
                },
            }
        alternate, parser_variants = _retry_pdf_parser_variants(
            deps=deps, ref=ref, run_dir=run_dir, resolve_result=resolve_result,
            tmp_path=tmp_path, initial_text=text, initial_method=extract_method,
            method=method, final_url=final_url, candidate=candidate,
            redirect_chain=redirect_chain,
        )
        if alternate is not None:
            alternate["failures"] = failures
            return {
                "return": alternate,
                "attempt": {
                    "outcome": "stored",
                    "reason": "fulltext stored after alternate PDF parser confirmed identity",
                    "extract_method": alternate.get("extract_method"),
                    "identity_extract_method": alternate.get("identity_extract_method"),
                    "parser_variants": parser_variants,
                },
            }
        stored = _aggregate_parser_identity(stored, parser_variants)
        failures.append(
            _failure(
                method,
                final_url,
                stored["reason"],
                corroborate_signal=stored.get("corroborate_signal"),
                corroborate_score=stored.get("corroborate_score"),
            )
        )
        ocr_stored = _maybe_retry_identity_with_ocr(
            deps=deps,
            ref=ref,
            run_dir=run_dir,
            resolve_result=resolve_result,
            pdf_path=tmp_path,
            method=method,
            final_url=final_url,
            failures=failures,
            original_text=text,
            extract_method=extract_method,
            content_version=candidate.get("content_version"),
            candidate_context=_candidate_context(candidate),
            redirect_chain=redirect_chain,
        )
        if ocr_stored is not None:
            ocr_stored["failures"] = failures
            return {
                "return": ocr_stored,
                "attempt": {
                    "outcome": "stored",
                    "reason": "fulltext stored after partial OCR confirmed document identity",
                    "extract_method": ocr_stored.get("extract_method", extract_method),
                    "identity_extract_method": ocr_stored.get("identity_extract_method"),
                    "corroborate_signal": ocr_stored.get("corroborate_signal"),
                    "corroborate_score": ocr_stored.get("corroborate_score"),
                },
            }
        if _suspicious_text_identity_fail(text, stored):
            body_bytes = fetched.get("body") or b"" if fetched else b""
            kept = deps.park_unreadable(
                run_dir,
                ref,
                origin=deps.origin_for_method(method, resolve_result),
                reason=(
                    "unreadable_pdf: extracted text passed the coarse quality gate "
                    "but looks garbled after identity corroboration failed; "
                    "likely a scan or corrupt encoding — queued for OCR"
                ),
                src_bytes=body_bytes,
                src_name=final_url,
            )
            ocr_outcome = (
                "ocr_needed" if _ocr_backends_available() else "ocr_backend_unavailable"
            )
            ocr_reason = (
                "unreadable_pdf: identity corroboration failed on "
                "suspicious extracted text — OCR backends are available; "
                "parked in the OCR queue for processing."
                if ocr_outcome == "ocr_needed"
                else "unreadable_pdf: identity corroboration failed on "
                "suspicious extracted text — no OCR backends are installed. "
                "This is a retryable degradation: install OCR tools "
                "(pip install -r requirements-ocr.txt) and rerun."
            )
            return {
                "return": {
                    "status": "quality_error",
                    "method": extract_method,
                    "reason": ocr_reason,
                    "kept_pdf": kept,
                    "pdf_url": url,
                    "content_type": content_type,
                    "failures": failures,
                },
                "attempt": {
                    "outcome": ocr_outcome,
                    "reason": "text passed quality gate but looks garbled after identity fail",
                    "chars": len(text),
                    "extract_method": extract_method,
                    "corroborate_signal": stored.get("corroborate_signal"),
                    "corroborate_score": stored.get("corroborate_score"),
                },
            }
        return {
            "return": None,
            "attempt": {
                "outcome": stored.get("status") or "identity_inconclusive",
                "reason": stored.get("reason"),
                "extract_method": extract_method,
                "corroborate_signal": stored.get("corroborate_signal"),
                "corroborate_score": stored.get("corroborate_score"),
                "parser_variants": parser_variants,
            },
            "last_identity": stored,
        }

    html_text = deps.decode_textual_body(fetched.get("body") or b"", content_type)
    if html_text is None:
        challenged = bool(
            callable(deps.is_challenge_url) and deps.is_challenge_url(final_url)
        )
        if challenged:
            if deps.remember_challenge_host is not None:
                deps.remember_challenge_host(
                    final_url,
                    profile="document",
                    strategy=candidate.get("strategy") or method or "auto",
                )
            reason = "publisher challenge page blocked automated retrieval"
            failures.append(
                _failure(
                    method,
                    final_url,
                    reason,
                    status=fetched.get("status"),
                    content_type=content_type or None,
                )
            )
            return {
                "return": None,
                "attempt": {"outcome": "challenge_blocked", "reason": reason},
            }
        failures.append(
            _failure(
                method,
                final_url,
                "unexpected non-text response",
                status=fetched.get("status"),
                content_type=content_type or None,
            )
        )
        return {
            "return": None,
            "attempt": {"outcome": "unexpected_non_text", "reason": "unexpected non-text response"},
        }

    landing = _maybe_handle_textual_response(
        deps=deps,
        ref=ref,
        run_dir=run_dir,
        resolve_result=resolve_result,
        queue=queue,
        seen=seen,
        failures=failures,
        method=method,
        strategy=candidate.get("strategy") or method or "auto",
        requested_url=url,
        final_url=final_url,
        html_text=html_text,
        status=fetched.get("status"),
        content_version=candidate.get("content_version"),
        candidate_context=_candidate_context(candidate),
        fallback_stage=candidate.get("fallback_stage"),
        redirect_chain=redirect_chain,
    )
    attempt_reason = landing.get("abstract_reason") or (landing.get("metadata_shell") or {}).get("reason")
    if attempt_reason is None and landing["trace_outcome"] == "challenge_blocked":
        attempt_reason = "publisher challenge page blocked automated retrieval"
    if attempt_reason is None:
        attempt_reason = (landing.get("last_identity") or {}).get("reason")
    attempt = {
        "outcome": landing["trace_outcome"],
        "reason": attempt_reason,
        "reason_code": (landing.get("last_identity") or {}).get("reason_code"),
        "html_content_kind": landing.get("content_kind"),
        "has_pdf_link": (landing.get("html_profile") or {}).get("has_pdf_link"),
        "shell_reason": (landing.get("html_profile") or {}).get("shell_reason"),
        "shell_markers": (landing.get("html_profile") or {}).get("shell_markers"),
        "page_chars": (landing.get("html_profile") or {}).get("page_chars"),
        "abstract_chars": (landing.get("html_profile") or {}).get("abstract_chars"),
    }
    if landing["result"] is not None:
        return {
            "return": landing["result"], "attempt": attempt,
            "landing_deferred": landing.get("landing_deferred"),
        }
    return {
        "return": None,
        "attempt": attempt,
        "paywalled": landing["paywalled"],
        "abstract_entry": landing["abstract_entry"],
        "abstract_reason": landing["abstract_reason"],
        "abstract_metadata": landing.get("abstract_metadata"),
        "abstract_method": landing.get("abstract_method"),
        "last_identity": landing["last_identity"],
        "metadata_shell": landing.get("metadata_shell"),
        "landing_deferred": landing.get("landing_deferred"),
    }


def process_queue(
    *,
    ref: dict,
    run_dir: str,
    resolve_result: dict,
    queue: list[dict],
    seen: set[str],
    tmp_path: str,
    max_fetch_candidates: int,
    candidate_workers: int,
    deps: FetchPipelineDeps,
    trace: dict | None = None,
    deadline: float | None = None,
    stage: str | None = None,
    persistence_hooks: FetchQueuePersistenceHooks | None = None,
    replay_frozen_candidate_ids: list[int] | None = None,
    allow_replay_queue_growth: bool = False,
) -> dict:
    if (
        persistence_hooks is not None
        and replay_frozen_candidate_ids is not None
        and not allow_replay_queue_growth
    ):
        raise ValueError("queue persistence hooks and replay IDs are mutually exclusive")
    if persistence_hooks is not None and stage is None:
        raise ValueError("stage is required with queue persistence hooks")

    frozen_candidate_ids: list[int] | None = None
    frozen_candidate_segments: list[tuple[int, int]] = []
    if replay_frozen_candidate_ids is not None:
        if (
            not isinstance(replay_frozen_candidate_ids, list)
            or len(replay_frozen_candidate_ids) != len(queue)
            or any(type(candidate_id) is not int or candidate_id <= 0 for candidate_id in replay_frozen_candidate_ids)
            or len(set(replay_frozen_candidate_ids)) != len(replay_frozen_candidate_ids)
        ):
            raise ValueError("invalid replay frozen candidate IDs")
        frozen_candidate_ids = list(replay_frozen_candidate_ids)

    def _scheduled_batch_index(
        queue_index: int,
        *,
        first_batch_index: int,
        batch_start_index: int,
        first_batch_capacity: int,
    ) -> int:
        first_batch_end = batch_start_index + first_batch_capacity
        if queue_index < first_batch_end:
            return first_batch_index
        return first_batch_index + 1 + (
            (queue_index - first_batch_end) // max(1, candidate_workers)
        )

    def _freeze_segment(
        start: int,
        end: int,
        *,
        first_batch_index: int,
        batch_start_index: int,
        first_batch_capacity: int,
    ) -> list[int]:
        assert persistence_hooks is not None
        records = []
        for queue_index in range(start, end):
            record = dict(queue[queue_index])
            record["queue_index"] = queue_index
            record["batch_index"] = _scheduled_batch_index(
                queue_index,
                first_batch_index=first_batch_index,
                batch_start_index=batch_start_index,
                first_batch_capacity=first_batch_capacity,
            )
            records.append(record)
        candidate_ids = persistence_hooks.freeze_stage_segment(stage, records)
        if (
            not isinstance(candidate_ids, list)
            or len(candidate_ids) != len(records)
            or any(type(candidate_id) is not int or candidate_id <= 0 for candidate_id in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
        ):
            raise ValueError("invalid frozen candidate IDs")
        return candidate_ids

    if persistence_hooks is not None and frozen_candidate_ids is None:
        frozen_candidate_ids = _freeze_segment(
            0,
            len(queue),
            first_batch_index=0,
            batch_start_index=0,
            first_batch_capacity=1,
        )
        frozen_candidate_segments.append((0, len(frozen_candidate_ids)))

    def _admit_batch_segments(start: int, end: int) -> None:
        assert persistence_hooks is not None
        assert frozen_candidate_ids is not None
        for segment_start, segment_end in frozen_candidate_segments:
            admitted_start = max(start, segment_start)
            admitted_end = min(end, segment_end)
            if admitted_start < admitted_end:
                persistence_hooks.admit_batch(
                    frozen_candidate_ids[admitted_start:admitted_end]
                )

    def _frozen_candidate_id(queue_index: int) -> int | None:
        if frozen_candidate_ids is None:
            return None
        if not 0 <= queue_index < len(frozen_candidate_ids):
            raise RuntimeError("frozen candidate ID alignment is invalid")
        candidate_id = frozen_candidate_ids[queue_index]
        if type(candidate_id) is not int or candidate_id <= 0:
            raise RuntimeError("frozen candidate ID alignment is invalid")
        return candidate_id

    failures = []
    abstract_entry = None
    abstract_reason = None
    abstract_metadata = None
    abstract_method = None
    last_identity = None
    metadata_shell = None
    earliest_deferred = None
    # Resolver OA status is declared metadata, not an observation made by this
    # fetch run. Only an encountered paywall page may produce this outcome.
    paywalled_seen = False

    idx = 0
    next_batch_index = 0
    next_batch_capacity = 1
    while idx < len(queue) and idx < max_fetch_candidates:
        batch_index = next_batch_index
        if deadline is not None and time.monotonic() > deadline:
            # Per-reference wall-clock budget expired mid-stage. Stop starting
            # new batches — a batch already in flight (network I/O runs inside
            # the worker pool below) is left to finish, but nothing further is
            # attempted. Record the untried tail so the trace explains the gap
            # rather than looking like a silent drop.
            remaining_end = min(len(queue), max_fetch_candidates)
            for offset, candidate in enumerate(queue[idx:remaining_end]):
                _trace_attempt(
                    trace,
                    queue_index=idx + offset,
                    batch_index=_scheduled_batch_index(
                        idx + offset,
                        first_batch_index=next_batch_index,
                        batch_start_index=idx,
                        first_batch_capacity=next_batch_capacity,
                    ),
                    candidate=candidate,
                    fetched=None,
                    outcome="deadline_exceeded",
                    reason="per-reference fetch deadline exceeded before this candidate could be attempted",
                    frozen_candidate_id=_frozen_candidate_id(idx + offset),
                )
            break
        batch_start = idx
        batch_size = next_batch_capacity
        batch_end = min(len(queue), max_fetch_candidates, idx + batch_size)
        batch = queue[batch_start:batch_end]
        if trace is not None:
            trace.setdefault("batches", []).append(
                {
                    "batch_index": batch_index,
                    "start": batch_start,
                    "end": batch_end,
                    "candidate_count": len(batch),
                    "candidate_urls": [item.get("url") for item in batch],
                }
            )
        if persistence_hooks is not None:
            _admit_batch_segments(batch_start, batch_end)
        prefetched = _prefetch_batch(deps, batch, candidate_workers)
        idx = batch_end
        next_batch_index = batch_index + 1
        next_batch_capacity = max(1, candidate_workers)
        for offset, item in enumerate(prefetched):
            candidate = item["candidate"]
            fetched = item["fetched"]
            queue_index = batch_start + offset
            # A received 429 is an audited physical request, but its retry is
            # governed by the limiter's authoritative deadline.  Keep the
            # candidate pending instead of letting the generic failure path
            # terminally classify it as download_error.
            if isinstance(fetched, dict) and fetched.get("status") == 429:
                host = _host_limiter.host_for_url(candidate.get("url"))
                until = _hosts._shared_host_limiter().cooldown_until(host)
                if (
                    isinstance(until, bool)
                    or not isinstance(until, (int, float))
                    or not math.isfinite(until)
                    or until <= 0
                ):
                    raise RuntimeError("HTTP 429 did not produce a finite host cooldown")
                _trace_attempt(
                    trace,
                    queue_index=queue_index,
                    batch_index=batch_index,
                    candidate=candidate,
                    fetched=fetched,
                    outcome="rate_limit_deferred",
                    reason="HTTP 429; candidate requeued at limiter cooldown expiry",
                    reason_code="rate_limit_response",
                    frozen_candidate_id=_frozen_candidate_id(queue_index),
                )
                deferred_result = {
                    "status": "rate_limit_deferred",
                    "method": candidate.get("method") or "auto",
                    "reason": "HTTP 429; candidate requeued at limiter cooldown expiry",
                    "deferred_host": host,
                    "deferred_until": until,
                }
                if earliest_deferred is None or until < earliest_deferred["deferred_until"]:
                    earliest_deferred = deferred_result
                continue
            if isinstance(fetched, dict) and fetched.get("fetch_deferred"):
                deferred_host = fetched.get("deferred_host")
                deferred_until = fetched.get("deferred_until")
                deferred_reason_code = fetched.get("deferred_reason_code", "host_cooldown")
                if (
                    not isinstance(deferred_host, str)
                    or not deferred_host
                    or not isinstance(deferred_until, (int, float))
                    or isinstance(deferred_until, bool)
                    or not math.isfinite(deferred_until)
                ):
                    raise ValueError("invalid fetch candidate admission deferral")
                if deferred_reason_code == "archive_circuit_open":
                    _trace_attempt(
                        trace, queue_index=queue_index, batch_index=batch_index,
                        candidate=candidate, fetched=None, outcome="skipped",
                        reason="Archive.org circuit is open for this Fetch run",
                        reason_code=deferred_reason_code,
                        frozen_candidate_id=_frozen_candidate_id(queue_index),
                    )
                    continue
                _trace_attempt(
                    trace,
                    queue_index=queue_index,
                    batch_index=batch_index,
                    candidate=candidate,
                    fetched=None,
                    outcome="rate_limit_deferred",
                    reason=(
                        "host cooldown deferred this candidate until "
                        f"{deferred_host} not-before {deferred_until:g}"
                    ),
                    reason_code="host_cooldown",
                    frozen_candidate_id=_frozen_candidate_id(queue_index),
                )
                deferred_result = {
                    "status": "rate_limit_deferred",
                    "method": candidate.get("method") or "auto",
                    "reason": "host cooldown deferred candidate admission",
                    "deferred_host": deferred_host,
                    "deferred_until": deferred_until,
                }
                if (
                    earliest_deferred is None
                    or deferred_until < earliest_deferred["deferred_until"]
                ):
                    earliest_deferred = deferred_result
                continue
            if isinstance(fetched, dict) and fetched.get("signed_url_expired"):
                expires_at = fetched.get("signed_url_expires_at") or "an unknown time"
                _trace_attempt(
                    trace,
                    queue_index=queue_index,
                    batch_index=batch_index,
                    candidate=candidate,
                    fetched=None,
                    outcome="skipped",
                    reason=f"signed URL expired at {expires_at}; request not sent",
                    reason_code="signed_url_expired",
                    frozen_candidate_id=_frozen_candidate_id(queue_index),
                )
                continue
            queue_length_before_handle = len(queue)
            kind = candidate.get("kind")
            if kind == "pdf":
                handled = _handle_pdf_candidate(
                    deps=deps,
                    ref=ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=queue,
                    seen=seen,
                    failures=failures,
                    candidate=candidate,
                    fetched=fetched,
                    tmp_path=tmp_path,
                )
            elif kind == "cran_archive":
                handled = _handle_cran_archive_candidate(
                    deps=deps,
                    ref=ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=queue,
                    seen=seen,
                    failures=failures,
                    candidate=candidate,
                    fetched=fetched,
                    tmp_path=tmp_path,
                )
            else:
                handled = _handle_document_candidate(
                    deps=deps,
                    ref=ref,
                    run_dir=run_dir,
                    resolve_result=resolve_result,
                    queue=queue,
                    seen=seen,
                    failures=failures,
                    candidate=candidate,
                    fetched=fetched,
                    tmp_path=tmp_path,
                )

            if len(queue) < queue_length_before_handle:
                raise RuntimeError("candidate handler removed queue entries")
            if persistence_hooks is not None and len(queue) > queue_length_before_handle:
                assert frozen_candidate_ids is not None
                segment_start = len(frozen_candidate_ids)
                frozen_candidate_ids.extend(_freeze_segment(
                    queue_length_before_handle,
                    len(queue),
                    first_batch_index=next_batch_index,
                    batch_start_index=idx,
                    first_batch_capacity=next_batch_capacity,
                ))
                frozen_candidate_segments.append((segment_start, len(frozen_candidate_ids)))
            if (
                replay_frozen_candidate_ids is not None
                and len(queue) != queue_length_before_handle
                and not allow_replay_queue_growth
            ):
                raise RuntimeError("frozen replay candidate attempted queue growth")
            _trace_attempt(
                trace,
                queue_index=queue_index,
                batch_index=batch_index,
                candidate=candidate,
                fetched=fetched,
                frozen_candidate_id=_frozen_candidate_id(queue_index),
                **handled.get("attempt", {}),
            )

            landing_deferred = handled.get("landing_deferred")
            if landing_deferred is not None:
                deferred_host = landing_deferred.get("deferred_host") if isinstance(landing_deferred, dict) else None
                deferred_until = landing_deferred.get("deferred_until") if isinstance(landing_deferred, dict) else None
                if (
                    not isinstance(deferred_host, str)
                    or not deferred_host
                    or not isinstance(deferred_until, (int, float))
                    or isinstance(deferred_until, bool)
                    or not math.isfinite(deferred_until)
                ):
                    raise ValueError("invalid provider landing admission deferral")
                deferred_result = {
                    "status": "rate_limit_deferred",
                    "method": candidate.get("method") or "auto",
                    "reason": "provider landing expansion admission deferred",
                    "deferred_host": deferred_host,
                    "deferred_until": deferred_until,
                }
                if (
                    earliest_deferred is None
                    or deferred_until < earliest_deferred["deferred_until"]
                ):
                    earliest_deferred = deferred_result

            paywalled_seen = paywalled_seen or bool(handled.get("paywalled"))
            if handled.get("abstract_entry") is not None:
                abstract_entry = handled["abstract_entry"]
                abstract_reason = handled.get("abstract_reason")
                abstract_metadata = handled.get("abstract_metadata")
                abstract_method = handled.get("abstract_method")
            if handled.get("last_identity") is not None:
                last_identity = handled["last_identity"]
            if handled.get("metadata_shell") is not None and metadata_shell is None:
                metadata_shell = handled["metadata_shell"]
            if handled.get("return") is not None:
                # The complete admitted batch was already fetched concurrently.  Do not
                # run tail handlers after an earlier candidate satisfies the request,
                # but retain the response facts for every request that was issued.
                for tail_offset, tail_item in enumerate(prefetched[offset + 1:], start=offset + 1):
                    tail_candidate = tail_item["candidate"]
                    tail_fetched = tail_item["fetched"]
                    tail_queue_index = batch_start + tail_offset
                    if isinstance(tail_fetched, dict) and tail_fetched.get("signed_url_expired"):
                        expires_at = tail_fetched.get("signed_url_expires_at") or "an unknown time"
                        _trace_attempt(
                            trace,
                            queue_index=tail_queue_index,
                            batch_index=batch_index,
                            candidate=tail_candidate,
                            fetched=None,
                            outcome="skipped",
                            reason=f"signed URL expired at {expires_at}; request not sent",
                            reason_code="signed_url_expired",
                            frozen_candidate_id=_frozen_candidate_id(tail_queue_index),
                        )
                    else:
                        _trace_attempt(
                            trace,
                            queue_index=tail_queue_index,
                            batch_index=batch_index,
                            candidate=tail_candidate,
                            fetched=tail_fetched,
                            outcome="prefetched_not_processed",
                            reason="an earlier candidate in the prefetched batch satisfied the fetch",
                            frozen_candidate_id=_frozen_candidate_id(tail_queue_index),
                        )
                return handled["return"]
    if earliest_deferred is not None:
        earliest_deferred["failures"] = failures
        return earliest_deferred
    if abstract_entry:
        result = {
            "status": _abstract_fetch_status(resolve_result),
            "stored_as": abstract_entry.get("stored_as"),
            # Name the candidate the abstract actually came from. "landing" is the
            # right word for a publisher page read live, but a fallback stage reads
            # something else entirely — an archived snapshot, say — and a report that
            # says "landing" there misstates what was verified. Falls back to the
            # generic label only when no candidate recorded its method.
            "method": abstract_method or "landing",
            "reason": abstract_reason or "only abstract was deterministically reachable",
            "failures": failures,
        }
        for key, value in (abstract_metadata or {}).items():
            if value not in (None, [], ""):
                result[key] = value
        if paywalled_seen:
            result["paywalled"] = True
        return result
    if last_identity:
        last_identity["failures"] = failures
        return last_identity
    if metadata_shell:
        return {
            "status": "metadata_only",
            "method": metadata_shell.get("method") or "landing",
            "reason": metadata_shell.get("reason")
            or "only a metadata landing page was deterministically reachable",
            "url": metadata_shell.get("url"),
            "has_pdf_link": metadata_shell.get("has_pdf_link"),
            "shell_markers": metadata_shell.get("shell_markers"),
            "failures": failures,
        }
    if paywalled_seen:
        return {
            "status": _abstract_fetch_status(resolve_result) if resolve_result.get("abstract") else "skipped",
            "method": "landing",
            "reason": "full text appears paywalled; no open-access full text was retrievable",
            "failures": failures,
        }
    return {
        "status": "download_error" if failures else "not_found",
        "method": "auto",
        "reason": "no candidate yielded a corroborated full text",
        "failures": failures,
    }
