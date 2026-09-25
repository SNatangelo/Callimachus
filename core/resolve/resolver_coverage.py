# core/resolve/resolver_coverage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Dated, append-only resolver journal-coverage observations.

Coverage is a local operational fact, separate from the immutable journal
authority catalogue and from article identity.  A failed refresh deliberately
supersedes a stale positive observation for new suspicion decisions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping
import argparse


CATALOG_ENV = "CALLIMACHUS_RESOLVER_COVERAGE_DB"
TTL_ENV = "CALLIMACHUS_RESOLVER_COVERAGE_TTL_DAYS"
SCHEMA_VERSION = 2
STATUSES = frozenset({"covered", "not_covered", "incomplete"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_path() -> Path:
    configured = str(os.environ.get(CATALOG_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "storage" / "resolver-coverage.sqlite"


def ttl_days(environ: Mapping[str, str] | None = None) -> int:
    raw = str((os.environ if environ is None else environ).get(TTL_ENV) or "30").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{TTL_ENV} must be a positive integer")
    return int(raw)


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class Catalog:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._bootstrap()
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def _bootstrap(self) -> None:
        existing_tables = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        existing_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if existing_tables and (
            "coverage_catalog" not in existing_tables
            or existing_version != SCHEMA_VERSION
        ):
            raise RuntimeError(
                "resolver coverage catalog schema is incompatible; rebuild the local catalog"
            )
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS coverage_catalog(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          created_at TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version=2)
        );
        CREATE TABLE IF NOT EXISTS coverage_authorities(
          authority_hash TEXT PRIMARY KEY CHECK(length(authority_hash)=64),
          authority_record_id TEXT NOT NULL, authority_snapshot_sha256 TEXT NOT NULL CHECK(length(authority_snapshot_sha256)=64),
          canonical_title TEXT NOT NULL, issns_json_sha256 TEXT NOT NULL CHECK(length(issns_json_sha256)=64)
        );
        CREATE TABLE IF NOT EXISTS coverage_authority_issns(
          authority_hash TEXT NOT NULL REFERENCES coverage_authorities(authority_hash),
          issn TEXT NOT NULL, PRIMARY KEY(authority_hash,issn)
        );
        CREATE TABLE IF NOT EXISTS coverage_payloads(
          payload_sha256 TEXT PRIMARY KEY CHECK(length(payload_sha256)=64), payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coverage_snapshots(
          snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
          authority_hash TEXT NOT NULL REFERENCES coverage_authorities(authority_hash),
          resolver TEXT NOT NULL, rule_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('covered','not_covered','incomplete')),
          provider_journal_id TEXT, work_count INTEGER,
          source_url TEXT, http_status INTEGER, response_sha256 TEXT REFERENCES coverage_payloads(payload_sha256),
          checked_at TEXT NOT NULL, expires_at TEXT NOT NULL, query_contract TEXT NOT NULL,
          completion TEXT NOT NULL CHECK(completion IN ('complete','partial','incomplete')),
          reason TEXT NOT NULL, refresh_mode TEXT NOT NULL CHECK(refresh_mode IN ('auto','manual'))
        );
        CREATE TRIGGER IF NOT EXISTS coverage_snapshots_no_update BEFORE UPDATE ON coverage_snapshots
        BEGIN SELECT RAISE(ABORT,'coverage snapshots are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS coverage_snapshots_no_delete BEFORE DELETE ON coverage_snapshots
        BEGIN SELECT RAISE(ABORT,'coverage snapshots are append-only'); END;
        """)
        self.conn.execute("INSERT OR IGNORE INTO coverage_catalog VALUES(1,?,?)", (_iso(_now()), SCHEMA_VERSION))
        row = self.conn.execute("SELECT schema_version FROM coverage_catalog WHERE singleton=1").fetchone()
        if row is None or row["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError("resolver coverage catalog schema is incompatible; rebuild the local catalog")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def authority_hash(self, authority: dict) -> str:
        required = {"record_id", "snapshot_sha256", "canonical_title", "issns"}
        if set(authority) != required or not isinstance(authority["issns"], tuple) or not authority["issns"]:
            raise ValueError("coverage authority shape is invalid")
        if any(not isinstance(value, str) or not value.strip() for value in authority.values() if value is not authority["issns"]):
            raise ValueError("coverage authority identity is invalid")
        return _sha({**authority, "issns": list(authority["issns"])})

    def append(self, authority: dict, probe: dict, *, now: datetime | None = None, ttl: int | None = None, refresh_mode: str = "auto") -> dict:
        now = now or _now()
        ttl = ttl if ttl is not None else ttl_days()
        if ttl < 1 or refresh_mode not in {"auto", "manual"}:
            raise ValueError("coverage TTL must be positive")
        required = {"resolver", "rule_version", "status", "provider_journal_id", "work_count", "source_url", "http_status", "response", "query_contract", "completion", "reason"}
        if set(probe) != required or probe["status"] not in STATUSES or probe["completion"] not in {"complete", "partial", "incomplete"}:
            raise ValueError("coverage probe shape is invalid")
        if probe["status"] != "covered" and probe["completion"] == "complete" and probe["status"] not in {"not_covered"}:
            raise ValueError("incomplete coverage cannot be complete")
        authority_hash = self.authority_hash(authority)
        payload = probe["response"]
        response_sha = _sha(payload) if payload is not None else None
        checked_at = _iso(now)
        expires_at = _iso(now + timedelta(days=ttl))
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO coverage_authorities VALUES(?,?,?,?,?)", (
                authority_hash, authority["record_id"], authority["snapshot_sha256"], authority["canonical_title"], _sha(list(authority["issns"])),
            ))
            for issn in authority["issns"]:
                self.conn.execute("INSERT OR IGNORE INTO coverage_authority_issns VALUES(?,?)", (authority_hash, issn))
            if response_sha is not None:
                self.conn.execute("INSERT OR IGNORE INTO coverage_payloads VALUES(?,?)", (response_sha, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
            self.conn.execute("""INSERT INTO coverage_snapshots(authority_hash,resolver,rule_version,status,provider_journal_id,work_count,source_url,http_status,response_sha256,checked_at,expires_at,query_contract,completion,reason,refresh_mode)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                authority_hash, probe["resolver"], probe["rule_version"], probe["status"], probe["provider_journal_id"], probe["work_count"], probe["source_url"], probe["http_status"], response_sha, checked_at, expires_at, probe["query_contract"], probe["completion"], probe["reason"], refresh_mode,
            ))
        return self.latest(authority_hash, probe["resolver"], probe["rule_version"], now=now) or {}

    def latest(self, authority_hash: str, resolver: str, rule_version: str, *, now: datetime | None = None) -> dict | None:
        row = self.conn.execute("""SELECT * FROM coverage_snapshots WHERE authority_hash=? AND resolver=? AND rule_version=? ORDER BY snapshot_id DESC LIMIT 1""", (authority_hash, resolver, rule_version)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["fresh"] = _parse_time(out["expires_at"]) > (now or _now()) and out["status"] != "incomplete"
        return out

    def identity(self) -> dict:
        row = self.conn.execute("SELECT created_at,schema_version FROM coverage_catalog WHERE singleton=1").fetchone()
        value = {"created_at": row["created_at"], "schema_version": row["schema_version"]}
        return {**value, "catalog_sha256": _sha(value)}

    def payload(self, payload_sha256: str | None) -> str | None:
        if payload_sha256 is None:
            return None
        row = self.conn.execute("SELECT payload FROM coverage_payloads WHERE payload_sha256=?", (payload_sha256,)).fetchone()
        return None if row is None else row["payload"]


def coverage_authority(ref: dict) -> dict | None:
    """Build a coverage identity only from the immutable recognized authority."""
    from .journal_authority import assess_local_journal, issns_for_record
    recognized = assess_local_journal(ref)
    if not recognized:
        return None
    issns = issns_for_record(recognized)
    if not issns:
        return None
    return {
        "record_id": recognized["record_id"], "snapshot_sha256": recognized["snapshot_sha256"],
        "canonical_title": recognized["canonical_title"], "issns": tuple(sorted(issns)),
    }


def evaluate(ref: dict, *, force: bool = False, now: datetime | None = None, catalog_path: str | None = None, resolvers: set[str] | None = None) -> list[dict]:
    """Refresh only missing/expired authority observations through registered probes."""
    authority = coverage_authority(ref)
    if authority is None:
        return []
    from . import providers
    now = now or _now()
    catalog = Catalog(catalog_path)
    try:
        authority_hash = catalog.authority_hash(authority)
        observations = []
        for _name, module in providers.journal_coverage_capable().items():
            capability = module.JOURNAL_COVERAGE
            if not providers._module_enabled(
                module, capability_name=capability["resolver"],
            ):
                continue
            if resolvers is not None and capability["resolver"] not in resolvers:
                continue
            existing = catalog.latest(authority_hash, capability["resolver"], capability["rule_version"], now=now)
            if existing is not None and existing["fresh"] and not force:
                observations.append(existing)
                continue
            try:
                probe = module.probe_journal_coverage(authority)
            except Exception as exc:
                probe = providers._incomplete_journal_coverage(module, f"{type(exc).__name__}: {exc}")
            probe = providers._normalize_journal_coverage(module, probe)
            observations.append(catalog.append(authority, probe, now=now, refresh_mode="manual" if force else "auto"))
        return observations
    finally:
        catalog.close()


def refresh(ref: dict, *, now: datetime | None = None, catalog_path: str | None = None, resolvers: set[str] | None = None) -> list[dict]:
    """Production module entry for a manual forced journal-coverage refresh."""
    return evaluate(ref, force=True, now=now, catalog_path=catalog_path, resolvers=resolvers)


def main(argv: list[str] | None = None) -> int:
    """Refresh cited authority journals from one current run database."""
    parser = argparse.ArgumentParser(description="Refresh local resolver journal coverage for a run database.")
    parser.add_argument("--run-db", required=True, help="Path to the run.sqlite database.")
    parser.add_argument("--resolver", action="append", help="Limit refresh to one registered resolver; repeatable.")
    parser.add_argument("--journal-authority-db", required=True, help="Path to the immutable journal authority catalog.")
    parser.add_argument("--catalog-db", required=True, help="Path to the resolver coverage catalog.")
    args = parser.parse_args(argv)
    if not Path(args.run_db).is_file():
        parser.error("run database is unavailable")
    if not Path(args.journal_authority_db).is_file():
        parser.error("journal authority catalog is unavailable")
    os.environ["CALLIMACHUS_JOURNAL_AUTHORITY_DB"] = args.journal_authority_db
    conn = sqlite3.connect(args.run_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT r.ref_id, r.raw_entry, c.coordinate_kind, c.normalized_value, c.raw_value FROM reference_entries r LEFT JOIN cited_bibliographic_coordinates c ON c.ref_id=r.ref_id ORDER BY r.ref_id,c.coordinate_id").fetchall()
    conn.close()
    refs: dict[str, dict] = {}
    for row in rows:
        ref = refs.setdefault(row["ref_id"], {"raw_entry": row["raw_entry"], "cited_coordinates": []})
        if row["coordinate_kind"] is not None:
            ref["cited_coordinates"].append({"kind": row["coordinate_kind"], "normalized_value": row["normalized_value"], "raw_value": row["raw_value"]})
    counts = {"references": len(refs), "eligible": 0, "queried": 0, "covered": 0, "not_covered": 0, "incomplete": 0}
    selected = set(args.resolver) if args.resolver else None
    from . import providers
    available = set(providers.journal_coverage_capable())
    unknown = sorted((selected or set()) - available)
    if unknown:
        parser.error("unknown resolver capability: " + ", ".join(unknown))
    unique: dict[str, dict] = {}
    for ref in refs.values():
        authority = coverage_authority(ref)
        if authority is not None:
            unique.setdefault(json.dumps(authority, default=list, sort_keys=True), ref)
    if not unique:
        parser.error("zero cited journals matched the authority catalog")
    counts["eligible"] = len(unique)
    for ref in unique.values():
        observations = refresh(ref, catalog_path=args.catalog_db, resolvers=selected)
        counts["queried"] += len(observations)
        for observation in observations:
            counts[observation["status"]] += 1
    print("coverage refresh: references {references}; eligible journals {eligible}; queried observations {queried}; covered {covered}; not covered {not_covered}; incomplete {incomplete}".format(**counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def suspicion(observations: list[dict], *, compatible_identity: bool, article_lookups: list[dict]) -> dict:
    """Apply the coverage-only rule; this never changes bibliographic adjudication."""
    if compatible_identity:
        return {"suspicion_level": "none", "reason": "compatible_identity", "evidence": []}
    fresh = [item for item in observations if item.get("fresh")]
    covered = [item for item in fresh if item.get("status") == "covered" and item.get("http_status") == 200 and item.get("completion") == "complete"]
    for item in covered:
        if any(
            lookup.get("resolver") == item.get("resolver")
            and lookup.get("completion") == "complete"
            and lookup.get("match_status") == "no_compatible_article"
            and lookup.get("http_status") == 200
            and lookup.get("scope") in {
                "complete issue inventory", "complete year inventory",
            }
            for lookup in article_lookups
        ):
            return {"suspicion_level": "high", "reason": "fresh_covered_same_resolver_complete_article_miss", "evidence": [item]}
    return {"suspicion_level": "none", "reason": "coverage_insufficient", "evidence": []}


def bibliographic_suspicion(
    ref: dict, *, status: str, attempts: list[dict], alias_assessment: dict | None,
    observations: list[dict], compatible_identity: bool,
    adjudication: dict | None = None,
) -> dict:
    """Return a non-diagnostic suspicion from completed independent misses.

    This deliberately consumes only typed attempt outcomes.  Operational failures
    (including 429 and timeout outcomes represented as ``unresolved``) are not
    misses, and a near alias is never converted into resolver authority.
    """
    none = {"suspicion_level": "none", "conclusion": "insufficient_completed_bibliographic_evidence", "providers": []}
    if status != "unverified" or compatible_identity:
        return none
    adjudication = adjudication or {}
    if (
        adjudication.get("outcome") in {"refuted", "identified", "identified_with_errors"}
        or adjudication.get("identity_status") in {"identified", "identified_with_errors", "ambiguous"}
        or adjudication.get("correction_status") in {"identified", "ambiguous", "not_needed"}
    ):
        return none
    providers = sorted({
        str(search.get("resolver") or "").strip()
        for attempt in attempts if isinstance(attempt, dict)
        for search in [attempt.get("identity_search")]
        if isinstance(search, dict)
        and search.get("completion") == "complete"
        and search.get("outcome") in {"no_compatible_identity", "candidate_incompatible"}
        and str(search.get("resolver") or "").strip()
    })
    if len(providers) < 2:
        return none
    issue_absence = next((
        item for item in adjudication.get("refutations") or ()
        if isinstance(item, dict)
        and item.get("kind") == "absent_from_complete_issue"
        and isinstance(item.get("source"), str) and item["source"].strip()
        and isinstance(item.get("basis"), str) and item["basis"].strip()
    ), None)
    if (
        adjudication.get("outcome") == "checks_incomplete"
        and adjudication.get("identity_status") == "not_identified"
        and adjudication.get("check_status") == "incomplete"
        and adjudication.get("correction_status") == "not_found"
        and issue_absence is not None
    ):
        return {
            "suspicion_level": "elevated",
            "conclusion": "complete_issue_absence_with_incomplete_identity_checks",
            "providers": providers,
        }
    if ref.get("source_kind") != "article_like":
        return none
    from .service import _has_author
    if not ref.get("title") or not ref.get("year") or not _has_author(ref):
        return none
    from .journal_authority import _cited_container
    if not _cited_container(ref):
        return none
    try:
        year = int(str(ref["year"])[:4])
    except (TypeError, ValueError):
        return none
    if year < 2000:
        return none
    assessment_status = (alias_assessment or {}).get("status")
    if assessment_status == "exact":
        closed_coverage = {
            item.get("resolver") for item in observations if
            item.get("fresh") and item.get("status") == "covered"
            and item.get("completion") == "complete" and item.get("http_status") == 200
            and item.get("query_contract") in {
                "complete issue inventory", "complete year inventory",
            }
        }
        if not closed_coverage.intersection(providers):
            return none
        conclusion = "recognized_journal_with_fresh_coverage_and_completed_bibliographic_misses"
    else:
        return none
    return {
        "suspicion_level": "elevated", "conclusion": conclusion,
        "providers": providers,
    }


def bibliographic_review_labels(
    *, reference: object, status: object, attempts: object, adjudication: object,
    coverage: object,
) -> list[dict]:
    """Return informational review labels from persisted typed resolver evidence.

    These labels intentionally do not participate in adjudication, suspicion,
    fabrication risk, or any status decision.  They identify completed search
    evidence that may help an operator choose what to inspect next.
    """
    if not isinstance(adjudication, dict) or not isinstance(reference, dict):
        return []
    labels = []
    if (
        status == "resolved"
        and adjudication.get("outcome") in {"identified", "identified_with_errors"}
        and adjudication.get("identity_status") in {"identified", "identified_with_errors"}
        and isinstance(attempts, list)
    ):
        try:
            from core.resolve import matching as _matching
        except ImportError:  # pragma: no cover - direct execution fallback
            from resolve import matching as _matching  # type: ignore[no-redef]
        cited_authors = _matching._explicit_preyear_surnames(reference)
        crossref_attempt = next((
            attempt for attempt in attempts
            if isinstance(attempt, dict)
            and attempt.get("status") == "resolved"
            and attempt.get("via") == "crossref"
            and attempt.get("enrichment_only") is not True
            and isinstance(attempt.get("matched_authors"), list)
        ), None)
        returned_authors = (
            crossref_attempt.get("matched_authors")
            if isinstance(crossref_attempt, dict) else []
        )
        returned_authors = [
            _matching._fold_author(author)
            for author in returned_authors
            if isinstance(author, str) and _matching._fold_author(author)
        ]
        if (
            len(cited_authors) >= 2
            and len(returned_authors) >= 2
            and any(_matching._author_names_equivalent(
                cited_authors[0], author
            ) for author in returned_authors)
        ):
            missing = [
                cited for cited in cited_authors
                if not any(_matching._author_names_equivalent(cited, returned)
                           for returned in returned_authors)
            ]
            if missing:
                labels.append({
                    "code": "author_list_discrepancy",
                    "provider": "crossref",
                    "missing_cited_authors": missing,
                    "evidence_scope": "returned_author_list_not_completeness_assertion",
                })
    if status != "unverified":
        return labels
    if (
        adjudication.get("outcome") != "checks_incomplete"
        or adjudication.get("identity_status") != "not_identified"
        or adjudication.get("check_status") != "incomplete"
        or adjudication.get("correction_status") not in {
            "not_attempted", "not_found",
        }
    ):
        return []
    authority = coverage.get("authority") if isinstance(coverage, dict) else None
    authority_valid = isinstance(authority, dict) and all(
        isinstance(authority.get(key), str) and authority[key].strip()
        for key in ("record_id", "snapshot_sha256", "canonical_title")
    )
    coordinates = reference.get("cited_coordinates") or []

    def has_coordinate(kind: str) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("kind") == kind
            and isinstance(item.get("normalized_value") or item.get("raw_value"), str)
            and (item.get("normalized_value") or item.get("raw_value")).strip()
            for item in coordinates
        )

    if (
        authority_valid
        and has_coordinate("container")
        and has_coordinate("volume")
        and not any(has_coordinate(kind) for kind in (
            "article_page_range", "elocator", "article_number", "article_locator",
        ))
    ):
        labels.append({
            "code": "incomplete_bibliographic_source",
            "missing_fields": ["article_locator"],
        })

    providers = sorted({
        str(search.get("resolver") or "").strip()
        for attempt in attempts if isinstance(attempt, dict)
        for search in [attempt.get("identity_search")]
        if isinstance(search, dict)
        and search.get("completion") == "complete"
        and search.get("outcome") in {
            "no_compatible_identity", "candidate_incompatible",
        }
        and str(search.get("resolver") or "").strip()
    }) if isinstance(attempts, list) else []
    if len(providers) < 2:
        return labels
    labels.append({
        "code": "not_found_after_completed_searches",
        "providers": providers,
    })
    if not authority_valid:
        return labels
    lookup = next((
        item for item in coverage.get("article_lookups", [])
        if isinstance(item, dict)
        and item.get("scope") == "canonical journal/year/volume/first locator"
        and item.get("completion") == "complete"
        and item.get("match_status") == "no_compatible_article"
        and item.get("http_status") == 200
        and all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in ("resolver", "query_contract", "source_url", "response_sha256", "reason")
        )
    ), None)
    if lookup is not None:
        labels.append({
            "code": "no_compatible_article_at_cited_coordinates",
            "providers": providers,
            "lookup": {
                "resolver": lookup["resolver"],
                "query_contract": lookup["query_contract"],
                "scope": lookup["scope"],
            },
        })
    return labels


def bibliographic_concern(resolve: object) -> dict[str, object] | None:
    """Project the strongest persisted bibliographic concern for one source."""
    if not isinstance(resolve, Mapping):
        return None
    profile = resolve.get("evidence_profile")
    if not isinstance(profile, Mapping):
        profile = {}
    adjudication = profile.get("bibliographic_adjudication")
    if not isinstance(adjudication, Mapping):
        adjudication = {}
    coverage = profile.get("resolver_coverage")
    if not isinstance(coverage, Mapping):
        coverage = {}
    suspicion = profile.get("bibliographic_suspicion")
    if not isinstance(suspicion, Mapping):
        suspicion = {}

    refutations = [
        item for item in adjudication.get("refutations", [])
        if isinstance(item, Mapping)
    ]
    positive_refutation = next((
        item for item in refutations
        if item.get("kind") == "coordinate_occupied_by_other_work"
    ), None)
    tagged = (
        resolve.get("status") == "suspected_fabricated"
        or resolve.get("reference_status_tag") == "suspected_fabricated"
    )
    if adjudication.get("outcome") == "refuted" and refutations:
        refutation = positive_refutation or refutations[0]
        refutation_kind = refutation.get("kind")
        return {
            "level": "reference_refuted",
            "basis": (
                "positive_refutation"
                if refutation_kind == "coordinate_occupied_by_other_work"
                and refutation.get("observed_value")
                else "documented_refutation"
            ),
            "conclusion": adjudication.get("outcome"),
            "resolver": refutation.get("source"),
            "cited_coordinates": refutation.get("cited_value"),
            "observed_work": refutation.get("observed_value"),
            "refutation_kind": refutation_kind,
            "refutation_basis": refutation.get("basis"),
            "journal": None,
            "issns": [],
            "scope": None,
            "providers": [],
        }

    if coverage.get("suspicion_level") == "high":
        observations = [
            item for item in coverage.get("observations", [])
            if isinstance(item, Mapping)
        ]
        lookups = [
            item for item in coverage.get("article_lookups", [])
            if isinstance(item, Mapping)
        ]
        matched_observation = None
        matched_lookup = None
        for observation in observations:
            if not (
                observation.get("fresh") is True
                and observation.get("status") == "covered"
                and observation.get("completion") == "complete"
                and observation.get("http_status") == 200
            ):
                continue
            lookup = next((
                item for item in lookups
                if item.get("resolver") == observation.get("resolver")
                and item.get("completion") == "complete"
                and item.get("match_status") == "no_compatible_article"
                and item.get("http_status") == 200
            ), None)
            if lookup is not None:
                matched_observation, matched_lookup = observation, lookup
                break
        authority = coverage.get("authority")
        if not isinstance(authority, Mapping):
            authority = {}
        return {
            "level": "high_fabrication_suspicion",
            "basis": "same_resolver_complete_absence",
            "conclusion": coverage.get("conclusion"),
            "resolver": (
                matched_observation.get("resolver")
                if matched_observation is not None else None
            ),
            "cited_coordinates": None,
            "observed_work": None,
            "journal": authority.get("canonical_title"),
            "issns": list(authority.get("issns") or []),
            "scope": matched_lookup.get("scope") if matched_lookup is not None else None,
            "providers": [],
        }

    if tagged:
        return {
            "level": "high_fabrication_suspicion",
            "basis": "deterministic_fabrication_tag",
            "conclusion": resolve.get("tag_reason"),
            "resolver": None,
            "cited_coordinates": None,
            "observed_work": None,
            "journal": None,
            "issns": [],
            "scope": None,
            "providers": [],
        }

    elevated_signal = next((
        signal for signal in (coverage, suspicion)
        if signal.get("suspicion_level") == "elevated"
    ), None)
    if elevated_signal is None:
        return None
    authority = coverage.get("authority")
    if not isinstance(authority, Mapping):
        authority = profile.get("journal_authority")
    if not isinstance(authority, Mapping):
        authority = {}
    issue_absence = next((
        item for item in refutations
        if item.get("kind") == "absent_from_complete_issue"
    ), None)
    issue_absence_with_incomplete_checks = (
        elevated_signal.get("conclusion")
        == "complete_issue_absence_with_incomplete_identity_checks"
        and issue_absence is not None
    )
    return {
        "level": "elevated_bibliographic_suspicion",
        "basis": (
            "complete_issue_absence_with_incomplete_identity_checks"
            if issue_absence_with_incomplete_checks
            else "completed_search_misses"
        ),
        "conclusion": elevated_signal.get("conclusion"),
        "resolver": (
            issue_absence.get("source")
            if issue_absence_with_incomplete_checks else None
        ),
        "cited_coordinates": (
            issue_absence.get("cited_value")
            if issue_absence_with_incomplete_checks else None
        ),
        "observed_work": None,
        "refutation_kind": (
            issue_absence.get("kind")
            if issue_absence_with_incomplete_checks else None
        ),
        "refutation_basis": (
            issue_absence.get("basis")
            if issue_absence_with_incomplete_checks else None
        ),
        "journal": authority.get("canonical_title"),
        "issns": list(authority.get("issns") or []),
        "scope": None,
        "providers": list(elevated_signal.get("providers") or []),
    }
