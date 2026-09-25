#!/usr/bin/env python3
# core/fetch/fallbacks/wayback.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Wayback Machine fallback for the fetch pipeline.

When the live fetch of a URL is blocked or empty — a bot/JS challenge, a dead
link, or a paywall/landing we could only read an abstract from — the Internet
Archive often holds a usable snapshot. This module turns a set of original URLs
into archived-snapshot fetch candidates, which the normal pipeline then fetches,
extracts and identity-probes like any other candidate. It complements the live
cookie handshake: the handshake clears an anti-bot wall in real time, Wayback
sidesteps it when the live page cannot be cleared.

What it can recover: the full text of an OA page that was open when archived, the
public abstract from an archived publisher landing, or a dead web citation. It
cannot recover a full text that was paywalled at archive time (the PDF behind the
wall was never captured). Snapshots are usually HTML; that is fine — the pipeline
extracts text from HTML just as readily.

There is no bulk API here on purpose: the availability lookup is one request per
URL, host-rate-limited and capped, because archive.org throttles aggressively.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from core.fetch.transport.http import FetchAdmissionDeferred

ENV_WAYBACK = "CITATION_VERIFIER_WAYBACK"
_AVAILABILITY = "https://archive.org/wayback/available?url={}"
_WAYBACK_HOSTS = ("web.archive.org", "archive.org")
_TIMESTAMP_RE = re.compile(r"(/web/\d{14})/")


def enabled(environ: dict | None = None) -> bool:
    value = str((environ or os.environ).get(ENV_WAYBACK, "1")).strip().lower()
    return value not in ("0", "false", "no", "off")


def _is_archive_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return any(host == h or host.endswith("." + h) for h in _WAYBACK_HOSTS)


def _raw_variant(snapshot: str) -> str:
    """Rewrite a snapshot URL to its raw ("id_") form, which serves the original
    captured bytes without the Wayback navigation chrome — cleaner to extract."""
    return _TIMESTAMP_RE.sub(r"\1id_/", snapshot, count=1)


def snapshot_url(original_url: str, *, fetch_url) -> str | None:
    """Return the raw archived-snapshot URL for ``original_url``, or None.

    ``fetch_url`` is the pipeline's fetcher: ``fetch_url(url, accept=..., profile=...)``
    returning a dict with a ``body``. Only a snapshot the archive reports as
    available and (where stated) HTTP 200 is accepted.
    """
    if not original_url or _is_archive_url(original_url):
        return None
    api = _AVAILABILITY.format(urllib.parse.quote(original_url, safe=""))
    try:
        result = fetch_url(api, accept="application/json", profile="document")
    except FetchAdmissionDeferred:
        raise
    except Exception:
        return None
    if isinstance(result, dict) and result.get("status") == 429:
        deferred = FetchAdmissionDeferred("archive.org", 0)
        deferred.reason_code = "rate_limit_response"
        deferred.http_status = 429
        deferred.url = api
        raise deferred
    body = result.get("body") if isinstance(result, dict) else None
    if not body:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    closest = (((data or {}).get("archived_snapshots") or {}).get("closest") or {})
    if not closest.get("available"):
        return None
    status = str(closest.get("status") or "200")
    if status != "200":
        return None
    snapshot = closest.get("url")
    return _raw_variant(snapshot) if snapshot else None


def _springer_legacy_alias(url: str) -> str | None:
    """Return the legacy Springer PDF spelling used by older Wayback captures."""
    parsed = urllib.parse.urlsplit(url)
    route = re.fullmatch(r"/content/pdf/(.+)", parsed.path, flags=re.IGNORECASE)
    if parsed.hostname != "link.springer.com" or route is None:
        return None
    doi_pdf = route.group(1)
    if not doi_pdf.lower().endswith(".pdf") or "%2f" not in doi_pdf.lower():
        return None
    doi_pdf = re.sub(r"%2f", "/", doi_pdf, flags=re.IGNORECASE)
    return urllib.parse.urlunsplit(("http", parsed.netloc, "/content/pdf/" + doi_pdf, parsed.query, parsed.fragment))


def build_candidates(urls, *, fetch_url, seen=None, max_lookups: int = 3) -> list[dict]:
    """Turn original URLs into archived-snapshot fetch candidates.

    At most ``max_lookups`` distinct non-archive URLs are looked up (archive.org
    is rate-limited); already-seen originals and snapshots are skipped.
    """
    seen = seen if seen is not None else set()
    out: list[dict] = []
    looked = 0
    for url in urls:
        if looked >= max_lookups:
            break
        if not url or url in seen or _is_archive_url(url):
            continue
        alias = _springer_legacy_alias(url)
        if alias and alias in seen:
            continue
        seen.add(url)
        if alias:
            seen.add(alias)
        looked += 1
        snapshot = snapshot_url(url, fetch_url=fetch_url)
        lookup_url = url
        if not snapshot and alias:
            snapshot = snapshot_url(alias, fetch_url=fetch_url)
            lookup_url = alias
        if not snapshot or snapshot in seen:
            continue
        seen.add(snapshot)
        kind = "pdf" if lookup_url.split("?", 1)[0].rstrip("/").lower().endswith(".pdf") else "html"
        out.append({
            "url": snapshot,
            "kind": kind,
            "method": "wayback",
            "fallback_stage": "wayback",
            "referer": None,
            "discovery_reason": f"wayback snapshot of {lookup_url}",
        })
    return out


_WALL_MARKERS = (
    "challenge", "paywall", "paywalled", "access denied", "denied",
    "did not return http 200", "403", "404", "410", "429",
    "cookieabsent", "login", "captcha",
)
_SOFT_DEAD_LINK_REDIRECT = "soft_dead_link_redirect"


def soft_dead_link_original_url(result: dict | None) -> str | None:
    """Return the cited URL behind a narrow soft-dead-link marker, if present."""
    if not result:
        return None
    for failure in result.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        if failure.get("reason_code") != _SOFT_DEAD_LINK_REDIRECT:
            continue
        requested_url = failure.get("requested_url")
        if isinstance(requested_url, str) and requested_url:
            return requested_url
    return None


def result_hit_access_wall(result: dict | None) -> bool:
    """Whether the live fetch was blocked/empty in a way an archived copy may fix.

    True when the run met an access wall (challenge, paywall, access-denied, dead
    link) or resolved only to a metadata shell — not when full text was simply
    never openly published.
    """
    if not result:
        return False
    if result.get("paywalled"):
        return True
    if str(result.get("status") or "") == "metadata_only":
        return True
    if soft_dead_link_original_url(result):
        return True
    # The result's own reason counts, not just the per-candidate failures. A run
    # that ends on a paywall reports it there ("full text appears paywalled…") and
    # carries no `paywalled` key — that one is only ever set on the per-candidate
    # dict — so reading failures alone missed the whole closed-access case, which
    # is precisely a case an archived landing can answer with its abstract.
    reasons = [str(result.get("reason") or "")]
    reasons += [str((failure or {}).get("reason") or "")
                for failure in result.get("failures") or []]
    return any(marker in reason.lower()
               for reason in reasons for marker in _WALL_MARKERS)
