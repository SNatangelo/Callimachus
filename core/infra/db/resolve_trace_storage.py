#!/usr/bin/env python3
# core/infra/db/resolve_trace_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed storage and closed validation for Resolve decision traces."""

from __future__ import annotations

import sqlite3
from typing import Any


JsonDict = dict[str, Any]

TRACE_BUCKET_STAGES = {
    "resolver_attempts": {
        "declared_present",
        "declared_cache_lookup",
        "strong_discovery",
        "final_resolution",
    },
    "identifier_validations": {
        "declared_validation",
        "discovered_validation",
        "strong_confirmed",
    },
    "retraction_checks": {"retraction_check"},
    "weak_corroboration_events": {"weak_aggregation"},
}

TRACE_DETAIL_TABLES = {
    "identifier": "resolve_trace_identifier_details",
    "reason": "resolve_trace_reason_details",
    "status": "resolve_trace_status_details",
    "confirmed": "resolve_trace_confirmed_details",
    "weak": "resolve_trace_weak_details",
    "retraction": "resolve_trace_retraction_details",
    "final": "resolve_trace_final_details",
}

TRACE_STAGE_DETAIL_KIND = {
    "declared_present": "identifier",
    "strong_discovery": "identifier",
    "declared_cache_lookup": "reason",
    "declared_validation": "status",
    "discovered_validation": "status",
    "strong_confirmed": "confirmed",
    "weak_aggregation": "weak",
    "retraction_check": "retraction",
    "final_resolution": "final",
}

TRACE_DETAIL_KEYS = {
    "declared_present": ("scheme", "value", "is_strong", "class_"),
    "strong_discovery": ("scheme", "value", "is_strong", "class_"),
    "declared_cache_lookup": ("reason",),
    "declared_validation": ("status", "via"),
    "discovered_validation": ("status", "via"),
    "strong_confirmed": ("identity_state", "resolution_basis"),
    "weak_aggregation": (
        "status", "reference_status_tag", "fabrication_risk",
    ),
    "retraction_check": ("checked_via", "retracted"),
    "final_resolution": ("status", "via", "attempt_count"),
}

_IDENTITY_STATES = {
    "resolved_strong_declared",
    "resolved_strong_discovered",
    "declared_identifier_failed",
    "weakly_corroborated",
    "unverified",
    "fabricated",
}
_STRONG_IDENTITY_STATES = {
    "resolved_strong_declared",
    "resolved_strong_discovered",
}
_GLOBAL_SCHEMES = {"doi", "pmid", "isbn", "arxiv_id", "pmcid"}
_AUTHORITY_LOCAL_SCHEMES = {"acl_id", "ssrn_abstract_id", "url"}
_STRONG_SCHEMES = _GLOBAL_SCHEMES | {"acl_id", "ssrn_abstract_id"}


def _text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"resolution trace {label} must be non-empty text")
    return value


def _validate_identity(stage: str, detail: JsonDict) -> None:
    class_ = detail["class_"]
    is_strong = detail["is_strong"]
    scheme = detail["scheme"]
    value = detail["value"]
    if class_ not in {"global", "authority_local", "none"}:
        raise ValueError("resolution trace identity class")
    if type(is_strong) is not bool:
        raise ValueError("resolution trace identity strength")
    _text(scheme, "identity scheme")
    if type(value) is not str or "\0" in value:
        raise ValueError("resolution trace identity value")

    expected_class = (
        "global" if scheme in _GLOBAL_SCHEMES
        else "authority_local" if scheme in _AUTHORITY_LOCAL_SCHEMES
        else "none"
    )
    if class_ != expected_class or is_strong is not (scheme in _STRONG_SCHEMES):
        raise ValueError("resolution trace identity classification")
    if scheme == "none":
        if value != "" or class_ != "none" or is_strong:
            raise ValueError("resolution trace absent identity")
    elif not value.strip():
        raise ValueError("resolution trace identity value")

    if stage == "declared_present":
        if scheme not in {"doi", "pmid", "isbn", "url", "none"}:
            raise ValueError("resolution trace declared identity scheme")
    elif not is_strong:
        raise ValueError("resolution trace discovered identity must be strong")


def _validate_detail(stage: str, detail: Any) -> None:
    if type(detail) is not dict or set(detail) != set(TRACE_DETAIL_KEYS[stage]):
        raise ValueError("resolution trace detail shape")
    if stage in {"declared_present", "strong_discovery"}:
        _validate_identity(stage, detail)
    elif stage == "declared_cache_lookup":
        if detail["reason"] != "resolver has no declared-identity cache gate":
            raise ValueError("resolution trace cache reason")
    elif stage in {"declared_validation", "discovered_validation"}:
        _text(detail["status"], "validation status")
        _text(detail["via"], "validation via", nullable=True)
    elif stage == "strong_confirmed":
        if detail["identity_state"] not in _STRONG_IDENTITY_STATES:
            raise ValueError("resolution trace confirmed identity state")
        _text(detail["resolution_basis"], "resolution basis", nullable=True)
    elif stage == "weak_aggregation":
        _text(detail["status"], "weak status")
        _text(detail["reference_status_tag"], "reference status tag", nullable=True)
        _text(detail["fabrication_risk"], "fabrication risk", nullable=True)
    elif stage == "retraction_check":
        if detail["checked_via"] not in {
            "retraction_watch", "authority_metadata", "none",
        } or type(detail["retracted"]) is not bool:
            raise ValueError("resolution trace retraction detail")
    else:
        _text(detail["status"], "final status")
        _text(detail["via"], "final via", nullable=True)
        if type(detail["attempt_count"]) is not int or detail["attempt_count"] < 0:
            raise ValueError("resolution trace attempt count")


def validate_resolution_trace(
    trace: Any,
    *,
    expected_attempt_count: int | None = None,
) -> list[JsonDict]:
    """Validate the exact decision.py producer union and return detached rows."""
    if type(trace) is not list or not trace:
        raise ValueError("resolution trace must be a non-empty list")
    if expected_attempt_count is not None and (
        type(expected_attempt_count) is not int or expected_attempt_count < 0
    ):
        raise ValueError("resolution trace expected attempt count")

    rows: list[JsonDict] = []
    for position, raw in enumerate(trace, 1):
        if type(raw) is not dict or set(raw) != {"stage", "outcome", "detail", "at"}:
            raise ValueError("resolution trace row shape")
        stage = raw["stage"]
        outcome = raw["outcome"]
        at = raw["at"]
        if stage not in TRACE_DETAIL_KEYS:
            raise ValueError("resolution trace stage")
        _text(outcome, "outcome")
        if type(at) is not str or at != f"stage_{position:02d}":
            raise ValueError("resolution trace stage order")
        _validate_detail(stage, raw["detail"])
        rows.append({
            "stage": stage,
            "outcome": outcome,
            "detail": dict(raw["detail"]),
            "at": at,
        })

    names = [row["stage"] for row in rows]
    cursor = 0
    if names[cursor] != "declared_present":
        raise ValueError("resolution trace must begin with declared_present")
    declared = rows[cursor]
    declared_strong = declared["detail"]["is_strong"]
    expected_declared_outcome = (
        "absent" if declared["detail"]["scheme"] == "none" else "present"
    )
    if declared["outcome"] != expected_declared_outcome:
        raise ValueError("resolution trace declared outcome")
    cursor += 1

    declared_validation = None
    if declared_strong:
        if names[cursor:cursor + 2] != [
            "declared_cache_lookup", "declared_validation",
        ]:
            raise ValueError("resolution trace declared validation sequence")
        if rows[cursor]["outcome"] != "not_checked":
            raise ValueError("resolution trace cache outcome")
        declared_validation = rows[cursor + 1]
        cursor += 2

    discovered_validation = None
    has_discovery = cursor < len(rows) and names[cursor] == "strong_discovery"
    if has_discovery:
        if names[cursor:cursor + 2] != [
            "strong_discovery", "discovered_validation",
        ]:
            raise ValueError("resolution trace discovered validation sequence")
        if rows[cursor]["outcome"] != "found" or rows[cursor + 1]["outcome"] != "matched":
            raise ValueError("resolution trace discovery outcome")
        discovered_validation = rows[cursor + 1]
        cursor += 2

    if cursor >= len(rows) or names[cursor] not in {
        "strong_confirmed", "weak_aggregation",
    }:
        raise ValueError("resolution trace aggregation stage")
    aggregate = rows[cursor]
    cursor += 1
    if names[cursor:] != ["retraction_check", "final_resolution"]:
        raise ValueError("resolution trace terminal sequence")
    retraction = rows[cursor]
    final = rows[cursor + 1]

    final_status = final["detail"]["status"]
    final_via = final["detail"]["via"]
    final_state = final["outcome"]
    if final_state not in _IDENTITY_STATES:
        raise ValueError("resolution trace final identity state")
    if expected_attempt_count is not None and (
        final["detail"]["attempt_count"] != expected_attempt_count
    ):
        raise ValueError("resolution trace final attempt count is inconsistent")

    if declared_validation is not None:
        expected = (
            "matched"
            if final_status == "resolved" and final_state == "resolved_strong_declared"
            else "failed"
            if final_status in {"not_found", "identifier_mismatch"}
            else "inconclusive"
        )
        if (
            declared_validation["outcome"] != expected
            or declared_validation["detail"] != {"status": final_status, "via": final_via}
        ):
            raise ValueError("resolution trace declared validation is inconsistent")
    if discovered_validation is not None and discovered_validation["detail"] != {
        "status": final_status, "via": final_via,
    }:
        raise ValueError("resolution trace discovered validation is inconsistent")

    if aggregate["stage"] == "strong_confirmed":
        if (
            aggregate["outcome"] != "confirmed"
            or final_status != "resolved"
            or aggregate["detail"]["identity_state"] != final_state
            or final_state not in _STRONG_IDENTITY_STATES
            or has_discovery != (final_state == "resolved_strong_discovered")
            or (final_state == "resolved_strong_declared" and not declared_strong)
        ):
            raise ValueError("resolution trace strong confirmation is inconsistent")
    elif (
        aggregate["outcome"] != final_state
        or aggregate["detail"]["status"] != final_status
        or final_state in _STRONG_IDENTITY_STATES
        or has_discovery
    ):
        raise ValueError("resolution trace weak aggregation is inconsistent")

    checked_via = retraction["detail"]["checked_via"]
    retracted = retraction["detail"]["retracted"]
    expected_retraction = (
        "unknown" if checked_via == "none"
        else "retracted" if retracted
        else "not_retracted"
    )
    if (
        retraction["outcome"] != expected_retraction
        or (checked_via == "none" and retracted)
        or (checked_via == "retraction_watch" and not retracted)
    ):
        raise ValueError("resolution trace retraction state is inconsistent")
    return rows


def split_resolution_trace(trace: list[JsonDict]) -> JsonDict:
    rows = [dict(row) for row in trace]
    sections: JsonDict = {"trace": rows}
    for key, stages in TRACE_BUCKET_STAGES.items():
        sections[key] = [row for row in rows if row["stage"] in stages]
    return sections


def replace_resolution_trace(
    conn: sqlite3.Connection,
    ref_id: str,
    rows: list[JsonDict] | None,
) -> None:
    conn.execute("DELETE FROM resolve_trace_state WHERE ref_id=?", (ref_id,))
    if rows is None:
        conn.execute(
            "INSERT INTO resolve_trace_state(ref_id,state,stage_count) VALUES(?,?,0)",
            (ref_id, "trace_not_produced"),
        )
        return
    conn.execute(
        "INSERT INTO resolve_trace_state(ref_id,state,stage_count) VALUES(?,?,?)",
        (ref_id, "produced", len(rows)),
    )
    for order, row in enumerate(rows):
        conn.execute(
            "INSERT INTO resolve_trace_stages(ref_id,stage_order,stage,outcome) "
            "VALUES(?,?,?,?)",
            (ref_id, order, row["stage"], row["outcome"]),
        )
        detail = row["detail"]
        stage = row["stage"]
        if stage in {"declared_present", "strong_discovery"}:
            conn.execute(
                "INSERT INTO resolve_trace_identifier_details("
                "ref_id,stage_order,class_,is_strong,scheme,value) VALUES(?,?,?,?,?,?)",
                (ref_id, order, detail["class_"], int(detail["is_strong"]),
                 detail["scheme"], detail["value"]),
            )
        elif stage == "declared_cache_lookup":
            conn.execute(
                "INSERT INTO resolve_trace_reason_details(ref_id,stage_order,reason) "
                "VALUES(?,?,?)",
                (ref_id, order, detail["reason"]),
            )
        elif stage in {"declared_validation", "discovered_validation"}:
            conn.execute(
                "INSERT INTO resolve_trace_status_details(ref_id,stage_order,status,via) "
                "VALUES(?,?,?,?)",
                (ref_id, order, detail["status"], detail["via"]),
            )
        elif stage == "strong_confirmed":
            conn.execute(
                "INSERT INTO resolve_trace_confirmed_details("
                "ref_id,stage_order,identity_state,resolution_basis) VALUES(?,?,?,?)",
                (ref_id, order, detail["identity_state"], detail["resolution_basis"]),
            )
        elif stage == "weak_aggregation":
            conn.execute(
                "INSERT INTO resolve_trace_weak_details("
                "ref_id,stage_order,status,reference_status_tag,fabrication_risk) "
                "VALUES(?,?,?,?,?)",
                (ref_id, order, detail["status"], detail["reference_status_tag"],
                 detail["fabrication_risk"]),
            )
        elif stage == "retraction_check":
            conn.execute(
                "INSERT INTO resolve_trace_retraction_details("
                "ref_id,stage_order,checked_via,retracted) VALUES(?,?,?,?)",
                (ref_id, order, detail["checked_via"], int(detail["retracted"])),
            )
        else:
            conn.execute(
                "INSERT INTO resolve_trace_final_details("
                "ref_id,stage_order,status,via) VALUES(?,?,?,?)",
                (ref_id, order, detail["status"], detail["via"]),
            )


def _detail_for_stage(
    conn: sqlite3.Connection,
    ref_id: str,
    stage_order: int,
    stage: str,
) -> JsonDict:
    if stage not in TRACE_STAGE_DETAIL_KIND:
        raise RuntimeError("typed resolution trace stage is invalid")
    found: dict[str, sqlite3.Row] = {}
    for kind, table in TRACE_DETAIL_TABLES.items():
        row = conn.execute(
            f"SELECT * FROM {table} WHERE ref_id=? AND stage_order=?",
            (ref_id, stage_order),
        ).fetchone()
        if row is not None:
            found[kind] = row
    expected_kind = TRACE_STAGE_DETAIL_KIND[stage]
    if set(found) != {expected_kind}:
        raise RuntimeError("typed resolution trace detail kind is inconsistent")
    row = found[expected_kind]
    detail: JsonDict = {}
    for key in TRACE_DETAIL_KEYS[stage]:
        if key == "attempt_count":
            continue
        value = row[key]
        if key in {"is_strong", "retracted"}:
            if type(value) is not int or value not in (0, 1):
                raise RuntimeError("typed resolution trace boolean is invalid")
            value = bool(value)
        detail[key] = value
    return detail


def read_resolution_trace(
    conn: sqlite3.Connection,
    ref_id: str,
    *,
    expected_attempt_count: int | None = None,
) -> list[JsonDict] | None:
    state = conn.execute(
        "SELECT state,stage_count FROM resolve_trace_state WHERE ref_id=?",
        (ref_id,),
    ).fetchone()
    if state is None:
        raise RuntimeError("typed resolution trace state is missing")
    stage_rows = conn.execute(
        "SELECT ref_id,stage_order,stage,outcome FROM resolve_trace_stages "
        "WHERE ref_id=? ORDER BY stage_order",
        (ref_id,),
    ).fetchall()
    if state["state"] == "trace_not_produced":
        if state["stage_count"] != 0 or stage_rows:
            raise RuntimeError("trace-not-produced state has decision stages")
        for table in TRACE_DETAIL_TABLES.values():
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE ref_id=? LIMIT 1", (ref_id,)
            ).fetchone() is not None:
                raise RuntimeError("trace-not-produced state has details")
        return None
    if state["state"] != "produced":
        raise RuntimeError("typed resolution trace state is invalid")
    if type(expected_attempt_count) is not int or expected_attempt_count < 0:
        raise RuntimeError("typed resolution trace attempt count is unavailable")

    rows: list[JsonDict] = []
    for expected_order, row in enumerate(stage_rows):
        if type(row["stage_order"]) is not int or row["stage_order"] != expected_order:
            raise RuntimeError("typed resolution trace order is sparse")
        detail = _detail_for_stage(conn, ref_id, expected_order, row["stage"])
        if row["stage"] == "final_resolution":
            detail["attempt_count"] = expected_attempt_count
        rows.append({
            "stage": row["stage"],
            "outcome": row["outcome"],
            "detail": detail,
            "at": f"stage_{expected_order + 1:02d}",
        })
    if type(state["stage_count"]) is not int or len(rows) != state["stage_count"]:
        raise RuntimeError("typed resolution trace count is inconsistent")
    try:
        return validate_resolution_trace(
            rows, expected_attempt_count=expected_attempt_count,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeError("typed resolution trace is invalid") from exc
