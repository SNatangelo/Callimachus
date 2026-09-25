#!/usr/bin/env python3
# core/infra/startup_preflight.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Lightweight startup preflight for optional API-backed features."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from core.fetch.transport.http_headers import open_request, request_headers
except ImportError:  # direct execution
    from http_headers import open_request, request_headers

ENV_GBOOKS = "GOOGLE_BOOKS_API_KEY"
ENV_CORE = "CORE_API_KEY"
ENV_STATE_DIR = "CITATION_VERIFIER_STATE_DIR"
TIMEOUT = 15
DISABLED_KEYS_FILE = "disabled_api_keys.tsv"
KEY_SPECS = (
    ("googlebooks", "Google Books", ENV_GBOOKS),
    ("core", "CORE", ENV_CORE),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    override = (env.get(ENV_STATE_DIR) or "").strip()
    if override:
        return os.path.abspath(override)
    if os.name == "nt":
        base = (env.get("APPDATA") or "").strip()
        if base:
            return os.path.join(base, "CitationVerifier")
    base = (env.get("XDG_CONFIG_HOME") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "citation-verifier")


def _disabled_keys_path(environ: dict[str, str] | None = None) -> str:
    return os.path.join(_state_dir(environ), DISABLED_KEYS_FILE)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_disabled(environ: dict[str, str] | None = None) -> dict:
    path = _disabled_keys_path(environ)
    try:
        with open(path, encoding="utf-8") as f:
            out = {}
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                env_name, fingerprint, reason, recorded_at = (line.split("\t", 3) + ["", "", "", ""])[:4]
                out[env_name] = {
                    "fingerprint": fingerprint,
                    "reason": reason,
                    "recorded_at": recorded_at,
                }
        return out
    except Exception:
        return {}


def _save_disabled(data: dict, environ: dict[str, str] | None = None) -> None:
    path = _disabled_keys_path(environ)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for env_name in sorted(data):
            row = data.get(env_name) or {}
            f.write(
                "\t".join(
                    [
                        env_name,
                        str(row.get("fingerprint") or ""),
                        str(row.get("reason") or ""),
                        str(row.get("recorded_at") or ""),
                    ]
                )
                + "\n"
            )


def apply_quarantine(environ: dict[str, str] | None = None) -> list[dict]:
    env = environ or os.environ
    data = _load_disabled(env)
    changed = False
    rows = []
    for name, label, env_name in KEY_SPECS:
        raw = (env.get(env_name) or "").strip()
        record = data.get(env_name)
        if not record or not raw:
            continue
        if record.get("fingerprint") == _fingerprint(raw):
            env.pop(env_name, None)
            rows.append(
                {
                    "name": name,
                    "label": label,
                    "env": env_name,
                    "status": "disabled_invalid_cached",
                    "http_status": None,
                    "message": "matches a previously invalid key; update the env value to re-enable",
                    "blocking": True,
                }
            )
            continue
        data.pop(env_name, None)
        changed = True
    if changed:
        _save_disabled(data, env)
    return rows


def quarantine_invalid_rows(rows: list[dict], environ: dict[str, str] | None = None) -> None:
    env = environ or os.environ
    data = _load_disabled(env)
    changed = False
    by_name = {name: (label, env_name) for name, label, env_name in KEY_SPECS}
    for row in rows or []:
        if row.get("status") != "invalid_key":
            continue
        spec = by_name.get(row.get("name"))
        if not spec:
            continue
        _label, env_name = spec
        raw = (env.get(env_name) or "").strip()
        if not raw:
            continue
        data[env_name] = {
            "fingerprint": _fingerprint(raw),
            "reason": "invalid_key",
            "recorded_at": _now(),
        }
        env.pop(env_name, None)
        changed = True
    if changed:
        _save_disabled(data, env)


def _request(url: str, *, headers_extra: dict[str, str] | None = None) -> dict:
    headers = request_headers(url=url, accept="application/json", profile="api")
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(url, headers=headers)
    try:
        with open_request(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace")
            return {
                "status": getattr(r, "status", None),
                "headers": dict(r.headers.items()),
                "body": body,
            }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return {
            "status": e.code,
            "headers": dict(e.headers.items()),
            "body": body,
            "http_error": True,
        }
    except (TimeoutError, socket.timeout) as e:
        return {"status": None, "headers": {}, "body": "", "error": type(e).__name__}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        return {
            "status": None,
            "headers": {},
            "body": "",
            "error": type(reason).__name__ if reason is not None else type(e).__name__,
        }
    except Exception as e:
        return {"status": None, "headers": {}, "body": "", "error": type(e).__name__}


def _extract_message(body: str) -> str | None:
    body = (body or "").strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return body[:240]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        msg = data.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return body[:240]


def _googlebooks_probe(key: str) -> dict:
    params = {
        "q": "intitle:test",
        "maxResults": "1",
        "key": key,
    }
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    result = _request(url)
    return _classify_result(
        "googlebooks",
        "Google Books",
        ENV_GBOOKS,
        result,
    )


def _core_probe(key: str) -> dict:
    params = {
        "q": 'doi:"10.1038/nature12373"',
        "limit": "1",
    }
    url = "https://api.core.ac.uk/v3/search/works/?" + urllib.parse.urlencode(params)
    result = _request(url, headers_extra={"Authorization": f"Bearer {key}"})
    return _classify_result(
        "core",
        "CORE",
        ENV_CORE,
        result,
    )


def _classify_result(name: str, label: str, env_name: str, result: dict) -> dict:
    status = result.get("status")
    message = _extract_message(result.get("body") or "")
    text = (message or "").lower()
    row = {
        "name": name,
        "label": label,
        "env": env_name,
        "status": "valid",
        "http_status": status,
        "message": message,
        "blocking": False,
    }
    if status and 200 <= int(status) < 300:
        return row
    if "api key not valid" in text or "api_key_invalid" in text or "not valid" in text:
        row["status"] = "invalid_key"
        row["blocking"] = True
        return row
    if status == 429 or "quota" in text or "rate limit" in text:
        row["status"] = "quota_or_rate_limited"
        row["blocking"] = True
        return row
    if status in (400, 401, 403):
        row["status"] = "auth_rejected_unknown"
        row["blocking"] = True
        return row
    if result.get("error"):
        row["status"] = "transient_error"
        row["message"] = result.get("error")
        return row
    row["status"] = "transient_error"
    return row


def key_status_rows(environ: dict[str, str] | None = None, *, probe_fn=None) -> list[dict]:
    env = environ or os.environ
    probe = probe_fn or _probe_key
    rows = []
    for name, label, env_name in KEY_SPECS:
        key = (env.get(env_name) or "").strip()
        if not key:
            rows.append(
                {
                    "name": name,
                    "label": label,
                    "env": env_name,
                    "status": "absent",
                    "http_status": None,
                    "message": None,
                    "blocking": False,
                }
            )
            continue
        rows.append(probe(name, key))
    return rows


def _probe_key(name: str, key: str) -> dict:
    if name == "googlebooks":
        return _googlebooks_probe(key)
    if name == "core":
        return _core_probe(key)
    raise ValueError(name)


def describe_row(row: dict) -> str:
    status = row.get("status")
    http_status = row.get("http_status")
    message = row.get("message")
    if status == "absent":
        return f"absent ({row.get('env')} not set)"
    if status == "disabled_invalid_cached":
        return "disabled (matches a previously invalid key; update env to re-enable)"
    if status == "valid":
        return "valid"
    if status == "invalid_key":
        return f"invalid key (HTTP {http_status}: {message})" if http_status else "invalid key"
    if status == "quota_or_rate_limited":
        return (
            f"quota/rate limited (HTTP {http_status}: {message})"
            if http_status
            else "quota/rate limited"
        )
    if status == "auth_rejected_unknown":
        return (
            f"authentication rejected (HTTP {http_status}: {message or 'unspecified'})"
            if http_status
            else "authentication rejected"
        )
    if status == "transient_error":
        return f"transient error ({message or 'network/timeout'})"
    return str(status)
