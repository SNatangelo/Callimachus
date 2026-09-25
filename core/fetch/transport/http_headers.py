#!/usr/bin/env python3
# core/fetch/transport/http_headers.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Dispatch optional HTTP request profiles for deterministic network calls."""

from __future__ import annotations

import http.cookiejar
import os
import re
import ssl
import urllib.parse
import urllib.request

try:
    import certifi as _certifi
except ImportError:
    _certifi = None

try:
    from core.fetch import http_profiles as _profiles
except ImportError:
    import http_profiles as _profiles

ENV_USER_AGENT = "CITATION_VERIFIER_USER_AGENT"
ENV_ACCEPT_LANGUAGE = "CITATION_VERIFIER_ACCEPT_LANGUAGE"
ENV_HTTP_PROFILE = _profiles.ENV_HTTP_PROFILE
HTTP_PROFILE_CHOICES = tuple(_profiles.discover_names())
DEFAULT_HTTP_PROFILE = _profiles.DEFAULT_PROFILE

# Browser-like default. Chrome 149.0.7827.102 was the current stable Windows desktop
# release when this default was introduced on 2026-06-19.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.7827.102 Safari/537.36"
)
DEFAULT_API_USER_AGENT = "CitationVerifier/1.0"
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9,it-IT;q=0.8,it;q=0.7"

_PROVIDER_CREDENTIAL_HEADERS = frozenset({
    "wiley-tdm-client-token",
    "x-els-apikey",
})


def configured_user_agent(profile: str = "document") -> str:
    override = os.environ.get(ENV_USER_AGENT)
    if override:
        return override
    if profile == "api":
        return DEFAULT_API_USER_AGENT
    return DEFAULT_BROWSER_USER_AGENT


def configured_accept_language() -> str:
    return os.environ.get(ENV_ACCEPT_LANGUAGE) or DEFAULT_ACCEPT_LANGUAGE


def configured_http_profile() -> str:
    return _profiles.configured_name()


def user_agent_with_mailto(mailto: str | None, *, profile: str = "document") -> str:
    ua = configured_user_agent(profile)
    return f"{ua} (mailto:{mailto})" if mailto else ua


def request_headers(
    *,
    url: str,
    accept: str,
    profile: str = "document",
    mailto: str | None = None,
    referer: str | None = None,
) -> dict[str, str]:
    mod = _profiles.active_module()
    if mod is None:
        return {
            "User-Agent": user_agent_with_mailto(mailto, profile=profile),
            "Accept": accept,
            **({"Referer": referer} if referer else {}),
        }
    return mod.build_headers(
        url=url,
        accept=accept,
        user_agent=user_agent_with_mailto(mailto, profile=profile),
        accept_language=configured_accept_language(),
        referer=referer,
        profile=profile,
    )


def _consent_cookie_for_url(url: str) -> str | None:
    """Return a consent cookie string for cookie-walled domains (ACM, etc.)."""
    mod = _profiles.active_module()
    fn = getattr(mod, "consent_cookie", None) if mod is not None else None
    return fn(url) if callable(fn) else None


def _http_origin(url: str) -> tuple[str, str, int] | None:
    """Return a canonical HTTP origin, or ``None`` when it is unsafe to compare."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 80 if scheme == "http" else 443
    return scheme, hostname.lower(), port


def _strip_provider_credentials(req: urllib.request.Request) -> None:
    """Remove publisher credentials from both urllib header stores."""
    for attribute in ("headers", "unredirected_hdrs"):
        headers = getattr(req, attribute, None)
        if not isinstance(headers, dict):
            continue
        for name in tuple(headers):
            if name.lower() in _PROVIDER_CREDENTIAL_HEADERS:
                del headers[name]


class _ConsentCookieRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that:
    1. Rewrites broken DOI redirects (e.g. ACM citation.cfm → /doi/abs/)
    2. Injects consent cookies for cookie-walled publishers.

    urllib.request.HTTPRedirectHandler copies *all* headers (including
    Cookie) across redirects — unlike browsers, which strip Cookie on
    cross-origin redirects. This handler strips any stale consent cookie
    before the redirect and re-adds it only when the target domain is a
    known cookie-walled publisher; authenticated publisher headers are
    retained only on same-origin hops.

    It also records the redirect chain (status code + rewritten target per
    hop) for the caller to inspect afterwards. The handler instance itself
    (``_consent_opener`` below) is a module-level singleton shared by
    concurrent fetch workers, so the chain cannot live on ``self`` — that
    would cross-contaminate interleaved requests. Instead it is carried on
    the ``Request`` object, which is per-call: each hop reads the chain off
    the incoming ``req`` (creating it if this is the first hop), appends its
    own entry, and hands the same list to ``new_req`` so the next hop keeps
    appending to it. The caller's original Request ends up holding the
    complete chain once ``opener.open()`` returns.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl2 = newurl

        # Rewrite broken ACM redirects: doi.org sends us to the deprecated
        # portal.acm.org/citation.cfm endpoint which always returns 403.
        # The canonical URL pattern is https://dl.acm.org/doi/abs/<doi>.
        if ("portal.acm.org/citation.cfm" in newurl2
                or "dl.acm.org/citation.cfm" in newurl2):
            doi_m = re.search(r"10\.\d{4,9}/[^\s?#\"']+", newurl2)
            if doi_m:
                doi_val = doi_m.group(0).rstrip(".")
                newurl2 = f"https://dl.acm.org/doi/abs/{doi_val}"

        # Read (or create) the chain on the INCOMING req. Creating it here means
        # the very first hop's req — the Request object the caller itself holds
        # — ends up owning this list, and since every later hop appends to the
        # same list in place (never replaces it), the caller sees all of them.
        chain = getattr(req, "_redirect_chain", None)
        if chain is None:
            chain = []
            req._redirect_chain = chain
        chain.append((code, newurl2))

        new_req = super().redirect_request(req, fp, code, msg, headers, newurl2)
        if new_req is not None:
            # urllib carries request headers across redirects. Publisher API
            # credentials must never leave their original HTTP origin.
            source_origin = _http_origin(req.full_url)
            destination_origin = _http_origin(new_req.full_url)
            if (
                source_origin is None
                or destination_origin is None
                or source_origin != destination_origin
            ):
                _strip_provider_credentials(new_req)
            # Strip any stale Cookie header carried over from the previous request
            # so we don't leak a consent cookie to third-party redirect targets.
            new_req.remove_header("Cookie")
            cookie = _consent_cookie_for_url(newurl2)
            if cookie:
                new_req.add_unredirected_header("Cookie", cookie)
            new_req._redirect_chain = chain
        return new_req

def _trusted_ssl_context():
    if _certifi is None:
        return None
    try:
        return ssl.create_default_context(cafile=_certifi.where())
    except Exception:
        return None


def _build_consent_opener():
    handlers: list[object] = [_ConsentCookieRedirectHandler()]
    trusted_context = _trusted_ssl_context()
    if trusted_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=trusted_context))
    return urllib.request.build_opener(*handlers)


_consent_opener = _build_consent_opener()


def open_request(req: urllib.request.Request, timeout: int):
    return _consent_opener.open(req, timeout=timeout)


def build_session_opener():
    """A one-off opener carrying a fresh cookie jar, for cookie-handshake retries.

    Unlike the shared ``_consent_opener`` (which injects only static consent
    cookies), this persists ``Set-Cookie`` responses across requests, so a
    warm-up GET on the origin can collect a server-issued session cookie that a
    follow-up request then replays to clear an Atypon ``cookieAbsent`` gate. Each
    call returns an isolated opener so concurrent fetches never share a jar.
    """
    jar = http.cookiejar.CookieJar()
    handlers: list[object] = [
        _ConsentCookieRedirectHandler(),
        urllib.request.HTTPCookieProcessor(jar),
    ]
    trusted_context = _trusted_ssl_context()
    if trusted_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=trusted_context))
    return urllib.request.build_opener(*handlers)
