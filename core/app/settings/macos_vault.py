# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""One protected macOS Keychain item per profile, with explicit legacy import."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

from . import macos_keychain

ACCOUNT_NAME = 'CREDENTIALS_V1'
_MAX_BYTES = 512 * 1024


def account(profile: str) -> str:
    return f'{profile}:{ACCOUNT_NAME}'


def _decode(value: str | None, service: str, profile: str):
    if value is None:
        return None
    from .credentials import is_secret_setting
    if not isinstance(value, str) or len(value.encode('utf-8')) > _MAX_BYTES:
        raise ValueError('invalid protected credential bundle')
    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate protected credential field')
            result[key] = item
        return result
    data = json.loads(value, object_pairs_hook=unique_pairs)
    if (not isinstance(data, dict) or set(data) != {'schema', 'service', 'profile', 'secrets'}
            or type(data['schema']) is not int or data['schema'] != 1
            or data['service'] != service or data['profile'] != profile
            or not isinstance(data['secrets'], dict) or len(data['secrets']) > 512
            or any(not is_secret_setting(name) or not isinstance(item, str)
                   for name, item in data['secrets'].items())):
        raise ValueError('invalid protected credential bundle')
    return data['secrets']


def _encode(values, service, profile):
    result = json.dumps({'schema': 1, 'service': service, 'profile': profile,
                         'secrets': values}, ensure_ascii=False, sort_keys=True)
    _decode(result, service, profile)
    return result


def _read(backend, service, name, *, interactive=False):
    if interactive:
        with macos_keychain.interaction_lock:
            return backend.get_password(service, name)
    return macos_keychain.read_without_ui(service, name)


def bundle(backend, service, profile, *, interactive=False, fresh=False):
    cache = getattr(backend, '_callimachus_bundle_cache', None)
    if cache is None:
        cache = {}; backend._callimachus_bundle_cache = cache
    key = (service, profile)
    if fresh or key not in cache:
        raw, reference = macos_keychain.read_item(service, account(profile), interactive=interactive)
        cache[key] = _decode(raw, service, profile)
        refs = getattr(backend, '_callimachus_bundle_refs', None)
        if refs is None:
            refs = {}; backend._callimachus_bundle_refs = refs
        refs[key] = reference
    return cache[key]


def read_secret(backend, name, *, service, profile, interactive=False):
    values = bundle(backend, service, profile, interactive=interactive)
    if values is not None and name in values:
        # Empty values are tombstones: do not resurrect a cleared legacy key.
        return values[name] or None
    return _read(backend, service, f'{profile}:{name}', interactive=interactive)


def _lock_directory():
    return Path.home() / 'Library/Application Support/Callimachus/keychain-locks'


@contextmanager
def _writer_lock(service: str, profile: str):
    """Serialize aggregate updates across application processes; no secret on disk."""
    import fcntl
    directory = _lock_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    suffix = hashlib.sha256((service + '\0' + profile).encode()).hexdigest()
    fd = os.open(directory / (suffix + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError('credential update lock is not owned by this user')
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_and_verify(backend, service, profile, values):
    encoded = _encode(values, service, profile)
    key = (service, profile)
    reference = backend._callimachus_bundle_refs[key]
    reference = macos_keychain.write_value(service, account(profile), encoded, reference=reference)
    backend._callimachus_bundle_refs[key] = reference
    # Verify that exact item, never another matching record in a second Keychain.
    raw, found = macos_keychain.read_item(service, account(profile), reference=reference)
    actual = _decode(raw, service, profile)
    if actual != values or found != reference:
        raise ValueError('protected credential bundle verification failed')
    backend._callimachus_bundle_cache[key] = actual


def _restore(backend, service, profile, previous):
    key = (service, profile)
    if previous is None:
        reference = backend._callimachus_bundle_refs.get(key)
        if reference is not None:
            macos_keychain.delete_item(service, account(profile), reference)
            raw, _ = macos_keychain.read_item(service, account(profile), reference=reference)
            if raw is not None:
                raise ValueError('new protected bundle was not removed')
        backend._callimachus_bundle_cache[key] = None
        backend._callimachus_bundle_refs[key] = None
    else:
        _write_and_verify(backend, service, profile, previous)


@contextmanager
def update(backend, updates, *, service, profile):
    """Atomic item update; restore after failed verification or paired settings."""
    from .credentials import CredentialStoreError
    with _writer_lock(service, profile):
        try:
            previous = bundle(backend, service, profile, fresh=True)
        except Exception:
            raise CredentialStoreError('Saved credentials need Keychain authorization before they can be changed.') from None
        try:
            after = {**(previous or {}), **updates}
            _write_and_verify(backend, service, profile, after)
        except BaseException as exc:
            try:
                _restore(backend, service, profile, previous)
            except Exception:
                raise CredentialStoreError('The macOS Keychain could not restore the previous credentials.') from None
            if isinstance(exc, Exception):
                raise CredentialStoreError('Saved credentials could not be updated in the macOS Keychain.') from None
            raise
        try:
            yield
        except BaseException:
            try:
                _restore(backend, service, profile, previous)
            except Exception:
                raise CredentialStoreError('The macOS Keychain could not restore the previous credentials.') from None
            raise


def import_authorized(backend, values, *, service, profile):
    """Consolidate keys already authorized through explicit human recovery."""
    if not values:
        return
    # Only exact classified accounts are candidates; never enumerate/delete by label.
    from .credentials import is_secret_setting
    values = {name: value for name, value in values.items()
              if is_secret_setting(name) and isinstance(value, str) and value}
    from .credentials import CredentialStoreError
    with _writer_lock(service, profile):
        try:
            previous = bundle(backend, service, profile, fresh=True)
            # A concurrent writer's values and deletion tombstones take priority.
            imported = {name: value for name, value in values.items()
                        if name not in (previous or {})}
            current = {**(previous or {}), **imported}
            if imported:
                try:
                    _write_and_verify(backend, service, profile, current)
                except BaseException:
                    _restore(backend, service, profile, previous)
                    raise
        except Exception:
            raise CredentialStoreError('Saved credentials could not be imported into the macOS Keychain.') from None
        # No transaction in another instance can still roll back while cleanup runs.
        for name, value in values.items():
            if current.get(name) != value:
                continue
            legacy = f'{profile}:{name}'
            try:
                if _read(backend, service, legacy) == value:
                    macos_keychain.delete_if_matches(service, legacy, value)
            except Exception:
                continue  # Uncertain ownership/readability preserves the old entry.
        return current
