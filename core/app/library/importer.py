# core/app/library/importer.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Review-first import of user documents into the personal content library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.fetch.extraction.pdf import _quality as _pdf_quality_ok
from core.fetch.storage import content_store
from core.parse import extract as parse_extract
from core.parse import manuscript_identity
from core.resolve import sources
from core.resolve.user_sources import corroborate_user
from core.shared.filesystem_paths import io_path, logical_path
from core.app.library import metadata as library_metadata


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d+$")
_IDENTITY_FIELDS = ("title", "doi", "pmid", "isbn", "arxiv_id", "pmcid", "year", "authors")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(io_path(path)).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    Path(io_path(path.parent)).mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    Path(io_path(temp)).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(io_path(temp), io_path(path))


class LibraryImportError(ValueError):
    """A library import failure with a stable, display-independent reason code."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        params: dict[str, Any] | None = None,
        raw_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = code
        self.reason_params = dict(params or {})
        self.raw_reason = raw_reason or f"ValueError: {message}"


class PendingLibraryReviewError(RuntimeError):
    """A pending-review load failure with a stable reason code."""

    def __init__(self, message: str, *, code: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason_code = code
        self.reason_params = dict(params or {})
        self.raw_reason = f"RuntimeError: {message}"


class _LibraryImportProgressError(Exception):
    """Keep progress callback failures out of per-file import error handling."""


def _identity_result(
    status: str,
    reason: str | None = None,
    score: float | None = None,
    ref_id: str | None = None,
    matched_ref: dict[str, Any] | None = None,
    method: str | None = None,
    reason_code: str | None = None,
    reason_params: dict[str, Any] | None = None,
) -> tuple[str, str | None, float | None, str | None, dict[str, Any] | None, str | None, str | None, dict[str, Any]]:
    return (
        status, reason, score, ref_id, matched_ref, method, reason_code,
        dict(reason_params or {}),
    )


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, (LibraryImportError, PendingLibraryReviewError)):
        return exc.raw_reason
    return f"{type(exc).__name__}: {exc}"


def _prefer_native_pdf_metadata_title(text: str, evidence: dict[str, Any]) -> bool:
    """Prefer PDFInfo over a masthead only with an exact native title block."""
    metadata_title = evidence.get("metadata_title")
    layout_title = evidence.get("layout_title")
    if (
        not isinstance(metadata_title, str)
        or not manuscript_identity._is_title_like(metadata_title)
        or not isinstance(layout_title, str)
        or manuscript_identity._title_key(metadata_title)
        == manuscript_identity._title_key(layout_title)
    ):
        return False
    target = manuscript_identity._title_key(metadata_title)
    front = manuscript_identity.bounded_front_matter(text)
    body = re.search(
        r"(?im)^[ \t]*(?:\d+(?:\.\d+)*[.)]?[ \t]+)?"
        r"(?:introduction|methods|results|discussion|conclusions?)"
        r"[ \t]*[:.]?[ \t]*$",
        front,
    )
    if body is not None:
        front = front[:body.start()]
    lines = [
        line.strip()
        for line in front.splitlines()[:80]
        if line.strip()
    ]
    for start in range(len(lines)):
        for end in range(start + 1, min(start + 6, len(lines)) + 1):
            if manuscript_identity._title_key(" ".join(lines[start:end])) == target:
                evidence.update({
                    "title": metadata_title,
                    "title_status": "validated_local",
                    "title_method": "pdf_metadata_front_matter",
                })
                return True
    return False


def _first_identifiers(evidence: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {
        "doi": [], "pmid": [], "isbn": [], "arxiv_id": [], "pmcid": [],
    }
    seen: set[tuple[str, str]] = set()
    for item in evidence.get("identifiers") or ():
        if not isinstance(item, dict):
            continue
        raw_scheme = str(item.get("scheme") or "").strip().casefold()
        scheme = {"arxiv": "arxiv_id", "pmc": "pmcid"}.get(raw_scheme, raw_scheme)
        if scheme not in found:
            continue
        value = str(item.get("value") or "").strip()
        canonical = library_metadata._canonical(scheme, value) or value
        key = (scheme, canonical.casefold())
        if value and key not in seen:
            seen.add(key)
            found[scheme].append(canonical)
    return found


def _manual_first_author_surname(authors: str) -> str | None:
    first_author = re.split(r"\s*(?:;|\band\b|&)\s*", authors, maxsplit=1, flags=re.I)[0].strip()
    if not first_author:
        return None

    if "," in first_author:
        family_name, given_names = first_author.split(",", maxsplit=1)
        if not given_names.strip():
            return None
        tokens = re.findall(r"[\w’'-]+", family_name)
        return tokens[-1] if tokens else None

    tokens = re.findall(r"[\w’'-]+", first_author)
    if not tokens or all(len(token) == 1 for token in tokens):
        return None
    if len(tokens) > 1 and all(len(token) == 1 for token in tokens[1:]):
        return tokens[0]
    return tokens[-1]


def _simple_row(record: dict[str, Any], session_id: str, digest: str) -> dict[str, Any]:
    evidence = record.get("initial_evidence") or {}
    identifiers = _first_identifiers(evidence)
    return {
        "file": record.get("source_path"),
        "source_extension": record.get("source_extension"),
        "content_family": record.get("content_family"),
        "extraction_format": record.get("extraction_format"),
        "extraction_method": record.get("extraction_method"),
        "sha256": digest,
        "status": record.get("status", "needs_review"),
        "title": record.get("title") or evidence.get("title") or record.get("matched_title"),
        "doi": record.get("doi") or (identifiers["doi"][0] if len(identifiers["doi"]) == 1 else None),
        "pmid": record.get("pmid") or (identifiers["pmid"][0] if len(identifiers["pmid"]) == 1 else None),
        "isbn": record.get("isbn") or (identifiers["isbn"][0] if len(identifiers["isbn"]) == 1 else None),
        "arxiv_id": record.get("arxiv_id") or (identifiers["arxiv_id"][0] if len(identifiers["arxiv_id"]) == 1 else None),
        "pmcid": record.get("pmcid") or (identifiers["pmcid"][0] if len(identifiers["pmcid"]) == 1 else None),
        "year": record.get("year"),
        "authors": record.get("authors"),
        "method": record.get("method"),
        "reason": record.get("reason"),
        "reason_code": record.get("reason_code"),
        "reason_params": dict(record.get("reason_params") or {}),
        "score": record.get("score"),
        "ref_id": record.get("ref_id"),
        "_identifier_choices": {
            scheme: list(values) for scheme, values in _first_identifiers(evidence).items()
            if values
        },
        "_session_id": session_id,
        "_file_id": digest,
    }


def _identity_snapshot(row: dict[str, Any]) -> dict[str, object]:
    """Canonical, UI-independent identity used to bind review approval to save."""
    snapshot: dict[str, object] = {}
    for scheme in ("doi", "pmid", "isbn", "arxiv_id", "pmcid"):
        snapshot[scheme] = library_metadata._canonical(scheme, row.get(scheme))
    title = " ".join(str(row.get("title") or "").split()).casefold()
    authors = " ".join(str(row.get("authors") or "").split()).casefold()
    year_text = str(row.get("year") or "").strip()
    snapshot["title"] = title or None
    snapshot["authors"] = authors or None
    snapshot["year"] = (
        int(year_text) if len(year_text) == 4 and year_text.isdigit() else None
    )
    return snapshot


class LibraryImporter:
    """Inspect and save supported documents with an append-only review trail."""

    def __init__(
        self,
        anchor_run: str,
        environ: dict[str, str],
        references: list[dict] = (),
    ) -> None:
        self.anchor_run = anchor_run
        self.environ = dict(environ)
        self.references = [dict(ref) for ref in references if isinstance(ref, dict)]
        state_root = logical_path(content_store.storage_root(anchor_run, self.environ))
        self.import_root = state_root.parent / "library_imports"
        self.session_id = str(uuid.uuid4())

    def _session_dir(self, session_id: str) -> Path:
        try:
            parsed = uuid.UUID(session_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise LibraryImportError(
                "invalid library import session", code="invalid_review_record"
            ) from exc
        if str(parsed) != session_id:
            raise LibraryImportError(
                "invalid library import session", code="invalid_review_record"
            )
        root = logical_path(self.import_root)
        session = logical_path(root / session_id)
        if session.parent != root:
            raise LibraryImportError(
                "library import session escapes its state directory",
                code="invalid_review_record",
            )
        return session

    def _session_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "review.json"

    def _load_session(self, session_id: str) -> dict[str, Any]:
        try:
            path = self._session_path(session_id)
            path_io = Path(io_path(path))
            if path_io.is_symlink():
                raise LibraryImportError(
                    "library import review record cannot be a symlink",
                    code="invalid_review_record",
                )
            payload = json.loads(path_io.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("session_id") != session_id
                or not isinstance(payload.get("files"), dict)
            ):
                raise LibraryImportError(
                    "invalid library import review record",
                    code="invalid_review_record",
                )
            return payload
        except LibraryImportError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            raise LibraryImportError(
                f"cannot read library import review record: {detail}",
                code="invalid_review_record",
            ) from exc

    @staticmethod
    def _files(paths: list[str]) -> list[Path]:
        found: dict[str, Path] = {}
        supported = {str(extension).casefold() for extension in parse_extract.supported_extensions()}
        for raw in paths:
            path = Path(raw).expanduser()
            path_io = Path(io_path(path))
            is_directory = path_io.is_dir()
            directory_root: Path | None = None
            if is_directory:
                # Folder scans skip links; an explicitly selected file is handled below.
                try:
                    directory_root = logical_path(path_io, strict=True)
                except OSError:
                    continue
                candidates: list[Path] = []
                pending = [directory_root]
                while pending:
                    current = pending.pop()
                    try:
                        with os.scandir(io_path(current)) as entries:
                            for entry in entries:
                                candidate = Path(entry.path)
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                except OSError:
                                    continue
                                if _is_link_or_reparse(candidate, info):
                                    continue
                                if stat.S_ISDIR(info.st_mode):
                                    pending.append(candidate)
                                elif stat.S_ISREG(info.st_mode):
                                    candidates.append(candidate)
                    except OSError:
                        continue
            else:
                candidates = [path_io]
            for candidate in candidates:
                if candidate.suffix.casefold() not in supported:
                    continue
                if not is_directory and not candidate.is_file():
                    continue
                try:
                    resolved = logical_path(candidate, strict=True)
                except OSError:
                    continue
                if directory_root is not None:
                    try:
                        resolved.relative_to(directory_root)
                    except ValueError:
                        continue
                found.setdefault(os.path.normcase(str(resolved)), resolved)
        return [found[key] for key in sorted(found)]

    @staticmethod
    def _file_reason_code(extension: str, *, pdf: str, document: str) -> str:
        return pdf if extension.casefold() == ".pdf" else document

    @staticmethod
    def _staged_extension(record: dict[str, Any]) -> str:
        extension = record.get("source_extension")
        supported = {
            str(value).casefold() for value in parse_extract.supported_extensions()
        }
        if (
            not isinstance(extension, str)
            or not extension
            or Path(extension).name != extension
            or "/" in extension
            or "\\" in extension
            or extension.casefold() not in supported
        ):
            raise LibraryImportError(
                "library import review has an invalid or unsupported file extension",
                code="invalid_review_record",
            )
        return extension

    def _staged_path(self, session_id: str, digest: str, record: dict[str, Any]) -> Path:
        extension = self._staged_extension(record)
        return self._session_dir(session_id) / "staged" / f"{digest}{extension}"

    def _read_staged_document(
        self,
        session_id: str,
        digest: str,
        record: dict[str, Any],
        *,
        check_initial_format: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[Path, str, str, dict[str, Any], str, str]:
        extension = self._staged_extension(record)
        staged_dir = self._session_dir(session_id) / "staged"
        staged = staged_dir / f"{digest}{extension}"
        staged_io = Path(io_path(staged))
        missing_code = self._file_reason_code(
            extension, pdf="staged_pdf_missing", document="staged_file_missing",
        )
        changed_code = self._file_reason_code(
            extension, pdf="staged_pdf_changed", document="staged_file_changed",
        )
        unreadable_code = self._file_reason_code(
            extension, pdf="unreadable_pdf", document="unreadable_file",
        )
        if staged_io.is_symlink() or not staged_io.is_file():
            raise LibraryImportError("staged document is unavailable", code=missing_code)
        if logical_path(staged, strict=True).parent != logical_path(staged_dir, strict=True):
            raise LibraryImportError(
                "staged document escapes its review session", code=missing_code,
            )
        if _sha256(staged) != digest:
            raise LibraryImportError(
                "staged document integrity check failed", code=changed_code,
            )
        try:
            probe = parse_extract.probe_file(io_path(staged))
            content_family = probe.get("detected_format") if isinstance(probe, dict) else None
            if not isinstance(content_family, str) or not content_family.strip():
                raise ValueError("file probe did not report a content format")
            if progress is not None:
                progress("extracting")
            text, fmt, metadata = parse_extract.extract_text(io_path(staged))
            if not isinstance(fmt, str) or not fmt.strip():
                raise ValueError("extractor did not report a document format")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("document extraction produced no readable text")
            if not isinstance(metadata, dict):
                raise ValueError("extractor did not return metadata")
            method = self._extraction_method(fmt, metadata)
            if check_initial_format:
                for field in ("content_family", "extraction_format", "extraction_method"):
                    value = record.get(field)
                    if not isinstance(value, str) or not value.strip():
                        raise LibraryImportError(
                            f"library import review is missing valid {field} metadata",
                            code="invalid_review_record",
                        )
                if content_family != record["content_family"]:
                    raise ValueError("staged document content format changed")
                if fmt != record["extraction_format"]:
                    raise ValueError("staged document extraction format changed")
                if method != record["extraction_method"]:
                    raise ValueError("staged document extraction method changed")
            if extension.casefold() == ".pdf" and fmt != "pdf":
                raise ValueError("PDF extractor returned an unexpected document format")
            if fmt == "pdf" and not _pdf_quality_ok(text):
                raise ValueError("PDF text failed the quality gate")
            if progress is not None:
                progress("checking")
        except _LibraryImportProgressError:
            raise
        except LibraryImportError:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            raise LibraryImportError(
                detail, code=unreadable_code, raw_reason=detail,
            ) from exc
        if _sha256(staged) != digest:
            raise LibraryImportError(
                "staged document changed during validation", code=changed_code,
            )
        return staged, text, fmt, metadata, content_family, method

    @staticmethod
    def _extraction_method(fmt: str, metadata: dict[str, Any]) -> str:
        value = metadata.get("pdf_method") or metadata.get("extraction_method") or metadata.get("method")
        if value:
            return str(value)
        return "pdf_extractor" if fmt == "pdf" else f"extractor:{fmt}"

    def _identify(self, text: str, evidence: dict[str, Any], fmt: str = "pdf") -> tuple[str, str | None, float | None, str | None, dict[str, Any] | None, str | None, str | None, dict[str, Any]]:
        identifiers = _first_identifiers(evidence)
        native_metadata_title = (
            fmt == "pdf" and _prefer_native_pdf_metadata_title(text, evidence)
        )
        metadata_title = evidence.get("metadata_title")
        layout_title = evidence.get("layout_title")
        if (
            metadata_title and layout_title and not native_metadata_title
            and manuscript_identity._title_key(metadata_title)
            != manuscript_identity._title_key(layout_title)
        ):
            return _identity_result(
                "needs_review",
                f"conflicting local document title evidence: metadata='{metadata_title}'; first-page layout='{layout_title}'",
                reason_code="title_conflict",
                reason_params={
                    "metadata_title": str(metadata_title),
                    "layout_title": str(layout_title),
                },
            )
        if any(len(items) > 1 for items in identifiers.values()):
            return _identity_result(
                "needs_review", "multiple front-matter identifiers need review",
                reason_code="multiple_identifiers",
            )

        candidates: list[tuple[float, dict[str, Any], str | None]] = []
        metadata = {
            "doi": identifiers["doi"][0] if identifiers["doi"] else None,
            "pmid": identifiers["pmid"][0] if identifiers["pmid"] else None,
            "title": evidence.get("title"),
        }
        for ref in self.references:
            # A document's declared front-matter identifiers are the strongest
            # signal. A different identifier is a conflict, never a score.
            if metadata["doi"] and ref.get("doi"):
                if str(ref["doi"]).casefold().rstrip(".") != str(metadata["doi"]).casefold().rstrip("."):
                    continue
            if metadata["pmid"] and ref.get("pmid"):
                if str(ref["pmid"]).strip() != str(metadata["pmid"]).strip():
                    continue
            signal, score, failure = corroborate_user(ref, text, metadata)
            if failure is None and score >= sources.AUTO_THRESHOLD:
                probe = sources.document_identity_probe(ref, text)
                if (
                    probe.get("decision") == "confirmed"
                    and probe.get("ok") is True
                    and probe.get("author_ok") is not False
                ):
                    candidates.append((float(score), ref, signal))
        candidates.sort(key=lambda row: (-row[0], str(row[1].get("id") or "")))
        if candidates:
            groups: list[dict[str, Any]] = []
            stable_schemes = {"doi", "pmid", "isbn", "url", "title_author_year"}
            for candidate in candidates:
                _score, ref, _signal = candidate
                aliases = {
                    alias for scheme, alias in content_store.identity_aliases_for_ref(ref)
                    if scheme in stable_schemes
                }
                # Shared title/author/year cannot erase distinct strong IDs.
                strong_aliases = {
                    alias for alias in aliases
                    if not alias.startswith("title_author_year:")
                }
                aliases = strong_aliases or aliases
                matching = []
                for group in groups:
                    if not aliases & group["aliases"]:
                        continue
                    conflict = False
                    for scheme in ("doi", "pmid", "isbn"):
                        prefix = f"{scheme}:"
                        observed = {alias for alias in aliases if alias.startswith(prefix)}
                        grouped = {alias for alias in group["aliases"] if alias.startswith(prefix)}
                        if observed and grouped and observed != grouped:
                            conflict = True
                            break
                    if not conflict:
                        matching.append(group)
                if not matching:
                    groups.append({"aliases": aliases, "candidates": [candidate]})
                    continue
                target = matching[0]
                target["aliases"].update(aliases)
                target["candidates"].append(candidate)
                for duplicate in matching[1:]:
                    target["aliases"].update(duplicate["aliases"])
                    target["candidates"].extend(duplicate["candidates"])
                    groups.remove(duplicate)
            representatives = []
            for group in groups:
                alternatives = sorted(
                    group["candidates"],
                    key=lambda item: (-item[0], str(item[1].get("id") or "")),
                )
                best = alternatives[0]
                merged = dict(best[1])
                for _score, alternate, _signal in alternatives[1:]:
                    for key, value in alternate.items():
                        if merged.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                            merged[key] = value
                representatives.append((best[0], merged, best[2]))
            representatives.sort(key=lambda row: (-row[0], str(row[1].get("id") or "")))
            best_score, best_ref, best_signal = representatives[0]
            tied = len(representatives) > 1 and representatives[1][0] == best_score
            if tied:
                return _identity_result(
                    "needs_review", "more than one reference matches this document", best_score,
                    reason_code="multiple_matches",
                )
            identity_method = "+".join(
                name for name in (best_signal, evidence.get("title_method")) if name
            ) or f"local_{fmt}_identity"
            return _identity_result(
                "identified", score=best_score,
                ref_id=str(best_ref.get("id") or "") or None,
                matched_ref=best_ref, method=identity_method,
            )
        has_identifier = any(identifiers.values())
        if not evidence.get("title") and not has_identifier:
            return _identity_result(
                "unidentified", "no reliable title or identifier was found in the document",
                reason_code="no_identity",
            )
        if has_identifier and evidence.get("title"):
            ref = {"title": evidence["title"]}
            if identifiers["doi"]:
                ref["doi"] = identifiers["doi"][0]
            if identifiers["pmid"]:
                ref["pmid"] = identifiers["pmid"][0]
            probe = sources.document_identity_probe(ref, text)
            if (
                probe.get("decision") == "confirmed"
                and probe.get("ok") is True
                and probe.get("author_ok") is not False
            ):
                return _identity_result(
                    "identified", score=1.0,
                    method="+".join(
                        name for name in ("front_matter_identifier", evidence.get("title_method")) if name
                    ),
                )
            reason_code = probe.get("reason_code")
            return _identity_result(
                "needs_review",
                str(probe.get("reason") or "local identity remains inconclusive"),
                reason_code="identity_inconclusive",
                reason_params={"probe_reason_code": str(reason_code)} if reason_code else {},
            )
        return _identity_result(
            "needs_review", "confirm title and add author and year before saving",
            reason_code="missing_author_year",
        )

    def _auto_resolve_title_conflict_from_doi(
        self,
        record: dict[str, Any],
        *,
        staged: Path,
        text: str,
        session_id: str,
        digest: str,
    ) -> None:
        """Corroborate a conflicting PDF property title from one declared DOI."""
        if record.get("status") != "needs_review" or record.get("reason_code") != "title_conflict":
            return
        identifiers = _first_identifiers(record.get("initial_evidence") or {})
        if len(identifiers["doi"]) != 1 or any(
            len(values) > 1 for values in identifiers.values()
        ):
            return
        doi = library_metadata._canonical("doi", identifiers["doi"][0])
        if not doi:
            return

        original_snapshot = {key: record.get(key) for key in _IDENTITY_FIELDS}
        candidate = _simple_row(record, session_id, digest)
        provenance: dict[str, Any] | None = None
        identity: dict[str, Any] | None = None
        title_identity: dict[str, Any] | None = None
        reason = None
        reason_code = None
        accepted = False

        try:
            if _sha256(staged) != digest:
                raise LibraryImportError(
                    "staged PDF integrity check failed", code="staged_pdf_changed"
                )

            lookup = library_metadata.lookup_identity("doi", doi)
            fields = lookup.get("fields") if isinstance(lookup, dict) else None
            raw_provenance = lookup.get("provenance") if isinstance(lookup, dict) else None
            if not isinstance(fields, dict) or not isinstance(raw_provenance, dict):
                raise LibraryImportError(
                    "exact DOI metadata did not include fields and provenance",
                    code="identity_metadata_unavailable",
                )
            provenance = dict(raw_provenance)
            if (
                library_metadata._canonical("doi", fields.get("doi")) != doi
                or library_metadata._canonical("doi", provenance.get("requested_identifier")) != doi
                or library_metadata._canonical("doi", provenance.get("returned_identifier")) != doi
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("response_sha256") or ""))
            ):
                raise library_metadata.LibraryMetadataError(
                    "metadata response did not attest the requested DOI",
                    code="identity_metadata_mismatch",
                    provenance=provenance,
                )

            candidate = _simple_row(record, session_id, digest)
            for key in _IDENTITY_FIELDS:
                candidate[key] = None
            candidate.update(fields)
            # Preserve other unique identifiers observed in this same front matter.
            for scheme, values in identifiers.items():
                if scheme != "doi" and len(values) == 1 and not candidate.get(scheme):
                    candidate[scheme] = values[0]
            candidate["doi"] = doi
            candidate["file"] = record.get("source_path")
            candidate["sha256"] = digest
            candidate["_session_id"] = session_id
            candidate["_file_id"] = digest

            if _sha256(staged) != digest:
                raise LibraryImportError(
                    "staged PDF changed during metadata lookup", code="staged_pdf_changed"
                )
            ref = self._validated_ref(candidate, record)
            title_identity = sources.manuscript_title_identity_probe(
                str(candidate.get("title") or ""), text
            )
            identity = sources.document_identity_probe(ref, text)
            title_ok = title_identity.get("ok") is True
            identity_ok = bool(
                identity.get("decision") == "confirmed"
                and identity.get("ok") is True
                and identity.get("author_ok") is not False
            )
            if not title_ok:
                reason_code = str(
                    title_identity.get("reason_code") or "identity_pdf_mismatch"
                )
                reason = str(
                    title_identity.get("reason")
                    or "DOI metadata title was not confirmed in PDF front matter"
                )
            elif not identity_ok:
                if identity.get("author_ok") is False:
                    reason_code = "author_mismatch"
                    reason = "front-matter author did not confirm the selected identity"
                else:
                    reason_code = "identity_pdf_mismatch"
                    reason = str(identity.get("reason") or "PDF identity was not confirmed")
            else:
                if _sha256(staged) != digest:
                    raise LibraryImportError(
                        "staged PDF changed during identity validation",
                        code="staged_pdf_changed",
                    )
                accepted = True
        except Exception as exc:
            provenance = getattr(exc, "provenance", None) or provenance
            accepted = False
            reason = str(exc)
            reason_code = getattr(exc, "reason_code", None) or "identity_metadata_unavailable"

        if accepted:
            record["method"] = "doi+front_matter_identifier"
        self._record_automatic_doi_review(
            record,
            original_snapshot=original_snapshot,
            candidate=candidate,
            provenance=provenance,
            identity=identity,
            title_identity=title_identity,
            accepted=accepted,
            reason=reason,
            reason_code=reason_code,
        )

    @staticmethod
    def _record_automatic_doi_review(
        record: dict[str, Any],
        *,
        original_snapshot: dict[str, Any],
        candidate: dict[str, Any],
        provenance: dict[str, Any] | None,
        identity: dict[str, Any] | None,
        title_identity: dict[str, Any] | None,
        accepted: bool,
        reason: str | None,
        reason_code: str | None,
    ) -> None:
        after = (
            {key: candidate.get(key) for key in _IDENTITY_FIELDS}
            if accepted else dict(original_snapshot)
        )
        if accepted:
            for key, value in after.items():
                record[key] = value if value not in (None, "") else None
            record["accepted_identity"] = {
                "fields": _identity_snapshot(candidate),
                "source": "automatic_identifier_metadata",
            }
            record.update({
                "status": "identified",
                "reason": None,
                "reason_code": None,
                "reason_params": {},
            })
        else:
            record.pop("accepted_identity", None)

        lookup_copy = dict(provenance) if provenance else None
        identity_copy = dict(identity) if isinstance(identity, dict) else None
        title_copy = dict(title_identity) if isinstance(title_identity, dict) else None
        record["identity_review"] = {
            "accepted": bool(accepted),
            "source": "automatic_identifier_metadata",
            "identity_snapshot": _identity_snapshot(candidate),
            "metadata_lookup": lookup_copy,
            "document_identity": identity_copy,
            "returned_title_identity": title_copy,
            "reason": reason,
            "reason_code": reason_code,
        }
        record.setdefault("revisions", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "before": dict(original_snapshot),
            "after": after,
            "decision": (
                "automatic_identity_accepted" if accepted
                else "automatic_identity_rejected"
            ),
            "evidence": {
                "source": "automatic_identifier_metadata",
                "metadata_lookup": lookup_copy,
                "document_identity": identity_copy,
                "returned_title_identity": title_copy,
                "reason": reason,
                "reason_code": reason_code,
            },
        })

    def inspect(
        self,
        paths: list[str],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        def emit(event: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(event)
            except Exception as exc:
                raise _LibraryImportProgressError("library import progress callback failed") from exc

        emit({"stage": "scanning", "completed": 0, "total": 0})
        files = self._files(paths)
        total = len(files)
        emit({
            "stage": "queued", "files": [str(path) for path in files],
            "completed": 0, "total": total,
        })
        session_dir = self._session_dir(self.session_id)
        staged_dir = session_dir / "staged"
        review_path = self._session_path(self.session_id)
        if Path(io_path(review_path)).exists():
            payload = self._load_session(self.session_id)
        else:
            payload = {"session_id": self.session_id, "files": {}}
        rows: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        for index, source in enumerate(files):
            base_event = {
                "index": index, "file": str(source), "completed": index, "total": total,
            }
            extension = source.suffix
            try:
                emit({**base_event, "stage": "reading"})
                digest = _sha256(source)
                if digest in seen:
                    if seen[digest].casefold() != extension.casefold():
                        rows.append({
                            "file": str(source), "sha256": digest,
                            "source_extension": extension, "status": "unreadable",
                            "title": None, "doi": None, "pmid": None, "isbn": None,
                            "arxiv_id": None, "pmcid": None, "year": None,
                            "authors": None, "method": None,
                            "reason": "identical file bytes were supplied with conflicting extensions",
                            "reason_code": "source_format_conflict", "reason_params": {},
                            "score": None, "ref_id": None,
                        })
                    continue
                seen[digest] = extension
                existing = payload["files"].get(digest)
                if existing is not None:
                    if str(existing.get("source_extension") or "").casefold() != extension.casefold():
                        rows.append({
                            "file": str(source), "sha256": digest,
                            "source_extension": extension, "status": "unreadable",
                            "title": None, "doi": None, "pmid": None, "isbn": None,
                            "arxiv_id": None, "pmcid": None, "year": None,
                            "authors": None, "method": None,
                            "reason": "identical file bytes were previously reviewed with a different extension",
                            "reason_code": "source_format_conflict", "reason_params": {},
                            "score": None, "ref_id": None,
                        })
                    else:
                        rows.append(_simple_row(existing, self.session_id, digest))
                    continue
                emit({**base_event, "stage": "staging"})
                Path(io_path(staged_dir)).mkdir(parents=True, exist_ok=True)
                staged = staged_dir / f"{digest}{extension}"
                staged_io = Path(io_path(staged))
                if not staged_io.exists():
                    shutil.copyfile(io_path(source), io_path(staged))
                evidence: dict[str, Any] = {}
                metadata: dict[str, Any] = {}
                method = None
                content_family = None
                fmt = None
                matched_ref = None
                identity_method = None
                reason = None
                reason_code = None
                reason_params: dict[str, Any] = {}
                status = "unreadable"
                try:
                    staged, text, fmt, metadata, content_family, method = self._read_staged_document(
                        self.session_id,
                        digest,
                        {"source_extension": extension},
                        check_initial_format=False,
                        progress=lambda stage: emit({**base_event, "stage": stage}),
                    )
                    # PDF citation superscripts may split a terminal identifier
                    # digit from the DOI token. Restore only that adjacent digit
                    # for the local manuscript identifier pass.
                    identity_text = (
                        re.sub(r"(?<=[\w./-])⟦SUP:(\d+)⟧", r"\1", text)
                        if fmt == "pdf" else text
                    )
                    emit({**base_event, "stage": "identifying"})
                    evidence = manuscript_identity.collect_local_evidence(
                        io_path(staged), fmt, identity_text, metadata
                    )
                    (
                        status, reason, score, ref_id, matched_ref, identity_method,
                        reason_code, reason_params,
                    ) = self._identify(text, evidence, fmt)
                    evidence["extraction"] = {
                        "content_family": content_family,
                        "format": fmt,
                        "method": method,
                    }
                    if matched_ref:
                        evidence["matched_reference"] = {
                            key: matched_ref.get(key)
                            for key in ("id", "title", "doi", "pmid", "year", "authors", "ay_surname")
                            if matched_ref.get(key) is not None
                        }
                except _LibraryImportProgressError:
                    raise
                except Exception as exc:
                    text, score, ref_id = "", None, None
                    reason = _exception_reason(exc)
                    reason_code = self._file_reason_code(
                        extension, pdf="unreadable_pdf", document="unreadable_file",
                    )
                    reason_params = {}
                record = {
                    "source_path": str(source),
                    "source_extension": extension,
                    "content_family": content_family,
                    "extraction_format": fmt,
                    "sha256": digest,
                    "status": status,
                    "title": evidence.get("title") or (matched_ref or {}).get("title"),
                    "doi": (matched_ref or {}).get("doi"),
                    "pmid": (matched_ref or {}).get("pmid"),
                    "isbn": (matched_ref or {}).get("isbn"),
                    "arxiv_id": (matched_ref or {}).get("arxiv_id"),
                    "pmcid": (matched_ref or {}).get("pmcid"),
                    "year": (matched_ref or {}).get("year"),
                    "authors": "; ".join((matched_ref or {}).get("authors") or [])
                    if isinstance((matched_ref or {}).get("authors"), (list, tuple))
                    else (matched_ref or {}).get("authors"),
                    "method": identity_method or (evidence.get("title_method") if evidence else None) or method,
                    "reason": reason,
                    "reason_code": reason_code,
                    "reason_params": reason_params,
                    "score": score,
                    "ref_id": ref_id,
                    "initial_evidence": evidence,
                    "extraction_method": method,
                    "extraction_flags": (
                        metadata.get("pdf_structure_flags") if fmt == "pdf" else None
                    ),
                    "revisions": [],
                    "saved": False,
                }
                ids = _first_identifiers(evidence)
                for scheme, values in ids.items():
                    if len(values) == 1:
                        record[scheme] = values[0]
                if fmt == "pdf":
                    self._auto_resolve_title_conflict_from_doi(
                        record,
                        staged=staged,
                        text=text,
                        session_id=self.session_id,
                        digest=digest,
                    )
                payload["files"][digest] = record
                rows.append(_simple_row(record, self.session_id, digest))
            except _LibraryImportProgressError:
                raise
            except Exception as exc:
                rows.append({
                    "file": str(source), "source_extension": extension,
                    "sha256": None, "status": "unreadable",
                    "title": None, "doi": None, "pmid": None, "isbn": None,
                    "arxiv_id": None, "pmcid": None, "year": None,
                    "authors": None, "method": None,
                    "reason": _exception_reason(exc),
                    "reason_code": self._file_reason_code(
                        extension, pdf="unreadable_pdf", document="unreadable_file",
                    ),
                    "reason_params": {}, "score": None,
                    "ref_id": None,
                })
            finally:
                emit({**base_event, "stage": "processed", "completed": index + 1})
        emit({"stage": "finalizing", "completed": total, "total": total})
        _json_write(review_path, payload)
        return rows

    def pending_rows(self) -> list[dict[str, Any]]:
        rows = []
        import_root_io = Path(io_path(self.import_root))
        if not import_root_io.exists():
            return rows
        for review_path_io in sorted(import_root_io.glob("*/review.json")):
            session_id = review_path_io.parent.name
            review_path = self._session_path(session_id)
            try:
                payload = self._load_session(session_id)
            except Exception as exc:
                raise PendingLibraryReviewError(
                    f"cannot read pending library review {review_path}: {exc}",
                    code="pending_review_unreadable",
                ) from exc
            for digest, record in sorted(payload["files"].items()):
                if record.get("saved"):
                    continue
                row = _simple_row(record, session_id, digest)
                try:
                    staged = self._staged_path(session_id, digest, record)
                except LibraryImportError as exc:
                    raise PendingLibraryReviewError(
                        f"pending library review has an invalid staged-file record: {exc}",
                        code="pending_review_unreadable",
                    ) from exc
                if Path(io_path(staged)).is_file():
                    rows.append(row)
        return rows

    @staticmethod
    def _validated_ref(row: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        file_kind = "PDF" if str(record.get("source_extension") or "").casefold() == ".pdf" else "document"
        evidence = record.get("initial_evidence") or {}
        initial_ids = _first_identifiers(evidence)
        values: dict[str, str | None] = {}
        for scheme in ("doi", "pmid", "isbn", "arxiv_id", "pmcid"):
            raw_value = str(row.get(scheme) or "").strip() or None
            choices = initial_ids[scheme]
            canonical_value = library_metadata._canonical(scheme, raw_value)
            canonical_choices = {
                library_metadata._canonical(scheme, value) or value
                for value in choices
            }
            if len(choices) > 1 and (
                not canonical_value or canonical_value not in canonical_choices
            ):
                if scheme == "doi":
                    code = "choose_doi"
                    message = f"choose one DOI observed in the {file_kind} front matter"
                elif scheme == "pmid":
                    code = "choose_pmid"
                    message = f"choose one PMID observed in the {file_kind} front matter"
                else:
                    code = "choose_identifier"
                    message = f"choose one {scheme.upper()} observed in the {file_kind} front matter"
                raise LibraryImportError(message, code=code)
            if len(choices) == 1:
                if raw_value and canonical_value != next(iter(canonical_choices)):
                    if scheme == "doi":
                        code = "doi_conflict"
                        message = f"edited DOI conflicts with the {file_kind} front matter"
                    elif scheme == "pmid":
                        code = "pmid_conflict"
                        message = f"edited PMID conflicts with the {file_kind} front matter"
                    else:
                        code = "identifier_conflict"
                        message = f"edited {scheme.upper()} conflicts with the {file_kind} front matter"
                    raise LibraryImportError(message, code=code)
                raw_value = choices[0]
                canonical_value = library_metadata._canonical(scheme, raw_value)
            if raw_value and not canonical_value:
                code = {
                    "doi": "invalid_doi",
                    "pmid": "invalid_pmid",
                }.get(scheme, "invalid_identity_identifier")
                raise LibraryImportError(
                    f"{scheme.upper()} is invalid", code=code,
                )
            values[scheme] = canonical_value

        doi = values["doi"]
        pmid = values["pmid"]
        isbn = values["isbn"]
        arxiv_id = values["arxiv_id"]
        if arxiv_id and not doi:
            base_arxiv_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.I)
            doi = f"10.48550/arXiv.{base_arxiv_id}"
        title = str(row.get("title") or "").strip()
        if not title or len(title) > 300:
            raise LibraryImportError(
                "title must contain 1 to 300 characters", code="invalid_title"
            )
        authors = str(row.get("authors") or "").strip()
        raw_year = row.get("year")
        year_text = str(raw_year or "").strip()
        if year_text and not year_text.isdigit():
            raise LibraryImportError("year must be a four-digit year", code="invalid_year")
        year = int(year_text) if year_text else None
        if year is not None and not 1000 <= year <= 9999:
            raise LibraryImportError("year must be a four-digit year", code="invalid_year")
        ref: dict[str, Any] = {"title": title}
        if doi:
            ref["doi"] = doi
        if pmid:
            ref["pmid"] = pmid
        if isbn:
            ref["isbn"] = isbn
        if authors:
            ref["authors"] = authors
            surname = _manual_first_author_surname(authors)
            if surname:
                ref["ay_surname"] = surname
        if year is not None:
            ref["year"] = year
        if not content_store.identity_aliases_for_ref(ref):
            reason_code = (
                "missing_author_year"
                if not doi and not pmid and not isbn
                and (not authors or year is None or not ref.get("ay_surname"))
                else "missing_stable_identity"
            )
            raise LibraryImportError(
                "a DOI, PMID, ISBN, or title with author and year is required",
                code=reason_code,
            )
        if not doi and not pmid and not isbn and (
            not authors or year is None or not ref.get("ay_surname")
        ):
            raise LibraryImportError(
                "title-only identity needs both author and year",
                code="missing_author_year",
            )
        return ref

    def review_identity(
        self,
        row: dict[str, Any],
        scheme: str = "",
        identifier: str = "",
        *,
        operator_attested: bool = False,
    ) -> dict[str, Any]:
        """Review identity against staged evidence or record explicit operator attestation."""
        supplied = dict(row)
        session_id = str(supplied.get("_session_id") or "")
        digest = str(supplied.get("_file_id") or "")
        record: dict[str, Any] | None = None
        payload: dict[str, Any] | None = None
        candidate = dict(supplied)
        provenance = None
        identity = None

        def failure_result(exc: Exception) -> dict[str, Any]:
            code = getattr(exc, "reason_code", None) or "identity_metadata_unavailable"
            result = dict(candidate)
            result.update({
                "status": "needs_review",
                "reason": str(exc),
                "reason_code": code,
                "reason_params": dict(getattr(exc, "reason_params", {}) or {}),
            })
            return result

        if type(operator_attested) is not bool:
            return failure_result(LibraryImportError(
                "operator identity confirmation must be explicit",
                code="invalid_operator_attestation",
            ))
        if operator_attested and (
            str(scheme or "").strip() or str(identifier or "").strip()
        ):
            return failure_result(LibraryImportError(
                "metadata lookup and operator attestation must be separate actions",
                code="invalid_identity_decision",
            ))

        try:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise LibraryImportError(
                    "invalid library import file token", code="invalid_review_record"
                )
            payload = self._load_session(session_id)
            record = payload["files"].get(digest)
            if record is None or record.get("saved"):
                raise LibraryImportError(
                    "library import item is unavailable or already saved",
                    code="item_unavailable",
                )
            staged, text, fmt, _metadata, _content_family, _method = self._read_staged_document(
                session_id, digest, record,
            )

            has_lookup = bool(str(scheme or "").strip() or str(identifier or "").strip())
            if has_lookup:
                lookup = library_metadata.lookup_identity(scheme, identifier)
                provenance = lookup["provenance"]
                candidate = dict(supplied)
                for key in _IDENTITY_FIELDS:
                    candidate[key] = None
                candidate.update(lookup["fields"])
                candidate["file"] = supplied.get("file") or record.get("source_path")
                candidate["sha256"] = digest
                candidate["_session_id"] = session_id
                candidate["_file_id"] = digest
            if _sha256(staged) != digest:
                changed_code = self._file_reason_code(
                    str(record.get("source_extension") or ""),
                    pdf="staged_pdf_changed", document="staged_file_changed",
                )
                raise LibraryImportError(
                    "staged document changed during validation", code=changed_code,
                )

            ref = self._validated_ref(candidate, record)
            initial_ids = _first_identifiers(record.get("initial_evidence") or {})
            for scheme in ("doi", "pmid", "isbn"):
                candidate[scheme] = ref.get(scheme)
            for scheme in ("arxiv_id", "pmcid"):
                canonical = library_metadata._canonical(scheme, candidate.get(scheme))
                if canonical is None and len(initial_ids[scheme]) == 1:
                    canonical = library_metadata._canonical(
                        scheme, initial_ids[scheme][0]
                    )
                candidate[scheme] = canonical
            if operator_attested:
                accepted = True
            else:
                identity = sources.document_identity_probe(ref, text)
                accepted = bool(
                    identity.get("decision") == "confirmed"
                    and identity.get("ok") is True
                    and identity.get("author_ok") is not False
                )
            reason_code = None
            reason = None
            if not accepted:
                if identity.get("author_ok") is False:
                    reason_code = "author_mismatch"
                    reason = "front-matter author did not confirm the selected identity"
                else:
                    is_pdf = str(record.get("source_extension") or "").casefold() == ".pdf"
                    reason_code = "identity_pdf_mismatch" if is_pdf else "identity_file_mismatch"
                    reason = str(
                        identity.get("reason")
                        or ("PDF identity was not confirmed" if is_pdf else "document identity was not confirmed")
                    )
            review_source = (
                "operator_attested" if operator_attested
                else "identifier_metadata" if provenance else "operator_review"
            )
            self._record_identity_review(
                payload,
                record,
                candidate,
                provenance=provenance,
                identity=identity,
                source=review_source,
                staged_sha256=digest,
                accepted=accepted,
                reason=reason,
                reason_code=reason_code,
            )
            try:
                _json_write(self._session_path(session_id), payload)
            except Exception as exc:
                return failure_result(LibraryImportError(
                    f"identity review could not be recorded ({type(exc).__name__})",
                    code="identity_review_persist_failed",
                ))
            result = _simple_row(record, session_id, digest)
            result["status"] = "identified" if accepted else "needs_review"
            result["reason"] = reason
            result["reason_code"] = reason_code
            result["reason_params"] = {}
            return result
        except Exception as exc:
            if record is not None and payload is not None and not record.get("saved"):
                provenance = getattr(exc, "provenance", None) or provenance
                review_source = (
                    "operator_attested" if operator_attested
                    else "identifier_metadata" if provenance else "operator_review"
                )
                self._record_identity_review(
                    payload,
                    record,
                    candidate,
                    provenance=provenance,
                    identity=identity,
                    source=review_source,
                    staged_sha256=digest,
                    accepted=False,
                    reason=str(exc),
                    reason_code=getattr(exc, "reason_code", None)
                    or "identity_metadata_unavailable",
                )
                try:
                    _json_write(self._session_path(session_id), payload)
                except Exception as persist_exc:
                    return failure_result(LibraryImportError(
                        f"identity review could not be recorded ({type(persist_exc).__name__})",
                        code="identity_review_persist_failed",
                    ))
            return failure_result(exc)

    @staticmethod
    def _record_identity_review(
        payload: dict[str, Any],
        record: dict[str, Any],
        candidate: dict[str, Any],
        *,
        provenance: dict[str, Any] | None,
        identity: dict[str, Any] | None,
        source: str,
        staged_sha256: str,
        accepted: bool,
        reason: str | None,
        reason_code: str | None,
    ) -> None:
        before = {key: record.get(key) for key in _IDENTITY_FIELDS}
        after = {key: candidate.get(key) for key in _IDENTITY_FIELDS}
        for key, value in after.items():
            record[key] = value if value not in (None, "") else None
        snapshot = _identity_snapshot(candidate)
        if accepted:
            record["accepted_identity"] = {
                "fields": snapshot,
                "source": source,
            }
        else:
            record.pop("accepted_identity", None)
        record["status"] = "identified" if accepted else "needs_review"
        record["reason"] = reason
        record["reason_code"] = reason_code
        record["reason_params"] = {}
        record["identity_review"] = {
            "accepted": bool(accepted),
            "source": source,
            "staged_sha256": staged_sha256,
            "identity_snapshot": snapshot,
            "metadata_lookup": dict(provenance) if provenance else None,
            "document_identity": dict(identity) if identity else None,
        }
        record.setdefault("revisions", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "before": before,
            "after": after,
            "decision": "identity_review_accepted" if accepted else "identity_review_rejected",
            "evidence": {
                "source": source,
                "staged_sha256": staged_sha256,
                "identity_snapshot": snapshot,
                "metadata_lookup": dict(provenance) if provenance else None,
                "document_identity": dict(identity) if identity else None,
                "reason_code": reason_code,
            },
        })

    def save(
        self,
        rows: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        def emit(event: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(event)
            except Exception as exc:
                raise _LibraryImportProgressError("library import progress callback failed") from exc

        total = len(rows)
        emit({
            "stage": "queued", "files": [str(row.get("file") or "") for row in rows],
            "completed": 0, "total": total,
        })
        results: list[dict[str, Any]] = []
        for index, supplied in enumerate(rows):
            source_path = str(supplied.get("file") or "")
            base_event = {
                "index": index, "file": source_path, "completed": index, "total": total,
            }
            try:
                emit({**base_event, "stage": "reading"})
                session_id = str(supplied.get("_session_id") or "")
                digest = str(supplied.get("_file_id") or "")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise LibraryImportError(
                        "invalid library import file token", code="invalid_review_record"
                    )
                payload = self._load_session(session_id)
                record = payload["files"].get(digest)
                if record is None or record.get("saved"):
                    raise LibraryImportError(
                        "library import item is unavailable or already saved",
                        code="item_unavailable",
                    )
                if str(record.get("status") or "").casefold() != "identified":
                    raise LibraryImportError(
                        "review and validate this identity before saving",
                        code="identity_review_required",
                    )
                accepted = record.get("accepted_identity")
                if isinstance(accepted, dict) and isinstance(accepted.get("fields"), dict):
                    expected_identity = accepted["fields"]
                else:
                    # Original identified rows remain eligible only while their
                    # identity matches the durable import record.
                    expected_identity = _identity_snapshot(
                        _simple_row(record, session_id, digest)
                    )
                if _identity_snapshot(supplied) != expected_identity:
                    file_kind = (
                        "PDF" if str(record.get("source_extension") or "").casefold() == ".pdf"
                        else "document"
                    )
                    raise LibraryImportError(
                        f"edited identity must be reviewed against the {file_kind} before saving",
                        code="identity_review_required",
                    )
                staged, text, fmt, metadata, _content_family, method = self._read_staged_document(
                    session_id,
                    digest,
                    record,
                    progress=lambda stage: emit({**base_event, "stage": stage}),
                )
                ref = self._validated_ref(supplied, record)
                emit({**base_event, "stage": "checking"})
                identity_review = record.get("identity_review")
                identity_review = identity_review if isinstance(identity_review, dict) else {}
                accepted_source = (
                    accepted.get("source") if isinstance(accepted, dict) else None
                )
                review_is_bound = bool(
                    isinstance(accepted, dict)
                    and isinstance(accepted.get("fields"), dict)
                    and identity_review.get("accepted") is True
                    and identity_review.get("identity_snapshot") == expected_identity
                    and identity_review.get("staged_sha256") == digest
                )
                operator_attested = accepted_source == "operator_attested"
                if operator_attested and not (
                    review_is_bound
                    and identity_review.get("source") == "operator_attested"
                    and identity_review.get("staged_sha256") == digest
                ):
                    raise LibraryImportError(
                        "operator identity confirmation is not bound to this staged document",
                        code="identity_review_required",
                    )
                reviewed_identity = identity_review.get("document_identity")
                reused_probe = bool(
                    review_is_bound
                    and accepted_source in {
                        "identifier_metadata", "automatic_identifier_metadata", "operator_review",
                    }
                    and isinstance(reviewed_identity, dict)
                    and reviewed_identity.get("decision") == "confirmed"
                    and reviewed_identity.get("ok") is True
                    and reviewed_identity.get("author_ok") is not False
                )
                if operator_attested:
                    identity = None
                elif reused_probe:
                    identity = dict(reviewed_identity)
                else:
                    identity = sources.document_identity_probe(ref, text)
                    if (
                        identity.get("decision") != "confirmed"
                        or identity.get("ok") is not True
                        or identity.get("author_ok") is False
                    ):
                        reason = (
                            "front-matter author did not confirm the selected identity"
                            if identity.get("author_ok") is False
                            else str(identity.get("reason") or "document identity was not confirmed")
                        )
                        identity_code = (
                            "author_mismatch"
                            if identity.get("author_ok") is False
                            else "identity_inconclusive"
                        )
                        identity_reason_code = identity.get("reason_code")
                        raise LibraryImportError(
                            reason,
                            code=identity_code,
                            params=(
                                {"probe_reason_code": str(identity_reason_code)}
                                if identity_reason_code and identity_code == "identity_inconclusive"
                                else {}
                            ),
                        )
                emit({**base_event, "stage": "storing"})
                library_item = content_store.archive_user_original(
                    self.anchor_run,
                    str(staged),
                    ref=ref,
                    supplied_via="user_library_review",
                    file_format=fmt,
                    move=False,
                    environ=self.environ,
                )
                if not library_item:
                    raise LibraryImportError(
                        "document does not have a cataloguable stable identity",
                        code="missing_stable_identity",
                    )
                accepted_identity = record.get("accepted_identity")
                automatic_identity = (
                    isinstance(accepted_identity, dict)
                    and accepted_identity.get("source") == "automatic_identifier_metadata"
                )
                operator_attestation = (
                    isinstance(accepted_identity, dict)
                    and accepted_identity.get("source") == "operator_attested"
                )
                if fmt == "pdf":
                    identity_note = (
                        "PDF identity passed automatic exact-DOI and returned-title front-matter checks; "
                        "saved after explicit Library selection"
                        if automatic_identity else
                        "PDF identity explicitly attested by the operator after local extraction; "
                        "saved after explicit Library selection"
                        if operator_attestation else
                        "PDF identity confirmed after local extraction and operator review"
                    )
                else:
                    identity_note = (
                        "Document identity explicitly attested by the operator after local extraction"
                        if operator_attestation else
                        "Document identity confirmed after local extraction and operator review"
                    )
                identity_note += (
                    f"; staged_sha256={digest}; original_path={record.get('source_path')}"
                )
                parsed = content_store.record_parsed_text(
                    self.anchor_run,
                    ref,
                    "fulltext",
                    "user",
                    text,
                    source_ref=record.get("source_path"),
                    mapping="user_library_review",
                    signal="operator_review",
                    score=1.0,
                    identity_status="user_library_reviewed",
                    identity_note=identity_note,
                    supplied_by="user",
                    supplied_via="user_library_review",
                    file_format=fmt,
                    library_item_id=library_item["library_item_id"],
                    extraction_method=method,
                    extraction_flags=(
                        metadata.get("pdf_structure_flags") if fmt == "pdf" else None
                    ),
                    environ=self.environ,
                )
                if parsed is None:
                    raise LibraryImportError(
                        "parsed text was not recorded", code="save_failed"
                    )
                before = {key: record.get(key) for key in _IDENTITY_FIELDS}
                after = {key: supplied.get(key) for key in _IDENTITY_FIELDS}
                review = record.get("identity_review")
                evidence = {
                    "content_family": record.get("content_family"),
                    "extraction_format": record.get("extraction_format"),
                    "extraction_method": record.get("extraction_method"),
                    "document_identity": identity,
                    "metadata_lookup": (
                        review.get("metadata_lookup") if isinstance(review, dict) else None
                    ),
                    "returned_title_identity": (
                        review.get("returned_title_identity")
                        if isinstance(review, dict) else None
                    ),
                    "identity_source": (
                        accepted_identity.get("source")
                        if isinstance(accepted_identity, dict) else None
                    ),
                }
                record["revisions"].append({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "before": before,
                    "after": after,
                    "decision": "saved",
                    "evidence": evidence,
                })
                record.update(after)
                record["saved"] = True
                record["status"] = "identified"
                record["reason"] = None
                record["reason_code"] = None
                record["reason_params"] = {}
                _json_write(self._session_path(session_id), payload)
                results.append({
                    **_simple_row(record, session_id, digest),
                    "status": "stored",
                    "library_item_id": library_item["library_item_id"],
                    "work_id": library_item["work_id"],
                    "parsed_text_id": (parsed or {}).get("parsed_text_id"),
                    "stored_path": library_item["stored_path"],
                })
            except _LibraryImportProgressError:
                raise
            except Exception as exc:
                reason_code = getattr(exc, "reason_code", None) or "save_failed"
                raw_params = getattr(exc, "reason_params", {})
                reason_params = dict(raw_params) if isinstance(raw_params, dict) else {}
                results.append({
                    **supplied,
                    "file": source_path, "sha256": supplied.get("sha256"),
                    "status": "needs_review", "reason": _exception_reason(exc),
                    "reason_code": reason_code, "reason_params": reason_params,
                    "title": supplied.get("title"), "doi": supplied.get("doi"),
                    "pmid": supplied.get("pmid"), "isbn": supplied.get("isbn"),
                    "arxiv_id": supplied.get("arxiv_id"), "pmcid": supplied.get("pmcid"),
                    "year": supplied.get("year"),
                    "authors": supplied.get("authors"), "method": supplied.get("method"),
                    "score": supplied.get("score"), "ref_id": supplied.get("ref_id"),
                })
            finally:
                emit({**base_event, "stage": "processed", "completed": index + 1})
        emit({"stage": "finalizing", "completed": total, "total": total})
        return results
