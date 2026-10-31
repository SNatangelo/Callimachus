# core/app/source_service_checks.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, read-only checks for source-service credentials."""
from __future__ import annotations

import json
import re
import socket
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping


REQUEST_TIMEOUT_SECONDS = 6
MAX_RESPONSE_BYTES = 64 * 1024
MAX_KEY_LENGTH = 4096

SOURCE_SERVICE_KEY_NAMES = (
    "GOOGLE_BOOKS_API_KEY",
    "CORE_API_KEY",
    "ZENODO_ACCESS_TOKEN",
    "ELSEVIER_API_KEY",
    "TDM_API_TOKEN",
    "SPRINGER_NATURE_META_API_KEY",
    "SPRINGER_NATURE_OPEN_ACCESS_API_KEY",
    "NCBI_API_KEY",
    "ENTREZ_API_KEY",
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "LENS_API_KEY",
    "COURTLISTENER_API_TOKEN",
    "TAVILY_API_KEY",
    "MOJEEK_API_KEY",
)

# These are provider-owned pages for obtaining or managing credentials. Keep
# them separate from the check endpoints so UI links never contain key data.
KEY_PORTALS = {
    "GOOGLE_BOOKS_API_KEY": "https://console.cloud.google.com/apis/credentials",
    "CORE_API_KEY": "https://core.ac.uk/services/api",
    "ZENODO_ACCESS_TOKEN": "https://zenodo.org/account/settings/applications/tokens/new/",
    "ELSEVIER_API_KEY": "https://dev.elsevier.com/",
    "TDM_API_TOKEN": "https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining",
    "SPRINGER_NATURE_META_API_KEY": "https://dev.springernature.com/",
    "SPRINGER_NATURE_OPEN_ACCESS_API_KEY": "https://dev.springernature.com/",
    "NCBI_API_KEY": "https://www.ncbi.nlm.nih.gov/account/settings/",
    "ENTREZ_API_KEY": "https://www.ncbi.nlm.nih.gov/account/settings/",
    "OPENALEX_API_KEY": "https://openalex.org/settings/api",
    "SEMANTIC_SCHOLAR_API_KEY": "https://www.semanticscholar.org/product/api",
    "LENS_API_KEY": "https://www.lens.org/lens/user/profile",
    "COURTLISTENER_API_TOKEN": "https://www.courtlistener.com/profile/api-token/",
    "TAVILY_API_KEY": "https://app.tavily.com/",
    "MOJEEK_API_KEY": "https://www.mojeek.com/services/search/web-search-api/",
}

_BODY_LEVEL_NCBI_KEY_ERROR = re.compile(
    r"api[\s_-]*key.{0,80}(?:invalid|not valid|cannot be used)", re.IGNORECASE | re.DOTALL
)
_GOOGLE_BOOKS_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_GOOGLE_BOOKS_REASON_ERRORS = {
    "API_KEY_INVALID": "Google Books reports that this API key is invalid.",
    "SERVICE_DISABLED": "The Google Books API is not enabled for this key's Google Cloud project.",
    "API_KEY_SERVICE_BLOCKED": "The API key's service restrictions block this Google Books request.",
    "API_KEY_HTTP_REFERRER_BLOCKED": "The API key's HTTP-referrer restriction blocks this desktop request.",
    "API_KEY_IP_ADDRESS_BLOCKED": "The API key's IP-address restriction blocks this desktop request.",
}


def _request_for_key(name: str, key: str) -> tuple[urllib.request.Request, frozenset[int]] | None:
    """Build one fixed provider-owned read request for a recognized key."""
    headers = {"Accept": "application/json", "User-Agent": "Callimachus source-key check"}
    params: dict[str, str]
    if name == "GOOGLE_BOOKS_API_KEY":
        params = {
            "q": "isbn:9780262033848",
            "maxResults": "1",
            "fields": "totalItems,items/id",
            "key": key,
        }
        url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    elif name == "CORE_API_KEY":
        headers["Authorization"] = f"Bearer {key}"
        params = {"q": "doi:10.1038/nature12373", "limit": "1"}
        from core.resolve.providers.core import CORE_SEARCH_WORKS_URL
        url = CORE_SEARCH_WORKS_URL + "?" + urllib.parse.urlencode(params)
    elif name == "ZENODO_ACCESS_TOKEN":
        headers["Authorization"] = f"Bearer {key}"
        params = {"size": "1"}
        url = "https://zenodo.org/api/deposit/depositions?" + urllib.parse.urlencode(params)
    elif name == "ELSEVIER_API_KEY":
        headers["X-ELS-APIKey"] = key
        url = "https://api.elsevier.com/authenticate?platform=SCOPUS"
    elif name == "SPRINGER_NATURE_META_API_KEY":
        params = {
            "q": "doi:10.1007/978-3-030-58259-3_6",
            "s": "1",
            "p": "1",
            "api_key": key,
        }
        url = "https://api.springernature.com/meta/v2/json?" + urllib.parse.urlencode(params)
    elif name == "SPRINGER_NATURE_OPEN_ACCESS_API_KEY":
        params = {
            "q": "keyword:science",
            "s": "1",
            "p": "1",
            "api_key": key,
        }
        url = "https://api.springernature.com/openaccess/json?" + urllib.parse.urlencode(params)
    elif name in {"NCBI_API_KEY", "ENTREZ_API_KEY"}:
        params = {
            "db": "pubmed",
            "term": "BRCA1[Gene]",
            "retmax": "0",
            "retmode": "json",
            "api_key": key,
        }
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    elif name == "OPENALEX_API_KEY":
        params = {"per-page": "1", "select": "id", "api_key": key}
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    elif name == "SEMANTIC_SCHOLAR_API_KEY":
        headers["x-api-key"] = key
        params = {
            "query": "citation verification",
            "limit": "1",
            "fields": "paperId",
        }
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    elif name == "LENS_API_KEY":
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "query": {"match_phrase": {"title": "The Structure of Scientific Revolutions"}},
            "size": 1,
            "include": ["lens_id"],
        }).encode("utf-8")
        return urllib.request.Request(
            "https://api.lens.org/scholarly/search",
            data=body,
            headers=headers,
            method="POST",
        ), frozenset({200})
    elif name == "COURTLISTENER_API_TOKEN":
        headers["Authorization"] = f"Token {key}"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode({
            "volume": "576", "reporter": "U.S.", "page": "644",
        }).encode("ascii")
        return urllib.request.Request(
            "https://www.courtlistener.com/api/rest/v4/citation-lookup/",
            data=body,
            headers=headers,
            method="POST",
        ), frozenset({200})
    elif name == "TAVILY_API_KEY":
        headers["Authorization"] = f"Bearer {key}"
        url = "https://api.tavily.com/usage"
    elif name == "MOJEEK_API_KEY":
        params = {
            "q": "citation verification",
            "api_key": key,
            "fmt": "json",
            "t": "1",
        }
        url = "https://api.mojeek.com/search?" + urllib.parse.urlencode(params)
    else:
        # Wiley TDM credentials are validated only during DOI-targeted full-text
        # retrieval, which could download an article and conflate IP entitlement
        # with token validity. Expose that limitation instead of a false green.
        return None
    success_codes = {200}
    if name == "ELSEVIER_API_KEY":
        # The official authentication endpoint can return 300 when a valid key
        # maps to more than one account and the caller must choose a platform.
        success_codes.add(300)
    return urllib.request.Request(url, headers=headers, method="GET"), frozenset(success_codes)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so provider credentials stay on the fixed endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


def _perform(request: urllib.request.Request) -> tuple[int, bytes]:
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read(MAX_RESPONSE_BYTES)
            return status, body
    except urllib.error.HTTPError as exc:
        # HTTPError includes the request URL in its repr; never forward it.
        try:
            body = exc.read(MAX_RESPONSE_BYTES)
        except Exception:
            body = b""
        return int(exc.code), body


def _failure(status_code: int) -> dict[str, object]:
    if status_code == 401:
        error = "The service rejected this credential (HTTP 401)."
    elif status_code == 403:
        error = (
            "The service denied access (HTTP 403); account permissions or access "
            "entitlement may be required."
        )
    elif status_code in {301, 302, 303, 307, 308}:
        error = f"The service redirected this request (HTTP {status_code}); the key was not validated."
    elif status_code == 404:
        error = "The service endpoint or requested resource was not found (HTTP 404); this does not establish that the key is invalid."
    elif status_code == 429:
        error = "The service is rate-limiting this check; the key was not validated."
    elif status_code in {408, 425}:
        error = "The service could not complete this check; the key was not validated."
    elif status_code >= 500:
        error = "The service is temporarily unavailable; the key was not validated."
    else:
        error = f"The service did not confirm this key (HTTP {status_code})."
    return {"ok": False, "error": error, "status_code": status_code}


def _decode_json(body: bytes) -> object | None:
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return None


def _google_books_error_reason(data: object) -> str | None:
    """Return a safe explanation for an allowlisted Google ErrorInfo reason."""
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if (detail.get("@type") != _GOOGLE_BOOKS_ERROR_INFO_TYPE
                or detail.get("domain") != "googleapis.com"):
            continue
        reason = detail.get("reason")
        if not isinstance(reason, str):
            continue
        message = _GOOGLE_BOOKS_REASON_ERRORS.get(reason)
        if message is not None:
            return message
    return None


def _google_books_failure(status_code: int, body: bytes) -> dict[str, object]:
    message = _google_books_error_reason(_decode_json(body))
    if message is not None:
        error = message
    elif status_code == 400:
        error = "Google Books rejected the request (HTTP 400); the key was not confirmed."
    else:
        return _failure(status_code)
    return {"ok": False, "error": error, "status_code": status_code}


def _body_error(name: str, body: bytes) -> str | None:
    data = _decode_json(body)
    if name == "GOOGLE_BOOKS_API_KEY" and isinstance(data, dict):
        if "error" in data or "errors" in data:
            return (_google_books_error_reason(data)
                    or "Google Books returned an error; the key was not confirmed.")
    if isinstance(data, dict) and any(data.get(field) for field in ("error", "errors")):
        return "The service did not confirm this key."
    if name in {"NCBI_API_KEY", "ENTREZ_API_KEY"}:
        if _BODY_LEVEL_NCBI_KEY_ERROR.search(body.decode("utf-8", errors="replace")):
            return "The service did not accept this credential."
    if name == "GOOGLE_BOOKS_API_KEY":
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return "The service did not confirm this key."
    if name == "MOJEEK_API_KEY":
        response = data.get("response") if isinstance(data, dict) else None
        if isinstance(response, dict):
            status = str(response.get("status") or "").strip()
            if status and status.upper() != "OK":
                if "limit" in status.casefold() or "rate" in status.casefold():
                    return "The service is rate-limiting this check; the key was not validated."
                return "The service did not confirm this key."
    if name in {"SPRINGER_NATURE_META_API_KEY", "SPRINGER_NATURE_OPEN_ACCESS_API_KEY"}:
        if isinstance(data, dict):
            message = str(data.get("apiMessage") or "").casefold()
            if "error" in message or "invalid" in message or "not authorized" in message:
                return "The service did not confirm this key."
    return None


def _is_success_response(name: str, body: bytes, status_code: int) -> bool:
    """Recognize the response envelope of the fixed check endpoint.

    HTTP success alone can also describe a login page or an intermediary's
    response. Empty search results are valid, but an absent envelope is not.
    """
    data = _decode_json(body)
    if name in {"ZENODO_ACCESS_TOKEN", "COURTLISTENER_API_TOKEN"}:
        field = "id" if name == "ZENODO_ACCESS_TOKEN" else "citation"
        return isinstance(data, list) and all(
            isinstance(item, dict) and field in item for item in data
        )
    if not isinstance(data, dict):
        return False
    if name == "GOOGLE_BOOKS_API_KEY":
        count = data.get("totalItems")
        return type(count) is int and count >= 0 and (
            "items" not in data or isinstance(data["items"], list)
        )
    if name == "CORE_API_KEY":
        return type(data.get("totalHits")) is int and isinstance(data.get("results"), list)
    if name == "ELSEVIER_API_KEY":
        auth = data.get("authenticate-response")
        if not isinstance(auth, dict):
            return False
        if status_code == 300:
            paths = auth.get("pathChoices")
            choices = paths.get("choice") if isinstance(paths, dict) else None
            return isinstance(choices, list) and bool(choices) and all(
                isinstance(choice, dict) and "@id" in choice for choice in choices
            )
        token = auth.get("authtoken")
        return isinstance(token, str) and bool(token.strip())
    if name in {"SPRINGER_NATURE_META_API_KEY", "SPRINGER_NATURE_OPEN_ACCESS_API_KEY"}:
        return isinstance(data.get("result"), list) and isinstance(data.get("records"), list)
    if name in {"NCBI_API_KEY", "ENTREZ_API_KEY"}:
        search = data.get("esearchresult")
        return isinstance(search, dict) and "count" in search and isinstance(search.get("idlist"), list)
    if name == "OPENALEX_API_KEY":
        return isinstance(data.get("meta"), dict) and isinstance(data.get("results"), list)
    if name in {"SEMANTIC_SCHOLAR_API_KEY", "LENS_API_KEY"}:
        return type(data.get("total")) is int and isinstance(data.get("data"), list)
    if name == "TAVILY_API_KEY":
        return isinstance(data.get("key"), dict) and isinstance(data.get("account"), dict)
    if name == "MOJEEK_API_KEY":
        response = data.get("response")
        return isinstance(response, dict) and response.get("status") == "OK" and isinstance(response.get("results"), list)
    return False


def check_source_service_key(
    name: str,
    updates: Mapping[str, object] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Check one source-service key with a single bounded, read-only request.

    ``updates`` takes precedence over the persisted effective environment so a
    settings form can check a newly edited value before saving it.
    """
    if name not in SOURCE_SERVICE_KEY_NAMES:
        return {
            "ok": False,
            "error": "Automated checks are unsupported for this key type.",
            "unsupported": True,
        }
    if name == "TDM_API_TOKEN":
        return {
            "ok": False,
            "error": "Automated checks are unsupported for Wiley TDM tokens.",
            "unsupported": True,
        }
    values = {} if environ is None else environ
    raw = updates.get(name) if updates is not None and name in updates else values.get(name)
    if not isinstance(raw, str):
        return {"ok": False, "error": "Enter a key before checking it."}
    key = raw.strip(" \t\r\n")
    if len(raw) > MAX_KEY_LENGTH or any(
        unicodedata.category(character) == "Cc" for character in key
    ):
        return {"ok": False, "error": "The key has an invalid format."}
    key = key.strip()
    if not key:
        return {"ok": False, "error": "Enter a key before checking it."}

    try:
        spec = _request_for_key(name, key)
        if spec is None:
            return {
                "ok": False,
                "error": "Automated checks are unsupported for this key type.",
                "unsupported": True,
            }
        request, success_codes = spec
        status_code, body = _perform(request)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
        return {"ok": False, "error": "Could not reach the service; the key was not validated."}
    except Exception:
        # Keep callback failures safe for the GUI; request reprs can include a
        # query-string credential even if a provider rejects malformed input.
        return {"ok": False, "error": "The service check failed; the key was not validated."}

    if status_code not in success_codes:
        if name == "GOOGLE_BOOKS_API_KEY":
            return _google_books_failure(status_code, body)
        return _failure(status_code)
    body_error = _body_error(name, body)
    if body_error:
        return {"ok": False, "error": body_error, "status_code": status_code}
    if not _is_success_response(name, body, status_code):
        return {
            "ok": False,
            "error": "The service returned an unexpected response; the key was not validated.",
            "status_code": status_code,
        }
    return {"ok": True, "error": None, "status_code": status_code}


def source_contact_state(environ: Mapping[str, str]) -> dict[str, bool]:
    """Return only presence flags needed by the optional GUI contact prompt."""
    return {
        "has_mail": bool(str(environ.get("CITATION_VERIFIER_MAILTO") or "").strip()),
        "has_keys": any(
            bool(str(environ.get(name) or "").strip())
            for name in SOURCE_SERVICE_KEY_NAMES
        ),
    }
