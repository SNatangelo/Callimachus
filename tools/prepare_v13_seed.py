#!/usr/bin/env python3
# tools/prepare_v13_seed.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Plan and, only with ``--apply``, build the vetted V13 content-store seed.

The command is offline and non-destructive.  V12 is the baseline; only the six
manually adjudicated valid losses are taken from V11, while known false rows are
required to fail the current material/identity gates.  Only exact, currently
validated bytes enter the seed; unavailable or rejected rows are left for the
live run to fetch again.  A dry run is the default and never creates output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.fetch.storage import content_store  # noqa: E402
from core.fetch.extraction import fetch_html  # noqa: E402
from core.fetch.extraction.pdf import _quality, fulltext_material_issue  # noqa: E402
from core.parse import source_text  # noqa: E402
from core.resolve import sources  # noqa: E402
from core.resolve.providers import openai_reports  # noqa: E402


EXPECTED_TRANSITION_COUNTS = {
    "valid_gain": 6,
    "valid_loss": 6,
    "false_gain_material": 3,
    "false_gain_identity": 1,
    "false_gain_ancillary": 1,
    "false_old_material": 3,
    "false_old_identity": 1,
}
FALSE_GAIN_CLASSES = frozenset(
    {"false_gain_material", "false_gain_identity", "false_gain_ancillary"}
)
FALSE_OLD_CLASSES = frozenset({"false_old_material", "false_old_identity"})
EXPECTED_PAPERS = 18
EXPECTED_V12_FULLTEXT = 538
EXPECTED_VETTED_UNION = 539
EXPECTED_SEEDABLE_REFS = 508
EXPECTED_REJECTION_COUNTS = {
    "ancillary_document_url": 3,
    "pubmed_metadata_record": 5,
    "source_exact_bytes_unavailable": 18,
    "source_identity_not_confirmed": 2,
    "ssrn_metadata_record": 3,
}
EXPECTED_RUN_SCHEMAS = {"V11": "70", "V12": "73"}


class SeedPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    paper: str
    ref_number: int
    transition: str
    audit_class: str
    reason: str
    source_origin: str
    source_locator: str

    @property
    def key(self) -> tuple[str, int]:
        return self.paper, self.ref_number


@dataclass(frozen=True)
class Candidate:
    paper: str
    ref_number: int
    source_bundle: str
    storage_root: Path
    ref: dict
    source_text_id: str
    origin: str
    stored_path: str
    sha256: str
    char_count: int
    source_ref: str | None
    mapping: str | None
    match_signal: str | None
    match_score: float | None
    identity_status: str | None
    identity_note: str | None
    content_version: str | None
    provenance_relation: str | None
    supplied_by: str | None
    supplied_via: str | None
    file_format: str | None
    extraction_flags: tuple[str, ...]
    extraction_method: str | None
    resolve_result: dict | None
    candidate_context: dict | None
    bytes_verified: bool

    @property
    def absolute_path(self) -> Path:
        return self.storage_root / self.stored_path

    @property
    def key(self) -> tuple[str, int]:
        return self.paper, self.ref_number


@dataclass(frozen=True)
class Validation:
    candidate: Candidate
    accepted: bool
    reason: str
    prepared_sha256: str | None = None
    prepared_char_count: int | None = None
    preparation: dict | None = None


def load_transitions(path: Path) -> dict[tuple[str, int], Transition]:
    rows: dict[tuple[str, int], Transition] = {}
    counts: Counter[str] = Counter()
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "paper", "ref_number", "transition", "audit_class", "reason",
            "source_origin", "source_locator",
        }
        if set(reader.fieldnames or ()) != required:
            raise SeedPlanError("transition ledger columns do not match the V12 audit contract")
        for raw in reader:
            try:
                row = Transition(
                    paper=str(raw["paper"]).strip(),
                    ref_number=int(raw["ref_number"]),
                    transition=str(raw["transition"]).strip(),
                    audit_class=str(raw["audit_class"]).strip(),
                    reason=str(raw["reason"]).strip(),
                    source_origin=str(raw["source_origin"]).strip(),
                    source_locator=str(raw["source_locator"]).strip(),
                )
            except (TypeError, ValueError) as exc:
                raise SeedPlanError(f"invalid transition ledger row: {raw!r}") from exc
            if not row.paper or row.ref_number <= 0 or row.key in rows:
                raise SeedPlanError(f"invalid or duplicate transition key: {row.key!r}")
            if row.audit_class not in EXPECTED_TRANSITION_COUNTS:
                raise SeedPlanError(f"unknown transition audit class: {row.audit_class}")
            rows[row.key] = row
            counts[row.audit_class] += 1
    if counts != Counter(EXPECTED_TRANSITION_COUNTS):
        raise SeedPlanError(
            f"transition ledger counts changed: {dict(sorted(counts.items()))}"
        )
    return rows


def _run_directories(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise SeedPlanError(f"run bundle does not exist: {root}")
    runs = {
        child.name: child
        for child in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if child.is_dir() and (child / "run.sqlite").is_file()
    }
    if len(runs) != EXPECTED_PAPERS:
        raise SeedPlanError(f"expected {EXPECTED_PAPERS} runs below {root}, found {len(runs)}")
    return runs


def _load_run_fulltext_refs(
    run_dir: Path,
    paper: str,
    source_bundle: str,
) -> dict[int, dict]:
    db_path = run_dir / "run.sqlite"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        schema_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        expected_schema = EXPECTED_RUN_SCHEMAS.get(source_bundle)
        if schema_row is None or str(schema_row[0]) != expected_schema:
            raise SeedPlanError(
                f"{source_bundle}/{paper}: expected schema {expected_schema}, "
                f"found {None if schema_row is None else schema_row[0]}"
            )
        required_columns = {
            "reference_entries": {
                "ref_id", "raw_entry", "title", "doi", "pmid", "isbn", "url",
                "year", "ay_surname", "ay_year", "ay_suffix", "source_type",
                "source_kind", "indexability", "source_type_confidence",
            },
            "operational_references": {"ref_id", "ref_number", "parse_ref_id"},
            "source_texts": {"ref_id", "tier"},
        }
        for table, required in required_columns.items():
            observed = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not required.issubset(observed):
                missing = sorted(required - observed)
                raise SeedPlanError(
                    f"{source_bundle}/{paper}: {table} is missing columns {missing}"
                )
        refs_by_id = {}
        for row in conn.execute(
            """
            SELECT
              o.ref_id AS operational_ref_id,
              o.ref_number AS operational_ref_number,
              r.*
            FROM operational_references o
            JOIN reference_entries r ON r.ref_id = o.parse_ref_id
            ORDER BY o.ref_number, o.ref_id
            """
        ):
            ref = {
                key: row[key]
                for key in row.keys()
                if key not in {"operational_ref_id", "operational_ref_number", "ref_id", "ref_number"}
            }
            ref["id"] = row["operational_ref_id"]
            ref["ref_number"] = int(row["operational_ref_number"])
            refs_by_id[ref["id"]] = ref
        out: dict[int, dict] = {}
        for row in conn.execute(
            "SELECT DISTINCT ref_id FROM source_texts WHERE tier = 'fulltext' "
            "ORDER BY ref_id"
        ):
            ref = refs_by_id.get(row["ref_id"])
            if not isinstance(ref, dict) or not isinstance(ref.get("ref_number"), int):
                raise SeedPlanError(f"{source_bundle}/{paper}: fulltext has no Parse identity")
            out[ref["ref_number"]] = ref
        return out
    finally:
        conn.close()


def _load_candidates(run_dir: Path, paper: str, source_bundle: str) -> dict[int, list[Candidate]]:
    """Locate run-selected fulltexts by their exact recorded checksum.

    ``run.sqlite`` is authoritative for both selection and provenance.  Bytes are
    taken preferentially from a unique content-cache path and only when their
    current checksum equals the source row.  The run-local path is a checked
    fallback; an overwritten path is never imported.
    """
    refs = _load_run_fulltext_refs(run_dir, paper, source_bundle)
    store_root = run_dir.parent / "_state" / paper / content_store.STORE_SUBDIR
    db_path = store_root / content_store.DB_FILENAME
    if not db_path.is_file():
        raise SeedPlanError(f"{source_bundle}/{paper}: content store does not exist")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        schema_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema_row is None or str(schema_row[0]) != str(content_store.SCHEMA_VERSION):
            raise SeedPlanError(
                f"{source_bundle}/{paper}: expected content-store schema "
                f"{content_store.SCHEMA_VERSION}, "
                f"found {None if schema_row is None else schema_row[0]}"
            )
    finally:
        conn.close()

    cached_by_sha: dict[str, list[Path]] = defaultdict(list)
    cache_dir = store_root / content_store.PARSED_CACHE_SUBDIR / "fulltext"
    for path in sorted(cache_dir.rglob("*.txt")) if cache_dir.is_dir() else ():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cached_by_sha[hashlib.sha256(text.encode("utf-8")).hexdigest()].append(path)

    db_path = run_dir / "run.sqlite"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        required_columns = {
            "source_texts": {
                "source_text_id", "ref_id", "tier", "origin", "stored_path", "sha256",
                "char_count", "source_ref", "mapping", "match_signal", "match_score",
                "identity_status", "identity_note", "content_version",
                "provenance_relation", "supplied_by", "supplied_via", "file_format",
                "extraction_method",
            },
            "source_text_extraction_flags": {"source_text_id", "flag"},
            "resolve_results": {"ref_id", "status", "via", "matched_title"},
            "resolve_evidence_profiles": {
                "ref_id", "profile_kind", "minimum_checks_completed",
                "minimum_checks_completed_present",
            },
            "resolve_evidence_metadata_matches": {
                "ref_id", "role", "author_match", "author_match_present",
                "year_match", "year_match_present", "venue_overlap",
                "venue_overlap_present",
            },
            "resolve_evidence_metadata_hard_conflicts": {
                "ref_id", "role", "conflict_order", "conflict",
            },
        }
        for table, required in required_columns.items():
            observed = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not required.issubset(observed):
                missing = sorted(required - observed)
                raise SeedPlanError(
                    f"{source_bundle}/{paper}: {table} is missing columns {missing}"
                )

        flags: dict[str, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT source_text_id, flag FROM source_text_extraction_flags "
            "ORDER BY source_text_id, flag"
        ):
            flags[row["source_text_id"]].append(row["flag"])

        resolve_by_ref: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT ref_id, status, via, matched_title FROM resolve_results ORDER BY ref_id"
        ):
            resolve_by_ref[row["ref_id"]] = {
                "status": row["status"],
                "via": row["via"],
                "matched_title": row["matched_title"],
            }
        for row in conn.execute(
            "SELECT * FROM resolve_evidence_profiles "
            "WHERE profile_kind = 'normal' ORDER BY ref_id"
        ):
            result = resolve_by_ref.setdefault(row["ref_id"], {})
            profile = result.setdefault("evidence_profile", {})
            if row["minimum_checks_completed_present"]:
                profile["minimum_checks_completed"] = bool(
                    row["minimum_checks_completed"]
                )
        metadata_by_ref: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT * FROM resolve_evidence_metadata_matches "
            "WHERE role IN ('best', 'top') "
            "ORDER BY ref_id, CASE role WHEN 'best' THEN 0 ELSE 1 END"
        ):
            if row["ref_id"] in metadata_by_ref:
                continue
            metadata = {}
            for key in ("author_match", "year_match", "venue_overlap"):
                if row[f"{key}_present"]:
                    metadata[key] = bool(row[key]) if row[key] is not None else None
            metadata_by_ref[row["ref_id"]] = metadata
        conflicts_by_ref: dict[str, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT ref_id, conflict FROM resolve_evidence_metadata_hard_conflicts "
            "ORDER BY ref_id, role, conflict_order"
        ):
            if row["conflict"] not in conflicts_by_ref[row["ref_id"]]:
                conflicts_by_ref[row["ref_id"]].append(row["conflict"])
        for ref_id in set(metadata_by_ref) | set(conflicts_by_ref):
            result = resolve_by_ref.setdefault(ref_id, {})
            profile = result.setdefault("evidence_profile", {})
            metadata = dict(metadata_by_ref.get(ref_id, {}))
            metadata["hard_conflicts"] = list(conflicts_by_ref.get(ref_id, ()))
            profile["metadata_match"] = metadata

        out: dict[int, list[Candidate]] = {number: [] for number in refs}
        for row in conn.execute(
            "SELECT * FROM source_texts WHERE tier = 'fulltext' "
            "ORDER BY ref_id, source_text_id"
        ):
            ref = next(
                (item for item in refs.values() if item["id"] == row["ref_id"]),
                None,
            )
            if ref is None:
                raise SeedPlanError(
                    f"{source_bundle}/{paper}: fulltext has no Parse identity"
                )
            located: Path | None = None
            if cached_by_sha.get(row["sha256"]):
                located = cached_by_sha[row["sha256"]][0]
            else:
                run_path = run_dir / row["stored_path"]
                try:
                    run_text = run_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    run_text = ""
                if (
                    run_text
                    and hashlib.sha256(run_text.encode("utf-8")).hexdigest()
                    == row["sha256"]
                ):
                    located = run_path
            bytes_verified = located is not None
            if located is None:
                # Keep provenance available for deterministic URL-only rejection
                # (for example a known SSRN record page), but never read or import
                # the now-overwritten bytes as if they matched this source row.
                located = run_dir / row["stored_path"]
            if located.is_relative_to(store_root):
                storage_root = store_root
                stored_path = located.relative_to(store_root).as_posix()
            else:
                storage_root = run_dir
                stored_path = located.relative_to(run_dir).as_posix()
            resolve_result = resolve_by_ref.get(row["ref_id"])
            matched_title = str((resolve_result or {}).get("matched_title") or "").strip()
            out[ref["ref_number"]].append(Candidate(
                paper=paper,
                ref_number=ref["ref_number"],
                source_bundle=source_bundle,
                storage_root=storage_root,
                ref=dict(ref),
                source_text_id=row["source_text_id"],
                origin=row["origin"],
                stored_path=stored_path,
                sha256=row["sha256"],
                char_count=row["char_count"],
                source_ref=row["source_ref"],
                mapping=row["mapping"],
                match_signal=row["match_signal"],
                match_score=row["match_score"],
                identity_status=row["identity_status"],
                identity_note=row["identity_note"],
                content_version=row["content_version"],
                provenance_relation=row["provenance_relation"],
                supplied_by=row["supplied_by"],
                supplied_via=row["supplied_via"],
                file_format=row["file_format"],
                extraction_flags=tuple(flags.get(row["source_text_id"], ())),
                extraction_method=row["extraction_method"],
                resolve_result=resolve_result,
                candidate_context={"title": matched_title} if matched_title else None,
                bytes_verified=bytes_verified,
            ))
        return out
    finally:
        conn.close()


def _validate(candidate: Candidate) -> Validation:
    if fetch_html.is_ancillary_document_url(candidate.source_ref or ""):
        return Validation(candidate, False, "ancillary_document_url")
    record_kind = fetch_html.metadata_record_kind(candidate.source_ref or "")
    if record_kind is not None:
        return Validation(candidate, False, record_kind.replace(" ", "_"))
    if not candidate.bytes_verified:
        return Validation(candidate, False, "source_exact_bytes_unavailable")
    path = candidate.absolute_path
    if not path.is_file():
        return Validation(candidate, False, "source_file_missing")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Validation(candidate, False, "source_file_unreadable")
    material_issue = fulltext_material_issue(text, candidate.ref)
    if material_issue:
        return Validation(candidate, False, material_issue)
    observed_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if observed_sha256 != candidate.sha256:
        return Validation(candidate, False, "source_sha256_mismatch")
    if len(text) != candidate.char_count:
        return Validation(candidate, False, "source_char_count_mismatch")
    if not _quality(text):
        return Validation(candidate, False, "source_below_quality_gate")
    hard_resolve_conflict = content_store._resolve_has_hard_identity_conflict(
        candidate.resolve_result
    )
    explicit_cited_identity = content_store._source_ref_matches_strong_identifier(
        candidate.ref,
        candidate.source_ref,
        candidate.mapping,
        candidate.identity_status,
    )
    citation_conflict_probe = None
    if hard_resolve_conflict and not explicit_cited_identity:
        citation_conflict_probe = sources.document_identity_probe(
            candidate.ref, text, None, None
        )
        if not (
            citation_conflict_probe.get("ok")
            and citation_conflict_probe.get("decision") == "confirmed"
            and citation_conflict_probe.get("author_ok") is not False
            and citation_conflict_probe.get("expected_title_source")
            in {"citation", "citation_quoted", "citation_raw_entry"}
        ):
            return Validation(candidate, False, "resolve_metadata_identity_conflict")
    strong_recorded_signal = (
        str(candidate.match_signal or "").casefold()
        in {
            "doi", "pmid", "arxiv_id", "acl_id",
            "official_curated_document",
        }
        and isinstance(candidate.match_score, (int, float))
        and float(candidate.match_score) >= 0.95
    )
    if (
        explicit_cited_identity
        or citation_conflict_probe is not None
        or openai_reports.known_document_identity(
            candidate.ref, candidate.source_ref, text
        )
    ):
        identity_ok = True
    elif content_store._source_ref_matches_strong_identifier(
        candidate.ref,
        candidate.source_ref,
        candidate.mapping,
        candidate.identity_status,
    ) or strong_recorded_signal:
        identity_ok = True
    else:
        citation_probe = sources.document_identity_probe(
            candidate.ref,
            text,
            candidate.resolve_result,
            None,
        )
        citation_probe_confirmed = bool(
            citation_probe.get("ok")
            and citation_probe.get("decision") == "confirmed"
            and citation_probe.get("author_ok") is not False
            and citation_probe.get("expected_title_source")
            in {"citation", "citation_quoted", "citation_raw_entry"}
        )
        probe = citation_probe if citation_probe_confirmed else sources.document_identity_probe(
            candidate.ref,
            text,
            candidate.resolve_result,
            candidate.candidate_context,
        )
        externally_corroborated_ok = bool(
            candidate.identity_status == "externally_corroborated_text"
            and candidate.mapping == "web"
            and candidate.match_signal == "title"
            and isinstance(candidate.match_score, (int, float))
            and float(candidate.match_score) >= 0.95
            and citation_probe.get("decision") != "rejected"
            and float(citation_probe.get("title_overlap") or 0.0) >= 0.9
            and citation_probe.get("author_ok") is True
        )
        identity_ok = bool(
            (probe.get("ok") and probe.get("author_ok") is not False)
            or externally_corroborated_ok
        )
    if not identity_ok:
        return Validation(candidate, False, "source_identity_not_confirmed")
    prepared, preparation = source_text.prepare_for_verify(
        text, format_hint=candidate.extraction_method
    )
    if not prepared or not _quality(prepared):
        return Validation(candidate, False, "prepared_source_below_quality_gate")
    return Validation(
        candidate,
        True,
        "accepted",
        prepared_sha256=hashlib.sha256(prepared.encode("utf-8")).hexdigest(),
        prepared_char_count=len(prepared),
        preparation=preparation,
    )


def _rank(validation: Validation) -> tuple:
    candidate = validation.candidate
    signal_rank = {
        "doi": 7,
        "pmid": 7,
        "arxiv_id": 6,
        "acl_id": 6,
        "official_curated_document": 6,
        "url": 5,
        "title": 4,
        "tokens": 3,
    }
    try:
        score = float(candidate.match_score)
    except (TypeError, ValueError):
        score = 0.0
    return (
        1 if candidate.content_version in (None, "published") else 0,
        1 if candidate.identity_status else 0,
        signal_rank.get(str(candidate.match_signal or "").casefold(), 0),
        score,
        candidate.char_count,
        candidate.source_ref or "",
    )


def build_plan(v11_root: Path, v12_root: Path, transitions_path: Path) -> dict:
    transitions = load_transitions(transitions_path)
    v11_runs = _run_directories(v11_root)
    v12_runs = _run_directories(v12_root)
    if set(v11_runs) != set(v12_runs):
        raise SeedPlanError("V11 and V12 paper sets differ")

    v11: dict[tuple[str, int], list[Candidate]] = {}
    v12: dict[tuple[str, int], list[Candidate]] = {}
    for paper in sorted(v12_runs, key=str.casefold):
        for ref_number, candidates in _load_candidates(v12_runs[paper], paper, "V12").items():
            v12[(paper, ref_number)] = candidates
        for ref_number, candidates in _load_candidates(v11_runs[paper], paper, "V11").items():
            v11[(paper, ref_number)] = candidates

    if len(v12) != EXPECTED_V12_FULLTEXT:
        raise SeedPlanError(
            f"V12 fulltext baseline changed: expected {EXPECTED_V12_FULLTEXT}, found {len(v12)}"
        )

    selected_keys = set(v12)
    selected_keys -= {
        key for key, transition in transitions.items()
        if transition.audit_class in FALSE_GAIN_CLASSES
    }
    selected_keys |= {
        key for key, transition in transitions.items()
        if transition.audit_class == "valid_loss"
    }
    if len(selected_keys) != EXPECTED_VETTED_UNION:
        raise SeedPlanError(
            f"vetted union arithmetic changed: expected {EXPECTED_VETTED_UNION}, "
            f"found {len(selected_keys)}"
        )

    accepted: list[Validation] = []
    rejected: list[dict] = []
    for key in sorted(selected_keys, key=lambda item: (item[0].casefold(), item[1])):
        transition = transitions.get(key)
        source_bundle = "V11" if transition and transition.audit_class == "valid_loss" else "V12"
        candidates = (v11 if source_bundle == "V11" else v12).get(key, [])
        validations = [_validate(candidate) for candidate in candidates]
        valid = [item for item in validations if item.accepted]
        if not valid:
            rejected.append({
                "paper": key[0],
                "ref_number": key[1],
                "source_bundle": source_bundle,
                "audit_class": transition.audit_class if transition else "stable_v12",
                "reasons": sorted({item.reason for item in validations}) or ["no_fulltext_source"],
            })
            continue
        accepted.append(max(valid, key=_rank))

    false_gate_results = []
    for key, transition in sorted(transitions.items()):
        if transition.audit_class not in FALSE_GAIN_CLASSES | FALSE_OLD_CLASSES:
            continue
        pool = v12 if transition.audit_class in FALSE_GAIN_CLASSES else v11
        validations = [_validate(candidate) for candidate in pool.get(key, [])]
        false_gate_results.append({
            "paper": key[0],
            "ref_number": key[1],
            "audit_class": transition.audit_class,
            "blocked": bool(validations) and not any(item.accepted for item in validations),
            "reasons": sorted({item.reason for item in validations}) or ["no_fulltext_source"],
        })

    rejection_counts: Counter[str] = Counter()
    for item in rejected:
        rejection_counts.update(item["reasons"])
    accepted_keys = {item.candidate.key for item in accepted}
    required_valid_keys = {
        key for key, transition in transitions.items()
        if transition.audit_class in {"valid_gain", "valid_loss"}
    }
    valid_transition_rejections = sorted(required_valid_keys - accepted_keys)
    rejection_contract_matches = (
        rejection_counts == Counter(EXPECTED_REJECTION_COUNTS)
    )

    summary = {
        "v12_fulltext_baseline": len(v12),
        "known_false_v12_excluded": sum(
            1 for item in transitions.values() if item.audit_class in FALSE_GAIN_CLASSES
        ),
        "valid_v11_losses_added": sum(
            1 for item in transitions.values() if item.audit_class == "valid_loss"
        ),
        "expected_vetted_union": EXPECTED_VETTED_UNION,
        "expected_seedable_refs": EXPECTED_SEEDABLE_REFS,
        "accepted_refs": len(accepted),
        "rejected_refs": len(rejected),
        "valid_transition_rejections": len(valid_transition_rejections),
        "rejection_contract_matches": rejection_contract_matches,
        "known_false_gate_checks": len(false_gate_results),
        "known_false_blocked": sum(1 for item in false_gate_results if item["blocked"]),
        "ready_to_apply": (
            len(accepted) == EXPECTED_SEEDABLE_REFS
            and rejection_contract_matches
            and not valid_transition_rejections
            and all(item["blocked"] for item in false_gate_results)
        ),
    }
    return {
        "summary": summary,
        "accepted": accepted,
        "rejected": rejected,
        "false_gate_results": false_gate_results,
        "transition_ledger_sha256": hashlib.sha256(transitions_path.read_bytes()).hexdigest(),
    }


def _ledger_record(validation: Validation) -> dict:
    candidate = validation.candidate
    return {
        "paper": candidate.paper,
        "ref_number": candidate.ref_number,
        "source_bundle": candidate.source_bundle,
        "source_text_id": candidate.source_text_id,
        "source_stored_path": candidate.stored_path,
        "source_sha256": candidate.sha256,
        "source_ref": candidate.source_ref,
        "origin": candidate.origin,
        "prepared_sha256": validation.prepared_sha256,
        "prepared_char_count": validation.prepared_char_count,
    }


def apply_plan(plan: dict, output_root: Path) -> None:
    if not plan["summary"]["ready_to_apply"]:
        raise SeedPlanError("seed plan is not ready; refusing an unvetted V13 seed")
    if output_root.exists():
        raise SeedPlanError(f"refusing to overwrite existing V13 path: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(
        prefix=f".{output_root.name}.seed-",
        dir=output_root.parent,
    ))
    previous_state = os.environ.get(content_store.ENV_STATE_DIR)
    try:
        for validation in plan["accepted"]:
            candidate = validation.candidate
            refreshed = _validate(candidate)
            if not refreshed.accepted or refreshed.prepared_sha256 != validation.prepared_sha256:
                raise SeedPlanError(
                    f"source changed after planning: {candidate.paper}/{candidate.ref_number}"
                )
            raw_text = candidate.absolute_path.read_text(encoding="utf-8", errors="replace")
            prepared, preparation = source_text.prepare_for_verify(
                raw_text, format_hint=candidate.extraction_method
            )
            prepared_sha256 = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
            if prepared_sha256 != validation.prepared_sha256:
                raise SeedPlanError(
                    "source changed while applying seed: "
                    f"{candidate.paper}/{candidate.ref_number}"
                )
            state_dir = staging_root / "_state" / candidate.paper
            os.environ[content_store.ENV_STATE_DIR] = str(state_dir)
            stored = content_store.record_parsed_text(
                str(staging_root / candidate.paper),
                candidate.ref,
                "fulltext",
                candidate.origin,
                prepared,
                source_ref=candidate.source_ref,
                mapping=candidate.mapping,
                signal=candidate.match_signal,
                score=candidate.match_score,
                identity_status=candidate.identity_status,
                identity_note=candidate.identity_note,
                content_version=candidate.content_version,
                provenance_relation=candidate.provenance_relation,
                supplied_by=candidate.supplied_by,
                supplied_via=candidate.supplied_via,
                file_format=candidate.file_format,
                extraction_method=candidate.extraction_method,
                extraction_flags=list(candidate.extraction_flags),
                preparation=preparation,
            )
            if not stored:
                raise SeedPlanError(
                    f"content store rejected {candidate.paper}/{candidate.ref_number}"
                )
        manifest = {
            "kind": "v13_vetted_union_seed",
            "transition_ledger_sha256": plan["transition_ledger_sha256"],
            "summary": plan["summary"],
            "sources": [_ledger_record(item) for item in plan["accepted"]],
            "false_gate_results": plan["false_gate_results"],
        }
        provenance_dir = staging_root / "_provenance"
        provenance_dir.mkdir(parents=True)
        (provenance_dir / "v13_seed_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output_root.exists():
            raise SeedPlanError(
                f"V13 path appeared during seed preparation: {output_root}"
            )
        staging_root.rename(output_root)
    finally:
        if previous_state is None:
            os.environ.pop(content_store.ENV_STATE_DIR, None)
        else:
            os.environ[content_store.ENV_STATE_DIR] = previous_state
        if staging_root.exists():
            shutil.rmtree(staging_root)


def _public_plan(plan: dict, *, details: bool = False) -> dict:
    reason_counts: Counter[str] = Counter()
    for item in plan["rejected"]:
        reason_counts.update(item["reasons"])
    transition_rejections = [
        item for item in plan["rejected"] if item["audit_class"] != "stable_v12"
    ]
    stable_rejections = [
        item for item in plan["rejected"] if item["audit_class"] == "stable_v12"
    ]
    public = {
        "summary": plan["summary"],
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "transition_rejections": transition_rejections,
        "stable_rejected_count": len(stable_rejections),
        "stable_rejected_sample": stable_rejections[:20],
        "false_gate_results": plan["false_gate_results"],
        "transition_ledger_sha256": plan["transition_ledger_sha256"],
    }
    if details:
        public["rejected"] = plan["rejected"]
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v11", required=True, type=Path)
    parser.add_argument("--v12", required=True, type=Path)
    parser.add_argument("--transitions", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create the V13 seed; without this flag the command is read-only",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="include every rejected reference in dry-run JSON output",
    )
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.v11, args.v12, args.transitions)
        print(json.dumps(_public_plan(plan, details=args.details), indent=2, sort_keys=True))
        if args.apply:
            apply_plan(plan, args.out)
        return 0 if plan["summary"]["ready_to_apply"] else 2
    except SeedPlanError as exc:
        print(f"V13 seed refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
