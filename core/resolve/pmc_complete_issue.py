#!/usr/bin/env python3
# core/resolve/pmc_complete_issue.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent PMC issue holdings inventories for full-participation journals."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
from datetime import datetime, timezone

try:
    from .journal_authority import _text_key
    from .matching import _author_names_equivalent
except ImportError:  # direct execution
    from resolve.journal_authority import _text_key
    from resolve.matching import _author_names_equivalent


NAME = "pmc_complete_issue"
RULE_VERSION = "pmc-issue-inventory/v4"
JOURNAL_LIST_URL = "https://cdn.ncbi.nlm.nih.gov/pmc/home/jlist.csv"
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
MAX_MEMBERS = 1000
SUMMARY_BATCH_SIZE = 200

_JOURNAL_FIELDS = {
    "Journal Title", "NLM Title Abbreviation (TA)", "Publisher",
    "ISSN (print)", "ISSN (online)", "NLM Unique ID", "Most Recent",
    "Earliest", "Release Delay (Embargo)", "Agreement Status",
    "Agreement to Deposit", "Journal Note", "PMC URL",
}
_journal_list_lock = threading.Lock()
_journal_list_cache: tuple[str, list[dict[str, str]]] | None = None


def _resolve_module():
    try:
        from core.resolve import service as resolve_mod
    except ImportError:  # direct execution
        from resolve import service as resolve_mod

    return resolve_mod


def _enabled(*, environ=None) -> bool:
    try:
        from core.resolve import provider_config
    except ImportError:  # direct execution
        from resolve import provider_config

    config = provider_config.load(environ=environ)
    return bool((config.get("providers", {}).get(NAME) or {}).get("enabled", True))


def reset_cache() -> None:
    global _journal_list_cache
    with _journal_list_lock:
        _journal_list_cache = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coordinate(ref: dict, kind: str) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value") or item.get("raw_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _empty_inventory(ref: dict) -> dict:
    return {
        "rule_version": RULE_VERSION,
        "provider": NAME,
        "status": "not_applicable",
        "reason": "citation lacks a supported journal volume and numeric issue",
        "scope": None,
        "target_status": "inconclusive",
        "target_member_order": None,
        "cited_container": _coordinate(ref, "container"),
        "cited_volume": _coordinate(ref, "volume"),
        "cited_issue": _coordinate(ref, "issue"),
        "journal_title": None,
        "journal_abbreviation": None,
        "nlm_unique_id": None,
        "issn": None,
        "agreement_status": None,
        "agreement_to_deposit": None,
        "coverage_earliest": None,
        "coverage_latest": None,
        "journal_note": None,
        "journal_list_url": JOURNAL_LIST_URL,
        "journal_list_sha256": None,
        "query_url": None,
        "query_sha256": None,
        "reported_count": None,
        "completeness_basis": None,
        "members": [],
    }


def _journal_rows() -> tuple[str, list[dict[str, str]]]:
    global _journal_list_cache
    with _journal_list_lock:
        if _journal_list_cache is not None:
            return _journal_list_cache
        status, body = _resolve_module()._get(JOURNAL_LIST_URL, accept="text/csv")
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"PMC journal list returned HTTP {status}")
        if not isinstance(body, str) or len(body.encode("utf-8")) > 20_000_000:
            raise ValueError("PMC journal list response is missing or oversized")
        reader = csv.DictReader(io.StringIO(body.lstrip("\ufeff")))
        if reader.fieldnames is None or not _JOURNAL_FIELDS.issubset(reader.fieldnames):
            raise ValueError("PMC journal list field set is incomplete")
        rows = [{key: str(row.get(key) or "").strip() for key in _JOURNAL_FIELDS} for row in reader]
        if not rows:
            raise ValueError("PMC journal list is empty")
        _journal_list_cache = (_sha256(body), rows)
        return _journal_list_cache


def _coverage_boundary(value: str) -> tuple[int, int | None, int] | None:
    match = re.fullmatch(r"v\.\s*(\d+)(?:\((\d+)\))?\s+((?:18|19|20)\d{2})", value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None, int(match.group(3))


def _coverage_contains(row: dict[str, str], *, volume: str, issue: str | None, year: int) -> bool:
    if not volume.isdigit() or issue is None or not issue.isdigit():
        return False
    earliest = _coverage_boundary(row["Earliest"])
    latest = _coverage_boundary(row["Most Recent"])
    if earliest is None or latest is None or earliest[1] is None or latest[1] is None:
        return False
    cited = (int(volume), int(issue), year)
    if not earliest[2] <= year <= latest[2]:
        return False
    cited_scope = cited[0], cited[1]
    earliest_scope = earliest[0], earliest[1]
    latest_scope = latest[0], latest[1]
    if cited_scope < earliest_scope or cited_scope >= latest_scope:
        return False
    embargo_match = re.match(r"(\d+)\s+months?", row["Release Delay (Embargo)"].casefold())
    embargo_months = int(embargo_match.group(1)) if embargo_match else 0
    if embargo_months:
        conservative_year = datetime.now(timezone.utc).year - ((embargo_months + 11) // 12) - 1
        if year > conservative_year:
            return False
    return True


def _journal_match(ref: dict, rows: list[dict[str, str]]) -> tuple[dict[str, str] | None, str]:
    container = _coordinate(ref, "container")
    if not container:
        return None, "citation has no journal container"
    cited_key = _text_key(container)
    matches = [
        row for row in rows
        if cited_key in {
            _text_key(row["Journal Title"]),
            _text_key(row["NLM Title Abbreviation (TA)"]),
        }
    ]
    if len(matches) != 1:
        return None, (
            "journal is absent from the PMC full-participation list"
            if not matches else "journal alias is ambiguous in the PMC journal list"
        )
    return matches[0], ""


def _article_ids(value: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict):
            raise ValueError("PMC summary article identifier is malformed")
        kind = str(item.get("idtype") or "").strip().lower()
        identifier = str(item.get("value") or "").strip()
        if not kind or not identifier or kind in out:
            raise ValueError("PMC summary article identifiers are incomplete or duplicated")
        out[kind] = identifier
    return out


def _member(record_id: str, value: object, *, volume: str, issue: str | None) -> dict:
    if not isinstance(value, dict) or str(value.get("uid") or "") != record_id:
        raise ValueError("PMC summary record identity is inconsistent")
    title = str(value.get("title") or "").strip()
    record_volume = str(value.get("volume") or "").strip()
    record_issue = str(value.get("issue") or "").strip() or None
    if not title or record_volume != volume or (issue is not None and record_issue != issue):
        raise ValueError("PMC summary member is outside the requested inventory")
    ids = _article_ids(value.get("articleids"))
    pmcid = ids.get("pmcid")
    if pmcid != f"PMC{record_id}":
        raise ValueError("PMC summary member lacks its canonical PMCID")
    authors = value.get("authors")
    first_author = None
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            first_author = str(first.get("name") or "").strip() or None
    pubdate = str(value.get("pubdate") or "")
    year_match = re.search(r"\b(?:18|19|20)\d{2}\b", pubdate)
    return {
        "record_id": record_id,
        "pmcid": pmcid,
        "pmid": ids.get("pmid"),
        "doi": ids.get("doi"),
        "title": title,
        "first_author": first_author,
        "year": int(year_match.group(0)) if year_match else None,
        "journal": str(value.get("fulljournalname") or value.get("source") or "").strip() or None,
        "volume": record_volume,
        "issue": record_issue,
        "locator": str(value.get("pages") or "").strip() or None,
    }


def _target_status(ref: dict, members: list[dict]) -> tuple[str, int | None]:
    resolve_mod = _resolve_module()
    cited_locator = (
        _coordinate(ref, "article_page_range")
        or _coordinate(ref, "elocator")
        or _coordinate(ref, "article_number")
        or _coordinate(ref, "article_locator")
    )
    borderline = False
    locator_owners: list[int] = []
    candidates: list[tuple[tuple[int, float], int]] = []
    for order, item in enumerate(members):
        msg = {
            "title": [item["title"]],
            "author": [{"family": item["first_author"]}] if item["first_author"] else [],
            "published": {"date-parts": [[item["year"]]]} if item["year"] else {},
            "container-title": [item["journal"]] if item["journal"] else [],
            "volume": item["volume"],
            "issue": item["issue"],
            "page": item["locator"],
        }
        profile = resolve_mod._metadata_match_profile(ref, msg, item["title"])
        overlap = profile.get("title_overlap")
        cited_author = ref.get("ay_surname")
        author_match = profile.get("author_match") is True or bool(
            cited_author
            and item["first_author"]
            and _author_names_equivalent(str(cited_author), item["first_author"])
        )
        comparisons = profile.get("coordinate_comparisons") or []
        locator_match = any(
            comparison.get("kind") in {
                "article_page_range", "elocator", "article_number", "article_locator",
            }
            and comparison.get("status") == "match"
            for comparison in comparisons
        )
        if (
            not profile.get("ordinal_conflict")
            and isinstance(overlap, (int, float))
            and (
                overlap >= 0.95
                or (overlap >= 0.85 and (not cited_author or author_match))
            )
            and (not ref.get("year") or profile.get("year_match") is True)
        ):
            strong_fields = int(bool(cited_author) and author_match)
            strong_fields += int(bool(cited_locator) and locator_match)
            candidates.append(((strong_fields, float(overlap)), order))
        borderline = borderline or bool(isinstance(overlap, (int, float)) and overlap >= 0.50)
        if cited_locator and item["locator"] and locator_match:
            locator_owners.append(order)
    if candidates:
        best_rank = max(rank for rank, _order in candidates)
        best_orders = [order for rank, order in candidates if rank == best_rank]
        return ("present", best_orders[0]) if len(best_orders) == 1 else ("inconclusive", None)
    if borderline:
        return "inconclusive", None
    if len(locator_owners) > 1:
        return "inconclusive", None
    return "absent", locator_owners[0] if locator_owners else None


def complete_issue_inventory(ref: dict) -> dict:
    """Return an exact PMC holdings enumeration or a fail-closed result."""
    out = _empty_inventory(ref)
    if not _enabled():
        out["reason"] = "PMC issue holdings inventory is disabled"
        return out
    container = out["cited_container"]
    volume = out["cited_volume"]
    issue = out["cited_issue"]
    if not container or not volume or not issue or not ref.get("year"):
        return out
    try:
        year = int(ref["year"])
    except (TypeError, ValueError):
        return out
    out["scope"] = "issue"
    try:
        journal_sha, rows = _journal_rows()
        out["journal_list_sha256"] = journal_sha
        row, reason = _journal_match(ref, rows)
        if row is None:
            out["reason"] = reason
            return out
        out.update({
            "journal_title": row["Journal Title"],
            "journal_abbreviation": row["NLM Title Abbreviation (TA)"],
            "nlm_unique_id": row["NLM Unique ID"],
            "issn": row["ISSN (print)"] or row["ISSN (online)"],
            "agreement_status": row["Agreement Status"],
            "agreement_to_deposit": row["Agreement to Deposit"],
            "coverage_earliest": row["Earliest"],
            "coverage_latest": row["Most Recent"],
            "journal_note": row["Journal Note"] or None,
        })
        if row["Agreement Status"] != "Active":
            out.update(reason="PMC participation agreement is not active")
            return out
        if row["Agreement to Deposit"] != "All articles":
            out.update(reason="PMC agreement does not cover all articles")
            return out
        if row["Journal Note"]:
            out.update(reason="PMC journal record declares a coverage note")
            return out
        if not _coverage_contains(row, volume=volume, issue=issue, year=year):
            out.update(reason="citation issue is outside the conservative PMC lookup window")
            return out
        if not out["issn"]:
            out.update(reason="PMC journal record has no ISSN")
            return out

        term = f'{out["issn"]}[journal] AND {volume}[volume]'
        term += f" AND {issue}[issue]"
        query_url = _resolve_module()._ncbi_url(ESEARCH_URL, {
            "db": "pmc", "retmode": "json", "retmax": str(MAX_MEMBERS),
            "retstart": "0", "term": term,
        })
        out["query_url"] = query_url
        status, search_body = _resolve_module()._get(query_url)
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"PMC issue search returned HTTP {status}")
        out["query_sha256"] = _sha256(search_body)
        search = json.loads(search_body).get("esearchresult") or {}
        count = int(search.get("count"))
        ids = [str(value) for value in search.get("idlist") or []]
        out["reported_count"] = count
        if count < 1 or count > MAX_MEMBERS or int(search.get("retstart") or 0) != 0:
            raise ValueError("PMC issue search count is outside the complete-inventory boundary")
        if len(ids) != count or len(ids) != len(set(ids)) or any(not item.isdigit() for item in ids):
            raise ValueError("PMC issue search did not enumerate its reported count exactly")
        translation = " ".join(
            str(item.get("to") or "")
            for item in search.get("translationset") or []
            if isinstance(item, dict)
        )
        if f"__jid{out['nlm_unique_id']}" not in translation:
            raise ValueError("PMC issue search did not bind the expected NLM journal identity")

        response_parts = [search_body]
        members: list[dict] = []
        for start in range(0, len(ids), SUMMARY_BATCH_SIZE):
            batch = ids[start:start + SUMMARY_BATCH_SIZE]
            summary_url = _resolve_module()._ncbi_url(ESUMMARY_URL, {
                "db": "pmc", "retmode": "json", "id": ",".join(batch),
            })
            summary_status, summary_body = _resolve_module()._get(summary_url)
            if not 200 <= int(summary_status) < 300:
                raise RuntimeError(f"PMC issue summary returned HTTP {summary_status}")
            response_parts.append(summary_body)
            out["query_sha256"] = _sha256("\n".join(response_parts))
            result = json.loads(summary_body).get("result") or {}
            if [str(value) for value in result.get("uids") or []] != batch:
                raise ValueError("PMC issue summary omitted or reordered members")
            members.extend(_member(record_id, result.get(record_id), volume=volume, issue=issue) for record_id in batch)
        if len(members) != count:
            raise ValueError("PMC issue member count does not match search count")
        target_status, target_member_order = _target_status(ref, members)
        if target_status == "absent":
            # Full Participation is a deposit commitment, not independent proof
            # that PMC's holdings are a closed inventory of this issue.  The
            # exact query remains useful to identify an enumerated member, but
            # a miss must never become absence evidence on its own.
            target_status = "inconclusive"
            target_member_order = None
        out.update({
            "status": "enumerated",
            "reason": "PMC full-participation issue holdings were enumerated exactly",
            "target_status": target_status,
            "target_member_order": target_member_order,
            "query_sha256": out["query_sha256"],
            "completeness_basis": (
                "PMC Full Participation Active All articles holdings query; ESearch count "
                "equals unique ESummary members bound to the historical issue scope"
            ),
            "members": members,
        })
        return out
    except Exception as exc:
        out.update(status="incomplete", reason=f"{type(exc).__name__}: {exc}")
        out["target_status"] = "inconclusive"
        out["target_member_order"] = None
        out["members"] = []
        out["completeness_basis"] = None
        return out
