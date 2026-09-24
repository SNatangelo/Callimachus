# tests/test_resolve_cooldown_resume.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from unittest import mock

from core.app.phases import resolve as resolve_phase


def test_discovery_cooldown_resume_is_finalized_before_persistence():
    ref = {"id": "ref-1"}
    discovery = {
        "result": {"status": "unverified", "via": None},
        "attempts": [{"status": "unverified", "via": "semantic_scholar"}],
    }
    final = {
        "ref_id": "ref-1",
        "status": "unverified",
        "attempts": discovery["attempts"],
        "trace": [{"attempt_index": 0, "via": "semantic_scholar"}],
    }

    with mock.patch.object(
        resolve_phase, "_finalize_reference_discovery", return_value=final
    ) as finalize:
        assert resolve_phase._finalize_resumed_semantic_scholar(ref, discovery) is final

    finalize.assert_called_once_with(ref, discovery)


def test_final_cooldown_resume_payload_passes_through():
    ref = {"id": "ref-1"}
    final = {
        "ref_id": "ref-1",
        "status": "resolved",
        "attempts": [],
        "trace": [],
    }

    with mock.patch.object(resolve_phase, "_finalize_reference_discovery") as finalize:
        assert resolve_phase._finalize_resumed_semantic_scholar(ref, final) is final

    finalize.assert_not_called()
