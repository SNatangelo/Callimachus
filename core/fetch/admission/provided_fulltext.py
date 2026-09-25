#!/usr/bin/env python3
# core/fetch/admission/provided_fulltext.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed admission of user-provided full texts.

``fulltext-index.txt`` provides a user-selected identity override. Supported
files without a row are instead matched conservatively against already-resolved
references; no path creates a citation, fetches a declared URL, or bypasses the
ordinary full-text identity and quality gates.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from core.fetch.storage import content_store, fetch_store
from core.fetch.extraction import fetch_html, ocr as fetch_ocr
from core.parse import extract as parse_extract
from core.parse import source_text
from core.resolve import sources, user_sources


INDEX_FILENAME = "fulltext-index.txt"
SUPPORTED_IDENTIFIER_SCHEMES = frozenset({"doi", "pmid", "isbn", "url"})
_HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})
_NOT_FOUND_PREFIX = "[not found] "
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_CONTROLLED_PDF_MIN_WORDS_PER_PAGE = 20
_PRECOMPUTED_OCR_URN_PREFIX = "urn:callimachus:ocr-scan:sha256:"
_PRECOMPUTED_OCR_URN_RE = re.compile(
    rf"^{re.escape(_PRECOMPUTED_OCR_URN_PREFIX)}([0-9a-f]{{64}})$"
)

_INDEX_TEMPLATE = """# Callimachus user-provided full-text index
# A populated row is an explicit identity override for that file. Supported
# unlisted files are auto-identified against resolved references.
# Syntax: relative file path = identifier scheme:identifier
# The file content must match its extension; renaming a file does not convert it.
# Corrupt, empty, binary-disguised, or unreadable files are rejected without storage.
# If the exact identifier already has reusable full text in the catalogue, that
# catalogue entry is reused and the newly mapped file is left untouched. Files
# without one unique identity stay in this directory as [not found] filenames.
# [not found] files are not retried automatically on later runs.
# Remove the prefix to retry, or map the marked filename explicitly below.
#
# article.pdf = doi:10.1234/example
# pubmed-export.html = pmid:12345678
# book.pdf = isbn:9780306406157
# saved-page.html = url:https://publisher.example/article
"""


@dataclass(frozen=True)
class _ManifestEntry:
    line_number: int
    path: Path
    relative_name: str
    scheme: str
    value: str

    @property
    def source_ref(self) -> str:
        return self.value if self.scheme == "url" else f"{self.scheme}:{self.value}"

    @property
    def identity_key(self) -> tuple[str, str]:
        return self.scheme, self.value


def ensure_index(directory: str | os.PathLike[str]) -> str:
    """Create the commented index template once, without overwriting user input."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    index = root / INDEX_FILENAME
    try:
        with index.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(_INDEX_TEMPLATE)
    except FileExistsError:
        pass
    return str(index)


def _normalize_doi(value: object) -> str:
    raw = urllib.parse.unquote(str(value or "").strip()).strip("<>")
    raw = re.sub(r"(?i)^doi\s*:\s*", "", raw)
    raw = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", raw).strip()
    if not _DOI_RE.fullmatch(raw):
        raise ValueError("invalid DOI")
    return raw.lower()


def _normalize_pmid(value: object) -> str:
    raw = re.sub(r"(?i)^pmid\s*:\s*", "", str(value or "").strip())
    if not re.fullmatch(r"\d+", raw) or int(raw) <= 0:
        raise ValueError("invalid PMID")
    return raw


def _isbn13_check_digit(first_twelve: str) -> str:
    total = sum((1 if index % 2 == 0 else 3) * int(char)
                for index, char in enumerate(first_twelve))
    return str((10 - total % 10) % 10)


def _normalize_isbn(value: object) -> str:
    raw = re.sub(r"(?i)^isbn(?:-1[03])?\s*:\s*", "", str(value or "").strip())
    compact = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(compact) == 10:
        digits = [10 if char == "X" else int(char) for char in compact]
        if "X" in compact[:-1] or sum((10 - index) * digit for index, digit in enumerate(digits)) % 11:
            raise ValueError("invalid ISBN-10 checksum")
        first_twelve = "978" + compact[:9]
        return first_twelve + _isbn13_check_digit(first_twelve)
    if len(compact) == 13 and compact.isdigit():
        if _isbn13_check_digit(compact[:12]) != compact[-1]:
            raise ValueError("invalid ISBN-13 checksum")
        return compact
    raise ValueError("ISBN must be a valid ISBN-10 or ISBN-13")


def _normalize_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if port is not None and not 0 < port <= 65535:
        raise ValueError("invalid URL port")
    aliases = content_store.identity_aliases_for_ref({"url": raw})
    alias = next((key for scheme, key in aliases if scheme == "url"), None)
    if not alias:
        raise ValueError("invalid URL")
    return alias.removeprefix("url:")


def _normalize_identifier(scheme: str, value: object) -> str:
    if scheme == "doi":
        return _normalize_doi(value)
    if scheme == "pmid":
        return _normalize_pmid(value)
    if scheme == "isbn":
        return _normalize_isbn(value)
    if scheme == "url":
        return _normalize_url(value)
    raise ValueError(f"unsupported identifier scheme: {scheme}")


def _unquote_filename(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _relative_key(value: str) -> str:
    normalized = value.replace("\\", "/")
    return Path(os.path.normpath(normalized)).as_posix().casefold()


def _resolve_manifest_path(root: Path, raw_name: str) -> tuple[Path, str]:
    if not raw_name or "\0" in raw_name:
        raise ValueError("file path is empty or contains NUL")
    if _WINDOWS_ABSOLUTE_RE.match(raw_name) or raw_name.startswith(("/", "\\\\")):
        raise ValueError("file path must be relative to the full-text directory")
    relative = Path(raw_name.replace("\\", "/"))
    candidate = (root / relative).resolve()
    try:
        common = os.path.commonpath([str(root), str(candidate)])
    except ValueError as exc:
        raise ValueError("file path is outside the full-text directory") from exc
    if common != str(root):
        raise ValueError("file path is outside the full-text directory")
    if candidate.name.casefold() == INDEX_FILENAME.casefold():
        raise ValueError(f"{INDEX_FILENAME} is reserved and cannot be a full text")
    return candidate, candidate.relative_to(root).as_posix()


def _read_manifest(
    directory: str | os.PathLike[str],
    allowed_extensions: set[str],
) -> tuple[list[_ManifestEntry], list[str], set[str]]:
    root = Path(directory).resolve()
    index = Path(ensure_index(root))
    try:
        lines = index.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        return [], [f"cannot read {INDEX_FILENAME}: {exc}"], set()

    entries: list[_ManifestEntry] = []
    errors: list[str] = []
    mentioned: set[str] = set()
    for line_number, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw_line:
            errors.append(f"line {line_number}: expected 'file = scheme:value'")
            continue
        raw_name, raw_identifier = raw_line.split("=", 1)
        name = _unquote_filename(raw_name)
        if name:
            mentioned.add(_relative_key(name))
        if ":" not in raw_identifier:
            errors.append(f"line {line_number}: identifier must include a scheme")
            continue
        raw_scheme, raw_value = raw_identifier.split(":", 1)
        scheme = raw_scheme.strip().casefold()
        if scheme not in SUPPORTED_IDENTIFIER_SCHEMES:
            errors.append(
                f"line {line_number}: unsupported identifier scheme {scheme or '<empty>'}"
            )
            continue
        try:
            value = _normalize_identifier(scheme, raw_value)
            path, relative_name = _resolve_manifest_path(root, name)
            mentioned.add(_relative_key(relative_name))
            if not path.is_file():
                raise ValueError(f"mapped file does not exist: {relative_name}")
            if path.suffix.lower() not in allowed_extensions:
                raise ValueError(f"unsupported full-text format: {path.suffix.lower() or '<none>'}")
        except (OSError, ValueError) as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        entries.append(_ManifestEntry(line_number, path, relative_name, scheme, value))

    duplicate_indexes: set[int] = set()
    for label, key_fn in (
        ("file", lambda item: item.relative_name.casefold()),
        ("identifier", lambda item: item.identity_key),
    ):
        grouped: dict[object, list[int]] = {}
        for index_number, entry in enumerate(entries):
            grouped.setdefault(key_fn(entry), []).append(index_number)
        for indexes in grouped.values():
            if len(indexes) <= 1:
                continue
            duplicate_indexes.update(indexes)
            line_numbers = ", ".join(str(entries[index].line_number) for index in indexes)
            errors.append(f"lines {line_numbers}: duplicate {label} mapping")
    entries = [entry for index, entry in enumerate(entries) if index not in duplicate_indexes]
    return entries, errors, mentioned


def _maybe_identifier(scheme: str, value: object) -> str | None:
    try:
        return _normalize_identifier(scheme, value)
    except (TypeError, ValueError):
        return None


def _reference_view(ref: dict, resolve_result: dict | None) -> dict:
    rr = resolve_result or {}
    view = user_sources.identity_view(dict(ref or {}), rr)
    resolved = rr.get("resolved_identifier") or {}
    resolved_scheme = str(resolved.get("type") or "").casefold()
    for scheme, fields in (
        ("doi", ("canonical_doi", "doi")),
        ("pmid", ("canonical_pmid", "pmid")),
        ("isbn", ("canonical_isbn", "isbn")),
    ):
        values = [view.get(scheme), *(rr.get(field) for field in fields)]
        if resolved_scheme == scheme:
            values.append(resolved.get("value"))
        normalized = next((item for value in values if (item := _maybe_identifier(scheme, value))), None)
        if normalized:
            view[scheme] = normalized
    return view


def _reference_identity_values(ref: dict, resolve_result: dict | None) -> dict[str, set[str]]:
    rr = resolve_result or {}
    view = _reference_view(ref, rr)
    values: dict[str, set[str]] = {scheme: set() for scheme in SUPPORTED_IDENTIFIER_SCHEMES}

    def add(scheme: str, value: object) -> None:
        normalized = _maybe_identifier(scheme, value)
        if normalized:
            values[scheme].add(normalized)

    for scheme in ("doi", "pmid", "isbn", "url"):
        add(scheme, view.get(scheme))
    for scheme, fields in (
        ("doi", ("canonical_doi", "doi")),
        ("pmid", ("canonical_pmid", "pmid")),
        ("isbn", ("canonical_isbn", "isbn")),
        ("url", ("canonical_url", "landing_url", "url")),
    ):
        for field in fields:
            add(scheme, rr.get(field))
    resolved = rr.get("resolved_identifier") or {}
    resolved_scheme = str(resolved.get("type") or "").casefold()
    if resolved_scheme in values:
        add(resolved_scheme, resolved.get("value"))
    for validation in rr.get("identifier_validations") or []:
        if not isinstance(validation, dict):
            continue
        scheme = str(validation.get("type") or validation.get("scheme") or "").casefold()
        if scheme in values:
            add(scheme, validation.get("value") or validation.get("identifier"))
    for item in (rr.get("fulltext_links") or []) + (rr.get("auxiliary_fulltext_links") or []):
        url = item.get("url") if isinstance(item, dict) else item
        add("url", url)
        add("doi", url)
    return values


def _candidate_context(resolve_result: dict | None, entry: _ManifestEntry) -> dict | None:
    if entry.scheme != "url":
        return None
    rr = resolve_result or {}
    for item in (rr.get("fulltext_links") or []) + (rr.get("auxiliary_fulltext_links") or []):
        if not isinstance(item, dict) or _maybe_identifier("url", item.get("url")) != entry.value:
            continue
        context = dict(item.get("identity_context") or {})
        for key in ("provider", "title", "year", "canonical_host", "identifiers"):
            if item.get(key) is not None:
                context.setdefault(key, item[key])
        return context or None
    return None


def _scored_result(
    outcome: str,
    path: Path,
    ref: dict | None,
    *,
    score: float,
    signal: str | None = None,
    reason: str | None = None,
) -> dict:
    result = {
        "outcome": outcome,
        "file": str(path),
        "ref_id": (ref or {}).get("id"),
        "ref_number": (ref or {}).get("ref_number"),
        "score": float(score),
    }
    if signal:
        result["signal"] = signal
    if reason:
        result["reason"] = reason
    return result


def _manifest_errors(index_path: str, errors: list[str]) -> list[dict]:
    return [
        {"outcome": "unreadable", "file": index_path, "error": error}
        for error in errors
    ]


def _discover_supported(directory: str, allowed_extensions: set[str]) -> list[Path]:
    root = Path(directory).resolve()
    files = []
    for path in root.rglob("*"):
        if path.name.casefold() == INDEX_FILENAME.casefold() or not path.is_file():
            continue
        try:
            resolved = path.resolve()
            if os.path.commonpath([str(root), str(resolved)]) != str(root):
                continue
        except (OSError, ValueError):
            continue
        if path.suffix.lower() in allowed_extensions:
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def _isbn_meta_values(meta: dict[str, list[str]]) -> set[str]:
    values: set[str] = set()
    for key in ("citation_isbn", "isbn", "dc.identifier"):
        for raw in meta.get(key) or []:
            normalized = _maybe_identifier("isbn", raw)
            if normalized:
                values.add(normalized)
    return values


def _front_matter_identifiers(text: str) -> dict[str, set[str]]:
    head = (text or "")[:12000]
    found: dict[str, set[str]] = {"doi": set(), "pmid": set(), "isbn": set()}
    for match in _DOI_IN_TEXT_RE.finditer(head):
        value = _maybe_identifier("doi", match.group(0).rstrip(".,;)"))
        if value:
            found["doi"].add(value)
    for match in re.finditer(r"(?i)\bPMID\s*[:#]?\s*(\d+)\b", head):
        value = _maybe_identifier("pmid", match.group(1))
        if value:
            found["pmid"].add(value)
    for match in re.finditer(
        r"(?i)\bISBN(?:-1[03])?\s*[:#]?\s*([0-9X][0-9X\s-]{8,20})",
        head,
    ):
        value = _maybe_identifier("isbn", match.group(1))
        if value:
            found["isbn"].add(value)
    return found


def _has_identifier_conflict(expected: dict[str, set[str]], observed: dict[str, set[str]]) -> bool:
    return any(
        expected.get(scheme) and values and not (expected[scheme] & values)
        for scheme, values in observed.items()
    )


def _is_duplicate(
    run_dir: str,
    ref: dict,
    source_ref: str,
    text: str,
    fmt: str,
    *,
    mapping: str,
) -> bool:
    prepared, _preparation = source_text.prepare_for_verify(text, format_hint=fmt)
    digest = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
    return any(
        item.get("ref_id") == ref.get("id")
        and item.get("tier") == "fulltext"
        and item.get("source_ref") == source_ref
        and item.get("mapping") == mapping
        and item.get("sha256") == digest
        for item in sources.load_manifest(run_dir).get("entries", [])
    )


def _controlled_pdf_sparse_extraction(path: Path, text: str) -> dict | None:
    """Identify multi-page task PDFs whose tiny text layer is not a full text."""
    pages = fetch_ocr.pdf_page_count(str(path))
    if pages is None or pages < 2:
        return None
    words = len(re.findall(r"[A-Za-zÀ-ÿ]{3,}", text or ""))
    minimum = pages * _CONTROLLED_PDF_MIN_WORDS_PER_PAGE
    if words >= minimum:
        return None
    return {"pages": pages, "words": words, "minimum_words": minimum}


def _not_none(value: object, fallback: object) -> object:
    return fallback if value is None else value


def _catalogue_identity_ref(entry: _ManifestEntry, ref: dict) -> dict:
    identity_ref = {entry.scheme: entry.value}
    if ref.get("source_kind") is not None:
        identity_ref["source_kind"] = ref["source_kind"]
    return identity_ref


def _reuse_catalogued_fulltext(
    run_dir: str,
    ref: dict,
    entry: _ManifestEntry,
) -> dict | None:
    """Seed an exact cached full text into this run without reading user input."""
    try:
        manifest = sources.load_manifest(run_dir)
    except Exception as exc:
        return {
            "outcome": "unreadable",
            "file": str(entry.path),
            "error": f"cannot inspect run source catalogue: {exc}",
        }
    if any(
        item.get("ref_id") == ref.get("id") and item.get("tier") == "fulltext"
        for item in manifest.get("entries", [])
    ):
        return _scored_result(
            "duplicate", entry.path, ref, signal="catalogue", score=1.0
        )

    try:
        cached = content_store.find_reusable_parsed_text(
            run_dir,
            _catalogue_identity_ref(entry, ref),
            tiers=("fulltext",),
        )
    except Exception as exc:
        return {
            "outcome": "unreadable",
            "file": str(entry.path),
            "error": f"cannot inspect parsed full-text catalogue: {exc}",
        }
    if not cached:
        return None
    try:
        with open(cached["stored_path"], encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        # The catalogue lookup marks missing paths when it can.  A race or a
        # user-cache permission change still leaves the supplied file available
        # for the ordinary deterministic admission path below.
        return None
    try:
        sources.store_text(
            run_dir,
            ref,
            "fulltext",
            cached["origin"],
            text,
            source_ref=cached.get("source_ref") or cached.get("stored_relpath"),
            mapping=cached.get("mapping") or "content_store_reuse",
            signal=cached.get("match_signal"),
            score=cached.get("match_score"),
            identity_status=cached.get("identity_status"),
            identity_note=cached.get("identity_note"),
            content_version=cached.get("content_version"),
            provenance_relation=cached.get("provenance_relation"),
            supplied_by=cached.get("supplied_by"),
            supplied_via=cached.get("supplied_via"),
            file_format=cached.get("file_format"),
            extraction_flags=cached.get("extraction_flags"),
            extraction_method=cached.get("extraction_method"),
        )
    except Exception as exc:
        return {
            "outcome": "unreadable",
            "file": str(entry.path),
            "error": f"catalogued full text could not be reused: {exc}",
        }
    return _scored_result(
        "duplicate", entry.path, ref, signal="catalogue", score=1.0
    )


def _ingest_entry(
    run_dir: str,
    ref: dict,
    resolve_result: dict | None,
    entry: _ManifestEntry,
    *,
    mapping: str = "user_manifest",
    supplied_via: str = "user_manifest",
    supplied_by: str = "user",
    method: str = "user",
    storage_source_ref: str | None = None,
    match_signal: str | None = None,
    match_score: float | None = None,
    apply_manifest_identity: bool = True,
    reuse_catalogue: bool = False,
    park_controlled_pdf: bool = False,
    identity_attested: bool = False,
) -> dict:
    rr = resolve_result or {}
    view = _reference_view(ref, rr)
    if apply_manifest_identity:
        view[entry.scheme] = entry.value
    if entry.scheme == "isbn":
        source_kind = str(view.get("source_kind") or "").casefold()
        whole_book = (
            str(view.get("source_type") or "").casefold() == "book"
            or source_kind == "book_like"
            or str(rr.get("work_type") or "").casefold() == "book"
        )
        if source_kind == "chapter_like" or not whole_book:
            return _scored_result(
                "needs_manual_confirmation", entry.path, view,
                signal="isbn", score=1.0,
            )

    if mapping == "user_manifest" or reuse_catalogue:
        catalogue_result = _reuse_catalogued_fulltext(run_dir, view, entry)
        if catalogue_result is not None:
            return catalogue_result

    def park_pdf(result: dict) -> dict:
        """Keep only controlled low-text PDFs for the existing OCR path."""
        if not park_controlled_pdf or entry.path.suffix.lower() != ".pdf":
            return result
        kept_as = sources.park_unreadable(
            run_dir,
            view,
            str(entry.path),
            origin=method,
            reason=str(result.get("error") or "PDF extraction or quality gate failed"),
            move=False,
        )
        if not kept_as:
            return result
        return {
            "status": "ocr_pending",
            "outcome": "ocr_pending",
            "file": str(entry.path),
            "ref_id": view.get("id"),
            "ref_number": view.get("ref_number"),
            "kept_as": kept_as,
        }

    try:
        parse_extract.probe_file(str(entry.path))
    except Exception as exc:
        return park_pdf({
            "outcome": "unreadable",
            "file": str(entry.path),
            "error": str(exc),
        })

    raw_html = None
    html_meta = None
    try:
        if entry.path.suffix.lower() in _HTML_EXTENSIONS:
            raw_html = entry.path.read_bytes().decode("utf-8", errors="replace")
            if fetch_html.is_challenge_html(raw_html):
                return {
                    "outcome": "challenge_or_login_page",
                    "file": str(entry.path),
                    "ref_id": view.get("id"),
                    "ref_number": view.get("ref_number"),
                }
            html_meta = fetch_html.meta_map(raw_html)
        text, fmt, extraction_meta = parse_extract.extract_text(str(entry.path))
    except Exception as exc:
        message = str(exc)
        if "challenge" in message.casefold() or "login" in message.casefold():
            return {
                "outcome": "challenge_or_login_page",
                "file": str(entry.path),
                "ref_id": view.get("id"),
                "ref_number": view.get("ref_number"),
            }
        suffix = entry.path.suffix.lower() or "<unknown>"
        return park_pdf({
            "outcome": "unreadable",
            "file": str(entry.path),
            "error": f"corrupt or unreadable {suffix} file: {message}",
        })

    if park_controlled_pdf and entry.path.suffix.lower() == ".pdf":
        sparse = _controlled_pdf_sparse_extraction(entry.path, text)
        if sparse is not None:
            return park_pdf({
                "outcome": "unreadable",
                "file": str(entry.path),
                "error": (
                    "controlled PDF extraction was too sparse for full text: "
                    f"{sparse['words']} words across {sparse['pages']} pages; "
                    f"minimum {sparse['minimum_words']}"
                ),
            })

    expected = _reference_identity_values(view, rr)
    corroborate_text = None
    if raw_html is not None and html_meta is not None:
        profile = fetch_html.landing_identity_profile(
            view,
            rr,
            html_meta,
            _candidate_context(rr, entry),
            requested_url=entry.source_ref if entry.scheme == "url" else None,
            final_url=entry.source_ref if entry.scheme == "url" else None,
        )
        if profile.get("status") == "conflict" and not identity_attested:
            return _scored_result(
                "identity_mismatch", entry.path, view,
                signal=str(profile.get("conflict_scheme") or entry.scheme), score=0.0,
            )
        observed_isbns = _isbn_meta_values(html_meta)
        if (expected.get("isbn") and observed_isbns and not (expected["isbn"] & observed_isbns)
                and not identity_attested):
            return _scored_result(
                "identity_mismatch", entry.path, view, signal="isbn", score=0.0,
            )
        if extraction_meta.get("html_outcome") == "abstract_only" or not fetch_html.html_fulltext_ok(text):
            return _scored_result(
                "insufficient_identity", entry.path, view, signal=entry.scheme, score=0.0,
            )
        corroborate_text = fetch_html.identity_corroboration_text(text, html_meta)
    elif (not identity_attested
          and _has_identifier_conflict(expected, _front_matter_identifiers(text))):
        return _scored_result(
            "identity_mismatch", entry.path, view, signal=entry.scheme, score=0.0,
        )

    source_ref = storage_source_ref or (
        entry.source_ref if mapping == "user_manifest" else str(entry.path)
    )
    if _is_duplicate(run_dir, view, source_ref, text, fmt, mapping=mapping):
        return _scored_result(
            "duplicate", entry.path, view,
            signal=match_signal or entry.scheme,
            score=float(_not_none(match_score, 1.0)),
        )

    archived = user_sources.archive_original(
        run_dir, str(entry.path), ref=view, tier="fulltext"
    )
    stored = fetch_store.try_store_fulltext(
        run_dir,
        view,
        rr,
        text,
        method=method,
        source_ref=source_ref,
        extract_method=fmt,
        corroborate_text=corroborate_text,
        candidate_context=_candidate_context(rr, entry),
        identity_extract_method=fmt,
        extraction_flags=extraction_meta.get("pdf_structure_flags"),
        storage_provenance={
            "mapping": mapping,
            "supplied_by": supplied_by,
            "supplied_via": supplied_via,
            "file_format": fmt,
        },
        identity_attested=identity_attested,
    )
    if stored.get("status") != "stored":
        status = stored.get("status")
        if status == "identity_mismatch":
            outcome = "identity_mismatch"
        elif status == "identity_inconclusive":
            outcome = "insufficient_identity"
        elif status == "quality_error":
            return park_pdf({
                "outcome": "unreadable",
                "file": str(entry.path),
                "error": str(stored.get("reason") or status or "full-text admission failed"),
            })
        else:
            return {
                "outcome": "unreadable",
                "file": str(entry.path),
                "error": str(stored.get("reason") or status or "full-text admission failed"),
            }
        return _scored_result(
            outcome,
            entry.path,
            view,
            signal=str(stored.get("corroborate_signal") or match_signal or entry.scheme),
            score=float(_not_none(
                stored.get("corroborate_score"), _not_none(match_score, 0.0)
            )),
        )

    content_store.archive_user_original(
        run_dir,
        str(entry.path),
        ref=view,
        supplied_via=supplied_via,
        file_format=fmt,
        move=False,
    )
    signal = str(stored.get("corroborate_signal") or match_signal or entry.scheme)
    score = float(_not_none(
        stored.get("corroborate_score"), _not_none(match_score, 1.0)
    ))
    probe = stored.get("identity_probe") or {}
    identity_status = str(
        stored.get("identity_status")
        or ("user_manifest_validated" if mapping == "user_manifest" else "auto_corroborated")
    )
    if mapping == "user_manifest":
        identity_note = (
            f"user manifest mapped {entry.relative_name} to {entry.source_ref}; "
            f"deterministic identity gate: {probe.get('reason_code') or 'confirmed'}"
        )
    else:
        identity_note = (
            f"automatic full-text match for {entry.relative_name} via {signal}; "
            f"deterministic identity gate: {probe.get('reason_code') or 'confirmed'}"
        )
    return {
        "outcome": "accepted_fulltext",
        "file": str(entry.path),
        "ref_id": view.get("id"),
        "ref_number": view.get("ref_number"),
        "stored_as": stored["stored_as"],
        "provided_as": archived["stored_as"],
        "signal": signal,
        "score": score,
        "identity_status": identity_status,
        "identity_note": identity_note,
    }


def ingest_task_answer(
    run_dir: str,
    ref: dict,
    resolve_result: dict | None,
    *,
    file_path: str | None = None,
    text: str | None = None,
    source_ref: str | None = None,
    origin: str,
    supplied_by: str,
    supplied_via: str,
    identity_attested: bool = False,
) -> dict:
    """Admit one controlled Fetch-task answer through ordinary source gates.

    The task's ``ref_id`` selects the destination only.  In particular, a URL
    submitted with the answer remains provenance (``source_ref``), never a
    manifest-style identity override.
    """
    if bool(file_path) == bool(text):
        raise ValueError("fetch task answer must include exactly one of file_path or text")
    if type(identity_attested) is not bool:
        raise ValueError("identity_attested must be boolean")
    if identity_attested and not (
        supplied_by == "user" and supplied_via.startswith("controlled_task_answer:")
    ):
        raise ValueError("identity attestation requires authenticated controlled task provenance")
    view = _reference_view(ref, resolve_result)
    queued_scan = _precomputed_ocr_scan(run_dir, view, source_ref)
    if queued_scan is not None and not file_path:
        raise ValueError("precomputed OCR marker requires a text file answer")
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(file_path)
        if queued_scan is not None and path.suffix.lower() != ".txt":
            raise ValueError("precomputed OCR marker requires a .txt file answer")
        kept_as = _matching_pending_ocr_scan(run_dir, view, path)
        if kept_as is not None:
            return {
                "status": "ocr_pending",
                "outcome": "ocr_pending",
                "file": str(path),
                "ref_id": view.get("id"),
                "ref_number": view.get("ref_number"),
                "kept_as": kept_as,
            }
        # ``url`` is intentionally not applied to the reference view.  It is
        # only passed as source provenance to the normal storage gate.
        entry = _ManifestEntry(
            line_number=0,
            path=path,
            relative_name=path.name,
            scheme="url",
            value=str(source_ref or path.resolve()),
        )
        result = _ingest_entry(
            run_dir,
            ref,
            resolve_result,
            entry,
            mapping="controlled_task_answer",
            supplied_via=supplied_via,
            supplied_by=supplied_by,
            method=origin,
            storage_source_ref=str(source_ref or path.resolve()),
            apply_manifest_identity=False,
            park_controlled_pdf=True,
            identity_attested=identity_attested,
        )
        if result.get("outcome") in {"accepted_fulltext", "duplicate"}:
            if queued_scan is not None:
                retired = sources.resolve_unreadable(
                    run_dir,
                    ref_id=None,
                    kept_as=queued_scan["kept_as"],
                    method="ocr:precomputed",
                )
                if not retired:
                    raise RuntimeError("precomputed OCR scan was not retired")
            return {**result, "status": "stored"}
        return {**result, "status": result.get("status") or "rejected"}

    payload_text = str(text)
    if fetch_html.is_challenge_html(payload_text):
        return {
            "status": "rejected",
            "outcome": "challenge_or_login_page",
            "reason": "submitted text is a browser challenge or login page",
        }
    if queued_scan is not None:
        raise ValueError("precomputed OCR marker requires a file answer")
    if not identity_attested and _has_identifier_conflict(
        _reference_identity_values(view, resolve_result),
        _front_matter_identifiers(payload_text),
    ):
        return {
            "status": "identity_mismatch",
            "outcome": "identity_mismatch",
            "reason": "submitted text contains conflicting bibliographic identifiers",
        }
    task_source_ref = str(source_ref or f"task_answer:{supplied_via}")
    if _is_duplicate(
        run_dir,
        view,
        task_source_ref,
        payload_text,
        "txt",
        mapping="controlled_task_answer",
    ):
        return {
            "status": "stored",
            "outcome": "duplicate",
            "ref_id": view.get("id"),
            "ref_number": view.get("ref_number"),
        }
    stored = fetch_store.try_store_fulltext(
        run_dir,
        view,
        resolve_result or {},
        payload_text,
        method=origin,
        source_ref=task_source_ref,
        extract_method="txt",
        storage_provenance={
            "mapping": "controlled_task_answer",
            "supplied_by": supplied_by,
            "supplied_via": supplied_via,
            "file_format": "txt",
        },
        identity_attested=identity_attested,
    )
    if stored.get("status") != "stored":
        status = str(stored.get("status") or "rejected")
        rejected = {
            "status": status,
            "outcome": (
                "identity_mismatch" if status == "identity_mismatch"
                else "insufficient_identity" if status == "identity_inconclusive"
                else "unreadable"
            ),
            "reason": str(stored.get("reason") or status),
        }
        for key in ("reason_code", "document_relation"):
            if key in stored:
                rejected[key] = stored[key]
        return rejected
    return {
        "status": "stored",
        "outcome": "accepted_fulltext",
        "stored_as": stored["stored_as"],
    }


def _matching_pending_ocr_scan(
    run_dir: str, ref: dict, path: Path
) -> str | None:
    """Return an existing queued scan only for an exact, pending same-ref match.

    A controlled Fetch answer may point at the exact PDF already parked by the
    automatic fetch path.  Re-extracting that scan can produce a different
    (and misleading) admission outcome, so reuse the queued evidence only
    after comparing its bytes.  Queue rows without a local, safely-contained
    file deliberately do not qualify.
    """
    ref_id = ref.get("id")
    if not ref_id or path.suffix.lower() != ".pdf":
        return None
    submitted_sha256 = _file_sha256(path)
    if submitted_sha256 is None:
        return None
    root = Path(run_dir).resolve()
    for entry in sources.load_unreadable(run_dir).get("entries", []):
        if (
            entry.get("ocr_status") != "pending"
            or entry.get("ref_id") != ref_id
            or not isinstance(entry.get("kept_as"), str)
        ):
            continue
        kept_as = entry["kept_as"]
        kept_path = Path(kept_as)
        if kept_path.is_absolute():
            continue
        try:
            queued_path = (root / kept_path).resolve()
            queued_path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not queued_path.is_file():
            continue
        if _file_sha256(queued_path) == submitted_sha256:
            return kept_as
    return None


def precomputed_ocr_source_ref(run_dir: str, ref_id: str) -> str:
    """Return the sole pending scan's immutable OCR source reference.

    This is deliberately a narrow bridge between an authenticated OCR text file
    and the exact scan it transcribes.  Ambiguous, missing, unsafe, or non-PDF
    queue entries cannot be represented as an OCR task answer.
    """
    entries = _pending_ocr_pdf_entries(run_dir, ref_id)
    if len(entries) != 1:
        raise ValueError("expected exactly one safely-contained pending unreadable PDF")
    digest = _file_sha256(entries[0]["path"])
    if digest is None:
        raise ValueError("pending unreadable PDF is unavailable")
    return f"{_PRECOMPUTED_OCR_URN_PREFIX}{digest}"


def is_precomputed_ocr_source_ref(source_ref: object) -> bool:
    """Whether a source reference reserves the precomputed-OCR namespace."""
    return isinstance(source_ref, str) and source_ref.startswith(_PRECOMPUTED_OCR_URN_PREFIX)


def _precomputed_ocr_scan(
    run_dir: str, ref: dict, source_ref: str | None
) -> dict | None:
    """Validate a reserved OCR marker against exactly one queued scan.

    A malformed marker is never treated as an ordinary URL: that would permit a
    caller to downgrade an OCR declaration into unverified web provenance.
    """
    if not is_precomputed_ocr_source_ref(source_ref):
        return None
    match = _PRECOMPUTED_OCR_URN_RE.fullmatch(str(source_ref))
    if match is None:
        raise ValueError("precomputed OCR source reference is malformed")
    ref_id = ref.get("id")
    if not isinstance(ref_id, str) or not ref_id:
        raise ValueError("precomputed OCR reference is missing")
    entries = _pending_ocr_pdf_entries(run_dir, ref_id)
    if len(entries) != 1:
        raise ValueError("precomputed OCR scan is missing or ambiguous")
    if _file_sha256(entries[0]["path"]) != match.group(1):
        raise ValueError("precomputed OCR scan hash does not match pending bytes")
    return entries[0]


def _pending_ocr_pdf_entries(run_dir: str, ref_id: str) -> list[dict]:
    root = Path(run_dir).resolve()
    entries = []
    for entry in sources.load_unreadable(run_dir).get("entries", []):
        kept_as = entry.get("kept_as")
        if (
            entry.get("ocr_status") != "pending"
            or entry.get("ref_id") != ref_id
            or not isinstance(kept_as, str)
            or Path(kept_as).suffix.lower() != ".pdf"
        ):
            continue
        kept_path = Path(kept_as)
        if kept_path.is_absolute():
            continue
        try:
            path = (root / kept_path).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            entries.append({"kept_as": kept_as, "path": path})
    return entries


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _rename_not_found(path: Path) -> tuple[Path, str | None]:
    """Mark an unresolved supplied file without overwriting any user file."""
    if path.name.casefold().startswith(_NOT_FOUND_PREFIX.casefold()):
        return path, None
    candidate = path.with_name(_NOT_FOUND_PREFIX + path.name)
    index = 2
    while candidate.exists():
        candidate = path.with_name(
            f"{_NOT_FOUND_PREFIX}{path.stem} ({index}){path.suffix}"
        )
        index += 1
    path.rename(candidate)
    return candidate, str(path)


def _mark_not_found(result: dict, path: Path) -> dict:
    """Keep unresolved input visible for a later explicit manifest decision."""
    try:
        renamed, renamed_from = _rename_not_found(path)
    except OSError as exc:
        result["file"] = str(path)
        result["error"] = f"could not rename unresolved full text: {exc}"
        return result
    result["file"] = str(renamed)
    if renamed_from is not None:
        result["renamed_from"] = renamed_from
    return result


def _automatic_read(path: Path) -> dict:
    """Probe first so malformed files retain their specific closed outcome."""
    try:
        parse_extract.probe_file(str(path))
    except Exception as exc:
        return {"outcome": "unreadable", "file": str(path), "error": str(exc)}
    if path.suffix.lower() in _HTML_EXTENSIONS:
        try:
            raw_html = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            return {"outcome": "unreadable", "file": str(path), "error": str(exc)}
        if fetch_html.is_challenge_html(raw_html):
            return {"outcome": "challenge_or_login_page", "file": str(path)}
    try:
        parsed = user_sources.read_user_source(str(path), tier="fulltext")
    except Exception as exc:
        return {
            "outcome": "unreadable",
            "file": str(path),
            "error": f"corrupt or unreadable {path.suffix.lower() or '<unknown>'} file: {exc}",
        }
    if parsed.get("outcome") != "ok":
        return {
            "outcome": parsed.get("outcome", "unreadable"),
            "file": str(path),
            "error": parsed.get("error"),
        }
    return parsed


def _automatic_entry(
    path: Path,
    root: Path,
    ref: dict,
    signal: str,
) -> _ManifestEntry:
    """A local carrier for existing admission code, never a manifest mapping."""
    return _ManifestEntry(
        0,
        path,
        path.resolve().relative_to(root).as_posix(),
        signal if signal in {"doi", "pmid"} else "doi",
        str(ref.get(signal) or "") if signal in {"doi", "pmid"} else "",
    )


def _ingest_automatic(
    run_dir: str,
    path: Path,
    root: Path,
    references: dict[str, dict],
    resolved: dict[str, dict],
) -> dict:
    parsed = _automatic_read(path)
    if parsed.get("outcome") != "ok":
        return parsed

    candidates: list[tuple[float, str, dict, dict]] = []
    failures: list[str] = []
    for ref_id in sorted(references):
        ref = references[ref_id]
        resolve_result = resolved.get(ref_id) or {}
        view = _reference_view(ref, resolve_result)
        signal, score, failure = user_sources.corroborate_user(
            view, parsed["text"], parsed.get("identity")
        )
        if failure is None:
            candidates.append((score, signal or "tokens", ref, resolve_result))
        else:
            failures.append(failure)

    candidates.sort(key=lambda item: (-item[0], str(item[2].get("id") or "")))
    if not candidates:
        failure = (
            failures[0]
            if failures and len(set(failures)) == 1
            else "needs_manual_confirmation"
        )
        return _mark_not_found(
            _scored_result(failure, path, None, score=0.0), path
        )
    top_score = candidates[0][0]
    tied = [item for item in candidates if item[0] == top_score]
    if len(tied) > 1:
        return _mark_not_found(
            _scored_result(
                "needs_manual_confirmation", path, None,
                reason="top_score_tie", score=top_score,
            ),
            path,
        )
    score, signal, ref, resolve_result = candidates[0]
    if score < sources.AUTO_THRESHOLD:
        return _mark_not_found(
            _scored_result(
                "needs_manual_confirmation", path, ref, signal=signal, score=score
            ),
            path,
        )
    mapping = "deterministic" if signal in {"doi", "pmid"} else "model_corroborated"
    result = _ingest_entry(
        run_dir,
        ref,
        resolve_result,
        _automatic_entry(
            path, root, _reference_view(ref, resolve_result), signal
        ),
        mapping=mapping,
        supplied_via="user_fulltext",
        match_signal=signal,
        match_score=score,
        apply_manifest_identity=False,
        reuse_catalogue=signal in {"doi", "pmid"},
    )
    if result.get("outcome") in {
        "identity_mismatch",
        "insufficient_identity",
        "needs_manual_confirmation",
    }:
        return _mark_not_found(result, path)
    return result


def ingest_directory(
    run_dir: str,
    ref_by_id: dict[str, dict],
    resolve_map: dict[str, dict],
    directory: str,
) -> list[dict]:
    """Use manifest mappings first, then conservatively auto-identify other files."""
    allowed = {str(extension).lower() for extension in parse_extract.supported_extensions()}
    entries, errors, mentioned = _read_manifest(directory, allowed)
    results = _manifest_errors(str(Path(directory) / INDEX_FILENAME), errors)
    references = ref_by_id or {}
    resolved = resolve_map or {}
    for entry in entries:
        matches = []
        for ref_id in sorted(references):
            ref = references[ref_id]
            values = _reference_identity_values(ref, resolved.get(ref_id))
            if entry.value in values.get(entry.scheme, set()):
                matches.append((ref, resolved.get(ref_id) or {}))
        if len(matches) != 1:
            outcome = "needs_manual_confirmation" if len(matches) > 1 else "insufficient_identity"
            results.append(
                _scored_result(
                    outcome,
                    entry.path,
                    matches[0][0] if len(matches) == 1 else None,
                    signal=entry.scheme,
                    score=1.0 if matches else 0.0,
                )
            )
            continue
        ref, resolve_result = matches[0]
        results.append(_ingest_entry(run_dir, ref, resolve_result, entry))

    root = Path(directory).resolve()
    for path in _discover_supported(directory, allowed):
        relative = path.resolve().relative_to(root).as_posix()
        if _relative_key(relative) in mentioned:
            continue
        if path.name.casefold().startswith(_NOT_FOUND_PREFIX.casefold()):
            continue
        results.append(_ingest_automatic(run_dir, path, root, references, resolved))
    return results
