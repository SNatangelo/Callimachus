# core/resolve/providers/issue_springerlink.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed-world issue attestations for explicitly routed SpringerLink journals."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from ._issue_common import coordinate, target_status, text_key


ISSUE_ATTESTATION = {
    "provider": "springerlink_official_issue",
    "rule_version": "springerlink-issue-attestation/v2",
}
_HOST = "link.springer.com"
_CLIENT_UA = "Callimachus/1.0 (bibliographic issue attestation)"
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# A route is evidence only for journals explicitly checked here.  Adding a
# Springer title requires its canonical title, accepted citation aliases and
# registered ISSNs to be reviewed together; this is not a publisher-wide claim.
_JOURNALS = ({
    "title": "Intensive Care Medicine",
    "journal_id": "134",
    "aliases": frozenset({
        text_key("Intensive Care Medicine"), text_key("Intensive Care Med"),
        text_key("Intensiv Care Med"),
    }),
    "issns": frozenset({"0342-4642", "1432-1238"}),
}, {
    "title": "Journal of Clinical Monitoring and Computing",
    "journal_id": "10877",
    "aliases": frozenset({
        text_key("Journal of Clinical Monitoring and Computing"),
        text_key("J Clin Monit Comput"),
    }),
    "issns": frozenset({"1387-1307", "1573-2614"}),
})


class _IssuePage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._card_depth = 0
        self._title_depth = 0
        self._title_container_depth = 0
        self._title: list[str] = []
        self._url: str | None = None
        self._authors: list[str] = []
        self._author_depth = 0
        self.text: list[str] = []
        self.blocked = False
        self.paged = False

    def handle_starttag(self, tag, attrs) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        classes = values.get("class", "").casefold()
        marker = " ".join((classes, values.get("data-test", "").casefold()))
        if any(word in marker for word in ("load-more", "pagination", "pager", "challenge")):
            self.paged = True
        if tag == "article" and (
            "c-listing__item" in classes
            or "article-card" in classes
            or "app-card-open" in classes
        ):
            if self._card_depth:
                raise ValueError("nested SpringerLink article cards")
            self._card_depth = 1
            self._title, self._authors, self._url = [], [], None
            return
        if self._card_depth:
            if tag in _VOID_TAGS:
                return
            self._card_depth += 1
            if tag == "h2" and "app-card-open__heading" in classes:
                self._title_container_depth = self._card_depth
            if tag == "a" and (
                "c-card__title" in classes
                or "article-title" in classes
                or self._title_container_depth
            ):
                self._title_depth = self._card_depth
                self._url = values.get("href")
            if (
                tag in {"span", "p", "li"}
                and ("author" in classes or "app-author-list__item" in classes)
            ):
                self._author_depth = self._card_depth

    def handle_endtag(self, _tag) -> None:
        if self._card_depth:
            if self._title_depth == self._card_depth:
                self._title_depth = 0
            if self._title_container_depth == self._card_depth:
                self._title_container_depth = 0
            if self._author_depth == self._card_depth:
                self._author_depth = 0
            self._card_depth -= 1
            if self._card_depth == 0:
                title = " ".join("".join(self._title).split())
                author = " ".join("".join(self._authors).split()) or None
                if not title or not self._url:
                    raise ValueError("SpringerLink article card lacks title or URL")
                self.cards.append({"title": title, "url": self._url, "first_author": author})

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data) -> None:
        self.text.append(data)
        if self._title_depth:
            self._title.append(data)
        if self._author_depth:
            self._authors.append(data)


def _journal(ref: dict) -> dict | None:
    cited = text_key(coordinate(ref, "container"))
    return next((item for item in _JOURNALS if cited in item["aliases"]), None)


def supports_issue_attestation(ref: dict) -> bool:
    source_kind = ref.get("source_kind") or ref.get("source_type")
    return bool(
        source_kind in {"article_like", "article"}
        and _journal(ref) is not None
        and str(coordinate(ref, "volume") or "").isdigit()
        and str(coordinate(ref, "issue") or "").isdigit()
    )


def _canonical_article_url(value: str) -> str:
    url = urljoin(f"https://{_HOST}/", value)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.casefold() != _HOST or not parts.path.startswith("/article/"):
        raise ValueError("SpringerLink article URL is not canonical")
    return url


def _members(body: str, *, journal: dict, volume: str, issue: str, url: str) -> list[dict]:
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 5_000_000:
        raise ValueError("SpringerLink issue response is missing or oversized")
    parser = _IssuePage()
    parser.feed(body)
    parser.close()
    text = " ".join(" ".join(parser.text).split())
    count_matches = re.findall(r"\b(\d+)\s+articles?\b", text, re.I)
    issue_identity = re.search(
        rf"\bVolume\s+{re.escape(volume)}\s*,?\s*Issue\s+{re.escape(issue)}\b",
        text, re.I,
    )
    if (
        len(count_matches) != 1 or parser.paged or issue_identity is None
        or any(word in text.casefold() for word in ("access denied", "verify you are human", "captcha"))
    ):
        raise ValueError("SpringerLink issue page is not a complete static TOC")
    declared = int(count_matches[0])
    if declared < 1 or declared != len(parser.cards):
        raise ValueError("SpringerLink declared article count does not match static cards")
    members = []
    for order, card in enumerate(parser.cards):
        member_url = _canonical_article_url(card["url"])
        record_key = "\x1f".join((journal["journal_id"], volume, issue, member_url))
        members.append({
            "record_id": "springerlink:" + hashlib.sha256(record_key.encode()).hexdigest(),
            "title": card["title"], "first_author": card["first_author"], "year": None,
            "journal": journal["title"], "volume": volume, "issue": issue,
            # The card order is an inventory ordinal, not a bibliographic page
            # or article number.  The current static card shape exposes neither.
            "locator": None, "pmid": None, "pmcid": None,
            "doi": None, "url": member_url,
        })
    return members


def _accepted_cookie_redirect(requested: str, final: str) -> bool:
    """Accept SpringerLink's known cookie marker without widening redirects."""
    if final == requested:
        return True
    expected = urlsplit(requested)
    observed = urlsplit(final)
    if (
        observed.scheme != "https"
        or observed.netloc.casefold() != _HOST
        or observed.path != expected.path
        or observed.fragment
    ):
        return False
    pairs = observed.query.split("&")
    if len(pairs) != 2:
        return False
    query = dict(item.split("=", 1) for item in pairs if "=" in item)
    if len(query) != 2 or query.get("error") != "cookies_not_supported":
        return False
    return re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        query.get("code", ""), re.I,
    ) is not None


def attest_issue(ref: dict) -> dict:
    journal = _journal(ref)
    volume, issue = coordinate(ref, "volume"), coordinate(ref, "issue")
    if journal is None or not volume or not issue:
        raise ValueError("SpringerLink issue coordinates are unsupported")
    url = f"https://{_HOST}/journal/{journal['journal_id']}/volumes-and-issues/{volume}-{issue}"
    from core.resolve import service as resolve_mod

    status, body, final_url = resolve_mod._get_with_final_url(
        url,
        accept="text/html",
        headers_extra={"User-Agent": _CLIENT_UA},
    )
    if not 200 <= int(status) < 300:
        raise RuntimeError(f"SpringerLink issue TOC returned HTTP {status}")
    if not _accepted_cookie_redirect(url, final_url):
        raise ValueError("SpringerLink issue TOC redirected away from its canonical URL")
    members = _members(body, journal=journal, volume=volume, issue=issue, url=url)
    target, order = target_status(ref, members)
    return {
        "provider": ISSUE_ATTESTATION["provider"], "rule_version": ISSUE_ATTESTATION["rule_version"],
        "status": "complete", "reason": "official static SpringerLink issue TOC has a matching declared article count",
        "scope": "issue", "target_status": target, "target_member_order": order,
        "cited_container": coordinate(ref, "container"), "cited_volume": volume, "cited_issue": issue,
        "journal_title": journal["title"], "completeness_basis": "official_static_toc_declared_count_matches_cards",
        "members": members,
        "sources": [{"role": "issue_toc", "url": url, "response_sha256": hashlib.sha256(body.encode()).hexdigest()}],
        "observations": [
            {"key": "issn", "value_type": "text", "text_value": sorted(journal["issns"])[0], "integer_value": None},
            {"key": "declared_article_count", "value_type": "integer", "text_value": None, "integer_value": len(members)},
        ],
    }
