"""Desktop historical redaction does not require an inaccessible old HMAC key."""
import json
import pytest
from core.report.human import public_export
from tests.test_report_html_cli import _historical_sealed_html, _report_data, _HISTORICAL_SIGNING_KEY

@pytest.mark.parametrize('valid_signature', [True, False])
def test_desktop_historical_redaction_without_reading_signing_key(tmp_path, monkeypatch, valid_signature):
    from core.app.commands.desktop import _export_desktop_report
    run = tmp_path/'historical'; run.mkdir()
    original = _historical_sealed_html(key=_HISTORICAL_SIGNING_KEY, valid_signature=valid_signature)
    source = run/'report.html'; source.write_bytes(original.encode('utf-8'))
    (run/'run.sqlite').write_bytes(b'unsupported database')
    output = tmp_path/'public.html'
    monkeypatch.setattr(public_export.signing, 'load_key', lambda: pytest.fail('historical Desktop export must not access the signing key'))
    _export_desktop_report(str(run), str(output), historical=True)
    exported = output.read_text(encoding='utf-8')
    manifest = json.loads(output.with_suffix('.html.redaction.json').read_text(encoding='utf-8'))
    assert manifest['historical_export']['source_html_seal']['authentication'] == 'hmac_not_authenticated'
    assert 'source signature was not authenticated' in exported
    assert 'CONFIDENTIAL_UNPUBLISHED_SOURCE_TEXT' not in exported
    assert _report_data(original)['projection']['pairs'] == _report_data(exported)['projection']['pairs']
    assert source.read_text(encoding='utf-8') == original


def test_desktop_historical_export_still_rejects_modified_content(tmp_path):
    from core.app.commands.desktop import _export_desktop_report
    run=tmp_path/'historical'; run.mkdir()
    original=_historical_sealed_html(key=_HISTORICAL_SIGNING_KEY)
    (run/'report.html').write_bytes(original.replace('Source title', 'Different title').encode('utf-8'))
    output=tmp_path/'public.html'
    with pytest.raises(ValueError, match='body does not match'):
        _export_desktop_report(str(run), str(output), historical=True)
    assert not output.exists()


def test_unverified_signature_opt_in_cannot_bypass_live_run_validation(tmp_path):
    with pytest.raises(ValueError, match='only allowed for historical'):
        public_export.export_public_html(str(tmp_path), str(tmp_path/'public.html'), allow_unverified_signature=True)
