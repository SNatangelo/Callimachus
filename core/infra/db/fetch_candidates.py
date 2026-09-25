# core/infra/db/fetch_candidates.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Typed, immutable storage for deadline-resumable Fetch candidates."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.shared.typed_canonical import TypedCanonicalError, encode

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCALARS = ("content_version", "fallback_stage", "candidate_key", "discovery_reason", "discovered_via", "referer", "profile", "fetch_profile", "strategy", "fetch_strategy")
_BOOLEANS = ("official_arxiv_html",)
_CITED_LANDING = ("cited_landing_pdf", "cited_landing_url")
_CONTEXT = ("title", "year", "provider", "provider_record_id", "first_author", "source_confidence", "canonical_host", "canonical_url", "landing_page_url", "expected_document_title", "official", "official_document_relation", "authors", "identifiers")
_STAGES = {"published", "oa_alternate", "preprint", "perma", "internet_archive_item", "wayback"}

def _now() -> str: return datetime.now(timezone.utc).isoformat()
def _hash(v: Any) -> str:
    try: return hashlib.sha256(encode(v)).hexdigest()
    except TypedCanonicalError as exc: raise ValueError("candidate facts are not canonical") from exc

def fingerprints(ref_id: str, context: dict[str, Any]) -> tuple[str, str]:
    """Return the closed context and plan bindings used by frozen candidates."""
    _text(ref_id, "ref_id")
    if not isinstance(context, dict):
        raise ValueError("context must be a dict")
    context_fingerprint = _hash(context)
    return context_fingerprint, _hash({"version": 1, "ref_id": ref_id, "context_fingerprint": context_fingerprint})
def _text(v: Any, name: str) -> None:
    if not isinstance(v, str) or not v: raise ValueError(f"{name} must be non-empty text")
def _context(c: Any) -> dict[str, Any]:
    if not isinstance(c, dict) or set(c)-set(_CONTEXT): raise ValueError("identity context has unknown or invalid fields")
    for k in ("title","provider","provider_record_id","first_author","canonical_url","landing_page_url","expected_document_title","official_document_relation"):
        if k in c and c[k] is not None and not isinstance(c[k],str): raise ValueError(f"identity context {k} invalid")
    if "year" in c and c["year"] is not None and (type(c["year"]) not in (int,str)): raise ValueError("identity context year invalid")
    if "source_confidence" in c and c["source_confidence"] is not None and type(c["source_confidence"]) not in (int,float): raise ValueError("identity context source_confidence invalid")
    for k in ("canonical_host","official"):
        if k in c and type(c[k]) is not bool: raise ValueError(f"identity context {k} invalid")
    if "authors" in c and (not isinstance(c["authors"],list) or any(not isinstance(x,str) or not x for x in c["authors"])): raise ValueError("identity context authors invalid")
    if "identifiers" in c and (not isinstance(c["identifiers"],dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in c["identifiers"].items())): raise ValueError("identity context identifiers invalid")
    return dict(c)
def _candidate(v: Any) -> dict[str, Any]:
    keys={"method","url","kind","queue_index","batch_index",*_SCALARS,*_BOOLEANS,*_CITED_LANDING,"identity_context_conflict","url_aliases","candidate_keys","provenance","discovery_reasons","identity_context","identity_contexts"}
    if not isinstance(v,dict) or set(v)-keys: raise ValueError("candidate has unknown or invalid fields")
    for k in ("method","url","kind"): _text(v.get(k),k)
    for k in ("queue_index","batch_index"):
        if type(v.get(k)) is not int or v[k]<0: raise ValueError(f"{k} invalid")
    for k in _SCALARS:
        if k in v and v[k] is not None and not isinstance(v[k],str): raise ValueError(f"{k} invalid")
    for k in _BOOLEANS:
        if k in v and type(v[k]) is not bool: raise ValueError(f"{k} invalid")
    cited_landing = [k in v for k in _CITED_LANDING]
    if any(cited_landing):
        if (
            cited_landing != [True, True]
            or v["cited_landing_pdf"] is not True
            or not isinstance(v["cited_landing_url"], str)
            or not v["cited_landing_url"].strip()
        ):
            raise ValueError("cited landing provenance invalid")
    if "identity_context_conflict" in v and type(v["identity_context_conflict"]) is not bool: raise ValueError("identity_context_conflict invalid")
    for k in ("url_aliases","candidate_keys","provenance","discovery_reasons"):
        if k in v and (not isinstance(v[k],list) or any(not isinstance(x,str) or not x for x in v[k])): raise ValueError(f"{k} invalid")
    if "identity_context" in v: _context(v["identity_context"])
    if "identity_contexts" in v and (not isinstance(v["identity_contexts"],list) or any(not isinstance(x,dict) for x in v["identity_contexts"])): raise ValueError("identity_contexts invalid")
    for c in v.get("identity_contexts",[]): _context(c)
    return dict(v)
def _projection(c: dict[str,Any]) -> dict[str,Any]: return c

def freeze_stage(conn: sqlite3.Connection, ref_id: str, stage: str, context: dict[str,Any], candidates: list[dict[str,Any]]) -> list[int]:
    _text(ref_id,"ref_id"); _text(stage,"stage")
    if stage not in _STAGES: raise ValueError("unknown frozen candidate stage")
    if not isinstance(context,dict): raise ValueError("context must be a dict")
    items=[_candidate(c) for c in candidates]
    ctx_hash, plan_hash = fingerprints(ref_id, context)
    stage_hash=_hash({"version":1,"stage":stage,"candidates":items})
    plan=conn.execute("SELECT * FROM fetch_candidate_plans WHERE ref_id=?",(ref_id,)).fetchone()
    if plan is not None and (plan["context_fingerprint"]!=ctx_hash or plan["plan_fingerprint"]!=plan_hash): raise ValueError("conflicting frozen candidate plan context")
    if plan is None:
        conn.execute("INSERT INTO fetch_candidate_plans(ref_id,context_fingerprint,plan_fingerprint,created_at) VALUES(?,?,?,?)",(ref_id,ctx_hash,plan_hash,_now()))
        plan=conn.execute("SELECT * FROM fetch_candidate_plans WHERE ref_id=?",(ref_id,)).fetchone()
    pid=int(plan["plan_id"])
    existing=conn.execute("SELECT * FROM fetch_candidate_stage_freezes WHERE plan_id=? AND stage=? AND stage_fingerprint=?",(pid,stage,stage_hash)).fetchone()
    if existing:
        if existing["candidate_count"]!=len(items): raise RuntimeError("conflicting frozen stage")
        rows=list(conn.execute("SELECT frozen_candidate_id FROM fetch_frozen_candidates WHERE stage_freeze_id=? ORDER BY candidate_order",(existing["stage_freeze_id"],)))
        if len(rows)!=len(items): raise RuntimeError("corrupt frozen stage")
        ids = [int(r["frozen_candidate_id"]) for r in rows]
        if [read_candidate(conn, candidate_id) for candidate_id in ids] != items:
            raise RuntimeError("frozen stage replay does not match stored candidates")
        if _hash({"version":1,"stage":stage,"candidates":[read_candidate(conn, candidate_id) for candidate_id in ids]}) != existing["stage_fingerprint"]:
            raise RuntimeError("frozen stage fingerprint mismatch")
        return ids
    generation=conn.execute("SELECT COALESCE(MAX(generation),-1)+1 FROM fetch_candidate_stage_freezes WHERE plan_id=? AND stage=?",(pid,stage)).fetchone()[0]
    conn.execute("INSERT INTO fetch_candidate_stage_freezes(plan_id,stage,generation,candidate_count,stage_fingerprint,created_at) VALUES(?,?,?,?,?,?)",(pid,stage,generation,len(items),stage_hash,_now()))
    sid=int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]); ids=[]
    for order,c in enumerate(items):
        fp=_hash(_projection(c)); vals=[c.get(k) for k in (*_SCALARS, *_BOOLEANS, *_CITED_LANDING)]; pres=[int(k in c) for k in (*_SCALARS, *_BOOLEANS, *_CITED_LANDING)]
        fields = (*_SCALARS, *_BOOLEANS, *_CITED_LANDING)
        conn.execute("""INSERT INTO fetch_frozen_candidates(stage_freeze_id,candidate_order,queue_index,batch_index,method,url,kind,"""+",".join(f"{k},{k}_present" for k in fields)+""",identity_context_conflict,identity_context_conflict_present,url_aliases_present,candidate_keys_present,provenance_present,discovery_reasons_present,primary_context_present,alternate_contexts_present,candidate_fingerprint) VALUES("""+",".join("?" for _ in range(7+2*len(fields)+2+6+1))+")", (sid,order,c["queue_index"],c["batch_index"],c["method"],c["url"],c["kind"],*sum(([v,p] for v,p in zip(vals,pres)),[]),None if "identity_context_conflict" not in c else int(c["identity_context_conflict"]),int("identity_context_conflict" in c),*(int(k in c) for k in ("url_aliases","candidate_keys","provenance","discovery_reasons","identity_context","identity_contexts")),fp))
        cid=int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]); ids.append(cid)
        for family in ("url_aliases","candidate_keys","provenance","discovery_reasons"):
            for i,x in enumerate(c.get(family,[])): conn.execute("INSERT INTO fetch_frozen_candidate_strings VALUES(?,?,?,?)",(cid,family,i,x))
        for family,contexts in (("primary",[c["identity_context"]] if "identity_context" in c else []),("alternate",c.get("identity_contexts",[]))):
            for co,ctx in enumerate(contexts): _insert_context(conn,cid,family,co,ctx)
    return ids

def _insert_context(conn,cid,family,co,c):
    year=c.get("year"); ykind="absent" if "year" not in c else "null" if year is None else "integer" if type(year) is int else "text"
    cols=("title","provider","provider_record_id","first_author","source_confidence","canonical_host","canonical_url","landing_page_url","expected_document_title","official","official_document_relation")
    values=[]
    for k in cols: values.extend([c.get(k),int(k in c)])
    conn.execute("""INSERT INTO fetch_frozen_candidate_contexts(frozen_candidate_id,context_kind,context_order,title,title_present,year_kind,year_integer,year_text,provider,provider_present,provider_record_id,provider_record_id_present,first_author,first_author_present,source_confidence,source_confidence_present,canonical_host,canonical_host_present,canonical_url,canonical_url_present,landing_page_url,landing_page_url_present,expected_document_title,expected_document_title_present,official,official_present,official_document_relation,official_document_relation_present,authors_present,identifiers_present) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(cid,family,co,c.get("title"),int("title" in c),ykind,year if ykind=="integer" else None,year if ykind=="text" else None,*values[2:],int("authors" in c),int("identifiers" in c)))
    for i,x in enumerate(c.get("authors",[])): conn.execute("INSERT INTO fetch_frozen_candidate_context_authors VALUES(?,?,?,?,?)",(cid,family,co,i,x))
    for i,(k,v) in enumerate(c.get("identifiers",{}).items()): conn.execute("INSERT INTO fetch_frozen_candidate_context_identifiers VALUES(?,?,?,?,?,?)",(cid,family,co,i,k,v))

_EVENT_TYPES = {
    "admitted",
    "deadline_skipped",
    "retry_claimed",
    "retry_deferred",
    "retry_completed",
    "retry_invalidated",
}


def _attempt_belongs(
    conn: sqlite3.Connection, candidate_id: int, fetch_attempt_id: int | None
) -> bool:
    if type(fetch_attempt_id) is not int or fetch_attempt_id <= 0:
        return False
    return conn.execute(
        """
        SELECT 1
        FROM fetch_frozen_candidates AS c
        JOIN fetch_candidate_stage_freezes AS s
          ON s.stage_freeze_id = c.stage_freeze_id
        JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
        JOIN fetch_attempts AS a ON a.fetch_attempt_id = ?
        JOIN fetch_execution_traces AS t ON t.fetch_attempt_id = a.fetch_attempt_id
        WHERE c.frozen_candidate_id = ?
          AND a.ref_id = p.ref_id
          AND t.frozen_candidate_id_present = 1
          AND t.frozen_candidate_id = c.frozen_candidate_id
        """,
        (fetch_attempt_id, candidate_id),
    ).fetchone() is not None


def _attempt_has_no_request(
    conn: sqlite3.Connection, candidate_id: int, fetch_attempt_id: int | None
) -> bool:
    if not _attempt_belongs(conn, candidate_id, fetch_attempt_id):
        return False
    return conn.execute(
        """
        SELECT 1
        FROM fetch_execution_traces
        WHERE fetch_attempt_id = ?
          AND frozen_candidate_id_present = 1
          AND frozen_candidate_id = ?
          AND request_present = 1
          AND request = 'none'
        """,
        (fetch_attempt_id, candidate_id),
    ).fetchone() is not None


def _attempt_is_host_cooldown_deferred(
    conn: sqlite3.Connection, candidate_id: int, fetch_attempt_id: int | None
) -> bool:
    if not _attempt_belongs(conn, candidate_id, fetch_attempt_id):
        return False
    return conn.execute(
        """
        SELECT 1
        FROM fetch_execution_traces
        WHERE fetch_attempt_id = ?
          AND frozen_candidate_id_present = 1
          AND frozen_candidate_id = ?
          AND outcome = 'rate_limit_deferred'
          AND request_present = 1
          AND request = 'none'
          AND reason_code_present = 1
          AND reason_code = 'host_cooldown'
        """,
        (fetch_attempt_id, candidate_id),
    ).fetchone() is not None


def _attempt_is_rate_limit_response(
    conn: sqlite3.Connection, candidate_id: int, fetch_attempt_id: int | None,
) -> bool:
    if not _attempt_belongs(conn, candidate_id, fetch_attempt_id):
        return False
    return conn.execute(
        """
        SELECT 1 FROM fetch_execution_traces
        WHERE fetch_attempt_id = ?
          AND frozen_candidate_id_present = 1
          AND frozen_candidate_id = ?
          AND outcome = 'rate_limit_deferred'
          AND status_present = 1 AND status = 429
          AND reason_code_present = 1 AND reason_code = 'rate_limit_response'
        """,
        (fetch_attempt_id, candidate_id),
    ).fetchone() is not None


def _has_bound_execution_trace(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Whether any persisted execution trace is bound to this exact candidate."""
    return conn.execute(
        """
        SELECT 1
        FROM fetch_frozen_candidates AS c
        JOIN fetch_candidate_stage_freezes AS s
          ON s.stage_freeze_id = c.stage_freeze_id
        JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
        JOIN fetch_attempts AS a ON a.ref_id = p.ref_id
        JOIN fetch_execution_traces AS t ON t.fetch_attempt_id = a.fetch_attempt_id
        WHERE c.frozen_candidate_id = ?
          AND t.frozen_candidate_id_present = 1
          AND t.frozen_candidate_id = c.frozen_candidate_id
        """,
        (candidate_id,),
    ).fetchone() is not None


def _require_event_shape(
    event_type: str,
    fetch_attempt_id: int | None,
    reason_code: str | None,
    reason_detail: str | None,
) -> None:
    if event_type in {"admitted", "retry_claimed"}:
        if fetch_attempt_id is not None or reason_code is not None or reason_detail is not None:
            raise ValueError(f"{event_type} has invalid event facts")
        return
    if event_type == "deadline_skipped":
        if (
            type(fetch_attempt_id) is not int
            or fetch_attempt_id <= 0
            or reason_code != "deadline_exceeded"
            or not isinstance(reason_detail, str)
            or not reason_detail
        ):
            raise ValueError(f"{event_type} has invalid event facts")
        return
    if event_type == "retry_completed":
        if (
            type(fetch_attempt_id) is not int
            or fetch_attempt_id <= 0
            or reason_code is not None
            or reason_detail is not None
        ):
            raise ValueError(f"{event_type} has invalid event facts")
        return
    if event_type == "retry_deferred":
        if (
            type(fetch_attempt_id) is not int
            or fetch_attempt_id <= 0
            or reason_code not in {"host_cooldown", "rate_limit_response"}
            or not isinstance(reason_detail, str)
            or not reason_detail
        ):
            raise ValueError("retry_deferred has invalid event facts")
        return
    if event_type == "retry_invalidated":
        if (
            (fetch_attempt_id is not None and (
                type(fetch_attempt_id) is not int or fetch_attempt_id <= 0
            ))
            or not isinstance(reason_code, str)
            or not reason_code
            or (reason_detail is not None and (not isinstance(reason_detail, str) or not reason_detail))
        ):
            raise ValueError("retry_invalidated has invalid event facts")
        return
    raise ValueError("event_type invalid")


def add_event(
    conn: sqlite3.Connection,
    candidate_id: int,
    event_type: str,
    *,
    fetch_attempt_id: int | None = None,
    reason_code: str | None = None,
    reason_detail: str | None = None,
) -> bool:
    """Append one valid lifecycle event; claims use the atomic claim boundary."""
    if type(candidate_id) is not int or candidate_id <= 0:
        raise ValueError("candidate_id invalid")
    if event_type not in _EVENT_TYPES:
        raise ValueError("event_type invalid")
    _require_event_shape(event_type, fetch_attempt_id, reason_code, reason_detail)
    if conn.execute(
        "SELECT 1 FROM fetch_frozen_candidates WHERE frozen_candidate_id = ?", (candidate_id,)
    ).fetchone() is None:
        raise ValueError("frozen candidate missing")

    if event_type == "retry_claimed":
        if claim(conn, candidate_id):
            return True
        last = conn.execute(
            """
            SELECT event_type FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ? ORDER BY event_id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if last is not None and last["event_type"] == "retry_claimed":
            return False
        raise ValueError("claim requires an eligible frozen candidate")

    existing = conn.execute(
        """
        SELECT fetch_attempt_id, reason_code, reason_detail
        FROM fetch_frozen_candidate_events
        WHERE frozen_candidate_id = ? AND event_type = ?
        """,
        (candidate_id, event_type),
    ).fetchone()
    if existing is not None and event_type not in {"retry_claimed", "retry_deferred"}:
        if tuple(existing) == (fetch_attempt_id, reason_code, reason_detail):
            return False
        raise ValueError("conflicting frozen candidate event")

    facts = {
        event["event_type"]
        for event in conn.execute(
            "SELECT event_type FROM fetch_frozen_candidate_events WHERE frozen_candidate_id = ?",
            (candidate_id,),
        )
    }
    terminal = {"retry_completed", "retry_invalidated"}
    if facts & terminal:
        raise ValueError("no event may follow a terminal retry event")
    if event_type == "admitted":
        if facts & {"deadline_skipped", "retry_claimed"}:
            raise ValueError("admission must precede deadline and retry events")
    elif event_type == "deadline_skipped":
        if facts & {"retry_claimed", "retry_completed", "retry_invalidated"}:
            raise ValueError("deadline skip cannot follow retry events")
        if not _attempt_belongs(conn, candidate_id, fetch_attempt_id):
            raise ValueError("attempt does not belong to frozen candidate reference")
    elif event_type == "retry_deferred":
        previous = conn.execute(
            """
            SELECT event_type
            FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ?
            ORDER BY event_id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if previous is None or previous["event_type"] != "retry_claimed":
            raise ValueError("deferral requires a current retry claim")
        deferred_ok = (
            _attempt_is_host_cooldown_deferred(conn, candidate_id, fetch_attempt_id)
            if reason_code == "host_cooldown"
            else _attempt_is_rate_limit_response(conn, candidate_id, fetch_attempt_id)
        )
        if not deferred_ok:
            required_trace = (
                "host cooldown" if reason_code == "host_cooldown"
                else "rate-limit response"
            )
            raise ValueError(f"deferred retry requires an exact {required_trace} trace")
    elif event_type == "retry_completed":
        previous = conn.execute(
            """
            SELECT event_type
            FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ?
            ORDER BY event_id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if previous is None or previous["event_type"] != "retry_claimed":
            raise ValueError("completion requires a claim and no invalidation")
        if not _attempt_belongs(conn, candidate_id, fetch_attempt_id):
            raise ValueError("attempt does not belong to frozen candidate reference")
        if _attempt_has_no_request(conn, candidate_id, fetch_attempt_id):
            raise ValueError("completion requires a request-bearing trace")
    elif event_type == "retry_invalidated":
        previous = conn.execute(
            """
            SELECT event_type
            FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ?
            ORDER BY event_id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if previous is None or previous["event_type"] not in {
            "admitted", "deadline_skipped", "retry_deferred", "retry_claimed",
        }:
            raise ValueError("invalidation requires an unclaimed retry candidate")
        if previous["event_type"] == "retry_claimed":
            if fetch_attempt_id is None or not _attempt_has_no_request(
                conn, candidate_id, fetch_attempt_id
            ):
                raise ValueError("claimed invalidation requires an unissued request trace")
        elif fetch_attempt_id is not None:
            raise ValueError("unclaimed invalidation cannot name an attempt")
        if previous["event_type"] == "admitted" and _has_bound_execution_trace(
            conn, candidate_id
        ):
            raise ValueError("admission invalidation requires no bound execution trace")

    try:
        conn.execute(
            """
            INSERT INTO fetch_frozen_candidate_events(
                frozen_candidate_id, event_type, fetch_attempt_id, reason_code,
                reason_detail, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (candidate_id, event_type, fetch_attempt_id, reason_code, reason_detail, _now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("conflicting frozen candidate event") from exc
    return True

def eligible(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("""
        SELECT c.frozen_candidate_id
        FROM fetch_frozen_candidates AS c
        WHERE (
            SELECT e.event_type
            FROM fetch_frozen_candidate_events AS e
            WHERE e.frozen_candidate_id = c.frozen_candidate_id
            ORDER BY e.event_id DESC LIMIT 1
        ) = 'retry_deferred'
        OR (
            (
                SELECT e.event_type
                FROM fetch_frozen_candidate_events AS e
                WHERE e.frozen_candidate_id = c.frozen_candidate_id
                ORDER BY e.event_id DESC LIMIT 1
            ) = 'deadline_skipped'
            AND NOT EXISTS(
                SELECT 1 FROM fetch_frozen_candidate_events AS admitted
                WHERE admitted.frozen_candidate_id = c.frozen_candidate_id
                  AND admitted.event_type = 'admitted'
            )
        )
        OR (
            (
                SELECT e.event_type
                FROM fetch_frozen_candidate_events AS e
                WHERE e.frozen_candidate_id = c.frozen_candidate_id
                ORDER BY e.event_id DESC LIMIT 1
            ) = 'admitted'
            AND NOT EXISTS(
                SELECT 1
                FROM fetch_frozen_candidates AS bound
                JOIN fetch_candidate_stage_freezes AS s
                  ON s.stage_freeze_id = bound.stage_freeze_id
                JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
                JOIN fetch_attempts AS a ON a.ref_id = p.ref_id
                JOIN fetch_execution_traces AS t
                  ON t.fetch_attempt_id = a.fetch_attempt_id
                WHERE bound.frozen_candidate_id = c.frozen_candidate_id
                  AND t.frozen_candidate_id_present = 1
                  AND t.frozen_candidate_id = c.frozen_candidate_id
            )
        )
        ORDER BY c.frozen_candidate_id
    """)]
def claim(conn: sqlite3.Connection,candidate_id:int) -> bool:
    # Conditional insert is the concurrency boundary; never pre-read eligibility.
    cur=conn.execute("""INSERT OR IGNORE INTO fetch_frozen_candidate_events(frozen_candidate_id,event_type,fetch_attempt_id,reason_code,reason_detail,created_at)
      SELECT ?, 'retry_claimed', NULL, NULL, NULL, ?
      WHERE (
          SELECT event_type
          FROM fetch_frozen_candidate_events
          WHERE frozen_candidate_id = ?
          ORDER BY event_id DESC LIMIT 1
      ) = 'retry_deferred'
      OR (
          (
              SELECT event_type
              FROM fetch_frozen_candidate_events
              WHERE frozen_candidate_id = ?
              ORDER BY event_id DESC LIMIT 1
          ) = 'deadline_skipped'
          AND NOT EXISTS(
              SELECT 1 FROM fetch_frozen_candidate_events
              WHERE frozen_candidate_id = ? AND event_type = 'admitted'
          )
      )
      OR (
          (
              SELECT event_type
              FROM fetch_frozen_candidate_events
              WHERE frozen_candidate_id = ?
              ORDER BY event_id DESC LIMIT 1
          ) = 'admitted'
          AND NOT EXISTS(
              SELECT 1
              FROM fetch_frozen_candidates AS c
              JOIN fetch_candidate_stage_freezes AS s
                ON s.stage_freeze_id = c.stage_freeze_id
              JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
              JOIN fetch_attempts AS a ON a.ref_id = p.ref_id
              JOIN fetch_execution_traces AS t
                ON t.fetch_attempt_id = a.fetch_attempt_id
              WHERE c.frozen_candidate_id = ?
                AND t.frozen_candidate_id_present = 1
                AND t.frozen_candidate_id = c.frozen_candidate_id
          )
      )""",(candidate_id,_now(),candidate_id,candidate_id,candidate_id,candidate_id,candidate_id))
    return cur.rowcount == 1

def admit_batch(conn: sqlite3.Connection, candidate_ids: list[int]) -> bool:
    if not candidate_ids or any(type(x) is not int or x <= 0 for x in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids): raise ValueError("candidate batch invalid")
    marks=",".join("?" for _ in candidate_ids)
    rows=list(conn.execute(f"""SELECT c.frozen_candidate_id,c.queue_index,c.batch_index,c.stage_freeze_id,s.stage,p.plan_id
        FROM fetch_frozen_candidates c JOIN fetch_candidate_stage_freezes s ON s.stage_freeze_id=c.stage_freeze_id
        JOIN fetch_candidate_plans p ON p.plan_id=s.plan_id WHERE c.frozen_candidate_id IN ({marks})""",candidate_ids))
    if len(rows)!=len(candidate_ids) or len({(r['plan_id'],r['stage'],r['stage_freeze_id'],r['batch_index']) for r in rows})!=1: raise ValueError("candidate batch is not coherent")
    all_ids={r[0] for r in conn.execute("""SELECT frozen_candidate_id FROM fetch_frozen_candidates WHERE stage_freeze_id=? AND batch_index=?""",(rows[0]['stage_freeze_id'],rows[0]['batch_index']))}
    if set(candidate_ids)!=all_ids: raise ValueError("candidate batch is incomplete")
    changed=False
    for ident in candidate_ids: changed=add_event(conn,ident,"admitted") or changed
    return changed

def read_candidate(conn: sqlite3.Connection, candidate_id: int) -> dict[str, Any]:
    row=conn.execute("SELECT * FROM fetch_frozen_candidates WHERE frozen_candidate_id=?",(candidate_id,)).fetchone()
    if row is None: raise RuntimeError("frozen candidate missing")
    c={k:row[k] for k in ("method","url","kind","queue_index","batch_index")}
    for k in _SCALARS:
        present = row[f"{k}_present"]
        if present not in (0, 1):
            raise RuntimeError("candidate scalar presence corrupt")
        if not present and row[k] is not None:
            raise RuntimeError("candidate scalar presence corrupt")
        if present:
            c[k] = row[k]
    for k in _BOOLEANS:
        present = row[f"{k}_present"]
        if present not in (0, 1) or (not present and row[k] is not None):
            raise RuntimeError("candidate boolean presence corrupt")
        if present:
            if row[k] not in (0, 1): raise RuntimeError("candidate boolean presence corrupt")
            c[k] = bool(row[k])
    cited_landing_presence = [row[f"{k}_present"] for k in _CITED_LANDING]
    if cited_landing_presence == [0, 0]:
        if row["cited_landing_pdf"] is not None or row["cited_landing_url"] is not None:
            raise RuntimeError("candidate cited landing provenance corrupt")
    elif cited_landing_presence == [1, 1]:
        if row["cited_landing_pdf"] != 1 or not isinstance(row["cited_landing_url"], str) or not row["cited_landing_url"].strip():
            raise RuntimeError("candidate cited landing provenance corrupt")
        c["cited_landing_pdf"] = True
        c["cited_landing_url"] = row["cited_landing_url"]
    else:
        raise RuntimeError("candidate cited landing provenance corrupt")
    conflict_present = row["identity_context_conflict_present"]
    if conflict_present not in (0, 1):
        raise RuntimeError("candidate conflict presence corrupt")
    if conflict_present:
        if row["identity_context_conflict"] not in (0, 1):
            raise RuntimeError("candidate conflict presence corrupt")
        c["identity_context_conflict"] = bool(row["identity_context_conflict"])
    elif row["identity_context_conflict"] is not None:
        raise RuntimeError("candidate conflict presence corrupt")
    for family in ("url_aliases","candidate_keys","provenance","discovery_reasons"):
        rows=list(conn.execute("SELECT item_order,value FROM fetch_frozen_candidate_strings WHERE frozen_candidate_id=? AND family=? ORDER BY item_order",(candidate_id,family)))
        if [x["item_order"] for x in rows]!=list(range(len(rows))): raise RuntimeError("candidate strings have sparse ordinals")
        if row[f"{family}_present"]: c[family]=[x["value"] for x in rows]
        elif rows: raise RuntimeError("unexpected candidate strings")
    contexts={"primary":[],"alternate":[]}
    for ctx in conn.execute("SELECT * FROM fetch_frozen_candidate_contexts WHERE frozen_candidate_id=? ORDER BY context_kind,context_order",(candidate_id,)):
        d={}; family=ctx["context_kind"]
        if family not in contexts:
            raise RuntimeError("unknown context family")
        for k in ("title","provider","provider_record_id","first_author","source_confidence","canonical_url","landing_page_url","expected_document_title","official_document_relation"):
            present = ctx[f"{k}_present"]
            if present not in (0, 1):
                raise RuntimeError("context scalar presence corrupt")
            if present: d[k]=ctx[k]
            elif ctx[k] is not None: raise RuntimeError("context scalar presence corrupt")
        if ctx["year_kind"]=="null": d["year"]=None
        elif ctx["year_kind"]=="integer": d["year"]=ctx["year_integer"]
        elif ctx["year_kind"]=="text": d["year"]=ctx["year_text"]
        elif ctx["year_kind"]!="absent": raise RuntimeError("context year corrupt")
        for k in ("canonical_host","official"):
            present = ctx[f"{k}_present"]
            if present not in (0, 1):
                raise RuntimeError("context boolean presence corrupt")
            if present:
                if ctx[k] not in (0, 1):
                    raise RuntimeError("context boolean presence corrupt")
                d[k]=bool(ctx[k])
            elif ctx[k] is not None:
                raise RuntimeError("context boolean presence corrupt")
        authors=list(conn.execute("SELECT author_order,author FROM fetch_frozen_candidate_context_authors WHERE frozen_candidate_id=? AND context_kind=? AND context_order=? ORDER BY author_order",(candidate_id,family,ctx["context_order"])))
        if [x["author_order"] for x in authors]!=list(range(len(authors))): raise RuntimeError("context authors have sparse ordinals")
        if ctx["authors_present"]: d["authors"]=[x["author"] for x in authors]
        elif authors: raise RuntimeError("unexpected context authors")
        ident=list(conn.execute("SELECT identifier_order,identifier_type,identifier_value FROM fetch_frozen_candidate_context_identifiers WHERE frozen_candidate_id=? AND context_kind=? AND context_order=? ORDER BY identifier_order",(candidate_id,family,ctx["context_order"])))
        if [x["identifier_order"] for x in ident]!=list(range(len(ident))): raise RuntimeError("context identifiers have sparse ordinals")
        if ctx["identifiers_present"]: d["identifiers"]={x["identifier_type"]:x["identifier_value"] for x in ident}
        elif ident: raise RuntimeError("unexpected context identifiers")
        contexts[family].append((ctx["context_order"], d))
    for family in contexts:
        if [order for order, _ in contexts[family]] != list(range(len(contexts[family]))):
            raise RuntimeError("context rows have sparse ordinals")
        contexts[family] = [context for _, context in contexts[family]]
        presence_column = (
            "primary_context_present"
            if family == "primary"
            else "alternate_contexts_present"
        )
        if row[presence_column] not in (0, 1):
            raise RuntimeError("context presence corrupt")
        expected=1 if family=="primary" and row["primary_context_present"] else 0
        if family=="primary" and len(contexts[family])!=expected: raise RuntimeError("primary context presence corrupt")
        if family=="alternate" and not row["alternate_contexts_present"] and contexts[family]: raise RuntimeError("unexpected alternate contexts")
    if row["primary_context_present"]: c["identity_context"]=contexts["primary"][0]
    if row["alternate_contexts_present"]: c["identity_contexts"]=contexts["alternate"]
    _candidate(c)
    if _hash(_projection(c))!=row["candidate_fingerprint"]: raise RuntimeError("frozen candidate fingerprint mismatch")
    return c

def _read_events(
    conn: sqlite3.Connection, candidate_id: int, ref_id: str
) -> list[dict[str, Any]]:
    events = list(
        conn.execute(
            """
            SELECT event_type, fetch_attempt_id, reason_code, reason_detail, created_at
            FROM fetch_frozen_candidate_events
            WHERE frozen_candidate_id = ?
            ORDER BY event_id
            """,
            (candidate_id,),
        )
    )
    seen: set[str] = set()
    terminal = {"retry_completed", "retry_invalidated"}
    for event_index, event in enumerate(events):
        event_type = event["event_type"]
        attempt_id = event["fetch_attempt_id"]
        code = event["reason_code"]
        detail = event["reason_detail"]
        created_at = event["created_at"]
        if (
            event_type not in _EVENT_TYPES
            or (event_type in seen and event_type not in {"retry_claimed", "retry_deferred"})
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise RuntimeError("frozen candidate event is corrupt")
        if seen & terminal:
            raise RuntimeError("event follows terminal frozen candidate event")
        try:
            _require_event_shape(event_type, attempt_id, code, detail)
        except ValueError as exc:
            raise RuntimeError("frozen candidate event has invalid facts") from exc
        if event_type == "admitted":
            if seen & {"deadline_skipped", "retry_claimed"}:
                raise RuntimeError("admission follows deadline or retry event")
        elif event_type == "deadline_skipped":
            if seen & {"retry_claimed", "retry_completed", "retry_invalidated"}:
                raise RuntimeError("deadline skip follows retry event")
            if not _attempt_belongs(conn, candidate_id, attempt_id):
                raise RuntimeError("deadline attempt belongs to another reference")
        elif event_type == "retry_claimed":
            if not seen or events[event_index - 1]["event_type"] not in {
                "admitted", "deadline_skipped", "retry_deferred",
            }:
                raise RuntimeError("retry claim has invalid lifecycle")
        elif event_type == "retry_deferred":
            if not seen or events[event_index - 1]["event_type"] != "retry_claimed":
                raise RuntimeError("retry deferral has invalid lifecycle")
            deferred_ok = (
                _attempt_is_host_cooldown_deferred(conn, candidate_id, attempt_id)
                if code == "host_cooldown"
                else _attempt_is_rate_limit_response(conn, candidate_id, attempt_id)
            )
            if not deferred_ok:
                raise RuntimeError("retry deferral lacks a rate-limit trace")
        elif event_type == "retry_completed":
            if not seen or events[event_index - 1]["event_type"] != "retry_claimed":
                raise RuntimeError("retry completion has invalid lifecycle")
            if not _attempt_belongs(conn, candidate_id, attempt_id):
                raise RuntimeError("retry attempt belongs to another reference")
            if _attempt_has_no_request(conn, candidate_id, attempt_id):
                raise RuntimeError("retry completion has no request")
        elif event_type == "retry_invalidated":
            if not seen or events[event_index - 1]["event_type"] not in {
                "admitted", "deadline_skipped", "retry_deferred", "retry_claimed",
            }:
                raise RuntimeError("retry invalidation has invalid lifecycle")
            if events[event_index - 1]["event_type"] == "retry_claimed":
                if not _attempt_has_no_request(conn, candidate_id, attempt_id):
                    raise RuntimeError("claimed retry invalidation issued a request")
            elif attempt_id is not None:
                raise RuntimeError("unclaimed retry invalidation names an attempt")
            if (
                events[event_index - 1]["event_type"] == "admitted"
                and _has_bound_execution_trace(conn, candidate_id)
            ):
                raise RuntimeError(
                    "admission retry invalidation has a bound execution trace"
                )
        seen.add(event_type)
    return [dict(event) for event in events]


def lifecycle_summaries(
    conn: sqlite3.Connection, ref_id: str
) -> list[dict[str, Any]]:
    """Read validated terminal/current lifecycle facts without decoding a candidate.

    This deliberately does not call :func:`read_candidate`: an invalidated record
    must remain visible to the manual audit even when its immutable candidate
    payload was corrupted.
    """
    _text(ref_id, "ref_id")
    rows = list(
        conn.execute(
            """
            SELECT c.frozen_candidate_id, p.ref_id, s.stage
            FROM fetch_frozen_candidates AS c
            JOIN fetch_candidate_stage_freezes AS s
              ON s.stage_freeze_id = c.stage_freeze_id
            JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
            WHERE p.ref_id = ?
            ORDER BY c.frozen_candidate_id
            """,
            (ref_id,),
        )
    )
    summaries: list[dict[str, Any]] = []
    for row in rows:
        candidate_id = row["frozen_candidate_id"]
        if (
            type(candidate_id) is not int
            or candidate_id <= 0
            or row["ref_id"] != ref_id
            or row["stage"] not in _STAGES
        ):
            raise RuntimeError("invalid frozen candidate lifecycle record")
        events = _read_events(conn, candidate_id, ref_id)
        if not events:
            continue
        event = events[-1]
        summaries.append(
            {
                "candidate_id": candidate_id,
                "ref_id": ref_id,
                "stage": row["stage"],
                "lifecycle": event["event_type"],
                "reason_code": event["reason_code"],
                "fetch_attempt_id": event["fetch_attempt_id"],
            }
        )
    return summaries


def read_record(conn: sqlite3.Connection, candidate_id: int) -> dict[str, Any]:
    """Read a closed frozen candidate record, rejecting any tampered relation."""
    if type(candidate_id) is not int or candidate_id <= 0:
        raise RuntimeError("frozen candidate id invalid")
    try:
        row = conn.execute(
            """
            SELECT
              p.plan_id, p.ref_id, p.context_fingerprint, p.plan_fingerprint,
              s.stage_freeze_id, s.stage, s.generation, s.candidate_count,
              s.stage_fingerprint, c.candidate_fingerprint
            FROM fetch_frozen_candidates AS c
            JOIN fetch_candidate_stage_freezes AS s
              ON s.stage_freeze_id = c.stage_freeze_id
            JOIN fetch_candidate_plans AS p ON p.plan_id = s.plan_id
            WHERE c.frozen_candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("frozen candidate missing")
        if (
            not isinstance(row["ref_id"], str)
            or not row["ref_id"]
            or row["stage"] not in _STAGES
            or type(row["plan_id"]) is not int
            or type(row["stage_freeze_id"]) is not int
            or type(row["generation"]) is not int
            or row["generation"] < 0
            or type(row["candidate_count"]) is not int
            or row["candidate_count"] < 0
            or not all(
                isinstance(row[key], str) and _HASH.fullmatch(row[key])
                for key in (
                    "context_fingerprint",
                    "plan_fingerprint",
                    "stage_fingerprint",
                    "candidate_fingerprint",
                )
            )
        ):
            raise RuntimeError("invalid frozen candidate record")
        if _hash(
            {
                "version": 1,
                "ref_id": row["ref_id"],
                "context_fingerprint": row["context_fingerprint"],
            }
        ) != row["plan_fingerprint"]:
            raise RuntimeError("frozen candidate plan fingerprint mismatch")
        candidate_rows = list(
            conn.execute(
                """
                SELECT frozen_candidate_id, candidate_order
                FROM fetch_frozen_candidates
                WHERE stage_freeze_id = ?
                ORDER BY candidate_order
                """,
                (row["stage_freeze_id"],),
            )
        )
        if (
            len(candidate_rows) != row["candidate_count"]
            or [candidate["candidate_order"] for candidate in candidate_rows]
            != list(range(len(candidate_rows)))
            or candidate_id not in {candidate["frozen_candidate_id"] for candidate in candidate_rows}
        ):
            raise RuntimeError("frozen stage candidate order is corrupt")
        candidates = [
            read_candidate(conn, candidate["frozen_candidate_id"])
            for candidate in candidate_rows
        ]
        if _hash(
            {"version": 1, "stage": row["stage"], "candidates": candidates}
        ) != row["stage_fingerprint"]:
            raise RuntimeError("frozen stage fingerprint mismatch")
        events = _read_events(conn, candidate_id, row["ref_id"])
    except RuntimeError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise RuntimeError("corrupt frozen candidate record") from exc
    return {
        "candidate_id": candidate_id,
        "candidate": candidates[
            [candidate["frozen_candidate_id"] for candidate in candidate_rows].index(candidate_id)
        ],
        "ref_id": row["ref_id"],
        "stage": row["stage"],
        "generation": row["generation"],
        "context_fingerprint": row["context_fingerprint"],
        "plan_fingerprint": row["plan_fingerprint"],
        "candidate_fingerprint": row["candidate_fingerprint"],
        "events": events,
    }
