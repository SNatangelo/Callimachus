# core/resolve/providers/un_digital_library.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""UN Digital Library resolver — existence confirmation by UN document symbol.

A UN document is cited by its symbol ("A/HRC/50/55"), a stable identifier the UN
Digital Library indexes exactly. This resolver reads that symbol out of the
citation, asks the library's MARC-XML export endpoint for it, and — on an exact
symbol hit — confirms the document exists and records its canonical catalogue
URL. It does not fetch the document text; it answers the prior question the
article databases cannot ("is this real, and what is it?") for a class of
citations (Special Rapporteur reports, resolutions) that carry no DOI and are
tagged low-indexability at parse time.

Determinism: same symbol -> same record. Fail-closed: any non-200, unparseable
body, or network error returns unresolved/unverified — never a false "resolved".

The endpoint 202-challenges browser-like User-Agents (the project's default
`_UA` is one) and serves plain client UAs; the honest urllib UA below is what
this client actually is, and it is what the library answers.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

try:
    from core.resolve import http as _http
except ImportError:  # direct execution
    from resolve import http as _http

NAME = "un_digital_library"
RESOLVE_NAME = "un_digital_library"

MANIFEST = {
    "origin": "un_digital_library",
    "via_aliases": ["un_digital_library"],
    "canonical_hosts": ["digitallibrary.un.org"],
}

_MARC = "{http://www.loc.gov/MARC21/slim}"
_SEARCH = "https://digitallibrary.un.org/search"
_RECORD = "https://digitallibrary.un.org/record/{}"
# See module docstring: a browser UA is met with an empty HTTP 202; the real
# urllib UA is served the MARC-XML. This is the truthful identity of the client.
_CLIENT_UA = "Python-urllib/3.11"

# A UN document symbol: a body/series prefix, then two or more slash-separated
# parts. The `{2,}` keeps a bare session tag ("A/HRC/47") from matching on its
# own while still admitting a real symbol truncated by a citation ("A/HRC/47/25").
# `\s*` around the slashes is load-bearing: PDF extraction breaks a long symbol
# across a line ("A/HRC/47/ 24/Add.2"), and without absorbing that space the
# match would stop at "A/HRC/47" — a whole session, 50 documents, none of them
# the one cited. The whitespace is stripped back out before use.
_UN_SYMBOL_RE = re.compile(
    r"\b(?:A|E|S|ST|DP|TD|CCPR|CEDAW|CRC|CAT|CERD|CMW|CRPD|HRI|FCCC|UNEP)"
    r"(?:\s*/\s*[A-Za-z0-9][A-Za-z0-9.\-]*){2,}")


def _symbol(raw_entry: str | None) -> str | None:
    """The longest well-formed UN document symbol in the citation, or None.

    Longest wins because a footnote may name a parent body before the document
    itself; the symbol carrying the most path segments is the specific one.
    """
    best = None
    for match in _UN_SYMBOL_RE.finditer(raw_entry or ""):
        candidate = re.sub(r"\s+", "", match.group(0)).rstrip(".,;:)")
        if any(ch.isdigit() for ch in candidate) and (best is None or len(candidate) > len(best)):
            best = candidate
    return best


def supports(ref: dict) -> bool:
    return bool(_symbol(ref.get("raw_entry")))


def _fetch(url: str, get_fn):
    if get_fn is not None:
        return get_fn(url, accept="application/xml")
    return _http._get(url, accept="application/xml", headers_extra={"User-Agent": _CLIENT_UA})


def _controlfield(record, tag: str) -> str | None:
    for field in record.findall(f"{_MARC}controlfield"):
        if field.get("tag") == tag:
            return (field.text or "").strip() or None
    return None


def _subfield(record, tag: str, code: str) -> str | None:
    for field in record.findall(f"{_MARC}datafield"):
        if field.get("tag") == tag:
            for sub in field.findall(f"{_MARC}subfield"):
                if sub.get("code") == code:
                    return (sub.text or "").strip() or None
    return None


def discover(ref: dict, get_fn=None) -> dict | None:
    symbol = _symbol(ref.get("raw_entry"))
    if not symbol:
        return None
    url = _SEARCH + "?" + urllib.parse.urlencode({"p": f'symbol:"{symbol}"', "of": "xm"})

    try:
        status, body = _fetch(url, get_fn)
    except Exception:
        # A network/policy failure is a statement about us, not the document:
        # retryable, never a "does not exist".
        return {"status": "unresolved", "via": NAME,
                "reason": "UN Digital Library did not answer (network or blocked)",
                "resolution_basis": "un_doc_symbol"}

    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if str(status) != "200":
        # 202 is this host's soft anti-bot challenge; any non-200 is transient.
        return {"status": "unresolved", "via": NAME,
                "reason": f"UN Digital Library returned HTTP {status} for symbol {symbol}",
                "resolution_basis": "un_doc_symbol"}

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {"status": "unresolved", "via": NAME,
                "reason": "UN Digital Library returned unparseable MARC-XML",
                "resolution_basis": "un_doc_symbol"}

    records = root.findall(f"{_MARC}record")
    # Resolve only on an EXACT symbol match. A symbol query is a prefix search:
    # a whole-session tag ("A/HRC/47") returns 50 unrelated documents, and taking
    # the first would confirm the wrong work. The record whose own symbol (191$a)
    # equals what we asked for is the cited document; anything else is a miss.
    want = symbol.replace(" ", "").upper()
    record = next(
        (r for r in records
         if (_subfield(r, "191", "a") or "").replace(" ", "").upper() == want),
        None)
    if record is None:
        return {"status": "unverified", "via": NAME,
                "reason": (f"symbol {symbol} not matched exactly in the UN Digital Library"
                           + (f" ({len(records)} near matches)" if records else "")),
                "resolution_basis": "un_doc_symbol", "existence_confidence": "low"}

    record_id = _controlfield(record, "001")
    title = _subfield(record, "245", "a") or _subfield(record, "246", "a")
    record_url = _RECORD.format(record_id) if record_id else None
    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": (title or "").strip(" /:") or None,
        # The canonical record URL is carried in the reason for the audit trail
        # rather than as a fulltext_link: the catalogue page is not the document
        # text, and the fetch stage must not store it as though it were.
        "reason": ("UN document confirmed by exact doc-symbol match in the UN Digital Library"
                   + (f" ({record_url})" if record_url else "")),
        "resolution_basis": "un_doc_symbol",
        "existence_confidence": "high",
        "retracted": False,
        "fulltext_exists": False,
        "oa_status": "unknown",
        "work_type": "report",
        "identity_basis": "un_doc_symbol",
    }
