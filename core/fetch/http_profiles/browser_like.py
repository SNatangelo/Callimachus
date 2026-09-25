#!/usr/bin/env python3
# core/fetch/http_profiles/browser_like.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser-like deterministic HTTP profile."""

from __future__ import annotations

import urllib.parse

NAME = "browser_like"

# Domains that serve a cookie-wall before showing content.
# A minimal consent cookie bypasses the wall without enabling tracking.
# Keys are domain suffixes (match any host ending with the key).
_COOKIE_CONSENT: dict[str, str] = {
    ".acm.org": "cookieConsent=accepted",
}


def _same_origin(url: str, referer: str | None) -> bool:
    if not referer:
        return False
    u = urllib.parse.urlparse(url)
    r = urllib.parse.urlparse(referer)
    return bool(u.scheme and u.netloc and u.scheme == r.scheme and u.netloc == r.netloc)


def _consent_cookie(url: str) -> str | None:
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    # Exact match first, then suffix match (e.g. ".acm.org" matches any ACM host).
    if host in _COOKIE_CONSENT:
        return _COOKIE_CONSENT[host]
    for suffix, cookie in _COOKIE_CONSENT.items():
        if suffix.startswith(".") and host.endswith(suffix):
            return cookie
    return None


# Public alias used by http_headers._ConsentCookieRedirectHandler.
consent_cookie = _consent_cookie


def build_headers(*, url: str, accept: str, user_agent: str,
                  accept_language: str, referer: str | None = None,
                  profile: str = "document") -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": accept_language,
        "DNT": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    if profile in ("document", "pdf"):
        headers["Upgrade-Insecure-Requests"] = "1"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "same-origin" if _same_origin(url, referer) else "none"
        headers["Sec-Fetch-User"] = "?1"
    elif profile == "api":
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "none"
    cookie = _consent_cookie(url)
    if cookie:
        headers["Cookie"] = cookie
    return headers
