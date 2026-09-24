# tests/test_pause_schema_recovery.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real pause writer: mocking _pause hid the Verify regression."""
from unittest import mock
import os

import pytest

from core.app.phases import verify as verify_phase
from core.app.runtime.repository import _repo_mark_phase
from core.app.runtime import tasks as runtime_tasks
from core.infra.db import RunRepository
from core.infra.db.phase_event_storage import normalize_phase_event
from tests.test_recovery_regressions import make_run
from tests.test_source_identity_attestation_phase import (
    _run_with_unverified_fulltext, _answer_identity,
)


@pytest.mark.parametrize('slot', ['fetch', 'research', 'verify', 'parse_review'])
def test_real_pause_persists_all_supported_slots(tmp_path, slot):
    run, repo, _ = make_run(tmp_path)
    phase = {'research': 'web_research', 'parse_review': 'parse'}.get(slot, slot)
    repo.update_run_phase(phase)
    repo.close()
    assert runtime_tasks._pause(slot, run, 2, 'Test instructions') == 10
    repo = RunRepository.open_readonly(run)
    try:
        event = repo.list_phase_events()[-1]
        assert event.event_type == 'pause'
        assert event.payload == {'slot': slot, 'pending_tasks': 2}
        assert repo.get_run().phase == phase
        assert repo.get_run().status == 'paused'
    finally:
        repo.close()


def test_verify_identity_pause_and_resume_without_mocking_pause(tmp_path):
    with mock.patch.dict("os.environ", {}):
        run = _run_with_unverified_fulltext(tmp_path)
    repo = RunRepository.open(run)
    repo.update_run_phase('verify')
    repo.close()
    with mock.patch.object(verify_phase._sources, 'document_identity_probe',
                           return_value={'decision': 'inconclusive'}), \
         mock.patch.object(verify_phase, '_execute_claim_evidence_tasks') as execute:
        assert verify_phase.phase_verify({'run_dir': run}) == 10
        execute.assert_not_called()
        _answer_identity(run, 'keep_unverified')
        assert verify_phase.phase_verify({'run_dir': run}) == 'web_research'
    repo = RunRepository.open_readonly(run)
    try:
        assert repo.list_phase_events()[-1].payload['slot'] == 'verify'
        assert all(t.status == 'applied' for t in repo.list_tasks(slot='verify'))
    finally:
        repo.close()


def test_old_incompatible_schemas_still_rejected(tmp_path):
    run, repo, _ = make_run(tmp_path)
    repo._conn.execute("UPDATE meta SET value='81' WHERE key='schema_version'")
    repo._conn.commit()
    repo.close()
    with pytest.raises(RuntimeError, match='version 81 is unsupported'):
        RunRepository.open_readonly(run)


@pytest.mark.parametrize('count', [-1, True, 1.5])
def test_pause_validation_stays_strict(count):
    with pytest.raises(ValueError, match='non-negative integer'):
        normalize_phase_event('verify', 'pause', None, {'slot': 'verify', 'pending_tasks': count})


@pytest.mark.skipif(os.name != "posix", reason="external integrity authority requires POSIX")
def test_pause_event_is_checkpointed_inside_authority_transition(tmp_path):
    from tests._integrity_fixtures import _gate, _run
    gate, protected = _gate(tmp_path)
    run = _run(protected)
    gate.enroll(str(run))
    gate.preflight(str(run))
    lease = gate.begin_pipeline_transition(
        str(run), checkpoint_kind='phase_boundary', mutates_content_store=False,
    )
    repo = RunRepository.open(str(run))
    try:
        repo.append_phase_event('verify', 'pause', payload={'slot': 'verify', 'pending_tasks': 1})
    finally:
        repo.close()
    gate.commit_pipeline_transition(str(run), lease)
    assert gate.preflight(str(run))['status'] == 'clean'


def test_pause_phase_transition_rolls_back_when_pause_event_is_rejected(tmp_path):
    run, repo, _ = make_run(tmp_path)
    repo.close()
    with pytest.raises(ValueError, match='pause slot'):
        _repo_mark_phase(
            run,
            'verify',
            status='paused',
            event_type='pause',
            payload={'slot': 'bogus', 'pending_tasks': 1},
        )
    repo = RunRepository.open_readonly(run)
    try:
        assert repo.get_run().phase == 'resolve'
        assert repo.get_run().status == 'active'
        assert repo.list_phase_events() == []
    finally:
        repo.close()
