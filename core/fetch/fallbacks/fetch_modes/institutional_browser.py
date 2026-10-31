#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/institutional_browser.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded publisher recovery using an explicitly selected institution profile."""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

from core.fetch import institutional_access
from core.fetch.extraction import fetch_html
from core.fetch.fallbacks.fetch_modes import interactive_browser, interactive_fallback

NAME = "institutional_browser"
MODE_NAME = "institutional"
USES_RESOLVE_MAP = True
CLOSED_ACCESS_MODE = True

MAX_STEPS_PER_REFERENCE = 6
MAX_URLS_PER_REFERENCE = 2
NAVIGATION_TIMEOUT_MS = 15_000
_CLICK_TIMEOUT_MS = 1_500
_AUTH_SURFACE_TIMEOUT_MS = 2_500
_PDF_FETCH_TIMEOUT_MS = 5_000
_MAX_PDF_FETCH_BYTES = 25 * 1024 * 1024

_FETCH_EXPLICIT_PDF_JS = """async ({url, maxBytes, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            credentials: 'include',
            signal: controller.signal,
        });
        const contentType = response.headers.get('content-type') || '';
        const contentRange = response.headers.get('content-range') || '';
        const metadata = {
            status: response.status,
            final_url: response.url,
            content_type: contentType,
            content_range: contentRange,
        };
        if (response.status !== 200 || contentRange ||
            contentType.split(';', 1)[0].trim().toLowerCase() !== 'application/pdf' ||
            !response.body) return metadata;

        const reader = response.body.getReader();
        const chunks = [];
        let size = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > maxBytes) {
                await reader.cancel();
                return {...metadata, too_large: true};
            }
            chunks.push(value);
        }
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
        }
        if (bytes.length < 4 || bytes[0] !== 0x25 || bytes[1] !== 0x50 ||
            bytes[2] !== 0x44 || bytes[3] !== 0x46) return metadata;
        let binary = '';
        for (let start = 0; start < bytes.length; start += 0x8000) {
            binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
        }
        return {...metadata, body_base64: btoa(binary)};
    } catch (_) {
        return null;
    } finally {
        clearTimeout(timer);
    }
}"""

_INTERACTIVE_ENTRY_SELECTOR = "a,button,[role='link'],[role='button']"
_INSTITUTION_SELECTOR = (
    "a,button,[role='link'],[role='button'],[role='option'],[role='radio'],option,label"
)
_INSTITUTION_SEARCH_SELECTOR = (
    "input[type='search'],input[type='text'],input:not([type]),[role='searchbox']"
)
_INSTITUTION_SEARCH_LABELS = (
    "Find your institution",
    "Search for your institution",
    "Institution name",
)
_EXACT_INSTITUTION_OPTION_JS = """({selector, label}) => {
    const normalize = value => String(value || '').normalize('NFKC')
        .toLocaleLowerCase().replace(/\\s+/g, ' ').trim();
    return Array.from(document.querySelectorAll(selector)).some(element => {
        const style = window.getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden' ||
            element.getClientRects().length === 0 ||
            element.closest('[hidden], [aria-hidden="true"]')) return false;
        return [element.innerText, element.textContent,
            element.getAttribute('aria-label'), element.getAttribute('label'),
            element.value, element.getAttribute('placeholder')]
            .some(value => normalize(value) === label);
    });
}"""

# Selectors are scoped to the tested host surfaces and do not establish that
# any particular account is entitled to a resource.
_PLATFORM_ADAPTERS = {
    "sciencedirect.com": {
        "name": "ScienceDirect",
        "entry_labels": (
            "Access through your institution",
            "Sign in via your institution",
            "Sign in through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
    "www.sciencedirect.com": {
        "name": "ScienceDirect",
        "entry_labels": (
            "Access through your institution",
            "Sign in via your institution",
            "Sign in through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
    "link.springer.com": {
        "name": "SpringerLink",
        "entry_labels": (
            "Log in via an institution",
            "Sign in via an institution",
            "Access through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
    "www.nature.com": {
        "name": "Nature",
        "entry_labels": (
            "Log in via an institution",
            "Sign in via an institution",
            "Access through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
    "onlinelibrary.wiley.com": {
        "name": "Wiley Online Library",
        "entry_labels": (
            "Institutional login",
            "Log in through your institution",
            "Access through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
    "www.onlinelibrary.wiley.com": {
        "name": "Wiley Online Library",
        "entry_labels": (
            "Institutional login",
            "Log in through your institution",
            "Access through your institution",
        ),
        "pdf_labels": ("Download PDF", "View PDF"),
    },
}

_LOGIN_GATE_PHRASES = (
    "please sign in to continue",
    "please log in to continue",
    "sign in to continue",
    "log in to continue",
    "sign in to access",
    "log in to access",
    "you must sign in",
    "you must log in",
    "access through your institution",
    "access via your institution",
    "sign in via your institution",
    "log in via your institution",
    "institutional login",
    "authentication required",
    "please authenticate",
)
_AUTH_URL_SEGMENT = re.compile(
    r"(?:^|/)(?:login|log-in|signin|sign-in|sso|auth|authentication|institutional-access)(?:/|$)",
    re.IGNORECASE,
)
_AUTH_GATE_CLASS = re.compile(
    r"(?:paywall|access[-_ ]?(?:denied|required|gate)|login[-_ ]?required|"
    r"authentication[-_ ]?required|institutional[-_ ]?access)",
    re.IGNORECASE,
)
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)


class _VisibleGateParser(HTMLParser):
    """Collect visible gate text while ignoring chrome and hidden content."""

    _EXCLUDED_TAGS = {"head", "script", "style", "template", "nav", "header", "footer"}
    _EXCLUDED_ROLES = {"navigation", "banner", "contentinfo"}
    _EXCLUDED_CHROME_CLASSES = {
        "header", "footer", "nav", "navigation", "site-header", "global-header",
        "site-footer", "global-footer", "site-nav", "global-nav", "masthead",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self._visible_text = []
        self._gate_text = []
        self._has_password_form = False

    def handle_starttag(self, tag, attrs):
        attributes = {str(name).lower(): str(value or "") for name, value in attrs}
        parent_hidden = self._stack[-1]["hidden"] if self._stack else False
        parent_excluded = self._stack[-1]["excluded"] if self._stack else False
        parent_gate = self._stack[-1]["gate"] if self._stack else False
        hidden = (
            parent_hidden
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or bool(_HIDDEN_STYLE.search(attributes.get("style", "")))
        )
        chrome_classes = set(
            f"{attributes.get('class', '')} {attributes.get('id', '')}".casefold().split()
        )
        excluded = parent_excluded or tag in self._EXCLUDED_TAGS or (
            attributes.get("role", "").casefold() in self._EXCLUDED_ROLES
        ) or bool(chrome_classes & self._EXCLUDED_CHROME_CLASSES)
        class_id = f"{attributes.get('class', '')} {attributes.get('id', '')}"
        gate = parent_gate or bool(_AUTH_GATE_CLASS.search(class_id))
        in_form = tag == "form" or bool(self._stack and self._stack[-1]["in_form"])
        if (
            not hidden
            and in_form
            and tag == "input"
            and attributes.get("type", "").casefold() == "password"
        ):
            self._has_password_form = True
        self._stack.append({
            "tag": tag,
            "hidden": hidden,
            "excluded": excluded,
            "gate": gate,
            "in_form": in_form,
        })

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if not data.strip() or not self._stack:
            return
        context = self._stack[-1]
        if context["hidden"] or context["excluded"]:
            return
        normalized = _normalize_label(data)
        self._visible_text.append(normalized)
        if context["gate"]:
            self._gate_text.append(normalized)

    def is_login_gate(self) -> bool:
        if self._has_password_form:
            return True
        visible_text = " ".join(self._visible_text)
        gate_text = " ".join(self._gate_text)
        return any(phrase in visible_text or phrase in gate_text for phrase in _LOGIN_GATE_PHRASES)


def groups(need, refs, fetch_results, *, resolve_map=None, include_closed=True):
    """Reuse the evidenced-route grouping, always including closed resources."""
    return interactive_fallback.groups(
        need,
        refs,
        fetch_results,
        resolve_map=resolve_map,
        include_closed=True,
    )


def _normalize_label(value) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _adapter_for_url(url: str):
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return None
    adapter = _PLATFORM_ADAPTERS.get(host)
    return dict(adapter) if adapter is not None else None


def _safe_pdf_href(base_url: str, href) -> str | None:
    """Resolve only an explicit, credential-free HTTP(S) anchor destination."""
    if not isinstance(href, str) or not href or href != href.strip():
        return None
    if any(char.isspace() or ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in href):
        return None
    try:
        resolved = urljoin(str(base_url or ""), href)
        parsed = urlparse(resolved)
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in parsed.netloc)
    ):
        return None
    return resolved


def _page_document_url(page) -> str | None:
    """Return the current document URL only when it is a safe HTTP(S) URL."""
    try:
        document_url = page.url
    except Exception:
        return None
    if not isinstance(document_url, str):
        return None
    if _safe_pdf_href("", document_url) != document_url:
        return None
    return document_url


def _unique_pdf_href(page, base_url: str, adapter) -> str | None:
    """Return one exact visible adapter PDF link, failing closed on ambiguity."""
    if adapter is None:
        return None
    matches = _matching_elements(page, "a", adapter.get("pdf_labels") or ())
    if len(matches) != 1:
        return None
    get_attribute = getattr(matches[0], "get_attribute", None)
    if not callable(get_attribute):
        return None
    try:
        href = get_attribute("href")
    except Exception:
        return None
    return _safe_pdf_href(base_url, href)


def _fetch_explicit_pdf(session, pdf_href: str, ref_id: str, index: int, requested_url: str):
    """Read an explicit PDF through the authenticated page before viewer navigation."""
    evaluate = getattr(getattr(session, "page", None), "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        response = evaluate(
            _FETCH_EXPLICIT_PDF_JS,
            {
                "url": pdf_href,
                "maxBytes": _MAX_PDF_FETCH_BYTES,
                "timeoutMs": _PDF_FETCH_TIMEOUT_MS,
            },
        )
    except Exception:
        return None
    if not isinstance(response, dict):
        return None
    final_url = response.get("final_url")
    if (
        response.get("status") != 200
        or response.get("content_range")
        or response.get("too_large")
        or _safe_pdf_href(pdf_href, final_url) != final_url
        or str(response.get("content_type") or "").split(";", 1)[0].strip().casefold()
        != "application/pdf"
    ):
        return None
    body_base64 = response.get("body_base64")
    if (
        not isinstance(body_base64, str)
        or len(body_base64) > ((_MAX_PDF_FETCH_BYTES + 2) // 3) * 4
    ):
        return None
    try:
        body = base64.b64decode(body_base64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not body.startswith(b"%PDF") or len(body) > _MAX_PDF_FETCH_BYTES:
        return None
    artifact_dir = getattr(session, "artifacts_dir", None)
    if artifact_dir is None:
        run_dir = getattr(session, "run_dir", None)
        if run_dir is None:
            return None
        artifact_dir = Path(run_dir) / "browser_interactive_artifacts"
    try:
        artifact_path = interactive_browser._write_artifact(
            Path(artifact_dir), ref_id, index, ".pdf", body
        )
    except Exception:
        return None
    return {
        "ref_id": ref_id,
        "capture_index": index,
        "artifact_path": artifact_path,
        "url": final_url,
        "requested_url": requested_url,
        "status": 200,
        "content_type": str(response.get("content_type") or "application/pdf"),
        "body": body,
    }


def _element_labels(element, *, attributes=("aria-label", "label", "value", "placeholder")) -> set[str]:
    values = set()
    for method_name in ("inner_text", "text_content"):
        method = getattr(element, method_name, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                value = None
            if value and _normalize_label(value):
                values.add(_normalize_label(value))
                break
    get_attribute = getattr(element, "get_attribute", None)
    if callable(get_attribute):
        for name in attributes:
            try:
                value = get_attribute(name)
            except Exception:
                value = None
            if value and _normalize_label(value):
                values.add(_normalize_label(value))
    return values


def _matching_elements(page, selector: str, labels) -> list:
    expected = {_normalize_label(label) for label in labels if _normalize_label(label)}
    if not expected:
        return []
    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory):
        return []
    try:
        elements = locator_factory(selector)
        count = int(elements.count())
    except Exception:
        return []
    matches = []
    for index in range(count):
        try:
            element = elements.nth(index)
            visible = getattr(element, "is_visible", None)
            if callable(visible) and not visible():
                continue
            if _element_labels(element) & expected:
                matches.append(element)
        except Exception:
            continue
    return matches


def _matching_institution_search_fields(page) -> list:
    expected = {_normalize_label(label) for label in _INSTITUTION_SEARCH_LABELS}
    locator_factory = getattr(page, "locator", None)
    if callable(locator_factory):
        try:
            elements = locator_factory(_INSTITUTION_SEARCH_SELECTOR)
            count = int(elements.count())
        except Exception:
            count = 0
            elements = None
        matches = []
        for index in range(count):
            try:
                element = elements.nth(index)
                visible = getattr(element, "is_visible", None)
                if callable(visible) and not visible():
                    continue
                get_attribute = getattr(element, "get_attribute", None)
                if callable(get_attribute):
                    try:
                        input_type = (get_attribute("type") or "").casefold()
                    except Exception:
                        input_type = ""
                    if input_type == "password":
                        continue
                if _element_labels(
                    element, attributes=("aria-label", "placeholder")
                ) & expected:
                    matches.append(element)
            except Exception:
                continue
        if matches:
            return matches

    # A real associated <label> is exposed through Playwright's exact-label
    # locator even when the input has no aria-label or placeholder attribute.
    get_by_label = getattr(page, "get_by_label", None)
    if callable(get_by_label):
        for label in _INSTITUTION_SEARCH_LABELS:
            try:
                matches = get_by_label(label, exact=True)
                count = int(matches.count())
            except Exception:
                continue
            if count:
                return [matches.nth(index) for index in range(count)]
    return []


def _fill_institution_search(page, institution: str, *, step_budget=None) -> str:
    matches = _matching_institution_search_fields(page)
    if not matches:
        return "not_found"
    if len(matches) != 1:
        return "ambiguous"
    if step_budget is not None:
        if step_budget[0] <= 0:
            return "limit"
        step_budget[0] -= 1
    try:
        matches[0].fill(institution, timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        return "failed"
    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        return "not_found"
    try:
        wait_for_function(
            _EXACT_INSTITUTION_OPTION_JS,
            arg={
                "selector": _INSTITUTION_SELECTOR,
                "label": _normalize_label(institution),
            },
            timeout=_AUTH_SURFACE_TIMEOUT_MS,
        )
    except Exception:
        return "not_found"
    return "selected"


def _click_unique_label(page, labels, *, institution=False, step_budget=None) -> str:
    selector = _INSTITUTION_SELECTOR if institution else _INTERACTIVE_ENTRY_SELECTOR
    matches = _matching_elements(page, selector, labels)
    if not matches:
        return "not_found"
    if len(matches) != 1:
        return "ambiguous"
    if step_budget is not None:
        if step_budget[0] <= 0:
            return "limit"
        step_budget[0] -= 1
    try:
        matches[0].click(timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        return "failed"
    return "selected"


def _wait_for_surface(page) -> None:
    wait = getattr(page, "wait_for_load_state", None)
    if callable(wait):
        try:
            wait("domcontentloaded", timeout=_AUTH_SURFACE_TIMEOUT_MS)
        except Exception:
            pass


def _page_state(page, status=None) -> str | None:
    """Classify only visible challenge/login evidence and actual HTTP failures."""
    try:
        page_url = str(page.url or "")
        markup = str(page.content() or "")
    except Exception:
        return "access_unavailable"
    if fetch_html.is_challenge_url(page_url) or fetch_html.is_challenge_html(markup):
        return "challenge"
    try:
        parsed_url = urlparse(page_url)
        auth_host = (parsed_url.hostname or "").casefold().split(".", 1)[0]
        auth_route = bool(_AUTH_URL_SEGMENT.search(parsed_url.path or ""))
    except ValueError:
        auth_host = ""
        auth_route = False
    if auth_route or auth_host in {"login", "auth", "sso", "signin"}:
        return "login_required"
    try:
        gate_parser = _VisibleGateParser()
        gate_parser.feed(markup)
    except Exception:
        return "access_unavailable"
    if gate_parser.is_login_gate():
        return "login_required"
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None
    if status_code is not None and status_code >= 400:
        # A generic 401/403 establishes unavailable access, never expiry.
        return "access_unavailable"
    return None


def _attempt_institutional_access(page, url: str, institution: str, *, step_budget=None) -> str:
    """Select one tested publisher route and the exact configured institution."""
    document_url = _page_document_url(page)
    adapter = _adapter_for_url(document_url) if document_url else None
    if adapter is None:
        return "unsupported"
    expected_institution = _normalize_label(institution)
    if not expected_institution:
        return "login_required"

    institution_result = _click_unique_label(
        page, (institution,), institution=True, step_budget=step_budget
    )
    if institution_result in {"ambiguous", "limit"}:
        return "unsupported"
    if institution_result == "not_found":
        entry_result = _click_unique_label(
            page, adapter["entry_labels"], step_budget=step_budget
        )
        if entry_result in {"ambiguous", "failed", "limit"}:
            return "unsupported"
        if entry_result == "not_found":
            return "unsupported"
        _wait_for_surface(page)
        institution_result = _click_unique_label(
            page, (institution,), institution=True, step_budget=step_budget
        )
        if institution_result == "not_found":
            search_result = _fill_institution_search(
                page, institution, step_budget=step_budget
            )
            if search_result in {"ambiguous", "failed", "limit"}:
                return "unsupported"
            if search_result == "selected":
                institution_result = _click_unique_label(
                    page, (institution,), institution=True, step_budget=step_budget
                )
    if institution_result in {"ambiguous", "limit"}:
        return "unsupported"
    if institution_result != "selected":
        return "unsupported"
    _wait_for_surface(page)
    return "selected"


def _capture_if_available(
    session, ref_id: str, url: str, index: int, navigation, *, step_budget=None
):
    pdf = session.capture_current_pdf(ref_id, index)
    if pdf is not None:
        return {
            "ref_id": ref_id,
            "found": True,
            **pdf,
            "requested_url": url,
        }

    # Pass the actual response metadata to the standard validator. The sync
    # session updates this response when a publisher navigation completes.
    response = getattr(session, "_last_response", None)
    status = navigation.get("status") if isinstance(navigation, dict) else None
    response_status = getattr(response, "status", None) if response is not None else None
    if response_status is not None:
        status = response_status
    state = _page_state(session.page, status)
    if state is not None:
        return state

    if response is None and status is not None:
        response = SimpleNamespace(
            status=status,
            headers={
                "content-type": (
                    navigation.get("content_type") or "text/html"
                    if isinstance(navigation, dict) else "text/html"
                )
            },
        )
    artifact = session.capture_current_html(ref_id, index, response=response)
    if artifact is None:
        return _page_state(session.page, status) or "access_unavailable"
    html_candidate = {
        "ref_id": ref_id,
        "found": True,
        **artifact,
        "requested_url": url,
    }

    document_url = _page_document_url(session.page)
    adapter = _adapter_for_url(document_url) if document_url else None
    pdf_href = _unique_pdf_href(session.page, document_url or "", adapter)
    if pdf_href and step_budget is not None and step_budget[0] > 0:
        # Try the explicit link in the authenticated page first. Some browsers
        # expose a viewer document, rather than the PDF bytes, after navigation.
        step_budget[0] -= 1
        pdf = _fetch_explicit_pdf(session, pdf_href, ref_id, index, url)
        if pdf is not None:
            return {
                "ref_id": ref_id,
                "found": True,
                **pdf,
                "requested_url": url,
            }
        # Keep the normal browser download/response path when there is budget.
        # The original HTML candidate remains available if that path also fails.
        if step_budget[0] <= 0:
            return html_candidate
        step_budget[0] -= 1
        try:
            session.open(pdf_href)
            pdf = session.capture_current_pdf(ref_id, index)
        except Exception:
            pdf = None
        if pdf is not None:
            return {
                "ref_id": ref_id,
                "found": True,
                **pdf,
                "requested_url": url,
            }
    return html_candidate


def _state_after_navigation(
    session, ref_id, url, index, navigation, institution, *, step_budget=None
):
    result = _capture_if_available(
        session, ref_id, url, index, navigation, step_budget=step_budget
    )
    if isinstance(result, dict):
        return result, "captured"
    if result != "login_required":
        return None, result

    action = _attempt_institutional_access(
        session.page, url, institution, step_budget=step_budget
    )
    if action != "selected":
        return None, action
    # Re-open the original article to obtain a fresh authenticated response.
    # Do not reuse response metadata from the pre-selection access denial.
    if step_budget is not None:
        if step_budget[0] <= 0:
            return None, "access_unavailable"
        step_budget[0] -= 1
    try:
        navigation = session.open(url)
    except Exception:
        return None, "access_unavailable"
    response = getattr(session, "_last_response", None)
    status = navigation.get("status") if isinstance(navigation, dict) else None
    response_status = getattr(response, "status", None) if response is not None else None
    if response_status is not None:
        status = response_status
    if response is None and status is None:
        return None, "access_unavailable"
    # Login forms, MFA prompts, and challenges are returned for operator review.
    state = _page_state(session.page, status)
    if state is not None:
        return None, state
    result = _capture_if_available(
        session,
        ref_id,
        url,
        index,
        navigation,
        step_budget=step_budget,
    )
    if isinstance(result, dict):
        return result, "captured"
    return None, result


def _domain(value: str) -> str | None:
    try:
        host = (urlparse(str(value)).hostname or "").lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def _append_state(states: dict, ref_id: str, state: str, domain: str | None) -> None:
    key = (ref_id, domain or "")
    rank = {
        "access_unavailable": 0,
        "login_required": 1,
        "unsupported": 2,
        "challenge": 3,
        "captured": 4,
    }
    current = states.get(key)
    if current is None or rank.get(state, -1) > rank.get(current["state"], -1):
        states[key] = {"ref_id": ref_id, "state": state, "domain": domain}


def recover(groups, run_dir: str) -> dict:
    """Try stored institutional access with bounded, non-interactive recovery."""
    settings = institutional_access.config()
    references = {}
    for group in groups or []:
        for reference in group.get("references") or []:
            if not isinstance(reference, dict) or not reference.get("ref_id"):
                continue
            ref_id = str(reference["ref_id"])
            entry = references.setdefault(
                ref_id, {"domains": [], "candidate_urls": []}
            )
            domain = group.get("domain")
            if domain and domain not in entry["domains"]:
                entry["domains"].append(domain)
            candidates = reference.get("candidate_urls") or []
            if not isinstance(candidates, (list, tuple)):
                continue
            for candidate in candidates:
                if candidate and candidate not in entry["candidate_urls"]:
                    if len(entry["candidate_urls"]) >= MAX_URLS_PER_REFERENCE:
                        break
                    entry["candidate_urls"].append(candidate)
    if not settings["enabled"]:
        return {
            "available": False,
            "reason_code": "institutional_access_disabled",
            "reason": "institutional browser recovery is not enabled",
            "items": [],
            "access_states": [],
        }
    if not references:
        return {
            "available": True,
            "reason_code": None,
            "reason": None,
            "items": [],
            "access_states": [],
        }

    session = interactive_browser.InteractiveBrowserSession(
        run_dir,
        profile_id=settings["profile"],
    )
    started = session.start()
    if not started["available"]:
        states = {}
        for ref_id, reference in references.items():
            for domain in reference["domains"] or [None]:
                _append_state(states, ref_id, "access_unavailable", domain)
        return {**started, "items": [], "access_states": list(states.values())}

    items = []
    states = {}
    try:
        session.page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        for ref_id, reference in references.items():
            urls = reference["candidate_urls"]
            step_budget = [MAX_STEPS_PER_REFERENCE]
            captured_for_reference = False
            last_state = "access_unavailable"
            for index, url in enumerate(urls):
                if step_budget[0] <= 0:
                    break
                try:
                    step_budget[0] -= 1
                    navigation = session.open(url)
                    result, state = _state_after_navigation(
                        session,
                        ref_id,
                        url,
                        index,
                        navigation,
                        settings["institution"],
                        step_budget=step_budget,
                    )
                except Exception:
                    result, state = None, "access_unavailable"
                if result is not None:
                    items.append(result)
                    captured_for_reference = True
                    state = "captured"
                last_state = state
                _append_state(
                    states,
                    ref_id,
                    state,
                    _domain(url) or (reference["domains"][0] if reference["domains"] else None),
                )
            if not urls:
                last_state = "unsupported"
                for domain in reference["domains"] or [None]:
                    _append_state(states, ref_id, last_state, domain)
            if not captured_for_reference:
                items.append({"ref_id": ref_id, "found": False})
    finally:
        session.close()

    return {
        "available": True,
        "reason_code": None,
        "reason": None,
        "items": items,
        "access_states": list(states.values()),
    }
