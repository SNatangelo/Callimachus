# core/infra/db/verify_setting_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed relational persistence for verification settings."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


CONTRACT_ID = "verify-claim-evidence-v10"
SELECTOR_ALGORITHM = "sha256-counter-v1"
SEED_DERIVATION = "sha256-run-contract-policy-v1"
CURSOR_SEMANTICS = "provider-credential-cycle-and-model-draw-v1"
COOLDOWN_POLICY_VERSION = "cooldown-policy-v1"

RELATIONAL_VERIFY_SETTING_KEYS = frozenset({
    "verify_runtime",
    "verify_claim_evidence_config",
    "verification_lifecycle_failure",
})

VERIFY_SETTING_TABLES = frozenset({
    "verify_runtime_setting",
    "verification_lifecycle_failure_setting",
    "verify_claim_evidence_config",
    "verify_claim_evidence_providers",
    "verify_claim_evidence_credentials",
    "verify_claim_evidence_models",
    "verify_claim_evidence_lanes",
    "verify_claim_evidence_pacing",
    "verify_claim_evidence_jury_roles",
    "verify_claim_evidence_cooldown_overrides",
})

VERIFY_SETTING_TRIGGERS = frozenset({
    "verify_claim_evidence_config_no_update",
    "verify_claim_evidence_config_no_delete",
    "verify_claim_evidence_provider_insert_guard",
    "verify_claim_evidence_provider_no_update",
    "verify_claim_evidence_provider_no_delete",
    "verify_claim_evidence_credential_insert_guard",
    "verify_claim_evidence_credential_no_update",
    "verify_claim_evidence_credential_no_delete",
    "verify_claim_evidence_model_insert_guard",
    "verify_claim_evidence_model_no_update",
    "verify_claim_evidence_model_no_delete",
    "verify_claim_evidence_lane_insert_guard",
    "verify_claim_evidence_lane_no_update",
    "verify_claim_evidence_lane_no_delete",
    "verify_claim_evidence_pacing_insert_guard",
    "verify_claim_evidence_pacing_no_update",
    "verify_claim_evidence_pacing_no_delete",
    "verify_claim_evidence_role_insert_guard",
    "verify_claim_evidence_role_no_update",
    "verify_claim_evidence_role_no_delete",
    "verify_claim_evidence_override_insert_guard",
    "verify_claim_evidence_override_no_update",
    "verify_claim_evidence_override_no_delete",
})


_SHA_CHECK = (
    "length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"
)

VERIFY_SETTING_DDL = f"""
CREATE TABLE IF NOT EXISTS verify_runtime_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  code_revision TEXT CHECK(code_revision IS NULL OR (length(trim(code_revision))>0 AND instr(code_revision,char(0))=0)),
  backend TEXT CHECK(backend IS NULL OR instr(backend,char(0))=0),
  model TEXT CHECK(model IS NULL OR instr(model,char(0))=0),
  reasoning TEXT CHECK(reasoning IS NULL OR instr(reasoning,char(0))=0),
  reasoning_effort TEXT CHECK(reasoning_effort IS NULL OR instr(reasoning_effort,char(0))=0),
  context_profile TEXT NOT NULL CHECK(length(trim(context_profile))>0 AND instr(context_profile,char(0))=0),
  max_source_chars TEXT NOT NULL CHECK(instr(max_source_chars,char(0))=0),
  require_fulltext INTEGER NOT NULL CHECK(typeof(require_fulltext)='integer' AND require_fulltext IN(0,1)),
  semantic_contract TEXT NOT NULL CHECK(semantic_contract='{CONTRACT_ID}'),
  identity_kind TEXT NOT NULL CHECK(identity_kind IN('revision','snapshot','snapshot_error')),
  code_dirty INTEGER CHECK(code_dirty IS NULL OR (typeof(code_dirty)='integer' AND code_dirty IN(0,1))),
  code_diff_sha256 TEXT,
  code_snapshot_id TEXT,
  code_snapshot_error TEXT,
  CHECK(
    (identity_kind='revision' AND code_dirty IS NULL AND code_diff_sha256 IS NULL AND code_snapshot_id IS NULL AND code_snapshot_error IS NULL)
    OR (identity_kind='snapshot' AND code_dirty IS NOT NULL AND {_SHA_CHECK.format(column='code_diff_sha256')} AND {_SHA_CHECK.format(column='code_snapshot_id')} AND code_snapshot_error IS NULL)
    OR (identity_kind='snapshot_error' AND code_dirty IS NULL AND code_diff_sha256 IS NULL AND code_snapshot_id IS NULL AND length(trim(code_snapshot_error))>0 AND instr(code_snapshot_error,char(0))=0)
  )
);

CREATE TABLE IF NOT EXISTS verification_lifecycle_failure_setting (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  kind TEXT NOT NULL CHECK(kind='infrastructure_error'),
  error_type TEXT NOT NULL CHECK(length(trim(error_type))>0 AND instr(error_type,char(0))=0),
  reason TEXT NOT NULL CHECK(length(reason)<=500 AND instr(reason,char(0))=0)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_config (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  contract_id TEXT NOT NULL CHECK(contract_id='{CONTRACT_ID}'),
  context_mode TEXT NOT NULL CHECK(context_mode IN('auto','full_text','extractive_rag')),
  profile TEXT NOT NULL CHECK(profile IN('large','medium','small')),
  max_tokens INTEGER CHECK(
    max_tokens IS NULL OR
    (typeof(max_tokens)='integer' AND max_tokens>0)
  ),
  reasoning TEXT NOT NULL CHECK(reasoning IN('auto','on','off')),
  reasoning_effort TEXT NOT NULL CHECK(reasoning_effort IN('low','medium','high','max','xhigh')),
  provider_confidence_threshold REAL CHECK(
    provider_confidence_threshold IS NULL OR (
      typeof(provider_confidence_threshold)='real'
      AND provider_confidence_threshold>0 AND provider_confidence_threshold<=1
    )
  ),
  confidence_policy_id TEXT CHECK(
    confidence_policy_id IS NULL OR (
      length(trim(confidence_policy_id))>0 AND instr(confidence_policy_id,char(0))=0
    )
  ),
  candidate_cap INTEGER NOT NULL CHECK(typeof(candidate_cap)='integer' AND candidate_cap>0),
  jury1_technical_cap INTEGER NOT NULL CHECK(typeof(jury1_technical_cap)='integer' AND jury1_technical_cap>0),
  jury2_technical_cap INTEGER NOT NULL CHECK(typeof(jury2_technical_cap)='integer' AND jury2_technical_cap>0),
  jury2_level TEXT NOT NULL CHECK(jury2_level IN('off','low','medium','high')),
  aggregate_in_flight INTEGER NOT NULL CHECK(typeof(aggregate_in_flight)='integer' AND aggregate_in_flight>0),
  global_pacing_ms INTEGER NOT NULL CHECK(typeof(global_pacing_ms)='integer' AND global_pacing_ms>=0),
  execution_policy_hash TEXT NOT NULL CHECK({_SHA_CHECK.format(column='execution_policy_hash')}),
  selection_seed TEXT NOT NULL CHECK(length(trim(selection_seed))>0 AND instr(selection_seed,char(0))=0),
  selector_algorithm TEXT NOT NULL CHECK(selector_algorithm='{SELECTOR_ALGORITHM}'),
  seed_derivation TEXT NOT NULL CHECK(seed_derivation='{SEED_DERIVATION}'),
  cursor_semantics TEXT NOT NULL CHECK(cursor_semantics='{CURSOR_SEMANTICS}'),
  credential_cursor INTEGER NOT NULL CHECK(typeof(credential_cursor)='integer' AND credential_cursor=0),
  model_draw_index INTEGER NOT NULL CHECK(typeof(model_draw_index)='integer' AND model_draw_index=0),
  cooldown_baseline_seconds INTEGER NOT NULL CHECK(typeof(cooldown_baseline_seconds)='integer' AND cooldown_baseline_seconds>0),
  cooldown_multiplier INTEGER NOT NULL CHECK(typeof(cooldown_multiplier)='integer' AND cooldown_multiplier>0),
  cooldown_local_max_seconds INTEGER NOT NULL CHECK(typeof(cooldown_local_max_seconds)='integer' AND cooldown_local_max_seconds>0),
  cooldown_stable_successes INTEGER NOT NULL CHECK(typeof(cooldown_stable_successes)='integer' AND cooldown_stable_successes>0),
  retry_after_is_lower_bound INTEGER NOT NULL CHECK(typeof(retry_after_is_lower_bound)='integer' AND retry_after_is_lower_bound=1),
  retry_after_not_truncated INTEGER NOT NULL CHECK(typeof(retry_after_not_truncated)='integer' AND retry_after_not_truncated=1),
  cooldown_version TEXT NOT NULL CHECK(cooldown_version='{COOLDOWN_POLICY_VERSION}'),
  jury1_prompt_id TEXT NOT NULL CHECK(length(trim(jury1_prompt_id))>0 AND instr(jury1_prompt_id,char(0))=0),
  jury1_prompt_version INTEGER NOT NULL CHECK(typeof(jury1_prompt_version)='integer' AND jury1_prompt_version>0),
  jury1_prompt_sha256 TEXT NOT NULL CHECK({_SHA_CHECK.format(column='jury1_prompt_sha256')}),
  jury2_prompt_id TEXT NOT NULL CHECK(length(trim(jury2_prompt_id))>0 AND instr(jury2_prompt_id,char(0))=0),
  jury2_prompt_version INTEGER NOT NULL CHECK(typeof(jury2_prompt_version)='integer' AND jury2_prompt_version>0),
  jury2_prompt_sha256 TEXT NOT NULL CHECK({_SHA_CHECK.format(column='jury2_prompt_sha256')}),
  provider_count INTEGER NOT NULL CHECK(typeof(provider_count)='integer' AND provider_count>0),
  pacing_count INTEGER NOT NULL CHECK(typeof(pacing_count)='integer' AND pacing_count>=0),
  jury1_only_count INTEGER NOT NULL CHECK(typeof(jury1_only_count)='integer' AND jury1_only_count>=0),
  jury2_only_count INTEGER NOT NULL CHECK(typeof(jury2_only_count)='integer' AND jury2_only_count>=0),
  cooldown_override_count INTEGER NOT NULL CHECK(typeof(cooldown_override_count)='integer' AND cooldown_override_count>=0)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_providers (
  provider_order INTEGER PRIMARY KEY CHECK(typeof(provider_order)='integer' AND provider_order>=0),
  singleton INTEGER NOT NULL DEFAULT 1 REFERENCES verify_claim_evidence_config(singleton),
  name TEXT NOT NULL UNIQUE CHECK(length(trim(name))>0 AND instr(name,char(0))=0),
  pairing_mode TEXT NOT NULL CHECK(pairing_mode IN('positional_exclusive','seeded_cross_product')),
  credential_count INTEGER NOT NULL CHECK(typeof(credential_count)='integer' AND credential_count>0),
  model_count INTEGER NOT NULL CHECK(typeof(model_count)='integer' AND model_count>0),
  lane_count INTEGER NOT NULL CHECK(typeof(lane_count)='integer' AND lane_count>0)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_credentials (
  provider_order INTEGER NOT NULL REFERENCES verify_claim_evidence_providers(provider_order),
  credential_order INTEGER NOT NULL CHECK(typeof(credential_order)='integer' AND credential_order>=0),
  alias TEXT NOT NULL CHECK(length(trim(alias))>0 AND instr(alias,char(0))=0),
  fingerprint TEXT CHECK(fingerprint IS NULL OR ({_SHA_CHECK.format(column='fingerprint')})),
  PRIMARY KEY(provider_order,credential_order),
  UNIQUE(provider_order,alias)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_models (
  provider_order INTEGER NOT NULL REFERENCES verify_claim_evidence_providers(provider_order),
  model_order INTEGER NOT NULL CHECK(typeof(model_order)='integer' AND model_order>=0),
  model TEXT NOT NULL CHECK(length(trim(model))>0 AND instr(model,char(0))=0),
  PRIMARY KEY(provider_order,model_order),
  UNIQUE(provider_order,model)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_lanes (
  provider_order INTEGER NOT NULL REFERENCES verify_claim_evidence_providers(provider_order),
  lane_order INTEGER NOT NULL CHECK(typeof(lane_order)='integer' AND lane_order>=0),
  key_alias TEXT NOT NULL,
  key_fingerprint TEXT CHECK(key_fingerprint IS NULL OR ({_SHA_CHECK.format(column='key_fingerprint')})),
  model TEXT NOT NULL,
  jury1_eligible INTEGER NOT NULL CHECK(typeof(jury1_eligible)='integer' AND jury1_eligible IN(0,1)),
  jury2_eligible INTEGER NOT NULL CHECK(typeof(jury2_eligible)='integer' AND jury2_eligible IN(0,1)),
  PRIMARY KEY(provider_order,lane_order),
  FOREIGN KEY(provider_order,key_alias) REFERENCES verify_claim_evidence_credentials(provider_order,alias),
  FOREIGN KEY(provider_order,model) REFERENCES verify_claim_evidence_models(provider_order,model)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_pacing (
  entry_order INTEGER PRIMARY KEY CHECK(typeof(entry_order)='integer' AND entry_order>=0),
  singleton INTEGER NOT NULL DEFAULT 1 REFERENCES verify_claim_evidence_config(singleton),
  selector TEXT NOT NULL UNIQUE CHECK(length(trim(selector))>0 AND instr(selector,char(0))=0),
  interval_ms INTEGER NOT NULL CHECK(typeof(interval_ms)='integer' AND interval_ms>=0)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_jury_roles (
  role TEXT NOT NULL CHECK(role IN('jury1_only','jury2_only')),
  selector_order INTEGER NOT NULL CHECK(typeof(selector_order)='integer' AND selector_order>=0),
  singleton INTEGER NOT NULL DEFAULT 1 REFERENCES verify_claim_evidence_config(singleton),
  selector TEXT NOT NULL CHECK(length(trim(selector))>0 AND instr(selector,char(0))=0),
  PRIMARY KEY(role,selector_order),
  UNIQUE(role,selector)
);

CREATE TABLE IF NOT EXISTS verify_claim_evidence_cooldown_overrides (
  entry_order INTEGER PRIMARY KEY CHECK(typeof(entry_order)='integer' AND entry_order>=0),
  singleton INTEGER NOT NULL DEFAULT 1 REFERENCES verify_claim_evidence_config(singleton),
  selector TEXT NOT NULL UNIQUE CHECK(length(trim(selector))>0 AND instr(selector,char(0))=0),
  seconds INTEGER NOT NULL CHECK(typeof(seconds)='integer' AND seconds>0)
);

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_config_no_update
BEFORE UPDATE ON verify_claim_evidence_config BEGIN SELECT RAISE(ABORT,'verify claim evidence config is immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_config_no_delete
BEFORE DELETE ON verify_claim_evidence_config BEGIN SELECT RAISE(ABORT,'verify claim evidence config is immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_provider_insert_guard
BEFORE INSERT ON verify_claim_evidence_providers
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_config WHERE singleton=NEW.singleton AND NEW.provider_order<provider_count)
BEGIN SELECT RAISE(ABORT,'verify provider order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_provider_no_update BEFORE UPDATE ON verify_claim_evidence_providers BEGIN SELECT RAISE(ABORT,'verify providers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_provider_no_delete BEFORE DELETE ON verify_claim_evidence_providers BEGIN SELECT RAISE(ABORT,'verify providers are immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_credential_insert_guard
BEFORE INSERT ON verify_claim_evidence_credentials
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_providers WHERE provider_order=NEW.provider_order AND NEW.credential_order<credential_count)
BEGIN SELECT RAISE(ABORT,'verify credential order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_credential_no_update BEFORE UPDATE ON verify_claim_evidence_credentials BEGIN SELECT RAISE(ABORT,'verify credentials are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_credential_no_delete BEFORE DELETE ON verify_claim_evidence_credentials BEGIN SELECT RAISE(ABORT,'verify credentials are immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_model_insert_guard
BEFORE INSERT ON verify_claim_evidence_models
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_providers WHERE provider_order=NEW.provider_order AND NEW.model_order<model_count)
BEGIN SELECT RAISE(ABORT,'verify model order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_model_no_update BEFORE UPDATE ON verify_claim_evidence_models BEGIN SELECT RAISE(ABORT,'verify models are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_model_no_delete BEFORE DELETE ON verify_claim_evidence_models BEGIN SELECT RAISE(ABORT,'verify models are immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_lane_insert_guard
BEFORE INSERT ON verify_claim_evidence_lanes
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_providers WHERE provider_order=NEW.provider_order AND NEW.lane_order<lane_count)
BEGIN SELECT RAISE(ABORT,'verify lane order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_lane_no_update BEFORE UPDATE ON verify_claim_evidence_lanes BEGIN SELECT RAISE(ABORT,'verify lanes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_lane_no_delete BEFORE DELETE ON verify_claim_evidence_lanes BEGIN SELECT RAISE(ABORT,'verify lanes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_pacing_insert_guard
BEFORE INSERT ON verify_claim_evidence_pacing
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_config WHERE singleton=NEW.singleton AND NEW.entry_order<pacing_count)
BEGIN SELECT RAISE(ABORT,'verify pacing order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_pacing_no_update BEFORE UPDATE ON verify_claim_evidence_pacing BEGIN SELECT RAISE(ABORT,'verify pacing is immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_pacing_no_delete BEFORE DELETE ON verify_claim_evidence_pacing BEGIN SELECT RAISE(ABORT,'verify pacing is immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_role_insert_guard
BEFORE INSERT ON verify_claim_evidence_jury_roles
WHEN NOT EXISTS (
  SELECT 1 FROM verify_claim_evidence_config WHERE singleton=NEW.singleton
  AND NEW.selector_order < CASE NEW.role WHEN 'jury1_only' THEN jury1_only_count ELSE jury2_only_count END
)
BEGIN SELECT RAISE(ABORT,'verify jury role order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_role_no_update BEFORE UPDATE ON verify_claim_evidence_jury_roles BEGIN SELECT RAISE(ABORT,'verify jury roles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_role_no_delete BEFORE DELETE ON verify_claim_evidence_jury_roles BEGIN SELECT RAISE(ABORT,'verify jury roles are immutable'); END;

CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_override_insert_guard
BEFORE INSERT ON verify_claim_evidence_cooldown_overrides
WHEN NOT EXISTS (SELECT 1 FROM verify_claim_evidence_config WHERE singleton=NEW.singleton AND NEW.entry_order<cooldown_override_count)
BEGIN SELECT RAISE(ABORT,'verify cooldown override order is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_override_no_update BEFORE UPDATE ON verify_claim_evidence_cooldown_overrides BEGIN SELECT RAISE(ABORT,'verify cooldown overrides are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verify_claim_evidence_override_no_delete BEFORE DELETE ON verify_claim_evidence_cooldown_overrides BEGIN SELECT RAISE(ABORT,'verify cooldown overrides are immutable'); END;
"""


_RUNTIME_COMMON_FIELDS = frozenset({
    "code_revision",
    "backend",
    "model",
    "reasoning",
    "reasoning_effort",
    "context_profile",
    "max_source_chars",
    "require_fulltext",
    "semantic_contract",
})
_RUNTIME_SNAPSHOT_FIELDS = frozenset({
    "code_dirty", "code_diff_sha256", "code_snapshot_id",
})
_RUNTIME_ERROR_FIELDS = _RUNTIME_SNAPSHOT_FIELDS | {"code_snapshot_error"}
_FAILURE_FIELDS = frozenset({"kind", "error_type", "reason"})
_CONFIG_FIELDS = frozenset({
    "providers", "context_mode", "profile", "max_tokens", "reasoning",
    "reasoning_effort", "candidate_cap",
    "jury1_technical_cap", "jury2_technical_cap", "jury2_level",
    "aggregate_in_flight", "global_pacing_ms", "pacing_by_model_ms",
    "jury1_only", "jury2_only", "execution_policy_hash", "selection_seed",
    "selector_algorithm", "seed_derivation", "cursor_semantics",
    "credential_cursor", "model_draw_index", "cooldown",
    "cooldown_overrides", "contract_id",
    "jury1_prompt_id", "jury1_prompt_version", "jury1_prompt_sha256",
    "jury2_prompt_id", "jury2_prompt_version", "jury2_prompt_sha256",
    "provider_confidence_threshold", "confidence_policy_id",
})
_PROVIDER_FIELDS = frozenset({
    "name", "credential_aliases", "credential_fingerprints", "models",
    "pairing_mode", "lanes",
})
_LANE_FIELDS = frozenset({
    "key_alias", "key_fingerprint", "model", "jury1_eligible",
    "jury2_eligible",
})
_COOLDOWN_FIELDS = frozenset({
    "baseline_seconds", "multiplier", "local_max_seconds", "stable_successes",
    "retry_after_is_lower_bound", "retry_after_not_truncated", "version",
})


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} has invalid fields")


def _text(
    value: Any,
    label: str,
    *,
    nonempty: bool = False,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or "\0" in value:
        raise ValueError(f"{label} must be text")
    if nonempty and not value.strip():
        raise ValueError(f"{label} must be nonempty")
    return value


def _positive_int(value: Any, label: str, *, zero: bool = False) -> int:
    if type(value) is not int or value < 0 or (not zero and value == 0):
        raise ValueError(f"{label} has an invalid integer")
    return value


def _sha256(value: Any, label: str) -> str:
    value = _text(value, label, nonempty=True)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _bool_from_db(value: Any, label: str) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise RuntimeError(f"{label} boolean storage is inconsistent")
    return bool(value)


def _dense(rows: list[Any], field: str, count: int, label: str) -> None:
    if len(rows) != count or [row[field] for row in rows] != list(range(count)):
        raise RuntimeError(f"{label} ordering is inconsistent")


def normalize_verify_runtime(value: Any) -> dict[str, Any]:
    value = _mapping(value, "verify_runtime")
    fields = set(value)
    identity_fields = fields - _RUNTIME_COMMON_FIELDS
    if not _RUNTIME_COMMON_FIELDS <= fields or identity_fields not in (
        frozenset(), _RUNTIME_SNAPSHOT_FIELDS, _RUNTIME_ERROR_FIELDS,
    ):
        raise ValueError("verify_runtime has invalid fields")

    for key in ("backend", "model", "reasoning", "reasoning_effort"):
        _text(value[key], f"verify_runtime.{key}", nullable=True)
    _text(
        value["code_revision"], "verify_runtime.code_revision",
        nonempty=value["code_revision"] is not None, nullable=True,
    )
    _text(value["context_profile"], "verify_runtime.context_profile", nonempty=True)
    _text(value["max_source_chars"], "verify_runtime.max_source_chars")
    if type(value["require_fulltext"]) is not bool:
        raise ValueError("verify_runtime.require_fulltext must be bool")
    if value["semantic_contract"] != CONTRACT_ID:
        raise ValueError("verify_runtime semantic contract is invalid")

    if identity_fields == _RUNTIME_SNAPSHOT_FIELDS:
        if type(value["code_dirty"]) is not bool:
            raise ValueError("verify_runtime.code_dirty must be bool")
        _sha256(value["code_diff_sha256"], "verify_runtime.code_diff_sha256")
        _sha256(value["code_snapshot_id"], "verify_runtime.code_snapshot_id")
    elif identity_fields == _RUNTIME_ERROR_FIELDS:
        if any(value[key] is not None for key in _RUNTIME_SNAPSHOT_FIELDS):
            raise ValueError("verify_runtime snapshot error identity is invalid")
        _text(
            value["code_snapshot_error"],
            "verify_runtime.code_snapshot_error",
            nonempty=True,
        )
    return dict(value)


def normalize_lifecycle_failure(value: Any) -> dict[str, Any]:
    value = _mapping(value, "verification_lifecycle_failure")
    _exact_fields(value, _FAILURE_FIELDS, "verification_lifecycle_failure")
    if value["kind"] != "infrastructure_error":
        raise ValueError("verification lifecycle failure kind is invalid")
    _text(value["error_type"], "verification lifecycle error_type", nonempty=True)
    reason = _text(value["reason"], "verification lifecycle reason")
    if len(reason) > 500:
        raise ValueError("verification lifecycle reason is too long")
    return dict(value)


def _selector_pairs(
    value: Any,
    label: str,
    known_selectors: frozenset[str],
    *,
    positive: bool,
) -> list[list[Any]]:
    if type(value) is not list:
        raise ValueError(f"{label} must be a list")
    out: list[list[Any]] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise ValueError(f"{label} entry is invalid")
        selector = _text(item[0], f"{label} selector", nonempty=True)
        amount = _positive_int(item[1], f"{label} value", zero=not positive)
        if selector not in known_selectors or selector in seen:
            raise ValueError(f"{label} selector is invalid")
        seen.add(selector)
        out.append([selector, amount])
    return out


def _policy_hash(value: Mapping[str, Any]) -> str:
    policy = {
        "contract_id": CONTRACT_ID,
        "providers": value["providers"],
        "values": [
            value["context_mode"],
            value["profile"],
            value["max_tokens"],
            value["reasoning"],
            value["reasoning_effort"],
            value["candidate_cap"],
            value["jury1_technical_cap"],
            value["jury2_technical_cap"],
            value["jury2_level"],
            value["aggregate_in_flight"],
            value["global_pacing_ms"],
        ],
        "pacing": value["pacing_by_model_ms"],
        "jury1_only": value["jury1_only"],
        "jury2_only": value["jury2_only"],
        "selector_algorithm": SELECTOR_ALGORITHM,
        "seed_derivation": SEED_DERIVATION,
        "cursor_semantics": CURSOR_SEMANTICS,
        "initial_cursors": [0, 0],
        "cooldown": value["cooldown"],
        "overrides": value["cooldown_overrides"],
        "provider_confidence_threshold": value["provider_confidence_threshold"],
        "confidence_policy_id": value["confidence_policy_id"],
        "jury1_prompt": {"prompt_id": value["jury1_prompt_id"], "version": value["jury1_prompt_version"], "sha256": value["jury1_prompt_sha256"]},
        "jury2_prompt": {"prompt_id": value["jury2_prompt_id"], "version": value["jury2_prompt_version"], "sha256": value["jury2_prompt_sha256"]},
    }
    material = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    material_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    final_policy = json.dumps(
        {"material": policy, "selection_seed": value["selection_seed"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    # Keep this local variable explicit: it documents the two-stage producer
    # contract even though only the final material is hashed below.
    if len(material_hash) != 64:
        raise AssertionError("unreachable policy material hash")
    return hashlib.sha256(final_policy.encode("utf-8")).hexdigest()


def normalize_claim_evidence_config(value: Any) -> dict[str, Any]:
    value = _mapping(value, "verify_claim_evidence_config")
    _exact_fields(value, _CONFIG_FIELDS, "verify_claim_evidence_config")
    if value["contract_id"] != CONTRACT_ID:
        raise ValueError("verify claim-evidence contract id is invalid")
    if value["context_mode"] not in {"auto", "full_text", "extractive_rag"}:
        raise ValueError("verify claim-evidence context mode is invalid")
    if value["profile"] not in {"large", "medium", "small"}:
        raise ValueError("verify claim-evidence profile is invalid")
    if value["reasoning"] not in {"auto", "on", "off"}:
        raise ValueError("verify claim-evidence reasoning mode is invalid")
    if value["reasoning_effort"] not in {"low", "medium", "high", "max", "xhigh"}:
        raise ValueError("verify claim-evidence reasoning effort is invalid")
    if value["jury2_level"] not in {"off", "low", "medium", "high"}:
        raise ValueError("verify claim-evidence Jury2 level is invalid")
    confidence_threshold = value["provider_confidence_threshold"]
    confidence_policy_id = value["confidence_policy_id"]
    if confidence_threshold is not None and (
        not isinstance(confidence_threshold, (int, float))
        or isinstance(confidence_threshold, bool)
        or not math.isfinite(confidence_threshold)
        or not 0 < confidence_threshold <= 1
    ):
        raise ValueError("verify claim-evidence provider confidence threshold is invalid")
    if confidence_policy_id is not None and (
        not isinstance(confidence_policy_id, str)
        or not confidence_policy_id.strip()
        or "\x00" in confidence_policy_id
    ):
        raise ValueError("verify claim-evidence confidence policy id is invalid")
    if (confidence_threshold is None) != (confidence_policy_id is None):
        raise ValueError("verify claim-evidence confidence policy is incomplete")
    if value["max_tokens"] is not None:
        _positive_int(
            value["max_tokens"], "verify_claim_evidence_config.max_tokens"
        )
    for key in (
        "candidate_cap", "jury1_technical_cap", "jury2_technical_cap",
        "aggregate_in_flight",
    ):
        _positive_int(value[key], f"verify_claim_evidence_config.{key}")
    _positive_int(
        value["global_pacing_ms"],
        "verify_claim_evidence_config.global_pacing_ms",
        zero=True,
    )
    if type(value["credential_cursor"]) is not int or value["credential_cursor"] != 0:
        raise ValueError("verify claim-evidence credential cursor is invalid")
    if type(value["model_draw_index"]) is not int or value["model_draw_index"] != 0:
        raise ValueError("verify claim-evidence model draw index is invalid")
    _sha256(value["execution_policy_hash"], "execution_policy_hash")
    _text(value["selection_seed"], "selection_seed", nonempty=True)
    if value["selector_algorithm"] != SELECTOR_ALGORITHM:
        raise ValueError("verify claim-evidence selector algorithm is invalid")
    if value["seed_derivation"] != SEED_DERIVATION:
        raise ValueError("verify claim-evidence seed derivation is invalid")
    if value["cursor_semantics"] != CURSOR_SEMANTICS:
        raise ValueError("verify claim-evidence cursor semantics is invalid")
    for stage in ("jury1", "jury2"):
        _text(value[stage + "_prompt_id"], stage + " prompt id", nonempty=True)
        _positive_int(value[stage + "_prompt_version"], stage + " prompt version")
        _sha256(value[stage + "_prompt_sha256"], stage + " prompt sha256")

    providers = value["providers"]
    if type(providers) is not list or not providers:
        raise ValueError("verify claim-evidence providers are required")
    provider_names: set[str] = set()
    known_selectors: set[str] = set()
    normalized_providers: list[dict[str, Any]] = []
    for provider in providers:
        provider = _mapping(provider, "verify claim-evidence provider")
        _exact_fields(provider, _PROVIDER_FIELDS, "verify claim-evidence provider")
        name = _text(provider["name"], "provider name", nonempty=True)
        if name in provider_names:
            raise ValueError("verify claim-evidence provider names must be unique")
        provider_names.add(name)
        aliases = provider["credential_aliases"]
        fingerprints = provider["credential_fingerprints"]
        models = provider["models"]
        lanes = provider["lanes"]
        if (
            type(aliases) is not list or not aliases
            or type(fingerprints) is not list or len(fingerprints) != len(aliases)
            or type(models) is not list or not models
            or type(lanes) is not list or not lanes
        ):
            raise ValueError("verify claim-evidence provider lists are invalid")
        if len(set(aliases)) != len(aliases) or len(set(models)) != len(models):
            raise ValueError("verify claim-evidence provider lists must be unique")
        for alias in aliases:
            _text(alias, "credential alias", nonempty=True)
        for fingerprint in fingerprints:
            if fingerprint is not None:
                _sha256(fingerprint, "credential fingerprint")
        for model in models:
            _text(model, "provider model", nonempty=True)
            known_selectors.add(f"{name}:{model}")
        pairing_mode = provider["pairing_mode"]
        if pairing_mode == "positional_exclusive":
            if len(aliases) != len(models):
                raise ValueError("positional provider pairing is invalid")
            expected_lanes = list(zip(aliases, fingerprints, models))
        elif pairing_mode == "seeded_cross_product":
            expected_lanes = [
                (alias, fingerprint, model)
                for alias, fingerprint in zip(aliases, fingerprints)
                for model in models
            ]
        else:
            raise ValueError("verify claim-evidence pairing mode is invalid")
        if len(lanes) != len(expected_lanes):
            raise ValueError("verify claim-evidence lane count is invalid")
        normalized_lanes: list[dict[str, Any]] = []
        for lane, expected in zip(lanes, expected_lanes):
            lane = _mapping(lane, "verify claim-evidence lane")
            _exact_fields(lane, _LANE_FIELDS, "verify claim-evidence lane")
            if tuple(lane[key] for key in (
                "key_alias", "key_fingerprint", "model",
            )) != expected:
                raise ValueError("verify claim-evidence lane pairing is invalid")
            if type(lane["jury1_eligible"]) is not bool or type(lane["jury2_eligible"]) is not bool:
                raise ValueError("verify claim-evidence lane eligibility is invalid")
            normalized_lanes.append(dict(lane))
        normalized_providers.append({
            "name": name,
            "credential_aliases": list(aliases),
            "credential_fingerprints": list(fingerprints),
            "models": list(models),
            "pairing_mode": pairing_mode,
            "lanes": normalized_lanes,
        })
    known = frozenset(known_selectors)
    role_lists: dict[str, list[str]] = {}
    for role in ("jury1_only", "jury2_only"):
        items = value[role]
        if type(items) is not list or items != sorted(set(items)):
            raise ValueError(f"verify claim-evidence {role} selectors are invalid")
        if any(type(item) is not str or item not in known for item in items):
            raise ValueError(f"verify claim-evidence {role} selector is unknown")
        role_lists[role] = list(items)
    if set(role_lists["jury1_only"]) & set(role_lists["jury2_only"]):
        raise ValueError("verify claim-evidence Jury role selectors conflict")
    jury1_only = frozenset(role_lists["jury1_only"])
    jury2_only = frozenset(role_lists["jury2_only"])
    for provider in normalized_providers:
        for lane in provider["lanes"]:
            selector = f"{provider['name']}:{lane['model']}"
            if lane["jury1_eligible"] != (selector not in jury2_only):
                raise ValueError("verify claim-evidence Jury1 eligibility is inconsistent")
            if lane["jury2_eligible"] != (selector not in jury1_only):
                raise ValueError("verify claim-evidence Jury2 eligibility is inconsistent")
    all_lanes = [lane for provider in normalized_providers for lane in provider["lanes"]]
    if not any(lane["jury1_eligible"] for lane in all_lanes):
        raise ValueError("verify claim-evidence has no Jury1 lane")
    if value["jury2_level"] != "off" and not any(
        lane["jury2_eligible"] for lane in all_lanes
    ):
        raise ValueError("verify claim-evidence has no Jury2 lane")

    pacing = _selector_pairs(
        value["pacing_by_model_ms"], "pacing_by_model_ms", known,
        positive=False,
    )
    overrides = _selector_pairs(
        value["cooldown_overrides"], "cooldown_overrides", known,
        positive=True,
    )
    cooldown = _mapping(value["cooldown"], "verify claim-evidence cooldown")
    _exact_fields(cooldown, _COOLDOWN_FIELDS, "verify claim-evidence cooldown")
    for key in (
        "baseline_seconds", "multiplier", "local_max_seconds", "stable_successes",
    ):
        _positive_int(cooldown[key], f"cooldown.{key}")
    if cooldown["retry_after_is_lower_bound"] is not True:
        raise ValueError("cooldown retry-after lower-bound policy is invalid")
    if cooldown["retry_after_not_truncated"] is not True:
        raise ValueError("cooldown retry-after truncation policy is invalid")
    if cooldown["version"] != COOLDOWN_POLICY_VERSION:
        raise ValueError("cooldown policy version is invalid")

    normalized = {
        **dict(value),
        "providers": normalized_providers,
        "pacing_by_model_ms": pacing,
        "jury1_only": role_lists["jury1_only"],
        "jury2_only": role_lists["jury2_only"],
        "cooldown": dict(cooldown),
        "cooldown_overrides": overrides,
    }
    if _policy_hash(normalized) != normalized["execution_policy_hash"]:
        raise ValueError("verify claim-evidence execution policy hash is invalid")
    return normalized


def replace_verify_runtime(conn, value: Any) -> None:
    value = normalize_verify_runtime(value)
    identity_fields = set(value) - _RUNTIME_COMMON_FIELDS
    identity_kind = (
        "revision" if not identity_fields
        else "snapshot_error" if identity_fields == _RUNTIME_ERROR_FIELDS
        else "snapshot"
    )
    conn.execute("DELETE FROM verify_runtime_setting")
    conn.execute(
        """
        INSERT INTO verify_runtime_setting(
          singleton,code_revision,backend,model,reasoning,reasoning_effort,
          context_profile,max_source_chars,require_fulltext,semantic_contract,
          identity_kind,code_dirty,code_diff_sha256,code_snapshot_id,
          code_snapshot_error
        ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            value["code_revision"], value["backend"], value["model"],
            value["reasoning"], value["reasoning_effort"],
            value["context_profile"], value["max_source_chars"],
            int(value["require_fulltext"]), value["semantic_contract"],
            identity_kind, value.get("code_dirty"), value.get("code_diff_sha256"),
            value.get("code_snapshot_id"), value.get("code_snapshot_error"),
        ),
    )


def read_verify_runtime(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM verify_runtime_setting WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    value = {key: row[key] for key in _RUNTIME_COMMON_FIELDS}
    value["require_fulltext"] = _bool_from_db(
        row["require_fulltext"], "verify runtime require_fulltext",
    )
    kind = row["identity_kind"]
    if kind == "revision":
        if any(row[key] is not None for key in (
            "code_dirty", "code_diff_sha256", "code_snapshot_id",
            "code_snapshot_error",
        )):
            raise RuntimeError("verify runtime revision identity is inconsistent")
    elif kind == "snapshot":
        value.update({
            "code_dirty": _bool_from_db(row["code_dirty"], "verify runtime code_dirty"),
            "code_diff_sha256": row["code_diff_sha256"],
            "code_snapshot_id": row["code_snapshot_id"],
        })
        if row["code_snapshot_error"] is not None:
            raise RuntimeError("verify runtime snapshot identity is inconsistent")
    elif kind == "snapshot_error":
        value.update({
            "code_dirty": row["code_dirty"],
            "code_diff_sha256": row["code_diff_sha256"],
            "code_snapshot_id": row["code_snapshot_id"],
            "code_snapshot_error": row["code_snapshot_error"],
        })
    else:
        raise RuntimeError("verify runtime identity kind is invalid")
    try:
        return normalize_verify_runtime(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("verify runtime storage is inconsistent") from exc


def replace_lifecycle_failure(conn, value: Any) -> None:
    value = normalize_lifecycle_failure(value)
    conn.execute("DELETE FROM verification_lifecycle_failure_setting")
    conn.execute(
        """
        INSERT INTO verification_lifecycle_failure_setting(
          singleton,kind,error_type,reason
        ) VALUES(1,?,?,?)
        """,
        (value["kind"], value["error_type"], value["reason"]),
    )


def read_lifecycle_failure(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT kind,error_type,reason FROM verification_lifecycle_failure_setting "
        "WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    try:
        return normalize_lifecycle_failure(dict(row))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("verification lifecycle failure storage is inconsistent") from exc


def write_claim_evidence_config(conn, value: Any) -> None:
    value = normalize_claim_evidence_config(value)
    existing = read_claim_evidence_config(conn)
    if existing is not None:
        if existing != value:
            raise ValueError("verify_claim_evidence_config is write-once")
        return

    cooldown = value["cooldown"]
    conn.execute(
        """
        INSERT INTO verify_claim_evidence_config(
          singleton,contract_id,context_mode,profile,max_tokens,reasoning,
          reasoning_effort,provider_confidence_threshold,confidence_policy_id,candidate_cap,
          jury1_technical_cap,jury2_technical_cap,jury2_level,
          aggregate_in_flight,global_pacing_ms,execution_policy_hash,
          selection_seed,selector_algorithm,seed_derivation,cursor_semantics,
          credential_cursor,model_draw_index,cooldown_baseline_seconds,
          cooldown_multiplier,cooldown_local_max_seconds,
          cooldown_stable_successes,retry_after_is_lower_bound,
          retry_after_not_truncated,cooldown_version,
          jury1_prompt_id,jury1_prompt_version,jury1_prompt_sha256,
          jury2_prompt_id,jury2_prompt_version,jury2_prompt_sha256,
          provider_count,pacing_count,jury1_only_count,jury2_only_count,
          cooldown_override_count
        ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            value["contract_id"], value["context_mode"], value["profile"],
            value["max_tokens"], value["reasoning"], value["reasoning_effort"],
            value["provider_confidence_threshold"], value["confidence_policy_id"],
            value["candidate_cap"],
            value["jury1_technical_cap"],
            value["jury2_technical_cap"], value["jury2_level"],
            value["aggregate_in_flight"], value["global_pacing_ms"],
            value["execution_policy_hash"], value["selection_seed"],
            value["selector_algorithm"], value["seed_derivation"],
            value["cursor_semantics"], value["credential_cursor"],
            value["model_draw_index"], cooldown["baseline_seconds"],
            cooldown["multiplier"], cooldown["local_max_seconds"],
            cooldown["stable_successes"],
            int(cooldown["retry_after_is_lower_bound"]),
            int(cooldown["retry_after_not_truncated"]), cooldown["version"],
            value["jury1_prompt_id"],
            value["jury1_prompt_version"], value["jury1_prompt_sha256"],
            value["jury2_prompt_id"], value["jury2_prompt_version"],
            value["jury2_prompt_sha256"], len(value["providers"]),
            len(value["pacing_by_model_ms"]), len(value["jury1_only"]),
            len(value["jury2_only"]), len(value["cooldown_overrides"]),
        ),
    )
    for provider_order, provider in enumerate(value["providers"]):
        conn.execute(
            """
            INSERT INTO verify_claim_evidence_providers(
              provider_order,name,pairing_mode,credential_count,model_count,lane_count
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                provider_order, provider["name"], provider["pairing_mode"],
                len(provider["credential_aliases"]), len(provider["models"]),
                len(provider["lanes"]),
            ),
        )
        conn.executemany(
            """
            INSERT INTO verify_claim_evidence_credentials(
              provider_order,credential_order,alias,fingerprint
            ) VALUES(?,?,?,?)
            """,
            (
                (provider_order, index, alias, provider["credential_fingerprints"][index])
                for index, alias in enumerate(provider["credential_aliases"])
            ),
        )
        conn.executemany(
            """
            INSERT INTO verify_claim_evidence_models(
              provider_order,model_order,model
            ) VALUES(?,?,?)
            """,
            (
                (provider_order, index, model)
                for index, model in enumerate(provider["models"])
            ),
        )
        conn.executemany(
            """
            INSERT INTO verify_claim_evidence_lanes(
              provider_order,lane_order,key_alias,key_fingerprint,model,
              jury1_eligible,jury2_eligible
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                (
                    provider_order, index, lane["key_alias"],
                    lane["key_fingerprint"], lane["model"],
                    int(lane["jury1_eligible"]), int(lane["jury2_eligible"]),
                )
                for index, lane in enumerate(provider["lanes"])
            ),
        )
    conn.executemany(
        """
        INSERT INTO verify_claim_evidence_pacing(entry_order,selector,interval_ms)
        VALUES(?,?,?)
        """,
        (
            (index, selector, interval)
            for index, (selector, interval) in enumerate(value["pacing_by_model_ms"])
        ),
    )
    for role in ("jury1_only", "jury2_only"):
        conn.executemany(
            """
            INSERT INTO verify_claim_evidence_jury_roles(
              role,selector_order,selector
            ) VALUES(?,?,?)
            """,
            (
                (role, index, selector)
                for index, selector in enumerate(value[role])
            ),
        )
    conn.executemany(
        """
        INSERT INTO verify_claim_evidence_cooldown_overrides(
          entry_order,selector,seconds
        ) VALUES(?,?,?)
        """,
        (
            (index, selector, seconds)
            for index, (selector, seconds) in enumerate(value["cooldown_overrides"])
        ),
    )
    if read_claim_evidence_config(conn) != value:
        raise RuntimeError("verify claim-evidence config round-trip is inconsistent")


def read_claim_evidence_config(conn) -> dict[str, Any] | None:
    root = conn.execute(
        "SELECT * FROM verify_claim_evidence_config WHERE singleton=1"
    ).fetchone()
    if root is None:
        child_tables = (
            "verify_claim_evidence_providers", "verify_claim_evidence_credentials",
            "verify_claim_evidence_models", "verify_claim_evidence_lanes",
            "verify_claim_evidence_pacing", "verify_claim_evidence_jury_roles",
            "verify_claim_evidence_cooldown_overrides",
        )
        if any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in child_tables):
            raise RuntimeError("orphan verify claim-evidence config rows")
        return None
    try:
        provider_rows = conn.execute(
            "SELECT * FROM verify_claim_evidence_providers ORDER BY provider_order"
        ).fetchall()
        _dense(provider_rows, "provider_order", root["provider_count"], "verify providers")
        providers: list[dict[str, Any]] = []
        for provider_row in provider_rows:
            provider_order = provider_row["provider_order"]
            credentials = conn.execute(
                "SELECT * FROM verify_claim_evidence_credentials "
                "WHERE provider_order=? ORDER BY credential_order",
                (provider_order,),
            ).fetchall()
            models = conn.execute(
                "SELECT * FROM verify_claim_evidence_models "
                "WHERE provider_order=? ORDER BY model_order",
                (provider_order,),
            ).fetchall()
            lanes = conn.execute(
                "SELECT * FROM verify_claim_evidence_lanes "
                "WHERE provider_order=? ORDER BY lane_order",
                (provider_order,),
            ).fetchall()
            _dense(credentials, "credential_order", provider_row["credential_count"], "verify credentials")
            _dense(models, "model_order", provider_row["model_count"], "verify models")
            _dense(lanes, "lane_order", provider_row["lane_count"], "verify lanes")
            providers.append({
                "name": provider_row["name"],
                "credential_aliases": [row["alias"] for row in credentials],
                "credential_fingerprints": [row["fingerprint"] for row in credentials],
                "models": [row["model"] for row in models],
                "pairing_mode": provider_row["pairing_mode"],
                "lanes": [{
                    "key_alias": row["key_alias"],
                    "key_fingerprint": row["key_fingerprint"],
                    "model": row["model"],
                    "jury1_eligible": _bool_from_db(row["jury1_eligible"], "Jury1 eligibility"),
                    "jury2_eligible": _bool_from_db(row["jury2_eligible"], "Jury2 eligibility"),
                } for row in lanes],
            })

        pacing_rows = conn.execute(
            "SELECT * FROM verify_claim_evidence_pacing ORDER BY entry_order"
        ).fetchall()
        _dense(pacing_rows, "entry_order", root["pacing_count"], "verify pacing")
        role_values: dict[str, list[str]] = {}
        for role, count_field in (
            ("jury1_only", "jury1_only_count"),
            ("jury2_only", "jury2_only_count"),
        ):
            rows = conn.execute(
                "SELECT * FROM verify_claim_evidence_jury_roles "
                "WHERE role=? ORDER BY selector_order", (role,),
            ).fetchall()
            _dense(rows, "selector_order", root[count_field], f"verify {role}")
            role_values[role] = [row["selector"] for row in rows]
        override_rows = conn.execute(
            "SELECT * FROM verify_claim_evidence_cooldown_overrides ORDER BY entry_order"
        ).fetchall()
        _dense(
            override_rows, "entry_order", root["cooldown_override_count"],
            "verify cooldown overrides",
        )
        value = {
            "providers": providers,
            "context_mode": root["context_mode"],
            "profile": root["profile"],
            "max_tokens": root["max_tokens"],
            "reasoning": root["reasoning"],
            "reasoning_effort": root["reasoning_effort"],
            "provider_confidence_threshold": root["provider_confidence_threshold"],
            "confidence_policy_id": root["confidence_policy_id"],
            "candidate_cap": root["candidate_cap"],
            "jury1_technical_cap": root["jury1_technical_cap"],
            "jury2_technical_cap": root["jury2_technical_cap"],
            "jury2_level": root["jury2_level"],
            "aggregate_in_flight": root["aggregate_in_flight"],
            "global_pacing_ms": root["global_pacing_ms"],
            "pacing_by_model_ms": [
                [row["selector"], row["interval_ms"]] for row in pacing_rows
            ],
            "jury1_only": role_values["jury1_only"],
            "jury2_only": role_values["jury2_only"],
            "execution_policy_hash": root["execution_policy_hash"],
            "selection_seed": root["selection_seed"],
            "selector_algorithm": root["selector_algorithm"],
            "seed_derivation": root["seed_derivation"],
            "cursor_semantics": root["cursor_semantics"],
            "credential_cursor": root["credential_cursor"],
            "model_draw_index": root["model_draw_index"],
            "cooldown": {
                "baseline_seconds": root["cooldown_baseline_seconds"],
                "multiplier": root["cooldown_multiplier"],
                "local_max_seconds": root["cooldown_local_max_seconds"],
                "stable_successes": root["cooldown_stable_successes"],
                "retry_after_is_lower_bound": _bool_from_db(
                    root["retry_after_is_lower_bound"], "cooldown lower bound",
                ),
                "retry_after_not_truncated": _bool_from_db(
                    root["retry_after_not_truncated"], "cooldown truncation",
                ),
                "version": root["cooldown_version"],
            },
            "cooldown_overrides": [
                [row["selector"], row["seconds"]] for row in override_rows
            ],
            "jury1_prompt_id": root["jury1_prompt_id"],
            "jury1_prompt_version": root["jury1_prompt_version"],
            "jury1_prompt_sha256": root["jury1_prompt_sha256"],
            "jury2_prompt_id": root["jury2_prompt_id"],
            "jury2_prompt_version": root["jury2_prompt_version"],
            "jury2_prompt_sha256": root["jury2_prompt_sha256"],
            "contract_id": root["contract_id"],
        }
        return normalize_claim_evidence_config(value)
    except RuntimeError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("verify claim-evidence config storage is inconsistent") from exc
