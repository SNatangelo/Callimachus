# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Recovery consent uses themed UI, stays optional and never loops."""
import pytest

from tests.test_macos_keychain_session import vault as vault


@pytest.mark.parametrize('dark', [False, True])
@pytest.mark.parametrize('confirm', [False, True])
def test_recovery_dialog_is_thematic_optional_and_offered_once(monkeypatch, tmp_path, dark, confirm):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6 import QtCore as C, QtWidgets as W
    original_settings = C.QSettings
    class Settings(original_settings):
        def __init__(self, *_a, **_kw):
            super().__init__(str(tmp_path/'preferences.ini'), original_settings.Format.IniFormat)
    monkeypatch.setattr(C, 'QSettings', Settings)
    Settings().setValue('dark_theme', dark)
    app = W.QApplication.instance() or W.QApplication([])
    from core.gui.desktop import create_desktop_window
    calls = []; dialogs = []
    pending = {'value': 2}
    def recover():
        calls.append('recover');pending['value'] = 0
        return {'recovered': 2, 'pending': 0}
    window = create_desktop_window(language='en', credential_recovery_state_loader=lambda: {'pending': pending['value']}, credential_recovery_callback=recover)
    original_exec = W.QDialog.exec
    def execute(dialog):
        if dialog.objectName() != 'credentialRecoveryDialog':
            return original_exec(dialog)
        dialogs.append(dialog)
        assert window.isVisible()
        body = dialog.findChild(W.QLabel, 'messageBody').text()
        assert 'Always Allow' in body
        assert 'continue without' in body
        cancel = next(b for b in dialog.findChildren(W.QPushButton) if b.text() == 'Cancel')
        accept = next(b for b in dialog.findChildren(W.QPushButton) if b.text() == 'Confirm')
        assert cancel.objectName() == 'dangerButton'
        assert accept.property('primaryAction')
        assert cancel.isDefault()
        assert not dialog.findChild(W.QLabel, 'messageIcon').pixmap().isNull()
        C.QTimer.singleShot(0, (accept if confirm else cancel).click)
        return original_exec(dialog)
    monkeypatch.setattr(W.QDialog, 'exec', execute)
    try:
        window.show();app.processEvents()
        assert len(dialogs) == 1
        assert calls == (['recover'] if confirm else [])
        for index in (1, 2, 3, 0):
            window.navigation.setCurrentRow(index);app.processEvents()
            assert window.pages.currentIndex() == index
        window.hide();window.show();app.processEvents()
        assert len(dialogs) == 1
    finally:
        window._settings_dirty.clear();window.close();app.processEvents()
        window.deleteLater();app.sendPostedEvents(None, C.QEvent.Type.DeferredDelete)


def test_mac_native_reader_forbids_ui_and_releases_resources(monkeypatch):
    from ctypes import c_void_p
    from types import SimpleNamespace
    import sys
    from core.app.settings.macos_keychain import read_without_ui
    query = {};released = []
    def create_query(**kwargs):query.update(kwargs);return 123
    def release(ptr):released.append(ptr.value if isinstance(ptr,c_void_p) else ptr)
    def read(q,result):result._obj.value = 9;return 0
    errors=SimpleNamespace(item_not_found=-25300,keychain_denied=-128,sec_auth_failed=-25293,sec_interaction_not_allowed=-25308)
    api=SimpleNamespace(create_query=create_query,k_=lambda s:s,SecItemCopyMatching=read,error=errors,Error=SimpleNamespace(raise_for_status=lambda _:None),cfstr_to_str=lambda _:'synthetic-key',_found=SimpleNamespace(CFRelease=release))
    def get_ui(state):
        state._obj.value = 1
        return 0
    def set_ui(_state):
        return 0
    api._sec = SimpleNamespace(SecKeychainGetUserInteractionAllowed=get_ui,
                               SecKeychainSetUserInteractionAllowed=set_ui)
    import keyring.backends.macOS as backend
    monkeypatch.setattr(backend,'api',api,raising=False)
    assert read_without_ui('synthetic-service','synthetic-account') == 'synthetic-key'
    assert query['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
    assert released == [9,123]
    for code in (-128,-25293,-25308):
        monkeypatch.setattr(api,'SecItemCopyMatching',lambda *_args,c=code:c)
        with pytest.raises(PermissionError):read_without_ui('synthetic-service','synthetic-account')


@pytest.mark.parametrize('dark', [False, True])
@pytest.mark.parametrize('first_failure', ['deny', 'exception'])
def test_real_vault_recovery_requires_explicit_settings_retry(vault, monkeypatch, tmp_path, dark, first_failure):
    """Real dialog → real session callbacks → simulated native consent boundary."""
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6 import QtCore as C, QtWidgets as W
    from core.app.settings import credentials as store
    from core.gui.desktop import create_desktop_window

    monkeypatch.setattr(store, '_SERVICE_NAME', 'Callimachus.PackageSmoke')
    monkeypatch.setenv('CALLIMACHUS_DATA_DIR', '/CallimachusPackageSmoke/18dfd723-d259-4c51-b326-450b8e0f5179')
    original_settings = C.QSettings
    class Settings(original_settings):
        def __init__(self, *_a, **_kw):
            super().__init__(str(tmp_path/'preferences.ini'), original_settings.Format.IniFormat)
            self.setFallbacksEnabled(False)
    monkeypatch.setattr(C, 'QSettings', Settings)
    Settings().setValue('dark_theme', dark)
    app = W.QApplication.instance() or W.QApplication([])
    values = {name: 'synthetic-gui-value' for name in (
        'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'CITATION_VERIFIER_SIGNING_KEY')}
    with store.update_secret_values(values):
        pass
    original_values = dict(vault.values)
    vault.grants.clear()
    dialogs = []
    original_exec = W.QDialog.exec
    def execute(dialog):
        if dialog.objectName() == 'credentialRecoveryDialog':
            dialogs.append(dialog)
            confirm = next(b for b in dialog.findChildren(W.QPushButton) if b.text() == 'Confirm')
            C.QTimer.singleShot(0, confirm.click)
        return original_exec(dialog)
    monkeypatch.setattr(W.QDialog, 'exec', execute)
    backend_factory = store._trusted_backend
    if first_failure == 'exception':
        def unavailable():
            raise OSError('synthetic native backend unavailable')
    windows = []
    def open_window():
        window = create_desktop_window(
            language='en', settings_loader=lambda: [],
            credential_recovery_state_loader=store.macos_credential_recovery_state,
            credential_recovery_callback=store.recover_macos_credentials)
        windows.append(window)
        window.show();app.processEvents()
        return window
    def navigate(window):
        # The optional CLI Console (4) is disabled in this synthetic profile.
        for index in (0, 1, 2, 3, 5):
            window.navigation.setCurrentRow(index);app.processEvents()
            assert window.pages.currentIndex() == index
    try:
        with store._macos_desktop_secret_session():
            assert store.load_secret_values(values) == {}
            assert store.macos_credential_recovery_state() == {'pending': 3}
            if first_failure == 'exception':
                monkeypatch.setattr(store, '_trusted_backend', unavailable)
            window = open_window()
            assert len(dialogs) == 1
            first_attempts = 1 if first_failure == 'deny' else 0
            assert len(vault.interactive) == first_attempts
            monkeypatch.setattr(store, '_trusted_backend', backend_factory)
            assert store.macos_credential_recovery_state() == {'pending': 3}
            assert window.statusBar().currentMessage()
            assert vault.values == original_values
            reads_after_failure = list(vault.reads)
            navigate(window)
            window.hide();window.show();app.processEvents()
            for _ in range(4):
                assert store.load_secret_values(values) == {}
                app.processEvents()
            assert len(dialogs) == 1
            assert len(vault.interactive) == first_attempts
            assert vault.reads == reads_after_failure
            window.navigation.setCurrentRow(window.pages.indexOf(window._settings_page))
            app.processEvents()
            retry = window.findChild(W.QPushButton, 'credentialRecoveryButton')
            assert retry is not None
            vault.grants.update(vault.values)  # Simulated native Always Allow.
            retry.click();app.processEvents()
            assert len(dialogs) == 2
            assert len(vault.interactive) == first_attempts + 1
            assert store.load_secret_values(values) == values
            assert store.macos_credential_recovery_state() == {'pending': 0}
            assert vault.values == original_values
            navigate(window)
            assert len(dialogs) == 2
            window.close();app.processEvents()
        # A cold session/window uses the persistent grant, not the old cache.
        with store._macos_desktop_secret_session():
            assert store.load_secret_values(values) == values
            fresh = open_window()
            navigate(fresh)
            assert len(dialogs) == 2
            assert len(vault.interactive) == first_attempts + 1
            assert vault.values == original_values
    finally:
        for window in windows:
            window._settings_dirty.clear();window.close();window.deleteLater()
        app.sendPostedEvents(None, C.QEvent.Type.DeferredDelete)


def test_package_gui_probe_wires_real_callbacks_and_isolates_only_preferences(vault, monkeypatch, tmp_path):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PySide6')
    from PySide6 import QtCore as C, QtWidgets as W
    from core.app.settings import credentials as store
    from core.gui import desktop
    import run as entry

    app = W.QApplication.instance() or W.QApplication([])
    monkeypatch.setattr(entry.sys, 'platform', 'darwin')
    probe_id = '3ead31ec-f68a-4695-b040-2b55d91cc160'
    monkeypatch.setenv('CALLIMACHUS_PACKAGE_KEYCHAIN_ID', probe_id)
    monkeypatch.setenv('CALLIMACHUS_DATA_DIR', str(tmp_path/'original-profile'))
    assert entry._probe_macos_keychain_update('create') == 0
    original_values = dict(vault.values)
    vault.grants.clear()
    real_create = desktop.create_desktop_window
    windows = []
    def create(**kwargs):
        assert store._SERVICE_NAME == 'Callimachus.PackageSmoke'
        assert entry.os.environ['CALLIMACHUS_DATA_DIR'] == '/CallimachusPackageSmoke/' + probe_id
        assert kwargs['credential_recovery_state_loader'] is store.macos_credential_recovery_state
        assert kwargs['credential_recovery_callback'] is store.recover_macos_credentials
        assert kwargs['settings_loader']() == []
        settings = C.QSettings('Callimachus', 'Callimachus')
        assert settings.format() == C.QSettings.Format.IniFormat
        assert not settings.fallbacksEnabled()
        assert settings.fileName().endswith('/preferences.ini')
        window = real_create(**kwargs)
        windows.append(window)
        C.QTimer.singleShot(50, window.close)
        return window
    monkeypatch.setattr(desktop, 'create_desktop_window', create)
    original_exec = W.QDialog.exec
    dialogs = []
    def execute(dialog):
        if dialog.objectName() == 'credentialRecoveryDialog':
            dialogs.append(dialog)
            vault.grants.update(vault.values)
            confirm = next(b for b in dialog.findChildren(W.QPushButton) if b.text() == 'Confirm')
            C.QTimer.singleShot(0, confirm.click)
        return original_exec(dialog)
    monkeypatch.setattr(W.QDialog, 'exec', execute)
    assert entry._probe_macos_keychain_update('gui') == 0
    assert len(windows) == len(dialogs) == len(vault.interactive) == 1
    assert vault.values == original_values
    assert entry.os.environ['CALLIMACHUS_DATA_DIR'] == str(tmp_path/'original-profile')
    assert store._MACOS_SECRET_SESSION is None
    app.processEvents()
