# core/infra/db/ingest_setting_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed relational persistence for ingestion and frozen-fork settings."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

RELATIONAL_INGEST_SETTING_KEYS = frozenset(
    {"user_source_ingest", "ingest_review", "frozen_fetch_provenance"}
)

# Keep schema vocabulary dependency-free: importing the Resolve package from the
# DB schema bootstrap creates a core.infra.db -> core.resolve -> core.infra.db cycle.
_USER_SOURCE_TIERS = ("fulltext", "abstract")
_USER_SOURCE_OUTCOMES = (
    "accepted_abstract",
    "accepted_fulltext",
    "duplicate",
    "identity_mismatch",
    "abstract_section_missing",
    "challenge_or_login_page",
    "insufficient_identity",
    "needs_manual_confirmation",
    "unreadable",
)
INGEST_SETTING_TABLES = frozenset(
    {
        "user_source_ingest_setting",
        "user_source_ingest_item",
        "ingest_review_setting",
        "ingest_review_item",
        "frozen_fetch_provenance_setting",
        "frozen_fetch_source_inventory",
        "frozen_fetch_copied_asset",
    }
)
INGEST_SETTING_TRIGGERS = frozenset(
    {
        "frozen_fetch_provenance_no_update",
        "frozen_fetch_provenance_no_delete",
        "frozen_fetch_inventory_insert_guard",
        "frozen_fetch_inventory_no_update",
        "frozen_fetch_inventory_no_delete",
        "frozen_fetch_asset_insert_guard",
        "frozen_fetch_asset_no_update",
        "frozen_fetch_asset_no_delete",
    }
)

_AUDIT_VARIANTS = (
    "accepted",
    "parser",
    "scored",
    "scored_signal",
    "tie",
    "unreadable_file",
    "unreadable_directory",
)
_REVIEW_VARIANTS = _AUDIT_VARIANTS + (
    "manual",
    "manual_tie",
    "mapped",
    "review_identity",
    "review_unassigned",
    "unreadable",
)
_INVENTORY_TIERS = ("fulltext", "abstract", "web")
_SHA_CHECK = "length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


INGEST_SETTING_DDL = f"""
CREATE TABLE IF NOT EXISTS user_source_ingest_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  fulltext_count INTEGER NOT NULL CHECK(typeof(fulltext_count)='integer' AND fulltext_count>=0),
  abstract_count INTEGER NOT NULL CHECK(typeof(abstract_count)='integer' AND abstract_count>=0)
);
CREATE TABLE IF NOT EXISTS user_source_ingest_item (
  tier TEXT NOT NULL CHECK(tier IN {_USER_SOURCE_TIERS!r}),
  item_order INTEGER NOT NULL CHECK(typeof(item_order)='integer' AND item_order>=0),
  variant TEXT NOT NULL CHECK(variant IN {_AUDIT_VARIANTS!r}),
  outcome TEXT NOT NULL CHECK(outcome IN {_USER_SOURCE_OUTCOMES!r}),
  file TEXT,
  ref_id TEXT,
  ref_number INTEGER,
  signal TEXT,
  score REAL,
  reason TEXT,
  error TEXT,
  stored_as TEXT,
  provided_as TEXT,
  identity_status TEXT,
  identity_note TEXT,
  PRIMARY KEY(tier,item_order)
);
CREATE TABLE IF NOT EXISTS ingest_review_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  review_kind TEXT NOT NULL CHECK(review_kind IN('abstract','fulltext')),
  auto_threshold REAL NOT NULL,
  result_count INTEGER NOT NULL CHECK(typeof(result_count)='integer' AND result_count>=0),
  mapped_count INTEGER NOT NULL CHECK(typeof(mapped_count)='integer' AND mapped_count>=0),
  review_count INTEGER NOT NULL CHECK(typeof(review_count)='integer' AND review_count>=0),
  unreadable_count INTEGER NOT NULL CHECK(typeof(unreadable_count)='integer' AND unreadable_count>=0)
);
CREATE TABLE IF NOT EXISTS ingest_review_item (
  review_kind TEXT NOT NULL CHECK(review_kind IN('abstract','fulltext')),
  item_kind TEXT NOT NULL CHECK(item_kind IN('result','mapped','review','unreadable')),
  item_order INTEGER NOT NULL CHECK(typeof(item_order)='integer' AND item_order>=0),
  variant TEXT NOT NULL CHECK(variant IN {_REVIEW_VARIANTS!r}),
  outcome TEXT,
  file TEXT,
  ref_id TEXT,
  ref_number INTEGER,
  signal TEXT,
  score REAL,
  best_score REAL,
  reason TEXT,
  error TEXT,
  stored_as TEXT,
  provided_as TEXT,
  library_as TEXT,
  note TEXT,
  best_ref_number INTEGER,
  best_ref_id TEXT,
  kept_as TEXT,
  next TEXT,
  PRIMARY KEY(review_kind,item_kind,item_order)
);
CREATE TABLE IF NOT EXISTS frozen_fetch_provenance_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  baseline_run_id TEXT NOT NULL,
  baseline_run_dir TEXT NOT NULL,
  baseline_code_revision TEXT,
  source_inventory_sha256 TEXT NOT NULL CHECK({_SHA_CHECK.format(column='source_inventory_sha256')}),
  baseline_verified_clean INTEGER NOT NULL CHECK(baseline_verified_clean IN(0,1)),
  baseline_identity_kind TEXT NOT NULL CHECK(baseline_identity_kind IN('revision','snapshot','snapshot_error')),
  baseline_code_dirty INTEGER,
  baseline_code_diff_sha256 TEXT,
  baseline_code_snapshot_id TEXT,
  baseline_code_snapshot_error TEXT,
  fork_code_revision TEXT,
  fork_identity_kind TEXT NOT NULL CHECK(fork_identity_kind IN('revision','snapshot','snapshot_error')),
  fork_code_dirty INTEGER,
  fork_code_diff_sha256 TEXT,
  fork_code_snapshot_id TEXT,
  fork_code_snapshot_error TEXT,
  inventory_count INTEGER NOT NULL CHECK(typeof(inventory_count)='integer' AND inventory_count>=0),
  asset_count INTEGER NOT NULL CHECK(typeof(asset_count)='integer' AND asset_count>=0)
);
CREATE TABLE IF NOT EXISTS frozen_fetch_source_inventory (
  item_order INTEGER PRIMARY KEY CHECK(typeof(item_order)='integer' AND item_order>=0),
  ref_id TEXT NOT NULL,
  tier TEXT NOT NULL CHECK(tier IN {_INVENTORY_TIERS!r}),
  origin TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  source_ref TEXT,
  sha256 TEXT NOT NULL CHECK({_SHA_CHECK.format(column='sha256')}),
  char_count INTEGER NOT NULL CHECK(typeof(char_count)='integer' AND char_count>=0)
);
CREATE TABLE IF NOT EXISTS frozen_fetch_copied_asset (
  item_order INTEGER PRIMARY KEY CHECK(typeof(item_order)='integer' AND item_order>=0),
  asset_path TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS frozen_fetch_provenance_no_update
BEFORE UPDATE ON frozen_fetch_provenance_setting
BEGIN SELECT RAISE(ABORT,'frozen fetch provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_provenance_no_delete
BEFORE DELETE ON frozen_fetch_provenance_setting
BEGIN SELECT RAISE(ABORT,'frozen fetch provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_inventory_insert_guard
BEFORE INSERT ON frozen_fetch_source_inventory
WHEN EXISTS(SELECT 1 FROM frozen_fetch_provenance_setting WHERE singleton=1)
BEGIN SELECT RAISE(ABORT,'frozen fetch inventory is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_inventory_no_update
BEFORE UPDATE ON frozen_fetch_source_inventory
BEGIN SELECT RAISE(ABORT,'frozen fetch inventory is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_inventory_no_delete
BEFORE DELETE ON frozen_fetch_source_inventory
BEGIN SELECT RAISE(ABORT,'frozen fetch inventory is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_asset_insert_guard
BEFORE INSERT ON frozen_fetch_copied_asset
WHEN EXISTS(SELECT 1 FROM frozen_fetch_provenance_setting WHERE singleton=1)
BEGIN SELECT RAISE(ABORT,'frozen fetch assets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_asset_no_update
BEFORE UPDATE ON frozen_fetch_copied_asset
BEGIN SELECT RAISE(ABORT,'frozen fetch assets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_fetch_asset_no_delete
BEFORE DELETE ON frozen_fetch_copied_asset
BEGIN SELECT RAISE(ABORT,'frozen fetch assets are immutable'); END;
"""


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} has invalid fields")
    return value


def _text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be nonempty NUL-free text")
    return value


def _integer(value: Any, label: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise ValueError(f"{label} must be integer")
    return value


def _number(value: Any, label: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{label} must be finite float")
    return value


def _sha(value: Any, label: str) -> str:
    result = _text(value, label)
    assert result is not None
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return result


_ACCEPTED_FIELDS = {
    "outcome",
    "file",
    "ref_id",
    "ref_number",
    "stored_as",
    "provided_as",
    "signal",
    "score",
    "identity_status",
    "identity_note",
}
_PARSER_FIELDS = {"outcome", "file", "ref_id", "ref_number"}
_SCORED_FIELDS = _PARSER_FIELDS | {"score"}
_SCORED_SIGNAL_FIELDS = _SCORED_FIELDS | {"signal"}
_TIE_FIELDS = _SCORED_FIELDS | {"reason"}
_UNREADABLE_FILE_FIELDS = {"outcome", "file", "error"}
_UNREADABLE_DIRECTORY_FIELDS = {"outcome", "error"}
_AUDIT_FIELDS_BY_VARIANT = {
    "accepted": _ACCEPTED_FIELDS,
    "parser": _PARSER_FIELDS,
    "scored": _SCORED_FIELDS,
    "scored_signal": _SCORED_SIGNAL_FIELDS,
    "tie": _TIE_FIELDS,
    "unreadable_file": _UNREADABLE_FILE_FIELDS,
    "unreadable_directory": _UNREADABLE_DIRECTORY_FIELDS,
}


def _audit_variant(item: dict[str, Any]) -> str:
    fields = set(item)
    outcome = item.get("outcome")
    if outcome in {"accepted_fulltext", "accepted_abstract"} and fields == _ACCEPTED_FIELDS:
        return "accepted"
    if (
        outcome in {"abstract_section_missing", "challenge_or_login_page", "unreadable"}
        and fields == _PARSER_FIELDS
    ):
        return "parser"
    if outcome == "unreadable" and fields == _UNREADABLE_FILE_FIELDS:
        return "unreadable_file"
    if outcome == "unreadable" and fields == _UNREADABLE_DIRECTORY_FIELDS:
        return "unreadable_directory"
    if outcome == "needs_manual_confirmation" and fields == _TIE_FIELDS:
        return "tie"
    scored_outcomes = {
        "identity_mismatch",
        "insufficient_identity",
        "needs_manual_confirmation",
    }
    if outcome in scored_outcomes and fields == _SCORED_FIELDS:
        return "scored"
    if outcome in scored_outcomes | {"duplicate"} and fields == _SCORED_SIGNAL_FIELDS:
        return "scored_signal"
    raise ValueError("user source audit item has invalid variant")


def _normalize_audit_item(item: Any) -> dict[str, Any]:
    if type(item) is not dict or item.get("outcome") not in _USER_SOURCE_OUTCOMES:
        raise ValueError("invalid user source audit item")
    variant = _audit_variant(item)
    for name in set(item) & {
        "file",
        "signal",
        "error",
        "stored_as",
        "provided_as",
        "identity_status",
        "identity_note",
    }:
        _text(item[name], name)
    if "ref_id" in item:
        _text(item["ref_id"], "ref_id", nullable=True)
    if "ref_number" in item:
        _integer(item["ref_number"], "ref_number", nullable=True)
    if "score" in item:
        _number(item["score"], "score")
    if variant == "tie" and item["reason"] != "top_score_tie":
        raise ValueError("invalid tie reason")
    return dict(item)


def normalize_user_source_ingest(value: Any) -> dict[str, list[dict[str, Any]]]:
    root = _exact(value, {"fulltext", "abstract"}, "user_source_ingest")
    result: dict[str, list[dict[str, Any]]] = {}
    for tier in _USER_SOURCE_TIERS:
        if type(root[tier]) is not list:
            raise ValueError("user source audit tier must be list")
        result[tier] = [_normalize_audit_item(item) for item in root[tier]]
        for item in result[tier]:
            if (
                item["outcome"].startswith("accepted_")
                and item["outcome"] != "accepted_" + tier
            ):
                raise ValueError("accepted outcome conflicts with tier")
    return result


_MAPPED_FIELDS = {
    "file",
    "ref_number",
    "ref_id",
    "signal",
    "score",
    "stored_as",
    "provided_as",
    "library_as",
}
_REVIEW_IDENTITY_FIELDS = {
    "file",
    "reason",
    "ref_id",
    "ref_number",
    "signal",
    "score",
    "note",
}
_REVIEW_UNASSIGNED_FIELDS = {
    "file",
    "best_ref_number",
    "best_ref_id",
    "signal",
    "score",
    "note",
}
_REVIEW_UNREADABLE_FIELDS = {"file", "kept_as", "error", "next"}
_MANUAL_FIELDS = {"outcome", "file", "ref_id", "ref_number", "best_score"}


def _normalize_review_item(item: Any, kind: str) -> dict[str, Any]:
    if type(item) is not dict:
        raise ValueError("invalid ingest review item")
    if kind == "result":
        if "best_score" not in item:
            return _normalize_audit_item(item)
        allowed = _MANUAL_FIELDS | ({"reason"} if "reason" in item else set())
        if set(item) != allowed or item["outcome"] != "needs_manual_confirmation":
            raise ValueError("invalid abstract manual item")
        _text(item["file"], "file")
        _text(item["ref_id"], "ref_id", nullable=True)
        _integer(item["ref_number"], "ref_number", nullable=True)
        _number(item["best_score"], "best_score")
        if "reason" in item and item["reason"] != "top_score_tie":
            raise ValueError("invalid tie reason")
        return dict(item)
    if kind == "mapped":
        _exact(item, _MAPPED_FIELDS, "mapped ingest review item")
        for name in ("file", "ref_id", "stored_as"):
            _text(item[name], name)
        _text(item["signal"], "signal", nullable=True)
        _text(item["provided_as"], "provided_as", nullable=True)
        _text(item["library_as"], "library_as", nullable=True)
        _integer(item["ref_number"], "ref_number")
        _number(item["score"], "score")
        return dict(item)
    if kind == "review":
        fields = set(item)
        if fields == _REVIEW_IDENTITY_FIELDS:
            for name in ("file", "reason", "note"):
                _text(item[name], name)
            _text(item["signal"], "signal", nullable=True)
            _text(item["ref_id"], "ref_id", nullable=True)
            _integer(item["ref_number"], "ref_number", nullable=True)
        elif fields == _REVIEW_UNASSIGNED_FIELDS:
            for name in ("file", "note"):
                _text(item[name], name)
            _text(item["signal"], "signal", nullable=True)
            _text(item["best_ref_id"], "best_ref_id", nullable=True)
            _integer(item["best_ref_number"], "best_ref_number", nullable=True)
        else:
            raise ValueError("invalid fulltext review fields")
        _number(item["score"], "score")
        return dict(item)
    if kind == "unreadable":
        _exact(item, _REVIEW_UNREADABLE_FIELDS, "unreadable ingest review item")
        for name in _REVIEW_UNREADABLE_FIELDS:
            _text(item[name], name)
        return dict(item)
    raise ValueError("invalid ingest review item kind")


def _review_variant(item: dict[str, Any], kind: str) -> str:
    if kind == "result":
        if "best_score" in item:
            return "manual_tie" if "reason" in item else "manual"
        return _audit_variant(item)
    if kind == "mapped":
        return "mapped"
    if kind == "unreadable":
        return "unreadable"
    return "review_identity" if "reason" in item else "review_unassigned"


def normalize_ingest_review(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("ingest_review must be object")
    if value.get("tier") == "abstract":
        _exact(value, {"results", "tier", "auto_threshold"}, "abstract ingest_review")
        _number(value["auto_threshold"], "auto_threshold")
        if type(value["results"]) is not list:
            raise ValueError("abstract results must be list")
        return {
            "results": [_normalize_review_item(item, "result") for item in value["results"]],
            "tier": "abstract",
            "auto_threshold": value["auto_threshold"],
        }
    _exact(
        value,
        {"mapped", "review", "unreadable", "auto_threshold"},
        "fulltext ingest_review",
    )
    _number(value["auto_threshold"], "auto_threshold")
    result = {"auto_threshold": value["auto_threshold"]}
    for key in ("mapped", "review", "unreadable"):
        if type(value[key]) is not list:
            raise ValueError(f"{key} must be list")
        result[key] = [_normalize_review_item(item, key) for item in value[key]]
    return {
        "mapped": result["mapped"],
        "review": result["review"],
        "unreadable": result["unreadable"],
        "auto_threshold": result["auto_threshold"],
    }


def _identity(value: dict[str, Any], prefix: str) -> dict[str, Any]:
    revision_key = prefix + "code_revision"
    if revision_key not in value:
        raise ValueError(revision_key + " required")
    revision = _text(value[revision_key], revision_key, nullable=True)
    snapshot_fields = {
        prefix + "code_dirty",
        prefix + "code_diff_sha256",
        prefix + "code_snapshot_id",
    }
    error_fields = snapshot_fields | {prefix + "code_snapshot_error"}
    present = set(value) & error_fields
    if not present:
        return {"kind": "revision", "code_revision": revision}
    if present == snapshot_fields:
        if type(value[prefix + "code_dirty"]) is not bool:
            raise ValueError(prefix + "code_dirty must be bool")
        return {
            "kind": "snapshot",
            "code_revision": revision,
            "code_dirty": value[prefix + "code_dirty"],
            "code_diff_sha256": _sha(
                value[prefix + "code_diff_sha256"], prefix + "code_diff_sha256"
            ),
            "code_snapshot_id": _sha(
                value[prefix + "code_snapshot_id"], prefix + "code_snapshot_id"
            ),
        }
    if present == error_fields:
        if any(value[name] is not None for name in snapshot_fields):
            raise ValueError("invalid code snapshot error union")
        return {
            "kind": "snapshot_error",
            "code_revision": revision,
            "code_snapshot_error": _text(
                value[prefix + "code_snapshot_error"], prefix + "code_snapshot_error"
            ),
        }
    raise ValueError("invalid code identity union")


def normalize_frozen_fetch_provenance(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("frozen provenance must be object")
    base = {
        "baseline_run_id",
        "baseline_run_dir",
        "baseline_code_revision",
        "source_inventory",
        "source_inventory_sha256",
        "copied_assets",
        "baseline_verified_clean",
        "fork_code_revision",
    }
    optional = {
        "baseline_code_dirty",
        "baseline_code_diff_sha256",
        "baseline_code_snapshot_id",
        "baseline_code_snapshot_error",
        "fork_code_dirty",
        "fork_code_diff_sha256",
        "fork_code_snapshot_id",
        "fork_code_snapshot_error",
    }
    if set(value) - base - optional or not base <= set(value):
        raise ValueError("frozen provenance has invalid fields")
    _text(value["baseline_run_id"], "baseline_run_id")
    _text(value["baseline_run_dir"], "baseline_run_dir")
    _sha(value["source_inventory_sha256"], "source_inventory_sha256")
    if type(value["baseline_verified_clean"]) is not bool:
        raise ValueError("baseline_verified_clean must be bool")
    if type(value["source_inventory"]) is not list:
        raise ValueError("source_inventory must be list")
    if type(value["copied_assets"]) is not list:
        raise ValueError("copied_assets must be list")

    inventory: list[dict[str, Any]] = []
    inventory_fields = {
        "ref_id",
        "tier",
        "origin",
        "stored_path",
        "source_ref",
        "sha256",
        "char_count",
    }
    for item in value["source_inventory"]:
        _exact(item, inventory_fields, "source inventory item")
        _text(item["ref_id"], "ref_id")
        if item["tier"] not in _INVENTORY_TIERS:
            raise ValueError("invalid inventory tier")
        _text(item["origin"], "origin")
        _text(item["stored_path"], "stored_path")
        _text(item["source_ref"], "source_ref", nullable=True)
        _sha(item["sha256"], "sha256")
        char_count = _integer(item["char_count"], "char_count")
        assert char_count is not None
        if char_count < 0:
            raise ValueError("char_count must be nonnegative")
        inventory.append(dict(item))
    inventory_key = lambda item: (
        item["ref_id"],
        item["tier"],
        item["stored_path"],
        item["sha256"],
    )
    if inventory != sorted(inventory, key=inventory_key):
        raise ValueError("source inventory must be sorted")
    if len({inventory_key(item) for item in inventory}) != len(inventory):
        raise ValueError("source inventory must be unique")
    digest = hashlib.sha256(
        json.dumps(
            inventory, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if digest != value["source_inventory_sha256"]:
        raise ValueError("source inventory digest mismatch")

    assets = [_text(asset, "copied asset") for asset in value["copied_assets"]]
    if assets != sorted(assets) or len(set(assets)) != len(assets):
        raise ValueError("copied assets must be sorted and unique")
    _identity(value, "baseline_")
    _identity(value, "fork_")
    return dict(value)


_AUDIT_COLUMNS = (
    "outcome",
    "file",
    "ref_id",
    "ref_number",
    "signal",
    "score",
    "reason",
    "error",
    "stored_as",
    "provided_as",
    "identity_status",
    "identity_note",
)
_REVIEW_COLUMNS = (
    "outcome",
    "file",
    "ref_id",
    "ref_number",
    "signal",
    "score",
    "best_score",
    "reason",
    "error",
    "stored_as",
    "provided_as",
    "library_as",
    "note",
    "best_ref_number",
    "best_ref_id",
    "kept_as",
    "next",
)


def _row_item(item: dict[str, Any], names: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(item.get(name) for name in names)


def _restore_fields(
    raw: dict[str, Any], fields: set[str], *, label: str
) -> dict[str, Any]:
    unexpected = [name for name, value in raw.items() if name not in fields and value is not None]
    if unexpected:
        raise RuntimeError(f"{label} has unexpected stored fields")
    return {name: raw[name] for name in fields}


def replace_user_source_ingest(conn, value: Any) -> None:
    normalized = normalize_user_source_ingest(value)
    conn.execute(
        """
        INSERT INTO user_source_ingest_setting(singleton,fulltext_count,abstract_count)
        VALUES(1,?,?)
        ON CONFLICT(singleton) DO UPDATE SET
          fulltext_count=excluded.fulltext_count,
          abstract_count=excluded.abstract_count
        """,
        (len(normalized["fulltext"]), len(normalized["abstract"])),
    )
    conn.execute("DELETE FROM user_source_ingest_item")
    sql = (
        "INSERT INTO user_source_ingest_item(tier,item_order,variant,"
        + ",".join(_AUDIT_COLUMNS)
        + ") VALUES(?,?,?,"
        + ",".join("?" for _ in _AUDIT_COLUMNS)
        + ")"
    )
    for tier in _USER_SOURCE_TIERS:
        conn.executemany(
            sql,
            [
                (
                    tier,
                    item_order,
                    _audit_variant(item),
                    *_row_item(item, _AUDIT_COLUMNS),
                )
                for item_order, item in enumerate(normalized[tier])
            ],
        )


def read_user_source_ingest(conn):
    header = conn.execute(
        """
        SELECT fulltext_count,abstract_count
        FROM user_source_ingest_setting WHERE singleton=1
        """
    ).fetchone()
    if header is None:
        if conn.execute("SELECT 1 FROM user_source_ingest_item LIMIT 1").fetchone():
            raise RuntimeError("user source ingest storage is inconsistent")
        return None
    result = {tier: [] for tier in _USER_SOURCE_TIERS}
    orders = {tier: [] for tier in _USER_SOURCE_TIERS}
    rows = conn.execute(
        "SELECT tier,item_order,variant,"
        + ",".join(_AUDIT_COLUMNS)
        + " FROM user_source_ingest_item ORDER BY tier,item_order"
    )
    for row in rows:
        tier, item_order, variant = row[:3]
        if tier not in result or variant not in _AUDIT_FIELDS_BY_VARIANT:
            raise RuntimeError("user source ingest storage is inconsistent")
        raw = dict(zip(_AUDIT_COLUMNS, row[3:]))
        item = _restore_fields(
            raw,
            _AUDIT_FIELDS_BY_VARIANT[variant],
            label="user source ingest item",
        )
        if _audit_variant(item) != variant:
            raise RuntimeError("user source ingest variant is inconsistent")
        result[tier].append(item)
        orders[tier].append(item_order)
    if any(
        orders[tier] != list(range(len(orders[tier])))
        for tier in _USER_SOURCE_TIERS
    ):
        raise RuntimeError("user source ingest ordering is inconsistent")
    if (len(result["fulltext"]), len(result["abstract"])) != tuple(header):
        raise RuntimeError("user source ingest storage is inconsistent")
    return normalize_user_source_ingest(result)


def replace_ingest_review(conn, value: Any) -> None:
    normalized = normalize_ingest_review(value)
    abstract = normalized.get("tier") == "abstract"
    groups = (
        {"result": normalized["results"]}
        if abstract
        else {key: normalized[key] for key in ("mapped", "review", "unreadable")}
    )
    review_kind = "abstract" if abstract else "fulltext"
    conn.execute(
        """
        INSERT INTO ingest_review_setting(
          singleton,review_kind,auto_threshold,result_count,
          mapped_count,review_count,unreadable_count
        ) VALUES(1,?,?,?,?,?,?)
        ON CONFLICT(singleton) DO UPDATE SET
          review_kind=excluded.review_kind,
          auto_threshold=excluded.auto_threshold,
          result_count=excluded.result_count,
          mapped_count=excluded.mapped_count,
          review_count=excluded.review_count,
          unreadable_count=excluded.unreadable_count
        """,
        (
            review_kind,
            normalized["auto_threshold"],
            len(groups.get("result", [])),
            len(groups.get("mapped", [])),
            len(groups.get("review", [])),
            len(groups.get("unreadable", [])),
        ),
    )
    conn.execute("DELETE FROM ingest_review_item")
    sql = (
        "INSERT INTO ingest_review_item(review_kind,item_kind,item_order,variant,"
        + ",".join(_REVIEW_COLUMNS)
        + ") VALUES(?,?,?,? ,"
        + ",".join("?" for _ in _REVIEW_COLUMNS)
        + ")"
    )
    for item_kind, items in groups.items():
        conn.executemany(
            sql,
            [
                (
                    review_kind,
                    item_kind,
                    item_order,
                    _review_variant(item, item_kind),
                    *_row_item(item, _REVIEW_COLUMNS),
                )
                for item_order, item in enumerate(items)
            ],
        )


def _review_fields(item_kind: str, variant: str) -> set[str]:
    if item_kind == "result":
        if variant == "manual":
            return _MANUAL_FIELDS
        if variant == "manual_tie":
            return _MANUAL_FIELDS | {"reason"}
        if variant in _AUDIT_FIELDS_BY_VARIANT:
            return _AUDIT_FIELDS_BY_VARIANT[variant]
    elif item_kind == "mapped" and variant == "mapped":
        return _MAPPED_FIELDS
    elif item_kind == "review" and variant == "review_identity":
        return _REVIEW_IDENTITY_FIELDS
    elif item_kind == "review" and variant == "review_unassigned":
        return _REVIEW_UNASSIGNED_FIELDS
    elif item_kind == "unreadable" and variant == "unreadable":
        return _REVIEW_UNREADABLE_FIELDS
    raise RuntimeError("ingest review variant is inconsistent")


def read_ingest_review(conn):
    header = conn.execute(
        """
        SELECT review_kind,auto_threshold,result_count,
               mapped_count,review_count,unreadable_count
        FROM ingest_review_setting WHERE singleton=1
        """
    ).fetchone()
    if header is None:
        if conn.execute("SELECT 1 FROM ingest_review_item LIMIT 1").fetchone():
            raise RuntimeError("ingest review storage is inconsistent")
        return None
    review_kind = header[0]
    groups: dict[str, list[dict[str, Any]]] = {}
    orders: dict[str, list[int]] = {}
    rows = conn.execute(
        "SELECT review_kind,item_kind,item_order,variant,"
        + ",".join(_REVIEW_COLUMNS)
        + " FROM ingest_review_item ORDER BY review_kind,item_kind,item_order"
    )
    for row in rows:
        stored_kind, item_kind, item_order, variant = row[:4]
        if stored_kind != review_kind:
            raise RuntimeError("ingest review kind is inconsistent")
        fields = _review_fields(item_kind, variant)
        raw = dict(zip(_REVIEW_COLUMNS, row[4:]))
        item = _restore_fields(raw, fields, label="ingest review item")
        if _review_variant(item, item_kind) != variant:
            raise RuntimeError("ingest review variant is inconsistent")
        groups.setdefault(item_kind, []).append(item)
        orders.setdefault(item_kind, []).append(item_order)
    if any(order != list(range(len(order))) for order in orders.values()):
        raise RuntimeError("ingest review ordering is inconsistent")
    actual_counts = (
        len(groups.get("result", [])),
        len(groups.get("mapped", [])),
        len(groups.get("review", [])),
        len(groups.get("unreadable", [])),
    )
    if actual_counts != tuple(header[2:]):
        raise RuntimeError("ingest review storage is inconsistent")
    if review_kind == "abstract":
        value = {
            "results": groups.get("result", []),
            "tier": "abstract",
            "auto_threshold": header[1],
        }
    elif review_kind == "fulltext":
        value = {
            key: groups.get(key, []) for key in ("mapped", "review", "unreadable")
        }
        value["auto_threshold"] = header[1]
    else:
        raise RuntimeError("ingest review kind is inconsistent")
    return normalize_ingest_review(value)


def write_frozen_fetch_provenance(conn, value: Any) -> None:
    normalized = normalize_frozen_fetch_provenance(value)
    existing = read_frozen_fetch_provenance(conn)
    if existing is not None:
        if existing != normalized:
            raise ValueError("frozen fetch provenance is write-once")
        return
    baseline = _identity(normalized, "baseline_")
    fork = _identity(normalized, "fork_")
    conn.executemany(
        "INSERT INTO frozen_fetch_source_inventory VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                item_order,
                item["ref_id"],
                item["tier"],
                item["origin"],
                item["stored_path"],
                item["source_ref"],
                item["sha256"],
                item["char_count"],
            )
            for item_order, item in enumerate(normalized["source_inventory"])
        ],
    )
    conn.executemany(
        "INSERT INTO frozen_fetch_copied_asset VALUES(?,?)",
        enumerate(normalized["copied_assets"]),
    )
    conn.execute(
        "INSERT INTO frozen_fetch_provenance_setting VALUES(1,"
        + ",".join("?" for _ in range(18))
        + ")",
        (
            normalized["baseline_run_id"],
            normalized["baseline_run_dir"],
            normalized["baseline_code_revision"],
            normalized["source_inventory_sha256"],
            int(normalized["baseline_verified_clean"]),
            baseline["kind"],
            baseline.get("code_dirty"),
            baseline.get("code_diff_sha256"),
            baseline.get("code_snapshot_id"),
            baseline.get("code_snapshot_error"),
            fork.get("code_revision"),
            fork["kind"],
            fork.get("code_dirty"),
            fork.get("code_diff_sha256"),
            fork.get("code_snapshot_id"),
            fork.get("code_snapshot_error"),
            len(normalized["source_inventory"]),
            len(normalized["copied_assets"]),
        ),
    )


def _stored_identity(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    kind = values[prefix + "identity_kind"]
    revision = values[prefix + "code_revision"]
    dirty = values[prefix + "code_dirty"]
    diff_sha256 = values[prefix + "code_diff_sha256"]
    snapshot_id = values[prefix + "code_snapshot_id"]
    snapshot_error = values[prefix + "code_snapshot_error"]
    result = {prefix + "code_revision": revision}
    if kind == "revision":
        if any(item is not None for item in (dirty, diff_sha256, snapshot_id, snapshot_error)):
            raise RuntimeError("frozen provenance identity storage is inconsistent")
    elif kind == "snapshot":
        if dirty not in (0, 1) or snapshot_error is not None:
            raise RuntimeError("frozen provenance identity storage is inconsistent")
        result.update(
            {
                prefix + "code_dirty": bool(dirty),
                prefix + "code_diff_sha256": diff_sha256,
                prefix + "code_snapshot_id": snapshot_id,
            }
        )
    elif kind == "snapshot_error":
        if any(item is not None for item in (dirty, diff_sha256, snapshot_id)):
            raise RuntimeError("frozen provenance identity storage is inconsistent")
        result.update(
            {
                prefix + "code_dirty": None,
                prefix + "code_diff_sha256": None,
                prefix + "code_snapshot_id": None,
                prefix + "code_snapshot_error": snapshot_error,
            }
        )
    else:
        raise RuntimeError("frozen provenance identity storage is inconsistent")
    return result


def read_frozen_fetch_provenance(conn):
    row = conn.execute(
        "SELECT * FROM frozen_fetch_provenance_setting WHERE singleton=1"
    ).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM frozen_fetch_source_inventory LIMIT 1").fetchone():
            raise RuntimeError("frozen provenance storage is inconsistent")
        if conn.execute("SELECT 1 FROM frozen_fetch_copied_asset LIMIT 1").fetchone():
            raise RuntimeError("frozen provenance storage is inconsistent")
        return None
    names = (
        "baseline_run_id",
        "baseline_run_dir",
        "baseline_code_revision",
        "source_inventory_sha256",
        "baseline_verified_clean",
        "baseline_identity_kind",
        "baseline_code_dirty",
        "baseline_code_diff_sha256",
        "baseline_code_snapshot_id",
        "baseline_code_snapshot_error",
        "fork_code_revision",
        "fork_identity_kind",
        "fork_code_dirty",
        "fork_code_diff_sha256",
        "fork_code_snapshot_id",
        "fork_code_snapshot_error",
        "inventory_count",
        "asset_count",
    )
    values = dict(zip(names, row[1:]))
    if values["baseline_verified_clean"] not in (0, 1):
        raise RuntimeError("frozen provenance storage is inconsistent")
    result = {
        "baseline_run_id": values["baseline_run_id"],
        "baseline_run_dir": values["baseline_run_dir"],
        "source_inventory_sha256": values["source_inventory_sha256"],
        "baseline_verified_clean": bool(values["baseline_verified_clean"]),
    }
    result.update(_stored_identity("baseline_", values))
    result.update(_stored_identity("fork_", values))

    inventory_columns = (
        "ref_id",
        "tier",
        "origin",
        "stored_path",
        "source_ref",
        "sha256",
        "char_count",
    )
    inventory_rows = list(
        conn.execute(
            "SELECT item_order,"
            + ",".join(inventory_columns)
            + " FROM frozen_fetch_source_inventory ORDER BY item_order"
        )
    )
    asset_rows = list(
        conn.execute(
            "SELECT item_order,asset_path FROM frozen_fetch_copied_asset ORDER BY item_order"
        )
    )
    if [item[0] for item in inventory_rows] != list(range(len(inventory_rows))):
        raise RuntimeError("frozen provenance inventory ordering is inconsistent")
    if [item[0] for item in asset_rows] != list(range(len(asset_rows))):
        raise RuntimeError("frozen provenance asset ordering is inconsistent")
    result["source_inventory"] = [
        dict(zip(inventory_columns, item[1:])) for item in inventory_rows
    ]
    result["copied_assets"] = [item[1] for item in asset_rows]
    if (
        len(result["source_inventory"]),
        len(result["copied_assets"]),
    ) != (values["inventory_count"], values["asset_count"]):
        raise RuntimeError("frozen provenance storage is inconsistent")
    return normalize_frozen_fetch_provenance(result)
