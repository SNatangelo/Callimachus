# core/infra/db/task_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed relational persistence for interactive task contracts.

The public task API exposes dictionaries as transient projections; the durable
authority is the current typed task and answer relations.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlparse

from core.verify.claim_evidence.evidence.context import (
    ContextError,
    build_effective_context,
    context_from_snapshot,
)


JsonDict = dict[str, Any]

_TASK_TABLES = (
    "task_fetch_details",
    "task_browser_challenge_details",
    "task_web_research_details",
    "task_claim_evidence_details",
    "task_manual_parse_review_details",
    "task_source_identity_attestation_details",
)
_ANSWER_TABLES = (
    "task_fetch_answers",
    "task_browser_answer_states",
    "task_research_answer_states",
    "task_manual_parse_review_answers",
    "task_source_identity_attestation_answers",
)
_SHA256 = frozenset("0123456789abcdef")
_FETCH_REFERENCE_FIELDS = (
    "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn",
    "year", "source_type", "source_kind", "indexability",
    "source_type_confidence",
)
_RESEARCH_REFERENCE_FIELDS = (
    "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn",
    "year", "source_type",
)
_LAST_ERROR_FIELDS = {"stage", "type", "message", "traceback"}


def _source_identity_attestation_snapshot(
    conn: sqlite3.Connection, ref_id: str, source_text_id: str,
) -> JsonDict:
    """Closed, non-secret attestation target reconstructed from authoritative rows."""
    reference = _operational_reference(conn, ref_id, _FETCH_REFERENCE_FIELDS)
    source = conn.execute(
        """
        SELECT source_text_id,ref_id,sha256,tier,identity_key,origin,mapping,
               match_signal,match_score,identity_status,identity_note,
               content_version,provenance_relation,supplied_by,supplied_via
        FROM source_texts WHERE source_text_id=?
        """,
        (source_text_id,),
    ).fetchone()
    if source is None or source["ref_id"] != ref_id:
        raise ValueError(
            "attestation source is unavailable or belongs to another reference"
        )
    resolve = conn.execute(
        """
        SELECT status,matched_title,reference_status_tag,fabrication_risk,
               retracted,resolution_basis,reason,tag_reason,
               resolved_identifier_type,resolved_identifier_value,
               resolved_identifier_validated_via
        FROM resolve_results WHERE ref_id=?
        """,
        (ref_id,),
    ).fetchone()
    if resolve is None:
        raise ValueError("attestation Resolve identity is unavailable")
    profile = _resolve_evidence_profile(conn, ref_id)
    context_conflict = conn.execute(
        """
        SELECT 1 FROM resolve_fulltext_links
        WHERE ref_id=? AND identity_context_conflict=1
        """,
        (ref_id,),
    ).fetchone() is not None
    identity = {
        **dict(resolve),
        # Resolve stores these values as relational scalar fields.  Identity
        # admission consumes the canonical identifier object, so reconstruct it
        # at this persistence boundary instead of making a task consumer depend
        # on one representation or the other.
        "resolved_identifier": {
            "type": resolve["resolved_identifier_type"],
            "value": resolve["resolved_identifier_value"],
            "validated_via": resolve["resolved_identifier_validated_via"],
        },
        "metadata_match": (
            None if profile is None else profile.get("metadata_match")
        ),
        "evidence_profile": profile,
        "identity_context_conflict": context_conflict,
    }
    source_identity = {
        key: source[key]
        for key in (
            "identity_key", "origin", "mapping", "match_signal",
            "match_score", "identity_status", "identity_note",
            "content_version", "provenance_relation", "supplied_by",
            "supplied_via",
        )
    }
    return {
        "ref_id": ref_id,
        "ref_number": reference["ref_number"],
        "reference": reference,
        "source_text_id": source["source_text_id"],
        "source_text_sha256": source["sha256"],
        "source_tier": source["tier"],
        "source_identity": source_identity,
        "resolve_identity": identity,
    }


def _source_identity_attestation_sha(snapshot: JsonDict) -> str:
    encoded = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    return text.replace("\x00", "\uFFFD")


def _sanitize(value: Any) -> Any:
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {(_clean_text(key) or ""): _sanitize(item) for key, item in value.items()}
    return _clean_text(value)


def _mapping(value: Any, label: str) -> JsonDict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact(value: Any, fields: set[str], label: str) -> JsonDict:
    out = _mapping(value, label)
    if set(out) != fields:
        raise ValueError(f"{label} has an unsupported shape")
    return out


def _nonempty_text(value: Any, label: str, *, exact: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    if exact:
        return value
    return _clean_text(value) or ""


def _nullable_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")
    return _clean_text(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return _clean_text(value) or ""


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in _SHA256 for char in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _row_value(row: Mapping[str, Any], name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _operational_reference(
    conn: sqlite3.Connection, ref_id: str, fields: tuple[str, ...],
) -> JsonDict:
    """Return the immutable raw or manual-split snapshot for an operational ID."""
    row = conn.execute(
        """
        SELECT o.provenance_kind,o.ref_number,
               r.raw_entry,r.title,r.doi,r.pmid,r.url,r.isbn,r.year,
               r.source_type,r.source_kind,r.indexability,r.source_type_confidence,
               s.source_text
        FROM operational_references o
        LEFT JOIN reference_entries r ON r.ref_id=o.ref_id
        LEFT JOIN manual_footnote_source_override_sources s ON s.source_ref_id=o.ref_id
        WHERE o.ref_id=?
        """,
        (ref_id,),
    ).fetchone()
    if row is None:
        raise ValueError("task reference is unavailable")
    if row["provenance_kind"] == "raw_parse":
        if row["raw_entry"] is None or row["source_text"] is not None:
            raise RuntimeError("raw operational reference snapshot is inconsistent")
        snapshot = {field: row[field] for field in _FETCH_REFERENCE_FIELDS}
    elif row["provenance_kind"] == "manual_footnote_split":
        if row["raw_entry"] is not None or row["source_text"] is None:
            raise RuntimeError("manual split operational reference snapshot is inconsistent")
        snapshot = {
            "ref_number": row["ref_number"], "raw_entry": row["source_text"],
            "title": None, "doi": None, "pmid": None, "url": None,
            "isbn": None, "year": None, "source_type": "unknown",
            "source_kind": "footnote_source", "indexability": "unknown",
            "source_type_confidence": "manual_split",
        }
    else:
        raise RuntimeError("operational reference has an unsupported provenance kind")
    return {field: snapshot[field] for field in fields}


def _metadata_match(conn: sqlite3.Connection, ref_id: str) -> Any:
    # The evidence-profile decoder is already the strict authority for these
    # relations.  Import lazily to avoid a repository-module import cycle.
    from .repository import _read_resolve_evidence_profile

    profile = _read_resolve_evidence_profile(conn, ref_id)
    return None if profile is None else profile.get("metadata_match")


def _resolve_evidence_profile(conn: sqlite3.Connection, ref_id: str) -> Any:
    from .repository import _read_resolve_evidence_profile
    return _read_resolve_evidence_profile(conn, ref_id)


def _source_identity(conn: sqlite3.Connection, ref_id: str) -> JsonDict:
    row = conn.execute(
        """
        SELECT reference_status_tag,fabrication_risk,matched_title,tag_reason
        FROM resolve_results WHERE ref_id = ?
        """,
        (ref_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Fetch task Resolve identity is unavailable")
    return {
        "reference_status_tag": row["reference_status_tag"],
        "fabrication_risk": row["fabrication_risk"],
        "tag_reason": row["tag_reason"],
        "matched_title": row["matched_title"],
        "metadata_match": _metadata_match(conn, ref_id),
    }


def _validate_parent(row: Mapping[str, Any], kind: str) -> None:
    expected_slot = {
        "fetch": "fetch",
        "browser_challenge": "fetch",
        "web_research": "research",
        "claim_evidence": "verify",
        "manual_parse_review": "parse_review",
        "source_identity_attestation": {"fetch", "verify"},
    }[kind]
    if (
        row["slot"] not in expected_slot
        if isinstance(expected_slot, set)
        else row["slot"] != expected_slot
    ):
        raise ValueError(f"{kind} task is stored in the wrong slot")
    if kind == "source_identity_attestation":
        if (
            not isinstance(row["ref_id"], str)
            or not row["ref_id"]
            or any(
                _row_value(row, key) is not None
                for key in ("note_id", "claim_id", "scope")
            )
        ):
            raise ValueError("source identity attestation parent is invalid")
    elif kind == "manual_parse_review":
        has_note = isinstance(_row_value(row, "note_id"), str) and bool(_row_value(row, "note_id"))
        has_ref = isinstance(row["ref_id"], str) and bool(row["ref_id"])
        if has_note and has_ref:
            raise ValueError("manual Parse review has conflicting target identity")
    elif kind == "browser_challenge":
        if any(_row_value(row, key) is not None for key in ("ref_id", "claim_id", "scope")):
            raise ValueError("browser task has unexpected parent identity")
    elif kind in {"fetch", "web_research"}:
        if not isinstance(row["ref_id"], str) or not row["ref_id"]:
            raise ValueError(f"{kind} task has no reference identity")
        if _row_value(row, "claim_id") is not None or _row_value(row, "scope") is not None:
            raise ValueError(f"{kind} task has unexpected claim identity")
    else:
        if any(not isinstance(row[key], str) or not row[key] for key in ("ref_id", "claim_id", "scope")):
            raise ValueError("ClaimEvidence task identity is incomplete")


def _last_error(payload: JsonDict) -> JsonDict | None:
    value = payload.get("last_error")
    if value is None:
        return None
    error = _exact(value, _LAST_ERROR_FIELDS, "task last_error")
    return {field: _text(error[field], f"last_error.{field}") for field in sorted(_LAST_ERROR_FIELDS)}


def _validate_status(row: Mapping[str, Any], payload: JsonDict, error: JsonDict | None) -> None:
    status = payload.get("status")
    if row["status"] == "applied":
        if status not in {"pending", "done"}:
            raise ValueError("applied task payload has an invalid status")
        if error is not None:
            raise ValueError("applied task retains a last_error")
    elif status != "pending":
        raise ValueError("open task payload status must be pending")
    if error is not None and row["status"] != "pending":
        raise ValueError("last_error is only valid on a pending task")
    if payload.get("answer") is not None:
        raise ValueError("authoritative task payload must not embed an answer")


def _source_reference_is_authorized(
    conn: sqlite3.Connection,
    task_ref_id: str,
    source_ref_id: str,
) -> bool:
    if source_ref_id == task_ref_id:
        return True
    source_reference = conn.execute(
        "SELECT ref_number FROM reference_entries WHERE ref_id = ?", (source_ref_id,),
    ).fetchone()
    resolution = conn.execute(
        "SELECT resolution_basis, via FROM resolve_results WHERE ref_id = ?", (task_ref_id,),
    ).fetchone()
    return (
        source_reference is not None
        and resolution is not None
        and resolution["resolution_basis"] == "cross_reference"
        and resolution["via"] == f"cross_reference:note_{source_reference['ref_number']}"
    )


def _source_asset(
    conn: sqlite3.Connection,
    run_dir: str,
    row: Mapping[str, Any],
    payload: JsonDict,
) -> tuple[Mapping[str, Any], str]:
    source_id = _nonempty_text(payload.get("source_text_id"), "source_text_id", exact=True)
    expected_sha = _sha256(payload.get("source_text_sha256"), "source_text_sha256")
    source = conn.execute(
        "SELECT * FROM source_texts WHERE source_text_id = ?", (source_id,),
    ).fetchone()
    if source is None:
        raise ValueError("ClaimEvidence source identity is unavailable")
    if not _source_reference_is_authorized(conn, row["ref_id"], source["ref_id"]):
        raise ValueError("ClaimEvidence source belongs to another reference")
    expected_sha = _sha256(expected_sha, "source_text_sha256")
    if source["sha256"] != expected_sha:
        raise ValueError("ClaimEvidence source ledger hash mismatch")

    stored_path = source["stored_path"]
    if (
        not isinstance(stored_path, str)
        or not stored_path
        or "\\" in stored_path
        or PurePosixPath(stored_path).is_absolute()
        or PureWindowsPath(stored_path).is_absolute()
        or ".." in PurePosixPath(stored_path).parts
        or PurePosixPath(stored_path).parts[:1] != ("sources",)
    ):
        raise ValueError("ClaimEvidence source path is not run-local")
    root = Path(run_dir).resolve()
    try:
        path = (root / Path(*PurePosixPath(stored_path).parts)).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("ClaimEvidence source asset is unavailable") from exc
    if not path.is_file():
        raise ValueError("ClaimEvidence source asset is not a regular file")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("ClaimEvidence source asset is not strict UTF-8") from exc
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("ClaimEvidence source asset hash mismatch")
    if len(text) != int(source["char_count"]):
        raise ValueError("ClaimEvidence source asset length does not match its ledger")
    return source, text


def normalize_task_payload(
    conn: sqlite3.Connection,
    run_dir: str,
    row: Mapping[str, Any],
    payload: JsonDict,
) -> JsonDict:
    """Validate one task projection and return its closed relational facts."""
    raw = _mapping(payload, "task payload")
    if raw.get("kind") != "claim_evidence":
        raw = _sanitize(raw)
    kind = raw.get("kind")
    if kind not in {"fetch", "browser_challenge", "web_research", "claim_evidence", "manual_parse_review", "source_identity_attestation"}:
        raise ValueError("task kind is unsupported by the current typed schema")
    _validate_parent(row, kind)
    error = _last_error(raw)
    _validate_status(row, raw, error)

    common = {"kind", "status", "answer"} | ({"last_error"} if "last_error" in raw else set())
    out: JsonDict = {"kind": kind, "last_error": error}
    if kind == "source_identity_attestation":
        expected = common | {
            "ref_id", "ref_number", "reference", "source_text_id",
            "source_text_sha256", "source_tier", "source_identity",
            "resolve_identity", "target_sha256", "instructions",
        }
        if set(raw) != expected:
            raise ValueError("source identity attestation has an unsupported shape")
        snapshot = _source_identity_attestation_snapshot(
            conn, row["ref_id"], raw["source_text_id"],
        )
        if (
            any(raw.get(key) != value for key, value in snapshot.items())
            or raw["target_sha256"] != _source_identity_attestation_sha(snapshot)
        ):
            raise ValueError("source identity attestation snapshot is stale")
        out.update(
            snapshot,
            target_sha256=raw["target_sha256"],
            instructions=_nonempty_text(
                raw["instructions"], "attestation instructions",
            ),
        )
        return out
    if kind == "manual_parse_review":
        expected = common | {"review_kind", "target_sha256", "instructions"}
        review_kind = raw.get("review_kind")
        attribution = review_kind in {"citation_reference_review", "reference_claim_review"}
        if attribution:
            expected |= {"candidates"}
        if set(raw) != expected or review_kind not in {"footnote_source_review", "reference_identity_review", "citation_reference_review", "reference_claim_review"}:
            raise ValueError("manual Parse review has an unsupported shape")
        if review_kind == "footnote_source_review":
            if row["ref_id"] is not None or row["claim_id"] is not None or _row_value(row, "scope") is not None:
                raise ValueError("footnote review parent is invalid")
            if not isinstance(_row_value(row, "note_id"), str) or row["ref_id"] is not None:
                raise ValueError("footnote review target does not match task row")
            target = conn.execute("SELECT raw_note FROM footnote_notes WHERE note_id=?", (row["note_id"],)).fetchone()
        elif review_kind == "reference_identity_review":
            if _row_value(row, "note_id") is not None or row["claim_id"] is not None or _row_value(row, "scope") is not None:
                raise ValueError("identity review parent is invalid")
            if not isinstance(row["ref_id"], str) or _row_value(row, "note_id") is not None:
                raise ValueError("identity review target does not match task row")
            target = conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (row["ref_id"],)).fetchone()
        elif review_kind == "citation_reference_review":
            if row["ref_id"] is not None or _row_value(row, "note_id") is not None or not row["claim_id"] or not _row_value(row, "scope"):
                raise ValueError("citation review parent is invalid")
            occurrence_id = _row_value(row, "scope")
            target = conn.execute("SELECT raw_citation_json FROM unresolved_citations WHERE occurrence_id=?", (occurrence_id,)).fetchone()
        else:
            if not row["ref_id"] or _row_value(row, "note_id") is not None or row["claim_id"] is not None or _row_value(row, "scope") is not None:
                raise ValueError("inverse citation review parent is invalid")
            target = conn.execute("SELECT raw_entry FROM reference_entries WHERE ref_id=?", (row["ref_id"],)).fetchone()
        material = target[0] if target is not None else ""
        normalized_candidates = []
        if attribution:
            candidates = raw.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("citation attribution candidates are required")
            allowed_origins = (
                {"parser", "orphan_match"}
                if review_kind == "citation_reference_review"
                else {"orphan_match_inverse"}
            )
            for item in candidates:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"id", "origin", "score"}
                    or not isinstance(item["id"], str)
                    or not isinstance(item["origin"], str)
                    or type(item["score"]) not in {int, float}
                ):
                    raise ValueError("citation attribution candidate is invalid")
                if item["origin"] not in allowed_origins or not (0.0 <= float(item["score"]) <= 1.0):
                    raise ValueError("citation attribution candidate provenance is invalid")
                table = "reference_entries" if review_kind == "citation_reference_review" else "claims"
                column = "ref_id" if table == "reference_entries" else "claim_id"
                if conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (item["id"],)).fetchone() is None:
                    raise ValueError("citation attribution candidate is dangling")
                normalized_candidates.append(
                    {"id": item["id"], "origin": item["origin"], "score": float(item["score"])}
                )
            if len({x["id"] for x in normalized_candidates}) != len(normalized_candidates):
                raise ValueError("citation attribution candidate is duplicated")
            canonical = normalized_candidates
            if review_kind == "citation_reference_review":
                occurrence = conn.execute("SELECT claim_id FROM unresolved_citations WHERE occurrence_id=?", (_row_value(row, "scope"),)).fetchone()
                if occurrence is None or occurrence["claim_id"] != row["claim_id"]:
                    raise ValueError("citation review claim binding is invalid")
                claim = conn.execute("SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?", (row["claim_id"],)).fetchone()
                if claim is None:
                    raise ValueError("citation review claim is dangling")
                material += json.dumps(dict(claim), sort_keys=True, separators=(",", ":"))
            else:
                claim_rows = [
                    dict(conn.execute(
                        "SELECT claim_id,sentence,context_window,marker_raw FROM claims WHERE claim_id=?",
                        (x["id"],),
                    ).fetchone())
                    for x in canonical
                ]
                material += json.dumps(claim_rows, sort_keys=True, separators=(",", ":"))
            material += json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        if target is None or raw["target_sha256"] != hashlib.sha256(material.encode("utf-8")).hexdigest():
            raise ValueError("manual Parse review target hash does not match current Parse fact")
        out.update(review_kind=review_kind, target_sha256=_sha256(raw["target_sha256"], "target_sha256"), instructions=_nonempty_text(raw["instructions"], "manual Parse review instructions"))
        if attribution:
            out["candidates"] = normalized_candidates
        return out
    if kind == "fetch":
        expected = common | {"ref_id", "ref_number", "reference", "source_identity", "instructions"}
        if set(raw) != expected:
            raise ValueError("Fetch task has an unsupported shape")
        ref_id = row["ref_id"]
        reference = _operational_reference(conn, ref_id, _FETCH_REFERENCE_FIELDS)
        if raw["ref_id"] != ref_id or raw["ref_number"] != reference["ref_number"]:
            raise ValueError("Fetch task reference identity does not match its row")
        if raw["reference"] != reference:
            raise ValueError("Fetch task reference snapshot does not match typed Parse facts")
        identity = _mapping(raw["source_identity"], "Fetch source_identity")
        if set(identity) != {
            "reference_status_tag", "fabrication_risk", "tag_reason",
            "matched_title", "metadata_match",
        }:
            raise ValueError("Fetch source_identity has an unsupported shape")
        for field in ("reference_status_tag", "fabrication_risk", "tag_reason", "matched_title"):
            _nullable_text(identity[field], f"Fetch source_identity.{field}")
        derived = _source_identity(conn, ref_id)
        if identity != derived:
            raise ValueError("Fetch task source identity does not match typed Resolve facts")
        out.update(instructions=_nonempty_text(raw["instructions"], "Fetch instructions"), tag_reason=identity["tag_reason"])
        return out

    if kind == "browser_challenge":
        expected = common | {"domain", "references", "candidate_urls", "instructions"}
        if set(raw) != expected:
            raise ValueError("browser challenge task has an unsupported shape")
        references = raw["references"]
        candidates = raw["candidate_urls"]
        if not isinstance(references, list) or not references:
            raise ValueError("browser challenge references must be non-empty")
        if not isinstance(candidates, list):
            raise ValueError("browser challenge candidate_urls must be a list")
        normalized_refs = []
        seen_refs: set[str] = set()
        for item in references:
            record = _exact(
                item,
                {"ref_id", "ref_number", "raw_entry", "doi", "url", "candidate_urls"},
                "browser challenge reference",
            )
            ref_id = _nonempty_text(record["ref_id"], "browser reference ref_id")
            if ref_id in seen_refs:
                raise ValueError("browser challenge reference is duplicated")
            seen_refs.add(ref_id)
            reference = _operational_reference(conn, ref_id, ("ref_number", "raw_entry", "doi", "url"))
            if any(record[field] != reference[field] for field in reference):
                raise ValueError("browser challenge reference does not match typed Parse facts")
            urls = record["candidate_urls"]
            if not isinstance(urls, list):
                raise ValueError("browser reference candidate_urls must be a list")
            clean_urls = [_nonempty_text(url, "browser reference URL") for url in urls]
            if len(set(clean_urls)) != len(clean_urls):
                raise ValueError("browser reference candidate URL is duplicated")
            normalized_refs.append({"ref_id": ref_id, "candidate_urls": clean_urls})
        clean_candidates = [_nonempty_text(url, "browser candidate URL") for url in candidates]
        if len(set(clean_candidates)) != len(clean_candidates):
            raise ValueError("browser challenge candidate URL is duplicated")
        out.update(
            domain=_nonempty_text(raw["domain"], "browser challenge domain"),
            instructions=_nonempty_text(raw["instructions"], "browser instructions"),
            references=normalized_refs,
            candidate_urls=clean_candidates,
        )
        return out

    if kind == "web_research":
        expected = common | {
            "attempts", "ref_id", "ref_number", "reference", "claim_sentences",
            "instructions",
        }
        if set(raw) != expected:
            raise ValueError("Web Research task has an unsupported shape")
        ref_id = row["ref_id"]
        reference = _operational_reference(conn, ref_id, _RESEARCH_REFERENCE_FIELDS)
        if raw["ref_id"] != ref_id or raw["ref_number"] != reference["ref_number"]:
            raise ValueError("Web Research task reference identity does not match its row")
        if raw["reference"] != reference:
            raise ValueError("Web Research task reference does not match typed Parse facts")
        sentences = raw["claim_sentences"]
        if not isinstance(sentences, list) or not sentences:
            raise ValueError("Web Research claim_sentences must be non-empty")
        out.update(
            attempts=_nonnegative_int(raw["attempts"], "Web Research attempts"),
            instructions=_nonempty_text(raw["instructions"], "Web Research instructions"),
            claim_sentences=[_nonempty_text(value, "Web Research claim sentence") for value in sentences],
        )
        return out

    expected = common | {
        "semantic_contract", "claim_id", "ref_id", "scope",
        "claim_evidence_payload", "effective_context",
    }
    expected |= {"source_text_id", "source_text_sha256"}
    if set(raw) != expected:
        raise ValueError("ClaimEvidence task has an unsupported shape")
    if raw["semantic_contract"] != "verify-claim-evidence-v10":
        raise ValueError("ClaimEvidence semantic contract is unsupported")
    if any(raw[name] != row[name] for name in ("claim_id", "ref_id", "scope")):
        raise ValueError("ClaimEvidence task identity does not match its row")
    claim = conn.execute(
        "SELECT sentence,context_window,marker_raw FROM claims WHERE claim_id = ?",
        (row["claim_id"],),
    ).fetchone()
    if claim is None:
        raise ValueError("ClaimEvidence claim is unavailable")
    source, source_text = _source_asset(conn, run_dir, row, raw)
    context_snapshot = _mapping(raw["effective_context"], "ClaimEvidence effective_context")
    try:
        context = context_from_snapshot(context_snapshot, source_text)
    except (ContextError, ValueError) as exc:
        raise ValueError("ClaimEvidence effective context is invalid") from exc
    expected_payload = {
        "claim": claim["sentence"],
        "claim_context": claim["context_window"],
        "citation_marker": claim["marker_raw"],
    }
    if raw["claim_evidence_payload"] != expected_payload:
        raise ValueError("ClaimEvidence claim/context payload is inconsistent")
    out.update(
        semantic_contract=raw["semantic_contract"],
        source_text_id=source["source_text_id"],
        source_text_sha256=source["sha256"],
        context_mode=context.mode,
        context_budget=context.budget,
        source_hash=_sha256(context.source_hash, "ClaimEvidence source_hash"),
        context_hash=_sha256(context.context_hash, "ClaimEvidence context_hash"),
        retrieval_algorithm=context.retrieval_algorithm,
        retrieval_config=dict(context.retrieval_config),
        ranges=[
            {"span_id": item.span_id, "raw_start": item.raw_start, "raw_end": item.raw_end}
            for item in context.ranges
        ],
    )
    return out


def replace_task_payload(conn: sqlite3.Connection, task_id: str, normalized: JsonDict) -> None:
    """Replace the one subtype row for ``task_id`` after validation."""
    for table in _TASK_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
    kind = normalized["kind"]
    error = normalized["last_error"]
    conn.execute(
        """
        UPDATE tasks
        SET task_kind = ?, last_error_stage = ?, last_error_type = ?,
            last_error_message = ?, last_error_traceback = ?
        WHERE task_id = ?
        """,
        (
            kind,
            None if error is None else error["stage"],
            None if error is None else error["type"],
            None if error is None else error["message"],
            None if error is None else error["traceback"],
            task_id,
        ),
    )
    if kind == "fetch":
        conn.execute(
            "INSERT INTO task_fetch_details(task_id,instructions) VALUES(?,?)",
            (task_id, normalized["instructions"]),
        )
    elif kind == "browser_challenge":
        refs = normalized["references"]
        urls = normalized["candidate_urls"]
        conn.execute(
            """
            INSERT INTO task_browser_challenge_details(
              task_id,domain,instructions,reference_count,candidate_url_count
            ) VALUES(?,?,?,?,?)
            """,
            (task_id, normalized["domain"], normalized["instructions"], len(refs), len(urls)),
        )
        for ref_order, reference in enumerate(refs):
            ref_urls = reference["candidate_urls"]
            conn.execute(
                """
                INSERT INTO task_browser_challenge_references(
                  task_id,reference_order,ref_id,candidate_url_count
                ) VALUES(?,?,?,?)
                """,
                (task_id, ref_order, reference["ref_id"], len(ref_urls)),
            )
            for url_order, url in enumerate(ref_urls):
                conn.execute(
                    """
                    INSERT INTO task_browser_challenge_reference_urls(
                      task_id,reference_order,url_order,url
                    ) VALUES(?,?,?,?)
                    """,
                    (task_id, ref_order, url_order, url),
                )
        for order, url in enumerate(urls):
            conn.execute(
                "INSERT INTO task_browser_challenge_candidate_urls(task_id,url_order,url) VALUES(?,?,?)",
                (task_id, order, url),
            )
    elif kind == "web_research":
        sentences = normalized["claim_sentences"]
        conn.execute(
            """
            INSERT INTO task_web_research_details(
              task_id,attempts,instructions,claim_sentence_count
            ) VALUES(?,?,?,?)
            """,
            (task_id, normalized["attempts"], normalized["instructions"], len(sentences)),
        )
        for order, sentence in enumerate(sentences):
            conn.execute(
                "INSERT INTO task_web_research_claim_sentences(task_id,sentence_order,sentence) VALUES(?,?,?)",
                (task_id, order, sentence),
            )
    elif kind == "claim_evidence":
        ranges = normalized["ranges"]
        conn.execute(
            """
            INSERT INTO task_claim_evidence_details(
              task_id,semantic_contract,source_text_id,source_text_sha256,
              context_mode,context_budget,source_hash,context_hash,
              retrieval_algorithm,retrieval_config,range_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id, normalized["semantic_contract"], normalized["source_text_id"],
                normalized["source_text_sha256"], normalized["context_mode"],
                normalized["context_budget"], normalized["source_hash"],
                normalized["context_hash"], normalized["retrieval_algorithm"],
                json.dumps(normalized["retrieval_config"], sort_keys=True, separators=(",", ":")),
                len(ranges),
            ),
        )
        for order, item in enumerate(ranges):
            conn.execute(
                """
                INSERT INTO task_claim_evidence_ranges(
                  task_id,range_order,span_id,raw_start,raw_end
                ) VALUES(?,?,?,?,?)
                """,
                (task_id, order, item["span_id"], item["raw_start"], item["raw_end"]),
            )
    elif kind == "source_identity_attestation":
        conn.execute(
            """
            INSERT INTO task_source_identity_attestation_details(
              task_id,source_text_id,source_text_sha256,target_sha256,instructions
            ) VALUES(?,?,?,?,?)
            """,
            (
                task_id,
                normalized["source_text_id"],
                normalized["source_text_sha256"],
                normalized["target_sha256"],
                normalized["instructions"],
            ),
        )
    else:
        conn.execute("INSERT INTO task_manual_parse_review_details(task_id,review_kind,target_sha256,instructions) VALUES(?,?,?,?)", (task_id, normalized["review_kind"], normalized["target_sha256"], normalized["instructions"]))
        for order, candidate in enumerate(normalized.get("candidates", [])):
            conn.execute("INSERT INTO task_manual_parse_review_candidates VALUES(?,?,?,?,?,?)", (task_id, order, candidate["id"] if normalized["review_kind"] == "citation_reference_review" else None, candidate["id"] if normalized["review_kind"] == "reference_claim_review" else None, candidate["origin"], candidate["score"]))


def _dense(rows: list[Mapping[str, Any]], key: str, count: int, label: str) -> None:
    if len(rows) != count or [int(row[key]) for row in rows] != list(range(count)):
        raise RuntimeError(f"{label} ordinals/count are inconsistent")


def _read_source_text(
    conn: sqlite3.Connection,
    run_dir: str,
    source_text_id: str,
    sha: str,
    expected_ref_id: str,
) -> str:
    source_row = conn.execute(
        "SELECT ref_id FROM source_texts WHERE source_text_id = ?", (source_text_id,),
    ).fetchone()
    if source_row is None:
        raise RuntimeError("typed ClaimEvidence source identity is unavailable")
    if not _source_reference_is_authorized(conn, expected_ref_id, source_row["ref_id"]):
        raise RuntimeError("typed ClaimEvidence source belongs to another reference")
    row = {"ref_id": expected_ref_id}
    try:
        source, text = _source_asset(
            conn,
            run_dir,
            row,
            {"source_text_id": source_text_id, "source_text_sha256": sha},
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if source["source_text_id"] != source_text_id:
        raise RuntimeError("typed ClaimEvidence source binding is inconsistent")
    return text


def read_task_payload(
    conn: sqlite3.Connection,
    run_dir: str,
    row: Mapping[str, Any],
) -> JsonDict:
    """Reconstruct the external task protocol from typed authoritative facts."""
    kind = row["task_kind"]
    try:
        _validate_parent(row, kind)
    except (KeyError, ValueError) as exc:
        raise RuntimeError("typed task parent identity is inconsistent") from exc
    hits = {
        table: conn.execute(f"SELECT * FROM {table} WHERE task_id = ?", (row["task_id"],)).fetchone()
        for table in _TASK_TABLES
    }
    expected_table = {
        "fetch": "task_fetch_details",
        "browser_challenge": "task_browser_challenge_details",
        "web_research": "task_web_research_details",
        "claim_evidence": "task_claim_evidence_details",
        "manual_parse_review": "task_manual_parse_review_details",
        "source_identity_attestation": "task_source_identity_attestation_details",
    }.get(kind)
    if expected_table is None or hits[expected_table] is None or sum(item is not None for item in hits.values()) != 1:
        raise RuntimeError("typed task subtype is missing or ambiguous")
    status = "done" if row["status"] == "applied" else "pending"
    payload: JsonDict = {"kind": kind, "status": status}
    error_values = [row[name] for name in (
        "last_error_stage", "last_error_type", "last_error_message", "last_error_traceback",
    )]
    if any(value is not None for value in error_values):
        if any(value is None for value in error_values) or row["status"] != "pending":
            raise RuntimeError("typed task last_error is inconsistent")
        payload["last_error"] = dict(zip(("stage", "type", "message", "traceback"), error_values))

    if kind == "fetch":
        ref_id = row["ref_id"]
        reference = _operational_reference(conn, ref_id, _FETCH_REFERENCE_FIELDS)
        payload.update(
            ref_id=ref_id,
            ref_number=reference["ref_number"],
            reference=reference,
            source_identity=_source_identity(conn, ref_id),
            instructions=hits[expected_table]["instructions"],
            answer=None,
        )
        return payload

    if kind == "manual_parse_review":
        detail = hits[expected_table]
        payload.update(
            review_kind=detail["review_kind"], target_sha256=detail["target_sha256"],
            instructions=detail["instructions"], answer=None,
        )
        candidates = list(conn.execute("SELECT candidate_order,candidate_ref_id,candidate_claim_id,candidate_origin,candidate_score FROM task_manual_parse_review_candidates WHERE task_id=? ORDER BY candidate_order", (row["task_id"],)))
        if candidates:
            _dense(candidates, "candidate_order", len(candidates), "manual Parse review candidates")
            payload["candidates"] = [{"id": x["candidate_ref_id"] or x["candidate_claim_id"], "origin": x["candidate_origin"], "score": x["candidate_score"]} for x in candidates]
        if detail["review_kind"] in {"citation_reference_review", "reference_claim_review"}:
            try:
                normalize_task_payload(conn, run_dir, row, payload)
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                raise RuntimeError("typed manual citation review is inconsistent") from exc
        return payload

    if kind == "source_identity_attestation":
        detail = hits[expected_table]
        try:
            snapshot = _source_identity_attestation_snapshot(
                conn, row["ref_id"], detail["source_text_id"],
            )
            if (
                detail["source_text_sha256"] != snapshot["source_text_sha256"]
                or detail["target_sha256"]
                != _source_identity_attestation_sha(snapshot)
            ):
                raise ValueError("attestation detail is stale")
            payload.update(
                snapshot,
                target_sha256=detail["target_sha256"],
                instructions=detail["instructions"],
                answer=None,
            )
            normalize_task_payload(conn, run_dir, row, payload)
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            raise RuntimeError("typed source identity attestation is inconsistent") from exc
        return payload

    if kind == "browser_challenge":
        detail = hits[expected_table]
        ref_rows = list(conn.execute(
            """
            SELECT * FROM task_browser_challenge_references
            WHERE task_id=? ORDER BY reference_order
            """,
            (row["task_id"],),
        ))
        _dense(ref_rows, "reference_order", int(detail["reference_count"]), "typed browser references")
        references = []
        for ref_row in ref_rows:
            url_rows = list(conn.execute(
                """
                SELECT * FROM task_browser_challenge_reference_urls
                WHERE task_id=? AND reference_order=? ORDER BY url_order
                """,
                (row["task_id"], ref_row["reference_order"]),
            ))
            _dense(url_rows, "url_order", int(ref_row["candidate_url_count"]), "typed browser reference URLs")
            reference = _operational_reference(conn, ref_row["ref_id"], ("ref_number", "raw_entry", "doi", "url"))
            references.append({"ref_id": ref_row["ref_id"], **reference, "candidate_urls": [item["url"] for item in url_rows]})
        url_rows = list(conn.execute(
            "SELECT * FROM task_browser_challenge_candidate_urls WHERE task_id=? ORDER BY url_order",
            (row["task_id"],),
        ))
        _dense(url_rows, "url_order", int(detail["candidate_url_count"]), "typed browser candidate URLs")
        payload.update(
            domain=detail["domain"], references=references,
            candidate_urls=[item["url"] for item in url_rows],
            instructions=detail["instructions"], answer=None,
        )
        return payload

    if kind == "web_research":
        detail = hits[expected_table]
        sentences = list(conn.execute(
            "SELECT * FROM task_web_research_claim_sentences WHERE task_id=? ORDER BY sentence_order",
            (row["task_id"],),
        ))
        _dense(sentences, "sentence_order", int(detail["claim_sentence_count"]), "typed research sentences")
        reference = _operational_reference(conn, row["ref_id"], _RESEARCH_REFERENCE_FIELDS)
        payload.update(
            attempts=int(detail["attempts"]), ref_id=row["ref_id"],
            ref_number=reference["ref_number"], reference=reference,
            claim_sentences=[item["sentence"] for item in sentences],
            instructions=detail["instructions"], answer=None,
        )
        return payload

    detail = hits[expected_table]
    ranges = list(conn.execute(
        "SELECT * FROM task_claim_evidence_ranges WHERE task_id=? ORDER BY range_order",
        (row["task_id"],),
    ))
    _dense(ranges, "range_order", int(detail["range_count"]), "typed ClaimEvidence ranges")
    source_text = _read_source_text(
        conn, run_dir, detail["source_text_id"], detail["source_text_sha256"], row["ref_id"],
    )
    try:
        retrieval_config = json.loads(detail["retrieval_config"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("typed ClaimEvidence retrieval config is invalid") from exc
    if not isinstance(retrieval_config, dict):
        raise RuntimeError("typed ClaimEvidence retrieval config is invalid")
    if detail["context_mode"] == "full_text":
        if retrieval_config:
            raise RuntimeError("typed full-text ClaimEvidence retrieval config is invalid")
        context = build_effective_context(
            source_text, mode="full_text", budget=int(detail["context_budget"]),
        )
    else:
        context = build_effective_context(
            source_text,
            mode="extractive_rag",
            budget=int(detail["context_budget"]),
            retrieved_ranges=[(int(item["raw_start"]), int(item["raw_end"])) for item in ranges],
            retrieval_algorithm=detail["retrieval_algorithm"],
            retrieval_config=retrieval_config,
        )
    if (
        context.source_hash != detail["source_hash"]
        or context.context_hash != detail["context_hash"]
        or [item.span_id for item in context.ranges] != [item["span_id"] for item in ranges]
    ):
        raise RuntimeError("typed ClaimEvidence context is internally inconsistent")
    claim = conn.execute(
        "SELECT sentence,context_window,marker_raw FROM claims WHERE claim_id=?",
        (row["claim_id"],),
    ).fetchone()
    if claim is None:
        raise RuntimeError("typed ClaimEvidence claim is unavailable")
    payload.update(
        semantic_contract=detail["semantic_contract"], claim_id=row["claim_id"],
        ref_id=row["ref_id"], scope=row["scope"],
        claim_evidence_payload={
            "claim": claim["sentence"],
            "claim_context": claim["context_window"],
            "citation_marker": claim["marker_raw"],
        },
        effective_context=context.snapshot(), source_text_id=detail["source_text_id"],
        source_text_sha256=detail["source_text_sha256"], answer=None,
    )
    return payload


def _source_answer(value: JsonDict, label: str, *, ref_id_required: bool) -> JsonDict:
    allowed = {"found", "file_path", "text", "url", "source_tier", "disposition", "guided_fetch", "identity_attested"} | ({"ref_id"} if ref_id_required else set())
    required = {"found"} | ({"ref_id"} if ref_id_required else set())
    if set(value) - allowed or not required <= set(value):
        raise ValueError(f"{label} has an unsupported shape")
    if type(value["found"]) is not bool:
        raise ValueError(f"{label}.found must be boolean")
    source_fields = [field for field in ("file_path", "text") if field in value]
    source_tier = value.get("source_tier", "fulltext")
    if source_tier not in {"fulltext", "abstract"}:
        raise ValueError(f"{label}.source_tier is invalid")
    disposition = value.get("disposition", "provided" if value["found"] else "not_found")
    if disposition not in {"provided", "not_found", "user_waived"}:
        raise ValueError(f"{label}.disposition is invalid")
    guided_fetch = value.get("guided_fetch", False)
    if type(guided_fetch) is not bool:
        raise ValueError(f"{label}.guided_fetch must be boolean")
    identity_attested = value.get("identity_attested", False)
    if type(identity_attested) is not bool:
        raise ValueError(f"{label}.identity_attested must be boolean")
    if identity_attested != bool(guided_fetch and value["found"]):
        raise ValueError(f"{label}.identity_attested requires guided_fetch=true and found=true")
    if disposition == "user_waived" and not guided_fetch:
        raise ValueError(f"{label}.user_waived requires guided_fetch=true")
    if not value["found"] and guided_fetch and disposition != "user_waived":
        raise ValueError(f"{label} guided found=false requires user_waived")
    if value["found"]:
        if len(source_fields) != 1:
            raise ValueError(f"{label} requires exactly one source value")
        if disposition != "provided":
            raise ValueError(f"{label} found=true requires disposition=provided")
    elif source_fields or source_tier != "fulltext" or disposition == "provided":
        raise ValueError(f"{label} found=false has an invalid disposition or source")
    source_kind = "none"
    source_value = None
    if source_fields:
        source_kind = source_fields[0]
        source_value = _nonempty_text(value[source_kind], f"{label}.{source_kind}")
    if source_tier == "abstract" and source_kind != "file_path":
        raise ValueError(f"{label} abstract sources require file_path")
    url_present = "url" in value
    url = None
    if url_present:
        url = _nonempty_text(value["url"], f"{label}.url")
    out = {
        "found": value["found"], "source_kind": source_kind,
        "source_value": source_value, "url_present": url_present, "url": url,
        "source_tier": source_tier, "disposition": disposition,
        "guided_fetch": guided_fetch,
        "identity_attested": identity_attested,
    }
    if ref_id_required:
        out["ref_id"] = _nonempty_text(value["ref_id"], f"{label}.ref_id")
    return out


def normalize_task_answer(
    conn: sqlite3.Connection,
    task_row: Mapping[str, Any],
    payload: JsonDict,
    *,
    task_spec: JsonDict | None = None,
) -> JsonDict:
    """Validate a task answer against the task's closed subtype contract."""
    value = _sanitize(_mapping(payload, "task answer"))
    kind = task_row["task_kind"]
    if kind == "source_identity_attestation":
        value = _mapping(payload, "source identity attestation answer")
        detail = conn.execute(
            """
            SELECT target_sha256
            FROM task_source_identity_attestation_details WHERE task_id=?
            """,
            (task_row["task_id"],),
        ).fetchone()
        if (
            detail is None
            or set(value) != {"action", "target_sha256", "reason"}
            or value.get("action") not in {
                "attest_identity", "keep_unverified",
            }
            or value.get("target_sha256") != detail["target_sha256"]
        ):
            raise ValueError("source identity attestation answer is invalid")
        return {
            "kind": kind,
            "action": value["action"],
            "target_sha256": detail["target_sha256"],
            "reason": _nonempty_text(
                value.get("reason"), "source identity attestation reason",
            ),
        }
    if kind == "manual_parse_review":
        value = _mapping(payload, "manual Parse review answer")
        detail = conn.execute("SELECT * FROM task_manual_parse_review_details WHERE task_id=?", (task_row["task_id"],)).fetchone()
        if detail is None:
            raise ValueError("manual Parse review details are unavailable")
        action = value.get("action")
        legal = ({"no_sources", "split_sources", "keep_ambiguous"} if detail["review_kind"] == "footnote_source_review" else {"correct_identity", "keep_ambiguous"} if detail["review_kind"] == "reference_identity_review" else {"select_reference", "keep_unresolved"} if detail["review_kind"] == "citation_reference_review" else {"select_claim", "keep_unresolved"})
        if action not in legal or value.get("target_sha256") != detail["target_sha256"]:
            raise ValueError("manual Parse review action or target hash is invalid")
        reason = _nonempty_text(value.get("reason"), "manual Parse review reason")
        if detail["review_kind"] in {"citation_reference_review", "reference_claim_review"}:
            expected = {"action", "target_sha256", "reason"} | ({"ref_id"} if action == "select_reference" else {"claim_id"} if action == "select_claim" else set())
            if set(value) != expected:
                raise ValueError("citation attribution answer has an unsupported shape")
            selected = value.get("ref_id") or value.get("claim_id")
            if selected is not None:
                column = "candidate_ref_id" if action == "select_reference" else "candidate_claim_id"
                candidates = {x[0] for x in conn.execute(f"SELECT {column} FROM task_manual_parse_review_candidates WHERE task_id=?", (task_row["task_id"],))}
                if selected not in candidates:
                    raise ValueError("citation attribution selection is outside the candidate set")
            return {"kind": kind, "action": action, "target_sha256": detail["target_sha256"], "reason": reason, "sources": [], "title": None, "doi": None, "selected_ref_id": selected if action == "select_reference" else None, "selected_claim_id": selected if action == "select_claim" else None}
        if action == "split_sources":
            if set(value) != {"action", "target_sha256", "reason", "source_texts"} or not isinstance(value["source_texts"], list) or len(value["source_texts"]) < 2:
                raise ValueError("split_sources requires at least two source_texts")
            sources = [_nonempty_text(item, "split source", exact=True) for item in value["source_texts"]]
            if len(set(sources)) != len(sources): raise ValueError("split source is duplicated")
            return {"kind": kind, "action": action, "target_sha256": detail["target_sha256"], "reason": reason, "sources": sources, "title": None, "doi": None, "selected_ref_id": None, "selected_claim_id": None}
        if action == "correct_identity":
            if set(value) != {"action", "target_sha256", "reason", "title", "doi"}:
                raise ValueError("correct_identity has an unsupported shape")
            title = _nullable_text(value["title"], "identity title")
            doi = _nullable_text(value["doi"], "identity doi")
            if title is None and doi is None: raise ValueError("correct_identity requires title or doi")
            return {"kind": kind, "action": action, "target_sha256": detail["target_sha256"], "reason": reason, "sources": [], "title": title, "doi": doi, "selected_ref_id": None, "selected_claim_id": None}
        if set(value) != {"action", "target_sha256", "reason"}: raise ValueError("manual Parse review has an unsupported answer shape")
        return {"kind": kind, "action": action, "target_sha256": detail["target_sha256"], "reason": reason, "sources": [], "title": None, "doi": None, "selected_ref_id": None, "selected_claim_id": None}
    if kind == "claim_evidence":
        raise ValueError("ClaimEvidence does not accept generic task answers")
    if kind == "fetch":
        return {"kind": kind, **_source_answer(value, "Fetch answer", ref_id_required=False)}
    if kind == "browser_challenge":
        answer = _exact(value, {"items"}, "browser challenge answer")
        if not isinstance(answer["items"], list):
            raise ValueError("browser challenge answer items must be a list")
        expected_ref_ids = (
            [item["ref_id"] for item in task_spec["references"]]
            if task_spec is not None
            else [
                item["ref_id"]
                for item in conn.execute(
                    """
                    SELECT ref_id FROM task_browser_challenge_references
                    WHERE task_id=? ORDER BY reference_order
                    """,
                    (task_row["task_id"],),
                )
            ]
        )
        items = [
            _source_answer(_mapping(item, "browser answer item"), "browser answer item", ref_id_required=True)
            for item in answer["items"]
        ]
        if [item["ref_id"] for item in items] != expected_ref_ids:
            raise ValueError("browser challenge answer does not cover declared references in order")
        return {"kind": kind, "items": items}
    if kind != "web_research":
        raise ValueError("task answer kind is unsupported")
    answer = _exact(value, {"found", "findings"}, "Web Research answer")
    if type(answer["found"]) is not bool or not isinstance(answer["findings"], list):
        raise ValueError("Web Research answer has invalid found/findings types")
    if answer["found"] is not bool(answer["findings"]):
        raise ValueError("Web Research found must agree with findings")
    findings = []
    for item in answer["findings"]:
        finding = _exact(item, {"url", "stance", "quote"}, "Web Research finding")
        url = _nonempty_text(finding["url"], "Web Research finding URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Web Research finding URL must be http(s)")
        if finding["stance"] not in {"supports", "contradicts"}:
            raise ValueError("Web Research finding stance is invalid")
        findings.append({
            "url": url, "stance": finding["stance"],
            "quote": _nonempty_text(finding["quote"], "Web Research quote"),
        })
    return {"kind": kind, "found": answer["found"], "findings": findings}


def replace_task_answer(conn: sqlite3.Connection, answer_id: str, normalized: JsonDict) -> None:
    for table in _ANSWER_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE answer_id = ?", (answer_id,))
    kind = normalized["kind"]
    conn.execute(
        "UPDATE task_answers SET answer_kind=? WHERE answer_id=?", (kind, answer_id),
    )
    if kind == "fetch":
        conn.execute(
            """
            INSERT INTO task_fetch_answers(
              answer_id,found,source_kind,source_value,source_tier,disposition,guided_fetch,identity_attested,url_present,url
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                answer_id, int(normalized["found"]), normalized["source_kind"],
                normalized["source_value"], normalized["source_tier"], normalized["disposition"],
                int(normalized["guided_fetch"]), int(normalized["identity_attested"]), int(normalized["url_present"]), normalized["url"],
            ),
        )
    elif kind == "browser_challenge":
        conn.execute(
            "INSERT INTO task_browser_answer_states(answer_id,item_count) VALUES(?,?)",
            (answer_id, len(normalized["items"])),
        )
        for order, item in enumerate(normalized["items"]):
            conn.execute(
                """
                INSERT INTO task_browser_answer_items(
                  answer_id,item_order,ref_id,found,source_kind,source_value,source_tier,disposition,guided_fetch,identity_attested,url_present,url
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    answer_id, order, item["ref_id"], int(item["found"]),
                    item["source_kind"], item["source_value"], item["source_tier"], item["disposition"],
                    int(item["guided_fetch"]), int(item["identity_attested"]), int(item["url_present"]), item["url"],
                ),
            )
    elif kind == "web_research":
        findings = normalized["findings"]
        conn.execute(
            "INSERT INTO task_research_answer_states(answer_id,found,finding_count) VALUES(?,?,?)",
            (answer_id, int(normalized["found"]), len(findings)),
        )
        for order, item in enumerate(findings):
            conn.execute(
                """
                INSERT INTO task_research_answer_findings(
                  answer_id,finding_order,url,stance,quote
                ) VALUES(?,?,?,?,?)
                """,
                (answer_id, order, item["url"], item["stance"], item["quote"]),
            )
    elif kind == "source_identity_attestation":
        conn.execute(
            """
            INSERT INTO task_source_identity_attestation_answers(
              answer_id,action,target_sha256,reason
            ) VALUES(?,?,?,?)
            """,
            (
                answer_id,
                normalized["action"],
                normalized["target_sha256"],
                normalized["reason"],
            ),
        )
    else:
        conn.execute("INSERT INTO task_manual_parse_review_answers(answer_id,action,target_sha256,reason,title_present,title,doi_present,doi,selected_ref_id,selected_claim_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (answer_id, normalized["action"], normalized["target_sha256"], normalized["reason"], int(normalized["title"] is not None), normalized["title"], int(normalized["doi"] is not None), normalized["doi"], normalized.get("selected_ref_id"), normalized.get("selected_claim_id")))
        for order, source in enumerate(normalized["sources"]):
            conn.execute("INSERT INTO task_manual_parse_review_split_sources(answer_id,source_order,source_text) VALUES(?,?,?)", (answer_id, order, source))


def _answer_source(row: Mapping[str, Any]) -> JsonDict:
    payload: JsonDict = {"found": bool(row["found"])}
    if row["source_kind"] == "file_path":
        payload["file_path"] = row["source_value"]
    elif row["source_kind"] == "text":
        payload["text"] = row["source_value"]
    elif row["source_kind"] != "none":
        raise RuntimeError("typed task answer source kind is invalid")
    if bool(row["url_present"]):
        if not isinstance(row["url"], str) or not row["url"].strip():
            raise RuntimeError("typed task answer URL is invalid")
        payload["url"] = row["url"]
    elif row["url"] is not None:
        raise RuntimeError("typed task answer URL presence is inconsistent")
    source_tier = row["source_tier"]
    disposition = row["disposition"]
    if source_tier not in {"fulltext", "abstract"} or disposition not in {"provided", "not_found", "user_waived"}:
        raise RuntimeError("typed task answer tier/disposition is invalid")
    if source_tier != "fulltext":
        payload["source_tier"] = source_tier
    if disposition != ("provided" if payload["found"] else "not_found"):
        payload["disposition"] = disposition
    if bool(row["guided_fetch"]):
        payload["guided_fetch"] = True
    if bool(row["identity_attested"]):
        if not payload.get("guided_fetch") or not payload["found"]:
            raise RuntimeError("typed task answer identity attestation is inconsistent")
        payload["identity_attested"] = True
    return payload


def read_task_answer(conn: sqlite3.Connection, row: Mapping[str, Any]) -> JsonDict:
    kind = row["answer_kind"]
    task = conn.execute(
        "SELECT task_kind,generation FROM tasks WHERE task_id=?", (row["task_id"],),
    ).fetchone()
    if (
        task is None
        or task["task_kind"] != kind
        or int(row["generation"]) > int(task["generation"])
    ):
        raise RuntimeError("typed task answer parent/generation is inconsistent")
    hits = {
        table: conn.execute(f"SELECT * FROM {table} WHERE answer_id=?", (row["answer_id"],)).fetchone()
        for table in _ANSWER_TABLES
    }
    expected = {
        "fetch": "task_fetch_answers",
        "browser_challenge": "task_browser_answer_states",
        "web_research": "task_research_answer_states",
        "manual_parse_review": "task_manual_parse_review_answers",
        "source_identity_attestation": "task_source_identity_attestation_answers",
    }.get(kind)
    if expected is None or hits[expected] is None or sum(item is not None for item in hits.values()) != 1:
        raise RuntimeError("typed task answer subtype is missing or ambiguous")
    if kind == "manual_parse_review":
        detail = hits[expected]
        sources = list(conn.execute("SELECT source_text FROM task_manual_parse_review_split_sources WHERE answer_id=? ORDER BY source_order", (row["answer_id"],)))
        payload = {"action": detail["action"], "target_sha256": detail["target_sha256"], "reason": detail["reason"]}
        if detail["action"] == "split_sources": payload["source_texts"] = [item["source_text"] for item in sources]
        if detail["action"] == "correct_identity": payload.update(title=detail["title"] if detail["title_present"] else None, doi=detail["doi"] if detail["doi_present"] else None)
        if detail["selected_ref_id"] is not None:
            payload["ref_id"] = detail["selected_ref_id"]
        if detail["selected_claim_id"] is not None:
            payload["claim_id"] = detail["selected_claim_id"]
        return payload
    if kind == "source_identity_attestation":
        detail = hits[expected]
        return {
            "action": detail["action"],
            "target_sha256": detail["target_sha256"],
            "reason": detail["reason"],
        }
    if kind == "fetch":
        return _answer_source(hits[expected])
    if kind == "browser_challenge":
        state = hits[expected]
        items = list(conn.execute(
            "SELECT * FROM task_browser_answer_items WHERE answer_id=? ORDER BY item_order",
            (row["answer_id"],),
        ))
        _dense(items, "item_order", int(state["item_count"]), "typed browser answer items")
        return {"items": [{"ref_id": item["ref_id"], **_answer_source(item)} for item in items]}
    state = hits[expected]
    findings = list(conn.execute(
        "SELECT * FROM task_research_answer_findings WHERE answer_id=? ORDER BY finding_order",
        (row["answer_id"],),
    ))
    _dense(findings, "finding_order", int(state["finding_count"]), "typed research findings")
    if bool(state["found"]) is not bool(findings):
        raise RuntimeError("typed research found/cardinality is inconsistent")
    return {
        "found": bool(state["found"]),
        "findings": [
            {"url": item["url"], "stance": item["stance"], "quote": item["quote"]}
            for item in findings
        ],
    }
