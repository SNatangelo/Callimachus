# core/resolve/user_sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Safe ingestion of user supplied full texts and abstracts.

This module is deliberately separate from :mod:`core.parse`: an HTML abstract is
metadata/landing-page input, not a document-format parser input.  It never imports
or executes page JavaScript and never makes network requests.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

from core.fetch.storage import content_store
from . import sources

TIERS = ("fulltext", "abstract")
OUTCOMES = (
    "accepted_abstract", "accepted_fulltext", "duplicate", "identity_mismatch",
    "abstract_section_missing", "challenge_or_login_page", "insufficient_identity",
    "needs_manual_confirmation", "unreadable",
)


def ensure_user_source_dirs(run_dir: str | os.PathLike[str]) -> dict[str, str]:
    """Create and return the user-facing tier directories (idempotently)."""
    root = Path(run_dir) / "user_sources"
    result = {tier: str(root / ("fulltexts" if tier == "fulltext" else "abstracts"))
              for tier in TIERS}
    for path in result.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    return result


def _provided_dir(run_dir: str, tier: str) -> Path:
    name = "fulltexts" if tier == "fulltext" else "abstracts"
    nested = Path(run_dir) / "sources" / "provided" / name
    nested.mkdir(parents=True, exist_ok=True)
    return nested


def archive_original(run_dir: str, path: str, *, ref: dict, tier: str) -> dict:
    """Archive a supplied original in its current tier folder."""
    nested = _provided_dir(run_dir, tier)
    base = f"{ref.get('ref_number', ref.get('id'))}_{os.path.basename(path)}"
    nested_path = nested / base
    if not nested_path.exists():
        shutil.copyfile(path, nested_path)
    return {
        "stored_as": str(nested_path.relative_to(run_dir)).replace(os.sep, "/")
    }


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().lower()


def _plain_text(value: object) -> str:
    """Drop markup/script payloads from metadata supplied as HTML fragments."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _challenge_marker(text: str) -> bool:
    """Recognize strong access-gate language, not ordinary prose mentioning access."""
    lowered = _norm(text)
    return bool(re.search(
        r"\b(?:access\s+denied|please\s+(?:log\s*in|sign\s+in)|"
        r"(?:log|sign)\s+in\s+to\s+(?:continue|access)|captcha|"
        r"verify\s+you\s+are\s+human|enable\s+javascript|checking\s+your\s+browser|"
        r"security\s+check|unusual\s+traffic|cloudflare)\b",
        lowered,
    ))


def _visible_raw_html(raw: str) -> str:
    """Return visible HTML text before structural sanitization.

    Page chrome is intentionally retained here: a challenge in a header or nav
    must win, while executable/style payloads must not be treated as visible.
    """
    without_payloads = re.sub(
        r"(?is)<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>",
        " ", raw,
    )
    return re.sub(r"(?s)<[^>]*>", " ", without_payloads)


def _doi(value: object) -> str | None:
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", _norm(value), re.I)
    return match.group(0).rstrip(".,;)").lower() if match else None


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.jsonld: list[str] = []
        self._stack: list[str] = []
        self._skip_stack: list[bool] = []
        self._skip = 0
        self._json = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower(); attrs = {str(k).lower(): str(v or "") for k, v in attrs}
        self._stack.append(tag)
        chrome = (attrs.get("class", "") + " " + attrs.get("id", "")).lower()
        chrome_marker = re.search(r"(?:cookie|consent|paywall|login|toolbar|sidebar|chrome|banner)", chrome)
        marked_skip = tag in {"script", "style", "nav", "footer", "form", "aside", "header"} or bool(chrome_marker)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._skip_stack.append(marked_skip)
        if marked_skip:
            self._skip += 1
        if tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._json = True
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or attrs.get("itemprop") or "").lower()
            if key and attrs.get("content"):
                self.meta.setdefault(key, []).append(attrs["content"])

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "script" and self._json:
            self._json = False
        if self._skip_stack and self._skip_stack.pop() and self._skip:
            self._skip -= 1
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._json:
            self.jsonld.append(data)
        elif not self._skip:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(x.strip() for x in self._parts if x.strip())).strip()


def _jsonld_values(parser: _HTML) -> dict:
    values = {}
    for raw in parser.jsonld:
        try:
            item = json.loads(raw.strip())
        except (TypeError, ValueError):
            continue
        items = item if isinstance(item, list) else [item]
        for obj in items:
            if isinstance(obj, dict):
                for key in ("abstract", "name", "headline", "datePublished", "author", "identifier"):
                    if key in obj and key not in values:
                        values[key] = obj[key]
    return values


def _explicit_abstract(parser: _HTML, values: dict) -> tuple[str | None, str | None]:
    for key in ("citation_abstract", "dc.description.abstract", "dcterms.abstract"):
        if parser.meta.get(key):
            return _plain_text(parser.meta[key][0]), key
    if values.get("abstract"):
        return _plain_text(values["abstract"]), "jsonld_abstract"
    text = parser.text
    match = re.search(r"(?im)^\s*abstract\s*[:\-]?\s*$", text)
    if not match:
        match = re.search(r"(?is)\babstract\s*[:\-]\s+", text)
    if not match:
        return None, None
    tail = text[match.end():]
    # Background/Methods/Results/Conclusions are common structured-abstract
    # subsections and must remain part of the abstract.  Introduction and later
    # headings mark the article body; keywords and references are never abstract.
    boundary = re.search(r"(?im)^\s*(?:keywords?|introduction|references?|bibliography)\s*[:\-]?\s*$", tail)
    abstract = tail[:boundary.start()] if boundary else tail
    return abstract.strip(), "abstract_section"


def extract_abstract_section(text: str) -> tuple[str | None, str | None]:
    """Extract an explicitly headed abstract from copied TXT/Markdown text.

    A bare abstract is intentionally not accepted: without a heading there is no
    safe way to distinguish it from an arbitrary note or a truncated page.
    """
    text = html.unescape(text or "")
    match = re.search(r"(?im)^\s{0,3}(?:#{1,6}\s*)?abstract\s*[:\-]?\s*$", text)
    if not match:
        return None, None
    tail = text[match.end():]
    boundary = re.search(
        r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:keywords?|introduction|references?|bibliography)\s*[:\-]?\s*$",
        tail,
    )
    return (tail[:boundary.start()] if boundary else tail).strip(), "abstract_section"


def _meta_identity(parser: "_HTML", values: dict | None = None) -> dict:
    """Build an identity dict (doi/pmid/title/year/author) from a page's <meta>
    tags and JSON-LD, independent of whether an abstract section was found.

    Shared by the abstract-tier extractor below and by :func:`html_meta_identity`
    (the fulltext-tier path), so both tiers read a publisher's declared
    citation_doi/citation_pmid the same way."""
    values = _jsonld_values(parser) if values is None else values
    json_identifier = values.get("identifier")
    if isinstance(json_identifier, dict):
        json_identifier = json_identifier.get("value")
    json_author = values.get("author")
    if isinstance(json_author, list):
        json_author = " ".join(
            str(item.get("family") or item.get("name") or item) if isinstance(item, dict) else str(item)
            for item in json_author
        )
    elif isinstance(json_author, dict):
        json_author = json_author.get("family") or json_author.get("name")
    identity = {
        "doi": _doi((parser.meta.get("citation_doi") or [json_identifier or None])[0]),
        "pmid": (parser.meta.get("citation_pmid") or parser.meta.get("pmid") or [None])[0],
        "title": (parser.meta.get("citation_title") or [values.get("name") or values.get("headline") or ""])[0],
        "year": None,
        "author": " ".join(parser.meta.get("citation_author") or []) or str(json_author or ""),
    }
    date = (parser.meta.get("citation_publication_date") or [values.get("datePublished") or ""])[0]
    year = re.search(r"\b(19|20)\d{2}\b", str(date))
    identity["year"] = int(year.group(0)) if year else None
    return identity


def html_meta_identity(path: str) -> dict:
    """Extract declared identity (doi/pmid/title/year/author) from an HTML file's
    <meta> tags and JSON-LD, without requiring an explicit abstract section.

    Used by the FULLTEXT ingest/map path: a publisher's citation_doi/citation_pmid
    meta tags are the authoritative identity even when the canonical article body
    text (the manuscript extractor's stdlib HTML->text conversion) never repeats
    the DOI in its prose. Returns {} for an unreadable file rather than raising —
    callers treat a missing identity as "nothing declared", not as an error."""
    try:
        raw = Path(path).read_bytes()
        decoded = raw.decode("utf-8", errors="replace")
        parser = _HTML(); parser.feed(decoded); parser.close()
    except (OSError, UnicodeError, ValueError):
        return {}
    return _meta_identity(parser)


def extract_abstract_html(path: str) -> dict:
    """Return sanitized abstract and identity metadata from an HTML file."""
    try:
        raw = Path(path).read_bytes()
        decoded = raw.decode("utf-8", errors="replace")
        if _challenge_marker(_visible_raw_html(decoded)):
            return {"outcome": "challenge_or_login_page", "identity": {}}
        parser = _HTML(); parser.feed(decoded); parser.close()
    except (OSError, UnicodeError, ValueError) as exc:
        return {"outcome": "unreadable", "error": str(exc)}
    if _challenge_marker(parser.text):
        return {"outcome": "challenge_or_login_page", "identity": {}}
    values = _jsonld_values(parser)
    abstract, signal = _explicit_abstract(parser, values)
    if abstract and _challenge_marker(abstract):
        return {"outcome": "challenge_or_login_page", "identity": {}}
    identity = _meta_identity(parser, values)
    abstract_marker = re.search(r"(?im)^\s*abstract\s*[:\-]?\s*$", parser.text)
    identity["identity_text"] = parser.text[:abstract_marker.start()] if abstract_marker else ""
    if not abstract:
        return {"outcome": "abstract_section_missing", "identity": identity}
    return {"outcome": "ok", "text": abstract, "identity": identity, "signal": signal}


def _identifier_in_body(text: str, identifier: str, *, bibliography_only: bool = True) -> bool:
    if not identifier:
        return False
    # HTML-to-text extraction may put the heading after the preceding paragraph
    # rather than on a fresh line; treat either layout as the bibliography boundary.
    before_refs = re.split(r"(?i)\b(?:references|bibliography)\b", text, maxsplit=1)[0]
    return identifier.lower() in (before_refs if bibliography_only else text).lower()


def corroborate_user(ref: dict, text: str, metadata: dict | None = None) -> tuple[str | None, float, str | None]:
    """Conservative user identity gate; returns signal, score, rejection reason."""
    metadata = metadata or {}
    ref_doi = _doi(ref.get("doi")); found_doi = _doi(metadata.get("doi") or text)
    if ref_doi and found_doi and ref_doi != found_doi:
        return "doi", 0.0, "identity_mismatch"
    if ref_doi and found_doi == ref_doi and (
        metadata.get("doi") or _identifier_in_body(text, ref_doi)
    ):
        return "doi", 1.0, None
    ref_pmid = _norm(ref.get("pmid")); found_pmid = _norm(metadata.get("pmid"))
    if ref_pmid and found_pmid and ref_pmid == found_pmid:
        return "pmid", 1.0, None
    # Never use a DOI appearing only after a bibliography boundary as corroboration.
    identity_text = " ".join(
        [text, str(metadata.get("identity_text") or ""),
         str(metadata.get("title") or ""), str(metadata.get("author") or ""),
         str(metadata.get("year") or "")]
    )
    title_candidates = [ref.get("title") or ref.get("raw_entry") or ""]
    title_candidates.extend(metadata.get("alternate_titles") or [])
    if metadata.get("title"):
        title_candidates.append(metadata["title"])
    body = re.split(r"(?i)\b(?:references|bibliography)\b", identity_text, maxsplit=1)[0]
    got = sources._tokens(body)
    overlap = max(
        (len(set(sources._tokens(title)) & got) / max(len(sources._tokens(title)), 1)
         for title in title_candidates if title),
        default=0.0,
    )
    author = str(ref.get("ay_surname") or "").casefold()
    year = str(ref.get("year") or "")
    author_ok = not author or bool(re.search(rf"(?<!\w){re.escape(author)}(?!\w)", body, re.I))
    year_ok = not year or re.search(rf"(?<!\d){re.escape(year)}(?!\d)", body)
    if overlap >= 0.75 and author_ok and year_ok:
        return "title_author_year", round(overlap, 3), None
    if overlap >= 0.55 or (author_ok and year_ok):
        return "tokens", round(overlap, 3), "needs_manual_confirmation"
    return "tokens", round(overlap, 3), "insufficient_identity"


def identity_view(ref: dict, resolve_result: dict | None = None) -> dict:
    """Overlay identifiers/title/year validated by the repository resolver."""
    view = dict(ref or {})
    rr = resolve_result or {}
    for key in ("canonical_doi", "canonical_pmid", "doi", "pmid"):
        if rr.get(key) and not view.get(key.replace("canonical_", "")):
            view[key.replace("canonical_", "")] = rr[key]
    ident = rr.get("resolved_identifier") or {}
    if isinstance(ident, dict) and ident.get("value"):
        typ = str(ident.get("type") or "").lower()
        if typ == "doi" and not view.get("doi"):
            view["doi"] = ident["value"]
        elif typ == "pmid" and not view.get("pmid"):
            view["pmid"] = ident["value"]
    for validation in rr.get("identifier_validations") or []:
        if not isinstance(validation, dict):
            continue
        value = validation.get("value") or validation.get("identifier")
        typ = str(validation.get("type") or validation.get("scheme") or "").lower()
        if value and typ == "doi" and not view.get("doi"):
            view["doi"] = value
        if value and typ == "pmid" and not view.get("pmid"):
            view["pmid"] = value
    if not view.get("doi"):
        for item in (rr.get("fulltext_links") or []) + (rr.get("auxiliary_fulltext_links") or []):
            value = item.get("url") if isinstance(item, dict) else item
            doi = _doi(value)
            if doi:
                view["doi"] = doi
                break
    basis = str((rr.get("evidence_profile") or {}).get("resolution_basis") or "").casefold()
    has_identifier = bool(
        rr.get("canonical_doi") or rr.get("canonical_pmid") or rr.get("doi") or rr.get("pmid")
        or rr.get("resolved_identifier") or rr.get("identifier_validations")
    )
    high_confidence_basis = basis in {"identifier", "metadata_search", "high_confidence", "validated"}
    if rr.get("matched_title") and rr.get("status") == "resolved" and (has_identifier or high_confidence_basis):
        view.setdefault("alternate_titles", []).append(rr["matched_title"])
    evidence = rr.get("evidence_profile") or {}
    for candidate in (evidence.get("metadata_match") or {}, evidence.get("best_candidate") or {}):
        if not view.get("year") and candidate.get("matched_year"):
            view["year"] = candidate["matched_year"]
    return view


def read_user_source(path: str, *, tier: str) -> dict:
    """Read one supplied source and return sanitized text plus identity metadata."""
    ext = Path(path).suffix.lower()
    if tier == "abstract" and ext in {".html", ".htm"}:
        return extract_abstract_html(path)
    try:
        raw_text, _fmt, _ = __import__("core.parse.extract", fromlist=["extract_text"]).extract_text(path)
    except Exception as exc:
        return {"outcome": "unreadable", "error": str(exc)}
    if tier == "abstract" and ext in {".txt", ".md", ".markdown"}:
        if _challenge_marker(raw_text):
            return {"outcome": "challenge_or_login_page"}
        text, signal = extract_abstract_section(raw_text)
        if not text:
            return {"outcome": "abstract_section_missing"}
        header = re.split(r"(?i)\b(?:references|bibliography)\b", raw_text, maxsplit=1)[0]
        pmid_match = re.search(r"(?i)\bpmid\s*[:#]?\s*(\d+)\b", header)
        return {"outcome": "ok", "text": text, "identity": {
            "section_signal": signal, "doi": _doi(header),
            "pmid": pmid_match.group(1) if pmid_match else None,
            "identity_text": header,
        }}
    return {"outcome": "ok", "text": raw_text, "identity": {}}


def ingest_file(
    run_dir: str,
    ref: dict,
    path: str,
    *,
    tier: str = "fulltext",
    supplied_by: str = "user",
    supplied_via: str | None = None,
    source_ref: str | None = None,
    identity_attested: bool = False,
) -> dict:
    """Admit one explicit user source through the tier-specific identity gate.

    ``supplied_via`` and ``source_ref`` are retained as immutable provenance for
    controlled callers such as guided Fetch.  They do not affect corroboration.
    """
    if tier not in TIERS:
        raise ValueError(f"unsupported user tier: {tier}")
    if type(identity_attested) is not bool:
        raise ValueError("identity_attested must be boolean")
    ensure_user_source_dirs(run_dir)
    ext = Path(path).suffix.lower()
    parsed = read_user_source(path, tier=tier)
    if parsed.get("outcome") != "ok":
        return {"outcome": parsed.get("outcome", "unreadable"), "file": path,
                "ref_id": ref.get("id"), "ref_number": ref.get("ref_number"),
                **({"error": parsed["error"]} if parsed.get("error") else {})}
    text, metadata = parsed["text"], parsed.get("identity") or {}
    if identity_attested and not (
        supplied_by == "user"
        and isinstance(supplied_via, str)
        and supplied_via.startswith("controlled_task_answer:")
    ):
        raise ValueError("identity attestation requires authenticated controlled task provenance")
    if identity_attested:
        signal, score, failure = "operator_confirmation", 1.0, None
    else:
        signal, score, failure = corroborate_user(ref, text, metadata)
    if failure:
        return {"outcome": failure, "file": path, "signal": signal, "score": score,
                "ref_id": ref.get("id"), "ref_number": ref.get("ref_number")}
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    manifest = sources.load_manifest(run_dir)
    if any(e.get("ref_id") == ref.get("id") and e.get("tier") == tier and e.get("sha256") == digest
           for e in manifest.get("entries", [])):
        return {"outcome": "duplicate", "file": path, "signal": signal, "score": score,
                "ref_id": ref.get("id"), "ref_number": ref.get("ref_number")}
    archived = archive_original(run_dir, path, ref=ref, tier=tier)
    try:
        flat = sources.store_provided_raw(run_dir, path, ref=ref)
        provenance_via = supplied_via or f"user_{tier}"
        entry = sources.store_text(
            run_dir, ref, tier, "user", text,
            source_ref=source_ref or flat or os.path.abspath(path),
            mapping=("operator_attested" if identity_attested else
                     "deterministic" if signal in {"doi", "pmid"} else "model_corroborated"),
            signal=signal, score=score, supplied_by=supplied_by, supplied_via=provenance_via,
            identity_status=("operator_attested" if identity_attested else "exact_identifier" if signal in {"doi", "pmid"}
                             else "externally_corroborated_text"),
            identity_note=("identity confirmed by the authenticated operator through the controlled task answer"
                           if identity_attested else "user supplied text matched the resolved DOI/PMID"
                           if signal in {"doi", "pmid"}
                           else "user supplied text corroborated by title, author and year"),
            file_format=("html" if ext in {".html", ".htm"} else None),
        )
        content_store.archive_user_original(run_dir, path, ref=ref,
                                            supplied_via=provenance_via,
                                            file_format="html" if ext in {".html", ".htm"} else None,
                                            move=False)
    except Exception as exc:
        return {"outcome": "unreadable", "file": path, "error": str(exc)}
    return {"outcome": "accepted_" + tier, "file": path, "ref_id": ref.get("id"),
            "ref_number": ref.get("ref_number"), "stored_as": entry["stored_as"],
            "provided_as": archived["stored_as"], "signal": signal, "score": score,
            "identity_status": entry.get("identity_status"),
            "identity_note": entry.get("identity_note")}


def ingest_directory(run_dir: str, ref_by_id: dict[str, dict], directory: str, *, tier: str = "fulltext",
                     auto_threshold: float = sources.AUTO_THRESHOLD) -> list[dict]:
    allowed = ({".html", ".htm", ".txt", ".md", ".markdown"} if tier == "abstract"
               else set(__import__("core.parse.extract", fromlist=["supported_extensions"]).supported_extensions()))
    refs = list((ref_by_id or {}).values())
    results = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and Path(path).suffix.lower() in allowed:
            candidates = []
            failures = []
            parsed = read_user_source(path, tier=tier)
            if parsed.get("outcome") != "ok":
                results.append({"outcome": parsed.get("outcome", "unreadable"), "file": path,
                                "ref_id": None, "ref_number": None})
                continue
            for ref in refs:
                sig, score, failure = corroborate_user(
                    ref, parsed["text"], parsed.get("identity"))
                if not failure:
                    candidates.append((score, sig, ref))
                else:
                    failures.append(failure)
            candidates.sort(key=lambda item: item[0], reverse=True)
            # A score tie does not identify which citation the file belongs to.
            # Even when tied refs share an identifier, keep the directory API
            # conservative and require explicit mapping; this avoids silently
            # duplicating one supplied source across references.
            tied = [item for item in candidates
                    if candidates and item[0] == candidates[0][0]]
            if len(tied) > 1:
                results.append({"outcome": "needs_manual_confirmation", "file": path,
                                "ref_id": None, "ref_number": None,
                                "score": candidates[0][0], "reason": "top_score_tie"})
                continue
            if not candidates or candidates[0][0] < auto_threshold:
                if not candidates and failures and len(set(failures)) == 1:
                    outcome = failures[0]
                else:
                    outcome = "needs_manual_confirmation"
                results.append({"outcome": outcome, "file": path,
                                "ref_id": candidates[0][2].get("id") if candidates else None,
                                "ref_number": candidates[0][2].get("ref_number") if candidates else None,
                                "score": candidates[0][0] if candidates else 0.0})
            else:
                results.append(ingest_file(run_dir, candidates[0][2], path, tier=tier))
    return results
