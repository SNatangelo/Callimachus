# core/infra/db/fetch_traces.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed typed persistence contract for Fetch traces and source flags."""
from __future__ import annotations

import math
import sqlite3
import unicodedata
from typing import Any


TRACE_FAMILIES = {"execution", "direct_text", "provider_diagnostic", "summary"}
SOURCE_FLAGS = {
    "low_alpha_ratio",
    "whitespace_bloat",
    "fragmented_lines",
    "repeated_line",
}
PROVIDER_ERROR_TYPES = {
    "resource_exhausted",
    "auth",
    "rate_limit",
    "transient_server",
    "http_error",
    "timeout",
    "network",
    "provider_error",
    "invalid_provider_result",
}

_EXECUTION_REQUIRED = {
    "queue_index", "batch_index", "method", "url", "kind", "outcome",
}
_EXECUTION_OPTIONAL = {
    "stage",
    "frozen_candidate_id",
    "request", "status", "final_url", "content_type", "headers", "body_head",
    "challenge_markers", "cached_challenge", "reason", "reason_code", "chars",
    "extract_method", "identity_extract_method", "corroborate_signal",
    "corroborate_score", "html_content_kind", "has_pdf_link", "shell_reason",
    "page_chars", "abstract_chars", "shell_markers", "parser_variants",
}
_EXECUTION_RESPONSE_KEYS = {"status", "final_url", "content_type"}
_DIRECT_BASE = {"method", "source_ref", "chars", "outcome"}
_PROVIDER_BASE = {
    "provider", "status", "error", "reason", "error_type", "error_reason",
    "retryable", "http_status", "items",
}
_DIRECT_PROVIDER_ITEM_KEYS = {
    "method", "source_ref", "extract_method", "candidate_key",
    "discovery_reason", "chars",
}
_CANDIDATE_PROVIDER_ITEM_KEYS = {
    "method", "url", "kind", "candidate_key", "discovery_reason",
    "fallback_stage",
}
_PARSER_RESULT_KEYS = {
    "method", "quality_ok", "quality_metrics", "structure_flags", "error",
}
_PARSER_RESULT_OPTIONAL = {"identity_decision", "identity_status"}
_QUALITY_METRIC_KEYS = {"chars", "alpha_ratio", "word_tokens"}


def _clean_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    return text.replace("\x00", "\uFFFD")


def _sanitize(value: Any) -> Any:
    kind = type(value)
    if value is None or kind in {bool, int}:
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid Fetch trace facts")
        return value
    if kind is str:
        return _clean_text(value)
    if kind is list:
        return [_sanitize(item) for item in value]
    if kind is tuple:
        return [_sanitize(item) for item in value]
    if kind is dict:
        out: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Fetch trace keys must be strings")
            cleaned_key = _clean_text(key)
            if cleaned_key in out:
                raise ValueError("Fetch trace keys collide after canonical text cleaning")
            out[cleaned_key] = _sanitize(item)
        return out
    raise ValueError(f"unsupported Fetch trace value type: {kind.__name__}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(
    value: dict[str, Any], required: set[str], optional: set[str], label: str,
) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise ValueError(
            f"{label} has invalid keys; missing={sorted(missing)} extra={sorted(extra)}"
        )


def _text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ValueError(f"{label} must be text{' or null' if nullable else ''}")
    return value


def _nonempty_text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    result = _text(value, label, nullable=nullable)
    if result is not None and not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _integer(value: Any, label: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer{' or null' if nullable else ''}")
    return value


def _nonnegative(value: Any, label: str, *, nullable: bool = False) -> int | None:
    result = _integer(value, label, nullable=nullable)
    if result is not None and result < 0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _boolean(value: Any, label: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean{' or null' if nullable else ''}")
    return value


def _finite_float(value: Any, label: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite float{' or null' if nullable else ''}")
    return value


def _ordered_strings(value: Any, label: str) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    output: list[str] = []
    for index, item in enumerate(value):
        output.append(_nonempty_text(item, f"{label}[{index}]") or "")
    if len(output) != len(set(output)):
        raise ValueError(f"{label} must not contain duplicates")
    return output


def _validate_parser_variant(value: Any, index: int) -> dict[str, Any]:
    item = _mapping(value, f"parser_variants[{index}]")
    if set(item) == {"method", "error"}:
        _nonempty_text(item["method"], f"parser_variants[{index}].method")
        _nonempty_text(item["error"], f"parser_variants[{index}].error")
        return item
    _exact_keys(item, _PARSER_RESULT_KEYS, _PARSER_RESULT_OPTIONAL, f"parser_variants[{index}]")
    _nonempty_text(item["method"], f"parser_variants[{index}].method")
    _boolean(item["quality_ok"], f"parser_variants[{index}].quality_ok")
    _text(item["error"], f"parser_variants[{index}].error", nullable=True)
    metrics = _mapping(item["quality_metrics"], f"parser_variants[{index}].quality_metrics")
    if set(metrics) - _QUALITY_METRIC_KEYS:
        raise ValueError("parser quality metrics contain unknown keys")
    if "chars" in metrics:
        _nonnegative(metrics["chars"], f"parser_variants[{index}].quality_metrics.chars")
    if "alpha_ratio" in metrics:
        ratio = _finite_float(
            metrics["alpha_ratio"],
            f"parser_variants[{index}].quality_metrics.alpha_ratio",
        )
        if ratio is not None and not 0.0 <= ratio <= 1.0:
            raise ValueError("parser alpha_ratio must be between zero and one")
    if "word_tokens" in metrics:
        _nonnegative(
            metrics["word_tokens"],
            f"parser_variants[{index}].quality_metrics.word_tokens",
        )
    _ordered_source_flags(
        item["structure_flags"], f"parser_variants[{index}].structure_flags",
    )
    for name in _PARSER_RESULT_OPTIONAL & set(item):
        _text(item[name], f"parser_variants[{index}].{name}", nullable=True)
    return item


def _ordered_source_flags(value: Any, label: str) -> list[str]:
    flags = _ordered_strings(value, label)
    unknown = set(flags) - SOURCE_FLAGS
    if unknown:
        raise ValueError(f"{label} contains unknown flags: {sorted(unknown)}")
    return flags


def _validate_execution(trace: dict[str, Any]) -> None:
    _exact_keys(trace, _EXECUTION_REQUIRED, _EXECUTION_OPTIONAL, "execution trace")
    _nonnegative(trace["queue_index"], "execution.queue_index")
    _nonnegative(trace["batch_index"], "execution.batch_index")
    if "frozen_candidate_id" in trace:
        frozen_candidate_id = _integer(
            trace["frozen_candidate_id"], "execution.frozen_candidate_id"
        )
        if frozen_candidate_id is None or frozen_candidate_id <= 0:
            raise ValueError("execution.frozen_candidate_id must be positive")
    for name in ("method", "url", "kind", "outcome"):
        _nonempty_text(trace[name], f"execution.{name}")
    if "stage" in trace:
        stage = _nonempty_text(trace["stage"], "execution.stage")
        if stage != "candidate_generation":
            raise ValueError("execution stage is unknown")

    if "request" in trace:
        if trace["request"] != "none":
            raise ValueError("execution.request must be 'none'")
        if set(trace) & {
            "status", "final_url", "content_type", "headers", "body_head",
            "challenge_markers", "cached_challenge",
        }:
            raise ValueError("unattempted execution trace contains response facts")
    elif not _EXECUTION_RESPONSE_KEYS <= set(trace):
        raise ValueError("attempted execution trace lacks response state")

    if "status" in trace:
        status = _integer(trace["status"], "execution.status", nullable=True)
        if status is not None and not 100 <= status <= 599:
            raise ValueError("execution.status is outside the HTTP range")
    for name in (
        "final_url", "content_type", "reason", "reason_code", "extract_method",
        "identity_extract_method", "corroborate_signal", "html_content_kind",
        "shell_reason",
    ):
        if name in trace:
            _text(trace[name], f"execution.{name}", nullable=name in _EXECUTION_RESPONSE_KEYS)
    if "headers" in trace:
        headers = _mapping(trace["headers"], "execution.headers")
        if not headers:
            raise ValueError("execution.headers must not be empty when present")
        for name, value in headers.items():
            _nonempty_text(name, "execution header name")
            _text(value, f"execution header {name}")
    if "body_head" in trace:
        _nonempty_text(trace["body_head"], "execution.body_head")
    for name in ("challenge_markers", "shell_markers"):
        if name in trace:
            _ordered_strings(trace[name], f"execution.{name}")
    if "cached_challenge" in trace and trace["cached_challenge"] is not True:
        raise ValueError("execution.cached_challenge must be true when present")
    for name in ("chars", "page_chars", "abstract_chars"):
        if name in trace:
            _nonnegative(trace[name], f"execution.{name}")
    if "corroborate_score" in trace:
        _finite_float(trace["corroborate_score"], "execution.corroborate_score")
    if "has_pdf_link" in trace:
        _boolean(trace["has_pdf_link"], "execution.has_pdf_link")
    if "parser_variants" in trace:
        variants = trace["parser_variants"]
        if type(variants) is not list:
            raise ValueError("execution.parser_variants must be an array")
        for index, item in enumerate(variants):
            _validate_parser_variant(item, index)


def _validate_direct_text(trace: dict[str, Any]) -> None:
    outcome = trace.get("outcome")
    identity_extract_method = trace.get("identity_extract_method")
    identity_optional = {"identity_extract_method"} if identity_extract_method is not None else set()
    if outcome == "quality_below_threshold":
        required, optional = _DIRECT_BASE, set()
    elif outcome == "stored":
        required, optional = _DIRECT_BASE | {"extract_method"}, set()
    else:
        required = _DIRECT_BASE | {
            "reason", "corroborate_signal", "corroborate_score",
        }
        optional = set()
    _exact_keys(trace, required, optional | identity_optional, "direct-text trace")
    _nonempty_text(trace["method"], "direct_text.method")
    _text(trace["source_ref"], "direct_text.source_ref", nullable=True)
    _nonnegative(trace["chars"], "direct_text.chars")
    _nonempty_text(trace["outcome"], "direct_text.outcome")
    if "extract_method" in trace:
        _nonempty_text(trace["extract_method"], "direct_text.extract_method")
    if "identity_extract_method" in trace:
        if (
            trace["method"] != "springer_openaccess"
            or trace["identity_extract_method"] != "api_jats_front"
            or outcome not in {
                "stored", "identity_inconclusive", "identity_mismatch", "quality_error",
            }
        ):
            raise ValueError("direct_text.identity_extract_method is not permitted")
        _nonempty_text(
            trace["identity_extract_method"], "direct_text.identity_extract_method"
        )
    if outcome not in {"quality_below_threshold", "stored"}:
        _text(trace["reason"], "direct_text.reason", nullable=True)
        _text(
            trace["corroborate_signal"], "direct_text.corroborate_signal", nullable=True,
        )
        _finite_float(
            trace["corroborate_score"], "direct_text.corroborate_score", nullable=True,
        )


def _validate_provider_diagnostic(trace: dict[str, Any]) -> None:
    direct = "item_count" in trace
    required = _PROVIDER_BASE | ({"item_count"} if direct else set())
    _exact_keys(trace, required, set(), "provider diagnostic")
    _text(trace["provider"], "provider.provider", nullable=True)
    if trace["status"] not in {"partial", "error"}:
        raise ValueError("provider diagnostic status must be partial or error")
    for name in ("error", "reason", "error_reason"):
        _text(trace[name], f"provider.{name}", nullable=True)
    error_type = _text(trace["error_type"], "provider.error_type", nullable=True)
    if error_type is not None and error_type not in PROVIDER_ERROR_TYPES:
        raise ValueError("provider diagnostic has an unknown error_type")
    _boolean(trace["retryable"], "provider.retryable", nullable=True)
    status = _integer(trace["http_status"], "provider.http_status", nullable=True)
    if status is not None and not 100 <= status <= 599:
        raise ValueError("provider http_status is outside the HTTP range")
    items = trace["items"]
    if type(items) is not list:
        raise ValueError("provider items must be an array")
    if direct and _nonnegative(trace["item_count"], "provider.item_count") != len(items):
        raise ValueError("provider item_count does not match items")
    expected = _DIRECT_PROVIDER_ITEM_KEYS if direct else _CANDIDATE_PROVIDER_ITEM_KEYS
    for index, raw in enumerate(items):
        item = _mapping(raw, f"provider.items[{index}]")
        _exact_keys(item, expected, set(), f"provider.items[{index}]")
        for name, value in item.items():
            if name == "chars":
                _nonnegative(value, f"provider.items[{index}].chars")
            else:
                _text(value, f"provider.items[{index}].{name}", nullable=True)


def validate_fetch_trace(value: Any) -> dict[str, Any] | None:
    """Sanitize and validate one closed Fetch trace payload."""
    if value is None:
        return None
    trace = _mapping(_sanitize(value), "Fetch trace")
    if set(trace) == {"summary_only"}:
        if trace["summary_only"] is not True:
            raise ValueError("summary trace marker must be true")
        return trace
    if _EXECUTION_REQUIRED <= set(trace):
        _validate_execution(trace)
        return trace
    if _DIRECT_BASE <= set(trace):
        _validate_direct_text(trace)
        return trace
    if _PROVIDER_BASE <= set(trace):
        _validate_provider_diagnostic(trace)
        return trace
    raise ValueError("unknown Fetch trace family")


def trace_family(trace: dict[str, Any] | None) -> str | None:
    if trace is None:
        return None
    if set(trace) == {"summary_only"}:
        return "summary"
    if _EXECUTION_REQUIRED <= set(trace):
        return "execution"
    if _DIRECT_BASE <= set(trace):
        return "direct_text"
    if _PROVIDER_BASE <= set(trace):
        return "provider_diagnostic"
    raise ValueError("unknown Fetch trace family")


def normalize_source_flags(value: list[str] | None) -> list[str] | None:
    """Normalize current source flags to a deterministic sorted set."""
    if value is None:
        return None
    if type(value) is not list:
        raise ValueError("source extraction flags must be an array or null")
    cleaned = []
    for index, item in enumerate(value):
        cleaned.append(_nonempty_text(_sanitize(item), f"source flag {index}") or "")
    unknown = set(cleaned) - SOURCE_FLAGS
    if unknown:
        raise ValueError(f"unknown source extraction flags: {sorted(unknown)}")
    normalized = sorted(set(cleaned))
    return normalized or None


def validate_stored_source_flags(value: Any) -> list[str] | None:
    """Validate the exact current relational representation."""
    if value is None:
        return None
    flags = _ordered_source_flags(_sanitize(value), "source extraction flags")
    if flags != sorted(flags):
        raise ValueError("stored source extraction flags are not sorted")
    return flags


def _state_rows(conn: sqlite3.Connection, attempt_id: int) -> dict[str, Any]:
    state = conn.execute(
        "SELECT * FROM fetch_attempt_trace_states WHERE fetch_attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if state is None:
        raise RuntimeError("typed Fetch trace state is missing")
    return dict(state)


def _parent_row(conn: sqlite3.Connection, attempt_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM fetch_attempts WHERE fetch_attempt_id = ?", (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Fetch attempt does not exist")
    return dict(row)


def _validate_parent(trace: dict[str, Any] | None, parent: dict[str, Any]) -> None:
    family = trace_family(trace)
    if family is None:
        return
    if family == "execution":
        pairs = {
            "method": "method", "url": "url", "kind": "kind", "outcome": "outcome",
            "status": "status_code", "final_url": "final_url",
            "content_type": "content_type", "reason": "reason",
        }
        for trace_key, parent_key in pairs.items():
            value = trace.get(trace_key)
            if value != parent.get(parent_key):
                raise ValueError(f"execution {trace_key} disagrees with Fetch attempt")
        if bool(parent.get("challenge_blocked")) != (trace["outcome"] == "challenge_blocked"):
            raise ValueError("execution challenge state disagrees with Fetch attempt")
    elif family == "direct_text":
        if parent.get("kind") != "direct_text" or parent.get("content_type") != "text/plain":
            raise ValueError("direct-text trace has incompatible Fetch attempt kind")
        if trace["method"] != parent.get("method"):
            raise ValueError("direct-text method disagrees with Fetch attempt")
        if trace["source_ref"] != parent.get("url") or trace["source_ref"] != parent.get("final_url"):
            raise ValueError("direct-text source_ref disagrees with Fetch attempt")
        if trace["outcome"] != parent.get("outcome") or trace.get("reason") != parent.get("reason"):
            raise ValueError("direct-text outcome disagrees with Fetch attempt")
    elif family == "provider_diagnostic":
        if parent.get("kind") != "provider_diagnostic":
            raise ValueError("provider diagnostic has incompatible Fetch attempt kind")
        expected_outcome = "provider_partial" if trace["status"] == "partial" else "provider_error"
        expected_reason = trace["error_reason"] or trace["reason"] or trace["error"]
        if (
            trace["provider"] != parent.get("method")
            or trace["http_status"] != parent.get("status_code")
            or expected_outcome != parent.get("outcome")
            or expected_reason != parent.get("reason")
        ):
            raise ValueError("provider diagnostic disagrees with Fetch attempt")
    elif parent.get("kind") != "summary":
        raise ValueError("summary trace has incompatible Fetch attempt kind")


def validate_fetch_trace_for_attempt(
    conn: sqlite3.Connection, attempt_id: int, value: Any,
) -> dict[str, Any] | None:
    """Validate a trace including every duplicated fact on its parent attempt."""
    trace = validate_fetch_trace(value)
    _validate_parent(trace, _parent_row(conn, attempt_id))
    if trace_family(trace) == "execution" and "frozen_candidate_id" in trace:
        row = conn.execute(
            """
            SELECT 1
            FROM fetch_attempts AS a
            JOIN fetch_frozen_candidates AS c
            JOIN fetch_candidate_stage_freezes AS s
              ON s.stage_freeze_id = c.stage_freeze_id
            JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
            WHERE a.fetch_attempt_id = ?
              AND c.frozen_candidate_id = ?
              AND a.ref_id = p.ref_id
            """,
            (attempt_id, trace["frozen_candidate_id"]),
        ).fetchone()
        if row is None:
            raise ValueError(
                "execution frozen candidate disagrees with Fetch attempt reference"
            )
    return trace


_EXECUTION_SCALARS = (
    "stage", "request", "status", "final_url", "content_type", "body_head",
    "cached_challenge", "reason", "reason_code", "chars", "extract_method",
    "identity_extract_method", "corroborate_signal", "corroborate_score",
    "html_content_kind", "has_pdf_link", "shell_reason", "page_chars",
    "abstract_chars",
)


def replace_fetch_trace(
    conn: sqlite3.Connection, attempt_id: int, value: Any,
) -> dict[str, Any] | None:
    trace = validate_fetch_trace_for_attempt(conn, attempt_id, value)
    conn.execute(
        "DELETE FROM fetch_attempt_trace_states WHERE fetch_attempt_id = ?",
        (attempt_id,),
    )
    family = trace_family(trace)
    conn.execute(
        "INSERT INTO fetch_attempt_trace_states(fetch_attempt_id,is_null,family) VALUES(?,?,?)",
        (attempt_id, 1 if trace is None else 0, family),
    )
    if trace is None or family == "summary":
        return trace
    if family == "execution":
        params: dict[str, Any] = {
            "fetch_attempt_id": attempt_id,
            "frozen_candidate_id": trace.get("frozen_candidate_id"),
            "frozen_candidate_id_present": int("frozen_candidate_id" in trace),
            "queue_index": trace["queue_index"],
            "batch_index": trace["batch_index"],
            "method": trace["method"],
            "url": trace["url"],
            "kind": trace["kind"],
            "outcome": trace["outcome"],
            "headers_present": int("headers" in trace),
            "headers_count": len(trace.get("headers", {})),
            "challenge_markers_present": int("challenge_markers" in trace),
            "challenge_markers_count": len(trace.get("challenge_markers", [])),
            "shell_markers_present": int("shell_markers" in trace),
            "shell_markers_count": len(trace.get("shell_markers", [])),
            "parser_variants_present": int("parser_variants" in trace),
            "parser_variants_count": len(trace.get("parser_variants", [])),
        }
        for name in _EXECUTION_SCALARS:
            params[name] = (
                int(trace[name]) if name in {"cached_challenge", "has_pdf_link"} and name in trace
                else trace.get(name)
            )
            params[f"{name}_present"] = int(name in trace)
        columns = tuple(params)
        conn.execute(
            f"INSERT INTO fetch_execution_traces({','.join(columns)}) "
            f"VALUES({','.join(':'+name for name in columns)})",
            params,
        )
        for order, (name, header_value) in enumerate(trace.get("headers", {}).items()):
            conn.execute(
                "INSERT INTO fetch_execution_headers VALUES(?,?,?,?)",
                (attempt_id, order, name, header_value),
            )
        for marker_kind, key in (("challenge", "challenge_markers"), ("shell", "shell_markers")):
            for order, marker in enumerate(trace.get(key, [])):
                conn.execute(
                    "INSERT INTO fetch_execution_markers VALUES(?,?,?,?)",
                    (attempt_id, marker_kind, order, marker),
                )
        for order, variant in enumerate(trace.get("parser_variants", [])):
            error_only = set(variant) == {"method", "error"}
            metrics = variant.get("quality_metrics", {})
            conn.execute(
                """INSERT INTO fetch_execution_parser_variants(
                   fetch_attempt_id,variant_order,variant_kind,method,quality_ok,
                   chars,chars_present,alpha_ratio,alpha_ratio_present,
                   word_tokens,word_tokens_present,error,identity_decision,
                   identity_decision_present,identity_status,identity_status_present,
                   structure_flags_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, order, "error" if error_only else "result",
                    variant["method"], None if error_only else int(variant["quality_ok"]),
                    metrics.get("chars"), int("chars" in metrics),
                    metrics.get("alpha_ratio"), int("alpha_ratio" in metrics),
                    metrics.get("word_tokens"), int("word_tokens" in metrics),
                    variant["error"], variant.get("identity_decision"),
                    int("identity_decision" in variant), variant.get("identity_status"),
                    int("identity_status" in variant),
                    len(variant.get("structure_flags", [])),
                ),
            )
            for flag_order, flag in enumerate(variant.get("structure_flags", [])):
                conn.execute(
                    "INSERT INTO fetch_execution_parser_flags VALUES(?,?,?,?)",
                    (attempt_id, order, flag_order, flag),
                )
    elif family == "direct_text":
        subtype = (
            "quality" if trace["outcome"] == "quality_below_threshold"
            else "stored" if trace["outcome"] == "stored" else "failed"
        )
        conn.execute(
            """INSERT INTO fetch_direct_text_traces(
               fetch_attempt_id,subtype,method,source_ref,chars,outcome,
               extract_method,identity_extract_method,identity_extract_method_present,
               reason,corroborate_signal,corroborate_score
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, subtype, trace["method"], trace["source_ref"],
                trace["chars"], trace["outcome"], trace.get("extract_method"),
                trace.get("identity_extract_method"),
                int("identity_extract_method" in trace),
                trace.get("reason"), trace.get("corroborate_signal"),
                trace.get("corroborate_score"),
            ),
        )
    else:
        subtype = "direct" if "item_count" in trace else "candidate"
        conn.execute(
            """INSERT INTO fetch_provider_diagnostic_traces(
               fetch_attempt_id,subtype,provider,status,error,reason,error_type,
               error_reason,retryable,http_status,item_count,items_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, subtype, trace["provider"], trace["status"],
                trace["error"], trace["reason"], trace["error_type"],
                trace["error_reason"],
                None if trace["retryable"] is None else int(trace["retryable"]),
                trace["http_status"], trace.get("item_count"), len(trace["items"]),
            ),
        )
        table = (
            "fetch_provider_direct_items" if subtype == "direct"
            else "fetch_provider_candidate_items"
        )
        item_columns = (
            tuple(_DIRECT_PROVIDER_ITEM_KEYS) if subtype == "direct"
            else tuple(_CANDIDATE_PROVIDER_ITEM_KEYS)
        )
        for order, item in enumerate(trace["items"]):
            columns = ("fetch_attempt_id", "item_order") + item_columns
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                (attempt_id, order, *(item[name] for name in item_columns)),
            )
    return trace


def _dense(rows: list[sqlite3.Row], key: str, label: str) -> None:
    if [int(row[key]) for row in rows] != list(range(len(rows))):
        raise RuntimeError(f"{label} ordinals are not contiguous")


def _one(conn: sqlite3.Connection, table: str, attempt_id: int):
    return conn.execute(
        f"SELECT * FROM {table} WHERE fetch_attempt_id = ?", (attempt_id,),
    ).fetchone()


def read_fetch_trace(conn: sqlite3.Connection, attempt_id: int) -> dict[str, Any] | None:
    state = _state_rows(conn, attempt_id)
    family = state["family"]
    is_null = bool(state["is_null"])
    detail_tables = (
        "fetch_execution_traces", "fetch_direct_text_traces",
        "fetch_provider_diagnostic_traces",
    )
    details = {table: _one(conn, table, attempt_id) for table in detail_tables}
    execution_children = {
        table: _one(conn, table, attempt_id)
        for table in (
            "fetch_execution_headers", "fetch_execution_markers",
            "fetch_execution_parser_variants", "fetch_execution_parser_flags",
        )
    }
    provider_children = {
        table: _one(conn, table, attempt_id)
        for table in ("fetch_provider_direct_items", "fetch_provider_candidate_items")
    }
    if is_null:
        if (
            family is not None or any(details.values())
            or any(execution_children.values()) or any(provider_children.values())
        ):
            raise RuntimeError("null Fetch trace state has typed payload")
        return None
    if family not in TRACE_FAMILIES:
        raise RuntimeError("typed Fetch trace family is invalid")
    expected_table = {
        "execution": "fetch_execution_traces",
        "direct_text": "fetch_direct_text_traces",
        "provider_diagnostic": "fetch_provider_diagnostic_traces",
        "summary": None,
    }[family]
    if any(row is not None for table, row in details.items() if table != expected_table):
        raise RuntimeError("typed Fetch trace has detail for another family")
    if expected_table is not None and details[expected_table] is None:
        raise RuntimeError("typed Fetch trace detail is missing")
    if family != "execution" and any(execution_children.values()):
        raise RuntimeError("non-execution Fetch trace has execution children")
    if family != "provider_diagnostic" and any(provider_children.values()):
        raise RuntimeError("non-provider Fetch trace has provider children")
    if family == "summary":
        trace: dict[str, Any] = {"summary_only": True}
    elif family == "execution":
        row = details["fetch_execution_traces"]
        trace = {
            name: row[name]
            for name in ("queue_index", "batch_index", "method", "url", "kind", "outcome")
        }
        frozen_candidate_present = row["frozen_candidate_id_present"]
        frozen_candidate_id = row["frozen_candidate_id"]
        if frozen_candidate_present not in (0, 1):
            raise RuntimeError("execution frozen candidate presence is corrupt")
        if frozen_candidate_present:
            if type(frozen_candidate_id) is not int or frozen_candidate_id <= 0:
                raise RuntimeError("execution frozen candidate is corrupt")
            trace["frozen_candidate_id"] = frozen_candidate_id
        elif frozen_candidate_id is not None:
            raise RuntimeError("absent execution frozen candidate has value")
        for name in _EXECUTION_SCALARS:
            present = bool(row[f"{name}_present"])
            value = row[name]
            if not present and value is not None:
                raise RuntimeError(f"absent execution {name} has a value")
            if present:
                trace[name] = bool(value) if name in {"cached_challenge", "has_pdf_link"} else value
        headers = conn.execute(
            "SELECT * FROM fetch_execution_headers WHERE fetch_attempt_id=? ORDER BY header_order",
            (attempt_id,),
        ).fetchall()
        _dense(headers, "header_order", "Fetch execution header")
        if len(headers) != int(row["headers_count"]):
            raise RuntimeError("Fetch execution header count mismatch")
        if bool(row["headers_present"]):
            trace["headers"] = {item["name"]: item["value"] for item in headers}
            if len(trace["headers"]) != len(headers):
                raise RuntimeError("Fetch execution header names are duplicated")
        elif headers:
            raise RuntimeError("absent Fetch execution headers have rows")
        for marker_kind, key, present_key, count_key in (
            ("challenge", "challenge_markers", "challenge_markers_present", "challenge_markers_count"),
            ("shell", "shell_markers", "shell_markers_present", "shell_markers_count"),
        ):
            rows = conn.execute(
                """SELECT * FROM fetch_execution_markers
                   WHERE fetch_attempt_id=? AND marker_kind=? ORDER BY marker_order""",
                (attempt_id, marker_kind),
            ).fetchall()
            _dense(rows, "marker_order", f"Fetch {marker_kind} marker")
            if len(rows) != int(row[count_key]):
                raise RuntimeError(f"Fetch {marker_kind} marker count mismatch")
            if bool(row[present_key]):
                trace[key] = [item["marker"] for item in rows]
            elif rows:
                raise RuntimeError(f"absent Fetch {marker_kind} markers have rows")
        variants = conn.execute(
            """SELECT * FROM fetch_execution_parser_variants
               WHERE fetch_attempt_id=? ORDER BY variant_order""",
            (attempt_id,),
        ).fetchall()
        _dense(variants, "variant_order", "Fetch parser variant")
        if len(variants) != int(row["parser_variants_count"]):
            raise RuntimeError("Fetch parser variant count mismatch")
        if bool(row["parser_variants_present"]):
            decoded = []
            for variant in variants:
                if variant["variant_kind"] == "error":
                    if _one(conn, "fetch_execution_parser_flags", attempt_id) is not None:
                        error_flags = conn.execute(
                            """SELECT 1 FROM fetch_execution_parser_flags
                               WHERE fetch_attempt_id=? AND variant_order=?""",
                            (attempt_id, variant["variant_order"]),
                        ).fetchone()
                        if error_flags is not None:
                            raise RuntimeError("error-only Fetch parser variant has flags")
                    item = {"method": variant["method"], "error": variant["error"]}
                elif variant["variant_kind"] == "result":
                    metrics = {}
                    for name in _QUALITY_METRIC_KEYS:
                        present = bool(variant[f"{name}_present"])
                        if not present and variant[name] is not None:
                            raise RuntimeError("absent parser metric has a value")
                        if present:
                            metrics[name] = variant[name]
                    flags = conn.execute(
                        """SELECT * FROM fetch_execution_parser_flags
                           WHERE fetch_attempt_id=? AND variant_order=? ORDER BY flag_order""",
                        (attempt_id, variant["variant_order"]),
                    ).fetchall()
                    _dense(flags, "flag_order", "Fetch parser flag")
                    if len(flags) != int(variant["structure_flags_count"]):
                        raise RuntimeError("Fetch parser flag count mismatch")
                    item = {
                        "method": variant["method"],
                        "quality_ok": bool(variant["quality_ok"]),
                        "quality_metrics": metrics,
                        "structure_flags": [flag["flag"] for flag in flags],
                        "error": variant["error"],
                    }
                    for name in ("identity_decision", "identity_status"):
                        present = bool(variant[f"{name}_present"])
                        if not present and variant[name] is not None:
                            raise RuntimeError("absent parser identity field has a value")
                        if present:
                            item[name] = variant[name]
                else:
                    raise RuntimeError("unknown Fetch parser variant kind")
                decoded.append(item)
            trace["parser_variants"] = decoded
        elif variants:
            raise RuntimeError("absent Fetch parser variants have rows")
    elif family == "direct_text":
        row = details["fetch_direct_text_traces"]
        trace = {
            "method": row["method"], "source_ref": row["source_ref"],
            "chars": row["chars"], "outcome": row["outcome"],
        }
        if row["subtype"] == "stored":
            trace["extract_method"] = row["extract_method"]
        elif row["subtype"] == "failed":
            trace.update(
                reason=row["reason"], corroborate_signal=row["corroborate_signal"],
                corroborate_score=row["corroborate_score"],
            )
        elif row["subtype"] != "quality":
            raise RuntimeError("unknown direct-text subtype")
        present = bool(row["identity_extract_method_present"])
        if not present and row["identity_extract_method"] is not None:
            raise RuntimeError("absent direct-text identity extract method has a value")
        if present:
            trace["identity_extract_method"] = row["identity_extract_method"]
    else:
        row = details["fetch_provider_diagnostic_traces"]
        subtype = row["subtype"]
        table = (
            "fetch_provider_direct_items" if subtype == "direct"
            else "fetch_provider_candidate_items" if subtype == "candidate" else None
        )
        if table is None:
            raise RuntimeError("unknown provider diagnostic subtype")
        items = conn.execute(
            f"SELECT * FROM {table} WHERE fetch_attempt_id=? ORDER BY item_order",
            (attempt_id,),
        ).fetchall()
        _dense(items, "item_order", "provider diagnostic item")
        if len(items) != int(row["items_count"]):
            raise RuntimeError("provider diagnostic item count mismatch")
        item_keys = (
            _DIRECT_PROVIDER_ITEM_KEYS if subtype == "direct"
            else _CANDIDATE_PROVIDER_ITEM_KEYS
        )
        trace = {
            "provider": row["provider"], "status": row["status"],
            "error": row["error"], "reason": row["reason"],
            "error_type": row["error_type"], "error_reason": row["error_reason"],
            "retryable": None if row["retryable"] is None else bool(row["retryable"]),
            "http_status": row["http_status"],
            "items": [{name: item[name] for name in item_keys} for item in items],
        }
        if subtype == "direct":
            trace["item_count"] = row["item_count"]
        other_table = (
            "fetch_provider_candidate_items" if subtype == "direct"
            else "fetch_provider_direct_items"
        )
        if _one(conn, other_table, attempt_id) is not None:
            raise RuntimeError("provider diagnostic has items for another subtype")
    try:
        validated = validate_fetch_trace(trace)
        validate_fetch_trace_for_attempt(conn, attempt_id, validated)
    except ValueError as exc:
        raise RuntimeError("typed Fetch trace is internally inconsistent") from exc
    return validated


def replace_source_flags(
    conn: sqlite3.Connection, source_text_id: str, value: list[str] | None,
) -> list[str] | None:
    flags = normalize_source_flags(value)
    exists = conn.execute(
        "SELECT 1 FROM source_texts WHERE source_text_id=?", (source_text_id,),
    ).fetchone()
    if exists is None:
        raise ValueError("source text does not exist")
    conn.execute(
        "DELETE FROM source_text_extraction_flag_states WHERE source_text_id=?",
        (source_text_id,),
    )
    conn.execute(
        "INSERT INTO source_text_extraction_flag_states(source_text_id,is_null,flag_count) VALUES(?,?,?)",
        (source_text_id, int(flags is None), 0 if flags is None else len(flags)),
    )
    for flag in flags or []:
        conn.execute(
            "INSERT INTO source_text_extraction_flags(source_text_id,flag) VALUES(?,?)",
            (source_text_id, flag),
        )
    return flags


def read_source_flags(conn: sqlite3.Connection, source_text_id: str) -> list[str]:
    state = conn.execute(
        "SELECT * FROM source_text_extraction_flag_states WHERE source_text_id=?",
        (source_text_id,),
    ).fetchone()
    if state is None:
        raise RuntimeError("typed source extraction-flag state is missing")
    rows = conn.execute(
        "SELECT flag FROM source_text_extraction_flags WHERE source_text_id=? ORDER BY flag",
        (source_text_id,),
    ).fetchall()
    flags = [row["flag"] for row in rows]
    if len(flags) != int(state["flag_count"]):
        raise RuntimeError("typed source extraction-flag count mismatch")
    if bool(state["is_null"]):
        if flags:
            raise RuntimeError("null source extraction-flag state has rows")
        return []
    try:
        validated = validate_stored_source_flags(flags)
    except ValueError as exc:
        raise RuntimeError("typed source extraction flags are invalid") from exc
    return validated or []
