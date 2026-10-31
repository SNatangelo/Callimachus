# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""One native authorization, narrow legacy cleanup, and aggregate rollback."""
from contextlib import contextmanager
import json

import pytest
from core.app.settings import credentials as store, macos_vault as aggregate, macos_keychain as native
from tests.test_macos_keychain_session import vault  # reuse the isolated native-store fixture


def put(vault, values):
    key=aggregate.account('default')
    vault.values[key]=aggregate._encode(values, 'Callimachus', 'default')
    vault.grants.add(key)
    return key


def test_many_keys_need_one_native_grant_after_update(vault):
    values={'OPENAI_API_KEY':'one','DEEPSEEK_API_KEY':'two','CITATION_VERIFIER_SIGNING_KEY':'three'}
    account=put(vault, values)
    vault.grants.discard(account)
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(values)=={}
        assert vault.interactive==[]
        vault.grants.add(account)
        assert store.recover_macos_credentials()=={'recovered':3,'pending':0}
        assert store.load_secret_values(values)==values
        assert vault.interactive==[account]
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(values)==values
    assert vault.interactive==[account]
    assert set(vault.values)=={account}


def test_legacy_import_keeps_other_services_profiles_and_names(vault):
    values={'OPENAI_API_KEY':'one','DEEPSEEK_API_KEY':'two'}
    untouched={'different-profile:OPENAI_API_KEY':'private','default:OTHER_APP_DATA':'untouched'}
    vault.values.update(untouched)
    vault.values.update({store._username(name):value for name,value in values.items()})
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(values)=={}
        vault.grants.update(store._username(name) for name in values)
        assert store.recover_macos_credentials()=={'recovered':2,'pending':0}
        assert store.load_secret_values(values)==values
    assert all(vault.values[name]==value for name,value in untouched.items())
    assert not any(store._username(name) in vault.values for name in values)
    assert aggregate.account('default') in vault.values


def test_partial_legacy_denial_imports_only_authorized_keys_and_never_reprompts(vault):
    names={'DEEPSEEK_API_KEY':'allowed','OPENAI_API_KEY':'denied'}
    vault.values.update({store._username(name):value for name,value in names.items()})
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(names)=={}
        vault.grants.add(store._username('DEEPSEEK_API_KEY'))
        assert store.recover_macos_credentials()=={'recovered':1,'pending':1}
        assert store.load_secret_values(names)=={'DEEPSEEK_API_KEY':'allowed'}
        calls=list(vault.interactive)
        assert store.load_secret_values(names)=={'DEEPSEEK_API_KEY':'allowed'}
        assert vault.interactive==calls
    assert store._username('OPENAI_API_KEY') in vault.values
    assert store._username('DEEPSEEK_API_KEY') not in vault.values


def test_cleared_key_does_not_reappear_from_legacy_storage(vault):
    old=store._username('OPENAI_API_KEY');vault.values[old]='old';vault.grants.add(old)
    with store.update_secret_values({'OPENAI_API_KEY':''}):
        pass
    assert store.load_secret_values({'OPENAI_API_KEY'})=={}
    assert vault.values[old]=='old'  # only explicit import is allowed to delete legacy records


@pytest.mark.parametrize('existing', [False, True])
def test_paired_file_failure_restores_entire_aggregate(vault, existing):
    before={'OPENAI_API_KEY':'old','DEEPSEEK_API_KEY':'preserved'}
    if existing:put(vault,before)
    with pytest.raises(OSError, match='paired'):
        with store.update_secret_values({'OPENAI_API_KEY':'new'}):
            raise OSError('paired settings write failed')
    assert store.load_secret_values(before)==(before if existing else {})
    if not existing:assert aggregate.account('default') not in vault.values


def test_failed_readback_restores_previous_bundle(vault, monkeypatch):
    before={'OPENAI_API_KEY':'old'};key=put(vault,before)
    real=native.write_value
    state={'writes':0}
    def write(service,account,value,**kwargs):
        state['writes']+=1;reference=real(service,account,value,**kwargs)
        if state['writes']==1:vault.values[account]='malformed readback'
        return reference
    monkeypatch.setattr(native,'write_value',write)
    with pytest.raises(store.CredentialStoreError):
        with store.update_secret_values({'OPENAI_API_KEY':'new'}):pass
    assert store.load_secret_values(before)==before


@pytest.mark.parametrize('data', [
    {'schema':2,'service':'Callimachus','profile':'default','secrets':{}},
    {'schema':1,'service':'Other','profile':'default','secrets':{}},
    {'schema':1,'service':'Callimachus','profile':'other','secrets':{}},
    {'schema':1,'service':'Callimachus','profile':'default','secrets':{'LANGUAGE':'en'}},
    {'schema':1,'service':'Callimachus','profile':'default','secrets':{'OPENAI_API_KEY':1}},
])
def test_invalid_or_foreign_bundle_does_not_fall_back(vault,data):
    key=aggregate.account('default');vault.values[key]=json.dumps(data);vault.grants.add(key)
    legacy=store._username('OPENAI_API_KEY');vault.values[legacy]='must not read';vault.grants.add(legacy)
    with pytest.raises(store.CredentialStoreError):store.load_secret_values({'OPENAI_API_KEY'})
    assert legacy not in vault.reads


def test_import_preserves_a_changed_legacy_record(vault,monkeypatch):
    old=store._username('OPENAI_API_KEY');vault.values[old]='original';vault.grants.add(old)
    real=aggregate._write_and_verify
    def write(*args):
        real(*args);vault.values[old]='changed by another writer'
    monkeypatch.setattr(aggregate,'_write_and_verify',write)
    aggregate.import_authorized(store._trusted_backend(),{'OPENAI_API_KEY':'original'},service='Callimachus',profile='default')
    assert vault.values[old]=='changed by another writer'
    assert store.load_secret_values({'OPENAI_API_KEY'})=={'OPENAI_API_KEY':'original'}


@pytest.mark.parametrize('latest', ['new-value',''])
def test_import_cannot_overwrite_newer_values_or_deletion_tombstones(vault, latest):
    backend=store._trusted_backend()
    old={'OPENAI_API_KEY':'old'}
    put(vault, old)
    assert aggregate.bundle(backend,'Callimachus','default')==old
    put(vault,{'OPENAI_API_KEY':latest})
    actual=aggregate.import_authorized(backend,{'OPENAI_API_KEY':'old','DEEPSEEK_API_KEY':'recovered'},service='Callimachus',profile='default')
    assert actual=={'OPENAI_API_KEY':latest,'DEEPSEEK_API_KEY':'recovered'}
    assert store.load_secret_values({'OPENAI_API_KEY','DEEPSEEK_API_KEY'})==({'OPENAI_API_KEY':latest,'DEEPSEEK_API_KEY':'recovered'} if latest else {'DEEPSEEK_API_KEY':'recovered'})


def test_allow_once_works_for_the_session_without_a_second_quiet_read(vault,monkeypatch):
    from types import SimpleNamespace
    values={'OPENAI_API_KEY':'one','DEEPSEEK_API_KEY':'two'}
    account=put(vault,values);vault.grants.discard(account)
    original=store._trusted_backend
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(values)=={}
        calls=[]
        def factory():
            backend=original()
            def temporary_grant(service,name):
                assert name==account;calls.append(name)
                return vault.values[name]  # Deliberately no persistent grant.
            backend.get_password=temporary_grant
            return backend
        monkeypatch.setattr(store,'_trusted_backend',factory)
        before=list(vault.reads)
        assert store.recover_macos_credentials()=={'recovered':2,'pending':0}
        assert store.load_secret_values(values)==values
        assert calls==[account]
        assert vault.reads==before
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(values)=={}
        assert store.macos_credential_recovery_state()=={'pending':2}
    assert calls==[account]  # Next startup does not request permission automatically.


@pytest.mark.parametrize('status',[-128,-25293,-25308])
def test_native_identity_and_data_query_denial_is_optional(monkeypatch,status):
    from types import SimpleNamespace
    events=[];queries=[]
    def get_ui(state):state._obj.value=1;return 0
    def set_ui(value):events.append(bool(value));return 0
    def release(_value):pass
    api=SimpleNamespace(
        _sec=SimpleNamespace(SecKeychainGetUserInteractionAllowed=get_ui,SecKeychainSetUserInteractionAllowed=set_ui),
        _found=SimpleNamespace(CFRelease=release),
        create_query=lambda **kw: queries.append(kw) or 123,
        k_=lambda name:name,SecItemCopyMatching=lambda *_args:status,
        error=SimpleNamespace(item_not_found=-25300,keychain_denied=-128,sec_auth_failed=-25293,sec_interaction_not_allowed=-25308),
        Error=SimpleNamespace(raise_for_status=lambda _code:None),
    )
    monkeypatch.setattr(native,'_native_api',lambda:api)
    with pytest.raises(PermissionError):native.read_item('Synthetic','account')
    assert events==[False,True]
    assert len(queries)==1
    assert queries[0]['kSecReturnData'] is True
    assert queries[0]['kSecReturnPersistentRef'] is True
    assert queries[0]['kSecUseAuthenticationUI']=='kSecUseAuthenticationUIFail'


def test_item_list_is_a_pointer_not_a_keyring_integer(monkeypatch):
    from ctypes import c_void_p
    from types import SimpleNamespace
    def create(*_args):return 2**40+7
    released=[]
    api=SimpleNamespace(_found=SimpleNamespace(CFArrayCreate=create,kCFTypeArrayCallBacks=c_void_p(1)))
    monkeypatch.setattr(native,'_make_data',lambda *_args:2**40+3)
    monkeypatch.setattr(native,'_release',lambda _api,item:released.append(item))
    with native._item_list(api,b'synthetic-reference') as items:
        assert isinstance(items,c_void_p)
        assert items.value==2**40+7
    assert released==[2**40+7,2**40+3]
