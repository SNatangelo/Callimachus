# core/infra/llm_runtime/schema.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent typed schema for replayable LLM operational state."""

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE credentials (
 credential_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, credential_alias TEXT NOT NULL,
 credential_fingerprint TEXT, created_at TEXT NOT NULL, UNIQUE(provider_id, credential_alias));
CREATE TABLE runtime_projection_events (
 projection_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL, source_event_id TEXT NOT NULL,
 projection_kind TEXT NOT NULL CHECK(projection_kind IN ('credential_lane','cooldown','backoff_profile','model_observation')),
 identity_codec TEXT NOT NULL CHECK(identity_codec='typed-v2'),
 payload_codec TEXT NOT NULL CHECK(payload_codec='typed-v2'),
 payload_hash TEXT NOT NULL, applied_at TEXT NOT NULL,
 UNIQUE(source_run_id, source_event_id, projection_kind));
CREATE TABLE credential_models (
 credential_id TEXT NOT NULL REFERENCES credentials(credential_id), model_id TEXT NOT NULL, lane_id TEXT NOT NULL,
 available INTEGER NOT NULL CHECK(available IN (0,1)), source_projection_id TEXT NOT NULL UNIQUE REFERENCES runtime_projection_events(projection_id),
 payload_hash TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(credential_id, model_id, lane_id));
CREATE TABLE cooldown_events (
 event_id TEXT PRIMARY KEY REFERENCES runtime_projection_events(projection_id), credential_id TEXT NOT NULL REFERENCES credentials(credential_id),
 model_id TEXT, observed_at TEXT NOT NULL, next_eligible_at_ms INTEGER,
 status TEXT NOT NULL CHECK(status IN ('cooldown','quarantined','cleared')),
 CHECK((status = 'cooldown' AND next_eligible_at_ms IS NOT NULL) OR (status IN ('quarantined','cleared') AND next_eligible_at_ms IS NULL)),
 CHECK(next_eligible_at_ms IS NULL OR next_eligible_at_ms >= 0));
CREATE TABLE cooldown_states (
 event_id TEXT PRIMARY KEY REFERENCES cooldown_events(event_id), next_eligible_at INTEGER NOT NULL,
 last_applied_seconds INTEGER, learned_seconds INTEGER, post_cooldown_successes INTEGER NOT NULL,
 policy_version TEXT NOT NULL,
 CHECK(next_eligible_at >= 0), CHECK(last_applied_seconds IS NULL OR last_applied_seconds >= 0),
 CHECK(learned_seconds IS NULL OR learned_seconds >= 0), CHECK(post_cooldown_successes >= 0));
CREATE TABLE credential_state (
 credential_id TEXT PRIMARY KEY REFERENCES credentials(credential_id), next_eligible_at_ms INTEGER,
 disabled INTEGER NOT NULL CHECK(disabled IN (0,1)), reason TEXT, source_projection_id TEXT NOT NULL REFERENCES runtime_projection_events(projection_id), updated_at TEXT NOT NULL,
 CHECK(next_eligible_at_ms IS NULL OR next_eligible_at_ms >= 0),
 CHECK((disabled = 1 AND reason = 'credential_invalid' AND next_eligible_at_ms IS NULL) OR (disabled = 0 AND reason = 'rate_limited' AND next_eligible_at_ms IS NOT NULL) OR (disabled = 0 AND reason IS NULL AND next_eligible_at_ms IS NULL)));
CREATE TABLE backoff_profiles (
 profile_key TEXT PRIMARY KEY, provider_id TEXT NOT NULL, credential_fingerprint TEXT NOT NULL, model_id TEXT NOT NULL,
 profile_version TEXT NOT NULL, baseline_seconds INTEGER NOT NULL CHECK(baseline_seconds >= 0), multiplier INTEGER NOT NULL CHECK(multiplier >= 1),
 local_max_seconds INTEGER NOT NULL CHECK(local_max_seconds >= baseline_seconds), stable_successes INTEGER NOT NULL CHECK(stable_successes >= 0),
 source_projection_id TEXT NOT NULL UNIQUE REFERENCES runtime_projection_events(projection_id), payload_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(provider_id, credential_fingerprint, model_id, profile_version));
CREATE TABLE model_observations (
 observation_id TEXT PRIMARY KEY REFERENCES runtime_projection_events(projection_id), logical_request_id TEXT NOT NULL, candidate_id TEXT,
 provider_id TEXT NOT NULL, model_id TEXT NOT NULL, credential_id TEXT NOT NULL REFERENCES credentials(credential_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('started','completed','failed','abandoned')), payload_hash TEXT NOT NULL, created_at TEXT NOT NULL);
"""

REQUIRED_COLUMNS = {
 "meta":{"key","value"}, "credentials":{"credential_id","provider_id","credential_alias","credential_fingerprint","created_at"},
 "runtime_projection_events":{"projection_id","source_run_id","source_event_id","projection_kind","identity_codec","payload_codec","payload_hash","applied_at"},
 "credential_models":{"credential_id","model_id","lane_id","available","source_projection_id","payload_hash","updated_at"},
 "cooldown_events":{"event_id","credential_id","model_id","observed_at","next_eligible_at_ms","status"},
 "cooldown_states":{"event_id","next_eligible_at","last_applied_seconds","learned_seconds","post_cooldown_successes","policy_version"},
 "credential_state":{"credential_id","next_eligible_at_ms","disabled","reason","source_projection_id","updated_at"},
 "backoff_profiles":{"profile_key","provider_id","credential_fingerprint","model_id","profile_version","baseline_seconds","multiplier","local_max_seconds","stable_successes","source_projection_id","payload_hash","updated_at"},
 "model_observations":{"observation_id","logical_request_id","candidate_id","provider_id","model_id","credential_id","event_type","payload_hash","created_at"},
}
IMMUTABLE_TABLES = ("credentials", "cooldown_events", "cooldown_states", "model_observations", "runtime_projection_events")
APPEND_ONLY_SQL = "\n".join(
    f"CREATE TRIGGER {table}_{suffix} BEFORE {operation} ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
    for table in IMMUTABLE_TABLES
    for suffix, operation in (("no_update", "UPDATE"), ("no_delete", "DELETE"))
)
