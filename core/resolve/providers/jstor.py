# core/resolve/providers/jstor.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""JSTOR stable-record resolver.

JSTOR's ``/stable/`` URLs are record identifiers, not necessarily DOI URLs:
for example, a citation may contain ``/stable/10.2307/27105880`` while the
canonical landing page is ``/stable/27105880``.  Resolve only an explicit URL
on JSTOR's canonical host and confirm that exact terminal stable id through
JSTOR's unauthenticated RIS endpoint.  A landing page is metadata evidence,
not evidence that full text is available or open.
"""

from __future__ import annotations

import html
import re
import urllib.parse

NAME = "jstor"
RESOLVE_NAME = "jstor_stable"
OPTIONAL_STAGE = True
AUTHORITATIVE_IDENTIFIER = {"scheme": "jstor", "supersedes": ("doi",)}
MANIFEST = {
    "origin": "jstor",
    "via_aliases": ["jstor", "jstor_stable"],
    "canonical_hosts": ["www.jstor.org"],
}

_RIS_URL = "https://www.jstor.org/citation/ris/{}"
_LANDING_URL = "https://www.jstor.org/stable/{}"
_URL_RE = re.compile(r"https?://www\.jstor\.org/stable/[^\s<>\"']+", re.IGNORECASE)
_SIMPLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\Z")


def _stable_id_from_url(value: object) -> tuple[str, str] | None:
    """Return (endpoint id, canonical terminal id) for one explicit URL.

    ``10.2307/<terminal>`` is accepted only as the path spelling used by
    JSTOR citation exports.  The terminal portion is what JSTOR itself emits
    in its canonical ``UR`` field and is the identifier persisted downstream.
    """
    text = str(value or "").strip().rstrip(".,;:)]}")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or parsed.netloc.lower() != "www.jstor.org":
        return None
    if parsed.query or parsed.fragment or not parsed.path.startswith("/stable/"):
        return None
    endpoint_id = parsed.path[len("/stable/"):]
    if not endpoint_id or "/" in endpoint_id and not endpoint_id.startswith("10.2307/"):
        return None
    if endpoint_id.startswith("10.2307/"):
        terminal = endpoint_id[len("10.2307/"):]
        if not terminal or "/" in terminal:
            return None
    else:
        terminal = endpoint_id
    if _SIMPLE_ID_RE.fullmatch(terminal) is None:
        return None
    return endpoint_id, terminal


def _stable_reference(ref: dict) -> tuple[str, str] | None:
    """Return the one unambiguous JSTOR stable URL declared by the citation."""
    candidates = []
    direct = _stable_id_from_url(ref.get("url"))
    if direct is not None:
        candidates.append(direct)
    raw = str(ref.get("raw_entry") or "")
    for match in _URL_RE.finditer(raw):
        parsed = _stable_id_from_url(match.group(0))
        if parsed is not None:
            candidates.append(parsed)
    if not candidates:
        return None
    terminal_ids = {item[1] for item in candidates}
    if len(terminal_ids) != 1:
        return None
    # Prefer the most specific cited spelling for the RIS request, but never
    # let duplicate copies of a URL influence the identifier selected.
    return candidates[0]


def supports(ref: dict) -> bool:
    """Only explicit canonical JSTOR stable URLs opt into this resolver."""
    return _stable_reference(ref) is not None


def superseded_identifier_schemes(ref: dict) -> tuple[str, ...]:
    """Suppress DOI lookup only for the stable-path spelling misparsed as DOI."""
    reference = _stable_reference(ref)
    if reference is None or not reference[0].casefold().startswith("10.2307/"):
        return ()
    doi = str(ref.get("doi") or "").strip().casefold()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi).rstrip(".,;:)]}")
    return ("doi",) if doi == reference[0].casefold() else ()


def _clean(value: str | None) -> str | None:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_ris(body: object) -> dict[str, list[str]] | None:
    """Parse one conservative RIS record, rejecting malformed/challenge text."""
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body or "")
    fields: dict[str, list[str]] = {}
    started = False
    ended = False
    last_tag: str | None = None
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z0-9]{2})  - ?(.*)", line)
        if match:
            tag, value = match.groups()
            if ended or (tag == "TY" and started):
                return None
            started = True
            fields.setdefault(tag, []).append(value)
            last_tag = tag
            if tag == "ER":
                ended = True
            continue
        if not started:
            # JSTOR prepends three human-readable transport lines before RIS.
            continue
        if not line.strip():
            continue
        if not ended and line[:1].isspace() and last_tag is not None:
            fields[last_tag][-1] = f"{fields[last_tag][-1]} {line.strip()}"
            continue
        return None
    if not started or not ended or fields.get("TY") != ["JOUR"] or len(fields.get("UR") or []) != 1:
        return None
    return fields


def _canonical_url_matches(value: str | None, stable_id: str) -> bool:
    parsed = _stable_id_from_url(value)
    return bool(parsed and parsed[1] == stable_id and parsed[0] == stable_id)


def _title(fields: dict[str, list[str]]) -> str | None:
    primary = _clean((fields.get("TI") or [None])[0])
    secondary = _clean((fields.get("T1") or [None])[0])
    if primary and secondary and primary.casefold() != secondary.casefold():
        return f"{primary}: {secondary}"
    return primary or secondary


def _authors(fields: dict[str, list[str]]) -> list[str]:
    return [author for author in (_clean(value) for value in fields.get("AU") or []) if author]


def _author_family(author: str) -> str:
    return author.split(",", 1)[0].strip() or author.rsplit(" ", 1)[-1].strip()


def _year(fields: dict[str, list[str]]) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", " ".join(fields.get("PY") or []))
    return int(match.group(0)) if match else None


def _first(fields: dict[str, list[str]], tag: str) -> str | None:
    return _clean((fields.get(tag) or [None])[0])


def _unverified(reason: str) -> dict:
    return {
        "status": "unverified", "via": NAME, "reason": reason,
        "resolution_basis": "jstor_stable_identifier", "existence_confidence": "low",
    }


def discover(ref: dict) -> dict | None:
    """Resolve an explicit JSTOR stable record through authoritative RIS metadata."""
    from core.resolve import service as resolve_mod

    reference = _stable_reference(ref)
    if reference is None:
        return None
    endpoint_id, stable_id = reference
    try:
        request_url = _RIS_URL.format(urllib.parse.quote(endpoint_id, safe="/"))
        status, body, final_url = resolve_mod._get_with_final_url(
            request_url,
            accept="text/plain",
        )
    except Exception as exc:
        return {
            "status": "unresolved", "via": NAME,
            "reason": f"JSTOR RIS request failed: {type(exc).__name__}",
            "resolution_basis": "jstor_stable_identifier",
        }
    if str(status) != "200":
        return {
            "status": "unresolved", "via": NAME,
            "reason": f"JSTOR RIS returned HTTP {status}",
            "resolution_basis": "jstor_stable_identifier",
        }
    if final_url != request_url:
        return _unverified("JSTOR RIS redirected away from the authoritative record endpoint")
    fields = _parse_ris(body)
    if fields is None:
        return _unverified("JSTOR RIS response was malformed or not a journal record")
    if not _canonical_url_matches((fields.get("UR") or [None])[0], stable_id):
        return _unverified("JSTOR RIS record did not return the cited stable identifier")
    title = _title(fields)
    if title is None:
        return _unverified("JSTOR RIS record has no usable title")
    authors = _authors(fields)
    year = _year(fields)
    metadata = {
        "title": [title],
        "author": [{"family": _author_family(author)} for author in authors],
        "container-title": [_first(fields, "T2")] if _first(fields, "T2") else [],
        "volume": _first(fields, "VL"),
        "issue": _first(fields, "IS"),
    }
    start_page = _first(fields, "SP")
    end_page = _first(fields, "EP")
    if start_page:
        metadata["page"] = f"{start_page}-{end_page}" if end_page else start_page
    if year is not None:
        metadata["published"] = {"date-parts": [[year]]}
    profile = resolve_mod._metadata_match_profile(ref, metadata, title)
    landing_url = _LANDING_URL.format(stable_id)
    return {
        "status": "resolved",
        "via": NAME,
        "record_id": stable_id,
        "matched_title": title,
        "matched_authors": authors,
        "matched_year": year,
        "abstract": _clean((fields.get("AB") or [None])[0]),
        "reason": "JSTOR RIS metadata confirmed the cited stable identifier",
        "resolution_basis": "jstor_stable_identifier",
        "existence_confidence": "high",
        "metadata_match": profile,
        "identifiers": {"jstor": stable_id},
        "resolved_identifier": {"type": "jstor", "value": stable_id, "validated_via": NAME},
        "retracted": False,
        # A JSTOR landing record proves identity only. It does not attest that
        # the article text is present or open to this caller.
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": "journal article",
        "fulltext_links": [{
            "url": landing_url,
            "content_type": "text/html",
            "identity_context": {
                "provider": NAME,
                "canonical_host": True,
                "canonical_url": landing_url,
                "provider_record_id": stable_id,
                "title": title,
                "identifiers": {"jstor": stable_id},
            },
        }],
    }
