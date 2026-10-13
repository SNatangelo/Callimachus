#!/usr/bin/env python3
# core/infra/db/resolve_attempt_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed typed storage for ordered current Resolve attempt facts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import urllib.parse
from typing import Any


JsonDict = dict[str, Any]

PROVIDER_ATTEMPT_KINDS = {
    "provider_result",
    "identifier_validation",
    "identifier_fallback",
    "enrichment",
}
ATTEMPT_KINDS = PROVIDER_ATTEMPT_KINDS | {
    "pubmed_esummary_failure",
    "resolver_exception",
    "repair_exception",
    "repair_failed",
    "fetch_repair",
}

PROVIDER_TEXT_FIELDS = (
    "abstract",
    "abstract_via",
    "availability_note",
    "book_availability",
    "doi",
    "error_reason",
    "error_type",
    "existence_confidence",
    "existence_corroboration",
    "fulltext_exists_refined_by",
    "identity_basis",
    "matched_title",
    "oa_declared_status",
    "oa_status",
    "paper_id",
    "pmcid",
    "pmid",
    "record_id",
    "resolution_basis",
    "work_type",
)
NULLABLE_PROVIDER_TEXT_FIELDS = {
    "abstract",
    "availability_note",
    "doi",
    "matched_title",
    "paper_id",
    "pmcid",
    "pmid",
    "work_type",
}
PROVIDER_BOOLEAN_FIELDS = ("retracted", "retryable")
PROVIDER_COLLECTION_FIELDS = (
    "article_ids",
    "identifiers",
    "matched_authors",
    "oa_license_urls",
    "fulltext_links",
)
PROVIDER_OBJECT_FIELDS = (
    "fulltext_availability",
    "metadata_match",
    "resolved_identifier",
    "trial_registration",
    "identity_search",
)
CROSSREF_NATIVE_WORK_PROOF_FIELD = "_crossref_native_work_proof"
CROSSREF_NATIVE_WORK_PROOF_FIELDS = {
    "version", "requested_doi", "request_url", "http_status",
    "response_body", "response_sha256",
}
CROSSREF_NATIVE_ARTICLE_PROOF_FIELDS = CROSSREF_NATIVE_WORK_PROOF_FIELDS | {
    "source_raw_sha256",
}
DATACITE_NATIVE_DOI_PROOF_FIELD = "_datacite_native_doi_proof"
DATACITE_NATIVE_DOI_PROOF_FIELDS = {
    "version", "requested_doi", "request_url", "http_status",
    "response_body", "response_sha256",
}

PROVIDER_FIELDS = {
    "status",
    "via",
    "reason",
    *PROVIDER_TEXT_FIELDS,
    *PROVIDER_BOOLEAN_FIELDS,
    "fulltext_exists",
    "http_status",
    "matched_year",
    *PROVIDER_COLLECTION_FIELDS,
    *PROVIDER_OBJECT_FIELDS,
    CROSSREF_NATIVE_WORK_PROOF_FIELD,
    DATACITE_NATIVE_DOI_PROOF_FIELD,
}

METADATA_MINIMAL_FIELDS = {"title_overlap", "matched_year"}
METADATA_ORDINARY_FIELDS = {
    "score",
    "title_overlap",
    "author_match",
    "cited_first_author",
    "matched_first_author",
    "year_match",
    "matched_year",
    "venue_overlap",
    "matched_venue",
}
METADATA_ORDINAL_FIELDS = {
    "ordinal_conflict",
    "cited_ordinals",
    "matched_ordinals",
}
METADATA_CONFLICT_FIELDS = {
    "author_conflict",
    "year_conflict",
    "venue_conflict",
    "metadata_conflict",
    "hard_conflicts",
}
METADATA_COORDINATE_COMPARISONS_FIELD = "coordinate_comparisons"
PUBLISHER_NATIVE_URI_PROOF_FIELD = "_publisher_native_uri_proof"
GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD = "_crossref_generic_article_locator_certificate"
TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD = "_titleless_native_venue_composition"
REGISTERED_COORDINATE_CERTIFICATE_FIELD = "registered_coordinate_certificate"
REGISTERED_COORDINATE_CERTIFICATE_RULE_VERSION = "crossref-nlm-registered-coordinates/v1"
GENERIC_ARTICLE_LOCATOR_CERTIFICATE_RULE_VERSION = "crossref-generic-locator-role/v1"
GENERIC_ARTICLE_LOCATOR_SEARCH_RULE_VERSION = "crossref-complete-generic-locator-query/v1"
GENERIC_ARTICLE_LOCATOR_REGISTERED_SCOPE_RULE_VERSION = "crossref-nlm-issn-source-scope/v2"
GENERIC_NATIVE_RECORD_FIELDS = {
    "DOI", "type", "title", "subtitle", "author", "published-print",
    "published-online", "published", "issued", "container-title",
    "short-container-title", "volume", "issue", "page", "article-number",
}
GENERIC_CERTIFICATE_FIELDS = {
    "rule_version", "raw_entry_sha256", "source", "native_locator_field",
    "native_locator_value", "native_venue", "native_record_sha256",
    "native_record_snapshot",
}
PUBLISHER_NATIVE_URI_PROOF_FIELDS = {
    "version",
    "selected_doi",
    "source_uri",
    "source_pii",
    "source_entry_sha256",
    "crossref_request_url",
    "crossref_http_status",
    "crossref_response_body",
    "crossref_response_sha256",
    "publisher_request_url",
    "publisher_final_url",
    "publisher_http_status",
    "publisher_response_body",
    "publisher_response_sha256",
}
METADATA_COORDINATE_KINDS = {
    "container", "volume", "issue", "article_page_range", "chapter_page_range",
    "elocator", "article_number", "article_locator",
}
METADATA_COORDINATE_STATUSES = {"match", "mismatch", "inconclusive"}

FULLTEXT_LINK_FIELDS = {
    "url",
    "availability",
    "content_type",
    "content_version",
    "identity_context",
    "intended_application",
    "site",
    "status",
}
FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS = (
    "availability",
    "content_type",
    "content_version",
    "intended_application",
    "site",
    "status",
)
IDENTITY_CONTEXT_FIELDS = {
    "title",
    "authors",
    "year",
    "identifiers",
    "provider",
    "provider_record_id",
    "first_author",
    "source_confidence",
    "canonical_host",
    "canonical_url",
    "landing_page_url",
    "expected_document_title",
    "official",
    "official_document_relation",
}

FETCH_REPAIR_FIELDS = {
    "status",
    "via",
    "method",
    "reason",
    "pdf_url",
    "content_version",
    "corroborate_signal",
    "corroborate_score",
    "direct_attempts",
    "execution_attempts",
}
DIRECT_ATTEMPT_FIELDS = {"method", "source_ref", "outcome", "reason"}
EXECUTION_ATTEMPT_FIELDS = {
    "method",
    "url",
    "final_url",
    "kind",
    "outcome",
    "reason",
    "status",
    "content_type",
}

ATTEMPT_TABLES = (
    "resolve_attempt_states",
    "resolve_attempts",
    "resolve_attempt_provider_details",
    "resolve_attempt_identifier_values",
    "resolve_attempt_matched_authors",
    "resolve_attempt_oa_license_urls",
    "resolve_attempt_fulltext_availability",
    "resolve_attempt_resolved_identifiers",
    "resolve_attempt_trial_registrations",
    "resolve_attempt_identity_searches",
    "resolve_attempt_metadata_matches",
    "resolve_attempt_metadata_coordinate_comparisons",
    "resolve_attempt_metadata_hard_conflicts",
    "resolve_attempt_metadata_ordinals",
    "resolve_attempt_fulltext_links",
    "resolve_attempt_link_contexts",
    "resolve_attempt_link_context_authors",
    "resolve_attempt_link_context_identifiers",
    "resolve_attempt_exceptions",
    "resolve_attempt_fetch_repairs",
    "resolve_attempt_fetch_repair_direct",
    "resolve_attempt_fetch_repair_execution",
)


def _present_columns(names: tuple[str, ...]) -> str:
    return ",\n  ".join(
        f"{name} TEXT, {name}_present INTEGER NOT NULL CHECK({name}_present IN (0,1))"
        for name in names
    )


def _presence_checks(names: tuple[str, ...]) -> str:
    return ",\n  ".join(
        f"CHECK({name}_present=1 OR {name} IS NULL)" for name in names
    )


_PROVIDER_TEXT_DDL = _present_columns(PROVIDER_TEXT_FIELDS)
_LINK_TEXT_DDL = _present_columns(FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS)
_PROVIDER_TEXT_CHECKS = _presence_checks(PROVIDER_TEXT_FIELDS)
_LINK_TEXT_CHECKS = _presence_checks(FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS)

ATTEMPT_DDL = f"""
CREATE TABLE IF NOT EXISTS resolve_attempt_states (
  ref_id TEXT PRIMARY KEY REFERENCES resolve_results(ref_id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('produced','attempts_not_produced')),
  attempt_count INTEGER NOT NULL CHECK(typeof(attempt_count)='integer' AND attempt_count>=0),
  CHECK(state='produced' OR attempt_count=0)
);

CREATE TABLE IF NOT EXISTS resolve_attempts (
  ref_id TEXT NOT NULL REFERENCES resolve_attempt_states(ref_id) ON DELETE CASCADE,
  attempt_order INTEGER NOT NULL CHECK(typeof(attempt_order)='integer' AND attempt_order>=0),
  attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ({','.join(repr(kind) for kind in sorted(ATTEMPT_KINDS))})),
  status TEXT,
  status_present INTEGER NOT NULL CHECK(status_present IN (0,1)),
  via TEXT NOT NULL CHECK(typeof(via)='text' AND length(trim(via))>0 AND instr(via,char(0))=0),
  reason TEXT,
  reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1)),
  identifier_type TEXT,
  identifier_value TEXT,
  provider_via TEXT,
  provider_via_present INTEGER NOT NULL CHECK(provider_via_present IN (0,1)),
  PRIMARY KEY(ref_id,attempt_order),
  CHECK((status_present=0 AND status IS NULL) OR
        (status_present=1 AND typeof(status)='text' AND length(trim(status))>0 AND instr(status,char(0))=0)),
  CHECK(reason_present=1 OR reason IS NULL),
  CHECK(provider_via_present=1 OR provider_via IS NULL),
  CHECK((attempt_kind='pubmed_esummary_failure' AND status_present=0) OR
        (attempt_kind!='pubmed_esummary_failure' AND status_present=1)),
  CHECK((attempt_kind='identifier_validation' AND identifier_type='doi' AND
         typeof(identifier_value)='text' AND length(trim(identifier_value))>0) OR
        (attempt_kind!='identifier_validation' AND identifier_type IS NULL AND identifier_value IS NULL)),
  CHECK((attempt_kind='identifier_fallback' AND provider_via_present=1) OR
        (attempt_kind!='identifier_fallback' AND provider_via_present=0 AND provider_via IS NULL))
);

CREATE TABLE IF NOT EXISTS resolve_attempt_provider_details (
  ref_id TEXT NOT NULL,
  attempt_order INTEGER NOT NULL,
  {_PROVIDER_TEXT_DDL},
  retracted INTEGER,
  retracted_present INTEGER NOT NULL CHECK(retracted_present IN (0,1)),
  retryable INTEGER,
  retryable_present INTEGER NOT NULL CHECK(retryable_present IN (0,1)),
  http_status INTEGER,
  http_status_present INTEGER NOT NULL CHECK(http_status_present IN (0,1)),
  fulltext_exists_kind TEXT NOT NULL CHECK(fulltext_exists_kind IN ('absent','boolean','text')),
  fulltext_exists_boolean INTEGER CHECK(fulltext_exists_boolean IN (0,1)),
  fulltext_exists_text TEXT,
  matched_year_kind TEXT NOT NULL CHECK(matched_year_kind IN ('absent','null','integer','text')),
  matched_year_integer INTEGER,
  matched_year_text TEXT,
  article_ids_present INTEGER NOT NULL CHECK(article_ids_present IN (0,1)),
  article_ids_count INTEGER NOT NULL CHECK(article_ids_count>=0),
  identifiers_present INTEGER NOT NULL CHECK(identifiers_present IN (0,1)),
  identifiers_count INTEGER NOT NULL CHECK(identifiers_count>=0),
  matched_authors_present INTEGER NOT NULL CHECK(matched_authors_present IN (0,1)),
  matched_authors_count INTEGER NOT NULL CHECK(matched_authors_count>=0),
  oa_license_urls_present INTEGER NOT NULL CHECK(oa_license_urls_present IN (0,1)),
  oa_license_urls_count INTEGER NOT NULL CHECK(oa_license_urls_count>=0),
  fulltext_links_present INTEGER NOT NULL CHECK(fulltext_links_present IN (0,1)),
  fulltext_links_count INTEGER NOT NULL CHECK(fulltext_links_count>=0),
  fulltext_availability_present INTEGER NOT NULL CHECK(fulltext_availability_present IN (0,1)),
  metadata_match_present INTEGER NOT NULL CHECK(metadata_match_present IN (0,1)),
  resolved_identifier_present INTEGER NOT NULL CHECK(resolved_identifier_present IN (0,1)),
  trial_registration_present INTEGER NOT NULL CHECK(trial_registration_present IN (0,1)),
  identity_search_present INTEGER NOT NULL CHECK(identity_search_present IN (0,1)),
  crossref_native_work_proof_json TEXT,
  crossref_native_work_proof_present INTEGER NOT NULL CHECK(crossref_native_work_proof_present IN (0,1)),
  datacite_native_doi_proof_json TEXT,
  datacite_native_doi_proof_present INTEGER NOT NULL CHECK(datacite_native_doi_proof_present IN (0,1)),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempts(ref_id,attempt_order) ON DELETE CASCADE,
  {_PROVIDER_TEXT_CHECKS},
  CHECK((retracted_present=0 AND retracted IS NULL) OR (retracted_present=1 AND retracted IN (0,1))),
  CHECK((retryable_present=0 AND retryable IS NULL) OR (retryable_present=1 AND retryable IN (0,1))),
  CHECK((http_status_present=0 AND http_status IS NULL) OR
        (http_status_present=1 AND typeof(http_status)='integer')),
  CHECK((fulltext_exists_kind='absent' AND fulltext_exists_boolean IS NULL AND fulltext_exists_text IS NULL) OR
        (fulltext_exists_kind='boolean' AND fulltext_exists_boolean IN (0,1) AND fulltext_exists_text IS NULL) OR
        (fulltext_exists_kind='text' AND fulltext_exists_boolean IS NULL AND fulltext_exists_text='unknown')),
  CHECK((matched_year_kind IN ('absent','null') AND matched_year_integer IS NULL AND matched_year_text IS NULL) OR
        (matched_year_kind='integer' AND typeof(matched_year_integer)='integer' AND matched_year_text IS NULL) OR
        (matched_year_kind='text' AND matched_year_integer IS NULL AND typeof(matched_year_text)='text')),
  CHECK(article_ids_present=1 OR article_ids_count=0),
  CHECK(identifiers_present=1 OR identifiers_count=0),
  CHECK(matched_authors_present=1 OR matched_authors_count=0),
  CHECK(oa_license_urls_present=1 OR oa_license_urls_count=0),
  CHECK(fulltext_links_present=1 OR fulltext_links_count=0),
  CHECK((crossref_native_work_proof_present=0 AND crossref_native_work_proof_json IS NULL) OR
        (crossref_native_work_proof_present=1 AND typeof(crossref_native_work_proof_json)='text')),
  CHECK((datacite_native_doi_proof_present=0 AND datacite_native_doi_proof_json IS NULL) OR
        (datacite_native_doi_proof_present=1 AND typeof(datacite_native_doi_proof_json)='text'))
);

CREATE TABLE IF NOT EXISTS resolve_attempt_identifier_values (
  ref_id TEXT NOT NULL,
  attempt_order INTEGER NOT NULL,
  identifier_family TEXT NOT NULL CHECK(identifier_family IN ('article_ids','identifiers')),
  identifier_order INTEGER NOT NULL CHECK(typeof(identifier_order)='integer' AND identifier_order>=0),
  identifier_type TEXT NOT NULL CHECK(length(trim(identifier_type))>0),
  identifier_value TEXT NOT NULL CHECK(length(trim(identifier_value))>0),
  PRIMARY KEY(ref_id,attempt_order,identifier_family,identifier_order),
  UNIQUE(ref_id,attempt_order,identifier_family,identifier_type),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_matched_authors (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  author_order INTEGER NOT NULL CHECK(typeof(author_order)='integer' AND author_order>=0),
  author TEXT NOT NULL CHECK(length(trim(author))>0),
  PRIMARY KEY(ref_id,attempt_order,author_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_oa_license_urls (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  license_order INTEGER NOT NULL CHECK(typeof(license_order)='integer' AND license_order>=0),
  license_url TEXT NOT NULL CHECK(length(trim(license_url))>0),
  PRIMARY KEY(ref_id,attempt_order,license_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_fulltext_availability (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(length(trim(status))>0),
  scope TEXT NOT NULL CHECK(length(trim(scope))>0),
  observed_by TEXT NOT NULL CHECK(length(trim(observed_by))>0),
  reason TEXT, reason_present INTEGER NOT NULL CHECK(reason_present IN (0,1)),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE,
  CHECK(reason_present=1 OR reason IS NULL)
);
CREATE TABLE IF NOT EXISTS resolve_attempt_identity_searches (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  resolver TEXT NOT NULL, query_contract TEXT NOT NULL,
  completion TEXT NOT NULL CHECK(completion IN ('complete','incomplete')),
  outcome TEXT NOT NULL CHECK(outcome IN ('no_compatible_identity','candidate_incompatible','compatible_identity','inconclusive')),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_resolved_identifiers (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  identifier_type TEXT NOT NULL CHECK(length(trim(identifier_type))>0),
  identifier_value TEXT NOT NULL CHECK(length(trim(identifier_value))>0),
  validated_via TEXT NOT NULL CHECK(length(trim(validated_via))>0),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_trial_registrations (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  registry TEXT NOT NULL CHECK(length(trim(registry))>0),
  registration_id TEXT NOT NULL CHECK(length(trim(registration_id))>0),
  status TEXT,
  url TEXT,
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_metadata_matches (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  metadata_kind TEXT NOT NULL CHECK(metadata_kind IN ('minimal','ordinary')),
  publisher_native_uri_proof_json TEXT,
  registered_coordinate_certificate_json TEXT,
  generic_article_locator_certificate_json TEXT,
  titleless_native_venue_composition_json TEXT,
  score REAL, score_present INTEGER NOT NULL CHECK(score_present IN (0,1)),
  title_overlap REAL, title_overlap_present INTEGER NOT NULL CHECK(title_overlap_present IN (0,1)),
  author_match INTEGER, author_match_present INTEGER NOT NULL CHECK(author_match_present IN (0,1)),
  cited_first_author TEXT, cited_first_author_present INTEGER NOT NULL CHECK(cited_first_author_present IN (0,1)),
  matched_first_author TEXT, matched_first_author_present INTEGER NOT NULL CHECK(matched_first_author_present IN (0,1)),
  year_match INTEGER, year_match_present INTEGER NOT NULL CHECK(year_match_present IN (0,1)),
  matched_year INTEGER, matched_year_present INTEGER NOT NULL CHECK(matched_year_present IN (0,1)),
  venue_overlap REAL, venue_overlap_present INTEGER NOT NULL CHECK(venue_overlap_present IN (0,1)),
  matched_venue TEXT, matched_venue_present INTEGER NOT NULL CHECK(matched_venue_present IN (0,1)),
  year_mismatch_plausible INTEGER, year_mismatch_plausible_present INTEGER NOT NULL CHECK(year_mismatch_plausible_present IN (0,1)),
  ordinal_conflict INTEGER, ordinal_conflict_present INTEGER NOT NULL CHECK(ordinal_conflict_present IN (0,1)),
  author_conflict INTEGER, author_conflict_present INTEGER NOT NULL CHECK(author_conflict_present IN (0,1)),
  year_conflict INTEGER, year_conflict_present INTEGER NOT NULL CHECK(year_conflict_present IN (0,1)),
  venue_conflict INTEGER, venue_conflict_present INTEGER NOT NULL CHECK(venue_conflict_present IN (0,1)),
  metadata_conflict INTEGER, metadata_conflict_present INTEGER NOT NULL CHECK(metadata_conflict_present IN (0,1)),
  cited_ordinals_present INTEGER NOT NULL CHECK(cited_ordinals_present IN (0,1)),
  cited_ordinals_count INTEGER NOT NULL CHECK(cited_ordinals_count>=0),
  matched_ordinals_present INTEGER NOT NULL CHECK(matched_ordinals_present IN (0,1)),
  matched_ordinals_count INTEGER NOT NULL CHECK(matched_ordinals_count>=0),
  hard_conflicts_present INTEGER NOT NULL CHECK(hard_conflicts_present IN (0,1)),
  hard_conflicts_count INTEGER NOT NULL CHECK(hard_conflicts_count>=0),
  coordinate_comparisons_present INTEGER NOT NULL CHECK(coordinate_comparisons_present IN (0,1)),
  coordinate_comparisons_count INTEGER NOT NULL CHECK(coordinate_comparisons_count>=0),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE,
  CHECK((coordinate_comparisons_present=0 AND coordinate_comparisons_count=0) OR
        (coordinate_comparisons_present=1 AND coordinate_comparisons_count>0))
);

CREATE TABLE IF NOT EXISTS resolve_attempt_metadata_coordinate_comparisons (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  comparison_order INTEGER NOT NULL CHECK(typeof(comparison_order)='integer' AND comparison_order>=0),
  coordinate_kind TEXT NOT NULL CHECK(coordinate_kind IN ('container','volume','issue','article_page_range','chapter_page_range','elocator','article_number','article_locator')),
  cited_value TEXT NOT NULL CHECK(length(trim(cited_value))>0),
  matched_value TEXT,
  status TEXT NOT NULL CHECK(status IN ('match','mismatch','inconclusive')),
  PRIMARY KEY(ref_id,attempt_order,comparison_order),
  UNIQUE(ref_id,attempt_order,coordinate_kind),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_metadata_matches(ref_id,attempt_order) ON DELETE CASCADE,
  CHECK((status='inconclusive' AND (matched_value IS NULL OR length(trim(matched_value))>0)) OR
        (status IN ('match','mismatch') AND matched_value IS NOT NULL AND length(trim(matched_value))>0))
);

CREATE TABLE IF NOT EXISTS resolve_attempt_metadata_hard_conflicts (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  conflict_order INTEGER NOT NULL CHECK(typeof(conflict_order)='integer' AND conflict_order>=0),
  conflict TEXT NOT NULL CHECK(conflict IN ('author','year','venue')),
  PRIMARY KEY(ref_id,attempt_order,conflict_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_metadata_matches(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_metadata_ordinals (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  ordinal_kind TEXT NOT NULL CHECK(ordinal_kind IN ('cited','matched')),
  ordinal_order INTEGER NOT NULL CHECK(typeof(ordinal_order)='integer' AND ordinal_order>=0),
  ordinal_value INTEGER NOT NULL CHECK(typeof(ordinal_value)='integer'),
  PRIMARY KEY(ref_id,attempt_order,ordinal_kind,ordinal_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_metadata_matches(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_fulltext_links (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  link_order INTEGER NOT NULL CHECK(typeof(link_order)='integer' AND link_order>=0),
  url TEXT NOT NULL CHECK(length(trim(url))>0),
  {_LINK_TEXT_DDL},
  identity_context_present INTEGER NOT NULL CHECK(identity_context_present IN (0,1)),
  PRIMARY KEY(ref_id,attempt_order,link_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_provider_details(ref_id,attempt_order) ON DELETE CASCADE,
  {_LINK_TEXT_CHECKS}
);

CREATE TABLE IF NOT EXISTS resolve_attempt_link_contexts (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL, link_order INTEGER NOT NULL,
  title TEXT, title_present INTEGER NOT NULL CHECK(title_present IN (0,1)),
  year_kind TEXT NOT NULL CHECK(year_kind IN ('absent','null','integer','text')),
  year_integer INTEGER, year_text TEXT,
  provider TEXT, provider_present INTEGER NOT NULL CHECK(provider_present IN (0,1)),
 provider_record_id TEXT, provider_record_id_present INTEGER NOT NULL CHECK(provider_record_id_present IN (0,1)),
 first_author TEXT, first_author_present INTEGER NOT NULL CHECK(first_author_present IN (0,1)),
  source_confidence_kind TEXT NOT NULL CHECK(source_confidence_kind IN ('absent','null','integer','real')),
  source_confidence_integer INTEGER, source_confidence_real REAL,
 canonical_host INTEGER, canonical_host_present INTEGER NOT NULL CHECK(canonical_host_present IN (0,1)),
 canonical_url TEXT, canonical_url_present INTEGER NOT NULL CHECK(canonical_url_present IN (0,1)),
 landing_page_url TEXT, landing_page_url_present INTEGER NOT NULL CHECK(landing_page_url_present IN (0,1)),
 expected_document_title TEXT, expected_document_title_present INTEGER NOT NULL CHECK(expected_document_title_present IN (0,1)),
 official INTEGER, official_present INTEGER NOT NULL CHECK(official_present IN (0,1)),
 official_document_relation TEXT, official_document_relation_present INTEGER NOT NULL CHECK(official_document_relation_present IN (0,1)),
  authors_present INTEGER NOT NULL CHECK(authors_present IN (0,1)),
  authors_count INTEGER NOT NULL CHECK(authors_count>=0),
  identifiers_present INTEGER NOT NULL CHECK(identifiers_present IN (0,1)),
  identifiers_count INTEGER NOT NULL CHECK(identifiers_count>=0),
  PRIMARY KEY(ref_id,attempt_order,link_order),
  FOREIGN KEY(ref_id,attempt_order,link_order) REFERENCES resolve_attempt_fulltext_links(ref_id,attempt_order,link_order) ON DELETE CASCADE,
  CHECK(title_present=1 OR title IS NULL),
  CHECK(provider_present=1 OR provider IS NULL),
 CHECK(provider_record_id_present=1 OR provider_record_id IS NULL),
 CHECK(first_author_present=1 OR first_author IS NULL),
  CHECK((year_kind IN ('absent','null') AND year_integer IS NULL AND year_text IS NULL) OR
        (year_kind='integer' AND typeof(year_integer)='integer' AND year_text IS NULL) OR
        (year_kind='text' AND year_integer IS NULL AND typeof(year_text)='text')),
  CHECK((source_confidence_kind IN ('absent','null') AND source_confidence_integer IS NULL AND source_confidence_real IS NULL) OR
        (source_confidence_kind='integer' AND typeof(source_confidence_integer)='integer' AND source_confidence_real IS NULL) OR
        (source_confidence_kind='real' AND source_confidence_integer IS NULL AND typeof(source_confidence_real)='real')),
 CHECK((canonical_host_present=0 AND canonical_host IS NULL) OR
        (canonical_host_present=1 AND canonical_host IN (0,1))),
 CHECK(canonical_url_present=1 OR canonical_url IS NULL),
 CHECK(landing_page_url_present=1 OR landing_page_url IS NULL),
 CHECK(expected_document_title_present=1 OR expected_document_title IS NULL),
 CHECK((official_present=0 AND official IS NULL) OR (official_present=1 AND official IN (0,1))),
 CHECK(official_document_relation_present=1 OR official_document_relation IS NULL),
 CHECK(authors_present=1 OR authors_count=0),
  CHECK(identifiers_present=1 OR identifiers_count=0)
);

CREATE TABLE IF NOT EXISTS resolve_attempt_link_context_authors (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL, link_order INTEGER NOT NULL,
  author_order INTEGER NOT NULL CHECK(typeof(author_order)='integer' AND author_order>=0),
  author TEXT NOT NULL CHECK(length(trim(author))>0),
  PRIMARY KEY(ref_id,attempt_order,link_order,author_order),
  FOREIGN KEY(ref_id,attempt_order,link_order) REFERENCES resolve_attempt_link_contexts(ref_id,attempt_order,link_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_link_context_identifiers (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL, link_order INTEGER NOT NULL,
  identifier_order INTEGER NOT NULL CHECK(typeof(identifier_order)='integer' AND identifier_order>=0),
  identifier_type TEXT NOT NULL CHECK(length(trim(identifier_type))>0),
  identifier_value TEXT NOT NULL CHECK(length(trim(identifier_value))>0),
  PRIMARY KEY(ref_id,attempt_order,link_order,identifier_order),
  UNIQUE(ref_id,attempt_order,link_order,identifier_type),
  FOREIGN KEY(ref_id,attempt_order,link_order) REFERENCES resolve_attempt_link_contexts(ref_id,attempt_order,link_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_exceptions (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  exception_type TEXT NOT NULL,
  message TEXT NOT NULL,
  traceback TEXT NOT NULL,
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempts(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_fetch_repairs (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  method TEXT, pdf_url TEXT, content_version TEXT, corroborate_signal TEXT,
  corroborate_score_kind TEXT NOT NULL CHECK(corroborate_score_kind IN ('null','integer','real')),
  corroborate_score_integer INTEGER,
  corroborate_score_real REAL,
  direct_attempt_count INTEGER NOT NULL CHECK(direct_attempt_count>=0),
  execution_attempt_count INTEGER NOT NULL CHECK(execution_attempt_count>=0),
  PRIMARY KEY(ref_id,attempt_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempts(ref_id,attempt_order) ON DELETE CASCADE,
  CHECK((corroborate_score_kind='null' AND corroborate_score_integer IS NULL AND corroborate_score_real IS NULL) OR
        (corroborate_score_kind='integer' AND typeof(corroborate_score_integer)='integer' AND corroborate_score_real IS NULL) OR
        (corroborate_score_kind='real' AND corroborate_score_integer IS NULL AND typeof(corroborate_score_real)='real'))
);

CREATE TABLE IF NOT EXISTS resolve_attempt_fetch_repair_direct (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  direct_order INTEGER NOT NULL CHECK(typeof(direct_order)='integer' AND direct_order>=0),
  method TEXT NOT NULL CHECK(length(trim(method))>0),
  source_ref TEXT,
  outcome TEXT NOT NULL CHECK(length(trim(outcome))>0),
  reason TEXT,
  PRIMARY KEY(ref_id,attempt_order,direct_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_fetch_repairs(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resolve_attempt_fetch_repair_execution (
  ref_id TEXT NOT NULL, attempt_order INTEGER NOT NULL,
  execution_order INTEGER NOT NULL CHECK(typeof(execution_order)='integer' AND execution_order>=0),
  method TEXT, url TEXT, final_url TEXT, kind TEXT, outcome TEXT, reason TEXT,
  status INTEGER, content_type TEXT,
  PRIMARY KEY(ref_id,attempt_order,execution_order),
  FOREIGN KEY(ref_id,attempt_order) REFERENCES resolve_attempt_fetch_repairs(ref_id,attempt_order) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS resolve_attempt_state_no_update
BEFORE UPDATE ON resolve_attempt_states BEGIN SELECT RAISE(ABORT,'resolve attempts are replace-only'); END;
CREATE TRIGGER IF NOT EXISTS resolve_attempt_insert_guard
BEFORE INSERT ON resolve_attempts
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_attempt_states
  WHERE ref_id=NEW.ref_id AND state='produced' AND NEW.attempt_order<attempt_count
)
BEGIN SELECT RAISE(ABORT,'resolve attempt storage is inconsistent'); END;
CREATE TRIGGER IF NOT EXISTS resolve_attempt_provider_insert_guard
BEFORE INSERT ON resolve_attempt_provider_details
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_attempts
  WHERE ref_id=NEW.ref_id AND attempt_order=NEW.attempt_order
    AND attempt_kind IN ('provider_result','identifier_validation','identifier_fallback','enrichment')
)
BEGIN SELECT RAISE(ABORT,'resolve attempt provider kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_attempt_exception_insert_guard
BEFORE INSERT ON resolve_attempt_exceptions
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_attempts
  WHERE ref_id=NEW.ref_id AND attempt_order=NEW.attempt_order
    AND attempt_kind IN ('resolver_exception','repair_exception')
)
BEGIN SELECT RAISE(ABORT,'resolve attempt exception kind mismatch'); END;
CREATE TRIGGER IF NOT EXISTS resolve_attempt_fetch_repair_insert_guard
BEFORE INSERT ON resolve_attempt_fetch_repairs
WHEN NOT EXISTS (
  SELECT 1 FROM resolve_attempts
  WHERE ref_id=NEW.ref_id AND attempt_order=NEW.attempt_order
    AND attempt_kind='fetch_repair'
)
BEGIN SELECT RAISE(ABORT,'resolve attempt fetch-repair kind mismatch'); END;
"""

for _table in ATTEMPT_TABLES[1:]:
    ATTEMPT_DDL += (
        f"CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table} "
        "BEGIN SELECT RAISE(ABORT,'resolve attempts are replace-only'); END;\n"
    )

ATTEMPT_TRIGGERS = (
    "resolve_attempt_state_no_update",
    "resolve_attempt_insert_guard",
    "resolve_attempt_provider_insert_guard",
    "resolve_attempt_exception_insert_guard",
    "resolve_attempt_fetch_repair_insert_guard",
    *(f"{table}_no_update" for table in ATTEMPT_TABLES[1:]),
)


def _text(value: Any, label: str, *, nullable: bool = False, nonempty: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or "\0" in value or (nonempty and not value.strip()):
        suffix = " or null" if nullable else ""
        raise ValueError(f"resolve attempt {label} must be text{suffix}")
    return value


def _finite_number(value: Any, label: str, *, nullable: bool) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"resolve attempt {label} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"resolve attempt {label} must be a finite number")
    return value


def _text_list(value: Any, label: str) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"resolve attempt {label} must be a list")
    for item in value:
        _text(item, f"{label} item", nonempty=True)
    return list(value)


def _identifier_map(value: Any, label: str) -> JsonDict:
    if type(value) is not dict:
        raise ValueError(f"resolve attempt {label} must be an object")
    out: JsonDict = {}
    for key, item in value.items():
        _text(key, f"{label} key", nonempty=True)
        _text(item, f"{label} value", nonempty=True)
        out[key] = item
    return out


def _fraction(value: Any, label: str, *, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"resolve attempt {label} must be a finite float in [0,1]")
    return value


def _validate_metadata_match(value: Any) -> JsonDict:
    if type(value) is not dict:
        raise ValueError("resolve attempt metadata_match must be an object")
    keys = set(value)
    if keys == METADATA_MINIMAL_FIELDS:
        _fraction(value["title_overlap"], "metadata_match.title_overlap")
        if value["matched_year"] is not None and type(value["matched_year"]) is not int:
            raise ValueError("resolve attempt metadata_match.matched_year must be integer or null")
        return dict(value)

    allowed = (
        METADATA_ORDINARY_FIELDS
        | METADATA_ORDINAL_FIELDS
        | METADATA_CONFLICT_FIELDS
        | {
            "year_mismatch_plausible",
            METADATA_COORDINATE_COMPARISONS_FIELD,
            PUBLISHER_NATIVE_URI_PROOF_FIELD,
            REGISTERED_COORDINATE_CERTIFICATE_FIELD,
            GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD,
            TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD,
        }
    )
    if not METADATA_ORDINARY_FIELDS <= keys or keys - allowed:
        raise ValueError("resolve attempt metadata_match has an invalid field set")
    _fraction(value["score"], "metadata_match.score", nullable=False)
    _fraction(value["title_overlap"], "metadata_match.title_overlap")
    _fraction(value["venue_overlap"], "metadata_match.venue_overlap")
    for key in ("author_match", "year_match"):
        if type(value[key]) is not bool:
            raise ValueError(f"resolve attempt metadata_match.{key} must be boolean")
    for key in ("cited_first_author", "matched_first_author", "matched_venue"):
        _text(value[key], f"metadata_match.{key}", nullable=True)
    if value["matched_year"] is not None and type(value["matched_year"]) is not int:
        raise ValueError("resolve attempt metadata_match.matched_year must be integer or null")
    if PUBLISHER_NATIVE_URI_PROOF_FIELD in value:
        _validate_publisher_native_uri_proof(value[PUBLISHER_NATIVE_URI_PROOF_FIELD])
    if REGISTERED_COORDINATE_CERTIFICATE_FIELD in value:
        _validate_registered_coordinate_certificate(
            value[REGISTERED_COORDINATE_CERTIFICATE_FIELD],
        )
    generic_certificate = None
    if GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD in value:
        generic_certificate = _validate_generic_article_locator_certificate(
            value[GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD],
        )
    if TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD in value:
        if generic_certificate is None:
            raise _generic_locator_certificate_error(
                "native venue companion has no generic article locator certificate",
            )
        _validate_generic_locator_venue_companion(
            value[TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD], generic_certificate,
        )
    elif (
        generic_certificate is not None
        and generic_certificate["native_venue"].get("mode") == "ordered_primary_short"
    ):
        raise _generic_locator_certificate_error("native venue companion is required")

    ordinal_keys = keys & METADATA_ORDINAL_FIELDS
    if ordinal_keys:
        if ordinal_keys != METADATA_ORDINAL_FIELDS or value["ordinal_conflict"] is not True:
            raise ValueError("resolve attempt metadata_match ordinal bundle is invalid")
        for key in ("cited_ordinals", "matched_ordinals"):
            items = value[key]
            if (
                type(items) is not list
                or not items
                or any(type(item) is not int for item in items)
                or items != sorted(set(items))
            ):
                raise ValueError(f"resolve attempt metadata_match.{key} is invalid")
    if "year_mismatch_plausible" in value and value["year_mismatch_plausible"] is not True:
        raise ValueError("resolve attempt metadata_match year mismatch flag is invalid")

    comparisons = value.get(METADATA_COORDINATE_COMPARISONS_FIELD)
    if comparisons is not None:
        if type(comparisons) is not list or not comparisons:
            raise ValueError("resolve attempt metadata coordinate comparisons are invalid")
        seen_kinds: set[str] = set()
        for comparison in comparisons:
            if type(comparison) is not dict or set(comparison) != {
                "kind", "cited_value", "matched_value", "status",
            }:
                raise ValueError("resolve attempt metadata coordinate comparison has an invalid field set")
            kind = comparison["kind"]
            if kind not in METADATA_COORDINATE_KINDS or kind in seen_kinds:
                raise ValueError("resolve attempt metadata coordinate comparison kind is invalid")
            seen_kinds.add(kind)
            _text(comparison["cited_value"], "metadata coordinate cited value", nonempty=True)
            matched_value = comparison["matched_value"]
            _text(matched_value, "metadata coordinate matched value", nullable=True)
            status = comparison["status"]
            if status not in METADATA_COORDINATE_STATUSES:
                raise ValueError("resolve attempt metadata coordinate comparison status is invalid")
            if status in {"match", "mismatch"} and matched_value is None:
                raise ValueError("resolve attempt metadata coordinate comparison values are inconsistent")

    conflict_keys = keys & METADATA_CONFLICT_FIELDS
    if conflict_keys:
        if conflict_keys != METADATA_CONFLICT_FIELDS or value["metadata_conflict"] is not True:
            raise ValueError("resolve attempt metadata_match conflict bundle is invalid")
        conflicts = value["hard_conflicts"]
        if (
            type(conflicts) is not list
            or not conflicts
            or any(item not in {"author", "year", "venue"} for item in conflicts)
            or len(conflicts) != len(set(conflicts))
        ):
            raise ValueError("resolve attempt metadata_match hard conflicts are invalid")
        for key, label in (
            ("author_conflict", "author"),
            ("year_conflict", "year"),
            ("venue_conflict", "venue"),
        ):
            if type(value[key]) is not bool or value[key] != (label in conflicts):
                raise ValueError("resolve attempt metadata_match conflicts are inconsistent")
    return copy.deepcopy(value)


def _validate_publisher_native_uri_proof(value: Any) -> JsonDict:
    if type(value) is not dict or set(value) != PUBLISHER_NATIVE_URI_PROOF_FIELDS:
        raise ValueError("resolve attempt publisher native URI proof has an invalid field set")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("resolve attempt publisher native URI proof version is invalid")
    for key in (
        "selected_doi", "source_uri", "source_pii", "crossref_request_url",
        "publisher_request_url", "publisher_final_url",
    ):
        _text(value[key], f"publisher native URI proof {key}", nonempty=True)
    source_digest = value["source_entry_sha256"]
    if type(source_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
        raise ValueError("resolve attempt publisher native URI proof source_entry_sha256 is invalid")
    for key in ("crossref_http_status", "publisher_http_status"):
        if type(value[key]) is not int:
            raise ValueError(f"resolve attempt publisher native URI proof {key} must be integer")
    for body_key, digest_key in (
        ("crossref_response_body", "crossref_response_sha256"),
        ("publisher_response_body", "publisher_response_sha256"),
    ):
        body = _text(value[body_key], f"publisher native URI proof {body_key}", nonempty=True)
        digest = value[digest_key]
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"resolve attempt publisher native URI proof {digest_key} is invalid")
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
            raise ValueError(f"resolve attempt publisher native URI proof {digest_key} does not match its body")
    return copy.deepcopy(value)


def _registered_coordinate_certificate_error(detail: str) -> ValueError:
    return ValueError(f"resolve attempt registered coordinate certificate {detail} is invalid")


def _validate_registered_coordinate_certificate(value: Any) -> JsonDict:
    required = {
        "rule_version", "registry", "registry_version", "record_id",
        "snapshot_sha256", "candidate_issns",
    }
    if type(value) is not dict or set(value) != required:
        raise _registered_coordinate_certificate_error("field set")
    if (
        _text(value["rule_version"], "registered coordinate certificate rule_version", nonempty=True)
        != REGISTERED_COORDINATE_CERTIFICATE_RULE_VERSION
        or _text(value["registry"], "registered coordinate certificate registry", nonempty=True)
        != "nlm-pubmed"
    ):
        raise _registered_coordinate_certificate_error("rule or registry")
    _text(value["registry_version"], "registered coordinate certificate registry_version", nonempty=True)
    _text(value["record_id"], "registered coordinate certificate record_id", nonempty=True)
    snapshot_sha256 = _text(
        value["snapshot_sha256"],
        "registered coordinate certificate snapshot_sha256",
        nonempty=True,
    )
    if re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is None:
        raise _registered_coordinate_certificate_error("snapshot_sha256")

    candidate_issns = value["candidate_issns"]
    if type(candidate_issns) is not list or not candidate_issns:
        raise _registered_coordinate_certificate_error("candidate_issns")
    for issn in candidate_issns:
        _text(issn, "registered coordinate certificate ISSN", nonempty=True)
        if re.fullmatch(r"[0-9]{4}-[0-9]{3}[0-9X]", issn) is None:
            raise _registered_coordinate_certificate_error("ISSN format")
        compact = issn.replace("-", "")
        weighted_sum = sum(
            int(char) * weight
            for char, weight in zip(compact[:7], range(8, 1, -1))
        )
        remainder = (11 - weighted_sum % 11) % 11
        expected_check_digit = "X" if remainder == 10 else str(remainder)
        if compact[7] != expected_check_digit:
            raise _registered_coordinate_certificate_error("ISSN check digit")
    if candidate_issns != sorted(set(candidate_issns)):
        raise _registered_coordinate_certificate_error("ISSN ordering or duplicates")
    return copy.deepcopy(value)


def _generic_locator_certificate_error(detail: str) -> ValueError:
    return ValueError(f"resolve attempt generic article locator certificate {detail} is invalid")


def _generic_locator_digest(value: Any, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise _generic_locator_certificate_error(label)
    return value


def _generic_locator_json_sha256(value: Any, label: str) -> str:
    try:
        serialized = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _generic_locator_certificate_error(label) from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _generic_locator_json_value(value: Any) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_generic_locator_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _generic_locator_json_value(item)
            for key, item in value.items()
        )
    return False


def _generic_locator_span(value: Any, label: str, *, allow_empty: bool = False) -> list[int]:
    if (
        type(value) is not list
        or len(value) != 2
        or any(type(item) is not int for item in value)
        or value[0] < 0
        or (value[1] < value[0] if allow_empty else value[1] <= value[0])
    ):
        raise _generic_locator_certificate_error(label)
    return list(value)


def _validate_generic_native_record_snapshot(
    value: Any,
    label: str,
    *,
    require_certificate_record: bool = False,
    registered_scope: bool = False,
) -> JsonDict:
    if type(value) is not dict or not _generic_locator_json_value(value):
        raise _generic_locator_certificate_error(label)
    allowed = GENERIC_NATIVE_RECORD_FIELDS | ({"ISSN"} if registered_scope else set())
    if set(value) - allowed:
        raise _generic_locator_certificate_error(label)
    if not require_certificate_record:
        return copy.deepcopy(value)
    for key in ("DOI", "type", "volume"):
        if key in value:
            _text(value[key], f"{label}.{key}", nonempty=True)
    for key in ("title", "container-title"):
        if key in value:
            _text_list(value[key], f"{label}.{key}")
    for key in ("subtitle", "short-container-title"):
        if key in value and value[key] is not None:
            _text_list(value[key], f"{label}.{key}")
    for key in ("published-print", "published-online", "published", "issued"):
        if key in value and value[key] is not None and type(value[key]) is not dict:
            raise _generic_locator_certificate_error(f"{label}.{key}")
    for key in ("page", "article-number"):
        if key in value and value[key] is not None:
            _text(value[key], f"{label}.{key}")
    if registered_scope and "ISSN" in value:
        _text_list(value["ISSN"], f"{label}.ISSN")
    required = {"DOI", "type", "title", "author", "container-title", "volume"}
    if not required <= set(value) or value.get("type") != "journal-article":
        raise _generic_locator_certificate_error(label)
    if not any(type(value.get(key)) is dict for key in (
        "published-print", "published-online", "published", "issued",
    )):
        raise _generic_locator_certificate_error(f"{label}.publication_date")
    title = _text_list(value["title"], f"{label}.title")
    container = _text_list(value["container-title"], f"{label}.container-title")
    authors = value["author"]
    if (
        not title or not title[0].strip()
        or len(container) != 1 or not container[0].strip()
        or type(authors) is not list or not authors
        or type(authors[0]) is not dict
        or not _generic_locator_json_value(authors)
    ):
        raise _generic_locator_certificate_error(label)
    first_author = authors[0].get("family") or authors[0].get("name")
    _text(first_author, f"{label}.author.family", nonempty=True)
    _text(value["volume"], f"{label}.volume", nonempty=True)
    if registered_scope:
        if "ISSN" not in value or not value["ISSN"]:
            raise _generic_locator_certificate_error(f"{label}.ISSN")
    return copy.deepcopy(value)


def _validate_generic_locator_source(value: Any, raw_digest: str) -> JsonDict:
    required = {
        "author", "year", "container", "volume", "issue", "locator",
        "spans", "title_span", "source_titleless",
    }
    if type(value) is not dict or set(value) != required:
        raise _generic_locator_certificate_error("source field set")
    for key in ("author", "container", "volume", "locator"):
        _text(value[key], f"source.{key}", nonempty=True)
    _text(value["issue"], "source.issue", nullable=True)
    if (
        type(value["year"]) is not int
        or re.fullmatch(r"[0-9]{1,4}", value["volume"]) is None
        or re.fullmatch(r"[0-9]+", value["locator"]) is None
        or (value["issue"] is not None and re.fullmatch(r"[0-9]+", value["issue"]) is None)
        or type(value["source_titleless"]) is not bool
    ):
        raise _generic_locator_certificate_error("source")
    spans = value["spans"]
    expected_span_keys = {"container", "volume", "article_locator"}
    if value["issue"] is not None:
        expected_span_keys.add("issue")
    if type(spans) is not dict or set(spans) != expected_span_keys:
        raise _generic_locator_certificate_error("source.spans field set")
    for key, span in spans.items():
        _generic_locator_span(span, f"source.spans.{key}")
    _generic_locator_span(value["title_span"], "source.title_span", allow_empty=True)
    return copy.deepcopy(value)


def _validate_generic_locator_native_venue(
    value: Any, snapshot: JsonDict, raw_digest: str,
) -> JsonDict:
    if type(value) is not dict:
        raise _generic_locator_certificate_error("native_venue")
    primary_values = snapshot.get("container-title")
    short_values = snapshot.get("short-container-title")
    expected_short = (
        short_values[0].strip()
        if type(short_values) is list and len(short_values) == 1
        and type(short_values[0]) is str else None
    )
    _text(value.get("primary"), "native_venue.primary", nonempty=True)
    _text(value.get("short"), "native_venue.short", nullable=True)
    if (
        type(primary_values) is not list or len(primary_values) != 1
        or value["primary"] != primary_values[0].strip()
        or value["short"] != expected_short
    ):
        raise _generic_locator_certificate_error("native_venue binding")
    mode = value.get("mode")
    if mode == "literal":
        if set(value) != {"primary", "short", "mode", "alias"}:
            raise _generic_locator_certificate_error("native_venue literal field set")
        _text(value["alias"], "native_venue.alias")
        if value["alias"] and not value["short"]:
            raise _generic_locator_certificate_error("native_venue alias")
    elif mode == "ordered_primary_short":
        required = {
            "primary", "short", "mode", "rule", "raw_entry_sha256",
            "native_primary", "native_short",
        }
        if set(value) != required:
            raise _generic_locator_certificate_error("native_venue composition field set")
        if (
            value["rule"] != "ordered_native_primary_short_tokens_v1"
            or value["raw_entry_sha256"] != raw_digest
            or value["native_primary"] != value["primary"]
            or value["native_short"] != value["short"]
            or not value["short"]
        ):
            raise _generic_locator_certificate_error("native_venue composition")
    else:
        raise _generic_locator_certificate_error("native_venue mode")
    return copy.deepcopy(value)


def _registered_generic_locator_source_scope_query(source: JsonDict) -> JsonDict:
    return {
        "kind": "generic",
        "author": source["author"],
        "year": source["year"],
        "volume": source["volume"],
        "locator": source["locator"],
        "locator_kind": "article_locator",
        "bibliographic": "",
    }


def _generic_scope_record_projection(value: JsonDict) -> JsonDict:
    return {
        key: item for key, item in value.items()
        if key in GENERIC_NATIVE_RECORD_FIELDS
        and not (key == "subtitle" and item == [])
        and not (key == "subtitle" and item is None)
    }


def _validate_registered_generic_locator_search(
    value: Any,
    source: JsonDict,
    raw_digest: str,
    native_snapshot: JsonDict,
) -> JsonDict:
    required = {
        "rule_version", "source_scope", "query", "scopes", "selected_doi",
        "selected_native_record_snapshot", "selected_native_record_sha256",
        "direct_native_record_snapshot", "direct_native_record_sha256",
    }
    if type(value) is not dict or set(value) != required:
        raise _generic_locator_certificate_error("registered search field set")
    expected_query = _registered_generic_locator_source_scope_query(source)
    query = value["query"]
    if (
        value["rule_version"] != GENERIC_ARTICLE_LOCATOR_REGISTERED_SCOPE_RULE_VERSION
        or type(query) is not dict
        or set(query) != set(expected_query)
        or type(query.get("year")) is not int
        or any(type(query.get(key)) is not str for key in expected_query if key != "year")
        or query != expected_query
    ):
        raise _generic_locator_certificate_error("registered search query")

    scope = value["source_scope"]
    scope_fields = {
        "status", "registry", "registry_version", "record_id", "cited_venue",
        "canonical_title", "matched_alias", "match_basis", "snapshot_sha256",
        "issns", "source", "venue_witness",
    }
    if type(scope) is not dict or set(scope) != scope_fields:
        raise _generic_locator_certificate_error("registered source scope field set")
    for key in (
        "registry_version", "record_id", "cited_venue", "canonical_title",
        "matched_alias",
    ):
        _text(scope[key], f"registered source scope.{key}", nonempty=True)
    if (
        scope["status"] != "recognized"
        or scope["registry"] != "nlm-pubmed"
        or scope["match_basis"] not in {
            "canonical_title", "registered_alias", "registered_title_alias_composition",
        }
    ):
        raise _generic_locator_certificate_error("registered source scope identity")
    _generic_locator_digest(scope["snapshot_sha256"], "registered source scope snapshot_sha256")
    issns = _text_list(scope["issns"], "registered source scope.issns")
    if not issns or len(issns) != len(set(issns)):
        raise _generic_locator_certificate_error("registered source scope issns")
    scope_source = scope["source"]
    if type(scope_source) is not dict or set(scope_source) != {
        "raw_entry_sha256", "container_span", "container_value",
    }:
        raise _generic_locator_certificate_error("registered source scope source")
    if _generic_locator_digest(
        scope_source["raw_entry_sha256"], "registered source scope raw_entry_sha256",
    ) != raw_digest:
        raise _generic_locator_certificate_error("registered source scope raw_entry_sha256")
    _generic_locator_span(scope_source["container_span"], "registered source scope container_span")
    _text(scope_source["container_value"], "registered source scope container_value", nonempty=True)
    venue_witness = scope["venue_witness"]
    if type(venue_witness) is not dict or set(venue_witness) != {
        "mode", "canonical_title", "registered_alias",
    }:
        raise _generic_locator_certificate_error("registered source scope venue witness")
    if venue_witness["mode"] not in {
        "exact_registered_name", "ordered_canonical_registered_alias_tokens_v1",
    }:
        raise _generic_locator_certificate_error("registered source scope venue witness mode")
    for key in ("canonical_title", "registered_alias"):
        _text(venue_witness[key], f"registered source scope venue witness.{key}", nonempty=True)
    if venue_witness["canonical_title"] != scope["canonical_title"]:
        raise _generic_locator_certificate_error("registered source scope venue witness binding")

    scopes = value["scopes"]
    if type(scopes) is not list or len(scopes) != len(issns):
        raise _generic_locator_certificate_error("registered search scopes")
    for expected_issn, item in zip(issns, scopes):
        if type(item) is not dict or set(item) != {
            "issn", "request_url", "complete", "result_total", "records", "records_sha256",
        }:
            raise _generic_locator_certificate_error("registered search scope field set")
        _text(item["issn"], "registered search scope issn", nonempty=True)
        if item["issn"] != expected_issn or item["complete"] is not True:
            raise _generic_locator_certificate_error("registered search scope identity")
        expected_url = "https://api.crossref.org/journals/" + urllib.parse.quote(
            expected_issn, safe="",
        ) + "/works?" + urllib.parse.urlencode({
            "rows": "1000",
            "query.author": source["author"],
            "filter": (
                f"from-pub-date:{source['year']}-01-01,"
                f"until-pub-date:{source['year']}-12-31"
            ),
        })
        if item["request_url"] != expected_url:
            raise _generic_locator_certificate_error("registered search scope request_url")
        records = item["records"]
        total = item["result_total"]
        _generic_locator_digest(item["records_sha256"], "registered search scope records_sha256")
        if (
            type(records) is not list or type(total) is not int
            or total != len(records) or total > 1000
            or _generic_locator_json_sha256(records, "registered search scope records")
            != item["records_sha256"]
        ):
            raise _generic_locator_certificate_error("registered search scope records")
        for record in records:
            _validate_generic_native_record_snapshot(
                record, "registered search record", registered_scope=True,
            )

    selected = _validate_generic_native_record_snapshot(
        value["selected_native_record_snapshot"], "registered selected record",
        require_certificate_record=True, registered_scope=True,
    )
    direct = _validate_generic_native_record_snapshot(
        value["direct_native_record_snapshot"], "registered direct record",
        require_certificate_record=True, registered_scope=True,
    )
    selected_digest = _generic_locator_digest(
        value["selected_native_record_sha256"], "selected_native_record_sha256",
    )
    direct_digest = _generic_locator_digest(
        value["direct_native_record_sha256"], "direct_native_record_sha256",
    )
    selected_doi = _text(value["selected_doi"], "registered selected_doi", nonempty=True)
    if (
        _generic_locator_json_sha256(selected, "selected native record") != selected_digest
        or _generic_locator_json_sha256(direct, "direct native record") != direct_digest
        or selected != direct
        or selected["DOI"].strip().casefold() != selected_doi.casefold()
        or _generic_scope_record_projection(selected) != native_snapshot
    ):
        raise _generic_locator_certificate_error("registered selected record binding")
    return copy.deepcopy(value)


def _validate_generic_locator_search_evidence(
    value: Any,
    source: JsonDict,
    raw_digest: str,
    native_snapshot: JsonDict,
) -> JsonDict:
    if type(value) is not dict:
        raise _generic_locator_certificate_error("search_evidence")
    if "registered_source_scope" in value or "route" in value:
        required = {"rule_version", "route", "registered_source_scope"}
        if (
            set(value) != required
            or value["rule_version"] != GENERIC_ARTICLE_LOCATOR_SEARCH_RULE_VERSION
            or value["route"] != GENERIC_ARTICLE_LOCATOR_REGISTERED_SCOPE_RULE_VERSION
        ):
            raise _generic_locator_certificate_error("registered search_evidence field set")
        _validate_registered_generic_locator_search(
            value["registered_source_scope"], source, raw_digest, native_snapshot,
        )
        return copy.deepcopy(value)

    required = {
        "rule_version", "query", "request_url", "complete", "result_total",
        "records", "records_sha256",
    }
    if set(value) != required or value["rule_version"] != GENERIC_ARTICLE_LOCATOR_SEARCH_RULE_VERSION:
        raise _generic_locator_certificate_error("search_evidence field set")
    query = value["query"]
    expected_query = {
        "bibliographic": " ".join((
            str(source["year"]), source["container"], source["volume"], source["locator"],
        )),
        "author": source["author"],
        "year": source["year"],
    }
    if (
        type(query) is not dict
        or set(query) != set(expected_query)
        or type(query.get("year")) is not int
        or any(type(query.get(key)) is not str for key in expected_query if key != "year")
        or query != expected_query
    ):
        raise _generic_locator_certificate_error("search_evidence query")
    expected_url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
        "query.bibliographic": expected_query["bibliographic"],
        "rows": "1000",
        "query.author": expected_query["author"],
        "filter": (
            f"from-pub-date:{source['year']}-01-01,"
            f"until-pub-date:{source['year']}-12-31"
        ),
    })
    if value["request_url"] != expected_url or value["complete"] is not True:
        raise _generic_locator_certificate_error("search_evidence request")
    records = value["records"]
    total = value["result_total"]
    record_digest = _generic_locator_digest(value["records_sha256"], "records_sha256")
    if (
        type(records) is not list or not records
        or type(total) is not int or total != len(records) or total > 1000
        or _generic_locator_json_sha256(records, "search_evidence records") != record_digest
    ):
        raise _generic_locator_certificate_error("search_evidence records")
    for record in records:
        _validate_generic_native_record_snapshot(record, "search record")
    if sum(record == native_snapshot for record in records) != 1:
        raise _generic_locator_certificate_error("search_evidence selected record")
    return copy.deepcopy(value)


def _validate_generic_article_locator_certificate(value: Any) -> JsonDict:
    if type(value) is not dict or set(value) - (GENERIC_CERTIFICATE_FIELDS | {"search_evidence"}) or not GENERIC_CERTIFICATE_FIELDS <= set(value):
        raise _generic_locator_certificate_error("field set")
    if value["rule_version"] != GENERIC_ARTICLE_LOCATOR_CERTIFICATE_RULE_VERSION:
        raise _generic_locator_certificate_error("rule_version")
    raw_digest = _generic_locator_digest(value["raw_entry_sha256"], "raw_entry_sha256")
    source = _validate_generic_locator_source(value["source"], raw_digest)
    native_snapshot = _validate_generic_native_record_snapshot(
        value["native_record_snapshot"], "native_record_snapshot",
        require_certificate_record=True,
    )
    snapshot_digest = _generic_locator_digest(
        value["native_record_sha256"], "native_record_sha256",
    )
    if _generic_locator_json_sha256(native_snapshot, "native_record_snapshot") != snapshot_digest:
        raise _generic_locator_certificate_error("native_record_sha256 binding")
    locator_field = value["native_locator_field"]
    if locator_field not in {"article-number", "page"}:
        raise _generic_locator_certificate_error("native_locator_field")
    locator_value = _text(value["native_locator_value"], "native_locator_value", nonempty=True)
    supplied = [
        key for key in ("article-number", "page")
        if native_snapshot.get(key) is not None and native_snapshot.get(key) != ""
    ]
    if (
        supplied != [locator_field]
        or native_snapshot.get(locator_field) != locator_value
        or (locator_field == "article-number" and locator_value != source["locator"])
    ):
        raise _generic_locator_certificate_error("native locator binding")
    if locator_field == "page":
        match = re.fullmatch(r"([0-9]+)\s*[-‐‑‒–—−]\s*([0-9]+)", locator_value)
        if (
            match is None or match.group(1) != source["locator"]
            or int(match.group(2)) < int(match.group(1))
        ):
            raise _generic_locator_certificate_error("native page range")
    _validate_generic_locator_native_venue(value["native_venue"], native_snapshot, raw_digest)
    if "search_evidence" in value:
        _validate_generic_locator_search_evidence(
            value["search_evidence"], source, raw_digest, native_snapshot,
        )
    return copy.deepcopy(value)


def _validate_generic_locator_venue_companion(
    value: Any, certificate: JsonDict,
) -> JsonDict:
    required = {"rule", "raw_entry_sha256", "native_primary", "native_short"}
    if type(value) is not dict or set(value) != required:
        raise _generic_locator_certificate_error("native venue companion field set")
    _text(value["rule"], "native venue companion.rule", nonempty=True)
    _generic_locator_digest(
        value["raw_entry_sha256"], "native venue companion.raw_entry_sha256",
    )
    _text(value["native_primary"], "native venue companion.native_primary", nonempty=True)
    _text(value["native_short"], "native venue companion.native_short", nonempty=True)

    native_venue = certificate["native_venue"]
    if (
        native_venue.get("mode") != "ordered_primary_short"
        or {key: native_venue.get(key) for key in required} != value
    ):
        raise _generic_locator_certificate_error("native venue companion binding")
    return copy.deepcopy(value)


def _validate_crossref_native_work_proof(value: Any) -> JsonDict:
    if type(value) is dict and value.get("version") == 2:
        return _validate_crossref_native_article_proof(value)
    if type(value) is not dict or set(value) != CROSSREF_NATIVE_WORK_PROOF_FIELDS:
        raise ValueError("resolve attempt Crossref native work proof has an invalid field set")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("resolve attempt Crossref native work proof version is invalid")
    requested_doi = _text(
        value["requested_doi"], "Crossref native work proof requested_doi", nonempty=True,
    )
    if re.fullmatch(r"10\.\d{4,9}/[^\s]+", requested_doi) is None:
        raise ValueError("resolve attempt Crossref native work proof DOI is invalid")
    expected_url = (
        "https://api.crossref.org/works/" + urllib.parse.quote(requested_doi)
    )
    if value["request_url"] != expected_url:
        raise ValueError("resolve attempt Crossref native work proof request URL is invalid")
    if type(value["http_status"]) is not int or value["http_status"] != 200:
        raise ValueError("resolve attempt Crossref native work proof HTTP status is invalid")
    body = _text(
        value["response_body"], "Crossref native work proof response_body", nonempty=True,
    )
    digest = value["response_sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("resolve attempt Crossref native work proof response_sha256 is invalid")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("resolve attempt Crossref native work proof body digest does not match")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("resolve attempt Crossref native work proof body is malformed") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    returned_doi = message.get("DOI") if isinstance(message, dict) else None
    if (
        not isinstance(returned_doi, str)
        or returned_doi.strip().casefold() != requested_doi.casefold()
        or message.get("type") != "posted-content"
        or message.get("subtype") != "preprint"
    ):
        raise ValueError("resolve attempt Crossref native work proof body is not the requested preprint")
    return copy.deepcopy(value)


def _validate_crossref_native_article_proof(value: Any) -> JsonDict:
    """Validate a source-bound direct journal-article response proof."""
    if type(value) is not dict or set(value) != CROSSREF_NATIVE_ARTICLE_PROOF_FIELDS:
        raise ValueError("resolve attempt Crossref native article proof has an invalid field set")
    if type(value["version"]) is not int or value["version"] != 2:
        raise ValueError("resolve attempt Crossref native article proof version is invalid")
    requested_doi = _text(
        value["requested_doi"], "Crossref native article proof requested_doi", nonempty=True,
    )
    if re.fullmatch(r"10\.\d{4,9}/[^\s]+", requested_doi) is None:
        raise ValueError("resolve attempt Crossref native article proof DOI is invalid")
    expected_url = (
        "https://api.crossref.org/works/" + urllib.parse.quote(requested_doi)
    )
    if value["request_url"] != expected_url:
        raise ValueError("resolve attempt Crossref native article proof request URL is invalid")
    if type(value["http_status"]) is not int or value["http_status"] != 200:
        raise ValueError("resolve attempt Crossref native article proof HTTP status is invalid")
    body = _text(
        value["response_body"], "Crossref native article proof response_body", nonempty=True,
    )
    digest = value["response_sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("resolve attempt Crossref native article proof response_sha256 is invalid")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("resolve attempt Crossref native article proof body digest does not match")
    source_digest = value["source_raw_sha256"]
    if type(source_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
        raise ValueError("resolve attempt Crossref native article proof source hash is invalid")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("resolve attempt Crossref native article proof body is malformed") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    returned_doi = message.get("DOI") if isinstance(message, dict) else None
    if (
        not isinstance(returned_doi, str)
        or returned_doi.strip().casefold() != requested_doi.casefold()
        or message.get("type") != "journal-article"
    ):
        raise ValueError(
            "resolve attempt Crossref native article proof body is not the requested journal article"
        )
    return copy.deepcopy(value)


def _validate_datacite_native_doi_proof(value: Any) -> JsonDict:
    if type(value) is not dict or set(value) != DATACITE_NATIVE_DOI_PROOF_FIELDS:
        raise ValueError("DataCite native DOI proof has an invalid field set")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("DataCite native DOI proof version is invalid")
    requested_doi = _text(
        value["requested_doi"], "DataCite native DOI proof requested DOI", nonempty=True,
    )
    if re.fullmatch(r"10\.\d{4,9}/[^\s]+", requested_doi, flags=re.IGNORECASE) is None:
        raise ValueError("DataCite native DOI proof requested DOI is invalid")
    expected_url = (
        "https://api.datacite.org/dois/" + urllib.parse.quote(requested_doi)
    )
    if value["request_url"] != expected_url:
        raise ValueError("DataCite native DOI proof request URL is invalid")
    if type(value["http_status"]) is not int or value["http_status"] != 200:
        raise ValueError("DataCite native DOI proof HTTP status is invalid")
    body = _text(
        value["response_body"], "DataCite native DOI proof response body", nonempty=True,
    )
    digest = value["response_sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("DataCite native DOI proof response digest is invalid")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("DataCite native DOI proof body digest does not match")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("DataCite native DOI proof body is malformed") from exc
    record = payload.get("data") if isinstance(payload, dict) else None
    attributes = record.get("attributes") if isinstance(record, dict) else None
    record_id = record.get("id") if isinstance(record, dict) else None
    record_doi = attributes.get("doi") if isinstance(attributes, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("type") != "dois"
        or not isinstance(record_id, str)
        or record_id.strip().casefold() != requested_doi.casefold()
        or not isinstance(attributes, dict)
        or not isinstance(record_doi, str)
        or record_doi.strip().casefold() != requested_doi.casefold()
    ):
        raise ValueError("DataCite native DOI proof body is not the requested DOI record")
    return copy.deepcopy(value)


def _validate_identity_context(value: Any) -> JsonDict:
    if type(value) is not dict or set(value) - IDENTITY_CONTEXT_FIELDS:
        raise ValueError("resolve attempt fulltext link identity_context is invalid")
    for key in (
        "title",
        "provider",
        "provider_record_id",
        "first_author",
        "canonical_url",
        "landing_page_url",
        "expected_document_title",
        "official_document_relation",
    ):
        if key in value:
            _text(value[key], f"identity_context.{key}", nullable=True)
    if "year" in value:
        year = value["year"]
        if year is not None and (isinstance(year, bool) or not isinstance(year, (int, str))):
            raise ValueError("resolve attempt identity_context.year is invalid")
    if "authors" in value:
        _text_list(value["authors"], "identity_context.authors")
    if "identifiers" in value:
        _identifier_map(value["identifiers"], "identity_context.identifiers")
    if "source_confidence" in value:
        _finite_number(value["source_confidence"], "identity_context.source_confidence", nullable=True)
    if "canonical_host" in value and type(value["canonical_host"]) is not bool:
        raise ValueError("resolve attempt identity_context.canonical_host must be boolean")
    if "official" in value and type(value["official"]) is not bool:
        raise ValueError("resolve attempt identity_context.official must be boolean")
    return copy.deepcopy(value)


def _validate_fulltext_links(value: Any) -> list[JsonDict]:
    if type(value) is not list:
        raise ValueError("resolve attempt fulltext_links must be a list")
    out: list[JsonDict] = []
    for raw in value:
        if type(raw) is not dict or "url" not in raw or set(raw) - FULLTEXT_LINK_FIELDS:
            raise ValueError("resolve attempt fulltext link has an invalid field set")
        _text(raw["url"], "fulltext link URL", nonempty=True)
        for key in FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS:
            if key in raw:
                _text(raw[key], f"fulltext link {key}", nullable=True)
        if "identity_context" in raw:
            _validate_identity_context(raw["identity_context"])
        out.append(copy.deepcopy(raw))
    return out


def _validate_provider_attempt(raw: JsonDict, kind: str) -> JsonDict:
    extras: set[str] = set()
    if kind == "identifier_validation":
        extras = {"identifier_type", "identifier_value"}
    elif kind == "identifier_fallback":
        extras = {"attempt_kind", "provider_via"}
    elif kind == "enrichment":
        extras = {"enrichment_only"}
    unknown = set(raw) - PROVIDER_FIELDS - extras
    if unknown:
        raise ValueError(f"unknown resolve attempt field: {sorted(unknown)[0]}")
    _text(raw.get("status"), "status", nonempty=True)
    _text(raw.get("via"), "via", nonempty=True)
    if "reason" in raw:
        _text(raw["reason"], "reason", nullable=True)
    for key in PROVIDER_TEXT_FIELDS:
        if key in raw:
            _text(raw[key], key, nullable=key in NULLABLE_PROVIDER_TEXT_FIELDS)
    for key in PROVIDER_BOOLEAN_FIELDS:
        if key in raw and type(raw[key]) is not bool:
            raise ValueError(f"resolve attempt {key} must be boolean")
    if "fulltext_exists" in raw:
        fulltext_exists = raw["fulltext_exists"]
        if type(fulltext_exists) is not bool and fulltext_exists != "unknown":
            raise ValueError("resolve attempt fulltext_exists is invalid")
    if "http_status" in raw and type(raw["http_status"]) is not int:
        raise ValueError("resolve attempt http_status must be an integer")
    if "matched_year" in raw:
        year = raw["matched_year"]
        if year is not None and (isinstance(year, bool) or not isinstance(year, (int, str))):
            raise ValueError("resolve attempt matched_year is invalid")
    for key in ("article_ids", "identifiers"):
        if key in raw:
            _identifier_map(raw[key], key)
    for key in ("matched_authors", "oa_license_urls"):
        if key in raw:
            _text_list(raw[key], key)
    if "fulltext_availability" in raw:
        item = raw["fulltext_availability"]
        if type(item) is not dict or not {"status", "scope", "observed_by"} <= set(item) or set(item) - {"status", "scope", "observed_by", "reason"}:
            raise ValueError("resolve attempt fulltext_availability has an invalid field set")
        for key in ("status", "scope", "observed_by"):
            _text(item[key], f"fulltext_availability.{key}", nonempty=True)
        if "reason" in item:
            _text(item["reason"], "fulltext_availability.reason", nullable=True)
    if "metadata_match" in raw:
        _validate_metadata_match(raw["metadata_match"])
    if "resolved_identifier" in raw:
        item = raw["resolved_identifier"]
        if type(item) is not dict or set(item) != {"type", "value", "validated_via"}:
            raise ValueError("resolve attempt resolved_identifier has an invalid field set")
        for key in ("type", "value", "validated_via"):
            _text(item[key], f"resolved_identifier.{key}", nonempty=True)
    if "trial_registration" in raw:
        item = raw["trial_registration"]
        if type(item) is not dict or set(item) != {"registry", "id", "status", "url"}:
            raise ValueError("resolve attempt trial_registration has an invalid field set")
        _text(item["registry"], "trial_registration.registry", nonempty=True)
        _text(item["id"], "trial_registration.id", nonempty=True)
        _text(item["status"], "trial_registration.status", nullable=True)
        _text(item["url"], "trial_registration.url", nullable=True)
    if "identity_search" in raw:
        item = raw["identity_search"]
        if type(item) is not dict or set(item) != {"resolver", "query_contract", "completion", "outcome"}:
            raise ValueError("resolve attempt identity_search has an invalid field set")
        _text(item["resolver"], "identity_search.resolver", nonempty=True)
        _text(item["query_contract"], "identity_search.query_contract", nonempty=True)
        if item["completion"] not in {"complete", "incomplete"} or item["outcome"] not in {"no_compatible_identity", "candidate_incompatible", "compatible_identity", "inconclusive"}:
            raise ValueError("resolve attempt identity_search outcome is invalid")
        if (item["completion"] == "incomplete") != (item["outcome"] == "inconclusive"):
            raise ValueError("identity search completion contradicts outcome")
    if CROSSREF_NATIVE_WORK_PROOF_FIELD in raw:
        proof = _validate_crossref_native_work_proof(
            raw[CROSSREF_NATIVE_WORK_PROOF_FIELD],
        )
        requested_doi = proof["requested_doi"].casefold()
        if proof["version"] == 1:
            if raw["status"] != "resolved" or raw["via"] not in {
                "crossref", "identifier_validation:crossref",
            }:
                raise ValueError("resolve attempt Crossref native proof is not attached to a direct result")
            if kind == "identifier_validation" and (
                str(raw.get("identifier_value") or "").strip().casefold() != requested_doi
            ):
                raise ValueError("resolve attempt Crossref native proof DOI does not match identifier value")
            if raw.get("doi") is not None and (
                str(raw.get("doi")).strip().casefold() != requested_doi
            ):
                raise ValueError("resolve attempt Crossref native proof DOI does not match provider DOI")
        else:
            resolved = raw.get("resolved_identifier")
            identity_search = raw.get("identity_search")
            if (
                kind != "provider_result"
                or raw["status"] != "resolved"
                or raw["via"] != "crossref_metadata"
                or raw.get("resolution_basis") != "metadata_search"
                or raw.get("work_type") != "journal-article"
                or not isinstance(resolved, dict)
                or resolved.get("type") != "doi"
                or str(resolved.get("value") or "").strip().casefold() != requested_doi
                or resolved.get("validated_via")
                != "crossref:ambiguous_title_journal_boundary_certificate"
                or not isinstance(identity_search, dict)
                or identity_search.get("completion") != "complete"
                or identity_search.get("outcome") != "compatible_identity"
            ):
                raise ValueError(
                    "resolve attempt Crossref native article proof is not attached to its admitted search result"
                )
            if raw.get("doi") is None or str(raw.get("doi")).strip().casefold() != requested_doi:
                raise ValueError("resolve attempt Crossref native article proof DOI does not match provider DOI")
    if DATACITE_NATIVE_DOI_PROOF_FIELD in raw:
        proof = _validate_datacite_native_doi_proof(
            raw[DATACITE_NATIVE_DOI_PROOF_FIELD],
        )
        if (
            kind != "identifier_validation"
            or raw.get("status") != "resolved"
            or raw.get("via") != "identifier_validation:datacite"
            or raw.get("identifier_type") != "doi"
        ):
            raise ValueError(
                "DataCite native DOI proof is not attached to a direct DOI validation"
            )
        requested_doi = proof["requested_doi"].casefold()
        if str(raw.get("identifier_value") or "").strip().casefold() != requested_doi:
            raise ValueError(
                "DataCite native DOI proof DOI does not match identifier value"
            )
        if raw.get("doi") is not None and (
            str(raw.get("doi")).strip().casefold() != requested_doi
        ):
            raise ValueError("DataCite native DOI proof DOI does not match provider DOI")
    if "fulltext_links" in raw:
        _validate_fulltext_links(raw["fulltext_links"])

    if kind == "identifier_validation":
        if raw["via"] not in {
            "identifier_validation:crossref",
            "identifier_validation:doi.org",
            "identifier_validation:datacite",
        }:
            raise ValueError("resolve attempt identifier-validation via is invalid")
        if raw.get("identifier_type") != "doi":
            raise ValueError("resolve attempt identifier-validation type is invalid")
        _text(raw.get("identifier_value"), "identifier_value", nonempty=True)
    elif kind == "identifier_fallback":
        if raw.get("attempt_kind") != "identifier_fallback" or "provider_via" not in raw:
            raise ValueError("resolve attempt identifier-fallback discriminator is invalid")
        _text(raw["provider_via"], "provider_via", nullable=True)
    elif kind == "enrichment" and raw.get("enrichment_only") is not True:
        raise ValueError("resolve attempt enrichment discriminator is invalid")
    return copy.deepcopy(raw)


def _attempt_kind(raw: JsonDict) -> str:
    if raw.get("attempt_kind") == "identifier_fallback":
        return "identifier_fallback"
    via = raw.get("via")
    if via == "resolver_exception":
        return "resolver_exception"
    if via == "repair_exception":
        return "repair_exception"
    if via == "repair_failed":
        return "repair_failed"
    if isinstance(via, str) and via.startswith("fetch_repair:"):
        return "fetch_repair"
    if raw.get("enrichment_only") is True:
        return "enrichment"
    if "status" not in raw and via == "pubmed":
        return "pubmed_esummary_failure"
    if isinstance(via, str) and via.startswith("identifier_validation:"):
        return "identifier_validation"
    return "provider_result"


def _validate_exception_attempt(raw: JsonDict, kind: str) -> JsonDict:
    if set(raw) != {"status", "via", "reason", "exception"}:
        raise ValueError("resolve attempt exception has an invalid field set")
    expected = "unresolved" if kind == "resolver_exception" else "repair_exception"
    if raw["status"] != expected or raw["via"] != kind:
        raise ValueError("resolve attempt exception discriminator is invalid")
    _text(raw["reason"], "exception reason", nonempty=True)
    detail = raw["exception"]
    if type(detail) is not dict or set(detail) != {"type", "message", "traceback"}:
        raise ValueError("resolve attempt exception detail has an invalid field set")
    for key in ("type", "message", "traceback"):
        _text(detail[key], f"exception.{key}")
    return copy.deepcopy(raw)


def _validate_fetch_repair(raw: JsonDict) -> JsonDict:
    if set(raw) != FETCH_REPAIR_FIELDS:
        raise ValueError("resolve attempt fetch-repair has an invalid field set")
    if raw["status"] != "stored" or not isinstance(raw["via"], str) or not raw["via"].startswith("fetch_repair:"):
        raise ValueError("resolve attempt fetch-repair discriminator is invalid")
    for key in ("method", "reason", "pdf_url", "content_version", "corroborate_signal"):
        _text(raw[key], f"fetch_repair.{key}", nullable=True)
    _finite_number(raw["corroborate_score"], "fetch_repair.corroborate_score", nullable=True)
    if raw["via"] != f"fetch_repair:{raw['method'] or 'unknown'}":
        raise ValueError("resolve attempt fetch-repair method is inconsistent")
    direct = raw["direct_attempts"]
    if type(direct) is not list:
        raise ValueError("resolve attempt fetch-repair direct_attempts must be a list")
    for item in direct:
        if type(item) is not dict or set(item) != DIRECT_ATTEMPT_FIELDS:
            raise ValueError("resolve attempt fetch-repair direct item has an invalid field set")
        _text(item["method"], "fetch_repair.direct.method", nonempty=True)
        _text(item["source_ref"], "fetch_repair.direct.source_ref", nullable=True)
        _text(item["outcome"], "fetch_repair.direct.outcome", nonempty=True)
        _text(item["reason"], "fetch_repair.direct.reason", nullable=True)
    execution = raw["execution_attempts"]
    if type(execution) is not list:
        raise ValueError("resolve attempt fetch-repair execution_attempts must be a list")
    for item in execution:
        if type(item) is not dict or set(item) != EXECUTION_ATTEMPT_FIELDS:
            raise ValueError("resolve attempt fetch-repair execution item has an invalid field set")
        for key in ("method", "url", "final_url", "kind", "outcome", "reason", "content_type"):
            _text(item[key], f"fetch_repair.execution.{key}", nullable=True)
        if item["status"] is not None and type(item["status"]) is not int:
            raise ValueError("resolve attempt fetch-repair execution status must be integer or null")
    return copy.deepcopy(raw)


def validate_resolve_attempts(value: Any) -> list[JsonDict] | None:
    """Validate the complete built-in Resolve attempt union."""
    if value is None:
        return None
    if type(value) is not list:
        raise ValueError("resolve attempts must be a list or null")
    rows: list[JsonDict] = []
    for raw in value:
        if type(raw) is not dict:
            raise ValueError("resolve attempt must be an object")
        kind = _attempt_kind(raw)
        if kind in PROVIDER_ATTEMPT_KINDS:
            item = _validate_provider_attempt(raw, kind)
        elif kind in {"resolver_exception", "repair_exception"}:
            item = _validate_exception_attempt(raw, kind)
        elif kind == "repair_failed":
            if set(raw) != {"status", "via", "reason"} or raw["status"] != "repair_failed" or raw["via"] != "repair_failed":
                raise ValueError("resolve attempt repair-failed shape is invalid")
            _text(raw["reason"], "repair-failed reason", nonempty=True)
            item = copy.deepcopy(raw)
        elif kind == "pubmed_esummary_failure":
            if set(raw) != {"via", "reason"} or raw["via"] != "pubmed":
                raise ValueError("resolve attempt PubMed esummary shape is invalid")
            reason = _text(raw["reason"], "PubMed esummary reason", nonempty=True)
            if not reason.startswith("esummary:"):
                raise ValueError("resolve attempt PubMed esummary reason is invalid")
            item = copy.deepcopy(raw)
        else:
            item = _validate_fetch_repair(raw)
        item["_attempt_kind"] = kind
        rows.append(item)
    return rows


def _presence_values(item: JsonDict, names: tuple[str, ...]) -> tuple[Any, ...]:
    values: list[Any] = []
    for name in names:
        values.extend((item.get(name), int(name in item)))
    return tuple(values)


def _tagged_year(value: Any, *, present: bool) -> tuple[str, int | None, str | None]:
    if not present:
        return "absent", None, None
    if value is None:
        return "null", None, None
    if type(value) is int:
        return "integer", value, None
    return "text", None, value


def _tagged_number(value: Any, *, nullable: bool = True) -> tuple[str, int | None, float | None]:
    if value is None and nullable:
        return "null", None, None
    if type(value) is int:
        return "integer", value, None
    return "real", None, value


def _write_metadata(conn: sqlite3.Connection, ref_id: str, order: int, item: JsonDict) -> None:
    ordinary = set(item) != METADATA_MINIMAL_FIELDS
    scalar_fields = (
        "score", "title_overlap", "author_match", "cited_first_author",
        "matched_first_author", "year_match", "matched_year", "venue_overlap",
        "matched_venue", "year_mismatch_plausible", "ordinal_conflict",
        "author_conflict", "year_conflict", "venue_conflict", "metadata_conflict",
    )
    proof_json = (
        json.dumps(
            item[PUBLISHER_NATIVE_URI_PROOF_FIELD],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        if PUBLISHER_NATIVE_URI_PROOF_FIELD in item else None
    )
    registered_coordinate_certificate_json = (
        json.dumps(
            item[REGISTERED_COORDINATE_CERTIFICATE_FIELD],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        if REGISTERED_COORDINATE_CERTIFICATE_FIELD in item else None
    )
    generic_locator_certificate_json = (
        json.dumps(
            item[GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        if GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD in item else None
    )
    titleless_native_venue_composition_json = (
        json.dumps(
            item[TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        if TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD in item else None
    )
    values: list[Any] = [
        ref_id, order, "ordinary" if ordinary else "minimal", proof_json,
        registered_coordinate_certificate_json, generic_locator_certificate_json,
        titleless_native_venue_composition_json,
    ]
    for name in scalar_fields:
        value = item.get(name)
        if name in {"author_match", "year_match", "year_mismatch_plausible", "ordinal_conflict", "author_conflict", "year_conflict", "venue_conflict", "metadata_conflict"} and name in item:
            value = int(value)
        values.extend((value, int(name in item)))
    for name in (
        "cited_ordinals", "matched_ordinals", "hard_conflicts",
        METADATA_COORDINATE_COMPARISONS_FIELD,
    ):
        values.extend((int(name in item), len(item.get(name, []))))
    columns = [
        "ref_id", "attempt_order", "metadata_kind",
        "publisher_native_uri_proof_json", "registered_coordinate_certificate_json",
        "generic_article_locator_certificate_json",
        "titleless_native_venue_composition_json",
    ]
    for name in scalar_fields:
        columns.extend((name, f"{name}_present"))
    for name in (
        "cited_ordinals", "matched_ordinals", "hard_conflicts",
        METADATA_COORDINATE_COMPARISONS_FIELD,
    ):
        columns.extend((f"{name}_present", f"{name}_count"))
    conn.execute(
        f"INSERT INTO resolve_attempt_metadata_matches({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values),
    )
    for kind, key in (("cited", "cited_ordinals"), ("matched", "matched_ordinals")):
        for child_order, ordinal in enumerate(item.get(key, [])):
            conn.execute(
                "INSERT INTO resolve_attempt_metadata_ordinals VALUES(?,?,?,?,?)",
                (ref_id, order, kind, child_order, ordinal),
            )
    for child_order, conflict in enumerate(item.get("hard_conflicts", [])):
        conn.execute(
            "INSERT INTO resolve_attempt_metadata_hard_conflicts VALUES(?,?,?,?)",
            (ref_id, order, child_order, conflict),
        )
    for comparison_order, comparison in enumerate(
        item.get(METADATA_COORDINATE_COMPARISONS_FIELD, [])
    ):
        conn.execute(
            "INSERT INTO resolve_attempt_metadata_coordinate_comparisons VALUES(?,?,?,?,?,?,?)",
            (
                ref_id, order, comparison_order, comparison["kind"],
                comparison["cited_value"], comparison["matched_value"], comparison["status"],
            ),
        )


def _write_link_context(
    conn: sqlite3.Connection,
    ref_id: str,
    order: int,
    link_order: int,
    context: JsonDict,
) -> None:
    year_kind, year_integer, year_text = _tagged_year(
        context.get("year"), present="year" in context,
    )
    confidence = context.get("source_confidence")
    confidence_kind = (
        "absent" if "source_confidence" not in context
        else _tagged_number(confidence)[0]
    )
    confidence_integer = confidence if confidence_kind == "integer" else None
    confidence_real = confidence if confidence_kind == "real" else None
    conn.execute(
        f"""
        INSERT INTO resolve_attempt_link_contexts(
          ref_id,attempt_order,link_order,title,title_present,
            year_kind,year_integer,year_text,provider,provider_present,
            provider_record_id,provider_record_id_present,
            first_author,first_author_present,
            source_confidence_kind,source_confidence_integer,source_confidence_real,
            canonical_host,canonical_host_present,canonical_url,canonical_url_present,
            landing_page_url,landing_page_url_present,
            expected_document_title,expected_document_title_present,
            official,official_present,
            official_document_relation,official_document_relation_present,
            authors_present,authors_count,
          identifiers_present,identifiers_count
        ) VALUES({','.join('?' for _ in range(33))})
        """,
        (
            ref_id, order, link_order, context.get("title"), int("title" in context),
            year_kind, year_integer, year_text,
            context.get("provider"), int("provider" in context),
            context.get("provider_record_id"), int("provider_record_id" in context),
            context.get("first_author"), int("first_author" in context),
            confidence_kind, confidence_integer, confidence_real,
            None if "canonical_host" not in context else int(context["canonical_host"]),
            int("canonical_host" in context),
            context.get("canonical_url"), int("canonical_url" in context),
            context.get("landing_page_url"), int("landing_page_url" in context),
            context.get("expected_document_title"), int("expected_document_title" in context),
            None if "official" not in context else int(context["official"]),
            int("official" in context),
            context.get("official_document_relation"),
            int("official_document_relation" in context),
            int("authors" in context),
            len(context.get("authors", [])), int("identifiers" in context),
            len(context.get("identifiers", {})),
        ),
    )
    for author_order, author in enumerate(context.get("authors", [])):
        conn.execute(
            "INSERT INTO resolve_attempt_link_context_authors VALUES(?,?,?,?,?)",
            (ref_id, order, link_order, author_order, author),
        )
    for identifier_order, (identifier_type, identifier_value) in enumerate(
        context.get("identifiers", {}).items()
    ):
        conn.execute(
            "INSERT INTO resolve_attempt_link_context_identifiers VALUES(?,?,?,?,?,?)",
            (ref_id, order, link_order, identifier_order, identifier_type, identifier_value),
        )


def _write_provider(conn: sqlite3.Connection, ref_id: str, order: int, item: JsonDict) -> None:
    columns = ["ref_id", "attempt_order"]
    values: list[Any] = [ref_id, order]
    for name in PROVIDER_TEXT_FIELDS:
        columns.extend((name, f"{name}_present"))
        values.extend((item.get(name), int(name in item)))
    for name in PROVIDER_BOOLEAN_FIELDS:
        columns.extend((name, f"{name}_present"))
        values.extend((None if name not in item else int(item[name]), int(name in item)))
    columns.extend(("http_status", "http_status_present"))
    values.extend((item.get("http_status"), int("http_status" in item)))
    fulltext_exists = item.get("fulltext_exists")
    fulltext_kind = (
        "absent" if "fulltext_exists" not in item
        else "boolean" if type(fulltext_exists) is bool
        else "text"
    )
    columns.extend(("fulltext_exists_kind", "fulltext_exists_boolean", "fulltext_exists_text"))
    values.extend((
        fulltext_kind,
        int(fulltext_exists) if fulltext_kind == "boolean" else None,
        fulltext_exists if fulltext_kind == "text" else None,
    ))
    year_kind, year_integer, year_text = _tagged_year(
        item.get("matched_year"), present="matched_year" in item,
    )
    columns.extend(("matched_year_kind", "matched_year_integer", "matched_year_text"))
    values.extend((year_kind, year_integer, year_text))
    for name in PROVIDER_COLLECTION_FIELDS:
        columns.extend((f"{name}_present", f"{name}_count"))
        value = item.get(name, {} if name in {"article_ids", "identifiers"} else [])
        values.extend((int(name in item), len(value)))
    for name in PROVIDER_OBJECT_FIELDS:
        columns.append(f"{name}_present")
        values.append(int(name in item))
    native_work_proof = item.get(CROSSREF_NATIVE_WORK_PROOF_FIELD)
    columns.extend(("crossref_native_work_proof_json", "crossref_native_work_proof_present"))
    values.extend((
        json.dumps(native_work_proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if native_work_proof is not None else None,
        int(CROSSREF_NATIVE_WORK_PROOF_FIELD in item),
    ))
    datacite_native_doi_proof = item.get(DATACITE_NATIVE_DOI_PROOF_FIELD)
    columns.extend((
        "datacite_native_doi_proof_json",
        "datacite_native_doi_proof_present",
    ))
    values.extend((
        json.dumps(
            datacite_native_doi_proof,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) if datacite_native_doi_proof is not None else None,
        int(DATACITE_NATIVE_DOI_PROOF_FIELD in item),
    ))
    conn.execute(
        f"INSERT INTO resolve_attempt_provider_details({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values),
    )

    for family in ("article_ids", "identifiers"):
        for identifier_order, (identifier_type, identifier_value) in enumerate(
            item.get(family, {}).items()
        ):
            conn.execute(
                "INSERT INTO resolve_attempt_identifier_values VALUES(?,?,?,?,?,?)",
                (ref_id, order, family, identifier_order, identifier_type, identifier_value),
            )
    for author_order, author in enumerate(item.get("matched_authors", [])):
        conn.execute(
            "INSERT INTO resolve_attempt_matched_authors VALUES(?,?,?,?)",
            (ref_id, order, author_order, author),
        )
    for license_order, license_url in enumerate(item.get("oa_license_urls", [])):
        conn.execute(
            "INSERT INTO resolve_attempt_oa_license_urls VALUES(?,?,?,?)",
            (ref_id, order, license_order, license_url),
        )
    availability = item.get("fulltext_availability")
    if availability is not None:
        conn.execute(
            "INSERT INTO resolve_attempt_fulltext_availability VALUES(?,?,?,?,?,?,?)",
            (
                ref_id, order, availability["status"], availability["scope"],
                availability["observed_by"], availability.get("reason"),
                int("reason" in availability),
            ),
        )
    identity_search = item.get("identity_search")
    if identity_search is not None:
        conn.execute("INSERT INTO resolve_attempt_identity_searches VALUES(?,?,?,?,?,?)", (
            ref_id, order, identity_search["resolver"], identity_search["query_contract"],
            identity_search["completion"], identity_search["outcome"],
        ))
    resolved_identifier = item.get("resolved_identifier")
    if resolved_identifier is not None:
        conn.execute(
            "INSERT INTO resolve_attempt_resolved_identifiers VALUES(?,?,?,?,?)",
            (
                ref_id, order, resolved_identifier["type"], resolved_identifier["value"],
                resolved_identifier["validated_via"],
            ),
        )
    trial = item.get("trial_registration")
    if trial is not None:
        conn.execute(
            "INSERT INTO resolve_attempt_trial_registrations VALUES(?,?,?,?,?,?)",
            (ref_id, order, trial["registry"], trial["id"], trial["status"], trial["url"]),
        )
    metadata = item.get("metadata_match")
    if metadata is not None:
        _write_metadata(conn, ref_id, order, metadata)
    for link_order, link in enumerate(item.get("fulltext_links", [])):
        columns = ["ref_id", "attempt_order", "link_order", "url"]
        values = [ref_id, order, link_order, link["url"]]
        for name in FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS:
            columns.extend((name, f"{name}_present"))
            values.extend((link.get(name), int(name in link)))
        columns.append("identity_context_present")
        values.append(int("identity_context" in link))
        conn.execute(
            f"INSERT INTO resolve_attempt_fulltext_links({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values),
        )
        if "identity_context" in link:
            _write_link_context(conn, ref_id, order, link_order, link["identity_context"])


def _write_fetch_repair(conn: sqlite3.Connection, ref_id: str, order: int, item: JsonDict) -> None:
    score_kind, score_integer, score_real = _tagged_number(item["corroborate_score"])
    conn.execute(
        """
        INSERT INTO resolve_attempt_fetch_repairs(
          ref_id,attempt_order,method,pdf_url,content_version,
          corroborate_signal,corroborate_score_kind,corroborate_score_integer,
          corroborate_score_real,direct_attempt_count,execution_attempt_count
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ref_id, order, item["method"], item["pdf_url"], item["content_version"],
            item["corroborate_signal"], score_kind,
            score_integer, score_real, len(item["direct_attempts"]),
            len(item["execution_attempts"]),
        ),
    )
    for child_order, child in enumerate(item["direct_attempts"]):
        conn.execute(
            "INSERT INTO resolve_attempt_fetch_repair_direct VALUES(?,?,?,?,?,?,?)",
            (
                ref_id, order, child_order, child["method"], child["source_ref"],
                child["outcome"], child["reason"],
            ),
        )
    for child_order, child in enumerate(item["execution_attempts"]):
        conn.execute(
            "INSERT INTO resolve_attempt_fetch_repair_execution VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                ref_id, order, child_order, child["method"], child["url"],
                child["final_url"], child["kind"], child["outcome"], child["reason"],
                child["status"], child["content_type"],
            ),
        )


def replace_resolve_attempts(
    conn: sqlite3.Connection,
    ref_id: str,
    value: Any,
) -> list[JsonDict] | None:
    rows = validate_resolve_attempts(value)
    conn.execute("DELETE FROM resolve_attempt_states WHERE ref_id=?", (ref_id,))
    conn.execute(
        "INSERT INTO resolve_attempt_states(ref_id,state,attempt_count) VALUES(?,?,?)",
        (
            ref_id,
            "attempts_not_produced" if rows is None else "produced",
            0 if rows is None else len(rows),
        ),
    )
    if rows is None:
        return None
    for order, raw in enumerate(rows):
        item = dict(raw)
        kind = item.pop("_attempt_kind")
        status_present = int("status" in item)
        reason_present = int("reason" in item)
        provider_via_present = int("provider_via" in item)
        conn.execute(
            """
            INSERT INTO resolve_attempts(
              ref_id,attempt_order,attempt_kind,status,status_present,via,
              reason,reason_present,identifier_type,identifier_value,
              provider_via,provider_via_present
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ref_id, order, kind, item.get("status"), status_present, item["via"],
                item.get("reason"), reason_present, item.get("identifier_type"),
                item.get("identifier_value"), item.get("provider_via"),
                provider_via_present,
            ),
        )
        if kind in PROVIDER_ATTEMPT_KINDS:
            _write_provider(conn, ref_id, order, item)
        elif kind in {"resolver_exception", "repair_exception"}:
            detail = item["exception"]
            conn.execute(
                "INSERT INTO resolve_attempt_exceptions VALUES(?,?,?,?,?)",
                (ref_id, order, detail["type"], detail["message"], detail["traceback"]),
            )
        elif kind == "fetch_repair":
            _write_fetch_repair(conn, ref_id, order, item)
    return rows


def _dense(rows: list[sqlite3.Row], key: str, label: str) -> None:
    if [row[key] for row in rows] != list(range(len(rows))):
        raise RuntimeError(f"typed resolve attempt {label} order is sparse")


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise RuntimeError(f"typed resolve attempt {label} boolean is invalid")
    return bool(value)


def _read_metadata(conn: sqlite3.Connection, ref_id: str, order: int) -> JsonDict:
    row = conn.execute(
        "SELECT * FROM resolve_attempt_metadata_matches WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    if row is None:
        raise RuntimeError("typed resolve attempt metadata is missing")
    scalar_fields = (
        "score", "title_overlap", "author_match", "cited_first_author",
        "matched_first_author", "year_match", "matched_year", "venue_overlap",
        "matched_venue", "year_mismatch_plausible", "ordinal_conflict",
        "author_conflict", "year_conflict", "venue_conflict", "metadata_conflict",
    )
    out: JsonDict = {}
    bool_fields = {
        "author_match", "year_match", "year_mismatch_plausible", "ordinal_conflict",
        "author_conflict", "year_conflict", "venue_conflict", "metadata_conflict",
    }
    for name in scalar_fields:
        present = _boolean(row[f"{name}_present"], f"metadata {name} presence")
        if present:
            value = row[name]
            if name in bool_fields:
                value = _boolean(value, f"metadata {name}")
            out[name] = value
        elif row[name] is not None:
            raise RuntimeError("typed resolve attempt metadata has an absent value")
    proof_json = row["publisher_native_uri_proof_json"]
    if proof_json is not None:
        try:
            out[PUBLISHER_NATIVE_URI_PROOF_FIELD] = json.loads(proof_json)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("typed resolve attempt publisher native URI proof is malformed") from exc
    registered_coordinate_certificate_json = row["registered_coordinate_certificate_json"]
    if registered_coordinate_certificate_json is not None:
        try:
            out[REGISTERED_COORDINATE_CERTIFICATE_FIELD] = json.loads(
                registered_coordinate_certificate_json,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "typed resolve attempt registered coordinate certificate is malformed"
            ) from exc
    generic_certificate_json = row["generic_article_locator_certificate_json"]
    if generic_certificate_json is not None:
        try:
            out[GENERIC_ARTICLE_LOCATOR_CERTIFICATE_FIELD] = json.loads(
                generic_certificate_json,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "typed resolve attempt generic article locator certificate is malformed"
            ) from exc
    venue_composition_json = row["titleless_native_venue_composition_json"]
    if venue_composition_json is not None:
        try:
            out[TITLELESS_NATIVE_VENUE_COMPOSITION_FIELD] = json.loads(
                venue_composition_json,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "typed resolve attempt generic locator venue companion is malformed"
            ) from exc
    for kind, key in (("cited", "cited_ordinals"), ("matched", "matched_ordinals")):
        children = list(conn.execute(
            "SELECT ordinal_order,ordinal_value FROM resolve_attempt_metadata_ordinals "
            "WHERE ref_id=? AND attempt_order=? AND ordinal_kind=? ORDER BY ordinal_order",
            (ref_id, order, kind),
        ))
        _dense(children, "ordinal_order", f"metadata {kind} ordinals")
        present = _boolean(row[f"{key}_present"], f"metadata {key} presence")
        if len(children) != row[f"{key}_count"] or (not present and children):
            raise RuntimeError("typed resolve attempt metadata ordinal count is inconsistent")
        if present:
            out[key] = [child["ordinal_value"] for child in children]
    conflicts = list(conn.execute(
        "SELECT conflict_order,conflict FROM resolve_attempt_metadata_hard_conflicts "
        "WHERE ref_id=? AND attempt_order=? ORDER BY conflict_order",
        (ref_id, order),
    ))
    _dense(conflicts, "conflict_order", "metadata hard conflicts")
    conflicts_present = _boolean(row["hard_conflicts_present"], "metadata conflict presence")
    if len(conflicts) != row["hard_conflicts_count"] or (not conflicts_present and conflicts):
        raise RuntimeError("typed resolve attempt metadata conflict count is inconsistent")
    if conflicts_present:
        out["hard_conflicts"] = [child["conflict"] for child in conflicts]
    comparisons = list(conn.execute(
        """
        SELECT comparison_order,coordinate_kind,cited_value,matched_value,status
        FROM resolve_attempt_metadata_coordinate_comparisons
        WHERE ref_id=? AND attempt_order=? ORDER BY comparison_order
        """,
        (ref_id, order),
    ))
    _dense(comparisons, "comparison_order", "metadata coordinate comparisons")
    comparisons_present = _boolean(
        row["coordinate_comparisons_present"],
        "metadata coordinate comparison presence",
    )
    if len(comparisons) != row["coordinate_comparisons_count"] or (
        comparisons_present != bool(comparisons)
    ):
        raise RuntimeError(
            "typed resolve attempt metadata coordinate comparison count is inconsistent"
        )
    if comparisons_present:
        out[METADATA_COORDINATE_COMPARISONS_FIELD] = [
            {
                "kind": child["coordinate_kind"],
                "cited_value": child["cited_value"],
                "matched_value": child["matched_value"],
                "status": child["status"],
            }
            for child in comparisons
        ]
    expected_kind = "minimal" if set(out) == METADATA_MINIMAL_FIELDS else "ordinary"
    try:
        _validate_metadata_match(out)
    except ValueError as exc:
        raise RuntimeError("typed resolve attempt metadata is inconsistent") from exc
    if row["metadata_kind"] != expected_kind:
        raise RuntimeError("typed resolve attempt metadata kind is inconsistent")
    return out


def _read_link_context(
    conn: sqlite3.Connection,
    ref_id: str,
    order: int,
    link_order: int,
) -> JsonDict:
    row = conn.execute(
        "SELECT * FROM resolve_attempt_link_contexts WHERE ref_id=? AND attempt_order=? AND link_order=?",
        (ref_id, order, link_order),
    ).fetchone()
    if row is None:
        raise RuntimeError("typed resolve attempt link context is missing")
    out: JsonDict = {}
    for name in (
        "title",
        "provider",
        "provider_record_id",
        "first_author",
        "canonical_url",
        "landing_page_url",
        "expected_document_title",
        "official_document_relation",
    ):
        if _boolean(row[f"{name}_present"], f"link context {name} presence"):
            out[name] = row[name]
        elif row[name] is not None:
            raise RuntimeError("typed resolve attempt link context has an absent value")
    year_kind = row["year_kind"]
    if year_kind == "null":
        out["year"] = None
    elif year_kind == "integer":
        out["year"] = row["year_integer"]
    elif year_kind == "text":
        out["year"] = row["year_text"]
    elif year_kind != "absent":
        raise RuntimeError("typed resolve attempt link context year is invalid")
    confidence_kind = row["source_confidence_kind"]
    if confidence_kind == "null":
        out["source_confidence"] = None
    elif confidence_kind == "integer":
        out["source_confidence"] = row["source_confidence_integer"]
    elif confidence_kind == "real":
        out["source_confidence"] = row["source_confidence_real"]
    elif confidence_kind != "absent":
        raise RuntimeError("typed resolve attempt link context confidence is invalid")
    if _boolean(row["canonical_host_present"], "link context canonical host presence"):
        out["canonical_host"] = _boolean(row["canonical_host"], "link context canonical host")
    elif row["canonical_host"] is not None:
        raise RuntimeError("typed resolve attempt link context has absent canonical host")
    if _boolean(row["official_present"], "link context official presence"):
        out["official"] = _boolean(row["official"], "link context official")
    elif row["official"] is not None:
        raise RuntimeError("typed resolve attempt link context has an absent official flag")
    authors = list(conn.execute(
        "SELECT author_order,author FROM resolve_attempt_link_context_authors "
        "WHERE ref_id=? AND attempt_order=? AND link_order=? ORDER BY author_order",
        (ref_id, order, link_order),
    ))
    _dense(authors, "author_order", "link context authors")
    authors_present = _boolean(row["authors_present"], "link context authors presence")
    if len(authors) != row["authors_count"] or (not authors_present and authors):
        raise RuntimeError("typed resolve attempt link context author count is inconsistent")
    if authors_present:
        out["authors"] = [child["author"] for child in authors]
    identifiers = list(conn.execute(
        "SELECT identifier_order,identifier_type,identifier_value "
        "FROM resolve_attempt_link_context_identifiers "
        "WHERE ref_id=? AND attempt_order=? AND link_order=? ORDER BY identifier_order",
        (ref_id, order, link_order),
    ))
    _dense(identifiers, "identifier_order", "link context identifiers")
    identifiers_present = _boolean(row["identifiers_present"], "link context identifiers presence")
    if len(identifiers) != row["identifiers_count"] or (not identifiers_present and identifiers):
        raise RuntimeError("typed resolve attempt link context identifier count is inconsistent")
    if identifiers_present:
        out["identifiers"] = {
            child["identifier_type"]: child["identifier_value"] for child in identifiers
        }
        if len(out["identifiers"]) != len(identifiers):
            raise RuntimeError("typed resolve attempt link context identifiers are duplicated")
    return out


def _read_provider(conn: sqlite3.Connection, ref_id: str, order: int) -> JsonDict:
    row = conn.execute(
        "SELECT * FROM resolve_attempt_provider_details WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    if row is None:
        raise RuntimeError("typed resolve attempt provider detail is missing")
    out: JsonDict = {}
    for name in PROVIDER_TEXT_FIELDS:
        if _boolean(row[f"{name}_present"], f"provider {name} presence"):
            out[name] = row[name]
        elif row[name] is not None:
            raise RuntimeError("typed resolve attempt provider has an absent value")
    for name in PROVIDER_BOOLEAN_FIELDS:
        if _boolean(row[f"{name}_present"], f"provider {name} presence"):
            out[name] = _boolean(row[name], f"provider {name}")
        elif row[name] is not None:
            raise RuntimeError("typed resolve attempt provider has an absent boolean")
    if _boolean(row["http_status_present"], "provider http status presence"):
        out["http_status"] = row["http_status"]
    elif row["http_status"] is not None:
        raise RuntimeError("typed resolve attempt provider has an absent HTTP status")
    if row["fulltext_exists_kind"] == "boolean":
        out["fulltext_exists"] = _boolean(row["fulltext_exists_boolean"], "provider fulltext exists")
    elif row["fulltext_exists_kind"] == "text":
        out["fulltext_exists"] = row["fulltext_exists_text"]
    elif row["fulltext_exists_kind"] != "absent":
        raise RuntimeError("typed resolve attempt provider fulltext state is invalid")
    if row["matched_year_kind"] == "null":
        out["matched_year"] = None
    elif row["matched_year_kind"] == "integer":
        out["matched_year"] = row["matched_year_integer"]
    elif row["matched_year_kind"] == "text":
        out["matched_year"] = row["matched_year_text"]
    elif row["matched_year_kind"] != "absent":
        raise RuntimeError("typed resolve attempt provider year is invalid")

    for family in ("article_ids", "identifiers"):
        children = list(conn.execute(
            "SELECT identifier_order,identifier_type,identifier_value "
            "FROM resolve_attempt_identifier_values "
            "WHERE ref_id=? AND attempt_order=? AND identifier_family=? ORDER BY identifier_order",
            (ref_id, order, family),
        ))
        _dense(children, "identifier_order", f"provider {family}")
        present = _boolean(row[f"{family}_present"], f"provider {family} presence")
        if len(children) != row[f"{family}_count"] or (not present and children):
            raise RuntimeError("typed resolve attempt provider identifier count is inconsistent")
        if present:
            out[family] = {
                child["identifier_type"]: child["identifier_value"] for child in children
            }
            if len(out[family]) != len(children):
                raise RuntimeError("typed resolve attempt provider identifiers are duplicated")

    for table, key, order_key, value_key in (
        ("resolve_attempt_matched_authors", "matched_authors", "author_order", "author"),
        ("resolve_attempt_oa_license_urls", "oa_license_urls", "license_order", "license_url"),
    ):
        children = list(conn.execute(
            f"SELECT {order_key},{value_key} FROM {table} WHERE ref_id=? AND attempt_order=? ORDER BY {order_key}",
            (ref_id, order),
        ))
        _dense(children, order_key, f"provider {key}")
        present = _boolean(row[f"{key}_present"], f"provider {key} presence")
        if len(children) != row[f"{key}_count"] or (not present and children):
            raise RuntimeError("typed resolve attempt provider list count is inconsistent")
        if present:
            out[key] = [child[value_key] for child in children]

    availability = conn.execute(
        "SELECT * FROM resolve_attempt_fulltext_availability WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    availability_present = _boolean(row["fulltext_availability_present"], "provider availability presence")
    if availability_present != (availability is not None):
        raise RuntimeError("typed resolve attempt provider availability state is inconsistent")
    if availability is not None:
        item = {
            "status": availability["status"], "scope": availability["scope"],
            "observed_by": availability["observed_by"],
        }
        if _boolean(availability["reason_present"], "provider availability reason presence"):
            item["reason"] = availability["reason"]
        elif availability["reason"] is not None:
            raise RuntimeError("typed resolve attempt provider availability has absent reason")
        out["fulltext_availability"] = item

    identity_search = conn.execute(
        "SELECT * FROM resolve_attempt_identity_searches WHERE ref_id=? AND attempt_order=?", (ref_id, order),
    ).fetchone()
    identity_search_present = _boolean(row["identity_search_present"], "provider identity search presence")
    if identity_search_present != (identity_search is not None):
        raise RuntimeError("typed resolve attempt provider identity search state is inconsistent")
    if identity_search is not None:
        out["identity_search"] = {key: identity_search[key] for key in ("resolver", "query_contract", "completion", "outcome")}

    native_proof_present = _boolean(
        row["crossref_native_work_proof_present"], "provider Crossref native proof presence",
    )
    native_proof_json = row["crossref_native_work_proof_json"]
    if native_proof_present:
        try:
            out[CROSSREF_NATIVE_WORK_PROOF_FIELD] = _validate_crossref_native_work_proof(
                json.loads(native_proof_json)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("typed resolve attempt Crossref native proof is malformed") from exc
    elif native_proof_json is not None:
        raise RuntimeError("typed resolve attempt Crossref native proof has an absent value")

    datacite_proof_present = _boolean(
        row["datacite_native_doi_proof_present"],
        "provider DataCite native DOI proof presence",
    )
    datacite_proof_json = row["datacite_native_doi_proof_json"]
    if datacite_proof_present:
        try:
            out[DATACITE_NATIVE_DOI_PROOF_FIELD] = (
                _validate_datacite_native_doi_proof(
                    json.loads(datacite_proof_json)
                )
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "typed resolve attempt DataCite native DOI proof is malformed"
            ) from exc
    elif datacite_proof_json is not None:
        raise RuntimeError(
            "typed resolve attempt DataCite native DOI proof has an absent value"
        )

    metadata_present = _boolean(row["metadata_match_present"], "provider metadata presence")
    metadata_exists = conn.execute(
        "SELECT 1 FROM resolve_attempt_metadata_matches WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone() is not None
    if metadata_present != metadata_exists:
        raise RuntimeError("typed resolve attempt provider metadata state is inconsistent")
    if metadata_present:
        out["metadata_match"] = _read_metadata(conn, ref_id, order)

    resolved = conn.execute(
        "SELECT * FROM resolve_attempt_resolved_identifiers WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    resolved_present = _boolean(row["resolved_identifier_present"], "provider resolved identifier presence")
    if resolved_present != (resolved is not None):
        raise RuntimeError("typed resolve attempt provider resolved identifier state is inconsistent")
    if resolved is not None:
        out["resolved_identifier"] = {
            "type": resolved["identifier_type"], "value": resolved["identifier_value"],
            "validated_via": resolved["validated_via"],
        }

    trial = conn.execute(
        "SELECT * FROM resolve_attempt_trial_registrations WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    trial_present = _boolean(row["trial_registration_present"], "provider trial presence")
    if trial_present != (trial is not None):
        raise RuntimeError("typed resolve attempt provider trial state is inconsistent")
    if trial is not None:
        out["trial_registration"] = {
            "registry": trial["registry"], "id": trial["registration_id"],
            "status": trial["status"], "url": trial["url"],
        }

    links = list(conn.execute(
        "SELECT * FROM resolve_attempt_fulltext_links WHERE ref_id=? AND attempt_order=? ORDER BY link_order",
        (ref_id, order),
    ))
    _dense(links, "link_order", "provider fulltext links")
    links_present = _boolean(row["fulltext_links_present"], "provider fulltext links presence")
    if len(links) != row["fulltext_links_count"] or (not links_present and links):
        raise RuntimeError("typed resolve attempt provider link count is inconsistent")
    if links_present:
        values: list[JsonDict] = []
        for link in links:
            item = {"url": link["url"]}
            for name in FULLTEXT_LINK_OPTIONAL_TEXT_FIELDS:
                if _boolean(link[f"{name}_present"], f"link {name} presence"):
                    item[name] = link[name]
                elif link[name] is not None:
                    raise RuntimeError("typed resolve attempt link has an absent value")
            context = conn.execute(
                "SELECT 1 FROM resolve_attempt_link_contexts WHERE ref_id=? AND attempt_order=? AND link_order=?",
                (ref_id, order, link["link_order"]),
            ).fetchone()
            context_present = _boolean(link["identity_context_present"], "link context presence")
            if context_present != (context is not None):
                raise RuntimeError("typed resolve attempt link context state is inconsistent")
            if context_present:
                item["identity_context"] = _read_link_context(
                    conn, ref_id, order, link["link_order"],
                )
            values.append(item)
        out["fulltext_links"] = values
    return out


def _read_fetch_repair(conn: sqlite3.Connection, ref_id: str, order: int) -> JsonDict:
    row = conn.execute(
        "SELECT * FROM resolve_attempt_fetch_repairs WHERE ref_id=? AND attempt_order=?",
        (ref_id, order),
    ).fetchone()
    if row is None:
        raise RuntimeError("typed resolve attempt fetch repair is missing")
    if row["corroborate_score_kind"] == "null":
        score = None
    elif row["corroborate_score_kind"] == "integer":
        score = row["corroborate_score_integer"]
    elif row["corroborate_score_kind"] == "real":
        score = row["corroborate_score_real"]
    else:
        raise RuntimeError("typed resolve attempt fetch-repair score is invalid")
    direct = list(conn.execute(
        "SELECT * FROM resolve_attempt_fetch_repair_direct WHERE ref_id=? AND attempt_order=? ORDER BY direct_order",
        (ref_id, order),
    ))
    execution = list(conn.execute(
        "SELECT * FROM resolve_attempt_fetch_repair_execution WHERE ref_id=? AND attempt_order=? ORDER BY execution_order",
        (ref_id, order),
    ))
    _dense(direct, "direct_order", "fetch-repair direct attempts")
    _dense(execution, "execution_order", "fetch-repair execution attempts")
    if len(direct) != row["direct_attempt_count"] or len(execution) != row["execution_attempt_count"]:
        raise RuntimeError("typed resolve attempt fetch-repair count is inconsistent")
    return {
        "method": row["method"], "pdf_url": row["pdf_url"],
        "content_version": row["content_version"],
        "corroborate_signal": row["corroborate_signal"], "corroborate_score": score,
        "direct_attempts": [
            {
                "method": child["method"], "source_ref": child["source_ref"],
                "outcome": child["outcome"], "reason": child["reason"],
            }
            for child in direct
        ],
        "execution_attempts": [
            {
                "method": child["method"], "url": child["url"],
                "final_url": child["final_url"], "kind": child["kind"],
                "outcome": child["outcome"], "reason": child["reason"],
                "status": child["status"], "content_type": child["content_type"],
            }
            for child in execution
        ],
    }


def read_resolve_attempts(
    conn: sqlite3.Connection,
    ref_id: str,
) -> list[JsonDict] | None:
    state = conn.execute(
        "SELECT state,attempt_count FROM resolve_attempt_states WHERE ref_id=?",
        (ref_id,),
    ).fetchone()
    if state is None:
        raise RuntimeError("typed resolve attempt state is missing")
    attempt_rows = list(conn.execute(
        "SELECT * FROM resolve_attempts WHERE ref_id=? ORDER BY attempt_order",
        (ref_id,),
    ))
    if state["state"] == "attempts_not_produced":
        if state["attempt_count"] != 0 or attempt_rows:
            raise RuntimeError("typed attempts-not-produced state has attempt rows")
        return None
    if state["state"] != "produced":
        raise RuntimeError("typed resolve attempt state is invalid")
    _dense(attempt_rows, "attempt_order", "parent")
    if len(attempt_rows) != state["attempt_count"]:
        raise RuntimeError("typed resolve attempt count is inconsistent")

    rows: list[JsonDict] = []
    for row in attempt_rows:
        kind = row["attempt_kind"]
        if kind not in ATTEMPT_KINDS:
            raise RuntimeError("typed resolve attempt kind is invalid")
        item: JsonDict = {"via": row["via"]}
        if _boolean(row["status_present"], "status presence"):
            item["status"] = row["status"]
        elif row["status"] is not None:
            raise RuntimeError("typed resolve attempt has an absent status")
        if _boolean(row["reason_present"], "reason presence"):
            item["reason"] = row["reason"]
        elif row["reason"] is not None:
            raise RuntimeError("typed resolve attempt has an absent reason")
        if kind == "identifier_validation":
            item["identifier_type"] = row["identifier_type"]
            item["identifier_value"] = row["identifier_value"]
        elif row["identifier_type"] is not None or row["identifier_value"] is not None:
            raise RuntimeError("typed resolve attempt has unexpected identifier validation")
        if kind == "identifier_fallback":
            item["attempt_kind"] = "identifier_fallback"
            if not _boolean(row["provider_via_present"], "provider via presence"):
                raise RuntimeError("typed resolve attempt identifier fallback lacks provider via")
            item["provider_via"] = row["provider_via"]
        elif _boolean(row["provider_via_present"], "provider via presence") or row["provider_via"] is not None:
            raise RuntimeError("typed resolve attempt has unexpected provider via")
        if kind == "enrichment":
            item["enrichment_only"] = True

        provider_exists = conn.execute(
            "SELECT 1 FROM resolve_attempt_provider_details WHERE ref_id=? AND attempt_order=?",
            (ref_id, row["attempt_order"]),
        ).fetchone() is not None
        exception_exists = conn.execute(
            "SELECT 1 FROM resolve_attempt_exceptions WHERE ref_id=? AND attempt_order=?",
            (ref_id, row["attempt_order"]),
        ).fetchone() is not None
        fetch_exists = conn.execute(
            "SELECT 1 FROM resolve_attempt_fetch_repairs WHERE ref_id=? AND attempt_order=?",
            (ref_id, row["attempt_order"]),
        ).fetchone() is not None
        expected = (
            (True, False, False) if kind in PROVIDER_ATTEMPT_KINDS
            else (False, True, False) if kind in {"resolver_exception", "repair_exception"}
            else (False, False, True) if kind == "fetch_repair"
            else (False, False, False)
        )
        if (provider_exists, exception_exists, fetch_exists) != expected:
            raise RuntimeError("typed resolve attempt subtype is inconsistent")
        if provider_exists:
            item.update(_read_provider(conn, ref_id, row["attempt_order"]))
        elif exception_exists:
            detail = conn.execute(
                "SELECT * FROM resolve_attempt_exceptions WHERE ref_id=? AND attempt_order=?",
                (ref_id, row["attempt_order"]),
            ).fetchone()
            item["exception"] = {
                "type": detail["exception_type"], "message": detail["message"],
                "traceback": detail["traceback"],
            }
        elif fetch_exists:
            item.update(_read_fetch_repair(conn, ref_id, row["attempt_order"]))
        rows.append(item)
    try:
        validated = validate_resolve_attempts(rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("typed resolve attempts are invalid") from exc
    if validated is None:
        raise RuntimeError("typed resolve attempts unexpectedly decoded as absent")
    for item in validated:
        item.pop("_attempt_kind", None)
    return validated
