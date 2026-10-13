# tests/test_guided_fetch_contract.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from core.app import guided_fetch


def test_guided_source_payload_carries_user_intent_and_requested_tier():
    assert guided_fetch.source_payload(
        file_path="/controlled/source.html", tier="abstract", source_ref="https://doi.org/10.1/test"
    ) == {
        "found": True, "guided_fetch": True, "identity_attested": True, "source_tier": "abstract",
        "file_path": "/controlled/source.html", "url": "https://doi.org/10.1/test",
    }


def test_guided_browser_waiver_preserves_complete_declared_order():
    assert guided_fetch.browser_waived_payload(["r2", "r1"]) == {
        "items": [
            {"ref_id": "r2", "found": False, "disposition": "user_waived", "guided_fetch": True},
            {"ref_id": "r1", "found": False, "disposition": "user_waived", "guided_fetch": True},
        ]
    }


def test_guided_source_attestation_is_closed_to_found_human_sources():
    assert "identity_attested" not in guided_fetch.waived_payload()


@pytest.mark.parametrize("kwargs", [
    {}, {"file_path": "x", "text": "y"}, {"file_path": "x", "tier": "web"},
    {"text": "abstract text", "tier": "abstract"},
])
def test_guided_source_payload_rejects_ambiguous_or_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        guided_fetch.source_payload(**kwargs)
