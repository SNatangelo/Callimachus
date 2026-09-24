# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Recovery fixture shared by the selected deployment tests."""
def _current_fetch_task(repo):
    """Create the complete Fetch snapshot required by the current task contract."""
    repo.upsert_resolve_result("r1", {
        "status": "resolved", "fulltext_exists": "unknown",
        "reference_status_tag": "confirmed", "fabrication_risk": "low",
        "tag_reason": "fixture",
    })
    row = repo._conn.execute("SELECT * FROM reference_entries WHERE ref_id='r1'").fetchone()
    reference = {field: row[field] for field in (
        "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn", "year",
        "source_type", "source_kind", "indexability", "source_type_confidence",
    )}
    return {
        "kind": "fetch", "status": "pending", "answer": None, "ref_id": "r1",
        "ref_number": reference["ref_number"], "reference": reference,
        "source_identity": {
            "reference_status_tag": "confirmed", "fabrication_risk": "low",
            "tag_reason": "fixture", "matched_title": None, "metadata_match": None,
        },
        "instructions": "Retrieve an auditable source.",
    }
