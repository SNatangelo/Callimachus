# tests/test_report_bibliography.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""An existence-only export must not execute or impersonate semantic Verify."""
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from core.app.commands import report_bibliography as report
from core.infra.db import RunRepository
from core.infra.db.schema import SCHEMA_VERSION
from core.infra.integrity import IntegrityGateError
from core.shared.typed_canonical import encode
from tests._recovery_fixtures import _current_fetch_task
from tests.test_recovery_regressions import make_run


def populated(tmp_path):
    run, repo, _ = make_run(tmp_path, count=5)
    repo.create_task(task_id='fetch:r1', slot='fetch', ref_id='r1', task_payload=_current_fetch_task(repo))
    for i, status, tag in (
        (1, 'unverified', 'suspected_fabricated'),
        (2, 'unresolved', 'unverified'),
        (3, 'resolved', 'verified'),
        (4, 'resolved', 'verified'),
    ):
        repo.upsert_resolve_result(f'r{i}', {
            'ref_id': f'r{i}', 'status': status, 'reference_status_tag': tag,
            'fabrication_risk': 'high' if i == 1 else 'unknown',
            'reason': 'Timeout' if i == 2 else 'Recorded reason <script>alert(1)</script>',
            'retracted': i == 4,
        })
    repo.update_run_phase('verify')
    repo.update_run_status('paused')
    repo.close()
    return run


def test_export_is_readonly_and_reports_missing_and_transient_separately(tmp_path):
    with mock.patch.dict('os.environ', {}):
        run = populated(tmp_path)
    path = Path(run) / 'run.sqlite'
    before = path.read_bytes()
    with mock.patch.object(RunRepository, 'open', side_effect=AssertionError('no writes')), \
         mock.patch('core.app.phases.verify.ClaimEvidenceRuntime.for_run', side_effect=AssertionError('no jury')), \
         mock.patch('core.app.phases.fetch.phase_fetch', side_effect=AssertionError('no fetch')):
        out = report.export(run)
    assert path.read_bytes() == before
    assert sorted(p.name for p in out.iterdir()) == [
        'bibliography-report.html', 'bibliography-report.json', 'bibliography-report.md',
    ]
    data = json.loads((out / 'bibliography-report.json').read_text())
    assert data['counts'] == {
        'references': 5, 'resolve_recorded': 4, 'not_checked': 1,
        'suspected_fabricated': 1, 'transient_unresolved': 1, 'retraction_flagged': 1,
        'identified': 0, 'identified_with_errors': 0, 'refuted': 0,
        'not_corroborated': 0, 'checks_incomplete': 0,
    }
    assert data['source_run']['schema_version'] == SCHEMA_VERSION
    digest = data.pop('snapshot_sha256')
    assert hashlib.sha256(encode(data)).hexdigest() == digest
    assert not data['full_pipeline_completion_asserted']
    assert not data['export_signed']
    assert data['references'][1]['suspected_fabricated'] is False
    assert data['references'][4]['resolve'] is None
    html = (out / 'bibliography-report.html').read_text()
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'BIBLIOGRAPHIC SCREENING ONLY' in html
    assert not (Path(run) / 'report.md').exists()
    repo = RunRepository.open_readonly(run)
    try:
        assert repo.get_task('fetch:r1').status == 'pending'
        assert repo.get_run().phase == 'verify'
        assert repo.get_run().status == 'paused'
        assert repo.list_phase_events() == []
    finally:
        repo.close()


def test_suspects_filter_does_not_hide_coverage_or_drop_json_rows(tmp_path):
    run = populated(tmp_path)
    out = report.export(run, suspects_only=True)
    text = (out / 'bibliography-report.md').read_text()
    assert '### Reference 1' in text
    assert '### Reference 2' not in text
    assert 'not checked: 1' in text
    data = json.loads((out / 'bibliography-report.json').read_text())
    assert len(data['references']) == 5


def test_no_source_no_resolve_is_not_reported_as_fabrication(tmp_path):
    run, repo, _ = make_run(tmp_path)
    repo.close()
    data = report.read_snapshot(run)
    assert data['counts']['not_checked'] == 1
    assert data['counts']['suspected_fabricated'] == 0
    assert 'not proof' in report.render_markdown(data, suspects_only=True)


def test_output_must_not_pollute_original_run_inventory(tmp_path):
    run = populated(tmp_path)
    with pytest.raises(ValueError, match='outside the run'):
        report.export(run, str(Path(run) / 'export'))


def test_attested_export_fails_closed_if_authority_unavailable(tmp_path):
    run = populated(tmp_path)
    with mock.patch.object(RunRepository, 'get_execution_assurance') as assurance, \
         mock.patch.object(report.RunIntegrityGate, 'from_environment',
                           side_effect=IntegrityGateError('authority missing')):
        assurance.return_value.protection = 'agent_attested'
        with pytest.raises(IntegrityGateError, match='authority missing'):
            report.export(run)
    assert not Path(run + '-bibliography').exists()


def test_attested_export_checks_without_mirroring_or_downgrading(tmp_path):
    run = populated(tmp_path)
    with mock.patch.object(RunRepository, 'get_execution_assurance') as assurance, \
         mock.patch.object(report.RunIntegrityGate, 'from_environment') as factory:
        assurance.return_value.protection = 'agent_attested'
        data = report.read_snapshot(run)
    assert factory.return_value.preflight.call_args_list == [
        mock.call(run, mirror_audit_records=False), mock.call(run, mirror_audit_records=False),
    ]
    assert data['source_run']['authority_preflight'] == 'checked'
    assert not data['export_signed']


def test_no_reference_run_refuses_misleading_empty_success(tmp_path):
    run, repo, _ = make_run(tmp_path, count=0)
    repo.close()
    with pytest.raises(ValueError, match='complete Parse'):
        report.export(run)


def test_export_repeat_is_deterministic(tmp_path):
    run = populated(tmp_path)
    output = report.export(run)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    report.export(run)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
