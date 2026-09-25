# core/resolve/providers/courtlistener.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""CourtListener resolver — existence confirmation for a US case by its reporter citation.

A US court opinion is cited by volume, reporter and page ("576 U.S. 644"), a stable
identifier CourtListener indexes exactly. This resolver reads that triple out of the
citation, asks the citation-lookup endpoint for it, and — when the case name the
citation claims matches the case name the database returns — confirms the opinion
exists and records its canonical URL. It does not fetch the opinion text; it answers
the prior question the article databases cannot ("is this real, and what is it?") for
a class of citations that carries no DOI and is tagged low-indexability at parse time.

Determinism: same volume/reporter/page -> same opinion. The lookup is keyed on the
triple rather than on the citation prose, so the endpoint's own text scanner never
gets to choose which citation in a footnote we meant.

Fail-closed: any non-200, unparseable body, missing token, or network error returns
None/unresolved. A citation CourtListener does not know returns `unverified`, never
`not_found` — that status reads downstream as "possible fabrication", and CourtListener's
coverage of unpublished and state trial opinions is not complete enough to support the
accusation. Absence of evidence is reported as absence of evidence.
"""

from __future__ import annotations

import json
import os
import re

try:
    from core.resolve import http as _http
except ImportError:  # direct execution
    from resolve import http as _http

NAME = "courtlistener"
CREDENTIAL_SPECS = ({
    "provider": "courtlistener",
    "env_name": "COURTLISTENER_API_TOKEN",
    "channels": ("resolve",),
    "label": "CourtListener",
},)
RESOLVE_NAME = "courtlistener"

MANIFEST = {
    "origin": "courtlistener",
    "via_aliases": ["courtlistener"],
    "canonical_hosts": ["courtlistener.com"],
}

ENV_API_TOKEN = "COURTLISTENER_API_TOKEN"
_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
_SITE = "https://www.courtlistener.com"

# A reporter citation: volume, reporter abbreviation, page. The reporter is one
# capitalised token ("U.S.", "F.3d") optionally followed by up to three more, so
# "F. Supp. 2d" and "L. Ed. 2d" survive while the run stops before the page digits
# (a bare number is neither a capitalised token nor an ordinal).
_REPORTER_TAIL = r"(?:[A-Z][A-Za-z0-9.'’]*|\d(?:d|th|st|nd))"
_CITE_RE = re.compile(
    r"\b(?P<vol>\d{1,4})\s+"
    r"(?P<rep>[A-Z][A-Za-z0-9.'’]*(?:\s+" + _REPORTER_TAIL + r"){0,3})"
    r"\s+(?P<page>\d{1,5})\b")

# The citation must name a case, not just carry a volume-reporter-page shape: a law
# review ("12 Harv. L. Rev. 100") has the same shape and is not what this resolves.
# The name is also what the match below is checked against, so without one there is
# nothing to verify against and the lookup is not worth making.
_V_RE = re.compile(r"\bv\.?\s")
_IN_RE_RE = re.compile(r"\b(?:In re|Ex parte)\s+", re.IGNORECASE)

# A party-name run: capitalised words, with the lowercase connectors a party name
# genuinely contains ("Board of Education", "Secretary of the Treasury").
_PARTY_TOKEN = r"(?:[A-Z][\w.'’&-]*|of|the|for|and|de|van|von)"
_PARTY_TAIL_RE = re.compile(r"^\s*(?P<name>" + _PARTY_TOKEN + r"(?:\s+" + _PARTY_TOKEN + r"){0,7})")

# Tokens shared by so many case names that agreement on them proves nothing. The
# check below needs a *distinctive* word in common, so these are not counted.
_GENERIC = {
    "the", "of", "and", "for", "inc", "llc", "llp", "ltd", "co", "corp", "corporation",
    "company", "et", "al", "state", "states", "united", "city", "county", "town",
    "dept", "department", "board", "commission", "commissioner", "secretary",
    "director", "administrator", "attorney", "general", "america", "american",
    "national", "federal", "government", "district", "county's", "office", "agency",
    "association", "committee", "council", "authority", "service", "services",
}


def api_token(environ: dict[str, str] | None = None) -> str | None:
    """The configured token, or None when this resolver is not set up.

    Deliberately not exposed as `enabled`/`disabled_reason`: the registry reads
    those names as a fetch capability, and this module resolves only — it has no
    candidates to hand the fetch stage. The requirement is declared instead by
    `api_key_env` in core/resolve/providers.json and documented in .env.example.
    """
    env = environ if environ is not None else os.environ
    return (env.get(ENV_API_TOKEN) or "").strip() or None


def _citation(raw_entry: str | None) -> tuple[str, str, str] | None:
    """The first reporter citation in the entry as (volume, reporter, page)."""
    match = _CITE_RE.search(raw_entry or "")
    if match is None:
        return None
    return match.group("vol"), re.sub(r"\s+", " ", match.group("rep")).strip(), match.group("page")


def _tokens(text: str | None) -> set[str]:
    """Distinctive lowercase word tokens of a case name."""
    words = re.findall(r"[A-Za-z][A-Za-z'’-]+", text or "")
    return {w.lower() for w in words if len(w) >= 3 and w.lower() not in _GENERIC}


def _claimed_parties(raw_entry: str, cite_start: int) -> str | None:
    """The case name the citation itself claims, read backwards from the citation.

    Bluebook puts the case name immediately before the reporter citation, so the
    text preceding it is where the parties are. Only the party run is taken, not
    the whole preceding sentence: a loose window would drag in surrounding prose
    and make the match check below accept almost anything.
    """
    head = raw_entry[:cite_start]
    connector = None
    for connector in _V_RE.finditer(head):
        pass
    if connector is not None:
        before = head[:connector.start()]
        # The first party is the capitalised run ending at "v.", read backwards.
        first = re.search(r"(?:" + _PARTY_TOKEN + r"\s+){0,7}" + _PARTY_TOKEN + r"[\s,]*$", before)
        second = _PARTY_TAIL_RE.match(head[connector.end():])
        return " ".join(part for part in (
            first.group(0) if first else "",
            second.group("name") if second else "",
        ) if part).strip()
    marker = None
    for marker in _IN_RE_RE.finditer(head):
        pass
    if marker is not None:
        tail = _PARTY_TAIL_RE.match(head[marker.end():])
        return tail.group("name") if tail else None
    return None


def supports(ref: dict) -> bool:
    raw = ref.get("raw_entry") or ""
    if _citation(raw) is None:
        return False
    return bool(_V_RE.search(raw) or _IN_RE_RE.search(raw))


def _lookup(volume: str, reporter: str, page: str, token: str, post_fn):
    payload = {"volume": volume, "reporter": reporter, "page": page}
    if post_fn is not None:
        return post_fn(_LOOKUP_URL, payload)
    return _http._post_json(
        _LOOKUP_URL, payload,
        headers_extra={"Authorization": f"Token {token}"})


def _unresolved(reason: str) -> dict:
    return {"status": "unresolved", "via": NAME, "reason": reason,
            "resolution_basis": "us_reporter_citation"}


def _unverified(reason: str) -> dict:
    return {"status": "unverified", "via": NAME, "reason": reason,
            "resolution_basis": "us_reporter_citation", "existence_confidence": "low"}


def discover(ref: dict, post_fn=None) -> dict | None:
    raw = ref.get("raw_entry") or ""
    match = _CITE_RE.search(raw)
    if match is None:
        return None
    volume, reporter, page = (
        match.group("vol"), re.sub(r"\s+", " ", match.group("rep")).strip(), match.group("page"))
    cite = f"{volume} {reporter} {page}"

    token = api_token()
    if token is None and post_fn is None:
        # Not configured is not a finding about the citation: stay silent rather
        # than record an attempt that says nothing. `disabled_reason` reports it.
        return None

    try:
        status, body = _lookup(volume, reporter, page, token or "", post_fn)
    except Exception:
        # A network or policy failure is a statement about us, not the opinion.
        return _unresolved("CourtListener did not answer (network or blocked)")

    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if str(status) != "200":
        return _unresolved(f"CourtListener returned HTTP {status} for {cite}")

    try:
        results = json.loads(body)
    except ValueError:
        return _unresolved("CourtListener returned an unparseable response")
    if not isinstance(results, list) or not results:
        return _unresolved("CourtListener returned no lookup result")

    entry = results[0] if isinstance(results[0], dict) else {}
    entry_status = entry.get("status")
    clusters = entry.get("clusters") or []
    if entry_status != 200 or not clusters:
        if entry_status == 400:
            return _unverified(f"{reporter} is not a reporter CourtListener indexes")
        # 404 and anything else: absent from this database, which is not the same
        # as non-existent. See the module docstring on why this is not `not_found`.
        return _unverified(f"{cite} was not found in CourtListener")

    # Confirm the case the database returned is the case the citation claimed. The
    # triple is authoritative for CourtListener, so the risk is not that it answers
    # the wrong question — it is that PDF extraction handed us the wrong volume or
    # page, in which case a real but unrelated opinion comes back and would be
    # confirmed as though it were the cited one.
    claimed = _tokens(_claimed_parties(raw, match.start()))
    if not claimed:
        return _unverified(
            f"{cite} resolves in CourtListener but the citation names no party to check it against")

    for cluster in clusters:
        name = cluster.get("case_name") or cluster.get("case_name_full")
        if not (claimed & _tokens(name)):
            continue
        url = _SITE + cluster["absolute_url"] if cluster.get("absolute_url") else None
        date_filed = cluster.get("date_filed") or ""
        return {
            "status": "resolved",
            "via": NAME,
            "matched_title": (name or "").strip() or None,
        "matched_year": date_filed[:4] if date_filed[:4].isdigit() else None,
            # The opinion URL is carried in the reason for the audit trail rather
            # than as a fulltext link: this resolver confirms existence, and the
            # fetch stage must not treat that as text it has retrieved.
            "reason": (f"US case confirmed by exact reporter citation {cite} in CourtListener"
                       + (f" ({url})" if url else "")),
            "resolution_basis": "us_reporter_citation",
            "existence_confidence": "high",
            "retracted": False,
            "fulltext_exists": False,
            "oa_status": "unknown",
            "work_type": "case",
            "identity_basis": "us_reporter_citation",
        }

    found = ", ".join(
        (c.get("case_name") or "?") for c in clusters[:3])
    return _unverified(
        f"{cite} is indexed in CourtListener as \"{found}\", which does not match the cited case")
