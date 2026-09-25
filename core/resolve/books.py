# core/resolve/books.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import importlib
import json
import os
import re
import urllib.error
import urllib.parse

try:
    from . import sources as _sources
except ImportError:  # direct execution
    from resolve import sources as _sources

try:
    from core.parse.reference_readers import bluebook as _bluebook
except ImportError:  # direct execution
    from parse.reference_readers import bluebook as _bluebook


def _resolve_get():
    try:
        resolve_mod = importlib.import_module("core.resolve.service")
    except ImportError:  # direct execution
        resolve_mod = importlib.import_module("service")
    return resolve_mod._get


def _extract_book_title(ref: dict) -> str | None:
    """Best book title for a catalog query: explicit field, else heuristic from the
    raw entry (quoted string first, then the clause after the author block)."""
    title = ref.get("title")
    if title and not _degraded_book_title(title):
        return title
    raw_entry = ref.get("raw_entry", "")
    if not raw_entry:
        return None
    m = re.search(r'"([^"]{5,})"', raw_entry)
    if m:
        return m.group(1)
    parts = [p.strip(" ,;:") for p in re.split(r"\.\s+", raw_entry) if p.strip(" ,;:")]
    titleish = []
    for p in parts[1:]:
        low = p.lower()
        if re.search(r"\b(press|publisher|edizioni|books?|norton|mit press)\b", low):
            continue
        if re.search(r"\b(19|20)\d{2}\b", p):
            continue
        if len(_sources._tokens(p)) >= 3:
            titleish.append(p)
    if titleish:
        return max(titleish, key=len)
    # Strip a leading "Author(s). " block, then take the first sentence-like clause.
    stripped = re.sub(r"^[^.]+\.\s+", "", raw_entry)
    candidate = re.split(r"\.\s", stripped)[0].strip()
    return candidate if len(candidate) > 8 else None


def _degraded_book_title(title: str | None) -> bool:
    """Parser debris is not an explicit title; fall back to the raw entry."""
    clean = str(title or "").strip()
    return not clean or clean.startswith(("&", ".", ",")) or clean.lower() in {"et al", "et al."}


def _strict_personal_book_route(ref: dict) -> dict | None:
    """The no-ISBN Bluebook route, independently re-read from the citation."""
    if ref.get("isbn") or ref.get("source_kind") != "book_like":
        return None
    return _bluebook.personal_book_route(str(ref.get("raw_entry") or ""))


def _title_only_catalog_route(ref: dict) -> dict | None:
    """Citation-owned identity needed to admit a title-only catalog result.

    A title query is discovery only: catalog ranking can put a containing work
    or a different work with the same title first.  Without an ISBN, retain a
    result only when the parsed title and cited first author both agree.
    """
    strict_route = _strict_personal_book_route(ref)
    if strict_route:
        return strict_route
    title = _extract_book_title(ref)
    raw_title_key = _sources._title_key(ref.get("raw_entry"))
    title_key = _sources._title_key(title)
    if title_key and raw_title_key.startswith(title_key):
        # A citation beginning with its title has no citation-owned author.
        # The generic source matcher may otherwise misread a title word as a
        # surname, which would turn a fail-closed lookup into a false absence.
        return None
    surname = _sources._first_author_surname(ref)
    if not title or not surname:
        return None
    return {"title": title, "first_author_surname": surname}


def _catalog_title_key(value: str | None) -> str:
    return _sources._title_key(value)


def _catalog_candidate_matches(route: dict, ref: dict, title, authors, year=None,
                               isbns=None, *, year_is_edition=False) -> bool:
    """Require citation-owned title and first author for the strict book route."""
    if _catalog_title_key(title) != _catalog_title_key(route["title"]):
        return False
    surname = route["first_author_surname"].casefold()
    author_values = [str(author or "") for author in (authors or [])]
    if not author_values:
        return False
    first_author_tokens = {
        part.casefold()
        for part in re.findall(r"[A-Za-zÀ-ÿ][\w'’-]*", author_values[0])
    }
    if surname not in first_author_tokens:
        return False
    cited_year = str(ref.get("year") or "")[:4]
    candidate_year = str(year or "")[:4]
    if year_is_edition and cited_year and candidate_year and cited_year != candidate_year:
        return False
    cited_isbn = re.sub(r"[^0-9Xx]", "", str(ref.get("isbn") or ""))
    candidate_isbns = {re.sub(r"[^0-9Xx]", "", str(value or "")) for value in (isbns or [])}
    if cited_isbn and candidate_isbns and cited_isbn not in candidate_isbns:
        return False
    return True


# Book availability is ADVISORY. A catalog's "viewability" reflects what it is
# licensed to SHOW in the user's region at query time - not whether a full text exists
# nor whether we can verify a claim against it. It is a provisioning hint
# (full | partial | none | unknown), never an input to a verdict.
BOOK_AVAILABILITY_NOTE = (
    "advisory: catalog-declared preview scope; varies by region and access, "
    "not a guarantee of full-text availability or verifiability"
)

# Google Books accessInfo.viewability / OpenLibrary ebook availability -> coarse buckets.
_GB_VIEWABILITY = {
    "ALL_PAGES": "full", "FULL_PUBLIC_DOMAIN": "full",
    "PARTIAL": "partial", "NO_PAGES": "none",
}
_OL_PREVIEW = {"full": "full", "borrow": "partial", "restricted": "partial",
               "noview": "none"}


def _gb_availability(access_info: dict) -> str:
    """Coarse availability bucket from a Google Books volume's accessInfo."""
    if access_info.get("publicDomain"):
        return "full"
    return _GB_VIEWABILITY.get(access_info.get("viewability"), "unknown")


def _ol_availability(book: dict) -> str:
    """Coarse availability bucket from an OpenLibrary jscmd=data record."""
    for eb in (book.get("ebooks") or []):
        av = (eb.get("availability") or "").lower()
        if av in _OL_PREVIEW:
            return _OL_PREVIEW[av]
        if av:
            return "partial"
    return _OL_PREVIEW.get((book.get("preview") or "").lower(), "unknown")


def _book_meta(via, title, authors, abstract, reason, availability="unknown") -> dict:
    return {
        "status": "resolved", "via": via,
        "matched_title": title,
        "matched_authors": authors or [],
        "abstract": abstract,
        "retracted": False,
        "fulltext_exists": "unknown",
        "oa_status": "unknown",
        "work_type": "book",
        "book_availability": availability,
        "availability_note": BOOK_AVAILABILITY_NOTE if availability != "unknown" else None,
        "reason": reason,
    }


def _openlibrary(ref: dict) -> dict:
    """One catalog: OpenLibrary. Returns resolved | _book_absent | unverified | unresolved.
    Never decides 'not_found' alone - that is the combiner's job (see _resolve_book)."""
    isbn = ref.get("isbn")
    get = _resolve_get()

    if isbn:
        clean = re.sub(r"[^0-9Xx]", "", isbn)
        url = (
            "https://openlibrary.org/api/books"
            f"?bibkeys=ISBN:{urllib.parse.quote(clean)}"
            "&jscmd=data&format=json"
        )
        try:
            _status, body = get(url)
            data = json.loads(body)
            key = f"ISBN:{clean}"
            if key in data:
                book = data[key]
                authors = [a.get("name", "") for a in (book.get("authors") or [])]
                return _book_meta("openlibrary", book.get("title"), authors, None, None,
                                  availability=_ol_availability(book))
            # ISBN absent on this catalog: neutral signal, combiner cross-checks.
            return {"status": "_book_absent", "via": "openlibrary",
                    "reason": f"ISBN {isbn} not on OpenLibrary"}
        except urllib.error.HTTPError as e:
            return {"status": "unresolved", "via": "openlibrary", "reason": f"HTTP {e.code}"}
        except Exception as e:
            return {"status": "unresolved", "via": "openlibrary",
                    "reason": f"network: {type(e).__name__}"}

    route = _title_only_catalog_route(ref)
    if not route:
        return {"status": "unverified", "via": "openlibrary",
                "reason": "no ISBN, usable title, and cited first author for deterministic book lookup"}
    params = {"title": route["title"][:200], "limit": "10",
              "fields": "key,title,author_name,isbn,first_publish_year"}
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    try:
        _status, body = get(url)
        docs = (json.loads(body).get("docs") or [])
        if not docs:
            return {"status": "_book_absent", "via": "openlibrary",
                    "reason": "no title match on OpenLibrary"}
        doc = next((candidate for candidate in docs if _catalog_candidate_matches(
            route, ref, candidate.get("title"), candidate.get("author_name"),
            isbns=candidate.get("isbn"))), None)
        if doc is None:
            return {"status": "_book_absent", "via": "openlibrary",
                    "reason": "no compatible title/author candidate on OpenLibrary"}
        return _book_meta("openlibrary", doc.get("title"), doc.get("author_name"),
                          None, "title-only match (weak): verify author and edition")
    except urllib.error.HTTPError as e:
        return {"status": "unresolved", "via": "openlibrary", "reason": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "unresolved", "via": "openlibrary",
                "reason": f"network: {type(e).__name__}"}


def _googlebooks(ref: dict) -> dict:
    """Second catalog: Google Books. Same return contract as _openlibrary.

    Uses GOOGLE_BOOKS_API_KEY when present; without it the Volumes API still works
    keyless but is more prone to quota/rate-limit failures.
    """
    isbn = ref.get("isbn")
    get = _resolve_get()
    route = None
    if isbn:
        clean = re.sub(r"[^0-9Xx]", "", isbn)
        q = f"isbn:{clean}"
    else:
        route = _title_only_catalog_route(ref)
        if not route:
            return {"status": "unverified", "via": "googlebooks",
                    "reason": "no ISBN, usable title, and cited first author for deterministic Google Books lookup"}
        q = f"intitle:{route['title'][:200]}"
    params = {"q": q, "maxResults": "10"}
    key = (os.environ.get("GOOGLE_BOOKS_API_KEY") or "").strip()
    if key:
        params["key"] = key
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    try:
        if key:
            from core.resolve import transport_telemetry
            with transport_telemetry.credential_scope(
                provider="google_books", env_name="GOOGLE_BOOKS_API_KEY"
            ):
                _status, body = get(url)
        else:
            _status, body = get(url)
        items = (json.loads(body).get("items") or [])
        if not items:
            return {"status": "_book_absent", "via": "googlebooks",
                    "reason": "no match on Google Books"}
        item = next((candidate for candidate in items if route is None or _catalog_candidate_matches(
            route, ref,
            (candidate.get("volumeInfo") or {}).get("title"),
            (candidate.get("volumeInfo") or {}).get("authors"),
            (candidate.get("volumeInfo") or {}).get("publishedDate"),
            [identifier.get("identifier") for identifier in
             ((candidate.get("volumeInfo") or {}).get("industryIdentifiers") or [])
             if isinstance(identifier, dict)], year_is_edition=True)), None)
        if item is None:
            return {"status": "_book_absent", "via": "googlebooks",
                    "reason": "no compatible title/author candidate on Google Books"}
        vol = item.get("volumeInfo", {})
        return _book_meta("googlebooks", vol.get("title"), vol.get("authors"),
                          vol.get("description"), None,
                          availability=_gb_availability(item.get("accessInfo", {})))
    except urllib.error.HTTPError as e:
        return {"status": "unresolved", "via": "googlebooks", "reason": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "unresolved", "via": "googlebooks",
                "reason": f"network: {type(e).__name__}"}


def _resolve_book(ref: dict) -> dict:
    """Combine OpenLibrary + Google Books. Adds 'existence_corroboration'."""
    isbn = ref.get("isbn")
    ol = _openlibrary(ref)
    if ol["status"] == "resolved":
        ol["existence_corroboration"] = "corroborated"
        return ol
    gb = _googlebooks(ref)
    if gb["status"] == "resolved":
        gb["existence_corroboration"] = "corroborated"
        return gb

    statuses = (ol["status"], gb["status"])
    # Transient failure on either catalog: don't conclude absence.
    if "unresolved" in statuses:
        return {"status": "unresolved", "via": "openlibrary/googlebooks",
                "reason": f"book catalogs unreachable (transient): "
                          f"OL={ol['reason']}; GB={gb['reason']}"}

    # Both catalogs returned a definite 'absent' for at least one, none resolved.
    searched = "_book_absent" in statuses
    if isbn:
        clean = re.sub(r"[^0-9Xx]", "", isbn)
        if len(clean) in (10, 13) and ol["status"] == "_book_absent" \
                and gb["status"] == "_book_absent":
            # Well-formed ISBN absent from BOTH catalogs: hard identifier error.
            return {"status": "not_found", "via": "openlibrary+googlebooks",
                    "reason": f"ISBN {isbn} not found on OpenLibrary or Google Books "
                              "(hard identifier error)",
                    "existence_corroboration": "searched_not_found"}

    if searched:
        # Title-only (or malformed ISBN) absent from the catalogs we could reach.
        # NEVER red: catalogs are not exhaustive. Loud orange + opt-in web escalation.
        return {"status": "unverified", "via": "openlibrary+googlebooks",
                "reason": "book not found on OpenLibrary or Google Books; "
                          "web search recommended (opt-in) to corroborate existence",
                "existence_corroboration": "searched_not_found"}

    # Nothing usable to search on (no ISBN, no title).
    return {"status": "unverified", "via": "openlibrary+googlebooks",
            "reason": "no ISBN or usable title: cannot search book catalogs",
            "existence_corroboration": "none"}
