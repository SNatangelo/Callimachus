# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

from core.fetch.storage import content_store
from core.parse.source_text import PREPARATION_VERSION


def test_content_store_reuse_skips_fulltext_with_wrong_front_matter(
    tmp_path, monkeypatch
):
    run = str(tmp_path)
    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    ref = {
        "id": "ref-34",
        "ref_number": 34,
        "doi": "10.48550/arxiv.1503.08895",
        "title": "End-to-end memory networks",
        "year": 2015,
        "raw_entry": (
            "Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. "
            "End-to-end memory networks. Advances in Neural Information "
            "Processing Systems 28. 2015."
        ),
    }
    good_text = (
        "End-to-end Memory Networks\n"
        "Sainbayar Sukhbaatar Arthur Szlam Jason Weston Rob Fergus\n"
        + "We introduce a memory network model trained end to end. " * 80
    )
    bad_text = (
        "End-to-End Memory Networks: A Survey\n"
        "Raheleh Jafari Sina Razvarz Alexander Gegov\n"
        + "This survey reviews dialog systems and neural memory models. " * 80
    )
    good = content_store.record_parsed_text(
        run,
        ref,
        "fulltext",
        "webfetch",
        good_text,
        source_ref="https://arxiv.org/pdf/1503.08895",
        mapping="tokens",
        signal="title",
        score=1.0,
        preparation={"preparation_version": PREPARATION_VERSION},
    )
    bad = content_store.record_parsed_text(
        run,
        ref,
        "fulltext",
        "core",
        bad_text,
        source_ref="https://eprints.example.org/survey.pdf",
        mapping="tokens",
        signal="title",
        score=1.0,
        preparation={"preparation_version": PREPARATION_VERSION},
    )

    cached = content_store.find_reusable_parsed_text(run, ref, tiers=("fulltext",))
    assert cached["parsed_text_id"] == good["parsed_text_id"]
    with sqlite3.connect(content_store.db_path(run)) as conn:
        row = conn.execute(
            "select active, missing from parsed_texts where parsed_text_id = ?",
            (bad["parsed_text_id"],),
        ).fetchone()
    assert row == (0, 1)
