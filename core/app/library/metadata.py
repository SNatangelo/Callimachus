# core/app/library/metadata.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-identifier metadata lookup for Library identity checks."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from xml.etree import ElementTree as ET

from core.fetch.storage import content_store
from core.resolve.providers import arxiv, europepmc


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d+$")
_PMID_INPUT_RE = re.compile(
    r"^(?:PMID\s*:?\s*)?(\d+)$|"
    r"^https?://(?:www\.)?ncbi\.nlm\.nih\.gov/pubmed/(\d+)/?$|"
    r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?$",
    re.IGNORECASE,
)
_ISBN_INPUT_RE = re.compile(r"^(?:ISBN(?:-1[03])?\s*:?\s*)?([0-9Xx]+(?:[-\s][0-9Xx]+)*)$", re.IGNORECASE)
_ARXIV_DOI_PREFIX = "10.48550/arXiv."
_CREDENTIAL_QUERY_KEYS = {
    "api_key", "apikey", "email", "mailto", "access_token", "token",
    "password", "client_secret",
}


class LibraryMetadataError(ValueError):
    """A closed failure while resolving one explicit Library identifier."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "identity_metadata_unavailable",
        provenance: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = code
        self.provenance = dict(provenance) if provenance else None


def _resolve_module():
    from core.resolve import service as resolve_mod

    return resolve_mod


def _safe_url(value: object) -> str:
    raw = str(value or "")
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.query:
        return raw
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = urllib.parse.urlencode([
        (key, "REDACTED" if key.casefold() in _CREDENTIAL_QUERY_KEYS else item)
        for key, item in query
    ])
    return urllib.parse.urlunsplit(parsed._replace(query=safe_query))


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _canonical_doi(value: object) -> str | None:
    normalized = _resolve_module()._normalize_doi_value(
        value if isinstance(value, str) else None
    )
    if not normalized or not _DOI_RE.fullmatch(normalized):
        return None
    return normalized


def _canonical_arxiv(value: object) -> str | None:
    from core.resolve import sources

    result = sources._arxiv_identifier(value)
    if result is None:
        result = sources._arxiv_identifier_from_url(value)
    if result is None:
        result = sources._arxiv_identifier_from_doi(value)
    if result is None:
        return None
    identifier, version = result
    return identifier + (f"v{version}" if version is not None else "")


def _canonical(scheme: str, value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if scheme == "doi":
        doi = _canonical_doi(text)
        return doi.casefold() if doi else None
    if scheme == "pmid":
        return _canonical_pmid(text)
    if scheme == "pmcid":
        return europepmc._normalize_pmcid(text)
    if scheme == "arxiv_id":
        return _canonical_arxiv(text)
    if scheme == "isbn":
        return _canonical_isbn(text)
    return None


def _canonical_pmid(value: object) -> str | None:
    match = _PMID_INPUT_RE.fullmatch(str(value or "").strip())
    return next((part for part in match.groups() if part), None) if match else None


def _canonical_isbn(value: object) -> str | None:
    match = _ISBN_INPUT_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    compact = re.sub(r"[-\s]", "", match.group(1)).upper()
    return content_store._canon_isbn(compact)


def _response(url: str, accept: str, *, provider: str, scheme: str, requested: str):
    resolve_mod = _resolve_module()
    base_provenance = {
        "provider": provider,
        "scheme": scheme,
        "requested_identifier": requested,
        "request_url": _safe_url(url),
    }
    try:
        status, body, final_url = resolve_mod._get_with_final_url(url, accept=accept)
    except Exception as exc:
        raise LibraryMetadataError(
            f"{provider} metadata request failed ({type(exc).__name__})",
            provenance=base_provenance,
        ) from exc
    base_provenance["final_url"] = _safe_url(final_url or url)
    base_provenance["http_status"] = int(status) if isinstance(status, int) else None
    if status != 200:
        raise LibraryMetadataError(
            f"{provider} metadata request returned HTTP {status}",
            provenance=base_provenance,
        )
    if not isinstance(body, (bytes, str)) or not body:
        raise LibraryMetadataError(
            f"{provider} returned an empty metadata response",
            provenance=base_provenance,
        )
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    provenance = {**base_provenance, "response_sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LibraryMetadataError(
                f"{provider} metadata was not UTF-8", provenance=provenance,
            ) from exc
    return body, provenance


def _json_response(url: str, accept: str, *, provider: str, scheme: str, requested: str):
    body, provenance = _response(
        url, accept, provider=provider, scheme=scheme, requested=requested,
    )
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise LibraryMetadataError(
            f"{provider} returned malformed JSON", provenance=provenance,
        ) from exc
    if not isinstance(payload, dict):
        raise LibraryMetadataError(
            f"{provider} returned an invalid metadata record", provenance=provenance,
        )
    return payload, provenance


def _authors(values: object) -> str | None:
    if not isinstance(values, list):
        return None
    names = []
    for item in values:
        if isinstance(item, str):
            name = _clean_text(item)
        elif isinstance(item, dict):
            family = _clean_text(item.get("family") or item.get("lastName"))
            given = _clean_text(item.get("given") or item.get("firstName"))
            literal = _clean_text(
                item.get("literal") or item.get("fullName") or item.get("name")
            )
            particle = _clean_text(item.get("non-dropping-particle"))
            if family and given:
                name = f"{particle + ' ' if particle else ''}{family}, {given}"
            else:
                name = literal or family or given
        else:
            name = None
        if name:
            names.append(name)
    return "; ".join(names) or None


def _year(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1000 <= value <= 9999:
        return value
    if isinstance(value, str):
        match = re.search(r"\b\d{4}\b", value)
        return int(match.group(0)) if match else None
    return None


def _date_parts_year(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list) or not parts[0]:
        return None
    return _year(parts[0][0])


def _metadata_fields(
    *,
    title: object,
    authors: object = None,
    year: object = None,
    doi: object = None,
    pmid: object = None,
    pmcid: object = None,
    isbn: object = None,
    arxiv_id: object = None,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    cleaned_title = _clean_text(title)
    if not cleaned_title or len(cleaned_title) > 300:
        raise LibraryMetadataError(
            "the exact identifier record has no usable title", provenance=provenance,
        )
    fields: dict[str, object] = {"title": cleaned_title}
    author_text = _authors(authors) if isinstance(authors, list) else _clean_text(authors)
    if author_text:
        fields["authors"] = author_text
    normalized_year = _year(year)
    if normalized_year is not None:
        fields["year"] = normalized_year
    for scheme, raw in (
        ("doi", doi), ("pmid", pmid), ("pmcid", pmcid),
        ("isbn", isbn), ("arxiv_id", arxiv_id),
    ):
        canonical = _canonical(scheme, raw)
        if canonical:
            fields[scheme] = canonical
    return fields


def _exact(
    scheme: str,
    requested: str,
    returned: object,
    *,
    provenance: dict[str, object] | None = None,
) -> str:
    observed = _canonical(scheme, returned)
    expected = _canonical(scheme, requested)
    if not expected or not observed or observed.casefold() != expected.casefold():
        raise LibraryMetadataError(
            f"metadata response did not attest the requested {scheme.upper()}",
            code="identity_metadata_mismatch",
            provenance=provenance,
        )
    return observed


def _doi(identifier: str) -> dict[str, object]:
    requested = _canonical_doi(identifier)
    if not requested:
        raise LibraryMetadataError("enter a valid DOI", code="invalid_identity_identifier")
    url = "https://doi.org/" + urllib.parse.quote(requested, safe="/")
    record, provenance = _json_response(
        url,
        "application/vnd.citationstyles.csl+json",
        provider="doi.org",
        scheme="doi",
        requested=requested,
    )
    returned = _exact(
        "doi", requested, record.get("DOI") or record.get("doi"),
        provenance=provenance,
    )
    provenance["returned_identifier"] = returned
    fields = _metadata_fields(
        title=record.get("title"),
        authors=record.get("author"),
        year=_date_parts_year(record.get("issued"))
        or _date_parts_year(record.get("published")),
        doi=returned,
        provenance=provenance,
    )
    return {"fields": fields, "provenance": provenance}


def _pmid(identifier: str) -> dict[str, object]:
    requested = _canonical_pmid(identifier)
    if not requested or not _PMID_RE.fullmatch(requested):
        raise LibraryMetadataError("enter a valid PMID", code="invalid_identity_identifier")
    resolve_mod = _resolve_module()
    url = resolve_mod._ncbi_url(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?",
        {"db": "pubmed", "retmode": "xml", "id": requested},
    )
    body, provenance = _response(
        url,
        "application/xml,text/xml;q=0.9,*/*;q=0.8",
        provider="pubmed",
        scheme="pmid",
        requested=requested,
    )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise LibraryMetadataError("PubMed returned malformed XML", provenance=provenance) from exc
    articles = root.findall(".//PubmedArticle")
    if len(articles) != 1:
        raise LibraryMetadataError(
            "PubMed did not return exactly one article record", provenance=provenance,
        )
    returned = _exact(
        "pmid", requested, europepmc._article_pmid(articles[0]), provenance=provenance,
    )
    meta = europepmc._pubmed_batch_metadata_from_article(
        articles[0], requested, include_citation_fields=True,
    )
    if not isinstance(meta, dict):
        raise LibraryMetadataError("PubMed record could not be normalized", provenance=provenance)
    provenance["returned_identifier"] = returned
    article_ids = meta.get("article_ids") if isinstance(meta.get("article_ids"), dict) else {}
    fields = _metadata_fields(
        title=meta.get("matched_title"),
        authors=meta.get("matched_authors"),
        year=meta.get("matched_year"),
        doi=article_ids.get("doi"),
        pmid=returned,
        pmcid=article_ids.get("pmc") or article_ids.get("pmcid"),
        provenance=provenance,
    )
    return {"fields": fields, "provenance": provenance}


def _pmcid(identifier: str) -> dict[str, object]:
    requested = europepmc._normalize_pmcid(identifier)
    if not requested:
        raise LibraryMetadataError("enter a valid PMCID", code="invalid_identity_identifier")
    url = europepmc.SEARCH_URL + "?" + urllib.parse.urlencode({
        "query": f"PMCID:{requested}",
        "format": "json",
        "resultType": "core",
        "pageSize": "2",
    })
    payload, provenance = _json_response(
        url,
        "application/json",
        provider="europepmc",
        scheme="pmcid",
        requested=requested,
    )
    result_list = payload.get("resultList")
    results = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(results, list):
        raise LibraryMetadataError(
            "Europe PMC returned an invalid result list", provenance=provenance,
        )
    exact = [
        item for item in results
        if isinstance(item, dict)
        and europepmc._normalize_pmcid(item.get("pmcid")) == requested
    ]
    if len(exact) != 1:
        raise LibraryMetadataError(
            "Europe PMC did not return exactly one record for the requested PMCID",
            code="identity_metadata_mismatch" if exact else "identity_metadata_unavailable",
            provenance=provenance,
        )
    record = exact[0]
    returned = _exact("pmcid", requested, record.get("pmcid"), provenance=provenance)
    provenance["returned_identifier"] = returned
    author_list = record.get("authorList")
    author_rows = author_list.get("author") if isinstance(author_list, dict) else None
    fields = _metadata_fields(
        title=record.get("title"),
        authors=author_rows if isinstance(author_rows, list) else record.get("authorString"),
        year=record.get("pubYear") or record.get("firstPublicationDate"),
        doi=record.get("doi"),
        pmid=record.get("pmid"),
        pmcid=returned,
        provenance=provenance,
    )
    return {"fields": fields, "provenance": provenance}


def _arxiv(identifier: str) -> dict[str, object]:
    requested = _canonical_arxiv(identifier)
    if not requested:
        raise LibraryMetadataError("enter a valid arXiv ID", code="invalid_identity_identifier")
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": requested})
    body, provenance = _response(
        url,
        "application/atom+xml",
        provider="arxiv",
        scheme="arxiv_id",
        requested=requested,
    )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise LibraryMetadataError("arXiv returned malformed Atom XML", provenance=provenance) from exc
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    if len(entries) != 1:
        raise LibraryMetadataError(
            "arXiv did not return exactly one record for the requested ID",
            provenance=provenance,
        )
    entry = entries[0]
    entry_ids = entry.findall("atom:id", ns)
    entry_id = entry_ids[0].text if len(entry_ids) == 1 else None
    if not arxiv._atom_id_matches(requested, entry_id):
        raise LibraryMetadataError(
            "arXiv record did not attest the requested ID",
            code="identity_metadata_mismatch",
            provenance=provenance,
        )
    canonical_id = _canonical_arxiv(entry_id)
    if not canonical_id:
        raise LibraryMetadataError(
            "arXiv record returned an invalid ID", code="identity_metadata_mismatch",
            provenance=provenance,
        )
    base_id = re.sub(r"v\d+$", "", canonical_id or "")
    title = entry.findtext("atom:title", default="", namespaces=ns)
    authors = [
        _clean_text(node.findtext("atom:name", default="", namespaces=ns))
        for node in entry.findall("atom:author", ns)
    ]
    published = entry.findtext("atom:published", default="", namespaces=ns)
    returned_doi = _ARXIV_DOI_PREFIX + base_id
    provenance["returned_identifier"] = canonical_id
    fields = _metadata_fields(
        title=title,
        authors=[name for name in authors if name],
        year=published,
        doi=returned_doi,
        arxiv_id=canonical_id,
        provenance=provenance,
    )
    return {"fields": fields, "provenance": provenance}


def _isbn(identifier: str) -> dict[str, object]:
    requested = _canonical_isbn(identifier)
    if not requested:
        raise LibraryMetadataError("enter a valid ISBN", code="invalid_identity_identifier")
    url = "https://openlibrary.org/api/books?" + urllib.parse.urlencode({
        "bibkeys": f"ISBN:{requested}",
        "jscmd": "data",
        "format": "json",
    })
    payload, provenance = _json_response(
        url,
        "application/json",
        provider="openlibrary",
        scheme="isbn",
        requested=requested,
    )
    record = payload.get(f"ISBN:{requested}")
    if not isinstance(record, dict):
        raise LibraryMetadataError(
            "OpenLibrary did not return a record for the requested ISBN",
            provenance=provenance,
        )
    identifiers = record.get("identifiers")
    returned_values = []
    if isinstance(identifiers, dict):
        for key in ("isbn_10", "isbn_13"):
            raw_values = identifiers.get(key)
            if isinstance(raw_values, list):
                returned_values.extend(raw_values)
    # The response key is the exact catalog lookup key; when alternate edition
    # identifiers are present, they must also confirm that same normalized ISBN.
    if returned_values and requested not in {
        canonical for value in returned_values
        if (canonical := content_store._canon_isbn(str(value)))
    }:
        raise LibraryMetadataError(
            "OpenLibrary record did not attest the requested ISBN",
            code="identity_metadata_mismatch",
            provenance=provenance,
        )
    provenance["returned_identifier"] = requested
    authors = record.get("authors")
    fields = _metadata_fields(
        title=record.get("title"),
        authors=authors if isinstance(authors, list) else None,
        year=record.get("publish_date"),
        isbn=requested,
        provenance=provenance,
    )
    return {"fields": fields, "provenance": provenance}


def lookup_identity(scheme: str, identifier: str) -> dict[str, object]:
    """Fetch one exact identifier record and return normalized fields + provenance."""
    normalized_scheme = str(scheme or "").strip().casefold()
    normalized_scheme = {"arxiv": "arxiv_id", "pmc": "pmcid"}.get(
        normalized_scheme, normalized_scheme,
    )
    value = str(identifier or "").strip()
    if not normalized_scheme or not value:
        raise LibraryMetadataError(
            "choose an identifier scheme and enter its value",
            code="invalid_identity_identifier",
        )
    handlers = {
        "doi": _doi,
        "pmid": _pmid,
        "pmcid": _pmcid,
        "arxiv_id": _arxiv,
        "isbn": _isbn,
    }
    handler = handlers.get(normalized_scheme)
    if handler is None:
        raise LibraryMetadataError(
            f"unsupported Library identifier scheme: {normalized_scheme}",
            code="invalid_identity_identifier",
        )
    return handler(value)
