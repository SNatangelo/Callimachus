#!/usr/bin/env python3
# core/infra/db/schema.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Schema definition for DB-native run storage."""

from __future__ import annotations

from .ingest_setting_storage import INGEST_SETTING_DDL
from .execution_assurance_storage import EXECUTION_ASSURANCE_DDL
from .integrity_storage import INTEGRITY_DDL, INTEGRITY_TRIGGERS_SQL
from .task_answer_provenance_storage import TASK_ANSWER_PROVENANCE_DDL
from .unit_progress_storage import UNIT_PROGRESS_DDL, UNIT_PROGRESS_TRIGGERS_SQL
from .resolve_attempt_storage import ATTEMPT_DDL
from .resolve_transport_storage import (
    RESOLVE_TRANSPORT_DDL,
    RESOLVE_TRANSPORT_TRIGGERS,
)
from .institutional_search_provenance_storage import (
    INSTITUTIONAL_SEARCH_PROVENANCE_DDL,
    INSTITUTIONAL_SEARCH_PROVENANCE_TRIGGERS,
)
from .fetch_transport_storage import FETCH_TRANSPORT_DDL, FETCH_TRANSPORT_TRIGGERS
from .credential_observation_storage import (
    CREDENTIAL_OBSERVATION_DDL,
    CREDENTIAL_OBSERVATION_TRIGGERS,
)
from .verify_setting_storage import VERIFY_SETTING_DDL

SCHEMA_VERSION = 109


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','paused','interrupted','completed','failed','abandoned')),
  phase TEXT NOT NULL CHECK (phase IN ('parse','resolve','resolve_repair','fetch','gaps','style','verify','web_research','report','done')),
  input_path TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  accuracy TEXT NOT NULL,
  style TEXT,
  model_id TEXT,
  http_profile TEXT,
  challenge_mode TEXT,
  fixture_fingerprint TEXT NOT NULL,
  parent_run_id TEXT,
  run_origin TEXT NOT NULL DEFAULT 'fresh'
);

CREATE TABLE IF NOT EXISTS completed_remediation_provenance (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  parent_run_id TEXT NOT NULL CHECK(length(parent_run_id)>0 AND instr(parent_run_id,char(0))=0),
  parent_input_sha256 TEXT NOT NULL CHECK(length(parent_input_sha256)=64 AND parent_input_sha256 NOT GLOB '*[^0-9a-f]*'),
  parent_report_sha256 TEXT NOT NULL CHECK(length(parent_report_sha256)=64 AND parent_report_sha256 NOT GLOB '*[^0-9a-f]*'),
  parent_journal_sha256 TEXT NOT NULL CHECK(length(parent_journal_sha256)=64 AND parent_journal_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_inventory_sha256 TEXT NOT NULL CHECK(length(source_inventory_sha256)=64 AND source_inventory_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(length(created_at)>0 AND instr(created_at,char(0))=0)
);
CREATE TRIGGER IF NOT EXISTS completed_remediation_provenance_no_update
BEFORE UPDATE ON completed_remediation_provenance
BEGIN SELECT RAISE(ABORT, 'completed remediation provenance is write-once'); END;
CREATE TRIGGER IF NOT EXISTS completed_remediation_provenance_no_delete
BEFORE DELETE ON completed_remediation_provenance
BEGIN SELECT RAISE(ABORT, 'completed remediation provenance is write-once'); END;

CREATE TABLE IF NOT EXISTS run_sessions (
  session_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','completed','interrupted','failed')),
  pid INTEGER,
  host TEXT
);

""" + EXECUTION_ASSURANCE_DDL + VERIFY_SETTING_DDL + INGEST_SETTING_DDL + """
CREATE TABLE IF NOT EXISTS run_runtime_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 mailto_present INTEGER NOT NULL DEFAULT 0 CHECK(mailto_present IN(0,1)), mailto TEXT,
 max_retries_present INTEGER NOT NULL DEFAULT 0 CHECK(max_retries_present IN(0,1)), max_retries INTEGER CHECK(max_retries IS NULL OR (typeof(max_retries)='integer' AND max_retries>=0)),
 fetch_workers_present INTEGER NOT NULL DEFAULT 0 CHECK(fetch_workers_present IN(0,1)), fetch_workers INTEGER CHECK(fetch_workers IS NULL OR (typeof(fetch_workers)='integer' AND fetch_workers>=0)),
 no_fetch_present INTEGER NOT NULL DEFAULT 0 CHECK(no_fetch_present IN(0,1)), no_fetch INTEGER CHECK(no_fetch IN(0,1)),
 references_only_present INTEGER NOT NULL DEFAULT 0 CHECK(references_only_present IN(0,1)), references_only INTEGER CHECK(references_only IN(0,1)),
 verify_backends_present INTEGER NOT NULL DEFAULT 0 CHECK(verify_backends_present IN(0,1)), verify_backends TEXT,
 autonomous_present INTEGER NOT NULL DEFAULT 0 CHECK(autonomous_present IN(0,1)), autonomous INTEGER CHECK(autonomous IN(0,1)),
 ocr_lang_present INTEGER NOT NULL DEFAULT 0 CHECK(ocr_lang_present IN(0,1)), ocr_lang TEXT,
 fetch_paused_present INTEGER NOT NULL DEFAULT 0 CHECK(fetch_paused_present IN(0,1)), fetch_paused INTEGER CHECK(fetch_paused IN(0,1)),
 auto_fetch_attempted_present INTEGER NOT NULL DEFAULT 0 CHECK(auto_fetch_attempted_present IN(0,1)), auto_fetch_attempted INTEGER CHECK(auto_fetch_attempted IN(0,1)),
 style_confidence_present INTEGER NOT NULL DEFAULT 0 CHECK(style_confidence_present IN(0,1)), style_confidence TEXT,
 style_present INTEGER NOT NULL DEFAULT 0 CHECK(style_present IN(0,1)), style TEXT,
 debug_mode_present INTEGER NOT NULL DEFAULT 0 CHECK(debug_mode_present IN(0,1)), debug_mode INTEGER CHECK(debug_mode IN(0,1)),
 manual_review_present INTEGER NOT NULL DEFAULT 0 CHECK(manual_review_present IN(0,1)), manual_review INTEGER CHECK(manual_review IN(0,1)),
 parse_review_paused_present INTEGER NOT NULL DEFAULT 0 CHECK(parse_review_paused_present IN(0,1)), parse_review_paused INTEGER CHECK(parse_review_paused IN(0,1)),
 verify_table_citations_present INTEGER NOT NULL DEFAULT 0 CHECK(verify_table_citations_present IN(0,1)), verify_table_citations INTEGER CHECK(verify_table_citations IN(0,1)),
 verify_semantic_contract_present INTEGER NOT NULL DEFAULT 0 CHECK(verify_semantic_contract_present IN(0,1)), verify_semantic_contract TEXT CHECK(verify_semantic_contract IS NULL OR length(trim(verify_semantic_contract))>0),
 debug_labels_present INTEGER NOT NULL DEFAULT 0 CHECK(debug_labels_present IN(0,1)), debug_labels_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(debug_labels_count)='integer' AND debug_labels_count>=0),
 manual_review_ref_numbers_present INTEGER NOT NULL DEFAULT 0 CHECK(manual_review_ref_numbers_present IN(0,1)), manual_review_ref_numbers_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(manual_review_ref_numbers_count)='integer' AND manual_review_ref_numbers_count>=0),
 CHECK(mailto_present=1 OR mailto IS NULL),
 CHECK((max_retries_present=0 AND max_retries IS NULL) OR (max_retries_present=1 AND max_retries IS NOT NULL)),
 CHECK(fetch_workers_present=1 OR fetch_workers IS NULL),
 CHECK((no_fetch_present=0 AND no_fetch IS NULL) OR (no_fetch_present=1 AND no_fetch IS NOT NULL)),
 CHECK((references_only_present=0 AND references_only IS NULL) OR (references_only_present=1 AND references_only IS NOT NULL)),
 CHECK(verify_backends_present=1 OR verify_backends IS NULL),
 CHECK((autonomous_present=0 AND autonomous IS NULL) OR (autonomous_present=1 AND autonomous IS NOT NULL)),
 CHECK(ocr_lang_present=1 OR ocr_lang IS NULL),
 CHECK((fetch_paused_present=0 AND fetch_paused IS NULL) OR (fetch_paused_present=1 AND fetch_paused IS NOT NULL)),
 CHECK((auto_fetch_attempted_present=0 AND auto_fetch_attempted IS NULL) OR (auto_fetch_attempted_present=1 AND auto_fetch_attempted IS NOT NULL)),
 CHECK(style_confidence_present=1 OR style_confidence IS NULL),
 CHECK(style_present=1 OR style IS NULL),
 CHECK((debug_mode_present=0 AND debug_mode IS NULL) OR (debug_mode_present=1 AND debug_mode IS NOT NULL)),
 CHECK((manual_review_present=0 AND manual_review IS NULL) OR (manual_review_present=1 AND manual_review IS NOT NULL)),
 CHECK((parse_review_paused_present=0 AND parse_review_paused IS NULL) OR (parse_review_paused_present=1 AND parse_review_paused IS NOT NULL)),
 CHECK((verify_table_citations_present=0 AND verify_table_citations IS NULL) OR (verify_table_citations_present=1 AND verify_table_citations IS NOT NULL)),
 CHECK((verify_semantic_contract_present=0 AND verify_semantic_contract IS NULL) OR (verify_semantic_contract_present=1 AND verify_semantic_contract IS NOT NULL)),
 CHECK(debug_labels_present=1 OR debug_labels_count=0),
 CHECK(manual_review_ref_numbers_present=1 OR manual_review_ref_numbers_count=0)
);
INSERT OR IGNORE INTO run_runtime_settings(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS run_runtime_debug_labels (
 label_order INTEGER PRIMARY KEY CHECK(typeof(label_order)='integer' AND label_order>=0), value TEXT NOT NULL CHECK(length(value)>0 AND instr(value,char(0))=0)
);
CREATE TABLE IF NOT EXISTS run_runtime_manual_review_ref_numbers (
 ref_number INTEGER PRIMARY KEY CHECK(typeof(ref_number)='integer' AND ref_number>0)
);
CREATE TABLE IF NOT EXISTS run_config_snapshot (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phase_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  phase TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('enter','exit','pause','resume','fail','done')),
  created_at TEXT NOT NULL,
  session_id TEXT REFERENCES run_sessions(session_id),
  detail_kind TEXT NOT NULL CHECK (detail_kind IN ('none','session_created','session_resumed','pause','restart','gate_failure','frozen_fork')),
  CHECK (
    (detail_kind='none' AND event_type IN ('enter','exit','fail','done'))
    OR (detail_kind='session_created' AND event_type='enter' AND session_id IS NOT NULL)
    OR (detail_kind='session_resumed' AND event_type='resume' AND session_id IS NOT NULL)
    OR (detail_kind='pause' AND event_type='pause')
    OR (detail_kind='restart' AND phase='parse' AND event_type='resume' AND session_id IS NULL)
    OR (detail_kind='gate_failure' AND event_type='fail')
    OR (detail_kind='frozen_fork' AND phase IN ('fetch','verify') AND event_type='enter' AND session_id IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS phase_event_session_created (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  created_via TEXT NOT NULL CHECK (created_via='core.run')
);
CREATE TABLE IF NOT EXISTS phase_event_session_resumed (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  resumed_via TEXT NOT NULL CHECK (resumed_via='core.run')
);
CREATE TABLE IF NOT EXISTS phase_event_pause (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  slot TEXT NOT NULL CHECK (slot IN ('fetch','research','verify','parse_review')),
  pending_tasks INTEGER NOT NULL CHECK (typeof(pending_tasks)='integer' AND pending_tasks>=0)
);
CREATE TABLE IF NOT EXISTS phase_event_restart (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  restart_reason TEXT NOT NULL CHECK (length(trim(restart_reason))>0)
);
CREATE TABLE IF NOT EXISTS phase_event_gate_failure (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  reason TEXT NOT NULL CHECK (reason='gate_failed')
);
CREATE TABLE IF NOT EXISTS phase_event_frozen_fork (
  event_id INTEGER PRIMARY KEY REFERENCES phase_events(event_id),
  run_origin TEXT NOT NULL CHECK (run_origin IN (
    'forked_from_frozen_fetch','forked_from_completed_verify',
    'forked_from_reference_only'
  )),
  parent_run_id TEXT NOT NULL CHECK (length(trim(parent_run_id))>0),
  source_inventory_sha256 TEXT NOT NULL CHECK (length(source_inventory_sha256)=64 AND source_inventory_sha256 NOT GLOB '*[^0123456789abcdef]*')
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  sentence TEXT NOT NULL,
  context_window TEXT,
  marker_raw TEXT,
  claim_scope TEXT CHECK (
    claim_scope IS NULL OR claim_scope IN ('sentence', 'sentence_fragment')
  ),
  parser_sentence_index INTEGER CHECK (
    parser_sentence_index IS NULL OR (
      typeof(parser_sentence_index) = 'integer' AND parser_sentence_index >= 0
    )
  ),
  marker_group_index INTEGER CHECK (
    marker_group_index IS NULL OR (
      typeof(marker_group_index) = 'integer' AND marker_group_index >= 0
    )
  ),
  marker_group_count INTEGER CHECK (
    marker_group_count IS NULL OR (
      typeof(marker_group_count) = 'integer' AND marker_group_count > 0
    )
  ),
  marker_start INTEGER CHECK (
    marker_start IS NULL OR (typeof(marker_start) = 'integer' AND marker_start >= 0)
  ),
  marker_end INTEGER CHECK (
    marker_end IS NULL OR (typeof(marker_end) = 'integer' AND marker_end > 0)
  ),
  structural_provenance_json TEXT,
  claim_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manuscript_text (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  content TEXT NOT NULL CHECK (length(content) > 0 AND instr(content, char(0)) = 0),
  sha256 TEXT NOT NULL CHECK (
    length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  char_count INTEGER NOT NULL CHECK (
    typeof(char_count) = 'integer' AND char_count > 0 AND char_count = length(content)
  )
);

CREATE TABLE IF NOT EXISTS manuscript_identity (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  input_sha256 TEXT NOT NULL CHECK (
    length(input_sha256) > 0 AND instr(input_sha256, char(0)) = 0
  ),
  local_title TEXT,
  local_status TEXT NOT NULL CHECK (
    local_status IN ('validated_local', 'inferred_local', 'unknown')
  ),
  local_method TEXT NOT NULL CHECK (
    length(local_method) > 0 AND instr(local_method, char(0)) = 0
  ),
  metadata_title TEXT,
  layout_title TEXT,
  selected_identifier_scheme TEXT,
  selected_identifier_value TEXT,
  resolution_status TEXT NOT NULL CHECK (
    resolution_status IN ('not_attempted', 'resolved', 'not_found', 'unresolved', 'error', 'conflict')
  ),
  resolved_identifier_scheme TEXT,
  resolved_identifier_value TEXT,
  resolved_title TEXT,
  resolved_via TEXT,
  resolution_attempt_state TEXT NOT NULL DEFAULT 'attempts_not_produced' CHECK (
    resolution_attempt_state IN ('produced','attempts_not_produced')
  ),
  resolution_attempts_json TEXT,
  final_title TEXT,
  final_status TEXT NOT NULL CHECK (
    final_status IN ('validated_identifier', 'validated_local', 'inferred_local', 'unknown')
  ),
  final_method TEXT NOT NULL CHECK (
    length(final_method) > 0 AND instr(final_method, char(0)) = 0
  ),
  reason TEXT,
  resolved_at TEXT,
  CHECK (local_title IS NULL OR (length(trim(local_title)) > 0 AND instr(local_title, char(0)) = 0)),
  CHECK (metadata_title IS NULL OR (length(trim(metadata_title)) > 0 AND instr(metadata_title, char(0)) = 0)),
  CHECK (layout_title IS NULL OR (length(trim(layout_title)) > 0 AND instr(layout_title, char(0)) = 0)),
  CHECK (resolved_title IS NULL OR (length(trim(resolved_title)) > 0 AND instr(resolved_title, char(0)) = 0)),
  CHECK (final_title IS NULL OR (length(trim(final_title)) > 0 AND instr(final_title, char(0)) = 0)),
  CHECK ((local_status = 'unknown' AND local_title IS NULL) OR (local_status != 'unknown' AND local_title IS NOT NULL)),
  CHECK ((final_status = 'unknown' AND final_title IS NULL) OR (final_status != 'unknown' AND final_title IS NOT NULL)),
  CHECK (
    (selected_identifier_scheme IS NULL AND selected_identifier_value IS NULL)
    OR (selected_identifier_scheme IS NOT NULL AND selected_identifier_value IS NOT NULL)
  ),
  CHECK (
    (resolved_identifier_scheme IS NULL AND resolved_identifier_value IS NULL)
    OR (resolved_identifier_scheme IS NOT NULL AND resolved_identifier_value IS NOT NULL)
  ),
  CHECK (
    (resolution_attempt_state = 'produced' AND resolution_attempts_json IS NOT NULL)
    OR (resolution_attempt_state = 'attempts_not_produced' AND resolution_attempts_json IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS manuscript_identity_identifiers (
  manuscript_singleton INTEGER NOT NULL DEFAULT 1
    REFERENCES manuscript_identity(singleton) ON DELETE CASCADE
    CHECK (manuscript_singleton = 1),
  identifier_order INTEGER NOT NULL CHECK (
    typeof(identifier_order) = 'integer' AND identifier_order >= 0
  ),
  scheme TEXT NOT NULL CHECK (scheme IN ('doi', 'pmid', 'isbn', 'url', 'arxiv_id')),
  value TEXT NOT NULL CHECK (length(trim(value)) > 0 AND instr(value, char(0)) = 0),
  source TEXT NOT NULL CHECK (length(trim(source)) > 0 AND instr(source, char(0)) = 0),
  PRIMARY KEY (manuscript_singleton, identifier_order),
  UNIQUE (manuscript_singleton, scheme, value)
);

CREATE TABLE IF NOT EXISTS claim_marker_members (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
  member_order INTEGER NOT NULL CHECK (
    typeof(member_order) = 'integer' AND member_order >= 0
  ),
  marker_number INTEGER NOT NULL CHECK (typeof(marker_number) = 'integer'),
  PRIMARY KEY (claim_id, member_order)
);

CREATE TABLE IF NOT EXISTS reference_entries (
  ref_id TEXT PRIMARY KEY,
  ref_number INTEGER NOT NULL,
  raw_entry TEXT NOT NULL,
  title TEXT,
  doi TEXT,
  pmid TEXT,
  isbn TEXT,
  url TEXT,
  year INTEGER,
  ay_surname TEXT,
  ay_year INTEGER,
  ay_suffix TEXT,
  source_type TEXT,
  source_kind TEXT,
  indexability TEXT,
  source_type_confidence TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_entries_ref_number
ON reference_entries(ref_number);

-- Cited coordinates are immutable Parse facts.  They remain distinct from
-- metadata returned by providers and each value retains its exact raw spans.
CREATE TABLE IF NOT EXISTS cited_bibliographic_coordinates (
  coordinate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  coordinate_kind TEXT NOT NULL CHECK(coordinate_kind IN (
    'container','repository','volume','issue','article_page_range','chapter_page_range',
    'elocator','article_number','article_locator'
  )),
  raw_value TEXT NOT NULL CHECK(length(raw_value)>0 AND instr(raw_value,char(0))=0),
  normalized_value TEXT NOT NULL CHECK(length(normalized_value)>0 AND instr(normalized_value,char(0))=0),
  rule_id TEXT NOT NULL CHECK(length(rule_id)>0 AND instr(rule_id,char(0))=0),
  extractor_version TEXT NOT NULL CHECK(extractor_version='cited-coordinates/v1'),
  UNIQUE(ref_id, coordinate_kind)
);
CREATE TABLE IF NOT EXISTS cited_bibliographic_coordinate_spans (
  coordinate_id INTEGER NOT NULL REFERENCES cited_bibliographic_coordinates(coordinate_id) ON DELETE CASCADE,
  span_order INTEGER NOT NULL CHECK(typeof(span_order)='integer' AND span_order>=0),
  raw_start INTEGER NOT NULL CHECK(typeof(raw_start)='integer' AND raw_start>=0),
  raw_end INTEGER NOT NULL CHECK(typeof(raw_end)='integer' AND raw_end>raw_start),
  PRIMARY KEY(coordinate_id, span_order)
);

-- Operational consumers can process immutable raw Parse references and
-- post-Parse manual split children.  Parse-time relationships remain tied to
-- reference_entries; only Resolve/Fetch/task state uses this supertype.
CREATE TABLE IF NOT EXISTS operational_references (
  ref_id TEXT PRIMARY KEY,
  ref_number INTEGER NOT NULL UNIQUE,
  provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('raw_parse','manual_footnote_split')),
  parse_ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
  note_id TEXT REFERENCES footnote_notes(note_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
  answer_id TEXT REFERENCES task_answers(answer_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
  created_at TEXT NOT NULL,
  CHECK(
    (provenance_kind='raw_parse' AND ref_id=parse_ref_id AND note_id IS NULL AND answer_id IS NULL)
    OR (provenance_kind='manual_footnote_split' AND ref_id<>parse_ref_id AND note_id IS NOT NULL AND answer_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS footnote_notes (
  note_id TEXT PRIMARY KEY,
  manuscript_id TEXT NOT NULL CHECK(length(manuscript_id)>0 AND instr(manuscript_id,char(0))=0),
  note_number INTEGER NOT NULL CHECK(typeof(note_number)='integer' AND note_number > 0),
  raw_note TEXT NOT NULL CHECK(length(raw_note)>0 AND instr(raw_note,char(0))=0),
  extraction_status TEXT NOT NULL CHECK(extraction_status IN ('no_sources','sources_extracted','ambiguous')),
  UNIQUE(manuscript_id, note_number)
);
CREATE TABLE IF NOT EXISTS footnote_note_sources (
  note_id TEXT NOT NULL REFERENCES footnote_notes(note_id) ON DELETE CASCADE,
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  source_order INTEGER NOT NULL CHECK(typeof(source_order)='integer' AND source_order>=0),
  raw_start INTEGER NOT NULL CHECK(typeof(raw_start)='integer' AND raw_start>=0),
  raw_end INTEGER NOT NULL CHECK(typeof(raw_end)='integer' AND raw_end>raw_start),
  PRIMARY KEY(note_id, source_order),
  UNIQUE(note_id, ref_id)
);
CREATE TABLE IF NOT EXISTS footnote_note_parents (
  note_id TEXT PRIMARY KEY REFERENCES footnote_notes(note_id) ON DELETE CASCADE,
  ref_id TEXT NOT NULL UNIQUE REFERENCES reference_entries(ref_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS claim_footnotes (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
  note_id TEXT NOT NULL REFERENCES footnote_notes(note_id) ON DELETE CASCADE,
  PRIMARY KEY(claim_id, note_id)
);

-- Parse-time table markers are provenance-bearing facts, not an opaque setting.
-- The singleton distinguishes an unrecorded parse from a recorded empty marker set.
CREATE TABLE IF NOT EXISTS parse_table_citation_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  present INTEGER NOT NULL DEFAULT 0 CHECK(present IN(0,1)),
  marker_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(marker_count)='integer' AND marker_count>=0),
  CHECK(present=1 OR marker_count=0)
);
INSERT OR IGNORE INTO parse_table_citation_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS parse_table_citation_markers (
  marker_order INTEGER PRIMARY KEY CHECK(typeof(marker_order)='integer' AND marker_order>=0),
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  marker_raw TEXT NOT NULL CHECK(length(marker_raw)>0 AND instr(marker_raw,char(0))=0)
);
CREATE TRIGGER IF NOT EXISTS parse_table_citation_state_no_delete
BEFORE DELETE ON parse_table_citation_state
BEGIN SELECT RAISE(ABORT, 'parse table citation state is required'); END;
CREATE TRIGGER IF NOT EXISTS parse_table_citation_marker_insert_guard
BEFORE INSERT ON parse_table_citation_markers
WHEN NOT EXISTS (SELECT 1 FROM parse_table_citation_state WHERE singleton=1 AND present=1 AND NEW.marker_order < marker_count)
BEGIN SELECT RAISE(ABORT, 'parse table citation storage is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS parse_table_citation_state_update_guard
BEFORE UPDATE OF present,marker_count ON parse_table_citation_state
WHEN (NEW.present=0 AND EXISTS(SELECT 1 FROM parse_table_citation_markers))
  OR (NEW.marker_count < (SELECT count(*) FROM parse_table_citation_markers))
BEGIN SELECT RAISE(ABORT, 'parse table citation storage is inconsistent'); END;
-- A PDF hyperlink can prove that a bibliography entry is cited even when text
-- extraction cannot recover a claim sentence for the marker.  These are
-- provenance-bearing coverage facts, not claim/reference edges to verify.
CREATE TABLE IF NOT EXISTS parse_link_silent_citation_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  present INTEGER NOT NULL DEFAULT 0 CHECK(present IN(0,1)),
  marker_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(marker_count)='integer' AND marker_count>=0),
  CHECK(present=1 OR marker_count=0)
);
INSERT OR IGNORE INTO parse_link_silent_citation_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS parse_link_silent_citation_markers (
  marker_order INTEGER PRIMARY KEY CHECK(typeof(marker_order)='integer' AND marker_order>=0),
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  marker_raw TEXT NOT NULL CHECK(length(marker_raw)>0 AND instr(marker_raw,char(0))=0),
  provenance TEXT NOT NULL CHECK(provenance='link_silent_resolved')
);
CREATE TRIGGER IF NOT EXISTS parse_link_silent_citation_state_no_delete
BEFORE DELETE ON parse_link_silent_citation_state
BEGIN SELECT RAISE(ABORT, 'parse link-silent citation state is required'); END;
CREATE TRIGGER IF NOT EXISTS parse_link_silent_citation_marker_insert_guard
BEFORE INSERT ON parse_link_silent_citation_markers
WHEN NOT EXISTS (
  SELECT 1 FROM parse_link_silent_citation_state
  WHERE singleton=1 AND present=1 AND NEW.marker_order < marker_count
)
BEGIN SELECT RAISE(ABORT, 'parse link-silent citation storage is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS parse_link_silent_citation_state_update_guard
BEFORE UPDATE OF present,marker_count ON parse_link_silent_citation_state
WHEN (NEW.present=0 AND EXISTS(SELECT 1 FROM parse_link_silent_citation_markers))
  OR (NEW.marker_count < (SELECT count(*) FROM parse_link_silent_citation_markers))
BEGIN SELECT RAISE(ABORT, 'parse link-silent citation storage is inconsistent'); END;
CREATE TABLE IF NOT EXISTS parse_coverage_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  superscript_source_kind TEXT NOT NULL DEFAULT 'absent' CHECK(superscript_source_kind IN ('absent','null','text')),
  superscript_source TEXT,
  further_reading_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(further_reading_count)='integer' AND further_reading_count>=0),
  CHECK((superscript_source_kind='text' AND superscript_source IS NOT NULL AND length(superscript_source)>0 AND instr(superscript_source,char(0))=0) OR (superscript_source_kind!='text' AND superscript_source IS NULL))
);
INSERT OR IGNORE INTO parse_coverage_state(singleton) VALUES(1);
CREATE TRIGGER IF NOT EXISTS parse_coverage_state_no_delete BEFORE DELETE ON parse_coverage_state BEGIN SELECT RAISE(ABORT, 'parse coverage state is required'); END;
CREATE TABLE IF NOT EXISTS parse_coverage_further_reading (
  ref_id TEXT PRIMARY KEY REFERENCES reference_entries(ref_id) ON DELETE CASCADE
);
CREATE TRIGGER IF NOT EXISTS parse_coverage_further_reading_insert_guard
BEFORE INSERT ON parse_coverage_further_reading
WHEN NOT EXISTS (SELECT 1 FROM parse_coverage_state WHERE singleton=1 AND (SELECT count(*) FROM parse_coverage_further_reading) < further_reading_count)
BEGIN SELECT RAISE(ABORT, 'parse coverage storage is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS parse_coverage_state_count_lowering_guard
BEFORE UPDATE OF further_reading_count ON parse_coverage_state
WHEN NEW.further_reading_count < (SELECT count(*) FROM parse_coverage_further_reading)
BEGIN SELECT RAISE(ABORT, 'parse coverage storage is inconsistent'); END;

CREATE TABLE IF NOT EXISTS citations (
  citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  ref_number INTEGER NOT NULL,
  UNIQUE (claim_id, ref_id)
);

-- Unresolved Parse facts are deliberately separate from resolved citation edges:
-- citations.ref_id remains a non-null verified edge.
CREATE TABLE IF NOT EXISTS unresolved_citations (
  occurrence_id TEXT PRIMARY KEY,
  occurrence_order INTEGER NOT NULL CHECK(occurrence_order>=0),
  claim_id TEXT REFERENCES claims(claim_id) ON DELETE CASCADE,
  marker_raw TEXT NOT NULL,
  finding_kind TEXT NOT NULL CHECK(finding_kind IN ('ambiguity','orphan')),
  ref_number INTEGER,
  surname TEXT,
  citation_year INTEGER,
  canonical TEXT,
  provenance_json TEXT NOT NULL,
  raw_citation_present INTEGER NOT NULL CHECK(raw_citation_present IN(0,1)),
  raw_citation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unresolved_citation_candidates (
  occurrence_id TEXT NOT NULL REFERENCES unresolved_citations(occurrence_id) ON DELETE CASCADE,
  candidate_order INTEGER NOT NULL CHECK(candidate_order>=0),
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE RESTRICT,
  PRIMARY KEY(occurrence_id,candidate_order), UNIQUE(occurrence_id,ref_id)
);
CREATE TABLE IF NOT EXISTS manual_citation_attribution_overrides (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE RESTRICT,
  occurrence_id TEXT REFERENCES unresolved_citations(occurrence_id) ON DELETE RESTRICT,
  claim_id TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
  ref_id TEXT NOT NULL REFERENCES reference_entries(ref_id) ON DELETE RESTRICT,
  direction TEXT NOT NULL CHECK(direction IN ('citation_to_reference','reference_to_claim')),
  candidate_origin TEXT NOT NULL CHECK(candidate_origin IN ('parser','orphan_match','orphan_match_inverse')),
  candidate_score REAL NOT NULL CHECK(candidate_score=candidate_score AND candidate_score>=0.0 AND candidate_score<=1.0),
  answer_id TEXT NOT NULL UNIQUE REFERENCES task_answers(answer_id) ON DELETE RESTRICT,
  applied_at TEXT NOT NULL,
  CHECK((direction='citation_to_reference' AND occurrence_id IS NOT NULL AND claim_id IS NULL)
     OR (direction='reference_to_claim' AND occurrence_id IS NULL AND claim_id IS NOT NULL)),
  CHECK((direction='citation_to_reference' AND candidate_origin IN ('parser','orphan_match')) OR (direction='reference_to_claim' AND candidate_origin='orphan_match_inverse'))
);

CREATE TABLE IF NOT EXISTS reference_identity (
  ref_id TEXT PRIMARY KEY REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  identity_key TEXT NOT NULL,
  identity_scheme TEXT NOT NULL CHECK (identity_scheme IN ('doi','pmid','isbn','url','title_author_year','raw_entry')),
  canonical_doi TEXT,
  canonical_pmid TEXT,
  canonical_isbn TEXT,
  canonical_url TEXT,
  normalized_title TEXT,
  normalized_author_year TEXT,
  identity_status TEXT NOT NULL CHECK (identity_status IN ('exact_identifier','cited_url_reachable','corroborated_bibliography','weak_match','unverified'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_identity_key
ON reference_identity(identity_key);

CREATE TABLE IF NOT EXISTS resolve_results (
  ref_id TEXT PRIMARY KEY REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  via TEXT,
  matched_title TEXT,
  abstract TEXT,
  abstract_via TEXT,
  retracted INTEGER NOT NULL DEFAULT 0,
  fulltext_exists TEXT NOT NULL DEFAULT 'null' CHECK (fulltext_exists IN ('true','false','unknown','null')),
  oa_status TEXT,
  work_type TEXT,
  resolution_basis TEXT,
  existence_confidence TEXT,
  reason TEXT,
  reference_status_tag TEXT,
  fabrication_risk TEXT,
  resolved_identifier_type TEXT,
  resolved_identifier_value TEXT,
  resolved_identifier_validated_via TEXT,
  updated_at TEXT NOT NULL,
  tag_reason TEXT,
  CHECK (
    (
      resolved_identifier_type IS NULL
      AND resolved_identifier_value IS NULL
      AND resolved_identifier_validated_via IS NULL
    )
    OR (
      resolved_identifier_type IS NOT NULL
      AND resolved_identifier_value IS NOT NULL
      AND length(trim(resolved_identifier_type)) > 0
      AND length(trim(resolved_identifier_value)) > 0
      AND (resolved_identifier_validated_via IS NULL OR length(trim(resolved_identifier_validated_via)) > 0)
    )
  )
);

CREATE TABLE IF NOT EXISTS resolve_trace_state (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_results(ref_id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('produced','trace_not_produced')),
  stage_count INTEGER NOT NULL CHECK(typeof(stage_count)='integer' AND stage_count>=0),
  CHECK(
    (state='produced' AND stage_count BETWEEN 4 AND 8)
    OR (state='trace_not_produced' AND stage_count=0)
  )
);
CREATE TABLE IF NOT EXISTS resolve_trace_stages (
  ref_id TEXT NOT NULL REFERENCES resolve_trace_state(ref_id) ON DELETE CASCADE,
  stage_order INTEGER NOT NULL CHECK(typeof(stage_order)='integer' AND stage_order>=0),
  stage TEXT NOT NULL CHECK(stage IN ('declared_present','declared_cache_lookup','declared_validation','strong_discovery','discovered_validation','strong_confirmed','weak_aggregation','retraction_check','final_resolution')),
  outcome TEXT NOT NULL CHECK(typeof(outcome)='text' AND length(trim(outcome))>0 AND instr(outcome,char(0))=0),
  PRIMARY KEY(ref_id,stage_order),
  CHECK(
    (stage='declared_present' AND outcome IN ('present','absent'))
    OR (stage='declared_cache_lookup' AND outcome='not_checked')
    OR (stage='declared_validation' AND outcome IN ('matched','failed','inconclusive'))
    OR (stage='strong_discovery' AND outcome='found')
    OR (stage='discovered_validation' AND outcome='matched')
    OR (stage='strong_confirmed' AND outcome='confirmed')
    OR (stage IN ('weak_aggregation','final_resolution') AND outcome IN ('resolved_strong_declared','resolved_strong_discovered','declared_identifier_failed','weakly_corroborated','unverified','fabricated'))
    OR (stage='retraction_check' AND outcome IN ('not_retracted','retracted','unknown'))
  )
);
CREATE TABLE IF NOT EXISTS resolve_trace_identifier_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  class_ TEXT NOT NULL CHECK(class_ IN ('global','authority_local','none')),
  is_strong INTEGER NOT NULL CHECK(typeof(is_strong)='integer' AND is_strong IN(0,1)),
  scheme TEXT NOT NULL CHECK(typeof(scheme)='text' AND length(trim(scheme))>0 AND instr(scheme,char(0))=0),
  value TEXT NOT NULL CHECK(typeof(value)='text' AND instr(value,char(0))=0),
  PRIMARY KEY(ref_id,stage_order),
  FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE,
  CHECK(
    (scheme IN ('doi','pmid','isbn','arxiv_id','pmcid') AND class_='global' AND is_strong=1 AND length(trim(value))>0)
    OR (scheme IN ('acl_id','ssrn_abstract_id') AND class_='authority_local' AND is_strong=1 AND length(trim(value))>0)
    OR (scheme='url' AND class_='authority_local' AND is_strong=0 AND length(trim(value))>0)
    OR (scheme='none' AND class_='none' AND is_strong=0 AND value='')
  )
);
CREATE TABLE IF NOT EXISTS resolve_trace_reason_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK(reason='resolver has no declared-identity cache gate'),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_trace_status_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(typeof(status)='text' AND length(trim(status))>0 AND instr(status,char(0))=0),
  via TEXT CHECK(via IS NULL OR (typeof(via)='text' AND length(trim(via))>0 AND instr(via,char(0))=0)),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_trace_confirmed_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  identity_state TEXT NOT NULL CHECK(identity_state IN ('resolved_strong_declared','resolved_strong_discovered')),
  resolution_basis TEXT CHECK(resolution_basis IS NULL OR (typeof(resolution_basis)='text' AND length(trim(resolution_basis))>0 AND instr(resolution_basis,char(0))=0)),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_trace_weak_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(typeof(status)='text' AND length(trim(status))>0 AND instr(status,char(0))=0),
  reference_status_tag TEXT CHECK(reference_status_tag IS NULL OR (typeof(reference_status_tag)='text' AND length(trim(reference_status_tag))>0 AND instr(reference_status_tag,char(0))=0)),
  fabrication_risk TEXT CHECK(fabrication_risk IS NULL OR (typeof(fabrication_risk)='text' AND length(trim(fabrication_risk))>0 AND instr(fabrication_risk,char(0))=0)),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_trace_retraction_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  checked_via TEXT NOT NULL CHECK(checked_via IN ('retraction_watch','authority_metadata','none')),
  retracted INTEGER NOT NULL CHECK(typeof(retracted)='integer' AND retracted IN(0,1)),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_trace_final_details (
  ref_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(typeof(status)='text' AND length(trim(status))>0 AND instr(status,char(0))=0),
  via TEXT CHECK(via IS NULL OR (typeof(via)='text' AND length(trim(via))>0 AND instr(via,char(0))=0)),
  PRIMARY KEY(ref_id,stage_order), FOREIGN KEY(ref_id,stage_order) REFERENCES resolve_trace_stages(ref_id,stage_order) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS resolve_trace_state_no_update
BEFORE UPDATE ON resolve_trace_state BEGIN SELECT RAISE(ABORT, 'resolve trace state is replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_stage_insert_guard
BEFORE INSERT ON resolve_trace_stages
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_trace_state
  WHERE ref_id=NEW.ref_id AND state='produced' AND NEW.stage_order<stage_count
)
BEGIN SELECT RAISE(ABORT, 'resolve trace stage is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_stage_no_update
BEFORE UPDATE ON resolve_trace_stages BEGIN SELECT RAISE(ABORT, 'resolve trace stages are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_identifier_kind_guard
BEFORE INSERT ON resolve_trace_identifier_details
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_trace_stages
  WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order
    AND stage IN ('declared_present','strong_discovery')
    AND (stage!='strong_discovery' OR NEW.is_strong=1)
)
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_reason_kind_guard
BEFORE INSERT ON resolve_trace_reason_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage='declared_cache_lookup')
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_status_kind_guard
BEFORE INSERT ON resolve_trace_status_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage IN ('declared_validation','discovered_validation'))
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_confirmed_kind_guard
BEFORE INSERT ON resolve_trace_confirmed_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage='strong_confirmed')
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_weak_kind_guard
BEFORE INSERT ON resolve_trace_weak_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage='weak_aggregation')
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_retraction_kind_guard
BEFORE INSERT ON resolve_trace_retraction_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage='retraction_check')
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_final_kind_guard
BEFORE INSERT ON resolve_trace_final_details
WHEN NOT EXISTS (SELECT 1 FROM resolve_trace_stages WHERE ref_id=NEW.ref_id AND stage_order=NEW.stage_order AND stage='final_resolution')
BEGIN SELECT RAISE(ABORT, 'resolve trace detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_identifier_no_update BEFORE UPDATE ON resolve_trace_identifier_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_reason_no_update BEFORE UPDATE ON resolve_trace_reason_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_status_no_update BEFORE UPDATE ON resolve_trace_status_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_confirmed_no_update BEFORE UPDATE ON resolve_trace_confirmed_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_weak_no_update BEFORE UPDATE ON resolve_trace_weak_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_retraction_no_update BEFORE UPDATE ON resolve_trace_retraction_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_trace_final_no_update BEFORE UPDATE ON resolve_trace_final_details BEGIN SELECT RAISE(ABORT, 'resolve trace details are replace-only'); END;

CREATE TABLE IF NOT EXISTS resolve_fulltext_link_sets (
  ref_id TEXT NOT NULL REFERENCES resolve_results(ref_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('primary','auxiliary')),
  is_null INTEGER NOT NULL CHECK (is_null IN (0,1)),
  PRIMARY KEY (ref_id, role)
);
CREATE TABLE IF NOT EXISTS resolve_fulltext_links (
  ref_id TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('primary','auxiliary')),
  link_order INTEGER NOT NULL CHECK (typeof(link_order)='integer' AND link_order >= 0),
  url TEXT NOT NULL CHECK (length(trim(url)) > 0), availability TEXT, availability_present INTEGER NOT NULL CHECK(availability_present IN (0,1)), site TEXT, site_present INTEGER NOT NULL CHECK(site_present IN (0,1)), content_type TEXT, content_type_present INTEGER NOT NULL CHECK(content_type_present IN (0,1)),
  content_version TEXT, content_version_present INTEGER NOT NULL CHECK(content_version_present IN (0,1)), intended_application TEXT, intended_application_present INTEGER NOT NULL CHECK(intended_application_present IN (0,1)), discovered_via TEXT, discovered_via_present INTEGER NOT NULL CHECK(discovered_via_present IN (0,1)),
  identity_context_conflict INTEGER CHECK (identity_context_conflict IN (0,1)),
  provenance_present INTEGER NOT NULL CHECK (provenance_present IN (0,1)),
  identity_context_present INTEGER NOT NULL CHECK(identity_context_present IN (0,1)), identity_contexts_present INTEGER NOT NULL CHECK(identity_contexts_present IN (0,1)),
  CHECK(availability_present=1 OR availability IS NULL),
  CHECK(site_present=1 OR site IS NULL),
  CHECK(content_type_present=1 OR content_type IS NULL),
  CHECK(content_version_present=1 OR content_version IS NULL),
  CHECK(intended_application_present=1 OR intended_application IS NULL),
  CHECK(discovered_via_present=1 OR discovered_via IS NULL),
  PRIMARY KEY(ref_id,role,link_order), UNIQUE(ref_id,role,url),
  FOREIGN KEY(ref_id,role) REFERENCES resolve_fulltext_link_sets(ref_id,role) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_fulltext_link_provenance (
  ref_id TEXT NOT NULL, role TEXT NOT NULL, link_order INTEGER NOT NULL,
  provenance_order INTEGER NOT NULL CHECK (provenance_order >= 0), value TEXT NOT NULL,
  PRIMARY KEY(ref_id,role,link_order,provenance_order),
  FOREIGN KEY(ref_id,role,link_order) REFERENCES resolve_fulltext_links(ref_id,role,link_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_fulltext_link_contexts (
  ref_id TEXT NOT NULL, role TEXT NOT NULL, link_order INTEGER NOT NULL,
  context_kind TEXT NOT NULL CHECK(context_kind IN ('primary','accumulated')), context_order INTEGER NOT NULL CHECK (context_order >= 0), title TEXT, title_present INTEGER NOT NULL CHECK(title_present IN (0,1)),
  year_kind TEXT NOT NULL CHECK(year_kind IN ('absent','null','integer','text')),
  year_integer INTEGER, year_text TEXT,
  provider TEXT, provider_present INTEGER NOT NULL CHECK(provider_present IN (0,1)), provider_record_id TEXT, provider_record_id_present INTEGER NOT NULL CHECK(provider_record_id_present IN (0,1)), first_author TEXT, first_author_present INTEGER NOT NULL CHECK(first_author_present IN (0,1)), source_confidence REAL, source_confidence_present INTEGER NOT NULL CHECK(source_confidence_present IN (0,1)),
  canonical_host INTEGER CHECK(canonical_host IN (0,1)), canonical_host_present INTEGER NOT NULL CHECK(canonical_host_present IN (0,1)), canonical_url TEXT, canonical_url_present INTEGER NOT NULL CHECK(canonical_url_present IN (0,1)), landing_page_url TEXT, landing_page_url_present INTEGER NOT NULL CHECK(landing_page_url_present IN (0,1)), expected_document_title TEXT, expected_document_title_present INTEGER NOT NULL CHECK(expected_document_title_present IN (0,1)), official INTEGER CHECK(official IN (0,1)), official_present INTEGER NOT NULL CHECK(official_present IN (0,1)), official_document_relation TEXT, official_document_relation_present INTEGER NOT NULL CHECK(official_document_relation_present IN (0,1)),
  authors_present INTEGER NOT NULL CHECK(authors_present IN (0,1)),
  identifiers_present INTEGER NOT NULL CHECK(identifiers_present IN (0,1)),
  CHECK(title_present=1 OR title IS NULL),
  CHECK(provider_present=1 OR provider IS NULL),
  CHECK(provider_record_id_present=1 OR provider_record_id IS NULL), CHECK(first_author_present=1 OR first_author IS NULL),
  CHECK(source_confidence_present=1 OR source_confidence IS NULL),
  CHECK((canonical_host_present=0 AND canonical_host IS NULL) OR (canonical_host_present=1 AND canonical_host IN (0,1))), CHECK(canonical_url_present=1 OR canonical_url IS NULL), CHECK(landing_page_url_present=1 OR landing_page_url IS NULL), CHECK(expected_document_title_present=1 OR expected_document_title IS NULL), CHECK((official_present=0 AND official IS NULL) OR (official_present=1 AND official IN (0,1))), CHECK(official_document_relation_present=1 OR official_document_relation IS NULL),
  CHECK(
    (year_kind IN ('absent','null') AND year_integer IS NULL AND year_text IS NULL)
    OR (year_kind='integer' AND typeof(year_integer)='integer' AND year_text IS NULL)
    OR (year_kind='text' AND year_integer IS NULL AND typeof(year_text)='text')
  ),
  PRIMARY KEY(ref_id,role,link_order,context_kind,context_order),
  FOREIGN KEY(ref_id,role,link_order) REFERENCES resolve_fulltext_links(ref_id,role,link_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_fulltext_link_context_authors (
  ref_id TEXT NOT NULL, role TEXT NOT NULL, link_order INTEGER NOT NULL, context_kind TEXT NOT NULL, context_order INTEGER NOT NULL,
  author_order INTEGER NOT NULL CHECK(author_order >= 0), author TEXT NOT NULL,
  PRIMARY KEY(ref_id,role,link_order,context_kind,context_order,author_order),
  FOREIGN KEY(ref_id,role,link_order,context_kind,context_order) REFERENCES resolve_fulltext_link_contexts(ref_id,role,link_order,context_kind,context_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_fulltext_link_context_identifiers (
  ref_id TEXT NOT NULL, role TEXT NOT NULL, link_order INTEGER NOT NULL, context_kind TEXT NOT NULL, context_order INTEGER NOT NULL,
  identifier_type TEXT NOT NULL, identifier_value TEXT NOT NULL,
  PRIMARY KEY(ref_id,role,link_order,context_kind,context_order,identifier_type),
  FOREIGN KEY(ref_id,role,link_order,context_kind,context_order) REFERENCES resolve_fulltext_link_contexts(ref_id,role,link_order,context_kind,context_order) ON DELETE CASCADE
);

-- Evidence profiles are a small, closed Resolve domain, not an opaque
-- payload.  Presence bits distinguish an absent optional field from JSON null.
CREATE TABLE IF NOT EXISTS resolve_evidence_profile_states (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_results(ref_id) ON DELETE CASCADE,
  is_null INTEGER NOT NULL CHECK(is_null IN (0,1))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_profiles (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_results(ref_id) ON DELETE CASCADE,
  profile_kind TEXT NOT NULL CHECK(profile_kind IN ('normal','resolver_exception')),
  has_identifier INTEGER NOT NULL CHECK(has_identifier IN (0,1)),
  has_searchable_title INTEGER, has_searchable_title_present INTEGER NOT NULL CHECK(has_searchable_title_present IN (0,1)),
  has_author INTEGER, has_author_present INTEGER NOT NULL CHECK(has_author_present IN (0,1)),
  has_year INTEGER, has_year_present INTEGER NOT NULL CHECK(has_year_present IN (0,1)),
  has_venue INTEGER, has_venue_present INTEGER NOT NULL CHECK(has_venue_present IN (0,1)),
  source_kind TEXT, source_kind_present INTEGER NOT NULL CHECK(source_kind_present IN (0,1)),
  source_type_confidence TEXT, source_type_confidence_present INTEGER NOT NULL CHECK(source_type_confidence_present IN (0,1)),
  indexability TEXT, indexability_present INTEGER NOT NULL CHECK(indexability_present IN (0,1)),
  minimum_checks_completed INTEGER, minimum_checks_completed_present INTEGER NOT NULL CHECK(minimum_checks_completed_present IN (0,1)),
  resolution_basis TEXT NOT NULL,
  existence_confidence TEXT, existence_confidence_present INTEGER NOT NULL CHECK(existence_confidence_present IN (0,1)),
  title_overlap REAL, title_overlap_present INTEGER NOT NULL CHECK(title_overlap_present IN (0,1)),
  checks_count INTEGER NOT NULL CHECK(checks_count >= 0),
  source_type_evidence_count INTEGER NOT NULL CHECK(source_type_evidence_count >= 0),
  best_candidate_is_null INTEGER NOT NULL CHECK(best_candidate_is_null IN (0,1)),
  metadata_match_is_null INTEGER NOT NULL CHECK(metadata_match_is_null IN (0,1)),
  identifier_fallback_is_null INTEGER NOT NULL CHECK(identifier_fallback_is_null IN (0,1)),
  fulltext_availability_present INTEGER NOT NULL CHECK(fulltext_availability_present IN (0,1)),
  exception_present INTEGER NOT NULL CHECK(exception_present IN (0,1)),
  repair_exception_present INTEGER NOT NULL CHECK(repair_exception_present IN (0,1)),
  repair_failed_present INTEGER NOT NULL CHECK(repair_failed_present IN (0,1)),
  fetch_repair_present INTEGER NOT NULL CHECK(fetch_repair_present IN (0,1)),
  journal_authority_present INTEGER NOT NULL CHECK(journal_authority_present IN (0,1)),
  journal_alias_assessment_present INTEGER NOT NULL CHECK(journal_alias_assessment_present IN (0,1)),
  bibliographic_suspicion_present INTEGER NOT NULL CHECK(bibliographic_suspicion_present IN (0,1)),
  issue_attestations_present INTEGER NOT NULL CHECK(issue_attestations_present IN (0,1)),
  issue_attestations_count INTEGER NOT NULL CHECK(issue_attestations_count >= 0),
  bibliographic_adjudication_present INTEGER NOT NULL CHECK(bibliographic_adjudication_present IN (0,1)),
  CHECK((issue_attestations_present=1 AND issue_attestations_count>0)
        OR (issue_attestations_present=0 AND issue_attestations_count=0))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_journal_authorities (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status='recognized'),
  registry TEXT NOT NULL CHECK(length(trim(registry))>0),
  registry_version TEXT NOT NULL CHECK(length(trim(registry_version))>0),
  record_id TEXT NOT NULL CHECK(length(trim(record_id))>0),
  cited_venue TEXT NOT NULL CHECK(length(trim(cited_venue))>0),
  canonical_title TEXT NOT NULL CHECK(length(trim(canonical_title))>0),
  matched_alias TEXT NOT NULL CHECK(length(trim(matched_alias))>0),
  match_basis TEXT NOT NULL CHECK(match_basis IN ('canonical_title','registered_alias')),
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64 AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*')
);
CREATE TABLE IF NOT EXISTS resolve_evidence_journal_alias_assessments (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('exact','near_unconfirmed','ambiguous','unrecognized')),
  registry TEXT NOT NULL, registry_version TEXT NOT NULL, cited_venue TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_journal_alias_candidates (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_journal_alias_assessments(ref_id) ON DELETE CASCADE,
  candidate_order INTEGER NOT NULL CHECK(candidate_order>=0), record_id TEXT NOT NULL,
  canonical_title TEXT NOT NULL, matched_alias TEXT NOT NULL,
  distance INTEGER NOT NULL CHECK(distance IN (0,1)),
  PRIMARY KEY(ref_id,candidate_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_bibliographic_suspicions (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  suspicion_level TEXT NOT NULL CHECK(suspicion_level='elevated'), conclusion TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolve_evidence_bibliographic_suspicion_providers (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_bibliographic_suspicions(ref_id) ON DELETE CASCADE,
  provider_order INTEGER NOT NULL CHECK(provider_order>=0), provider TEXT NOT NULL,
  PRIMARY KEY(ref_id,provider_order), UNIQUE(ref_id,provider)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverages (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  authority_record_id TEXT NOT NULL, authority_snapshot_sha256 TEXT NOT NULL CHECK(length(authority_snapshot_sha256)=64), canonical_title TEXT NOT NULL,
  authority_hash TEXT NOT NULL CHECK(length(authority_hash)=64),
  suspicion_level TEXT NOT NULL CHECK(suspicion_level IN ('none','elevated','high')),
  conclusion TEXT NOT NULL, article_lookup_complete INTEGER NOT NULL CHECK(article_lookup_complete IN (0,1)),
  match_status TEXT NOT NULL CHECK(match_status IN ('no_compatible_article','incomplete'))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverage_issns (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_resolver_coverages(ref_id) ON DELETE CASCADE,
  issn_order INTEGER NOT NULL CHECK(issn_order>=0), issn TEXT NOT NULL,
  PRIMARY KEY(ref_id,issn_order), UNIQUE(ref_id,issn)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverage_observations (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_resolver_coverages(ref_id) ON DELETE CASCADE,
  observation_order INTEGER NOT NULL CHECK(observation_order>=0),
  resolver TEXT NOT NULL, rule_version TEXT NOT NULL, catalog_snapshot_id INTEGER,
  coverage_status TEXT NOT NULL CHECK(coverage_status IN ('covered','not_covered','incomplete')),
  fresh INTEGER NOT NULL CHECK(fresh IN (0,1)), checked_at TEXT, expires_at TEXT,
  query_contract TEXT NOT NULL, completion TEXT NOT NULL CHECK(completion IN ('complete','partial','incomplete')),
  source_url TEXT, http_status INTEGER, response_sha256 TEXT CHECK(response_sha256 IS NULL OR length(response_sha256)=64),
  provider_journal_id TEXT, work_count INTEGER, reason TEXT NOT NULL,
  refresh_mode TEXT NOT NULL CHECK(refresh_mode IN ('auto','manual')),
  PRIMARY KEY(ref_id,observation_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverage_article_lookups (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_resolver_coverages(ref_id) ON DELETE CASCADE,
  lookup_order INTEGER NOT NULL CHECK(lookup_order>=0), resolver TEXT NOT NULL,
  query_contract TEXT NOT NULL, scope TEXT NOT NULL,
  completion TEXT NOT NULL CHECK(completion IN ('complete','partial','incomplete')),
  match_status TEXT NOT NULL CHECK(match_status IN ('no_compatible_article','compatible','ambiguous','incomplete')),
  source_url TEXT, http_status INTEGER, response_sha256 TEXT, reason TEXT NOT NULL,
  PRIMARY KEY(ref_id,lookup_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverage_catalogs (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_resolver_coverages(ref_id) ON DELETE CASCADE,
  schema_version INTEGER NOT NULL CHECK(schema_version>0), created_at TEXT NOT NULL,
  catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_resolver_coverage_payloads (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_resolver_coverages(ref_id) ON DELETE CASCADE,
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64), media_type TEXT NOT NULL,
  payload_body BLOB NOT NULL, PRIMARY KEY(ref_id,payload_sha256)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_issue_attestations (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  attestation_order INTEGER NOT NULL CHECK(attestation_order >= 0),
  rule_version TEXT NOT NULL CHECK(length(trim(rule_version))>0),
  provider TEXT NOT NULL CHECK(length(trim(provider))>0),
  status TEXT NOT NULL CHECK(status IN ('complete','enumerated','incomplete','not_applicable')),
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  scope TEXT CHECK(scope='issue'),
  target_status TEXT NOT NULL CHECK(target_status IN ('present','absent','inconclusive')),
  target_member_order INTEGER CHECK(target_member_order >= 0),
  cited_container TEXT, cited_volume TEXT, cited_issue TEXT,
  journal_title TEXT,
  completeness_basis TEXT,
  members_count INTEGER NOT NULL CHECK(members_count >= 0),
  sources_count INTEGER NOT NULL CHECK(sources_count >= 0),
  observations_count INTEGER NOT NULL CHECK(observations_count >= 0),
  CHECK((status IN ('complete','enumerated') AND scope IS NOT NULL
         AND completeness_basis IS NOT NULL AND members_count>0 AND sources_count>0)
        OR (status IN ('incomplete','not_applicable') AND target_status='inconclusive'
            AND target_member_order IS NULL AND members_count=0)),
  CHECK(status!='enumerated' OR target_status!='absent'),
  CHECK(target_status!='present' OR target_member_order IS NOT NULL),
  CHECK(target_status!='inconclusive' OR target_member_order IS NULL),
  CHECK(target_member_order IS NULL OR status IN ('complete','enumerated')),
  PRIMARY KEY(ref_id,attestation_order), UNIQUE(ref_id,provider)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_issue_attestation_members (
  ref_id TEXT NOT NULL, attestation_order INTEGER NOT NULL,
  member_order INTEGER NOT NULL CHECK(member_order >= 0),
  record_id TEXT NOT NULL CHECK(length(trim(record_id))>0),
  pmcid TEXT, pmid TEXT, doi TEXT,
  title TEXT NOT NULL CHECK(length(trim(title))>0),
  first_author TEXT, year INTEGER,
  journal TEXT, volume TEXT, issue TEXT, locator TEXT, url TEXT,
  PRIMARY KEY(ref_id,attestation_order,member_order),
  UNIQUE(ref_id,attestation_order,record_id),
  FOREIGN KEY(ref_id,attestation_order) REFERENCES resolve_evidence_issue_attestations(ref_id,attestation_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_evidence_issue_attestation_sources (
  ref_id TEXT NOT NULL, attestation_order INTEGER NOT NULL,
  source_order INTEGER NOT NULL CHECK(source_order >= 0),
  role TEXT NOT NULL CHECK(length(trim(role))>0), url TEXT,
  response_sha256 TEXT CHECK(length(response_sha256)=64 AND response_sha256 NOT GLOB '*[^0-9a-f]*'),
  PRIMARY KEY(ref_id,attestation_order,source_order),
  FOREIGN KEY(ref_id,attestation_order) REFERENCES resolve_evidence_issue_attestations(ref_id,attestation_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_evidence_issue_attestation_observations (
  ref_id TEXT NOT NULL, attestation_order INTEGER NOT NULL,
  observation_order INTEGER NOT NULL CHECK(observation_order >= 0),
  observation_key TEXT NOT NULL CHECK(length(trim(observation_key))>0),
  value_type TEXT NOT NULL CHECK(value_type IN ('text','integer')),
  text_value TEXT, integer_value INTEGER,
  PRIMARY KEY(ref_id,attestation_order,observation_order), UNIQUE(ref_id,attestation_order,observation_key),
  CHECK((value_type='text' AND text_value IS NOT NULL AND integer_value IS NULL)
        OR (value_type='integer' AND text_value IS NULL AND integer_value IS NOT NULL)),
  FOREIGN KEY(ref_id,attestation_order) REFERENCES resolve_evidence_issue_attestations(ref_id,attestation_order) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_evidence_bibliographic_adjudications (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  rule_version TEXT NOT NULL CHECK(length(trim(rule_version))>0),
  outcome TEXT NOT NULL CHECK(outcome IN ('identified','identified_with_errors','refuted','not_corroborated','checks_incomplete')),
  identity_status TEXT NOT NULL CHECK(identity_status IN ('identified','identified_with_errors','not_identified','ambiguous')),
  check_status TEXT NOT NULL CHECK(check_status IN ('complete','incomplete')),
  correction_status TEXT NOT NULL CHECK(correction_status IN ('not_needed','identified','ambiguous','not_found','not_attempted')),
  refutations_count INTEGER NOT NULL CHECK(refutations_count >= 0)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_bibliographic_refutations (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_bibliographic_adjudications(ref_id) ON DELETE CASCADE,
  refutation_order INTEGER NOT NULL CHECK(refutation_order >= 0),
  kind TEXT NOT NULL CHECK(kind IN ('identifier_not_found','identifier_targets_other_work','coordinate_mismatch','coordinate_occupied_by_other_work','year_mismatch','author_mismatch','absent_from_complete_issue')),
  field TEXT NOT NULL CHECK(length(trim(field))>0),
  cited_value TEXT NOT NULL CHECK(length(trim(cited_value))>0),
  observed_value TEXT,
  source TEXT NOT NULL CHECK(length(trim(source))>0),
  basis TEXT NOT NULL CHECK(length(trim(basis))>0),
  PRIMARY KEY(ref_id,refutation_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_checks (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  check_order INTEGER NOT NULL CHECK(check_order >= 0), check_name TEXT NOT NULL,
  PRIMARY KEY(ref_id, check_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_source_type_evidence (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  evidence_order INTEGER NOT NULL CHECK(evidence_order >= 0), evidence TEXT NOT NULL,
  PRIMARY KEY(ref_id, evidence_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_risk_signals (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  signal_order INTEGER NOT NULL CHECK(signal_order >= 0), signal TEXT NOT NULL,
  PRIMARY KEY(ref_id, signal_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_best_candidate_authors (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_best_candidates(ref_id) ON DELETE CASCADE,
  author_order INTEGER NOT NULL CHECK(author_order >= 0), author TEXT NOT NULL,
  PRIMARY KEY(ref_id, author_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_metadata_hard_conflicts (
  ref_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('top','best','fallback')),
  conflict_order INTEGER NOT NULL CHECK(conflict_order >= 0), conflict TEXT NOT NULL,
  PRIMARY KEY(ref_id, role, conflict_order),
  FOREIGN KEY(ref_id, role) REFERENCES resolve_evidence_metadata_conflicts(ref_id, role) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_evidence_metadata_matches (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('top','best','fallback')),
  score REAL, score_present INTEGER NOT NULL CHECK(score_present IN (0,1)), title_overlap REAL, title_overlap_present INTEGER NOT NULL CHECK(title_overlap_present IN (0,1)),
  author_match INTEGER, author_match_present INTEGER NOT NULL CHECK(author_match_present IN (0,1)), cited_first_author TEXT, cited_first_author_present INTEGER NOT NULL CHECK(cited_first_author_present IN (0,1)), matched_first_author TEXT, matched_first_author_present INTEGER NOT NULL CHECK(matched_first_author_present IN (0,1)),
  year_match INTEGER, year_match_present INTEGER NOT NULL CHECK(year_match_present IN (0,1)), matched_year INTEGER, matched_year_present INTEGER NOT NULL CHECK(matched_year_present IN (0,1)), venue_overlap REAL, venue_overlap_present INTEGER NOT NULL CHECK(venue_overlap_present IN (0,1)), matched_venue TEXT, matched_venue_present INTEGER NOT NULL CHECK(matched_venue_present IN (0,1)),
  coordinate_comparisons_present INTEGER NOT NULL CHECK(coordinate_comparisons_present IN (0,1)),
  coordinate_comparisons_count INTEGER NOT NULL CHECK(coordinate_comparisons_count>=0),
  PRIMARY KEY(ref_id,role),
  CHECK((coordinate_comparisons_present=0 AND coordinate_comparisons_count=0) OR
        (coordinate_comparisons_present=1 AND coordinate_comparisons_count>0))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_metadata_coordinate_comparisons (
  ref_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('top','best','fallback')),
  comparison_order INTEGER NOT NULL CHECK(comparison_order >= 0),
  coordinate_kind TEXT NOT NULL CHECK(coordinate_kind IN ('container','volume','issue','article_page_range','chapter_page_range','elocator','article_number','article_locator')),
  cited_value TEXT NOT NULL CHECK(length(trim(cited_value))>0),
  matched_value TEXT,
  status TEXT NOT NULL CHECK(status IN ('match','mismatch','inconclusive')),
  PRIMARY KEY(ref_id,role,comparison_order),
  UNIQUE(ref_id,role,coordinate_kind),
  FOREIGN KEY(ref_id,role) REFERENCES resolve_evidence_metadata_matches(ref_id,role) ON DELETE CASCADE,
  CHECK((status='inconclusive' AND (matched_value IS NULL OR length(trim(matched_value))>0)) OR
        (status IN ('match','mismatch') AND matched_value IS NOT NULL AND length(trim(matched_value))>0))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_metadata_conflicts (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('top','best','fallback')),
  author_conflict INTEGER, author_conflict_present INTEGER NOT NULL CHECK(author_conflict_present IN (0,1)), metadata_conflict INTEGER, metadata_conflict_present INTEGER NOT NULL CHECK(metadata_conflict_present IN (0,1)), venue_conflict INTEGER, venue_conflict_present INTEGER NOT NULL CHECK(venue_conflict_present IN (0,1)), year_conflict INTEGER, year_conflict_present INTEGER NOT NULL CHECK(year_conflict_present IN (0,1)), ordinal_conflict INTEGER, ordinal_conflict_present INTEGER NOT NULL CHECK(ordinal_conflict_present IN (0,1)), year_mismatch_plausible INTEGER, year_mismatch_plausible_present INTEGER NOT NULL CHECK(year_mismatch_plausible_present IN (0,1)), hard_conflicts_present INTEGER NOT NULL CHECK(hard_conflicts_present IN (0,1)), hard_conflicts_count INTEGER NOT NULL CHECK(hard_conflicts_count >= 0), cited_ordinals_count INTEGER NOT NULL CHECK(cited_ordinals_count >= 0), matched_ordinals_count INTEGER NOT NULL CHECK(matched_ordinals_count >= 0), PRIMARY KEY(ref_id,role)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_metadata_ordinals (
  ref_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('top','best','fallback')), ordinal_kind TEXT NOT NULL CHECK(ordinal_kind IN ('cited','matched')), ordinal_order INTEGER NOT NULL CHECK(ordinal_order >= 0), ordinal_value INTEGER NOT NULL, PRIMARY KEY(ref_id,role,ordinal_kind,ordinal_order), FOREIGN KEY(ref_id,role) REFERENCES resolve_evidence_metadata_conflicts(ref_id,role) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS resolve_evidence_best_candidates (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  source TEXT, source_present INTEGER NOT NULL CHECK(source_present IN (0,1)), title TEXT, title_present INTEGER NOT NULL CHECK(title_present IN (0,1)), title_overlap REAL, title_overlap_present INTEGER NOT NULL CHECK(title_overlap_present IN (0,1)), status TEXT, status_present INTEGER NOT NULL CHECK(status_present IN (0,1)), reason TEXT, reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1)), metadata_match_present INTEGER NOT NULL CHECK(metadata_match_present IN (0,1)), metadata_match_is_null INTEGER NOT NULL CHECK(metadata_match_is_null IN (0,1)), authors_present INTEGER NOT NULL CHECK(authors_present IN (0,1)), authors_is_null INTEGER NOT NULL CHECK(authors_is_null IN (0,1)), authors_count INTEGER NOT NULL CHECK(authors_count >= 0), CHECK(authors_present = 1 OR (authors_is_null = 0 AND authors_count = 0)), CHECK(authors_is_null = 0 OR authors_count = 0)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_identifier_fallbacks (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  status TEXT, status_present INTEGER NOT NULL CHECK(status_present IN (0,1)), via TEXT, via_present INTEGER NOT NULL CHECK(via_present IN (0,1)), reason TEXT, reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1)), metadata_match_present INTEGER NOT NULL CHECK(metadata_match_present IN (0,1)),
  matched_title TEXT, matched_title_present INTEGER NOT NULL CHECK(matched_title_present IN (0,1)), abstract TEXT, abstract_present INTEGER NOT NULL CHECK(abstract_present IN (0,1)), retracted INTEGER, retracted_present INTEGER NOT NULL CHECK(retracted_present IN (0,1)), fulltext_exists TEXT, fulltext_exists_present INTEGER NOT NULL CHECK(fulltext_exists_present IN (0,1)), oa_status TEXT, oa_status_present INTEGER NOT NULL CHECK(oa_status_present IN (0,1)), work_type TEXT, work_type_present INTEGER NOT NULL CHECK(work_type_present IN (0,1)), resolution_basis TEXT, resolution_basis_present INTEGER NOT NULL CHECK(resolution_basis_present IN (0,1)), existence_confidence TEXT, existence_confidence_present INTEGER NOT NULL CHECK(existence_confidence_present IN (0,1)), oa_declared_status TEXT, oa_declared_status_present INTEGER NOT NULL CHECK(oa_declared_status_present IN (0,1)), book_availability TEXT, book_availability_present INTEGER NOT NULL CHECK(book_availability_present IN (0,1)), availability_note TEXT, availability_note_present INTEGER NOT NULL CHECK(availability_note_present IN (0,1)), existence_corroboration TEXT, existence_corroboration_present INTEGER NOT NULL CHECK(existence_corroboration_present IN (0,1)),
  matched_authors_kind TEXT NOT NULL CHECK(matched_authors_kind IN ('absent','null','list')), matched_authors_count INTEGER NOT NULL CHECK(matched_authors_count >= 0), oa_license_urls_kind TEXT NOT NULL CHECK(oa_license_urls_kind IN ('absent','null','list')), oa_license_urls_count INTEGER NOT NULL CHECK(oa_license_urls_count >= 0), fulltext_links_kind TEXT NOT NULL CHECK(fulltext_links_kind IN ('absent','null','list')), fulltext_links_count INTEGER NOT NULL CHECK(fulltext_links_count >= 0),
  CHECK(matched_title_present=1 OR matched_title IS NULL), CHECK(abstract_present=1 OR abstract IS NULL), CHECK((retracted_present=0 AND retracted IS NULL) OR (retracted_present=1 AND retracted IN (0,1))), CHECK(fulltext_exists_present=1 OR fulltext_exists IS NULL), CHECK(matched_authors_kind='list' OR matched_authors_count=0), CHECK(oa_license_urls_kind='list' OR oa_license_urls_count=0), CHECK(fulltext_links_kind='list' OR fulltext_links_count=0)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_identifier_fallback_authors (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_identifier_fallbacks(ref_id) ON DELETE CASCADE, author_order INTEGER NOT NULL CHECK(author_order >= 0), author TEXT NOT NULL, PRIMARY KEY(ref_id, author_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_identifier_fallback_licenses (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_identifier_fallbacks(ref_id) ON DELETE CASCADE, license_order INTEGER NOT NULL CHECK(license_order >= 0), license_url TEXT NOT NULL, PRIMARY KEY(ref_id, license_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_identifier_fallback_links (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_identifier_fallbacks(ref_id) ON DELETE CASCADE, link_order INTEGER NOT NULL CHECK(link_order >= 0), url TEXT NOT NULL, content_type TEXT, content_type_present INTEGER NOT NULL CHECK(content_type_present IN (0,1)), intended_application TEXT, intended_application_present INTEGER NOT NULL CHECK(intended_application_present IN (0,1)), CHECK(content_type_present=1 OR content_type IS NULL), CHECK(intended_application_present=1 OR intended_application IS NULL), PRIMARY KEY(ref_id, link_order)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_risks (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  score INTEGER, score_present INTEGER NOT NULL CHECK(score_present IN (0,1)), band TEXT, band_present INTEGER NOT NULL CHECK(band_present IN (0,1)), reason TEXT, reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1)), signals_count INTEGER NOT NULL CHECK(signals_count >= 0)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_fulltext_availability (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE,
  status TEXT, status_present INTEGER NOT NULL CHECK(status_present IN (0,1)), scope TEXT, scope_present INTEGER NOT NULL CHECK(scope_present IN (0,1)), observed_by TEXT, observed_by_present INTEGER NOT NULL CHECK(observed_by_present IN (0,1)), reason TEXT, reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_exceptions (
  ref_id TEXT NOT NULL REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('resolver','repair')),
  trigger TEXT, trigger_present INTEGER NOT NULL CHECK(trigger_present IN (0,1)), type TEXT, type_present INTEGER NOT NULL CHECK(type_present IN (0,1)), message TEXT, message_present INTEGER NOT NULL CHECK(message_present IN (0,1)), traceback TEXT, traceback_present INTEGER NOT NULL CHECK(traceback_present IN (0,1)), PRIMARY KEY(ref_id,role)
);
CREATE TABLE IF NOT EXISTS resolve_evidence_repair_failed (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE, trigger TEXT, trigger_present INTEGER NOT NULL CHECK(trigger_present IN (0,1)), fetch_status TEXT, fetch_status_present INTEGER NOT NULL CHECK(fetch_status_present IN (0,1)), fetch_method TEXT, fetch_method_present INTEGER NOT NULL CHECK(fetch_method_present IN (0,1)), fetch_reason TEXT, fetch_reason_present INTEGER NOT NULL CHECK(fetch_reason_present IN (0,1))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_fetch_repairs (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_profiles(ref_id) ON DELETE CASCADE, trigger TEXT, trigger_present INTEGER NOT NULL CHECK(trigger_present IN (0,1)), original_status TEXT, original_status_present INTEGER NOT NULL CHECK(original_status_present IN (0,1)), original_via TEXT, original_via_present INTEGER NOT NULL CHECK(original_via_present IN (0,1)), stored_via TEXT, stored_via_present INTEGER NOT NULL CHECK(stored_via_present IN (0,1)), stored_source_ref TEXT, stored_source_ref_present INTEGER NOT NULL CHECK(stored_source_ref_present IN (0,1)), content_version TEXT, content_version_present INTEGER NOT NULL CHECK(content_version_present IN (0,1)), corroborate_signal TEXT, corroborate_signal_present INTEGER NOT NULL CHECK(corroborate_signal_present IN (0,1)), corroborate_score REAL, corroborate_score_present INTEGER NOT NULL CHECK(corroborate_score_present IN (0,1)), abstract_disposition_present INTEGER NOT NULL CHECK(abstract_disposition_present IN (0,1))
);
CREATE TABLE IF NOT EXISTS resolve_evidence_abstract_dispositions (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_evidence_fetch_repairs(ref_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK(action IN ('absent','kept','suppressed')),
  reason TEXT,
  corroborate_signal TEXT,
  corroborate_score REAL,
  source_ref TEXT,
  origin TEXT
);

CREATE TABLE IF NOT EXISTS fetch_attempts (
  fetch_attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  method TEXT,
  url TEXT,
  kind TEXT,
  final_url TEXT,
  status_code INTEGER,
  content_type TEXT,
  outcome TEXT NOT NULL,
  reason TEXT,
  origin TEXT,
  challenge_blocked INTEGER NOT NULL DEFAULT 0,
  paywalled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fetch_attempts_ref_id
ON fetch_attempts(ref_id);

CREATE TABLE IF NOT EXISTS fetch_attempt_trace_states (
  fetch_attempt_id INTEGER PRIMARY KEY REFERENCES fetch_attempts(fetch_attempt_id) ON DELETE CASCADE,
  is_null INTEGER NOT NULL CHECK (is_null IN (0,1)),
  family TEXT CHECK (family IN ('execution','direct_text','provider_diagnostic','summary')),
  CHECK ((is_null = 1 AND family IS NULL) OR (is_null = 0 AND family IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS fetch_execution_traces (
  fetch_attempt_id INTEGER PRIMARY KEY REFERENCES fetch_attempt_trace_states(fetch_attempt_id) ON DELETE CASCADE,
  frozen_candidate_id INTEGER REFERENCES fetch_frozen_candidates(frozen_candidate_id) ON DELETE RESTRICT,
  frozen_candidate_id_present INTEGER NOT NULL CHECK (frozen_candidate_id_present IN (0,1)),
  queue_index INTEGER NOT NULL CHECK (queue_index >= 0),
  batch_index INTEGER NOT NULL CHECK (batch_index >= 0),
  method TEXT NOT NULL CHECK (length(method) > 0),
  url TEXT NOT NULL CHECK (length(url) > 0),
  kind TEXT NOT NULL CHECK (length(kind) > 0),
  outcome TEXT NOT NULL CHECK (length(outcome) > 0),
  stage TEXT, stage_present INTEGER NOT NULL CHECK (stage_present IN (0,1)),
  request TEXT, request_present INTEGER NOT NULL CHECK (request_present IN (0,1)),
  status INTEGER, status_present INTEGER NOT NULL CHECK (status_present IN (0,1)),
  final_url TEXT, final_url_present INTEGER NOT NULL CHECK (final_url_present IN (0,1)),
  content_type TEXT, content_type_present INTEGER NOT NULL CHECK (content_type_present IN (0,1)),
  headers_present INTEGER NOT NULL CHECK (headers_present IN (0,1)),
  headers_count INTEGER NOT NULL CHECK (headers_count >= 0),
  body_head TEXT, body_head_present INTEGER NOT NULL CHECK (body_head_present IN (0,1)),
  challenge_markers_present INTEGER NOT NULL CHECK (challenge_markers_present IN (0,1)),
  challenge_markers_count INTEGER NOT NULL CHECK (challenge_markers_count >= 0),
  cached_challenge INTEGER, cached_challenge_present INTEGER NOT NULL CHECK (cached_challenge_present IN (0,1)),
  reason TEXT, reason_present INTEGER NOT NULL CHECK (reason_present IN (0,1)),
  reason_code TEXT, reason_code_present INTEGER NOT NULL CHECK (reason_code_present IN (0,1)),
  chars INTEGER, chars_present INTEGER NOT NULL CHECK (chars_present IN (0,1)),
  extract_method TEXT, extract_method_present INTEGER NOT NULL CHECK (extract_method_present IN (0,1)),
  identity_extract_method TEXT, identity_extract_method_present INTEGER NOT NULL CHECK (identity_extract_method_present IN (0,1)),
  corroborate_signal TEXT, corroborate_signal_present INTEGER NOT NULL CHECK (corroborate_signal_present IN (0,1)),
  corroborate_score REAL, corroborate_score_present INTEGER NOT NULL CHECK (corroborate_score_present IN (0,1)),
  html_content_kind TEXT, html_content_kind_present INTEGER NOT NULL CHECK (html_content_kind_present IN (0,1)),
  has_pdf_link INTEGER, has_pdf_link_present INTEGER NOT NULL CHECK (has_pdf_link_present IN (0,1)),
  shell_reason TEXT, shell_reason_present INTEGER NOT NULL CHECK (shell_reason_present IN (0,1)),
  page_chars INTEGER, page_chars_present INTEGER NOT NULL CHECK (page_chars_present IN (0,1)),
  abstract_chars INTEGER, abstract_chars_present INTEGER NOT NULL CHECK (abstract_chars_present IN (0,1)),
  shell_markers_present INTEGER NOT NULL CHECK (shell_markers_present IN (0,1)),
  shell_markers_count INTEGER NOT NULL CHECK (shell_markers_count >= 0),
  parser_variants_present INTEGER NOT NULL CHECK (parser_variants_present IN (0,1)),
  parser_variants_count INTEGER NOT NULL CHECK (parser_variants_count >= 0),
  CHECK (request_present = 1 OR request IS NULL),
  CHECK (stage_present = 1 OR stage IS NULL),
  CHECK (stage IS NULL OR stage = 'candidate_generation'),
  CHECK ((frozen_candidate_id_present = 0 AND frozen_candidate_id IS NULL) OR
         (frozen_candidate_id_present = 1 AND frozen_candidate_id > 0)),
  CHECK (status_present = 1 OR status IS NULL),
  CHECK (status IS NULL OR status BETWEEN 100 AND 599),
  CHECK (final_url_present = 1 OR final_url IS NULL),
  CHECK (content_type_present = 1 OR content_type IS NULL),
  CHECK (body_head_present = 1 OR body_head IS NULL),
  CHECK ((cached_challenge_present = 0 AND cached_challenge IS NULL) OR
         (cached_challenge_present = 1 AND cached_challenge = 1)),
  CHECK (reason_present = 1 OR reason IS NULL),
  CHECK (reason_code_present = 1 OR reason_code IS NULL),
  CHECK ((chars_present = 0 AND chars IS NULL) OR (chars_present = 1 AND chars >= 0)),
  CHECK (extract_method_present = 1 OR extract_method IS NULL),
  CHECK (identity_extract_method_present = 1 OR identity_extract_method IS NULL),
  CHECK (corroborate_signal_present = 1 OR corroborate_signal IS NULL),
  CHECK (corroborate_score_present = 1 OR corroborate_score IS NULL),
  CHECK (html_content_kind_present = 1 OR html_content_kind IS NULL),
  CHECK ((has_pdf_link_present = 0 AND has_pdf_link IS NULL) OR
         (has_pdf_link_present = 1 AND has_pdf_link IN (0,1))),
  CHECK (shell_reason_present = 1 OR shell_reason IS NULL),
  CHECK ((page_chars_present = 0 AND page_chars IS NULL) OR (page_chars_present = 1 AND page_chars >= 0)),
  CHECK ((abstract_chars_present = 0 AND abstract_chars IS NULL) OR (abstract_chars_present = 1 AND abstract_chars >= 0)),
  CHECK (headers_present = 1 OR headers_count = 0),
  CHECK (challenge_markers_present = 1 OR challenge_markers_count = 0),
  CHECK (shell_markers_present = 1 OR shell_markers_count = 0),
  CHECK (parser_variants_present = 1 OR parser_variants_count = 0)
);

CREATE TABLE IF NOT EXISTS fetch_execution_headers (
  fetch_attempt_id INTEGER NOT NULL REFERENCES fetch_execution_traces(fetch_attempt_id) ON DELETE CASCADE,
  header_order INTEGER NOT NULL CHECK (header_order >= 0),
  name TEXT NOT NULL CHECK (length(name) > 0),
  value TEXT NOT NULL,
  PRIMARY KEY (fetch_attempt_id, header_order),
  UNIQUE (fetch_attempt_id, name)
);

CREATE TABLE IF NOT EXISTS fetch_execution_markers (
  fetch_attempt_id INTEGER NOT NULL REFERENCES fetch_execution_traces(fetch_attempt_id) ON DELETE CASCADE,
  marker_kind TEXT NOT NULL CHECK (marker_kind IN ('challenge','shell')),
  marker_order INTEGER NOT NULL CHECK (marker_order >= 0),
  marker TEXT NOT NULL CHECK (length(marker) > 0),
  PRIMARY KEY (fetch_attempt_id, marker_kind, marker_order),
  UNIQUE (fetch_attempt_id, marker_kind, marker)
);

CREATE TABLE IF NOT EXISTS fetch_execution_parser_variants (
  fetch_attempt_id INTEGER NOT NULL REFERENCES fetch_execution_traces(fetch_attempt_id) ON DELETE CASCADE,
  variant_order INTEGER NOT NULL CHECK (variant_order >= 0),
  variant_kind TEXT NOT NULL CHECK (variant_kind IN ('error','result')),
  method TEXT NOT NULL CHECK (length(method) > 0),
  quality_ok INTEGER CHECK (quality_ok IN (0,1)),
  chars INTEGER, chars_present INTEGER NOT NULL CHECK (chars_present IN (0,1)),
  alpha_ratio REAL, alpha_ratio_present INTEGER NOT NULL CHECK (alpha_ratio_present IN (0,1)),
  word_tokens INTEGER, word_tokens_present INTEGER NOT NULL CHECK (word_tokens_present IN (0,1)),
  error TEXT,
  identity_decision TEXT, identity_decision_present INTEGER NOT NULL CHECK (identity_decision_present IN (0,1)),
  identity_status TEXT, identity_status_present INTEGER NOT NULL CHECK (identity_status_present IN (0,1)),
  structure_flags_count INTEGER NOT NULL CHECK (structure_flags_count >= 0),
  PRIMARY KEY (fetch_attempt_id, variant_order),
  CHECK ((variant_kind = 'error' AND quality_ok IS NULL AND error IS NOT NULL AND
          chars_present = 0 AND alpha_ratio_present = 0 AND word_tokens_present = 0 AND
          identity_decision_present = 0 AND identity_status_present = 0 AND structure_flags_count = 0) OR
         (variant_kind = 'result' AND quality_ok IN (0,1))),
  CHECK ((chars_present = 0 AND chars IS NULL) OR (chars_present = 1 AND chars >= 0)),
  CHECK ((alpha_ratio_present = 0 AND alpha_ratio IS NULL) OR
         (alpha_ratio_present = 1 AND alpha_ratio BETWEEN 0.0 AND 1.0)),
  CHECK ((word_tokens_present = 0 AND word_tokens IS NULL) OR
         (word_tokens_present = 1 AND word_tokens >= 0)),
  CHECK (identity_decision_present = 1 OR identity_decision IS NULL),
  CHECK (identity_status_present = 1 OR identity_status IS NULL)
);

CREATE TABLE IF NOT EXISTS fetch_execution_parser_flags (
  fetch_attempt_id INTEGER NOT NULL,
  variant_order INTEGER NOT NULL,
  flag_order INTEGER NOT NULL CHECK (flag_order >= 0),
  flag TEXT NOT NULL CHECK (flag IN ('low_alpha_ratio','whitespace_bloat','fragmented_lines','repeated_line')),
  PRIMARY KEY (fetch_attempt_id, variant_order, flag_order),
  UNIQUE (fetch_attempt_id, variant_order, flag),
  FOREIGN KEY (fetch_attempt_id, variant_order)
    REFERENCES fetch_execution_parser_variants(fetch_attempt_id, variant_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fetch_direct_text_traces (
  fetch_attempt_id INTEGER PRIMARY KEY REFERENCES fetch_attempt_trace_states(fetch_attempt_id) ON DELETE CASCADE,
  subtype TEXT NOT NULL CHECK (subtype IN ('quality','stored','failed')),
  method TEXT NOT NULL CHECK (length(method) > 0),
  source_ref TEXT,
  chars INTEGER NOT NULL CHECK (chars >= 0),
  outcome TEXT NOT NULL CHECK (length(outcome) > 0),
  extract_method TEXT,
  identity_extract_method TEXT,
  identity_extract_method_present INTEGER NOT NULL CHECK (identity_extract_method_present IN (0,1)),
  reason TEXT,
  corroborate_signal TEXT,
  corroborate_score REAL,
  CHECK ((subtype = 'quality' AND outcome = 'quality_below_threshold' AND
          extract_method IS NULL AND identity_extract_method_present = 0 AND identity_extract_method IS NULL AND reason IS NULL AND corroborate_signal IS NULL AND corroborate_score IS NULL) OR
         (subtype = 'stored' AND outcome = 'stored' AND extract_method IS NOT NULL AND
          reason IS NULL AND corroborate_signal IS NULL AND corroborate_score IS NULL) OR
         (subtype = 'failed' AND outcome NOT IN ('stored','quality_below_threshold') AND extract_method IS NULL)),
  CHECK (identity_extract_method_present = 1 OR identity_extract_method IS NULL)
);

CREATE TABLE IF NOT EXISTS fetch_provider_diagnostic_traces (
  fetch_attempt_id INTEGER PRIMARY KEY REFERENCES fetch_attempt_trace_states(fetch_attempt_id) ON DELETE CASCADE,
  subtype TEXT NOT NULL CHECK (subtype IN ('direct','candidate')),
  provider TEXT,
  status TEXT NOT NULL CHECK (status IN ('partial','error')),
  error TEXT,
  reason TEXT,
  error_type TEXT CHECK (error_type IN (
    'resource_exhausted','auth','rate_limit','transient_server','http_error',
    'timeout','network','provider_error','invalid_provider_result'
  )),
  error_reason TEXT,
  retryable INTEGER CHECK (retryable IN (0,1)),
  http_status INTEGER CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
  item_count INTEGER,
  items_count INTEGER NOT NULL CHECK (items_count >= 0),
  CHECK ((subtype = 'direct' AND item_count IS NOT NULL AND item_count = items_count) OR
         (subtype = 'candidate' AND item_count IS NULL))
);

CREATE TABLE IF NOT EXISTS fetch_provider_direct_items (
  fetch_attempt_id INTEGER NOT NULL REFERENCES fetch_provider_diagnostic_traces(fetch_attempt_id) ON DELETE CASCADE,
  item_order INTEGER NOT NULL CHECK (item_order >= 0),
  method TEXT,
  source_ref TEXT,
  extract_method TEXT,
  candidate_key TEXT,
  discovery_reason TEXT,
  chars INTEGER NOT NULL CHECK (chars >= 0),
  PRIMARY KEY (fetch_attempt_id, item_order)
);

CREATE TABLE IF NOT EXISTS fetch_provider_candidate_items (
  fetch_attempt_id INTEGER NOT NULL REFERENCES fetch_provider_diagnostic_traces(fetch_attempt_id) ON DELETE CASCADE,
  item_order INTEGER NOT NULL CHECK (item_order >= 0),
  method TEXT,
  url TEXT,
  kind TEXT,
  candidate_key TEXT,
  discovery_reason TEXT,
  fallback_stage TEXT,
  PRIMARY KEY (fetch_attempt_id, item_order)
);

CREATE TABLE IF NOT EXISTS source_texts (
  source_text_id TEXT PRIMARY KEY,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  identity_key TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('fulltext','abstract','web')),
  origin TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  source_ref TEXT,
  mapping TEXT,
  match_signal TEXT,
  match_score REAL,
  identity_status TEXT,
  identity_note TEXT,
    content_version TEXT,
    provenance_relation TEXT,
    supplied_by TEXT,
    supplied_via TEXT,
    file_format TEXT,
    extraction_method TEXT,
  recorded_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_texts_path
ON source_texts(stored_path);

CREATE INDEX IF NOT EXISTS idx_source_texts_ref_id
ON source_texts(ref_id);

CREATE TABLE IF NOT EXISTS source_text_extraction_flag_states (
  source_text_id TEXT PRIMARY KEY REFERENCES source_texts(source_text_id) ON DELETE CASCADE,
  is_null INTEGER NOT NULL CHECK (is_null IN (0,1)),
  flag_count INTEGER NOT NULL CHECK (flag_count >= 0),
  CHECK (is_null = 0 OR flag_count = 0)
);

CREATE TABLE IF NOT EXISTS source_text_extraction_flags (
  source_text_id TEXT NOT NULL REFERENCES source_text_extraction_flag_states(source_text_id) ON DELETE CASCADE,
  flag TEXT NOT NULL CHECK (flag IN ('low_alpha_ratio','whitespace_bloat','fragmented_lines','repeated_line')),
  PRIMARY KEY (source_text_id, flag)
);

CREATE TABLE IF NOT EXISTS source_text_materialization_intents (
  intent_id TEXT PRIMARY KEY,
  source_text_id TEXT NOT NULL,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  identity_key TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('fulltext','abstract','web')),
  origin TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  char_count INTEGER NOT NULL CHECK(char_count >= 0),
  source_ref TEXT, mapping TEXT, match_signal TEXT, match_score REAL,
  identity_status TEXT, identity_note TEXT, content_version TEXT,
  provenance_relation TEXT, supplied_by TEXT, supplied_via TEXT,
  file_format TEXT, extraction_method TEXT, prepared_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_text_materialization_intent_flag_states (
  intent_id TEXT PRIMARY KEY REFERENCES source_text_materialization_intents(intent_id) ON DELETE CASCADE,
  is_null INTEGER NOT NULL CHECK(is_null IN (0,1)),
  flag_count INTEGER NOT NULL CHECK(flag_count >= 0),
  CHECK(is_null = 0 OR flag_count = 0)
);
CREATE TABLE IF NOT EXISTS source_text_materialization_intent_flags (
  intent_id TEXT NOT NULL REFERENCES source_text_materialization_intent_flag_states(intent_id) ON DELETE CASCADE,
  flag TEXT NOT NULL CHECK(flag IN ('low_alpha_ratio','whitespace_bloat','fragmented_lines','repeated_line')),
  PRIMARY KEY(intent_id, flag)
);
CREATE TABLE IF NOT EXISTS source_text_materialization_outcomes (
  intent_id TEXT PRIMARY KEY REFERENCES source_text_materialization_intents(intent_id) ON DELETE CASCADE,
  outcome TEXT NOT NULL CHECK(outcome IN ('registered','file_missing','file_hash_mismatch','file_not_regular','file_not_utf8','char_count_mismatch','file_invalid_path')),
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_text_materialization_invalidations (
  invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_text_id TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  origin TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK(reason IN ('file_missing','file_hash_mismatch','file_not_regular','file_not_utf8','char_count_mismatch','file_invalid_path')),
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unreadable_sources (
  unreadable_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_id TEXT REFERENCES operational_references(ref_id) ON DELETE SET NULL,
  ref_number INTEGER,
  kept_path TEXT,
  source_ref TEXT,
  origin TEXT NOT NULL,
  reason TEXT NOT NULL,
  ocr_status TEXT NOT NULL DEFAULT 'pending' CHECK (ocr_status IN ('pending','done')),
  ocr_method TEXT,
  recorded_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  slot TEXT NOT NULL CHECK (slot IN ('fetch','research','verify','parse_review')),
  ref_id TEXT REFERENCES operational_references(ref_id),
  note_id TEXT REFERENCES footnote_notes(note_id),
  claim_id TEXT REFERENCES claims(claim_id),
  scope TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','answered','applied','cancelled')),
  created_at TEXT NOT NULL,
  answered_at TEXT,
  applied_at TEXT,
  task_kind TEXT NOT NULL DEFAULT 'fetch' CHECK (task_kind IN (
    'fetch','browser_challenge','web_research','claim_evidence','manual_parse_review','source_identity_attestation'
  )),
  generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(generation) = 'integer' AND generation >= 0
  ),
  last_error_stage TEXT,
  last_error_type TEXT,
  last_error_message TEXT,
  last_error_traceback TEXT,
  CHECK (
    (last_error_stage IS NULL AND last_error_type IS NULL AND
     last_error_message IS NULL AND last_error_traceback IS NULL)
    OR
    (last_error_stage IS NOT NULL AND last_error_type IS NOT NULL AND
     last_error_message IS NOT NULL AND last_error_traceback IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_slot
ON tasks(status, slot);

CREATE TABLE IF NOT EXISTS task_answers (
  answer_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('llm','user','script','agent')),
  submitted_at TEXT NOT NULL,
  accepted_for_processing INTEGER NOT NULL DEFAULT 0 CHECK (
    accepted_for_processing IN (0,1)
  ),
  generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(generation) = 'integer' AND generation >= 0
  ),
  answer_kind TEXT NOT NULL DEFAULT 'fetch' CHECK (answer_kind IN (
    'fetch','browser_challenge','web_research','manual_parse_review','source_identity_attestation'
  ))
);

CREATE INDEX IF NOT EXISTS idx_task_answers_task_id
ON task_answers(task_id);

CREATE INDEX IF NOT EXISTS idx_task_answers_current
ON task_answers(task_id, generation, accepted_for_processing, submitted_at, answer_id);

CREATE TABLE IF NOT EXISTS task_manual_parse_review_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  review_kind TEXT NOT NULL CHECK (review_kind IN ('footnote_source_review','reference_identity_review','citation_reference_review','reference_claim_review')),
  target_sha256 TEXT NOT NULL CHECK (length(target_sha256)=64 AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  instructions TEXT NOT NULL CHECK(length(trim(instructions))>0)
);
CREATE TABLE IF NOT EXISTS task_source_identity_attestation_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  source_text_id TEXT NOT NULL REFERENCES source_texts(source_text_id) ON DELETE RESTRICT,
  source_text_sha256 TEXT NOT NULL CHECK(length(source_text_sha256)=64 AND source_text_sha256 NOT GLOB '*[^0-9a-f]*'),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64 AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  instructions TEXT NOT NULL CHECK(length(trim(instructions))>0)
);
CREATE TABLE IF NOT EXISTS task_source_identity_attestation_answers (
  answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK(action IN ('attest_identity','keep_unverified')),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64 AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  reason TEXT NOT NULL CHECK(length(trim(reason))>0)
);
CREATE TABLE IF NOT EXISTS source_identity_attestation_decisions (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE RESTRICT,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id) ON DELETE RESTRICT,
  source_text_id TEXT NOT NULL UNIQUE REFERENCES source_texts(source_text_id) ON DELETE RESTRICT,
  action TEXT NOT NULL CHECK(action IN ('attest_identity','keep_unverified')),
  target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64 AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  answer_id TEXT NOT NULL UNIQUE REFERENCES task_answers(answer_id) ON DELETE RESTRICT,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_manual_parse_review_candidates (
  task_id TEXT NOT NULL REFERENCES task_manual_parse_review_details(task_id) ON DELETE CASCADE,
  candidate_order INTEGER NOT NULL CHECK(candidate_order>=0),
  candidate_ref_id TEXT REFERENCES reference_entries(ref_id) ON DELETE RESTRICT,
  candidate_claim_id TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
  candidate_origin TEXT NOT NULL CHECK(candidate_origin IN ('parser','orphan_match','orphan_match_inverse')),
  candidate_score REAL NOT NULL CHECK(candidate_score>=0.0 AND candidate_score<=1.0),
  CHECK((candidate_ref_id IS NOT NULL AND candidate_claim_id IS NULL) OR (candidate_ref_id IS NULL AND candidate_claim_id IS NOT NULL)),
  PRIMARY KEY(task_id,candidate_order), UNIQUE(task_id,candidate_ref_id), UNIQUE(task_id,candidate_claim_id)
);
CREATE TABLE IF NOT EXISTS task_manual_parse_review_answers (
  answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('no_sources','split_sources','correct_identity','select_reference','select_claim','keep_ambiguous','keep_unresolved')),
  target_sha256 TEXT NOT NULL CHECK (length(target_sha256)=64 AND target_sha256 NOT GLOB '*[^0-9a-f]*'),
  reason TEXT NOT NULL CHECK(length(trim(reason))>0),
  title_present INTEGER NOT NULL CHECK(title_present IN(0,1)),
  title TEXT,
  doi_present INTEGER NOT NULL CHECK(doi_present IN(0,1)),
  doi TEXT,
  selected_ref_id TEXT REFERENCES reference_entries(ref_id) ON DELETE RESTRICT,
  selected_claim_id TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
  CHECK((selected_ref_id IS NOT NULL AND selected_claim_id IS NULL) OR (selected_ref_id IS NULL AND selected_claim_id IS NOT NULL) OR (selected_ref_id IS NULL AND selected_claim_id IS NULL)),
  CHECK((action='select_reference' AND selected_ref_id IS NOT NULL AND selected_claim_id IS NULL)
     OR (action='select_claim' AND selected_ref_id IS NULL AND selected_claim_id IS NOT NULL)
     OR (action NOT IN ('select_reference','select_claim') AND selected_ref_id IS NULL AND selected_claim_id IS NULL)),
  CHECK((title_present=0 AND title IS NULL) OR (title_present=1 AND length(trim(title))>0)),
  CHECK((doi_present=0 AND doi IS NULL) OR (doi_present=1 AND length(trim(doi))>0))
);
CREATE TABLE IF NOT EXISTS task_manual_parse_review_split_sources (
  answer_id TEXT NOT NULL REFERENCES task_manual_parse_review_answers(answer_id) ON DELETE CASCADE,
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  source_text TEXT NOT NULL CHECK(length(trim(source_text))>0),
  PRIMARY KEY(answer_id,source_order), UNIQUE(answer_id,source_text)
);
CREATE TABLE IF NOT EXISTS manual_reference_identity_overrides (
  ref_id TEXT PRIMARY KEY REFERENCES reference_entries(ref_id) ON DELETE CASCADE,
  original_raw_entry_sha256 TEXT NOT NULL CHECK(length(original_raw_entry_sha256)=64 AND original_raw_entry_sha256 NOT GLOB '*[^0-9a-f]*'),
  title_present INTEGER NOT NULL CHECK(title_present IN(0,1)), title TEXT,
  doi_present INTEGER NOT NULL CHECK(doi_present IN(0,1)), doi TEXT,
  answer_id TEXT NOT NULL UNIQUE REFERENCES task_answers(answer_id), applied_at TEXT NOT NULL,
  CHECK((title_present=0 AND title IS NULL) OR (title_present=1 AND length(trim(title))>0)),
  CHECK((doi_present=0 AND doi IS NULL) OR (doi_present=1 AND length(trim(doi))>0))
);

-- Manual footnote adjudication is an operational overlay.  It must never
-- rewrite the immutable Parse-time note, source, citation, or reference rows.
CREATE TABLE IF NOT EXISTS manual_footnote_source_overrides (
  note_id TEXT PRIMARY KEY REFERENCES footnote_notes(note_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK(action IN ('no_sources','split_sources','keep_ambiguous')),
  original_raw_note_sha256 TEXT NOT NULL CHECK(length(original_raw_note_sha256)=64 AND original_raw_note_sha256 NOT GLOB '*[^0-9a-f]*'),
  answer_id TEXT NOT NULL UNIQUE REFERENCES task_manual_parse_review_answers(answer_id) ON DELETE RESTRICT,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_footnote_source_override_sources (
  note_id TEXT NOT NULL REFERENCES manual_footnote_source_overrides(note_id) ON DELETE CASCADE,
  source_ref_id TEXT NOT NULL UNIQUE REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  source_text TEXT NOT NULL CHECK(length(trim(source_text))>0),
  raw_start INTEGER NOT NULL CHECK(raw_start>=0),
  raw_end INTEGER NOT NULL CHECK(raw_end>raw_start),
  PRIMARY KEY(note_id,source_order),
  UNIQUE(note_id,source_text)
);
CREATE TABLE IF NOT EXISTS manual_parse_review_applications (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  answer_id TEXT NOT NULL UNIQUE REFERENCES task_answers(answer_id),
  generation INTEGER NOT NULL CHECK(generation>=0), applied_at TEXT NOT NULL
);

-- Frozen Fetch candidates are a closed, typed replay plan.  They deliberately
-- contain no payload column: every fact used by a resume is independently
-- constrained and auditable.
CREATE TABLE IF NOT EXISTS fetch_candidate_plans (
  plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id) ON DELETE CASCADE,
  context_fingerprint TEXT NOT NULL CHECK(length(context_fingerprint)=64 AND context_fingerprint NOT GLOB '*[^0-9a-f]*'),
  plan_fingerprint TEXT NOT NULL CHECK(length(plan_fingerprint)=64 AND plan_fingerprint NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  UNIQUE(ref_id)
);
CREATE TABLE IF NOT EXISTS fetch_candidate_stage_freezes (
  stage_freeze_id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL REFERENCES fetch_candidate_plans(plan_id) ON DELETE RESTRICT,
  stage TEXT NOT NULL CHECK(stage IN('published','oa_alternate','preprint','perma','internet_archive_item','wayback')),
  generation INTEGER NOT NULL CHECK(generation>=0), candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
  stage_fingerprint TEXT NOT NULL CHECK(length(stage_fingerprint)=64 AND stage_fingerprint NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  UNIQUE(plan_id,stage,generation), UNIQUE(plan_id,stage,stage_fingerprint)
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidates (
  frozen_candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_freeze_id INTEGER NOT NULL REFERENCES fetch_candidate_stage_freezes(stage_freeze_id) ON DELETE RESTRICT,
  candidate_order INTEGER NOT NULL CHECK(candidate_order>=0),
  queue_index INTEGER NOT NULL CHECK(queue_index>=0), batch_index INTEGER NOT NULL CHECK(batch_index>=0),
  method TEXT NOT NULL CHECK(length(method)>0), url TEXT NOT NULL CHECK(length(url)>0), kind TEXT NOT NULL CHECK(length(kind)>0),
  content_version TEXT, content_version_present INTEGER NOT NULL CHECK(content_version_present IN(0,1)),
  fallback_stage TEXT, fallback_stage_present INTEGER NOT NULL CHECK(fallback_stage_present IN(0,1)),
  candidate_key TEXT, candidate_key_present INTEGER NOT NULL CHECK(candidate_key_present IN(0,1)),
  discovery_reason TEXT, discovery_reason_present INTEGER NOT NULL CHECK(discovery_reason_present IN(0,1)),
  discovered_via TEXT, discovered_via_present INTEGER NOT NULL CHECK(discovered_via_present IN(0,1)),
  referer TEXT, referer_present INTEGER NOT NULL CHECK(referer_present IN(0,1)),
  profile TEXT, profile_present INTEGER NOT NULL CHECK(profile_present IN(0,1)),
  fetch_profile TEXT, fetch_profile_present INTEGER NOT NULL CHECK(fetch_profile_present IN(0,1)),
  strategy TEXT, strategy_present INTEGER NOT NULL CHECK(strategy_present IN(0,1)),
  fetch_strategy TEXT, fetch_strategy_present INTEGER NOT NULL CHECK(fetch_strategy_present IN(0,1)),
  official_arxiv_html INTEGER, official_arxiv_html_present INTEGER NOT NULL CHECK(official_arxiv_html_present IN(0,1)),
  cited_landing_pdf INTEGER, cited_landing_pdf_present INTEGER NOT NULL CHECK(cited_landing_pdf_present IN(0,1)),
  cited_landing_url TEXT, cited_landing_url_present INTEGER NOT NULL CHECK(cited_landing_url_present IN(0,1)),
  identity_context_conflict INTEGER, identity_context_conflict_present INTEGER NOT NULL CHECK(identity_context_conflict_present IN(0,1)),
  url_aliases_present INTEGER NOT NULL CHECK(url_aliases_present IN(0,1)), candidate_keys_present INTEGER NOT NULL CHECK(candidate_keys_present IN(0,1)),
  provenance_present INTEGER NOT NULL CHECK(provenance_present IN(0,1)), discovery_reasons_present INTEGER NOT NULL CHECK(discovery_reasons_present IN(0,1)),
  primary_context_present INTEGER NOT NULL CHECK(primary_context_present IN(0,1)), alternate_contexts_present INTEGER NOT NULL CHECK(alternate_contexts_present IN(0,1)),
  candidate_fingerprint TEXT NOT NULL CHECK(length(candidate_fingerprint)=64 AND candidate_fingerprint NOT GLOB '*[^0-9a-f]*'),
  CHECK((identity_context_conflict_present=0 AND identity_context_conflict IS NULL) OR (identity_context_conflict_present=1 AND identity_context_conflict IN(0,1))),
  CHECK((official_arxiv_html_present=0 AND official_arxiv_html IS NULL) OR (official_arxiv_html_present=1 AND official_arxiv_html IN(0,1))),
  CHECK((cited_landing_pdf_present=0 AND cited_landing_pdf IS NULL AND cited_landing_url_present=0 AND cited_landing_url IS NULL) OR (cited_landing_pdf_present=1 AND cited_landing_pdf IS 1 AND cited_landing_url_present=1 AND typeof(cited_landing_url)='text' AND length(trim(cited_landing_url))>0)),
  UNIQUE(stage_freeze_id,candidate_order), UNIQUE(stage_freeze_id,queue_index), UNIQUE(stage_freeze_id,candidate_fingerprint)
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidate_strings (
  frozen_candidate_id INTEGER NOT NULL REFERENCES fetch_frozen_candidates(frozen_candidate_id) ON DELETE RESTRICT,
  family TEXT NOT NULL CHECK(family IN('url_aliases','candidate_keys','provenance','discovery_reasons')),
  item_order INTEGER NOT NULL CHECK(item_order>=0), value TEXT NOT NULL CHECK(length(value)>0),
  PRIMARY KEY(frozen_candidate_id,family,item_order)
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidate_contexts (
  frozen_candidate_id INTEGER NOT NULL REFERENCES fetch_frozen_candidates(frozen_candidate_id) ON DELETE RESTRICT,
  context_kind TEXT NOT NULL CHECK(context_kind IN('primary','alternate')), context_order INTEGER NOT NULL CHECK(context_order>=0),
  title TEXT, title_present INTEGER NOT NULL CHECK(title_present IN(0,1)),
  year_kind TEXT NOT NULL CHECK(year_kind IN('absent','null','integer','text')), year_integer INTEGER, year_text TEXT,
  provider TEXT, provider_present INTEGER NOT NULL CHECK(provider_present IN(0,1)), provider_record_id TEXT, provider_record_id_present INTEGER NOT NULL CHECK(provider_record_id_present IN(0,1)),
  first_author TEXT, first_author_present INTEGER NOT NULL CHECK(first_author_present IN(0,1)), source_confidence REAL, source_confidence_present INTEGER NOT NULL CHECK(source_confidence_present IN(0,1)),
  canonical_host INTEGER, canonical_host_present INTEGER NOT NULL CHECK(canonical_host_present IN(0,1)), canonical_url TEXT, canonical_url_present INTEGER NOT NULL CHECK(canonical_url_present IN(0,1)),
  landing_page_url TEXT, landing_page_url_present INTEGER NOT NULL CHECK(landing_page_url_present IN(0,1)), expected_document_title TEXT, expected_document_title_present INTEGER NOT NULL CHECK(expected_document_title_present IN(0,1)),
  official INTEGER, official_present INTEGER NOT NULL CHECK(official_present IN(0,1)), official_document_relation TEXT, official_document_relation_present INTEGER NOT NULL CHECK(official_document_relation_present IN(0,1)),
  authors_present INTEGER NOT NULL CHECK(authors_present IN(0,1)), identifiers_present INTEGER NOT NULL CHECK(identifiers_present IN(0,1)),
  PRIMARY KEY(frozen_candidate_id,context_kind,context_order),
  CHECK((year_kind IN('absent','null') AND year_integer IS NULL AND year_text IS NULL) OR (year_kind='integer' AND typeof(year_integer)='integer' AND year_text IS NULL) OR (year_kind='text' AND year_integer IS NULL AND typeof(year_text)='text')),
  CHECK(title_present=1 OR title IS NULL), CHECK(provider_present=1 OR provider IS NULL), CHECK(provider_record_id_present=1 OR provider_record_id IS NULL), CHECK(first_author_present=1 OR first_author IS NULL), CHECK(source_confidence_present=1 OR source_confidence IS NULL), CHECK((canonical_host_present=0 AND canonical_host IS NULL) OR (canonical_host_present=1 AND canonical_host IN(0,1))), CHECK(canonical_url_present=1 OR canonical_url IS NULL), CHECK(landing_page_url_present=1 OR landing_page_url IS NULL), CHECK(expected_document_title_present=1 OR expected_document_title IS NULL), CHECK((official_present=0 AND official IS NULL) OR (official_present=1 AND official IN(0,1))), CHECK(official_document_relation_present=1 OR official_document_relation IS NULL)
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidate_context_authors (
  frozen_candidate_id INTEGER NOT NULL, context_kind TEXT NOT NULL, context_order INTEGER NOT NULL, author_order INTEGER NOT NULL CHECK(author_order>=0), author TEXT NOT NULL CHECK(length(author)>0),
  PRIMARY KEY(frozen_candidate_id,context_kind,context_order,author_order), FOREIGN KEY(frozen_candidate_id,context_kind,context_order) REFERENCES fetch_frozen_candidate_contexts(frozen_candidate_id,context_kind,context_order) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidate_context_identifiers (
  frozen_candidate_id INTEGER NOT NULL, context_kind TEXT NOT NULL, context_order INTEGER NOT NULL, identifier_order INTEGER NOT NULL CHECK(identifier_order>=0), identifier_type TEXT NOT NULL CHECK(length(identifier_type)>0), identifier_value TEXT NOT NULL CHECK(length(identifier_value)>0),
  PRIMARY KEY(frozen_candidate_id,context_kind,context_order,identifier_order), UNIQUE(frozen_candidate_id,context_kind,context_order,identifier_type), FOREIGN KEY(frozen_candidate_id,context_kind,context_order) REFERENCES fetch_frozen_candidate_contexts(frozen_candidate_id,context_kind,context_order) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS fetch_frozen_candidate_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, frozen_candidate_id INTEGER NOT NULL REFERENCES fetch_frozen_candidates(frozen_candidate_id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL CHECK(event_type IN('admitted','deadline_skipped','retry_claimed','retry_deferred','retry_completed','retry_invalidated')),
  fetch_attempt_id INTEGER REFERENCES fetch_attempts(fetch_attempt_id) ON DELETE RESTRICT,
  reason_code TEXT, reason_detail TEXT, created_at TEXT NOT NULL,
CHECK((event_type IN('admitted','retry_claimed') AND fetch_attempt_id IS NULL AND reason_code IS NULL AND reason_detail IS NULL) OR (event_type='deadline_skipped' AND fetch_attempt_id IS NOT NULL AND reason_code='deadline_exceeded' AND reason_detail IS NOT NULL) OR (event_type='retry_deferred' AND fetch_attempt_id IS NOT NULL AND reason_code IN('host_cooldown','rate_limit_response') AND reason_detail IS NOT NULL) OR (event_type='retry_completed' AND fetch_attempt_id IS NOT NULL AND reason_code IS NULL AND reason_detail IS NULL) OR (event_type='retry_invalidated' AND reason_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_fetch_frozen_candidate_eligibility ON fetch_frozen_candidate_events(frozen_candidate_id,event_type);

CREATE TRIGGER IF NOT EXISTS fetch_candidate_plans_no_update BEFORE UPDATE ON fetch_candidate_plans BEGIN SELECT RAISE(ABORT,'fetch_candidate_plans immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_candidate_plans_no_delete BEFORE DELETE ON fetch_candidate_plans BEGIN SELECT RAISE(ABORT,'fetch_candidate_plans immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_candidate_stage_freezes_no_update BEFORE UPDATE ON fetch_candidate_stage_freezes BEGIN SELECT RAISE(ABORT,'fetch_candidate_stage_freezes immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_candidate_stage_freezes_no_delete BEFORE DELETE ON fetch_candidate_stage_freezes BEGIN SELECT RAISE(ABORT,'fetch_candidate_stage_freezes immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidates_no_update BEFORE UPDATE ON fetch_frozen_candidates BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidates immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidates_no_delete BEFORE DELETE ON fetch_frozen_candidates BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidates immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_strings_no_update BEFORE UPDATE ON fetch_frozen_candidate_strings BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_strings immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_strings_no_delete BEFORE DELETE ON fetch_frozen_candidate_strings BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_strings immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_contexts_no_update BEFORE UPDATE ON fetch_frozen_candidate_contexts BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_contexts immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_contexts_no_delete BEFORE DELETE ON fetch_frozen_candidate_contexts BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_contexts immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_context_authors_no_update BEFORE UPDATE ON fetch_frozen_candidate_context_authors BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_context_authors immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_context_authors_no_delete BEFORE DELETE ON fetch_frozen_candidate_context_authors BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_context_authors immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_context_identifiers_no_update BEFORE UPDATE ON fetch_frozen_candidate_context_identifiers BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_context_identifiers immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_context_identifiers_no_delete BEFORE DELETE ON fetch_frozen_candidate_context_identifiers BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_context_identifiers immutable'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_events_no_update BEFORE UPDATE ON fetch_frozen_candidate_events BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_events append-only'); END;
CREATE TRIGGER IF NOT EXISTS fetch_frozen_candidate_events_no_delete BEFORE DELETE ON fetch_frozen_candidate_events BEGIN SELECT RAISE(ABORT,'fetch_frozen_candidate_events append-only'); END;

CREATE TABLE IF NOT EXISTS task_fetch_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  instructions TEXT NOT NULL CHECK (length(trim(instructions)) > 0)
);

CREATE TABLE IF NOT EXISTS task_browser_challenge_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  domain TEXT NOT NULL CHECK (length(trim(domain)) > 0),
  instructions TEXT NOT NULL CHECK (length(trim(instructions)) > 0),
  reference_count INTEGER NOT NULL CHECK (reference_count > 0),
  candidate_url_count INTEGER NOT NULL CHECK (candidate_url_count >= 0)
);

CREATE TABLE IF NOT EXISTS task_browser_challenge_references (
  task_id TEXT NOT NULL REFERENCES task_browser_challenge_details(task_id) ON DELETE CASCADE,
  reference_order INTEGER NOT NULL CHECK (reference_order >= 0),
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id),
  candidate_url_count INTEGER NOT NULL CHECK (candidate_url_count >= 0),
  PRIMARY KEY (task_id, reference_order),
  UNIQUE (task_id, ref_id)
);

CREATE TABLE IF NOT EXISTS task_browser_challenge_reference_urls (
  task_id TEXT NOT NULL,
  reference_order INTEGER NOT NULL,
  url_order INTEGER NOT NULL CHECK (url_order >= 0),
  url TEXT NOT NULL CHECK (length(trim(url)) > 0),
  PRIMARY KEY (task_id, reference_order, url_order),
  UNIQUE (task_id, reference_order, url),
  FOREIGN KEY (task_id, reference_order)
    REFERENCES task_browser_challenge_references(task_id, reference_order)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_browser_challenge_candidate_urls (
  task_id TEXT NOT NULL REFERENCES task_browser_challenge_details(task_id) ON DELETE CASCADE,
  url_order INTEGER NOT NULL CHECK (url_order >= 0),
  url TEXT NOT NULL CHECK (length(trim(url)) > 0),
  PRIMARY KEY (task_id, url_order),
  UNIQUE (task_id, url)
);

CREATE TABLE IF NOT EXISTS task_web_research_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempts INTEGER NOT NULL CHECK (typeof(attempts) = 'integer' AND attempts >= 0),
  instructions TEXT NOT NULL CHECK (length(trim(instructions)) > 0),
  claim_sentence_count INTEGER NOT NULL CHECK (claim_sentence_count > 0)
);

CREATE TABLE IF NOT EXISTS task_web_research_claim_sentences (
  task_id TEXT NOT NULL REFERENCES task_web_research_details(task_id) ON DELETE CASCADE,
  sentence_order INTEGER NOT NULL CHECK (sentence_order >= 0),
  sentence TEXT NOT NULL CHECK (length(trim(sentence)) > 0),
  PRIMARY KEY (task_id, sentence_order)
);

CREATE TABLE IF NOT EXISTS task_claim_evidence_details (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  semantic_contract TEXT NOT NULL CHECK (semantic_contract = 'verify-claim-evidence-v10'),
  source_text_id TEXT NOT NULL REFERENCES source_texts(source_text_id),
  source_text_sha256 TEXT NOT NULL CHECK (
    length(source_text_sha256) = 64 AND source_text_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  context_mode TEXT NOT NULL CHECK (context_mode IN ('full_text','extractive_rag')),
  context_budget INTEGER NOT NULL CHECK (
    typeof(context_budget) = 'integer' AND context_budget > 0
  ),
  source_hash TEXT NOT NULL CHECK (
    length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'
  ),
  context_hash TEXT NOT NULL CHECK (
    length(context_hash) = 64 AND context_hash NOT GLOB '*[^0-9a-f]*'
  ),
  retrieval_algorithm TEXT,
  retrieval_config TEXT NOT NULL,
  range_count INTEGER NOT NULL CHECK (range_count >= 0),
  CHECK (
    (context_mode = 'full_text' AND retrieval_algorithm IS NULL AND range_count = 0)
    OR
    (context_mode = 'extractive_rag' AND
     length(trim(retrieval_algorithm)) > 0 AND range_count > 0)
  )
);

CREATE TABLE IF NOT EXISTS task_claim_evidence_ranges (
  task_id TEXT NOT NULL REFERENCES task_claim_evidence_details(task_id) ON DELETE CASCADE,
  range_order INTEGER NOT NULL CHECK (range_order >= 0),
  span_id TEXT NOT NULL CHECK (length(trim(span_id)) > 0),
  raw_start INTEGER NOT NULL CHECK (raw_start >= 0),
  raw_end INTEGER NOT NULL CHECK (raw_end > raw_start),
  PRIMARY KEY (task_id, range_order),
  UNIQUE (task_id, span_id)
);

CREATE TABLE IF NOT EXISTS task_fetch_answers (
  answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
  found INTEGER NOT NULL CHECK (found IN (0,1)),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('none','file_path','text')),
  source_value TEXT,
  source_tier TEXT NOT NULL CHECK (source_tier IN ('fulltext','abstract')),
  disposition TEXT NOT NULL CHECK (disposition IN ('provided','not_found','user_waived')),
  guided_fetch INTEGER NOT NULL DEFAULT 0 CHECK (guided_fetch IN (0,1)),
  identity_attested INTEGER NOT NULL DEFAULT 0 CHECK (identity_attested IN (0,1)),
  url_present INTEGER NOT NULL CHECK (url_present IN (0,1)),
  url TEXT,
  CHECK (
    (found = 0 AND source_kind = 'none' AND source_value IS NULL
      AND source_tier = 'fulltext' AND disposition IN ('not_found','user_waived'))
    OR
    (found = 1 AND source_kind IN ('file_path','text') AND length(trim(source_value)) > 0
      AND disposition = 'provided')
  ),
  CHECK (
    (url_present = 0 AND url IS NULL)
    OR (url_present = 1 AND length(trim(url)) > 0)
  ),
  CHECK (source_tier != 'abstract' OR source_kind = 'file_path'),
  CHECK (disposition != 'user_waived' OR guided_fetch = 1),
  CHECK (guided_fetch = 0 OR found = 1 OR disposition = 'user_waived'),
  CHECK (identity_attested = CASE WHEN guided_fetch = 1 AND found = 1 THEN 1 ELSE 0 END)
);

CREATE TABLE IF NOT EXISTS task_browser_answer_states (
  answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
  item_count INTEGER NOT NULL CHECK (item_count > 0)
);

CREATE TABLE IF NOT EXISTS task_browser_answer_items (
  answer_id TEXT NOT NULL REFERENCES task_browser_answer_states(answer_id) ON DELETE CASCADE,
  item_order INTEGER NOT NULL CHECK (item_order >= 0),
  ref_id TEXT NOT NULL REFERENCES operational_references(ref_id),
  found INTEGER NOT NULL CHECK (found IN (0,1)),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('none','file_path','text')),
  source_value TEXT,
  source_tier TEXT NOT NULL CHECK (source_tier IN ('fulltext','abstract')),
  disposition TEXT NOT NULL CHECK (disposition IN ('provided','not_found','user_waived')),
  guided_fetch INTEGER NOT NULL DEFAULT 0 CHECK (guided_fetch IN (0,1)),
  identity_attested INTEGER NOT NULL DEFAULT 0 CHECK (identity_attested IN (0,1)),
  url_present INTEGER NOT NULL CHECK (url_present IN (0,1)),
  url TEXT,
  PRIMARY KEY (answer_id, item_order),
  UNIQUE (answer_id, ref_id),
  CHECK (
    (found = 0 AND source_kind = 'none' AND source_value IS NULL
      AND source_tier = 'fulltext' AND disposition IN ('not_found','user_waived'))
    OR
    (found = 1 AND source_kind IN ('file_path','text') AND length(trim(source_value)) > 0
      AND disposition = 'provided')
  ),
  CHECK (
    (url_present = 0 AND url IS NULL)
    OR (url_present = 1 AND length(trim(url)) > 0)
  ),
  CHECK (source_tier != 'abstract' OR source_kind = 'file_path'),
  CHECK (disposition != 'user_waived' OR guided_fetch = 1),
  CHECK (guided_fetch = 0 OR found = 1 OR disposition = 'user_waived'),
  CHECK (identity_attested = CASE WHEN guided_fetch = 1 AND found = 1 THEN 1 ELSE 0 END)
);

CREATE TABLE IF NOT EXISTS task_research_answer_states (
  answer_id TEXT PRIMARY KEY REFERENCES task_answers(answer_id) ON DELETE CASCADE,
  found INTEGER NOT NULL CHECK (found IN (0,1)),
  finding_count INTEGER NOT NULL CHECK (finding_count >= 0),
  CHECK (found = (finding_count > 0))
);

CREATE TABLE IF NOT EXISTS task_research_answer_findings (
  answer_id TEXT NOT NULL REFERENCES task_research_answer_states(answer_id) ON DELETE CASCADE,
  finding_order INTEGER NOT NULL CHECK (finding_order >= 0),
  url TEXT NOT NULL CHECK (length(trim(url)) > 0),
  stance TEXT NOT NULL CHECK (stance IN ('supports','contradicts')),
  quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
  PRIMARY KEY (answer_id, finding_order)
);

-- Mutable, authoritative projection of one verification pair.  The event
-- ledger remains append-only; this row is the compare-and-set guard that
-- prevents two concurrent workers from publishing incompatible terminals.
CREATE TABLE IF NOT EXISTS verification_pair_state (
  -- Kept independent from the materialized claim/reference projections so
  -- early failure paths remain auditable before those rows are persisted.
  claim_id TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
    'open','accepted','uncertain','exhausted','deadline_exceeded','cancelled','infrastructure_error'
  )),
  terminal_outcome TEXT,
  terminal_cause TEXT,
  winner_call_id TEXT,
  opened_at TEXT NOT NULL,
  terminal_at TEXT,
  active_elapsed_ms REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (claim_id, ref_id, scope),
  CHECK ((status = 'open' AND terminal_at IS NULL) OR (status <> 'open' AND terminal_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS verification_pair_transitions (
  transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  call_id TEXT,
  cause TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verification_pair_state_status
ON verification_pair_state(status);

-- Forward-only structured audit for the verifier.
CREATE TABLE IF NOT EXISTS llm_logical_requests (
  logical_request_id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  ref_id TEXT,
  scope TEXT NOT NULL,
  candidate_id TEXT REFERENCES verification_candidates(candidate_id),
  candidate_cycle INTEGER NOT NULL CHECK (
    typeof(candidate_cycle) = 'integer' AND candidate_cycle >= 1
  ),
  stage TEXT NOT NULL CHECK (stage IN (
    'support_gate','full_support_gate','contrary_gate','topic_gate',
    'explanation_evidence','jury2'
  )),
  payload_hash TEXT NOT NULL,
  source_hash TEXT,
  context_hash TEXT,
  retrieval_hash TEXT,
  prompt_hash TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (
    (stage IN (
      'support_gate','full_support_gate','contrary_gate','topic_gate',
      'explanation_evidence'
    ) AND candidate_id IS NULL)
    OR (stage = 'jury2' AND candidate_id IS NOT NULL)
  ),
  CHECK (ref_id IS NOT NULL AND length(trim(ref_id)) > 0
    AND source_hash IS NOT NULL AND context_hash IS NOT NULL
    AND retrieval_hash IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS verification_ledger_metadata (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  fingerprint_version TEXT NOT NULL CHECK (
    fingerprint_version = 'claim-evidence-fingerprint-v2'
  )
);

CREATE TABLE IF NOT EXISTS llm_jury1_request_payloads (
  logical_request_id TEXT PRIMARY KEY REFERENCES llm_logical_requests(logical_request_id),
  claim_text TEXT NOT NULL CHECK(length(trim(claim_text)) > 0),
  claim_context TEXT NOT NULL CHECK(length(trim(claim_context)) > 0),
  citation_marker TEXT NOT NULL CHECK(length(trim(citation_marker)) > 0),
  source_hash TEXT NOT NULL CHECK(length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'),
  cited_source_mode TEXT NOT NULL CHECK(cited_source_mode IN ('full_text','extractive_rag')),
  determined_outcome TEXT CHECK(
    determined_outcome IS NULL OR determined_outcome IN (
      'supports','partial','contradicts','related','off_topic'
    )
  ),
  task_id TEXT NOT NULL CHECK(task_id IN (
    'support_gate','full_support_gate','contrary_gate','topic_gate',
    'explanation_evidence'
  )),
  task_instructions TEXT NOT NULL CHECK(length(trim(task_instructions)) > 0),
  source_span_count INTEGER NOT NULL CHECK(typeof(source_span_count) = 'integer' AND source_span_count > 0),
  CHECK (
    (task_id = 'explanation_evidence' AND determined_outcome IS NOT NULL)
    OR
    (task_id <> 'explanation_evidence' AND determined_outcome IS NULL)
  )
);
CREATE TABLE IF NOT EXISTS llm_jury1_request_source_spans (
  logical_request_id TEXT NOT NULL REFERENCES llm_jury1_request_payloads(logical_request_id),
  span_order INTEGER NOT NULL CHECK(typeof(span_order) = 'integer' AND span_order >= 0),
  span_id TEXT NOT NULL CHECK(length(trim(span_id)) > 0),
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  PRIMARY KEY(logical_request_id, span_order),
  UNIQUE(logical_request_id, span_id)
);

CREATE TABLE IF NOT EXISTS llm_jury2_request_payloads (
  logical_request_id TEXT PRIMARY KEY REFERENCES llm_logical_requests(logical_request_id),
  asserted_relation TEXT NOT NULL CHECK(asserted_relation IN ('basis','contrary')),
  passage_subject TEXT NOT NULL CHECK(length(trim(passage_subject)) > 0),
  passage_count INTEGER NOT NULL CHECK(typeof(passage_count) = 'integer' AND passage_count > 0)
);

CREATE TABLE IF NOT EXISTS llm_jury2_request_passages (
  logical_request_id TEXT NOT NULL REFERENCES llm_jury2_request_payloads(logical_request_id),
  passage_order INTEGER NOT NULL CHECK(typeof(passage_order) = 'integer' AND passage_order >= 0),
  span_id TEXT NOT NULL CHECK(length(trim(span_id)) > 0),
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  PRIMARY KEY(logical_request_id, passage_order),
  UNIQUE(logical_request_id, span_id),
  UNIQUE(logical_request_id, text)
);

CREATE TABLE IF NOT EXISTS verification_candidates (
  candidate_id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  candidate_cycle INTEGER NOT NULL CHECK (
    typeof(candidate_cycle) = 'integer' AND candidate_cycle >= 1
  ),
  origin_logical_request_id TEXT NOT NULL
    REFERENCES llm_logical_requests(logical_request_id),
  fingerprint TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('supports','partial','contradicts','related','off_topic','non_decidable')),
    explanation TEXT,
    provider_confidence REAL CHECK(
        provider_confidence IS NULL OR (
            typeof(provider_confidence)='real'
            AND provider_confidence >= 0.0 AND provider_confidence <= 1.0
        )
    ),
  claim_hash TEXT NOT NULL,
  supported_part TEXT,
  incompatible_proposition TEXT,
  non_decidable_reason TEXT,
  source_hash TEXT NOT NULL,
  context_hash TEXT NOT NULL,
  retrieval_hash TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
    UNIQUE(claim_id, ref_id, scope, candidate_cycle),
    CHECK(
        (provider_confidence IS NULL AND explanation IS NOT NULL
            AND length(trim(explanation)) > 0)
        OR
        (provider_confidence IS NOT NULL AND explanation IS NULL
            AND supported_part IS NULL AND incompatible_proposition IS NULL
            AND (
                (outcome='non_decidable' AND non_decidable_reason IN (
                    'material_limit','no_consensus','verification_unavailable','retrieval_limit','provider_uncertain'
                ))
                OR (outcome!='non_decidable' AND non_decidable_reason IS NULL)
            ))
    )
);

CREATE TABLE IF NOT EXISTS verification_candidate_evidence (
  candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
  evidence_order INTEGER NOT NULL CHECK (typeof(evidence_order) = 'integer' AND evidence_order >= 0),
  quotation TEXT NOT NULL,
  PRIMARY KEY (candidate_id, evidence_order),
  UNIQUE (candidate_id, quotation)
);

CREATE TABLE IF NOT EXISTS verification_candidate_grounding (
  candidate_id TEXT NOT NULL,
  evidence_order INTEGER NOT NULL,
  text TEXT NOT NULL,
  raw_start INTEGER NOT NULL CHECK (typeof(raw_start) = 'integer' AND raw_start >= 0),
  raw_end INTEGER NOT NULL CHECK (typeof(raw_end) = 'integer' AND raw_end > raw_start),
  span_id TEXT NOT NULL,
  match_mode TEXT NOT NULL CHECK (match_mode IN ('exact_raw','normalized','fuzzy')),
  score REAL NOT NULL CHECK (
    (match_mode IN ('exact_raw','normalized') AND score = 1.0)
    OR (match_mode = 'fuzzy' AND score >= 0.92 AND score <= 1.0)
  ),
  PRIMARY KEY (candidate_id, evidence_order),
  FOREIGN KEY (candidate_id, evidence_order)
    REFERENCES verification_candidate_evidence(candidate_id, evidence_order)
);

CREATE TABLE IF NOT EXISTS verification_candidate_events (
  event_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
  event_type TEXT NOT NULL CHECK (event_type IN (
    'guard_accepted','guard_rejected',
    'jury2_yes','jury2_no','jury2_technical_exhausted',
    'requeued','terminal'
  )),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_candidate_guard_rejections (
  event_id TEXT PRIMARY KEY REFERENCES verification_candidate_events(event_id),
  cause TEXT NOT NULL CHECK (cause IN (
    'schema_invalid','evidence_cardinality_invalid','grounding_invalid','provenance_invalid','stage_disagreement',
    'grounding/empty_quotation','grounding/ambiguous_exact_raw_match','grounding/ambiguous_normalized_match','grounding/ungrounded_omission_marker','grounding/no_guarded_fuzzy_match','grounding/ambiguous_guarded_fuzzy_match','grounding/malformed_locator_result','grounding/invalid_locator_range','grounding/exact_locator_mismatch','grounding/normalized_locator_mismatch','grounding/fuzzy_locator_policy_violation','grounding/decision_or_context_invalid','grounding/duplicate_evidence_range','grounding/duplicate_evidence_text','grounding/evidence_outside_context'
  ))
);
CREATE TABLE IF NOT EXISTS verification_candidate_jury2_decisions (
  event_id TEXT PRIMARY KEY REFERENCES verification_candidate_events(event_id),
  logical_request_id TEXT NOT NULL REFERENCES llm_logical_requests(logical_request_id),
  jury2_payload_hash TEXT NOT NULL,
    reason TEXT,
    provider_confidence REAL CHECK(
        provider_confidence IS NULL OR (
            typeof(provider_confidence)='real'
            AND provider_confidence >= 0.0 AND provider_confidence <= 1.0
        )
    ),
    CHECK(
        (reason IS NOT NULL AND length(trim(reason)) > 0
            AND provider_confidence IS NULL)
        OR (reason IS NULL AND provider_confidence IS NOT NULL)
    )
);
CREATE TABLE IF NOT EXISTS verification_candidate_jury2_technical_failures (
  event_id TEXT PRIMARY KEY REFERENCES verification_candidate_events(event_id),
  logical_request_id TEXT NOT NULL REFERENCES llm_logical_requests(logical_request_id),
  jury2_payload_hash TEXT NOT NULL,
  failure_cause TEXT NOT NULL CHECK (failure_cause IN ('rate_limited','credential_invalid','lane_unavailable','timeout','transport','provider_failure'))
);
CREATE TABLE IF NOT EXISTS verification_candidate_requeues (
  event_id TEXT PRIMARY KEY REFERENCES verification_candidate_events(event_id),
  cause TEXT NOT NULL CHECK (cause IN ('jury2_rejected','jury2_provider_uncertain'))
);
CREATE TABLE IF NOT EXISTS verification_candidate_terminals (
  event_id TEXT PRIMARY KEY REFERENCES verification_candidate_events(event_id),
  resolution TEXT NOT NULL,
  assurance TEXT NOT NULL CHECK (assurance IN ('passed','not_evaluated','contested','non_crediting')),
  CHECK ((resolution = 'jury2_not_eligible' AND assurance = 'not_evaluated') OR (resolution = 'jury2_off' AND assurance = 'not_evaluated') OR (resolution = 'jury2_accepted' AND assurance = 'passed') OR (resolution = 'jury2_rejected_nonbinding' AND assurance = 'contested') OR (resolution = 'majority_fallback' AND assurance = 'contested') OR (resolution IN ('jury1_technical','jury1_guard','jury2_technical','no_consensus','jury2_rejected','jury1_provider_uncertain','jury2_provider_uncertain','cancelled') AND assurance = 'non_crediting'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_candidate_event_once
ON verification_candidate_events(candidate_id, event_type);

CREATE TABLE IF NOT EXISTS jury1_rejection_events (
  event_id TEXT PRIMARY KEY,
  logical_request_id TEXT NOT NULL UNIQUE REFERENCES llm_logical_requests(logical_request_id),
  state_cause TEXT NOT NULL CHECK (state_cause IN ('schema_invalid','evidence_cardinality_invalid','grounding_invalid','provenance_invalid','stage_disagreement')),
  cause TEXT NOT NULL CHECK (cause IN (
    'schema_invalid','evidence_cardinality_invalid','grounding_invalid','provenance_invalid','stage_disagreement',
    'grounding/empty_quotation','grounding/ambiguous_exact_raw_match','grounding/ambiguous_normalized_match','grounding/ungrounded_omission_marker','grounding/no_guarded_fuzzy_match','grounding/ambiguous_guarded_fuzzy_match','grounding/malformed_locator_result','grounding/invalid_locator_range','grounding/exact_locator_mismatch','grounding/normalized_locator_mismatch','grounding/fuzzy_locator_policy_violation','grounding/decision_or_context_invalid','grounding/duplicate_evidence_range','grounding/duplicate_evidence_text','grounding/evidence_outside_context'
  )),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_dispatch_attempts (
  dispatch_attempt_id TEXT PRIMARY KEY,
  logical_request_id TEXT NOT NULL REFERENCES llm_logical_requests(logical_request_id),
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  credential_id TEXT NOT NULL,
  credential_fingerprint TEXT,
  lane_id TEXT NOT NULL,
  credential_cursor INTEGER NOT NULL CHECK (typeof(credential_cursor) = 'integer' AND credential_cursor >= 0),
  model_draw_index INTEGER NOT NULL CHECK (typeof(model_draw_index) = 'integer' AND model_draw_index >= 0),
  selection_hash TEXT NOT NULL,
  pacing_hash TEXT NOT NULL,
  prior_global_start_at TEXT,
  prior_model_start_at TEXT,
  next_eligible_at TEXT,
  global_interval_ms INTEGER NOT NULL CHECK (typeof(global_interval_ms) = 'integer' AND global_interval_ms >= 0),
  model_interval_ms INTEGER NOT NULL CHECK (typeof(model_interval_ms) = 'integer' AND model_interval_ms >= 0),
  queued_at TEXT NOT NULL,
  leased_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_dispatch_events (
  event_id TEXT PRIMARY KEY,
  logical_request_id TEXT NOT NULL REFERENCES llm_logical_requests(logical_request_id),
  dispatch_attempt_id TEXT REFERENCES llm_dispatch_attempts(dispatch_attempt_id),
  event_type TEXT NOT NULL CHECK (event_type IN (
    'leased','started','completed','failed','abandoned','pacing_wait'
  )),
  created_at TEXT NOT NULL,
  CHECK (
    (event_type = 'pacing_wait' AND dispatch_attempt_id IS NULL)
    OR (event_type <> 'pacing_wait' AND dispatch_attempt_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS llm_http_attempts (
  event_order INTEGER PRIMARY KEY AUTOINCREMENT,
  network_attempt_id TEXT NOT NULL UNIQUE CHECK(length(trim(network_attempt_id)) > 0),
  logical_request_id TEXT,
  dispatch_attempt_id TEXT,
  jury_stage TEXT CHECK(jury_stage IS NULL OR jury_stage IN (
    'support_gate','full_support_gate','contrary_gate','topic_gate',
    'explanation_evidence','jury2'
  )),
  provider_id TEXT,
  model_id TEXT,
  credential_alias TEXT,
  credential_fingerprint TEXT,
  lane_id TEXT,
  started_at_ms REAL CHECK(started_at_ms IS NULL OR (typeof(started_at_ms) IN ('integer','real') AND started_at_ms >= 0)),
  endpoint_url TEXT NOT NULL CHECK(length(trim(endpoint_url)) > 0),
  attempt_number INTEGER NOT NULL CHECK(typeof(attempt_number)='integer' AND attempt_number >= 1),
  outcome TEXT NOT NULL CHECK(outcome IN ('response','http_error','invalid_response','network_error')),
  http_status INTEGER CHECK(http_status IS NULL OR (typeof(http_status)='integer' AND http_status BETWEEN 100 AND 599)),
  retryable INTEGER CHECK(retryable IS NULL OR retryable IN (0,1)),
  error_type TEXT CHECK(error_type IS NULL OR length(trim(error_type)) > 0),
  duration_ms REAL NOT NULL CHECK(typeof(duration_ms) IN ('integer','real') AND duration_ms >= 0),
  prompt_cache_hit_tokens INTEGER CHECK(
    prompt_cache_hit_tokens IS NULL OR (
      typeof(prompt_cache_hit_tokens)='integer' AND prompt_cache_hit_tokens >= 0
    )
  ),
  prompt_cache_miss_tokens INTEGER CHECK(
    prompt_cache_miss_tokens IS NULL OR (
      typeof(prompt_cache_miss_tokens)='integer' AND prompt_cache_miss_tokens >= 0
    )
  ),
  created_at TEXT NOT NULL,
  CHECK(
    (outcome='response' AND http_status IS NOT NULL AND retryable IS NULL AND error_type IS NULL)
    OR (outcome='http_error' AND http_status IS NOT NULL AND retryable IS NOT NULL AND error_type IS NULL)
    OR (outcome IN ('invalid_response','network_error') AND http_status IS NULL AND retryable IS NOT NULL AND error_type IS NOT NULL)
  ),
  CHECK(
    (prompt_cache_hit_tokens IS NULL AND prompt_cache_miss_tokens IS NULL)
    OR (
      outcome='response' AND prompt_cache_hit_tokens IS NOT NULL
      AND prompt_cache_miss_tokens IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS llm_dispatch_pacing_details (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_events(event_id),
  pacing_hash TEXT NOT NULL CHECK (length(pacing_hash) = 64 AND pacing_hash NOT GLOB '*[^0-9a-f]*'),
  prior_global_start_at TEXT CHECK (prior_global_start_at IS NULL OR length(trim(prior_global_start_at)) > 0),
  prior_model_start_at TEXT CHECK (prior_model_start_at IS NULL OR length(trim(prior_model_start_at)) > 0),
  next_eligible_at TEXT NOT NULL CHECK (length(trim(next_eligible_at)) > 0),
  global_interval_ms INTEGER NOT NULL CHECK (typeof(global_interval_ms) = 'integer' AND global_interval_ms >= 0),
  model_interval_ms INTEGER NOT NULL CHECK (typeof(model_interval_ms) = 'integer' AND model_interval_ms >= 0)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_terminal_details (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_events(event_id),
  technical_result TEXT NOT NULL CHECK (technical_result IN (
    'answer_received','not_started','protocol_invalid','rate_limited',
    'credential_invalid','lane_unavailable','timeout','transport',
    'provider_failure'
  )),
  http_status INTEGER CHECK (http_status IS NULL OR (typeof(http_status) = 'integer' AND http_status BETWEEN 100 AND 599)),
  latency_ms REAL CHECK (latency_ms IS NULL OR (typeof(latency_ms) IN ('integer','real') AND latency_ms >= 0)),
  retry_cause TEXT CHECK (retry_cause IS NULL OR retry_cause IN (
    'rate_limited','credential_invalid','lane_unavailable','timeout',
    'transport','provider_failure','lease_expired'
  )),
  answer_hash TEXT CHECK (answer_hash IS NULL OR (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*')),
  retry_after_seconds INTEGER CHECK (retry_after_seconds IS NULL OR (typeof(retry_after_seconds) = 'integer' AND retry_after_seconds >= 0))
);
CREATE TABLE IF NOT EXISTS llm_dispatch_protocol_errors (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_details(event_id),
  error_code TEXT NOT NULL CHECK(error_code IN (
    'contract_invalid','evidence_basis_missing',
    'evidence_cardinality_exceeded','evidence_non_decidable_nonempty',
    'evidence_span_overlap','invalid_utf8_payload','json_duplicate_keys',
    'json_object_ambiguous','json_object_missing','json_wrapper_competing',
    'json_wrapper_extra_value','non_decidable_reason_invalid',
    'response_boolean_invalid','response_content_empty','response_expected_null',
    'response_fields_invalid','response_list_duplicate',
    'response_string_invalid','response_string_list_invalid'
  )),
  response_hash TEXT CHECK(
    response_hash IS NULL OR (
      length(response_hash) = 64
      AND response_hash NOT GLOB '*[^0-9a-f]*'
    )
  )
);
CREATE TABLE IF NOT EXISTS llm_dispatch_terminal_answers (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_details(event_id),
  logical_request_id TEXT NOT NULL REFERENCES llm_logical_requests(logical_request_id),
  payload_fingerprint TEXT NOT NULL CHECK (length(payload_fingerprint) = 64 AND payload_fingerprint NOT GLOB '*[^0-9a-f]*'),
  answer_hash TEXT NOT NULL CHECK (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*')
);
CREATE TABLE IF NOT EXISTS llm_dispatch_support_gate_answers (
    event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
    source_supports_any INTEGER NOT NULL CHECK(source_supports_any IN (0,1)),
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
    CHECK(provider_uncertain=0 OR provider_confidence IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_full_support_gate_answers (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
    source_supports_fully INTEGER NOT NULL CHECK(source_supports_fully IN (0,1)),
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
    CHECK(provider_uncertain=0 OR provider_confidence IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_contrary_gate_answers (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
    paper_demonstrates_opposite INTEGER NOT NULL CHECK(paper_demonstrates_opposite IN (0,1)),
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
    CHECK(provider_uncertain=0 OR provider_confidence IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_topic_gate_answers (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
    same_specific_subject INTEGER NOT NULL CHECK(same_specific_subject IN (0,1)),
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
    CHECK(provider_uncertain=0 OR provider_confidence IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_explanation_evidence_answers (
    event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
    reason TEXT,
  supported_content TEXT CHECK(
    supported_content IS NULL OR length(trim(supported_content)) > 0
  ),
  unsupported_content TEXT CHECK(
    unsupported_content IS NULL OR length(trim(unsupported_content)) > 0
  ),
  incompatible_proposition TEXT CHECK(
    incompatible_proposition IS NULL OR length(trim(incompatible_proposition)) > 0
  ),
  non_decidable_reason TEXT CHECK(non_decidable_reason IS NULL OR non_decidable_reason IN (
    'material_limit','no_consensus','verification_unavailable','retrieval_limit','provider_uncertain'
  )),
  evidence_span_count INTEGER NOT NULL CHECK(
    typeof(evidence_span_count) = 'integer'
    AND evidence_span_count >= 0
    AND evidence_span_count <= 6
    ),
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
  CHECK (
    non_decidable_reason IS NULL
    OR (
      supported_content IS NULL
      AND unsupported_content IS NULL
      AND incompatible_proposition IS NULL
    AND evidence_span_count = 0
    )
    ),
    CHECK(
        (provider_confidence IS NULL AND reason IS NOT NULL AND length(trim(reason)) > 0)
        OR (provider_confidence IS NOT NULL AND reason IS NULL
            AND supported_content IS NULL AND unsupported_content IS NULL
            AND incompatible_proposition IS NULL)
    ),
    CHECK(
      (provider_uncertain=1 AND provider_confidence IS NOT NULL
       AND non_decidable_reason='provider_uncertain')
      OR (provider_uncertain=0 AND non_decidable_reason IS NOT 'provider_uncertain')
    )
);
CREATE TABLE IF NOT EXISTS llm_dispatch_explanation_evidence_spans (
  event_id TEXT NOT NULL REFERENCES llm_dispatch_explanation_evidence_answers(event_id),
  span_order INTEGER NOT NULL CHECK(typeof(span_order) = 'integer' AND span_order >= 0),
  span_id TEXT NOT NULL CHECK(length(trim(span_id)) > 0),
  PRIMARY KEY(event_id, span_order),
  UNIQUE(event_id, span_id)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_jury2_answers (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_answers(event_id),
  passages_fit_claim INTEGER NOT NULL CHECK (passages_fit_claim IN (0,1)),
    reason TEXT,
    provider_confidence REAL CHECK(provider_confidence IS NULL OR (typeof(provider_confidence)='real' AND provider_confidence >= 0.0 AND provider_confidence <= 1.0)),
    provider_uncertain INTEGER NOT NULL CHECK(provider_uncertain IN (0,1)),
    CHECK(
        (reason IS NOT NULL AND length(trim(reason)) > 0 AND provider_confidence IS NULL)
        OR (reason IS NULL AND provider_confidence IS NOT NULL)
    ),
    CHECK(provider_uncertain=0 OR provider_confidence IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_scheduler_controls (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_terminal_details(event_id),
  source_event_id TEXT NOT NULL CHECK (source_event_id = event_id)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_scheduler_cooldowns (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_scheduler_controls(event_id),
  credential_id TEXT NOT NULL CHECK (length(trim(credential_id)) > 0),
  model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
  next_eligible_at INTEGER CHECK (next_eligible_at IS NULL OR (typeof(next_eligible_at) = 'integer' AND next_eligible_at >= 0)),
  status TEXT NOT NULL CHECK (status IN ('cooldown','quarantined','cleared')),
  disabled INTEGER NOT NULL CHECK (disabled IN (0,1)), reason TEXT,
  state_next_eligible_at INTEGER CHECK (state_next_eligible_at IS NULL OR (typeof(state_next_eligible_at) = 'integer' AND state_next_eligible_at >= 0)),
  policy_version TEXT, last_applied_seconds INTEGER CHECK (last_applied_seconds IS NULL OR (typeof(last_applied_seconds) = 'integer' AND last_applied_seconds >= 0)),
  learned_seconds INTEGER CHECK (learned_seconds IS NULL OR (typeof(learned_seconds) = 'integer' AND learned_seconds >= 0)),
  post_cooldown_successes INTEGER CHECK (post_cooldown_successes IS NULL OR (typeof(post_cooldown_successes) = 'integer' AND post_cooldown_successes >= 0)),
  CHECK (
    (status = 'quarantined' AND disabled = 1 AND reason = 'credential_invalid' AND next_eligible_at IS NULL AND state_next_eligible_at IS NULL AND policy_version IS NULL AND last_applied_seconds IS NULL AND learned_seconds IS NULL AND post_cooldown_successes IS NULL)
    OR (status = 'cooldown' AND disabled = 0 AND reason = 'rate_limited' AND next_eligible_at = state_next_eligible_at * 1000 AND state_next_eligible_at IS NOT NULL AND length(trim(policy_version)) > 0 AND post_cooldown_successes IS NOT NULL)
    OR (status = 'cleared' AND disabled = 0 AND reason IS NULL AND next_eligible_at IS NULL AND state_next_eligible_at IS NOT NULL AND length(trim(policy_version)) > 0 AND post_cooldown_successes IS NOT NULL)
  )
);
CREATE TABLE IF NOT EXISTS llm_dispatch_scheduler_lanes (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_scheduler_controls(event_id), credential_id TEXT NOT NULL,
  model_id TEXT NOT NULL, lane_id TEXT NOT NULL,
  available INTEGER NOT NULL CHECK (available = 0),
  CHECK (length(trim(credential_id)) > 0 AND length(trim(model_id)) > 0 AND length(trim(lane_id)) > 0)
);
CREATE TABLE IF NOT EXISTS llm_dispatch_scheduler_profiles (
  event_id TEXT PRIMARY KEY REFERENCES llm_dispatch_scheduler_controls(event_id), provider_id TEXT NOT NULL,
  credential_fingerprint TEXT NOT NULL CHECK (length(credential_fingerprint) = 64 AND credential_fingerprint NOT GLOB '*[^0-9a-f]*'),
  model_id TEXT NOT NULL, profile_version TEXT NOT NULL,
  baseline_seconds INTEGER NOT NULL CHECK (typeof(baseline_seconds) = 'integer' AND baseline_seconds >= 0),
  multiplier INTEGER NOT NULL CHECK (typeof(multiplier) = 'integer' AND multiplier >= 1),
  local_max_seconds INTEGER NOT NULL CHECK (typeof(local_max_seconds) = 'integer' AND local_max_seconds >= baseline_seconds),
  stable_successes INTEGER NOT NULL CHECK (typeof(stable_successes) = 'integer' AND stable_successes >= 0),
  CHECK (length(trim(provider_id)) > 0 AND length(trim(model_id)) > 0 AND length(trim(profile_version)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_dispatch_event_once
ON llm_dispatch_events(dispatch_attempt_id, event_type)
WHERE dispatch_attempt_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_dispatch_terminal_once
ON llm_dispatch_events(dispatch_attempt_id)
WHERE event_type IN ('completed','failed','abandoned');

-- The only mutable technical projection.  It is deliberately separate from
-- immutable attempts so a retry can be admitted only after release/terminal.
CREATE TABLE IF NOT EXISTS llm_dispatch_leases (
  logical_request_id TEXT PRIMARY KEY REFERENCES llm_logical_requests(logical_request_id),
  dispatch_attempt_id TEXT NOT NULL REFERENCES llm_dispatch_attempts(dispatch_attempt_id),
  status TEXT NOT NULL CHECK (status IN ('live','released','terminal')),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_dispatch_attempts_request
ON llm_dispatch_attempts(logical_request_id, created_at);

CREATE TABLE IF NOT EXISTS performance_snapshot (
  session_id TEXT PRIMARY KEY REFERENCES run_sessions(session_id),
  recorded_at TEXT NOT NULL,
  span_count INTEGER NOT NULL CHECK(typeof(span_count)='integer' AND span_count >= 0)
);
CREATE TABLE IF NOT EXISTS performance_spans (
  session_id TEXT NOT NULL REFERENCES performance_snapshot(session_id),
  phase TEXT NOT NULL CHECK(length(trim(phase)) > 0),
  span_key_present INTEGER NOT NULL CHECK(span_key_present IN (0,1)),
  span_key TEXT NOT NULL,
  sample_count INTEGER NOT NULL CHECK(typeof(sample_count)='integer' AND sample_count >= 1),
  total_seconds REAL NOT NULL CHECK(typeof(total_seconds) IN ('integer','real') AND total_seconds >= 0),
  max_seconds REAL NOT NULL CHECK(typeof(max_seconds) IN ('integer','real') AND max_seconds >= 0 AND max_seconds <= total_seconds),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(session_id, phase, span_key_present, span_key),
  CHECK((span_key_present=0 AND span_key='') OR (span_key_present=1 AND length(trim(span_key)) > 0))
);
"""

SCHEMA_SQL += (
    "\n" + ATTEMPT_DDL + "\n" + INTEGRITY_DDL
    + "\n" + TASK_ANSWER_PROVENANCE_DDL + "\n" + UNIT_PROGRESS_DDL
    + "\n" + RESOLVE_TRANSPORT_DDL
    + "\n" + INSTITUTIONAL_SEARCH_PROVENANCE_DDL
    + "\n" + INSTITUTIONAL_SEARCH_PROVENANCE_TRIGGERS
    + "\n" + FETCH_TRANSPORT_DDL
    + "\n" + CREDENTIAL_OBSERVATION_DDL
)

APPEND_ONLY_TRIGGERS_SQL = (
    INTEGRITY_TRIGGERS_SQL
    + UNIT_PROGRESS_TRIGGERS_SQL
    + CREDENTIAL_OBSERVATION_TRIGGERS
    + """
CREATE TRIGGER IF NOT EXISTS run_config_snapshot_no_update BEFORE UPDATE ON run_config_snapshot BEGIN SELECT RAISE(ABORT, 'run config snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS run_config_snapshot_no_delete BEFORE DELETE ON run_config_snapshot BEGIN SELECT RAISE(ABORT, 'run config snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS run_runtime_settings_no_delete BEFORE DELETE ON run_runtime_settings BEGIN SELECT RAISE(ABORT, 'run runtime settings singleton is required'); END;
CREATE TRIGGER IF NOT EXISTS run_runtime_debug_labels_insert_guard
BEFORE INSERT ON run_runtime_debug_labels
WHEN NOT EXISTS (
  SELECT 1 FROM run_runtime_settings
  WHERE singleton=1 AND debug_labels_present=1
    AND NEW.label_order < debug_labels_count
)
BEGIN
  SELECT RAISE(ABORT, 'debug label storage is inconsistent');
END;
CREATE TRIGGER IF NOT EXISTS phase_events_no_update
BEFORE UPDATE ON phase_events BEGIN
  SELECT RAISE(ABORT, 'phase_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS source_text_materialization_intents_no_update
BEFORE UPDATE ON source_text_materialization_intents BEGIN SELECT RAISE(ABORT, 'source text materialization intents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_text_materialization_intent_flag_states_no_update
BEFORE UPDATE ON source_text_materialization_intent_flag_states BEGIN SELECT RAISE(ABORT, 'source text materialization intent flags are immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_text_materialization_intent_flags_no_update
BEFORE UPDATE ON source_text_materialization_intent_flags BEGIN SELECT RAISE(ABORT, 'source text materialization intent flags are immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_text_materialization_outcomes_no_update
BEFORE UPDATE ON source_text_materialization_outcomes BEGIN SELECT RAISE(ABORT, 'source text materialization outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_text_materialization_invalidations_no_update
BEFORE UPDATE ON source_text_materialization_invalidations BEGIN SELECT RAISE(ABORT, 'source text materialization invalidations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_text_materialization_invalidations_no_delete
BEFORE DELETE ON source_text_materialization_invalidations BEGIN SELECT RAISE(ABORT, 'source text materialization invalidations are append-only'); END;

CREATE TRIGGER IF NOT EXISTS phase_events_no_delete
BEFORE DELETE ON phase_events BEGIN
  SELECT RAISE(ABORT, 'phase_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS phase_event_session_created_no_update BEFORE UPDATE ON phase_event_session_created BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_session_created_no_delete BEFORE DELETE ON phase_event_session_created BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_session_created_kind_guard BEFORE INSERT ON phase_event_session_created WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='session_created') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_session_resumed_no_update BEFORE UPDATE ON phase_event_session_resumed BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_session_resumed_no_delete BEFORE DELETE ON phase_event_session_resumed BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_session_resumed_kind_guard BEFORE INSERT ON phase_event_session_resumed WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='session_resumed') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_pause_no_update BEFORE UPDATE ON phase_event_pause BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_pause_no_delete BEFORE DELETE ON phase_event_pause BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_pause_kind_guard BEFORE INSERT ON phase_event_pause WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='pause') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_restart_no_update BEFORE UPDATE ON phase_event_restart BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_restart_no_delete BEFORE DELETE ON phase_event_restart BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_restart_kind_guard BEFORE INSERT ON phase_event_restart WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='restart') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_gate_failure_no_update BEFORE UPDATE ON phase_event_gate_failure BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_gate_failure_no_delete BEFORE DELETE ON phase_event_gate_failure BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_gate_failure_kind_guard BEFORE INSERT ON phase_event_gate_failure WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='gate_failure') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_frozen_fork_no_update BEFORE UPDATE ON phase_event_frozen_fork BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_frozen_fork_no_delete BEFORE DELETE ON phase_event_frozen_fork BEGIN SELECT RAISE(ABORT, 'phase event details are append-only'); END;
CREATE TRIGGER IF NOT EXISTS phase_event_frozen_fork_kind_guard BEFORE INSERT ON phase_event_frozen_fork WHEN NOT EXISTS (SELECT 1 FROM phase_events WHERE event_id=NEW.event_id AND detail_kind='frozen_fork') BEGIN SELECT RAISE(ABORT, 'phase event detail kind mismatch'); END;

CREATE TRIGGER IF NOT EXISTS verification_pair_transitions_no_update
BEFORE UPDATE ON verification_pair_transitions BEGIN
  SELECT RAISE(ABORT, 'verification_pair_transitions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS verification_pair_transitions_no_delete
BEFORE DELETE ON verification_pair_transitions BEGIN
  SELECT RAISE(ABORT, 'verification_pair_transitions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS verification_candidates_no_update
BEFORE UPDATE ON verification_candidates BEGIN
  SELECT RAISE(ABORT, 'verification_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS verification_candidates_no_delete
BEFORE DELETE ON verification_candidates BEGIN
  SELECT RAISE(ABORT, 'verification_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_events_no_update
BEFORE UPDATE ON verification_candidate_events BEGIN
  SELECT RAISE(ABORT, 'verification_candidate_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_events_no_delete
BEFORE DELETE ON verification_candidate_events BEGIN
  SELECT RAISE(ABORT, 'verification_candidate_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_guard_rejections_no_update BEFORE UPDATE ON verification_candidate_guard_rejections BEGIN SELECT RAISE(ABORT, 'verification_candidate_guard_rejections is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_guard_rejections_no_delete BEFORE DELETE ON verification_candidate_guard_rejections BEGIN SELECT RAISE(ABORT, 'verification_candidate_guard_rejections is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_jury2_decisions_no_update BEFORE UPDATE ON verification_candidate_jury2_decisions BEGIN SELECT RAISE(ABORT, 'verification_candidate_jury2_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_jury2_decisions_no_delete BEFORE DELETE ON verification_candidate_jury2_decisions BEGIN SELECT RAISE(ABORT, 'verification_candidate_jury2_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_jury2_technical_failures_no_update BEFORE UPDATE ON verification_candidate_jury2_technical_failures BEGIN SELECT RAISE(ABORT, 'verification_candidate_jury2_technical_failures is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_jury2_technical_failures_no_delete BEFORE DELETE ON verification_candidate_jury2_technical_failures BEGIN SELECT RAISE(ABORT, 'verification_candidate_jury2_technical_failures is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_requeues_no_update BEFORE UPDATE ON verification_candidate_requeues BEGIN SELECT RAISE(ABORT, 'verification_candidate_requeues is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_requeues_no_delete BEFORE DELETE ON verification_candidate_requeues BEGIN SELECT RAISE(ABORT, 'verification_candidate_requeues is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_terminals_no_update BEFORE UPDATE ON verification_candidate_terminals BEGIN SELECT RAISE(ABORT, 'verification_candidate_terminals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_terminals_no_delete BEFORE DELETE ON verification_candidate_terminals BEGIN SELECT RAISE(ABORT, 'verification_candidate_terminals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS jury1_rejection_events_no_update
BEFORE UPDATE ON jury1_rejection_events BEGIN
  SELECT RAISE(ABORT, 'jury1_rejection_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS jury1_rejection_events_no_delete
BEFORE DELETE ON jury1_rejection_events BEGIN
  SELECT RAISE(ABORT, 'jury1_rejection_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_logical_requests_no_update
BEFORE UPDATE ON llm_logical_requests BEGIN
  SELECT RAISE(ABORT, 'llm_logical_requests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_logical_requests_no_delete
BEFORE DELETE ON llm_logical_requests BEGIN
  SELECT RAISE(ABORT, 'llm_logical_requests is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_jury1_request_payloads_no_update BEFORE UPDATE ON llm_jury1_request_payloads BEGIN SELECT RAISE(ABORT, 'llm_jury1_request_payloads is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury1_request_payloads_no_delete BEFORE DELETE ON llm_jury1_request_payloads BEGIN SELECT RAISE(ABORT, 'llm_jury1_request_payloads is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury1_request_source_spans_no_update BEFORE UPDATE ON llm_jury1_request_source_spans BEGIN SELECT RAISE(ABORT, 'llm_jury1_request_source_spans is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury1_request_source_spans_no_delete BEFORE DELETE ON llm_jury1_request_source_spans BEGIN SELECT RAISE(ABORT, 'llm_jury1_request_source_spans is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury2_request_payloads_no_update BEFORE UPDATE ON llm_jury2_request_payloads BEGIN SELECT RAISE(ABORT, 'llm_jury2_request_payloads is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury2_request_payloads_no_delete BEFORE DELETE ON llm_jury2_request_payloads BEGIN SELECT RAISE(ABORT, 'llm_jury2_request_payloads is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury2_request_passages_no_update BEFORE UPDATE ON llm_jury2_request_passages BEGIN SELECT RAISE(ABORT, 'llm_jury2_request_passages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_jury2_request_passages_no_delete BEFORE DELETE ON llm_jury2_request_passages BEGIN SELECT RAISE(ABORT, 'llm_jury2_request_passages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_evidence_no_update BEFORE UPDATE ON verification_candidate_evidence BEGIN SELECT RAISE(ABORT, 'verification_candidate_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_evidence_no_delete BEFORE DELETE ON verification_candidate_evidence BEGIN SELECT RAISE(ABORT, 'verification_candidate_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_grounding_no_update BEFORE UPDATE ON verification_candidate_grounding BEGIN SELECT RAISE(ABORT, 'verification_candidate_grounding is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidate_grounding_no_delete BEFORE DELETE ON verification_candidate_grounding BEGIN SELECT RAISE(ABORT, 'verification_candidate_grounding is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_ledger_metadata_no_update BEFORE UPDATE ON verification_ledger_metadata BEGIN SELECT RAISE(ABORT, 'verification_ledger_metadata is append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_ledger_metadata_no_delete BEFORE DELETE ON verification_ledger_metadata BEGIN SELECT RAISE(ABORT, 'verification_ledger_metadata is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_attempts_no_update
BEFORE UPDATE ON llm_dispatch_attempts BEGIN
  SELECT RAISE(ABORT, 'llm_dispatch_attempts is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_attempts_no_delete
BEFORE DELETE ON llm_dispatch_attempts BEGIN
  SELECT RAISE(ABORT, 'llm_dispatch_attempts is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_events_no_update
BEFORE UPDATE ON llm_dispatch_events BEGIN
  SELECT RAISE(ABORT, 'llm_dispatch_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_events_no_delete
BEFORE DELETE ON llm_dispatch_events BEGIN
  SELECT RAISE(ABORT, 'llm_dispatch_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS llm_http_attempts_no_update BEFORE UPDATE ON llm_http_attempts BEGIN SELECT RAISE(ABORT, 'llm_http_attempts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_http_attempts_no_delete BEFORE DELETE ON llm_http_attempts BEGIN SELECT RAISE(ABORT, 'llm_http_attempts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS performance_spans_no_update BEFORE UPDATE ON performance_spans BEGIN SELECT RAISE(ABORT, 'performance_spans is immutable'); END;
CREATE TRIGGER IF NOT EXISTS performance_spans_no_delete BEFORE DELETE ON performance_spans BEGIN SELECT RAISE(ABORT, 'performance_spans is immutable'); END;
CREATE TRIGGER IF NOT EXISTS performance_snapshot_no_update BEFORE UPDATE ON performance_snapshot BEGIN SELECT RAISE(ABORT, 'performance_snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS performance_snapshot_no_delete BEFORE DELETE ON performance_snapshot BEGIN SELECT RAISE(ABORT, 'performance_snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_pacing_details_no_update BEFORE UPDATE ON llm_dispatch_pacing_details BEGIN SELECT RAISE(ABORT, 'llm_dispatch_pacing_details is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_pacing_details_no_delete BEFORE DELETE ON llm_dispatch_pacing_details BEGIN SELECT RAISE(ABORT, 'llm_dispatch_pacing_details is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_terminal_details_no_update BEFORE UPDATE ON llm_dispatch_terminal_details BEGIN SELECT RAISE(ABORT, 'llm_dispatch_terminal_details is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_terminal_details_no_delete BEFORE DELETE ON llm_dispatch_terminal_details BEGIN SELECT RAISE(ABORT, 'llm_dispatch_terminal_details is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_protocol_errors_no_update BEFORE UPDATE ON llm_dispatch_protocol_errors BEGIN SELECT RAISE(ABORT, 'llm_dispatch_protocol_errors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_protocol_errors_no_delete BEFORE DELETE ON llm_dispatch_protocol_errors BEGIN SELECT RAISE(ABORT, 'llm_dispatch_protocol_errors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_terminal_answers_no_update BEFORE UPDATE ON llm_dispatch_terminal_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_terminal_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_terminal_answers_no_delete BEFORE DELETE ON llm_dispatch_terminal_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_terminal_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_support_gate_answers_no_update BEFORE UPDATE ON llm_dispatch_support_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_support_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_support_gate_answers_no_delete BEFORE DELETE ON llm_dispatch_support_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_support_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_full_support_gate_answers_no_update BEFORE UPDATE ON llm_dispatch_full_support_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_full_support_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_full_support_gate_answers_no_delete BEFORE DELETE ON llm_dispatch_full_support_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_full_support_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_contrary_gate_answers_no_update BEFORE UPDATE ON llm_dispatch_contrary_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_contrary_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_contrary_gate_answers_no_delete BEFORE DELETE ON llm_dispatch_contrary_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_contrary_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_topic_gate_answers_no_update BEFORE UPDATE ON llm_dispatch_topic_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_topic_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_topic_gate_answers_no_delete BEFORE DELETE ON llm_dispatch_topic_gate_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_topic_gate_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_explanation_evidence_answers_no_update BEFORE UPDATE ON llm_dispatch_explanation_evidence_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_explanation_evidence_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_explanation_evidence_answers_no_delete BEFORE DELETE ON llm_dispatch_explanation_evidence_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_explanation_evidence_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_explanation_evidence_spans_no_update BEFORE UPDATE ON llm_dispatch_explanation_evidence_spans BEGIN SELECT RAISE(ABORT, 'llm_dispatch_explanation_evidence_spans is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_explanation_evidence_spans_no_delete BEFORE DELETE ON llm_dispatch_explanation_evidence_spans BEGIN SELECT RAISE(ABORT, 'llm_dispatch_explanation_evidence_spans is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_jury2_answers_no_update BEFORE UPDATE ON llm_dispatch_jury2_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_jury2_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_jury2_answers_no_delete BEFORE DELETE ON llm_dispatch_jury2_answers BEGIN SELECT RAISE(ABORT, 'llm_dispatch_jury2_answers is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_controls_no_update BEFORE UPDATE ON llm_dispatch_scheduler_controls BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_controls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_controls_no_delete BEFORE DELETE ON llm_dispatch_scheduler_controls BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_controls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_cooldowns_no_update BEFORE UPDATE ON llm_dispatch_scheduler_cooldowns BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_cooldowns is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_cooldowns_no_delete BEFORE DELETE ON llm_dispatch_scheduler_cooldowns BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_cooldowns is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_lanes_no_update BEFORE UPDATE ON llm_dispatch_scheduler_lanes BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_lanes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_lanes_no_delete BEFORE DELETE ON llm_dispatch_scheduler_lanes BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_lanes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_profiles_no_update BEFORE UPDATE ON llm_dispatch_scheduler_profiles BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_profiles is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_dispatch_scheduler_profiles_no_delete BEFORE DELETE ON llm_dispatch_scheduler_profiles BEGIN SELECT RAISE(ABORT, 'llm_dispatch_scheduler_profiles is append-only'); END;
"""
    + RESOLVE_TRANSPORT_TRIGGERS + FETCH_TRANSPORT_TRIGGERS
)
