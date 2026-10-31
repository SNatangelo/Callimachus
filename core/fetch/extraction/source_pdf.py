# core/fetch/extraction/source_pdf.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run-scoped reuse for deterministic PDF source text extraction."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import unicodedata
import uuid
from typing import Any

from core.parse import extract as parse_extract
from core.shared.filesystem_paths import io_path, logical_path


_CACHE_SCHEMA = 1
_LOG = logging.getLogger(__name__)
_EXTRA_DEPENDENCY_FILES = (
    "fetch/extraction/pdf.py",
    "fetch/extraction/source_pdf.py",
)


class PdfSourceChangedError(RuntimeError):
    """Raised when source bytes change while they are being extracted or read."""


class _CacheUnavailable(Exception):
    pass


def extract_text(path, run_dir=None, **kwargs):
    """Extract a PDF once per exact source/configuration within one run.

    Non-PDF calls and calls without a run directory preserve the parser's direct
    behaviour. Only successful parser output is cached; identity, admission,
    OCR, and provenance decisions remain live. Extraction failures are retried.
    """
    candidate = Path(path)
    if (
        candidate.suffix.lower() != ".pdf"
        or not run_dir
        or not _cacheable_call(kwargs)
    ):
        return parse_extract.extract_text(path, **kwargs)

    try:
        before_digest, before_identity = _stable_file_digest(candidate)
    except _CacheUnavailable:
        _warn("PDF extraction cache is unavailable; extracting normally.")
        return parse_extract.extract_text(path, **kwargs)

    try:
        fingerprint = _configuration_fingerprint(kwargs)
        cache_path = _cache_path(run_dir, before_digest, fingerprint)
    except Exception as exc:
        _warn(f"PDF extraction cache is unavailable; extracting normally ({type(exc).__name__}: {exc}).")
        return _extract_and_check(path, candidate, before_digest, before_identity, kwargs)

    if cache_path is not None:
        cached = _load_cache(cache_path, before_digest, fingerprint)
        if cached is not None:
            _verify_source_unchanged(candidate, before_digest, before_identity)
            return cached

    return _extract_and_cache(
        path, candidate, before_digest, before_identity, kwargs,
        cache_path=cache_path,
        fingerprint=fingerprint,
    )


def _cacheable_call(kwargs: dict[str, Any]) -> bool:
    if set(kwargs) - {"manuscript_ocr", "ocr_lang", "ocr_notice"}:
        return False
    if kwargs.get("manuscript_ocr", False) is not False:
        return False
    return kwargs.get("ocr_notice") is None


def _extract_and_check(path, candidate: Path, digest: str, identity: tuple, kwargs: dict):
    try:
        result = parse_extract.extract_text(path, **kwargs)
    except Exception:
        _verify_source_unchanged(candidate, digest, identity)
        raise
    _verify_source_unchanged(candidate, digest, identity)
    return result


def _extract_and_cache(
    path,
    candidate: Path,
    digest: str,
    identity: tuple,
    kwargs: dict,
    *,
    cache_path: Path | None,
    fingerprint: str,
):
    try:
        result = parse_extract.extract_text(path, **kwargs)
    except Exception:
        _verify_source_unchanged(candidate, digest, identity)
        raise

    _verify_source_unchanged(candidate, digest, identity)
    if cache_path is not None:
        _write_cache(cache_path, digest, fingerprint, result)
    return result


def _stable_file_digest(path: Path) -> tuple[str, tuple]:
    io_candidate = Path(io_path(path))
    if io_candidate.is_symlink():
        raise _CacheUnavailable("PDF source is a symlink")
    try:
        before = io_candidate.stat()
        if not stat.S_ISREG(before.st_mode):
            raise _CacheUnavailable("PDF source is not a regular file")
        digest = hashlib.sha256()
        with io_candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = io_candidate.stat()
    except OSError as exc:
        raise _CacheUnavailable(str(exc)) from exc
    before_identity = _stat_identity(before)
    if before_identity != _stat_identity(after):
        raise PdfSourceChangedError("PDF source changed while its bytes were being hashed")
    return digest.hexdigest(), before_identity


def _stat_identity(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _verify_source_unchanged(path: Path, expected_digest: str, expected_identity: tuple) -> None:
    digest, identity = _stable_file_digest(path)
    if digest != expected_digest or identity != expected_identity:
        raise PdfSourceChangedError("PDF source changed during extraction or cache lookup")


def _configuration_fingerprint(kwargs: dict[str, Any]) -> str:
    core_root = Path(parse_extract.__file__).resolve().parent.parent
    config = __import__("core.parse.parser_config", fromlist=["load"]).load()
    dependency_hashes = {}
    for relative in _dependency_files(core_root):
        source = core_root / relative
        if not source.is_file() or source.is_symlink():
            raise _CacheUnavailable(f"PDF extraction dependency is unavailable: {relative}")
        dependency_hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()

    options = {
        "manuscript_ocr": kwargs.get("manuscript_ocr", False),
        "ocr_lang": kwargs.get("ocr_lang", "eng"),
    }
    identity = {
        "schema": _CACHE_SCHEMA,
        "dependencies": dependency_hashes,
        "parser_config": config,
        "options": options,
        "runtime": {
            "python": list(sys.version_info[:3]),
            "implementation": platform.python_implementation(),
            "platform": platform.system(),
            "unicode": unicodedata.unidata_version,
            "pymupdf": _distribution_version("PyMuPDF"),
            "pdfminer": _distribution_version("pdfminer.six"),
            "pdftotext": _executable_identity("pdftotext"),
            "pdf_links": os.environ.get("CITATION_VERIFIER_PDF_LINKS"),
            "pdf_superscripts": os.environ.get("CITATION_VERIFIER_PDF_SUPERSCRIPTS"),
        },
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _dependency_files(core_root: Path) -> tuple[str, ...]:
    # PDF parsing reaches shared text, footnote, and reference-reader helpers.
    # Hashing the small parse source/config tree avoids an incomplete manual list.
    parse_root = core_root / "parse"
    parse_dependencies = {
        path.relative_to(core_root).as_posix()
        for path in parse_root.rglob("*")
        if path.suffix.lower() in {".py", ".json"}
    }
    return tuple(sorted(parse_dependencies.union(_EXTRA_DEPENDENCY_FILES)))


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _executable_identity(name: str) -> dict | None:
    executable = shutil.which(name)
    if not executable:
        return None
    resolved = os.path.realpath(executable)
    try:
        details = os.stat(resolved)
    except OSError:
        return {"path": resolved, "available": False}
    return {
        "path": resolved,
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
    }


def _cache_path(run_dir, source_digest: str, fingerprint: str) -> Path:
    from core.fetch.storage import content_store

    run_root = logical_path(run_dir, strict=True)
    run_root_io = Path(io_path(run_root))
    if not run_root_io.is_dir():
        raise _CacheUnavailable("run directory is unavailable")
    resolved_run_root = run_root_io.resolve(strict=True)
    run_key = hashlib.sha256(os.fsencode(str(run_root))).hexdigest()
    storage_root = Path(io_path(content_store.storage_root(str(run_root))))
    if storage_root.is_symlink():
        raise _CacheUnavailable("content store root is a symlink")
    resolved_storage_root = storage_root.resolve(strict=False)
    cache_root = resolved_storage_root / "external_cache" / "pdf-extractions" / run_key
    if _is_within(cache_root.resolve(strict=False), resolved_run_root):
        raise _CacheUnavailable("PDF extraction cache would be inside the run directory")
    for directory in (storage_root / "external_cache", storage_root / "external_cache" / "pdf-extractions", cache_root):
        if directory.is_symlink():
            raise _CacheUnavailable("PDF extraction cache directory is a symlink")
        if directory.exists() and not directory.is_dir():
            raise _CacheUnavailable("PDF extraction cache path is not a directory")
    cache_root.mkdir(parents=True, exist_ok=True)
    if _is_within(cache_root.resolve(strict=True), resolved_run_root):
        raise _CacheUnavailable("PDF extraction cache resolved inside the run directory")
    return cache_root / f"{source_digest}-{fingerprint}.json"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_cache(path: Path, source_digest: str, fingerprint: str) -> tuple[str, str, dict] | None:
    if path.is_symlink():
        _warn("PDF extraction cache entry is a symlink; extracting again.")
        return None
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("cache entry is not a regular file")
        envelope = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema", "source_sha256", "fingerprint", "payload_sha256", "payload"}
            or envelope.get("schema") != _CACHE_SCHEMA
            or envelope.get("source_sha256") != source_digest
            or envelope.get("fingerprint") != fingerprint
            or not isinstance(envelope.get("payload"), dict)
            or hashlib.sha256(_canonical_json(envelope["payload"])).hexdigest()
            != envelope.get("payload_sha256")
        ):
            raise ValueError("cache entry failed its identity or payload hash")
        result = _decode_result(envelope["payload"])
        return result
    except Exception as exc:
        _warn(f"PDF extraction cache entry is invalid; extracting again ({type(exc).__name__}: {exc}).")
        return None


def _write_cache(path: Path, source_digest: str, fingerprint: str, result: Any) -> None:
    try:
        encoded = _encode_result(result)
        envelope = {
            "schema": _CACHE_SCHEMA,
            "source_sha256": source_digest,
            "fingerprint": fingerprint,
            "payload_sha256": hashlib.sha256(_canonical_json(encoded)).hexdigest(),
            "payload": encoded,
        }
        data = _canonical_json(envelope) + b"\n"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with Path(io_path(temporary)).open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(Path(io_path(temporary)), Path(io_path(path)))
        finally:
            try:
                Path(io_path(temporary)).unlink()
            except FileNotFoundError:
                pass
    except Exception as exc:
        _warn(f"PDF extraction cache is unavailable; extraction result was not saved ({type(exc).__name__}).")


def _encode_result(result: Any) -> dict:
    if not isinstance(result, tuple) or len(result) != 3:
        raise TypeError("PDF parser result has an unsupported shape")
    if not isinstance(result[0], str) or not isinstance(result[1], str) or not isinstance(result[2], dict):
        raise TypeError("PDF parser result has unsupported value types")
    return _pack(result)


def _decode_result(value: Any) -> tuple[str, str, dict]:
    result = _unpack(value)
    if (
        not isinstance(result, tuple)
        or len(result) != 3
        or not isinstance(result[0], str)
        or not isinstance(result[1], str)
        or not isinstance(result[2], dict)
    ):
        raise ValueError("cached PDF parser result has an unsupported shape")
    return result


def _pack(value: Any) -> Any:
    """Encode only the primitive and PDF metadata types used by this parser."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not (value == value and abs(value) != float("inf")):
            raise TypeError("non-finite floats are not cacheable")
        return value
    link_types = _link_types()
    if isinstance(value, link_types[0]):
        return {
            "$type": "LinkLayer",
            "references": _pack(value.references),
            "citations": _pack(value.citations),
        }
    if isinstance(value, link_types[1]):
        return {
            "$type": "Citation", "page": _pack(value.page),
            "rect": _pack(value.rect), "text": _pack(value.text),
            "target": _pack(value.target), "years": _pack(value.years),
        }
    if isinstance(value, link_types[2]):
        return {
            "$type": "Reference", "name": _pack(value.name),
            "page": _pack(value.page), "y": _pack(value.y),
            "text": _pack(value.text), "key": _pack(value.key),
        }
    if isinstance(value, dict):
        return {"$type": "dict", "items": [[_pack(key), _pack(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return {"$type": "list", "items": [_pack(item) for item in value]}
    if isinstance(value, tuple):
        return {"$type": "tuple", "items": [_pack(item) for item in value]}
    if isinstance(value, set):
        items = [_pack(item) for item in value]
        items.sort(key=_canonical_json)
        return {"$type": "set", "items": items}
    if isinstance(value, frozenset):
        items = [_pack(item) for item in value]
        items.sort(key=_canonical_json)
        return {"$type": "frozenset", "items": items}
    raise TypeError(f"unsupported PDF metadata type: {type(value).__name__}")


def _unpack(value: Any) -> Any:
    if not isinstance(value, dict) or "$type" not in value:
        if isinstance(value, list):
            return [_unpack(item) for item in value]
        return value
    kind = value.get("$type")
    if kind in {"dict", "list", "tuple", "set", "frozenset"}:
        if set(value) != {"$type", "items"} or not isinstance(value["items"], list):
            raise ValueError("cached PDF metadata container is invalid")
        items = value["items"]
        if kind == "dict":
            output = {}
            for pair in items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("cached PDF metadata mapping is invalid")
                key, item = _unpack(pair[0]), _unpack(pair[1])
                if key in output:
                    raise ValueError("cached PDF metadata mapping contains duplicate keys")
                output[key] = item
            return output
        unpacked = [_unpack(item) for item in items]
        if kind == "list":
            return unpacked
        if kind == "tuple":
            return tuple(unpacked)
        if kind == "set":
            return set(unpacked)
        return frozenset(unpacked)
    link_layer, citation, reference = _link_types()
    if kind == "LinkLayer":
        if set(value) != {"$type", "references", "citations"}:
            raise ValueError("cached PDF link layer is invalid")
        refs = _unpack(value["references"])
        citations = _unpack(value["citations"])
        if not isinstance(refs, dict) or not isinstance(citations, list):
            raise ValueError("cached PDF link layer fields are invalid")
        return link_layer(references=refs, citations=citations)
    if kind == "Citation":
        if set(value) != {"$type", "page", "rect", "text", "target", "years"}:
            raise ValueError("cached PDF citation is invalid")
        return citation(
            page=_unpack(value["page"]), rect=_unpack(value["rect"]),
            text=_unpack(value["text"]), target=_unpack(value["target"]),
            years=_unpack(value["years"]),
        )
    if kind == "Reference":
        if set(value) != {"$type", "name", "page", "y", "text", "key"}:
            raise ValueError("cached PDF reference is invalid")
        return reference(
            name=_unpack(value["name"]), page=_unpack(value["page"]),
            y=_unpack(value["y"]), text=_unpack(value["text"]),
            key=_unpack(value["key"]),
        )
    raise ValueError("cached PDF metadata contains an unknown type")


def _link_types():
    from core.parse.pdf_links import Citation, LinkLayer, Reference

    return LinkLayer, Citation, Reference


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _warn(message: str) -> None:
    _LOG.warning(message)
