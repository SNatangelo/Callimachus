# core/app/settings/credentials.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed access to the operating system credential store."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import importlib
import os
import platform
import re
from threading import RLock
from collections.abc import Iterator, Mapping
from pathlib import Path


_SERVICE_NAME = "Callimachus"
_SECRET_SUFFIXES = (
    "_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN",
    "_SECRET", "_PASSWORD", "_CREDENTIAL",
)
_SECRET_NAMES = {"CITATION_VERIFIER_SIGNING_KEY"}
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*="
)


class CredentialStoreError(RuntimeError):
    """A user-safe failure while reading or writing saved credentials."""


@dataclass
class _StartupSecretSnapshot:
    profile_scope: str
    values: dict[str, str | None]


_STARTUP_SECRET_SNAPSHOT: ContextVar[_StartupSecretSnapshot | None] = ContextVar(
    "callimachus_startup_secret_snapshot", default=None,
)



class _MacOSAccessRequired(CredentialStoreError):
    """An existing Keychain item needs explicit user authorization."""


@dataclass
class _MacOSSecretSession:
    profile_scope: str
    values: dict[str, str | None] = field(default_factory=dict, repr=False)
    blocked: set[str] = field(default_factory=set, repr=False)
    lock: object = field(default_factory=RLock, repr=False)
    active: bool = True


_MACOS_SECRET_SESSION: _MacOSSecretSession | None = None


def _macos_session() -> _MacOSSecretSession | None:
    session = _MACOS_SECRET_SESSION
    return session if session is not None and session.profile_scope == _profile_scope() else None


@contextmanager
def _macos_desktop_secret_session():
    """Share authorized reads across GUI workers, for one desktop lifetime only."""
    global _MACOS_SECRET_SESSION
    if _MACOS_SECRET_SESSION is not None:
        raise RuntimeError("a macOS credential session is already active")
    session = _MacOSSecretSession(_profile_scope())
    _MACOS_SECRET_SESSION = session
    try:
        yield session
    finally:
        with session.lock:
            session.active = False
            session.values.clear()
            session.blocked.clear()
            _MACOS_SECRET_SESSION = None


def macos_credential_recovery_state() -> dict[str, int]:
    session = _macos_session()
    if session is None:
        return {"pending": 0}
    with session.lock:
        return {"pending": len(session.blocked) if session.active else 0}


def recover_macos_credentials() -> dict[str, int]:
    """Ask macOS only following consent; retain partial grants, stop on refusal."""
    session = _macos_session()
    if session is None:
        return {"recovered": 0, "pending": 0}
    recovered = 0
    with session.lock:
        if not session.active:
            raise CredentialStoreError("The credential session has ended.")
        backend = _trusted_backend()
        for name in sorted(session.blocked):
            try:
                # Native Always Allow records this app's identity on the item.
                value = _read_value(backend, name, profile_scope=session.profile_scope, interactive=True)
            except CredentialStoreError:
                break  # Deny/Cancel must not open another chain of dialogs.
            session.values[name] = value
            session.blocked.discard(name)
            recovered += int(value is not None)
        if recovered:
            from .macos_vault import import_authorized
            authorized_bundle = getattr(backend, "_callimachus_bundle_cache", {}).get((_SERVICE_NAME, session.profile_scope))
            legacy_values = {name: value for name, value in session.values.items()
                             if value is not None and name not in (authorized_bundle or {})}
            if legacy_values:
                actual = import_authorized(backend, legacy_values, service=_SERVICE_NAME, profile=session.profile_scope)
            else:
                # A one-time native grant is sufficient for this GUI session;
                # do not demand a second quiet read or rewrite to persist the grant.
                actual = authorized_bundle or {}
            for name, value in actual.items():
                session.values[name] = value or None
                session.blocked.discard(name)
        return {"recovered": recovered, "pending": len(session.blocked)}


def is_secret_setting(name: str) -> bool:
    return isinstance(name, str) and (
        name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)
    )


def secret_names_from_template(path: str | os.PathLike[str]) -> set[str]:
    """Return secret setting names declared by the current settings template."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        raise CredentialStoreError(
            "The current settings template is unavailable."
        ) from None
    names = {
        match.group("name")
        for line in lines
        if (match := _ASSIGNMENT.match(line)) is not None
        and is_secret_setting(match.group("name"))
    }
    names.add("CITATION_VERIFIER_SIGNING_KEY")
    return names


def secret_names_in_file(path: str | os.PathLike[str]) -> set[str]:
    """Identify secret assignment names without returning their values."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    except OSError:
        raise CredentialStoreError(
            "The current settings file could not be read."
        ) from None
    return {
        match.group("name")
        for line in lines
        if (match := _ASSIGNMENT.match(line)) is not None
        and is_secret_setting(match.group("name"))
    }


def _backend_for_system(system: str):
    """Instantiate only the built-in backend for this operating system."""
    backends = {
        "Windows": ("keyring.backends.Windows", "WinVaultKeyring"),
        "Darwin": ("keyring.backends.macOS", "Keyring"),
        "Linux": ("keyring.backends.SecretService", "Keyring"),
    }
    selected = backends.get(system)
    if selected is None:
        raise CredentialStoreError(
            "The operating system credential store is unavailable on this platform."
        )
    module_name, class_name = selected
    try:
        backend_type = getattr(importlib.import_module(module_name), class_name)
        backend = backend_type()
        if not all(callable(getattr(backend, name, None)) for name in (
            "get_password", "set_password", "delete_password",
        )):
            raise TypeError("unsupported credential backend")
        return backend
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError(
            "The operating system credential store is unavailable or locked."
        ) from None


def _trusted_backend():
    try:
        system = platform.system()
    except Exception:
        raise CredentialStoreError(
            "The operating system credential store is unavailable."
        ) from None
    return _backend_for_system(system)


def _profile_scope() -> str:
    """Keep separate explicit data-directory profiles in separate vault entries."""
    configured = os.environ.get("CALLIMACHUS_DATA_DIR", "").strip()
    if not configured:
        return "default"
    normalized = os.path.normcase(
        os.path.abspath(os.path.expanduser(configured))
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"profile-{digest}"


@contextmanager
def _capture_startup_secret_snapshot() -> Iterator[_StartupSecretSnapshot]:
    """Capture vault reads made while the frozen launcher loads its settings."""
    snapshot = _StartupSecretSnapshot(_profile_scope(), {})
    token = _STARTUP_SECRET_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _STARTUP_SECRET_SNAPSHOT.reset(token)


@contextmanager
def _use_startup_secret_snapshot(
    snapshot: _StartupSecretSnapshot,
) -> Iterator[None]:
    """Reuse one captured snapshot only across the initial desktop construction."""
    token = _STARTUP_SECRET_SNAPSHOT.set(snapshot)
    try:
        yield
    finally:
        _STARTUP_SECRET_SNAPSHOT.reset(token)


def _invalidate_startup_secret_snapshot(names) -> None:
    snapshot = _STARTUP_SECRET_SNAPSHOT.get()
    if snapshot is not None and snapshot.profile_scope == _profile_scope():
        for name in names:
            snapshot.values.pop(name, None)
    session = _macos_session()
    if session is not None:
        with session.lock:
            for name in names:
                session.values.pop(name, None)
                session.blocked.discard(name)


def _username(name: str, *, profile_scope: str | None = None) -> str:
    scope = _profile_scope() if profile_scope is None else profile_scope
    return f"{scope}:{name}"


def _backend_operation(backend, method: str, *args):
    # Do not allow a concurrent quiet read to suppress an explicit consent/write dialog.
    if platform.system() == "Darwin":
        from .macos_keychain import interaction_lock
        with interaction_lock:
            return getattr(backend, method)(*args)
    return getattr(backend, method)(*args)


def _read_value(
    backend, name: str, *, profile_scope: str | None = None, interactive: bool = False,
) -> str | None:
    try:
        if platform.system() == "Darwin":
            from .macos_vault import read_secret
            value = read_secret(backend, name, service=_SERVICE_NAME,
                                profile=_profile_scope() if profile_scope is None else profile_scope,
                                interactive=interactive)
        else:
            value = _backend_operation(backend, "get_password",
                _SERVICE_NAME, _username(name, profile_scope=profile_scope),
            )
    except PermissionError:
        if platform.system() == "Darwin" and not interactive:
            raise _MacOSAccessRequired("Saved credentials need Keychain authorization.") from None
        raise CredentialStoreError("Saved credentials could not be read from the operating system credential store.") from None
    except Exception:
        raise CredentialStoreError(
            "Saved credentials could not be read from the operating system credential store."
        ) from None
    if value is not None and not isinstance(value, str):
        raise CredentialStoreError(
            "The operating system credential store returned an invalid credential."
        )
    return value


def _load_secret_values_with_startup_snapshot(names) -> dict[str, str]:
    """Read only classified secrets from the selected operating system vault."""
    requested = sorted({name for name in names if is_secret_setting(name)})
    if not requested:
        return {}
    profile_scope = _profile_scope()
    snapshot = _STARTUP_SECRET_SNAPSHOT.get()
    if snapshot is not None and snapshot.profile_scope == profile_scope:
        resolved = {
            name: snapshot.values[name]
            for name in requested if name in snapshot.values
        }
    else:
        resolved = {}
    pending = [name for name in requested if name not in resolved]
    values = {
        name: value for name, value in resolved.items() if value is not None
    }
    if not pending:
        return values

    backend = _trusted_backend()
    for name in pending:
        try:
            value = _read_value(backend, name, profile_scope=profile_scope)
        except _MacOSAccessRequired:
            value = None  # A child launched after refusal also continues without this key.
        if snapshot is not None and snapshot.profile_scope == profile_scope:
            snapshot.values[name] = value
        if value is not None:
            values[name] = value
    return values



def load_secret_values(names) -> dict[str, str]:
    """Load only secrets; macOS GUI workers share one synchronized session."""
    requested = sorted({name for name in names if is_secret_setting(name)})
    session = _macos_session()
    if session is None:
        return _load_secret_values_with_startup_snapshot(requested)
    with session.lock:
        if not session.active:
            raise CredentialStoreError("The credential session has ended.")
        pending = [name for name in requested if name not in session.values]
        if pending:
            backend = _trusted_backend()
            for name in pending:
                try:
                    value = _read_value(backend, name, profile_scope=session.profile_scope)
                except _MacOSAccessRequired:
                    session.blocked.add(name)
                    value = None
                session.values[name] = value
        return {name: session.values[name] for name in requested if session.values[name] is not None}


def _restore(backend, previous: Mapping[str, str | None], names: list[str]) -> bool:
    try:
        for name in reversed(names):
            expected = previous[name]
            current = _read_value(backend, name)
            if expected is None:
                if current is not None:
                    _backend_operation(backend, "delete_password", _SERVICE_NAME, _username(name))
            elif current != expected:
                _backend_operation(backend, "set_password", _SERVICE_NAME, _username(name), expected)
            if _read_value(backend, name) != expected:
                return False
    except Exception:
        return False
    return True


@contextmanager
def _update_secret_values(updates: Mapping[str, str]) -> Iterator[None]:
    """Apply and verify vault changes, rolling them back if the paired file write fails."""
    if not isinstance(updates, Mapping):
        raise CredentialStoreError("The credential update is invalid.")
    normalized: dict[str, str] = {}
    for name, value in updates.items():
        if not is_secret_setting(name) or type(value) is not str:
            raise CredentialStoreError("The credential update is invalid.")
        normalized[name] = value
    if not normalized:
        yield
        return

    _invalidate_startup_secret_snapshot(normalized)
    backend = _trusted_backend()
    if platform.system() == "Darwin":
        from .macos_vault import update
        try:
            with update(backend, normalized, service=_SERVICE_NAME, profile=_profile_scope()):
                yield
        finally:
            _invalidate_startup_secret_snapshot(normalized)
        return
    previous = {name: _read_value(backend, name) for name in normalized}
    changed: list[str] = []
    try:
        for name, value in normalized.items():
            changed.append(name)
            if value:
                try:
                    _backend_operation(backend, "set_password", _SERVICE_NAME, _username(name), value)
                except Exception:
                    raise CredentialStoreError(
                        "Saved credentials could not be updated in the operating system credential store."
                    ) from None
                expected = value
            else:
                if previous[name] is not None:
                    try:
                        _backend_operation(backend, "delete_password", _SERVICE_NAME, _username(name))
                    except Exception:
                        raise CredentialStoreError(
                            "Saved credentials could not be updated in the operating system credential store."
                        ) from None
                expected = None
            if _read_value(backend, name) != expected:
                raise CredentialStoreError(
                    "Saved credentials could not be verified in the operating system credential store."
                )
    except Exception as exc:
        if not _restore(backend, previous, changed):
            raise CredentialStoreError(
                "The credential store could not restore the previous settings."
            ) from None
        if isinstance(exc, CredentialStoreError):
            raise
        raise CredentialStoreError(
            "Saved credentials could not be updated in the operating system credential store."
        ) from None

    try:
        yield
    except BaseException:
        if not _restore(backend, previous, changed):
            raise CredentialStoreError(
                "Settings could not be saved because the credential store could not restore previous credentials."
            ) from None
        raise
    finally:
        _invalidate_startup_secret_snapshot(normalized)


@contextmanager
def update_secret_values(updates: Mapping[str, str]) -> Iterator[None]:
    """Exclude GUI readers until both the vault and paired settings commit/rollback."""
    session = _macos_session()
    with session.lock if session is not None else nullcontext():
        if session is not None and not session.active:
            raise CredentialStoreError("The credential session has ended.")
        with _update_secret_values(updates):
            yield


__all__ = [
    "CredentialStoreError",
    "is_secret_setting",
    "load_secret_values",
    "secret_names_in_file",
    "secret_names_from_template",
    "update_secret_values",
]
