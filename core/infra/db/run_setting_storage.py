# core/infra/db/run_setting_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed typed persistence for current run settings."""
from __future__ import annotations

import json
from typing import Any

SCALAR_KEYS = {
    "mailto": "text", "max_retries": "int", "fetch_workers": "int_null",
    "no_fetch": "bool", "references_only": "bool",
    "verify_backends": "text", "autonomous": "bool", "ocr_lang": "text",
    "fetch_paused": "bool",
    "auto_fetch_attempted": "bool", "style_confidence": "text", "style": "text",
    "debug_mode": "bool",
    "manual_review": "bool", "parse_review_paused": "bool",
    "verify_table_citations": "bool", "verify_semantic_contract": "nonempty_text",
}
COMPLEX_KEYS = {
    "user_source_ingest", "ingest_review", "verify_runtime", "verify_claim_evidence_config",
    "verification_lifecycle_failure", "frozen_fetch_provenance", "manual_review_ref_numbers",
}
CONFIG_FIELDS = {
    "accuracy": str, "style": (str, type(None)), "mailto_provided": bool,
    "google_books_key_present": bool, "http_profile": str,
    "challenge_mode": str, "fetch_workers": (int, type(None)),
    "autonomous": bool, "ocr_lang": str, "model": (str, type(None)),
    "verify_table_citations": bool, "fresh_start_from": (str, type(None)),
}
_REQUIRED_CONFIG_TEXT_FIELDS = {"accuracy", "http_profile", "challenge_mode", "ocr_lang"}

def _text(value: Any, *, nonempty: bool = False) -> str | None:
    if value is not None and type(value) is not str:
        raise ValueError("run setting must be text or null")
    if value is not None and "\0" in value:
        raise ValueError("run setting text contains NUL")
    if nonempty and (value is None or not value.strip()):
        raise ValueError("run setting must be nonempty text")
    return value

def normalize_setting(key: str, value: Any) -> Any:
    if key == "manual_review_ref_numbers":
        if type(value) is not list or any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("manual_review_ref_numbers must be positive integer list")
        return sorted(set(value))
    if key == "debug_labels":
        if type(value) is not list or any(type(item) is not str or not item or "\0" in item for item in value):
            raise ValueError("debug_labels must be a list of nonempty text")
        return list(value)
    if key == "config_snapshot":
        return normalize_config(value)
    kind = SCALAR_KEYS.get(key)
    if kind is None:
        if key not in COMPLEX_KEYS:
            raise ValueError(f"unknown run setting key: {key!r}")
        if key in {
            "verify_runtime", "verify_claim_evidence_config",
            "verification_lifecycle_failure",
        }:
            from .verify_setting_storage import (
                normalize_claim_evidence_config,
                normalize_lifecycle_failure,
                normalize_verify_runtime,
            )
            return {
                "verify_runtime": normalize_verify_runtime,
                "verify_claim_evidence_config": normalize_claim_evidence_config,
                "verification_lifecycle_failure": normalize_lifecycle_failure,
            }[key](value)
        if key in {
            "user_source_ingest", "ingest_review", "frozen_fetch_provenance",
        }:
            from .ingest_setting_storage import (
                normalize_frozen_fetch_provenance,
                normalize_ingest_review,
                normalize_user_source_ingest,
            )
            return {
                "user_source_ingest": normalize_user_source_ingest,
                "ingest_review": normalize_ingest_review,
                "frozen_fetch_provenance": normalize_frozen_fetch_provenance,
            }[key](value)
        if type(value) is not dict:
            raise ValueError(f"{key} must be an object")
        return value
    if kind == "bool":
        if type(value) is not bool: raise ValueError(f"{key} must be bool")
    elif kind == "int":
        if type(value) is not int or value < 0: raise ValueError(f"{key} must be a nonnegative integer")
    elif kind == "int_null":
        if value is not None and (type(value) is not int or value < 0): raise ValueError(f"{key} must be a nonnegative integer or null")
    elif kind == "nonempty_text":
        return _text(value, nonempty=True)
    else:
        return _text(value)
    return value

def validate_key(key: str) -> None:
    if key not in SCALAR_KEYS and key not in COMPLEX_KEYS and key not in {"debug_labels", "config_snapshot"}:
        raise ValueError(f"unknown run setting key: {key!r}")

def normalize_config(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(CONFIG_FIELDS):
        raise ValueError("config_snapshot has invalid fields")
    for key, expected in CONFIG_FIELDS.items():
        item = value[key]
        if isinstance(expected, tuple):
            if type(item) not in expected: raise ValueError("config_snapshot has invalid type")
        elif type(item) is not expected:
            raise ValueError("config_snapshot has invalid type")
        if type(item) is str and "\0" in item:
            raise ValueError("config_snapshot text contains NUL")
        if key in _REQUIRED_CONFIG_TEXT_FIELDS and not item.strip():
            raise ValueError(f"config_snapshot {key} must be nonempty text")
        if key == "fetch_workers" and item is not None and item < 0: raise ValueError("config_snapshot fetch_workers invalid")
    return dict(value)

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def strict_json_loads(value: str) -> Any:
    def pairs(items):
        out = {}
        for key, item in items:
            if key in out: raise ValueError("duplicate JSON key")
            out[key] = item
        return out
    return json.loads(value, object_pairs_hook=pairs,
                      parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))


def write_setting(conn, key: str, value: Any) -> None:
    value = normalize_setting(key, value)
    if key == "manual_review_ref_numbers":
        conn.execute(
            "UPDATE run_runtime_settings SET manual_review_ref_numbers_present=1, "
            "manual_review_ref_numbers_count=? WHERE singleton=1", (len(value),),
        )
        conn.execute("DELETE FROM run_runtime_manual_review_ref_numbers")
        conn.executemany(
            "INSERT INTO run_runtime_manual_review_ref_numbers(ref_number) VALUES(?)",
            ((number,) for number in value),
        )
        return
    if key == "debug_labels":
        conn.execute("UPDATE run_runtime_settings SET debug_labels_present=1, debug_labels_count=? WHERE singleton=1", (len(value),))
        conn.execute("DELETE FROM run_runtime_debug_labels")
        conn.executemany("INSERT INTO run_runtime_debug_labels(label_order,value) VALUES(?,?)", enumerate(value))
    elif key == "config_snapshot":
        encoded = canonical_json(value)
        row = conn.execute("SELECT snapshot_json FROM run_config_snapshot WHERE singleton=1").fetchone()
        if row is None: conn.execute("INSERT INTO run_config_snapshot(singleton,snapshot_json) VALUES(1,?)", (encoded,))
        elif row[0] != encoded: raise ValueError("config_snapshot is write-once")
    elif key in SCALAR_KEYS:
        col = key
        conn.execute(f"UPDATE run_runtime_settings SET {col}_present=1, {col}=? WHERE singleton=1", (value,))
    elif key == "verify_runtime":
        from .verify_setting_storage import replace_verify_runtime
        replace_verify_runtime(conn, value)
    elif key == "verification_lifecycle_failure":
        from .verify_setting_storage import replace_lifecycle_failure
        replace_lifecycle_failure(conn, value)
    elif key == "verify_claim_evidence_config":
        from .verify_setting_storage import write_claim_evidence_config
        write_claim_evidence_config(conn, value)
    elif key == "user_source_ingest":
        from .ingest_setting_storage import replace_user_source_ingest
        replace_user_source_ingest(conn, value)
    elif key == "ingest_review":
        from .ingest_setting_storage import replace_ingest_review
        replace_ingest_review(conn, value)
    elif key == "frozen_fetch_provenance":
        from .ingest_setting_storage import write_frozen_fetch_provenance
        write_frozen_fetch_provenance(conn, value)
    else:
        raise RuntimeError("current run setting has no typed storage")

def read_setting(conn, key: str, default: Any = None) -> Any:
    validate_key(key)
    if key == "manual_review_ref_numbers":
        row = conn.execute(
            "SELECT manual_review_ref_numbers_present,manual_review_ref_numbers_count "
            "FROM run_runtime_settings WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("run runtime settings singleton is missing")
        values = [item[0] for item in conn.execute(
            "SELECT ref_number FROM run_runtime_manual_review_ref_numbers ORDER BY ref_number"
        )]
        if not row[0]:
            if row[1] != 0 or values:
                raise RuntimeError("manual Parse review selector storage is inconsistent")
            return default
        if len(values) != row[1]:
            raise RuntimeError("manual Parse review selector storage is inconsistent")
        return normalize_setting(key, values)
    if key == "debug_labels":
        row = conn.execute("SELECT debug_labels_present,debug_labels_count FROM run_runtime_settings WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("run runtime settings singleton is missing")
        if not row[0]:
            if row[1] != 0 or conn.execute("SELECT 1 FROM run_runtime_debug_labels LIMIT 1").fetchone(): raise RuntimeError("debug label storage is inconsistent")
            return default
        values = [r[0] for r in conn.execute("SELECT value FROM run_runtime_debug_labels ORDER BY label_order")]
        if len(values) != row[1] or list(range(len(values))) != [r[0] for r in conn.execute("SELECT label_order FROM run_runtime_debug_labels ORDER BY label_order")]: raise RuntimeError("debug label storage is inconsistent")
        return normalize_setting(key, values)
    if key == "config_snapshot":
        row = conn.execute("SELECT snapshot_json FROM run_config_snapshot WHERE singleton=1").fetchone()
        return default if row is None else normalize_config(strict_json_loads(row[0]))
    if (
        key in {
            "verify_runtime", "verification_lifecycle_failure",
            "verify_claim_evidence_config",
        }
    ):
        from .verify_setting_storage import read_verify_runtime, read_lifecycle_failure, read_claim_evidence_config
        value = {"verify_runtime": read_verify_runtime, "verification_lifecycle_failure": read_lifecycle_failure, "verify_claim_evidence_config": read_claim_evidence_config}[key](conn)
        return default if value is None else value
    if (
        key in {
            "user_source_ingest", "ingest_review", "frozen_fetch_provenance",
        }
    ):
        from .ingest_setting_storage import (
            read_frozen_fetch_provenance,
            read_ingest_review,
            read_user_source_ingest,
        )
        value = {
            "user_source_ingest": read_user_source_ingest,
            "ingest_review": read_ingest_review,
            "frozen_fetch_provenance": read_frozen_fetch_provenance,
        }[key](conn)
        return default if value is None else value
    if key in SCALAR_KEYS:
        row = conn.execute(f"SELECT {key}_present,{key} FROM run_runtime_settings WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("run runtime settings singleton is missing")
        present, value = row[0], row[1]
        if not present:
            if value is not None: raise RuntimeError("run setting presence is inconsistent")
            return default
        if SCALAR_KEYS[key] == "bool":
            if type(value) is not int or value not in (0, 1):
                raise RuntimeError("run setting boolean storage is inconsistent")
            return bool(value)
        return normalize_setting(key, value)
    raise RuntimeError("current run setting has no typed storage")

def list_settings(conn) -> dict[str, Any]:
    out = {}
    for key in [*SCALAR_KEYS, "debug_labels", "config_snapshot", *COMPLEX_KEYS]:
        marker = object()
        value = read_setting(conn, key, marker)
        if value is not marker: out[key] = value
    return out
