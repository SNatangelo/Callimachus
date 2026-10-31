# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in native regressions address only UUID synthetic PackageSmoke items."""
import os
import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != 'darwin' or os.environ.get('CALLIMACHUS_RUN_NATIVE_KEYCHAIN_TESTS') != '1' or not os.environ.get('CALLIMACHUS_NATIVE_KEYCHAIN_PATH'),
    reason='requires an explicitly enabled macOS synthetic Keychain test',
)


@pytest.fixture(autouse=True)
def owned_keychain(monkeypatch,tmp_path):
    from ctypes import POINTER, byref, c_int32, c_void_p, c_char_p
    from core.app.settings import macos_keychain as native, macos_vault
    api=native._native_api()
    reference=c_void_p()
    open_keychain=api._sec.SecKeychainOpen
    open_keychain.argtypes=(c_char_p,POINTER(c_void_p));open_keychain.restype=c_int32
    api.Error.raise_for_status(open_keychain(os.fsencode(os.environ['CALLIMACHUS_NATIVE_KEYCHAIN_PATH']),byref(reference)))
    monkeypatch.setattr(macos_vault,'_lock_directory',lambda:tmp_path/'locks')
    # Every fixture operation is pinned to the owned test/build keychain;
    # no default/search-list settings or personal login items are changed.
    class ScopedAPI:
        def __getattr__(self,name):return getattr(api,name)
        def create_query(self,**kwargs):
            if 'kSecClass' in kwargs:
                if 'kSecValueData' in kwargs:
                    kwargs['kSecUseKeychain']=reference
                else:
                    with _search_list() as items:
                        kwargs['kSecMatchSearchList']=items
                        return api.create_query(**kwargs)
            return api.create_query(**kwargs)
    from contextlib import contextmanager
    @contextmanager
    def _search_list():
        from ctypes import c_ssize_t
        create=api._found.CFArrayCreate
        create.argtypes=(c_void_p,POINTER(c_void_p),c_ssize_t,c_void_p);create.restype=c_void_p
        items=create(None,(c_void_p*1)(reference.value),1,api._found.kCFTypeArrayCallBacks)
        try:yield c_void_p(items)
        finally:native._release(api,items)
    monkeypatch.setattr(native,'_native_api',lambda:ScopedAPI())
    try:yield
    finally:native._release(api,reference.value)


def test_native_item_update_and_rollback_target_the_same_reference():
    from core.app.settings import macos_keychain as native
    service='Callimachus.PackageSmoke'
    account='native-reference-'+str(uuid.uuid4())
    reference=None
    try:
        reference=native.write_value(service, account, 'synthetic-first')
        assert reference
        assert native.read_item(service, account, reference=reference)==('synthetic-first',reference)
        updated=native.write_value(service, account, 'synthetic-second',reference=reference)
        assert updated==reference
        assert native.read_item(service, account, reference=reference)==('synthetic-second',reference)
        assert native.delete_if_matches(service, account, 'different') is False
        native.write_value(service, account, 'synthetic-first',reference=reference)
        assert native.read_item(service, account, reference=reference)==('synthetic-first',reference)
        assert native.delete_if_matches(service, account, 'synthetic-first') is True
        assert native.read_item(service, account)[0] is None
    finally:
        if reference:native.delete_item(service, account, reference)


def test_native_legacy_import_preserves_values_and_removes_only_copied_items(monkeypatch,tmp_path):
    from core.app.settings import credentials as store, macos_keychain as native, macos_vault as aggregate
    service='Callimachus.PackageSmoke'
    monkeypatch.setattr(store,'_SERVICE_NAME',service)
    monkeypatch.setenv('CALLIMACHUS_DATA_DIR', str(tmp_path/str(uuid.uuid4())))
    profile=store._profile_scope();backend=store._trusted_backend()
    values={'OPENAI_API_KEY':'synthetic-one','DEEPSEEK_API_KEY':'synthetic-two','CITATION_VERIFIER_SIGNING_KEY':'synthetic-three'}
    refs={}
    try:
        for name,value in values.items():
            account=store._username(name)
            reference=native.write_value(service,account,value)
            raw,reference=native.read_item(service,account,reference=reference)
            assert raw==value and reference
            refs[account]=reference
        assert aggregate.import_authorized(backend,values,service=service,profile=profile)==values
        raw,reference=native.read_item(service,aggregate.account(profile))
        assert reference
        refs[aggregate.account(profile)]=reference
        assert aggregate._decode(raw,service,profile)==values
        for name in values:
            assert native.read_item(service,store._username(name))[0] is None
        with store._macos_desktop_secret_session():
            assert store.load_secret_values(values)==values
            assert store.macos_credential_recovery_state()=={'pending':0}
        with pytest.raises(OSError):
            with store.update_secret_values({'OPENAI_API_KEY':'synthetic-replacement'}):
                raise OSError('paired settings fixture failure')
        assert store.load_secret_values(values)==values
    finally:
        # Discover only this test's exact aggregate if a failure occurred after create.
        for account in [aggregate.account(profile),*(store._username(name) for name in values)]:
            if account not in refs:
                _value,ref=native.read_item(service,account)
                if ref:refs[account]=ref
        for account,ref in refs.items():native.delete_item(service,account,ref)
        assert all(native.read_item(service,account)[0] is None for account in refs)
