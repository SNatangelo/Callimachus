#!/usr/bin/env python3
# core/resolve/providers/scholar_archive.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Internet Archive Scholar (scholar.archive.org) resolver.

A last-resort, optional-stage resolver for grey literature and journals that are
not indexed by Crossref/OpenAlex/Europe PMC — e.g. small society journals, trade
publications, meeting proceedings archived only as web PDFs.  IA Scholar (backed
by fatcat) surfaces those works with an archived full-text copy served through a
``/work/<ident>/access/<type>/<original-url>`` link, which the fetch stage can
download (and its landing identity probe still verifies independently).

There is no public JSON search API: discovery scrapes the HTML result list.  The
match is held strict — a normalized-title identity or a high title overlap with
author corroboration — so a same-keyword neighbour is never adopted.
"""

from __future__ import annotations

import html
import re
import threading
import urllib.parse

NAME = "scholar_archive_search"
MANIFEST = {}
# Deliberately NOT an optional-stage resolver. The optional stage adopts the
# highest-scoring candidate, which would let an archived grey-literature copy
# override an authoritative index match. `core.resolve` instead invokes this
# provider through a dedicated last-resort gate that fires only when nothing
# else resolved the work, so it can add a result but never displace one.
OPTIONAL_STAGE = False

_SEARCH_URL = "https://scholar.archive.org/search"

# A result item: the title anchor (its href is the archived-copy access path),
# an optional release-stage span, then the author line.
_ITEM_RE = re.compile(
    r'<div class="result-title"><a href="(?P<href>/work/[^"]+)">(?P<title>.*?)</a></div>'
    r'(?:\s*<span class="release-stage">(?P<stage>[^<]*)</span>)?'
    r'\s*<div class="result-authors">(?P<authors>[^<]*)',
    re.S,
)
_ACCESS_RE = re.compile(r"^/work/(?P<ident>[a-z0-9]+)/access/(?P<atype>[a-z_]+)/(?P<url>.+)$")

# scholar.archive.org increasingly gates non-browser clients behind a JavaScript
# "Session Verification" interstitial served with HTTP 200 but no result list.
# These markers identify that page so it is treated as a transient block rather
# than a genuine empty result.
_CHALLENGE_MARKERS = (
    "session verification",
    "checking your browser",
    "enable javascript",
    "verifying you are human",
    "verifying your browser",
    "just a moment",
)
_LOCAL = threading.local()


class CircuitSession:
    """Paper-local single-flight guard for a deterministic anti-bot page."""

    def __init__(self):
        self._condition = threading.Condition()
        self._state = "unknown"
        self._waiters = 0

    def before(self) -> tuple[bool, bool]:
        """Return ``(is_probe_owner, must_skip)`` for the current request."""
        with self._condition:
            if self._state in {"closed", "open"}:
                return False, True
            if self._state == "unknown":
                self._state = "probing"
                return True, False
            if self._state == "available":
                return False, False
            self._waiters += 1
            self._condition.notify_all()
            try:
                while self._state == "probing":
                    self._condition.wait()
                return False, self._state in {"closed", "open"}
            finally:
                self._waiters -= 1
                self._condition.notify_all()

    def complete_probe(self, challenge: bool) -> None:
        with self._condition:
            if self._state == "probing":
                self._state = "open" if challenge else "available"
            self._condition.notify_all()

    def open_circuit(self) -> None:
        with self._condition:
            if self._state != "closed":
                self._state = "open"
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._state = "closed"
            self._condition.notify_all()


def new_circuit_session() -> CircuitSession:
    return CircuitSession()


class _Binding:
    def __init__(self, session: CircuitSession):
        self._session = session
        self._previous = None

    def __enter__(self):
        self._previous = getattr(_LOCAL, "session", None)
        _LOCAL.session = self._session
        return self._session

    def __exit__(self, *_exc):
        _LOCAL.session = self._previous


def bind_circuit_session(session: CircuitSession) -> _Binding:
    return _Binding(session)


def _looks_like_challenge(index_html: str) -> bool:
    low = (index_html or "").lower()
    if "result-title" in low:  # a real result list is never the challenge page
        return False
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _norm(text: str | None) -> str:
    if not text:
        return ""
    cleaned = html.unescape(str(text))
    cleaned = re.sub(r"[‐-―]", "-", cleaned)
    cleaned = re.sub(r"(\w)-\s+(\w)", r"\1\2", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    if ref.get("url"):
        return False
    if not resolve_mod._article_like_resolution_candidate(ref):
        return False
    title = resolve_mod._article_title_candidate(ref)
    # Require a reasonably distinctive query so the scrape is not run on stubs.
    return bool(title and len(_norm(title).split()) >= 3)


def _extract_entries(index_html: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for match in _ITEM_RE.finditer(index_html or ""):
        href = html.unescape(match.group("href")).strip()
        access = _ACCESS_RE.match(href)
        if not access or href in seen:
            continue
        seen.add(href)
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", match.group("title")))).strip()
        title = re.sub(r"\s*®\s*$", "", title).strip()
        out.append(
            {
                "href": href,
                "title": title,
                "authors": html.unescape(match.group("authors") or "").strip(),
                "stage": (match.group("stage") or "").strip().lower(),
                "orig_url": access.group("url"),
            }
        )
    return out


def _entry_matches(ref: dict, entry: dict) -> bool:
    from core.resolve import service as resolve_mod

    cited_title = resolve_mod._article_title_candidate(ref)
    matched_title = entry.get("title") or ""
    if not cited_title or not matched_title:
        return False
    if _norm(matched_title) == _norm(cited_title):
        return True
    profile = _profile(ref, entry)
    overlap = profile.get("title_overlap")
    return bool(
        profile.get("author_match") is True
        and overlap is not None
        and overlap >= 0.90
    )


def _profile(ref: dict, entry: dict) -> dict:
    from core.resolve import service as resolve_mod

    authors = entry.get("authors") or ""
    msg = {
        "title": [entry.get("title") or ""],
        "author": [
            {"family": token}
            for token in re.findall(r"[A-Za-z][A-Za-z'’-]+", authors)
        ],
    }
    return resolve_mod._metadata_match_profile(ref, msg, entry.get("title") or "")


def _choose_entry(ref: dict, entries: list[dict]) -> dict | None:
    for entry in entries:
        if _entry_matches(ref, entry):
            return entry
    return None


def _fulltext_links(entry: dict) -> list[dict]:
    access_url = "https://scholar.archive.org" + entry["href"]
    content_type = "pdf" if entry.get("orig_url", "").lower().endswith(".pdf") else "html"
    return [{"url": access_url, "content_type": content_type}]


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref)
    if not title:
        return {
            "status": "unverified",
            "via": NAME,
            "reason": "no usable title for IA Scholar search",
        }

    session = getattr(_LOCAL, "session", None)
    if session is not None:
        owner, skip = session.before()
        if skip:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "IA Scholar anti-bot circuit open; request skipped",
            }
    else:
        owner = False
    query = urllib.parse.urlencode({"q": f'"{title}"'})
    try:
        status, body = resolve_mod._get(
            f"{_SEARCH_URL}?{query}",
            accept="text/html,application/xhtml+xml",
        )
    except BaseException as exc:
        if session is not None and owner:
            session.complete_probe(False)
        if not isinstance(exc, Exception):
            raise
        return {
            "status": "unresolved",
            "via": NAME,
            "reason": f"network: {type(exc).__name__}",
        }

    challenge = False
    try:
        index_html = (
            body.decode("utf-8", "replace")
            if isinstance(body, (bytes, bytearray))
            else (body or "")
        )
        challenge = _looks_like_challenge(index_html)
        if challenge:
            if session is not None and not owner:
                session.open_circuit()
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": "IA Scholar served an anti-bot session-verification page, not results",
            }

        # A non-2xx status is transient and never evidence that IA Scholar lacks
        # the work. Challenge bodies above take precedence, including HTTP 403.
        if not 200 <= int(status or 0) < 300:
            return {
                "status": "unresolved",
                "via": NAME,
                "reason": f"IA Scholar HTTP {status}",
            }

        entry = _choose_entry(ref, _extract_entries(index_html))
        if entry is None:
            return {
                "status": "unverified",
                "via": NAME,
                "reason": "no IA Scholar full-text match",
                "resolution_basis": "metadata_search",
                "existence_confidence": "low",
            }

        return {
            "status": "resolved",
            "via": NAME,
            "matched_title": entry["title"],
            "reason": "IA Scholar archived full-text match",
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium",
            "metadata_match": _profile(ref, entry),
            "retracted": False,
            "fulltext_exists": True,
            "oa_status": "open",
            "work_type": "article",
            "fulltext_links": _fulltext_links(entry),
        }
    finally:
        if session is not None and owner:
            session.complete_probe(challenge)
