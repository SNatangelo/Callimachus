# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from core.infra.db import RunRepository


def test_parse_payload_roundtrips_claims_references_and_citations(tmp_path):
    repo = RunRepository.create(
        str(tmp_path),
        run_id="run-parse-roundtrip",
        input_path="paper.pdf",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="deployment-test",
    )
    try:
        repo.replace_parse_payload(
            claims=[
                {
                    "id": "claim-1",
                    "sentence": "A local example is cited [1].",
                    "context_window": "A local example is cited [1].",
                    "marker_raw": "[1]",
                    "structural_provenance": {
                        "boundary_kinds": ["page_layout_interruption"]
                    },
                }
            ],
            references=[{
                "id": "ref-1",
                "ref_number": 1,
                "raw_entry": "Smith. A local example. 2020.",
                "title": "A local example",
                "doi": "10.1000/example",
                "source_type": "article",
                "source_kind": "article_like",
                "indexability": "high",
                "source_type_confidence": "high",
            }],
            citations=[{
                "claim_id": "claim-1",
                "ref_id": "ref-1",
                "ref_number": 1,
            }],
        )

        claims = repo.list_claims()
        references = repo.list_references()
        citations = repo.list_citations()

        assert len(claims) == 1
        assert claims[0].claim_id == "claim-1"
        assert claims[0].structural_provenance == {
            "boundary_kinds": ["page_layout_interruption"]
        }
        assert len(references) == 1
        assert references[0].ref_id == "ref-1"
        assert references[0].doi == "10.1000/example"
        assert [(item.claim_id, item.ref_id) for item in citations] == [
            ("claim-1", "ref-1")
        ]
        assert repo.get_run().phase == "resolve"
    finally:
        repo.close()
