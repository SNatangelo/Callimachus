# tests/test_resolve_deferred_progress.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from unittest import mock

from core.app.phases import resolve as resolve_phase


def test_deferred_progress_reports_reference_stage_count_and_rounded_wait():
    with mock.patch.object(resolve_phase, "_progress") as progress:
        resolve_phase._progress_deferred_wait(
            87.01, {"id": "ref-abc", "ref_number": 9}, pending=3, stage="discovery"
        )

    progress.assert_called_once_with(
        "Semantic Scholar cooldown: reference 9 deferred during discovery; "
        "retrying in 88s (3 deferred)"
    )


def test_deferred_progress_uses_stable_ref_id_when_number_missing():
    with mock.patch.object(resolve_phase, "_progress") as progress:
        resolve_phase._progress_deferred_wait(0.01, {"id": "ref-abc"}, pending=1, stage="finalization")

    progress.assert_called_once_with(
        "Semantic Scholar cooldown: reference ref-abc deferred during finalization; "
        "retrying in 1s (1 deferred)"
    )


def test_deferred_progress_never_rounds_fractional_wait_down():
    with mock.patch.object(resolve_phase, "_progress") as progress:
        resolve_phase._progress_deferred_wait(
            87.0001, {"id": "ref-abc", "ref_number": 9}, pending=1, stage="discovery"
        )
    assert "retrying in 88s" in progress.call_args.args[0]
