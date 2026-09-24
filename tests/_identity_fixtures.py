# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic source identity fixture for deployment tests."""

import hashlib
from core.infra.db.repository import RunRepository


def _repo_with_attestation(
    tmp_path, *, task_id="identity-review", slot="verify",
):
    run_dir = tmp_path / "run"
    source_text = "A cited study\nSmith\nSource text inspected by the operator."
    source_bytes = source_text.encode("utf-8")
    source_path = run_dir / "sources" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_bytes)
    repo = RunRepository.create(
        str(run_dir),
        run_id="identity",
        input_path="paper.pdf",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile=None,
        challenge_mode=None,
        fixture_fingerprint="identity-attestation",
    )
    repo.replace_parse_payload(
        claims=[{"claim_id": "c1", "sentence": "Claim"}],
        references=[{
            "ref_id": "r1",
            "ref_number": 1,
            "raw_entry": "Smith. A cited study. 2020.",
            "title": "A cited study",
            "year": 2020,
        }],
        citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
    )
    repo.store_source_text(
        source_text_id="source-1",
        ref_id="r1",
        identity_key="source:r1",
        tier="fulltext",
        origin="user",
        stored_path="sources/source.txt",
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        char_count=len(source_text),
        mapping="user_manifest",
        match_signal="title",
        match_score=0.5,
        identity_status="unverified",
        identity_note="needs review",
        content_version="version_of_record",
        supplied_by="user",
        supplied_via="user_manifest",
    )
    repo.upsert_resolve_result("r1", {
        "ref_id": "r1",
        "status": "unverified",
        "reason": "inconclusive",
        "matched_title": "A cited study",
        "reference_status_tag": "unverified",
        "fabrication_risk": "low",
    })
    repo.create_source_identity_attestation(
        task_id=task_id,
        ref_id="r1",
        source_text_id="source-1",
        slot=slot,
        instructions="Attest this exact source identity or keep it unverified.",
    )
    target_sha256 = repo.get_task(task_id).task_payload["target_sha256"]
    return repo, source_path, target_sha256
