# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""A non-interactive macOS Keychain read using the bundled native backend."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from threading import RLock


# The legacy Keychain UI flag is process-wide, so all native operations share this lock.
interaction_lock = RLock()


def _native_api():
    from keyring.backends.macOS import api
    return api


@contextmanager
def _without_legacy_ui(api):
    from ctypes import POINTER, byref, c_int32, c_ubyte

    get = api._sec.SecKeychainGetUserInteractionAllowed
    get.argtypes = (POINTER(c_ubyte),)
    get.restype = c_int32
    set_ = api._sec.SecKeychainSetUserInteractionAllowed
    set_.argtypes = (c_ubyte,)
    set_.restype = c_int32
    previous = c_ubyte()
    api.Error.raise_for_status(get(byref(previous)))
    api.Error.raise_for_status(set_(False))
    try:
        yield
    finally:
        api.Error.raise_for_status(set_(previous.value))


def read_without_ui(service: str, account: str) -> str | None:
    # Imported only on macOS, never on source/Windows/Linux startup.
    api = _native_api()
    with interaction_lock, _without_legacy_ui(api):
        return _read(api, service, account)


def _read(api, service: str, account: str, *, item_list=None) -> str | None:
    from ctypes import byref, c_void_p

    extra = {"kSecMatchItemList": item_list} if item_list is not None else {}
    query = api.create_query(
        **extra,
        kSecClass=api.k_("kSecClassGenericPassword"),
        kSecMatchLimit=api.k_("kSecMatchLimitOne"),
        kSecAttrService=service,
        kSecAttrAccount=account,
        kSecReturnData=True,
        kSecUseAuthenticationUI=api.k_("kSecUseAuthenticationUIFail"),
    )
    data = c_void_p()
    release = api._found.CFRelease
    release.argtypes = (c_void_p,)
    release.restype = None
    try:
        status = api.SecItemCopyMatching(query, byref(data))
        if status == api.error.item_not_found:
            return None
        if status in {api.error.keychain_denied, api.error.sec_auth_failed, api.error.sec_interaction_not_allowed}:
            raise PermissionError("Keychain authorization required")
        api.Error.raise_for_status(status)
        return api.cfstr_to_str(data)
    finally:
        if data.value:
            release(data)
        release(query)



def _release(api, value):
    from ctypes import c_void_p
    if value:
        release = api._found.CFRelease
        release.argtypes = (c_void_p,); release.restype = None
        release(value)


def _make_data(api, raw):
    from ctypes import POINTER, c_ssize_t, c_ubyte, c_void_p
    create = api._found.CFDataCreate
    create.argtypes = (c_void_p, POINTER(c_ubyte), c_ssize_t); create.restype = c_void_p
    value = create(None, (c_ubyte * len(raw)).from_buffer_copy(raw), len(raw))
    if not value:
        raise MemoryError("Keychain data allocation failed")
    return value


def _data_bytes(api, value):
    from ctypes import string_at
    return string_at(api.CFDataGetBytePtr(value), api.CFDataGetLength(value))


@contextmanager
def _item_list(api, reference):
    from ctypes import POINTER, c_ssize_t, c_void_p
    data = _make_data(api, reference); items = None
    try:
        create = api._found.CFArrayCreate
        create.argtypes = (c_void_p, POINTER(c_void_p), c_ssize_t, c_void_p)
        create.restype = c_void_p
        items = create(None, (c_void_p * 1)(data), 1, api._found.kCFTypeArrayCallBacks)
        if not items:
            raise MemoryError("Keychain item allocation failed")
        yield c_void_p(items)
    finally:
        _release(api, items); _release(api, data)


def read_item(service, account, *, interactive=False, reference=None):
    """Read data and persistent identity in ONE query, including Allow Once."""
    from ctypes import byref, c_void_p
    api = _native_api()
    with interaction_lock, (nullcontext() if interactive else _without_legacy_ui(api)):
        with (_item_list(api, reference) if reference is not None else nullcontext(None)) as items:
            extras = {"kSecMatchItemList": items} if items is not None else {}
            query = api.create_query(
                **extras,
                kSecClass=api.k_("kSecClassGenericPassword"),
                kSecAttrService=service, kSecAttrAccount=account,
                kSecMatchLimit=api.k_("kSecMatchLimitOne"),
                kSecReturnData=True, kSecReturnPersistentRef=True,
                kSecUseAuthenticationUI=api.k_("kSecUseAuthenticationUIAllow" if interactive else "kSecUseAuthenticationUIFail"),
            )
            result = c_void_p()
            try:
                status = api.SecItemCopyMatching(query, byref(result))
                if status == api.error.item_not_found:
                    return None, None
                if status in {api.error.keychain_denied, api.error.sec_auth_failed, api.error.sec_interaction_not_allowed}:
                    raise PermissionError("Keychain authorization required")
                api.Error.raise_for_status(status)
                get = api._found.CFDictionaryGetValue
                get.argtypes = (c_void_p, c_void_p); get.restype = c_void_p
                data = get(result, api.k_("kSecValueData"))
                found = get(result, api.k_("kSecValuePersistentRef"))
                if not data or not found:
                    raise ValueError("Keychain result omitted data or persistent identity")
                return api.cfstr_to_str(data), _data_bytes(api, found)
            finally:
                _release(api, result.value); _release(api, query)


def write_value(service, account, value, *, reference=None):
    """Create or atomically update only the item whose identity was read."""
    from ctypes import byref, c_int32, c_void_p
    api = _native_api()
    with interaction_lock:
        data = _make_data(api, value.encode("utf-8"))
        query = None; changes = None; returned = c_void_p()
        try:
            if reference is None:
                query = api.create_query(
                    kSecClass=api.k_("kSecClassGenericPassword"),
                    kSecAttrService=service, kSecAttrAccount=account,
                    kSecValueData=c_void_p(data), kSecReturnPersistentRef=True,
                )
                api.Error.raise_for_status(api.SecItemAdd(query, byref(returned)))
                return _data_bytes(api, returned)
            with _item_list(api, reference) as items:
                query = api.create_query(
                    kSecClass=api.k_("kSecClassGenericPassword"),
                    kSecAttrService=service, kSecAttrAccount=account,
                    kSecMatchItemList=items,
                )
                changes = api.create_query(kSecValueData=c_void_p(data))
                update = api._sec.SecItemUpdate
                update.argtypes = (c_void_p, c_void_p); update.restype = c_int32
                api.Error.raise_for_status(update(query, changes))
            return reference
        finally:
            for item in (returned.value, changes, query, data):
                _release(api, item)


def delete_item(service, account, reference):
    """Remove only a known persistent item; absence/denial cannot broaden lookup."""
    if not reference:
        raise ValueError("an exact Keychain item reference is required")
    api = _native_api()
    with interaction_lock, _without_legacy_ui(api), _item_list(api, reference) as items:
        query = api.create_query(
            kSecClass=api.k_("kSecClassGenericPassword"),
            kSecAttrService=service, kSecAttrAccount=account, kSecMatchItemList=items,
            kSecUseAuthenticationUI=api.k_("kSecUseAuthenticationUIFail"),
        )
        try:
            status = api.SecItemDelete(query)
            if status != api.error.item_not_found:
                api.Error.raise_for_status(status)
        finally:
            _release(api, query)


def delete_if_matches(service, account, expected):
    """Delete one recovered legacy record, never all namesakes in other Keychains."""
    value, reference = read_item(service, account)
    if reference is None or value != expected:
        return False
    delete_item(service, account, reference)
    return True
