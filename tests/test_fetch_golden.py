#!/usr/bin/env python3
# tests/test_fetch_golden.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Golden characterization tests for fetch queue ordering."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "fetch_golden" / "cases.json"
)


def load_cases() -> list[dict]:
    with _FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


_CASE_DEFINITIONS = [
    {
        "name": "published_official_auxiliary_beats_preprint_winner_links",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "End-to-end memory networks",
            "raw_entry": (
                "Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. "
                "End-to-end memory networks. In Advances in Neural Information "
                "Processing Systems 28, 2015."
            ),
            "source_type": "article",
            "source_kind": "article_like",
            "doi": None,
            "url": None,
            "year": 2015,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "open",
            "fulltext_exists": True,
            "fulltext_links": [
                {"url": "https://doi.org/10.48550/arxiv.1503.08895", "content_type": "doi"},
                {"url": "https://arxiv.org/pdf/1503.08895", "content_type": "pdf"},
            ],
            "auxiliary_fulltext_links": [
                {
                    "url": "https://proceedings.neurips.cc/paper_files/paper/2015/file/8fb21ee7a2207526da55a679f0332de2-Paper.pdf",
                    "content_type": "pdf",
                    "discovered_via": "neurips_search",
                }
            ],
        },
    },
    {
        "name": "genuine_preprint_citation_keeps_preprint_primary",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "End-to-end memory networks",
            "raw_entry": (
                "Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. "
                "End-to-end memory networks. arXiv preprint arXiv:1503.08895, 2015."
            ),
            "source_type": "article",
            "source_kind": "article_like",
            "doi": "10.48550/arxiv.1503.08895",
            "url": None,
            "year": 2015,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "open",
            "fulltext_exists": True,
            "fulltext_links": [
                {"url": "https://doi.org/10.48550/arxiv.1503.08895", "content_type": "doi"},
                {"url": "https://arxiv.org/pdf/1503.08895", "content_type": "pdf"},
            ],
            "auxiliary_fulltext_links": [
                {
                    "url": "https://proceedings.neurips.cc/paper_files/paper/2015/file/8fb21ee7a2207526da55a679f0332de2-Paper.pdf",
                    "content_type": "pdf",
                    "discovered_via": "neurips_search",
                }
            ],
        },
    },
    {
        "name": "doi_only_reference_generates_doi_landing",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "Example DOI-only paper",
            "raw_entry": "Doe J. Example DOI-only paper. J Testing. 2020.",
            "source_type": "article",
            "source_kind": "article_like",
            "doi": "10.1234/example.doi",
            "url": None,
            "year": 2020,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "unknown",
            "fulltext_exists": "unknown",
            "fulltext_links": [],
            "auxiliary_fulltext_links": [],
        },
    },
    {
        "name": "pmid_europepmc_provider_candidates_feed_queue",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "Example Europe PMC paper",
            "raw_entry": "Doe J. Example Europe PMC paper. J Testing. 2021.",
            "source_type": "article",
            "source_kind": "article_like",
            "doi": None,
            "pmid": "12345678",
            "url": None,
            "year": 2021,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "open",
            "fulltext_exists": True,
            "fulltext_links": [],
            "auxiliary_fulltext_links": [],
        },
        "provider_candidate_rows": [
            {
                "provider": "europepmc",
                "error": None,
                "items": [
                    {
                        "method": "europepmc",
                        "url": "https://europepmc.org/articles/PMC1234567?pdf=render",
                        "kind": "pdf",
                    },
                    {
                        "method": "europepmc",
                        "url": "https://europepmc.org/articles/PMC1234567",
                        "kind": "landing",
                    },
                ],
            }
        ],
    },
    {
        "name": "trusted_reference_url_enters_queue_directly",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "Example CVF paper",
            "raw_entry": "Doe J. Example CVF paper. ICCV 2021.",
            "source_type": "article",
            "source_kind": "article_like",
            "doi": None,
            "url": "https://openaccess.thecvf.com/content/ICCV2021/papers/Doe_Example_CVF_Paper_ICCV_2021_paper.pdf",
            "year": 2021,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "open",
            "fulltext_exists": True,
            "fulltext_links": [],
            "auxiliary_fulltext_links": [],
        },
    },
    {
        "name": "abstract_only_reference_never_builds_queue",
        "ref": {
            "id": "r1",
            "ref_number": 1,
            "title": "Example abstract-only reference",
            "raw_entry": "Doe J. Example abstract-only reference. J Abstracts. 2019.",
            "source_type": "article",
            "source_kind": "article_like",
            "doi": "10.9999/abstract-only",
            "url": None,
            "year": 2019,
        },
        "resolve_result": {
            "status": "resolved",
            "oa_status": "closed",
            "fulltext_exists": False,
            "fulltext_links": [],
            "auxiliary_fulltext_links": [],
            "abstract": "A stored abstract exists for this work.",
        },
    },
]


def _run_case(case: dict) -> dict:
    import core.fetch.service as fetch_mod

    captured = {"queues": []}
    saved_process_queue = fetch_mod._fetch_pipeline.process_queue
    saved_direct_rows = fetch_mod._provider_direct_text_rows
    saved_candidate_rows = fetch_mod._provider_candidate_rows
    saved_preprint_specs = fetch_mod._preprint_resolver_specs

    run = tempfile.mkdtemp(prefix="fetch-golden-")
    os.makedirs(os.path.join(run, "sources"), exist_ok=True)

    def fake_process_queue(**kwargs):
        captured["queues"].append(
            [item.get("url") for item in (kwargs.get("queue") or [])]
        )
        return {
            "status": "not_found",
            "method": "metadata",
            "reason": "golden characterization stub",
        }

    try:
        fetch_mod._fetch_pipeline.process_queue = fake_process_queue
        fetch_mod._provider_direct_text_rows = (
            lambda ref, email: copy.deepcopy(case.get("provider_direct_text_rows") or [])
        )
        fetch_mod._provider_candidate_rows = (
            lambda ref, email: copy.deepcopy(case.get("provider_candidate_rows") or [])
        )
        fetch_mod._preprint_resolver_specs = (
            lambda ref, email: copy.deepcopy(case.get("preprint_resolver_specs") or [])
        )
        result = fetch_mod.fetch_fulltext(
            copy.deepcopy(case["ref"]),
            run,
            copy.deepcopy(case["resolve_result"]),
            mailto="x@y.edu",
        )
        candidate_generation = (result.get("fetch_trace") or {}).get("candidate_generation") or {}
        return {
            "status": result.get("status"),
            "process_queues": captured["queues"],
            "final_queue": [
                item.get("url") for item in (candidate_generation.get("final_queue") or [])
            ],
            "preprint_queue": [
                item.get("url") for item in (candidate_generation.get("preprint_queue") or [])
            ],
        }
    finally:
        fetch_mod._fetch_pipeline.process_queue = saved_process_queue
        fetch_mod._provider_direct_text_rows = saved_direct_rows
        fetch_mod._provider_candidate_rows = saved_candidate_rows
        fetch_mod._preprint_resolver_specs = saved_preprint_specs
        shutil.rmtree(run, ignore_errors=True)


def _generate_cases() -> list[dict]:
    generated = []
    for case in _CASE_DEFINITIONS:
        row = copy.deepcopy(case)
        row["expected"] = _run_case(case)
        generated.append(row)
    return generated


def _write_cases() -> None:
    _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FIXTURE_PATH.write_text(
        json.dumps(_generate_cases(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class TestFetchGolden(unittest.TestCase):
    def test_fetch_queue_order_matches_golden_snapshots(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                self.assertIsNotNone(case.get("expected"), "Missing expected snapshot")
                actual = _run_case(case)
                self.assertEqual(actual, case["expected"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--generate":
        _write_cases()
        raise SystemExit(0)
    unittest.main()
