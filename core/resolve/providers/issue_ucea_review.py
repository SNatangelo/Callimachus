# core/resolve/providers/issue_ucea_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official full-issue PDF attestation for UCEA Review."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from ._issue_common import coordinate, target_status, text_key


ISSUE_ATTESTATION = {
    "provider": "ucea_review_official_issue",
    "rule_version": "issue-attestation/v1",
}
_JOURNAL = "UCEA Review"
_ALIASES = frozenset({text_key(_JOURNAL), text_key("University Council for Educational Administration Review")})
_ARCHIVE_URL = "https://www.ucea.org/ucea_review.php"
_HOST = "www.ucea.org"


class _Archive(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a" or self.href is not None:
            return
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        self.href = values.get("href")
        self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join("".join(self.parts).split())))
            self.href = None
            self.parts = []


def supports_issue_attestation(ref: dict) -> bool:
    source_kind = ref.get("source_kind") or ref.get("source_type")
    return bool(
        source_kind in {"article_like", "article"}
        and text_key(coordinate(ref, "container")) in _ALIASES
        and str(coordinate(ref, "volume") or "").isdigit()
        and str(coordinate(ref, "issue") or "").isdigit()
    )


def _issue_pdf_url(body: str, *, volume: str, issue: str) -> tuple[str, str | None]:
    parser = _Archive()
    parser.feed(body)
    parser.close()
    pattern = re.compile(
        rf"^Volume\s+{re.escape(volume)}\s+Number\s+{re.escape(issue)}\b(?:\s*,\s*(\w+))?",
        re.IGNORECASE,
    )
    matches = [(href, pattern.match(label)) for href, label in parser.links]
    matches = [(href, match) for href, match in matches if match is not None]
    if len(matches) != 1:
        raise ValueError("UCEA archive does not identify exactly one cited issue")
    href, match = matches[0]
    url = urljoin(_ARCHIVE_URL, href)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.casefold() != _HOST or not parts.path.casefold().endswith(".pdf"):
        raise ValueError("UCEA issue link is not a canonical official PDF")
    return url, match.group(1) if match else None


def _pdf_members(body: bytes, *, volume: str, issue: str, pdf_url: str) -> tuple[int, str, list[dict]]:
    if not isinstance(body, bytes) or not body.startswith(b"%PDF-") or len(body) > 100_000_000:
        raise ValueError("UCEA issue PDF response is missing, invalid, or oversized")
    import pymupdf as fitz

    document = None
    try:
        document = fitz.open(stream=body, filetype="pdf")
        if document.page_count < 2 or document.page_count > 500:
            raise ValueError("UCEA issue PDF page count is implausible")
        document_page_count = document.page_count
        first = " ".join((document[0].get_text() or "").split())
        header = re.search(
            rf"\bVolume\s+{re.escape(volume)}\s+Number\s+{re.escape(issue)}\b",
            first,
            re.IGNORECASE,
        )
        year_match = re.search(r"\b(?:18|19|20)\d{2}\b", first)
        if "UCEA REVIEW" not in first.upper() or header is None or year_match is None:
            raise ValueError("UCEA PDF does not attest the requested issue identity")
        year = int(year_match.group(0))
        toc_text = document[1].get_text() or ""
        lines = [" ".join(line.split()) for line in toc_text.splitlines() if line.strip()]
    finally:
        if document is not None:
            document.close()
    try:
        start = next(index for index, line in enumerate(lines) if text_key(line) == "in this issue") + 1
    except StopIteration as exc:
        raise ValueError("UCEA issue PDF has no explicit table of contents") from exc
    members: list[dict] = []
    pending: list[str] = []
    item_re = re.compile(r"^(.*?)(?:\.{3,})\s*(\d+)\s*$")
    for line in lines[start:]:
        match = item_re.match(line)
        if match is None:
            pending.append(line)
            if len(pending) > 4:
                raise ValueError("UCEA table of contents structure is incomplete")
            continue
        title = " ".join((*pending, match.group(1))).strip()
        pending = []
        locator = int(match.group(2))
        if len(text_key(title).split()) < 2 or not 1 <= locator <= document_page_count:
            raise ValueError("UCEA table of contents member is malformed")
        key = "\x1f".join((volume, issue, str(locator), text_key(title)))
        members.append({
            "record_id": "ucea-review:" + hashlib.sha256(key.encode()).hexdigest(),
            "title": title,
            "first_author": None,
            "year": year,
            "journal": _JOURNAL,
            "volume": volume,
            "issue": issue,
            "locator": str(locator),
            "pmid": None,
            "pmcid": None,
            "doi": None,
            "url": pdf_url,
        })
        if locator == document_page_count:
            break
    # A complete issue PDF must enumerate from printed page one through its
    # final PDF page.  Anything weaker is useful discovery, not closed-world evidence.
    locators = [int(member["locator"]) for member in members]
    page_count = max(locators, default=0)
    if (
        len(members) < 3
        or locators[0] != 1
        or len(locators) != len(set(locators))
        or page_count == 0
    ):
        raise ValueError("UCEA table of contents does not bound the complete issue")
    if page_count != document_page_count:
        raise ValueError("UCEA table of contents does not reach the final PDF page")
    return year, hashlib.sha256(body).hexdigest(), members


def attest_issue(ref: dict) -> dict:
    volume = coordinate(ref, "volume")
    issue = coordinate(ref, "issue")
    if not volume or not issue:
        raise ValueError("UCEA issue coordinates are missing")
    from core.resolve import service as resolve_mod

    archive_status, archive_body, final_archive_url = resolve_mod._get_with_final_url(
        _ARCHIVE_URL, accept="text/html",
    )
    if not 200 <= int(archive_status) < 300:
        raise RuntimeError(f"UCEA archive returned HTTP {archive_status}")
    if final_archive_url != _ARCHIVE_URL:
        raise ValueError("UCEA archive redirected away from its canonical URL")
    pdf_url, season = _issue_pdf_url(archive_body, volume=volume, issue=issue)
    pdf_status, pdf_body, final_pdf_url = resolve_mod._get_bytes_with_final_url(
        pdf_url, accept="application/pdf",
    )
    if not 200 <= int(pdf_status) < 300:
        raise RuntimeError(f"UCEA issue PDF returned HTTP {pdf_status}")
    if final_pdf_url != pdf_url:
        raise ValueError("UCEA issue PDF redirected outside its attested archive URL")
    year, pdf_sha, members = _pdf_members(pdf_body, volume=volume, issue=issue, pdf_url=pdf_url)
    target, order = target_status(ref, members)
    observations = [
        {"key": "publication_year", "value_type": "integer", "text_value": None, "integer_value": year},
        {"key": "document_page_count", "value_type": "integer", "text_value": None, "integer_value": max(int(m["locator"]) for m in members)},
        {"key": "toc_member_count", "value_type": "integer", "text_value": None, "integer_value": len(members)},
    ]
    if season:
        observations.append({"key": "issue_season", "value_type": "text", "text_value": season, "integer_value": None})
    return {
        "provider": ISSUE_ATTESTATION["provider"],
        "rule_version": ISSUE_ATTESTATION["rule_version"],
        "status": "complete",
        "reason": "official full-issue PDF has an exact issue header and a TOC spanning the document",
        "scope": "issue",
        "target_status": target,
        "target_member_order": order,
        "cited_container": coordinate(ref, "container"),
        "cited_volume": volume,
        "cited_issue": issue,
        "journal_title": _JOURNAL,
        "completeness_basis": "official_full_issue_pdf_bounded_by_toc",
        "members": members,
        "sources": [
            {"role": "issue_archive", "url": _ARCHIVE_URL, "response_sha256": hashlib.sha256(archive_body.encode()).hexdigest()},
            {"role": "full_issue_pdf", "url": pdf_url, "response_sha256": pdf_sha},
        ],
        "observations": observations,
    }
