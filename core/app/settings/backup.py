# core/app/settings/backup.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Portable export, import, and reset for the desktop's editable settings."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.app.settings.config import _ASSIGNMENT, _dotenv_values, load_gui_settings_inventory
from core.app.settings.credentials import (
    CredentialStoreError,
    is_secret_setting,
    load_secret_values,
    update_secret_values,
)
from core.gui.settings.preferences import default_preferences, validate_preferences


_FORMAT = "callimachus-settings-backup"
_VERSION = 1
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_ENV_VALUE_CHARS = 8192
_MAX_PASSWORD_BYTES = 1024
_KDF = {
    "algorithm": "argon2id",
    "length": 32,
    "iterations": 3,
    "lanes": 4,
    "memory_cost_kib": 65536,
}
_AAD = b"Callimachus settings backup\x00version=1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_CHOICE_NAMES = frozenset({
    "CITATION_VERIFIER_MODEL",
    "CITATION_VERIFIER_VERIFY_BACKENDS",
    "CITATION_VERIFIER_VERIFY_JURY1_ONLY",
    "CITATION_VERIFIER_VERIFY_JURY2_ONLY",
})
_PRESERVE_SUFFIXES = (
    "_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN",
)
_ENDPOINT_SUFFIXES = ("_BASE_URL", "_ENDPOINT", "_HOST")
_PROVIDER_URL_NAMES = frozenset({"BIBLIO_GLUTTON_URL"})


class SettingsTransferError(ValueError):
    """A user-safe, non-secret settings transfer failure."""


def transfer_settings(
    action: str,
    payload: Mapping[str, object],
    *,
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    inventory_loader: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    environment_loader: Callable[[], Mapping[str, str]] | None = None,
) -> dict[str, object]:
    """Run one explicit backup action and return only safe UI status data."""
    try:
        if action not in {"inspect", "export", "import", "reset"}:
            raise SettingsTransferError("Unsupported settings action.")
        if not isinstance(payload, Mapping):
            raise SettingsTransferError("Invalid settings request.")
        if action == "inspect":
            encrypted = _inspect_backup_file(payload)
            return {"ok": True, "action": "inspect", "encrypted": encrypted}
        template = _read_template(template_path)
        editable = _editable_names(
            env_path, template_path, template,
            inventory_loader=inventory_loader,
        )
        if action == "export":
            _export(
                payload, env_path, template_path, template, editable,
                environment_loader=environment_loader,
            )
            return {"ok": True, "action": "export"}
        if action == "import":
            preferences = _import(
                payload, env_path, template_path, template, editable,
            )
            return {
                "ok": True, "action": "import", "preferences": preferences,
                "environment_names": sorted(editable),
            }
        preferences = _reset(
            payload, env_path, template_path, template, editable,
            environment_loader=environment_loader,
        )
        return {
            "ok": True, "action": "reset", "preferences": preferences,
            "environment_names": sorted(editable),
        }
    except SettingsTransferError as exc:
        return {"ok": False, "error": str(exc)}
    except CredentialStoreError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        # Filesystem, JSON, crypto, and platform errors can contain paths,
        # credentials, or ciphertext context. Keep them out of the GUI result.
        return {"ok": False, "error": "Settings could not be transferred."}


def _export(
    request: Mapping[str, object],
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    template: Mapping[str, str],
    editable: frozenset[str],
    *,
    environment_loader: Callable[[], Mapping[str, str]] | None,
) -> None:
    destination = _validated_path(request.get("path"))
    _ensure_distinct_backup_target(destination, env_path, template_path)
    if "password" not in request:
        raise SettingsTransferError("Invalid settings request.")
    password = _password(request.get("password"), allow_none=True)
    current = _current_editable_values(
        env_path, editable, environment_loader=environment_loader,
    )
    environment = {
        name: _valid_env_value(current.get(name, template.get(name, "")))
        for name in sorted(editable)
    }
    raw_preferences = request.get("preferences", {})
    if not isinstance(raw_preferences, Mapping):
        raise SettingsTransferError("Application preferences are invalid.")
    try:
        preferences = validate_preferences(raw_preferences, ignore_unknown=True)
    except (TypeError, ValueError):
        raise SettingsTransferError("Application preferences are invalid.") from None
    payload = {"environment": environment, "preferences": preferences}
    encoded = _encode_backup(payload, password)
    _atomic_write(destination, encoded)


def _import(
    request: Mapping[str, object],
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    template: Mapping[str, str],
    editable: frozenset[str],
) -> dict[str, object]:
    source = _validated_path(request.get("path"))
    if "password" not in request:
        raise SettingsTransferError("Invalid settings request.")
    password = _password(request.get("password"), allow_none=True)
    raw = _read_bounded(source)
    document = _parse_json(raw)
    payload = _decode_backup(document, password)
    environment = payload.get("environment")
    if not isinstance(environment, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    updates = _validate_import_environment(environment, editable)
    raw_preferences = payload.get("preferences", {})
    if not isinstance(raw_preferences, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    preferences_to_apply = {
        name: value for name, value in raw_preferences.items()
        if isinstance(name, str) and name in default_preferences()
    }
    try:
        preferences = validate_preferences(preferences_to_apply)
    except (TypeError, ValueError):
        raise SettingsTransferError("The settings file is invalid or damaged.") from None

    # Resolve a complete current-template baseline before touching the target.
    # The live inventory decides which values the GUI may import; all other
    # template values stay at their versioned defaults.
    complete = dict(template)
    complete.update(updates)
    secret_names = {
        name for name in template if _is_secret(name)
    } | {name for name in editable if _is_secret(name)}
    _write_settings_state(
        env_path, template_path, template, complete, secret_names,
    )
    return preferences


def _reset(
    request: Mapping[str, object],
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    template: Mapping[str, str],
    editable: frozenset[str],
    *,
    environment_loader: Callable[[], Mapping[str, str]] | None,
) -> dict[str, object]:
    keep = request.get("keep_api_keys")
    if type(keep) is not bool:
        raise SettingsTransferError("Invalid settings request.")
    complete = dict(template)
    if keep:
        current = _current_editable_values(
            env_path, editable, environment_loader=environment_loader,
        )
        for name in editable:
            if _is_preserved_endpoint_or_key(name) and name in current:
                complete[name] = _valid_env_value(current[name])
    # Reset model and role choices even when a template later gains a nonempty
    # sample value. Per-provider model fields are detected from live names.
    secret_names = {
        name for name in template if _is_secret(name)
    } | {name for name in editable if _is_secret(name)}
    for name in secret_names:
        if name not in template:
            complete[name] = ""
    for name in template:
        if name.endswith("_MODEL") or name in _MODEL_CHOICE_NAMES:
            complete[name] = ""
        if not keep and _is_secret(name):
            complete[name] = ""
    _write_settings_state(
        env_path, template_path, template, complete, secret_names,
    )
    return default_preferences()


def _read_template(path: str | os.PathLike[str]) -> dict[str, str]:
    values = _dotenv_values(path)
    if not values:
        raise SettingsTransferError("The current settings template is unavailable.")
    if any(not _ENV_NAME.fullmatch(name) for name in values):
        raise SettingsTransferError("The current settings template is invalid.")
    return values


def _editable_names(
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    template: Mapping[str, str],
    *,
    inventory_loader: Callable[[], Sequence[Mapping[str, object]]] | None,
) -> frozenset[str]:
    if inventory_loader is None:
        rows = load_gui_settings_inventory(
            env_path=env_path, template_path=template_path, environ={},
        )
    else:
        rows = inventory_loader()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise SettingsTransferError("The current settings inventory is unavailable.")
    row_names: set[str] = set()
    locked_names: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if isinstance(name, str):
            row_names.add(name)
            if row.get("locked") is True:
                locked_names.add(name)
    # Use only fields that still exist in both the versioned template and the
    # current inventory. This keeps exports portable across app versions.
    known = set(template) & row_names
    from core.app.runtime_paths import is_frozen

    if is_frozen():
        known.add("CITATION_VERIFIER_SIGNING_KEY")
    if not known:
        raise SettingsTransferError("The current settings inventory is unavailable.")
    return frozenset(known - locked_names)


def _read_dotenv(path: str | os.PathLike[str]) -> dict[str, str]:
    try:
        values = _dotenv_values(path)
        from core.app.runtime_paths import is_frozen

        if is_frozen():
            values = {
                name: value for name, value in values.items()
                if not _is_secret(name)
            }
        return values
    except Exception:
        raise SettingsTransferError("The current settings file could not be read.") from None


def _current_editable_values(
    env_path: str | os.PathLike[str],
    editable: frozenset[str],
    *,
    environment_loader: Callable[[], Mapping[str, str]] | None,
) -> dict[str, str]:
    values = _read_dotenv(env_path)
    from core.app.runtime_paths import is_frozen

    if is_frozen():
        names = {name for name in editable if _is_secret(name)}
        from core.app.runtime_paths import process_environment_without_dotenv

        values.update(load_secret_values(
            names - set(process_environment_without_dotenv())
        ))
    if environment_loader is None:
        return values
    snapshot = environment_loader()
    if not isinstance(snapshot, Mapping):
        raise SettingsTransferError("The current settings environment is unavailable.")
    # Snapshot only the live, versioned editable names. This preserves values
    # supplied by the process environment without exporting unrelated variables.
    for name in editable:
        if name in snapshot:
            values[name] = _valid_env_value(snapshot[name])
    return values


def _is_secret(name: str) -> bool:
    return is_secret_setting(name)


def _write_settings_state(
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
    template: Mapping[str, str],
    values: Mapping[str, str],
    secret_names: set[str],
) -> None:
    from core.app.runtime_paths import is_frozen

    if not is_frozen():
        _atomic_write(
            Path(env_path),
            _render_template(template_path, values).encode("utf-8"),
        )
        return

    vault_updates = {
        name: _valid_env_value(values.get(name, ""))
        for name in secret_names if _is_secret(name)
    }
    file_values = {
        name: _valid_env_value(values.get(name, template[name]))
        for name in template
    }
    for name in template:
        if _is_secret(name):
            file_values[name] = ""
    output = _render_template(template_path, file_values).encode("utf-8")
    with update_secret_values(vault_updates):
        _atomic_write(Path(env_path), output)


def _validate_import_environment(
    environment: Mapping[object, object], editable: frozenset[str],
) -> dict[str, str]:
    updates: dict[str, str] = {}
    for name, value in environment.items():
        if not isinstance(name, str) or name not in editable:
            # Unknown and removed fields are intentionally ignored.
            continue
        updates[name] = _valid_env_value(value)
    return updates


def _valid_env_value(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > _MAX_ENV_VALUE_CHARS
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise SettingsTransferError("A settings value is invalid or too long.")
    return value


def _validated_path(value: object) -> Path:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 4096
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise SettingsTransferError("Choose a valid settings file.")
    try:
        return Path(value).expanduser()
    except (TypeError, ValueError, OSError):
        raise SettingsTransferError("Choose a valid settings file.") from None


def _ensure_distinct_backup_target(
    target: Path,
    env_path: str | os.PathLike[str],
    template_path: str | os.PathLike[str],
) -> None:
    try:
        resolved = target.resolve(strict=False)
        protected = {
            Path(env_path).resolve(strict=False),
            Path(template_path).resolve(strict=False),
        }
    except (OSError, RuntimeError):
        raise SettingsTransferError("Choose a valid settings file.") from None
    if resolved in protected:
        raise SettingsTransferError("Choose a separate backup file.")


def _password(value: object, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or not value or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
    ):
        raise SettingsTransferError("The backup password is invalid.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SettingsTransferError("The backup password is invalid.") from None
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise SettingsTransferError("The backup password is invalid.")
    return value


def _encode_backup(payload: Mapping[str, object], password: str | None) -> bytes:
    plain = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(plain) > _MAX_FILE_BYTES:
        raise SettingsTransferError("The settings are too large to export.")
    if password is None:
        document: dict[str, object] = {
            "format": _FORMAT, "version": _VERSION, "encrypted": False,
            "payload": payload,
        }
    else:
        if len(password) < 6:
            raise SettingsTransferError("Choose a password with at least 6 characters.")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
        except ImportError:
            raise SettingsTransferError(
                "Encrypted backups are unavailable because required cryptography support is missing."
            ) from None
        salt = os.urandom(16)
        nonce = os.urandom(12)
        try:
            key = Argon2id(
                salt=salt, length=_KDF["length"],
                iterations=_KDF["iterations"], lanes=_KDF["lanes"],
                memory_cost=_KDF["memory_cost_kib"],
            ).derive(password.encode("utf-8"))
            ciphertext = AESGCM(key).encrypt(nonce, plain, _AAD)
        except Exception:
            raise SettingsTransferError("Encrypted backup could not be created.") from None
        document = {
            "format": _FORMAT,
            "version": _VERSION,
            "encrypted": True,
            "kdf": {**_KDF, "salt": _b64(salt)},
            "cipher": {
                "algorithm": "aes-256-gcm",
                "nonce": _b64(nonce),
                "ciphertext": _b64(ciphertext),
            },
        }
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise SettingsTransferError("The settings are too large to export.")
    return encoded


def _decode_backup(document: object, password: str | None) -> Mapping[str, object]:
    if not isinstance(document, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if (
        document.get("format") != _FORMAT
        or type(document.get("version")) is not int
        or document.get("version") != _VERSION
    ):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    encrypted = document.get("encrypted")
    if encrypted is False:
        if set(document) != {"format", "version", "encrypted", "payload"}:
            raise SettingsTransferError("The settings file is invalid or damaged.")
        if password is not None:
            raise SettingsTransferError("The backup does not use a password.")
        payload = document.get("payload")
    elif encrypted is True:
        if set(document) != {"format", "version", "encrypted", "kdf", "cipher"}:
            raise SettingsTransferError("The settings file is invalid or damaged.")
        if password is None:
            raise SettingsTransferError("This backup requires its password.")
        payload = _decrypt(document, password)
    else:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if not isinstance(payload, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    return payload


def _inspect_backup_file(request: Mapping[str, object]) -> bool:
    source = _validated_path(request.get("path"))
    document = _parse_json(_read_bounded(source))
    if not isinstance(document, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if (
        document.get("format") != _FORMAT
        or type(document.get("version")) is not int
        or document.get("version") != _VERSION
    ):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    encrypted = document.get("encrypted")
    if encrypted is False:
        if set(document) != {"format", "version", "encrypted", "payload"}:
            raise SettingsTransferError("The settings file is invalid or damaged.")
        payload = document.get("payload")
        if (
            not isinstance(payload, Mapping)
            or not isinstance(payload.get("environment"), Mapping)
            or not isinstance(payload.get("preferences", {}), Mapping)
        ):
            raise SettingsTransferError("The settings file is invalid or damaged.")
    elif encrypted is True:
        if set(document) != {"format", "version", "encrypted", "kdf", "cipher"}:
            raise SettingsTransferError("The settings file is invalid or damaged.")
        _validate_encrypted_metadata(document)
    else:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    return encrypted


def _decrypt(document: Mapping[str, object], password: str) -> object:
    salt, nonce, ciphertext = _validate_encrypted_metadata(document)
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    except ImportError:
        raise SettingsTransferError(
            "Encrypted backups are unavailable because required cryptography support is missing."
        ) from None
    try:
        key = Argon2id(
            salt=salt, length=_KDF["length"],
            iterations=_KDF["iterations"], lanes=_KDF["lanes"],
            memory_cost=_KDF["memory_cost_kib"],
        ).derive(password.encode("utf-8"))
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        decoded = _parse_json(plaintext)
    except InvalidTag:
        raise SettingsTransferError("The password is incorrect or the backup is damaged.") from None
    except SettingsTransferError:
        raise
    except Exception:
        raise SettingsTransferError("The password is incorrect or the backup is damaged.") from None
    return decoded


def _validate_encrypted_metadata(
    document: Mapping[str, object],
) -> tuple[bytes, bytes, bytes]:
    kdf = document.get("kdf")
    cipher = document.get("cipher")
    if not isinstance(kdf, Mapping) or not isinstance(cipher, Mapping):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    # Strict fixed parameters prevent attacker-controlled Argon2 resource use.
    if set(kdf) != {"algorithm", "salt", "length", "iterations", "lanes", "memory_cost_kib"}:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if set(cipher) != {"algorithm", "nonce", "ciphertext"}:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if any(kdf.get(name) != expected for name, expected in _KDF.items()):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if kdf.get("algorithm") != _KDF["algorithm"]:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if cipher.get("algorithm") != "aes-256-gcm":
        raise SettingsTransferError("The settings file is invalid or damaged.")
    salt = _unb64(kdf.get("salt"), expected_length=16)
    nonce = _unb64(cipher.get("nonce"), expected_length=12)
    ciphertext = _unb64(cipher.get("ciphertext"), maximum_length=_MAX_FILE_BYTES)
    if len(ciphertext) < 16:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    return salt, nonce, ciphertext


def _parse_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise SettingsTransferError("The settings file is invalid or damaged.") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_bounded(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_FILE_BYTES + 1)
    except OSError:
        raise SettingsTransferError("The settings file could not be read.") from None
    if len(raw) > _MAX_FILE_BYTES:
        raise SettingsTransferError("The settings file is too large.")
    return raw


def _render_template(
    template_path: str | os.PathLike[str], values: Mapping[str, str],
) -> str:
    try:
        lines = Path(template_path).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        raise SettingsTransferError("The current settings template is unavailable.") from None
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        content = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        match = _ASSIGNMENT.match(content)
        if match is None:
            output.append(line)
            continue
        name = match.group("name")
        if name not in values:
            output.append(line)
            continue
        value = _valid_env_value(values[name])
        line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        prefix = content[:match.start("value")]
        output.append(prefix + _dotenv_literal(value) + line_ending)
        seen.add(name)
    if seen != set(values):
        raise SettingsTransferError("The current settings template is inconsistent.")
    return "".join(output)


def _dotenv_literal(value: str) -> str:
    # The app's dotenv readers remove a balanced outer quote pair. Quote values
    # containing an inline-comment marker or a balanced boundary quote, choosing
    # a delimiter not present in the value so the parser preserves it exactly.
    quote_needed = " #" in value or (
        len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
    )
    if quote_needed:
        if "'" not in value:
            return "'" + value + "'"
        if '"' not in value:
            return '"' + value + '"'
        raise SettingsTransferError("A settings value cannot be safely stored.")
    return value


def _is_preserved_endpoint_or_key(name: str) -> bool:
    if name.endswith("_MODEL") or name in _MODEL_CHOICE_NAMES:
        return False
    if name.endswith(_PRESERVE_SUFFIXES):
        return True
    if name in _PROVIDER_URL_NAMES:
        return True
    if name.startswith(("CITATION_VERIFIER_", "CALLIMACHUS_")):
        return False
    return name.endswith(_ENDPOINT_SUFFIXES)


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        parent = path.parent
        if not parent.is_dir():
            raise OSError("destination folder missing")
        previous_mode: int | None = None
        try:
            previous_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".callimachus-settings-", dir=str(parent),
        )
        temporary = Path(temporary_name)
        try:
            if previous_mode is not None:
                os.chmod(temporary, previous_mode)
            elif os.name != "nt":
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
    except SettingsTransferError:
        raise
    except OSError:
        raise SettingsTransferError("Settings could not be written.") from None


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(
    value: object,
    *,
    expected_length: int | None = None,
    maximum_length: int | None = None,
) -> bytes:
    if type(value) is not str or len(value) > _MAX_FILE_BYTES * 2:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise SettingsTransferError("The settings file is invalid or damaged.") from None
    if expected_length is not None and len(decoded) != expected_length:
        raise SettingsTransferError("The settings file is invalid or damaged.")
    if maximum_length is not None and (not decoded or len(decoded) > maximum_length):
        raise SettingsTransferError("The settings file is invalid or damaged.")
    return decoded


__all__ = ["SettingsTransferError", "transfer_settings"]
