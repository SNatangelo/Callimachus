# core/gui/network_permissions.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-mediated network permission storage and dialogs.

This module deliberately has no Qt imports.  The desktop injects its QtWidgets
module only when one of the small dialogs is opened.
"""
from __future__ import annotations

import importlib
import json
import re
import threading
from collections import deque
from collections.abc import Callable
from typing import Any


_WIRE_MODULE = "core.fetch.transport.network_consent"
_MAX_CONTROL_FRAME_BYTES = 16 * 1024
_MAX_STORED_ENTRIES = 256
_MAX_STORED_BYTES = _MAX_STORED_ENTRIES * _MAX_CONTROL_FRAME_BYTES
_MAX_RECENT_REQUEST_IDS = 2048
_CHOICES = frozenset({"deny", "once", "session", "always"})
_HEX_64 = re.compile(r"[0-9a-fA-F]{64}\Z")
_HEX_32 = re.compile(r"[0-9a-fA-F]{32}\Z")


def _wire_api():
    """Load the transport contract only when a scope or frame is used."""
    return importlib.import_module(_WIRE_MODULE)


def _scope_identity(scope: Any, *, require_canonical: bool = False) -> tuple[dict[str, Any], str]:
    wire = _wire_api()
    canonical = wire.canonical_scope(scope)
    if type(canonical) is not dict:
        raise ValueError("invalid network permission scope")
    if require_canonical and canonical != scope:
        raise ValueError("network permission scope is not canonical")
    key = wire.scope_key(canonical)
    if type(key) is not str or _HEX_64.fullmatch(key) is None:
        raise ValueError("invalid network permission scope key")
    # Copy through JSON so a caller cannot mutate a remembered scope later.
    frozen = json.loads(json.dumps(canonical, ensure_ascii=True, separators=(",", ":")))
    return frozen, key.lower()


class PermissionStore:
    """Keep exact-scope session grants in memory and durable grants in JSON text."""

    def __init__(self, load: Callable[[], str], save: Callable[[str], None]) -> None:
        self._load_callback = load
        self._save_callback = save
        self._lock = threading.RLock()
        self._session: dict[str, dict[str, Any]] = {}
        self._revoked: set[str] = set()
        self._always = self._load_always()

    def _load_always(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._load_callback()
        except Exception:
            return {}
        if type(raw) is not str or len(raw.encode("utf-8", errors="replace")) > _MAX_STORED_BYTES:
            return {}
        if not raw:
            return {}
        try:
            records = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if type(records) is not list or len(records) > _MAX_STORED_ENTRIES:
            return {}

        valid: dict[str, dict[str, Any]] = {}
        duplicates: set[str] = set()
        for record in records:
            if type(record) is not dict or set(record) != {"key", "scope", "choice"}:
                continue
            if record.get("choice") != "always":
                continue
            try:
                scope, computed_key = _scope_identity(record.get("scope"), require_canonical=True)
            except Exception:
                continue
            key = record.get("key")
            if type(key) is not str or _HEX_64.fullmatch(key) is None or key.lower() != computed_key:
                continue
            if key.lower() in valid or key.lower() in duplicates:
                valid.pop(key.lower(), None)
                duplicates.add(key.lower())
                continue
            valid[key.lower()] = {"key": key.lower(), "scope": scope, "choice": "always"}
        if len(valid) > _MAX_STORED_ENTRIES:
            return {}
        return valid

    @staticmethod
    def _serialize(records: dict[str, dict[str, Any]]) -> str:
        ordered = [records[key] for key in sorted(records)]
        serialized = json.dumps(ordered, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > _MAX_STORED_BYTES:
            raise ValueError("remembered network permissions exceed the storage limit")
        return serialized

    def entries(self) -> list[dict[str, Any]]:
        """Return detached records for the permission manager."""
        with self._lock:
            combined = dict(self._always)
            combined.update(self._session)
            return [
                json.loads(json.dumps(combined[key], ensure_ascii=True, separators=(",", ":")))
                for key in sorted(combined)
            ]

    def forget(self, key: str) -> None:
        """Revoke a session and/or persistent grant by its exact scope key."""
        if type(key) is not str or _HEX_64.fullmatch(key) is None:
            return
        normalized = key.lower()
        with self._lock:
            self._session.pop(normalized, None)
            if normalized not in self._always:
                return
            updated = dict(self._always)
            updated.pop(normalized, None)
            # Block the old grant in this process immediately, but retain the
            # durable entry until saving succeeds so a failed revoke is retryable.
            self._revoked.add(normalized)
            self._save_callback(self._serialize(updated))
            self._always = updated
            self._revoked.discard(normalized)

    def decide(self, scope: dict[str, Any], choice: str) -> bool:
        """Apply an explicit choice; durable grants are saved before success."""
        if type(choice) is not str or choice not in _CHOICES:
            return False
        try:
            canonical, key = _scope_identity(scope)
        except Exception:
            return False
        if choice == "deny":
            return False
        if choice == "once":
            return True

        record = {"key": key, "scope": canonical, "choice": choice}
        with self._lock:
            current_size = len(set(self._always) | set(self._session))
            replacing = key in self._always or key in self._session
            if current_size >= _MAX_STORED_ENTRIES and not replacing:
                return False
            if choice == "session":
                if key in self._always and key not in self._revoked:
                    return True
                self._session[key] = record
                return True

            updated = dict(self._always)
            updated[key] = {**record, "choice": "always"}
            remaining_session = dict(self._session)
            remaining_session.pop(key, None)
            if len(set(updated) | set(remaining_session)) > _MAX_STORED_ENTRIES:
                return False
            # Do not publish the grant in memory or to the controller until the
            # persistence callback has completed successfully.
            try:
                self._save_callback(self._serialize(updated))
            except Exception:
                return False
            self._always = updated
            self._session = remaining_session
            self._revoked.discard(key)
            return True

    def remembered(self, scope: dict[str, Any]) -> str | None:
        """Return the saved choice only for an identical canonical scope."""
        try:
            canonical, key = _scope_identity(scope)
        except Exception:
            return None
        with self._lock:
            record = self._session.get(key)
            if record is None and key not in self._revoked:
                record = self._always.get(key)
            if record is None or record.get("scope") != canonical:
                return None
            choice = record.get("choice")
            return choice if choice in {"session", "always"} else None


class PermissionController:
    """Authenticate stderr control frames and return ordinary stderr text."""

    def __init__(
        self,
        key: str,
        store: PermissionStore,
        ask: Callable[[dict[str, Any]], str],
        send: Callable[[str], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._key = key if type(key) is str and _HEX_64.fullmatch(key) is not None else None
        self._store = store
        self._ask = ask
        self._send = send
        self._on_error = on_error
        self._buffer = ""
        self._passthrough_until_newline = False
        self._buffer_lock = threading.RLock()
        self._recent_ids: deque[str] = deque()
        self._recent_set: set[str] = set()
        self._recent_lock = threading.Lock()

    @staticmethod
    def _byte_length(value: str) -> int:
        return len(value.encode("utf-8", errors="replace"))

    @classmethod
    def _complete_line_frame_length(cls, line: str) -> int:
        frame = line[:-1] if line.endswith("\n") else line
        if frame.endswith("\r"):
            frame = frame[:-1]
        return cls._byte_length(frame)

    def _extract(self, text: str) -> list[tuple[str, bool]]:
        """Return complete lines and oversized ordinary chunks in stream order."""
        events: list[tuple[str, bool]] = []
        with self._buffer_lock:
            incoming = text
            if self._passthrough_until_newline:
                newline = incoming.find("\n")
                if newline < 0:
                    if incoming:
                        events.append((incoming, False))
                    return events
                events.append((incoming[:newline + 1], False))
                incoming = incoming[newline + 1:]
                self._passthrough_until_newline = False

            combined = self._buffer + incoming
            self._buffer = ""
            start = 0
            while True:
                newline = combined.find("\n", start)
                if newline < 0:
                    break
                line = combined[start:newline + 1]
                if self._complete_line_frame_length(line) <= _MAX_CONTROL_FRAME_BYTES:
                    events.append((line, True))
                else:
                    events.append((line, False))
                start = newline + 1
            tail = combined[start:]
            if self._byte_length(tail) <= _MAX_CONTROL_FRAME_BYTES:
                self._buffer = tail
            elif tail:
                events.append((tail, False))
                self._passthrough_until_newline = True
        return events

    def _mark_request_id(self, request_id: str) -> bool:
        normalized = request_id.lower()
        with self._recent_lock:
            if normalized in self._recent_set:
                return False
            if len(self._recent_ids) >= _MAX_RECENT_REQUEST_IDS:
                old = self._recent_ids.popleft()
                self._recent_set.discard(old)
            self._recent_ids.append(normalized)
            self._recent_set.add(normalized)
            return True

    def _report_error(self, message: str) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(message)
        except Exception:
            pass

    def _handle_control_line(self, line: str) -> str:
        try:
            wire = _wire_api()
            prefix = wire.REQUEST_PREFIX
        except Exception:
            return line
        if not line.startswith(prefix) or self._key is None:
            return line
        if self._complete_line_frame_length(line) > _MAX_CONTROL_FRAME_BYTES:
            return line
        frame = line[:-1] if line.endswith("\n") else line
        if frame.endswith("\r"):
            frame = frame[:-1]
        try:
            payload = wire.decode_message(self._key, frame)
        except Exception:
            return line
        if payload is None:
            return line

        # An authenticated control frame is not ordinary diagnostic output.
        # Invalid authenticated payloads are consumed without granting access.
        if type(payload) is not dict or set(payload) != {"type", "request_id", "scope"}:
            return ""
        if payload.get("type") != "network_request":
            return ""
        request_id = payload.get("request_id")
        if type(request_id) is not str or _HEX_32.fullmatch(request_id) is None:
            return ""
        if not self._mark_request_id(request_id):
            return ""

        try:
            canonical, permission_id = _scope_identity(payload.get("scope"), require_canonical=True)
        except Exception:
            return ""

        # The request line was removed from the buffer before reaching this
        # callback, so opening a modal dialog cannot replay it on reentry.
        choice = "deny"
        try:
            remembered = self._store.remembered(canonical)
        except Exception:
            remembered = "__error__"
            self._report_error("Network permission could not be checked; the request was denied.")
        if remembered in {"session", "always"}:
            choice = remembered
        elif remembered is None:
            prompt_error = object()
            try:
                proposed = self._ask(canonical)
            except Exception:
                proposed = prompt_error
                self._report_error("Network permission prompt failed; the request was denied.")
            if type(proposed) is str and proposed == "deny":
                choice = "deny"
            elif type(proposed) is str and proposed in _CHOICES:
                try:
                    if self._store.decide(canonical, proposed) is True:
                        choice = proposed
                    else:
                        self._report_error("Network permission could not be saved; the request was denied.")
                except Exception:
                    self._report_error("Network permission could not be saved; the request was denied.")
            elif proposed is not prompt_error:
                self._report_error("Network permission prompt returned an invalid choice; the request was denied.")
        elif remembered == "__error__":
            pass
        else:
            self._report_error("Network permission state was invalid; the request was denied.")

        response = {
            "type": "network_decision",
            "request_id": request_id,
            "allowed": choice != "deny",
            "choice": choice,
            "permission_id": permission_id,
        }
        try:
            encoded = wire.encode_message(self._key, response)
            if type(encoded) is str and self._byte_length(encoded) <= _MAX_CONTROL_FRAME_BYTES:
                self._send(encoded)
            else:
                self._report_error("Network permission decision could not be returned; access was denied.")
        except Exception:
            # A missing or failed response is fail-closed at the broker.
            self._report_error("Network permission decision could not be returned; access was denied.")
        return ""

    def feed(self, text: str) -> str:
        """Consume complete authenticated frames and return visible stderr."""
        if type(text) is not str or not text:
            return ""
        output: list[str] = []
        for chunk, complete_line in self._extract(text):
            if complete_line:
                visible = self._handle_control_line(chunk)
                if visible:
                    output.append(visible)
            else:
                output.append(chunk)
        return "".join(output)

    def flush(self) -> str:
        """Return any incomplete tail as ordinary stderr when the child exits."""
        with self._buffer_lock:
            tail = self._buffer
            self._buffer = ""
            self._passthrough_until_newline = False
        return tail


def _endpoint(host: Any, port: Any) -> str:
    value = str(host or "")
    if ":" in value and not value.startswith("["):
        value = f"[{value}]"
    return f"{value}:{port}"


def _permission_text(scope: dict[str, Any], language: str) -> str:
    italian = language == "it"
    origin_label = "Destinazione" if italian else "Destination"
    endpoint_label = "Endpoint di connessione" if italian else "Connection endpoint"
    route_label = "Percorso" if italian else "Route"
    proxy_route = "proxy" if italian else "proxy"
    direct_route = "diretto" if italian else "direct"
    addresses_label = "Indirizzi IP risolti" if italian else "Resolved IP addresses"
    address_change = (
        "La richiesta verrà ripetuta se cambia l'endpoint o l'insieme degli IP."
        if italian else
        "You will be asked again if the endpoint or IP address set changes."
    )
    lifetime = (
        "Le autorizzazioni per la sessione durano fino alla chiusura dell'app. "
        "Puoi revocare quelle permanenti in Impostazioni > Rete."
        if italian else
        "Session grants last until the app closes. Always grants can be revoked "
        "in Settings > Network."
    )
    proxy_disclaimer = (
        "Un proxy può scegliere autonomamente la destinazione finale; Callimachus "
        "non può verificarla in modo indipendente."
        if italian else
        "A proxy may choose the final destination independently; Callimachus "
        "cannot independently verify it."
    )
    addresses = scope.get("addresses", [])
    address_text = ", ".join(str(address) for address in addresses) if addresses else ("(nessuno)" if italian else "(none)")
    explanation = (
        "Questo accesso usa un indirizzo privato o un proxy. Può essere previsto dalla rete "
        "istituzionale: consenti solo se riconosci il sito e il percorso indicati. "
        "Autorizzare la connessione non conferma l’identità o l’affidabilità della fonte."
        if italian else
        "This request uses a private address or a proxy. It may be expected on an institutional "
        "network: allow it only if you recognize the site and route below. "
        "Allowing the connection does not confirm the source's identity or reliability."
    )
    lines = [
        explanation,
        "",
        f"{origin_label}: {scope.get('origin', '')}",
        f"{endpoint_label}: {_endpoint(scope.get('connection_host'), scope.get('connection_port'))}",
        f"{route_label}: {proxy_route if scope.get('via_proxy') else direct_route}",
        f"{addresses_label}: {address_text}",
        "",
        address_change,
        lifetime,
    ]
    if scope.get("via_proxy"):
        lines.extend(["", proxy_disclaimer])
    return "\n".join(lines)


def ask_permission(parent, QtWidgets, scope: dict[str, Any], language: str = "en") -> str:
    """Show a fail-closed owner prompt for one exact destination scope."""
    italian = language == "it"
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("networkPermissionDialog")
    dialog.setWindowTitle("Autorizzazione di rete" if italian else "Network permission")
    dialog.setModal(True)
    layout = QtWidgets.QVBoxLayout(dialog)

    details = QtWidgets.QPlainTextEdit(dialog)
    details.setObjectName("networkPermissionDetails")
    details.setReadOnly(True)
    details.setPlainText(_permission_text(scope, "it" if italian else "en"))
    layout.addWidget(details, 1)

    button_row = QtWidgets.QVBoxLayout()
    layout.addLayout(button_row)
    selected = {"choice": "deny"}
    labels = (
        (("deny", "Annulla"), ("once", "Consenti una volta"),
         ("session", "Ricorda per questa sessione"), ("always", "Ricorda sempre"))
        if italian else
        (("deny", "Cancel"), ("once", "Allow once"),
         ("session", "Remember for this session"), ("always", "Always remember"))
    )
    for choice, label in labels:
        button = QtWidgets.QPushButton(label, dialog)
        button.setObjectName(f"networkPermission_{choice}")
        if choice == "deny":
            button.setDefault(True)
            button.setAutoDefault(True)
        button.clicked.connect(
            lambda _checked=False, value=choice: (selected.__setitem__("choice", value), dialog.accept())
        )
        button_row.addWidget(button)

    dialog.exec()
    choice = selected["choice"]
    return choice if choice in _CHOICES else "deny"


def manage_permissions(parent, QtWidgets, store: PermissionStore, language: str = "en") -> None:
    """Show remembered network scopes and revoke the selected one."""
    italian = language == "it"
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("networkPermissionManagerDialog")
    dialog.setWindowTitle("Autorizzazioni di rete" if italian else "Network permissions")
    dialog.setModal(True)
    layout = QtWidgets.QVBoxLayout(dialog)
    entries_list = QtWidgets.QListWidget(dialog)
    entries_list.setObjectName("networkPermissionEntries")
    layout.addWidget(entries_list, 1)

    try:
        records = store.entries()
    except Exception:
        records = []
        failure_text = (
            "Impossibile caricare le autorizzazioni di rete." if italian
            else "Could not load network permissions."
        )
    else:
        failure_text = ""

    visible_records = []
    for record in records:
        if type(record) is not dict:
            continue
        scope = record.get("scope", {})
        if type(scope) is not dict:
            continue
        route = "proxy" if scope.get("via_proxy") else ("diretto" if italian else "direct")
        choice = "sempre" if record.get("choice") == "always" and italian else (
            "sessione" if record.get("choice") == "session" and italian else record.get("choice", "")
        )
        addresses = ", ".join(str(value) for value in scope.get("addresses", []))
        row = (
            f"{scope.get('origin', '')} · {route} · "
            f"{_endpoint(scope.get('connection_host'), scope.get('connection_port'))} · "
            f"{addresses} · {choice}"
        )
        entries_list.addItem(row)
        visible_records.append(record)

    status = QtWidgets.QLabel(failure_text, dialog)
    if not failure_text and not records:
        status.setText(
            "Non ci sono autorizzazioni di rete memorizzate." if italian
            else "No remembered network permissions."
        )
    status.setObjectName("networkPermissionManagerStatus")
    status.setWordWrap(True)
    layout.addWidget(status)

    buttons = QtWidgets.QHBoxLayout()
    layout.addLayout(buttons)
    revoke = QtWidgets.QPushButton(
        "Revoca selezionata" if italian else "Revoke selected", dialog
    )
    revoke.setObjectName("networkPermissionRevokeSelected")
    revoke.setEnabled(False)
    entries_list.currentRowChanged.connect(lambda row: revoke.setEnabled(row >= 0))
    close = QtWidgets.QPushButton("Chiudi" if italian else "Close", dialog)
    close.setObjectName("networkPermissionManagerClose")
    close.clicked.connect(dialog.accept)
    buttons.addWidget(revoke)
    buttons.addWidget(close)

    def revoke_selected(_checked: bool = False) -> None:
        row = entries_list.currentRow()
        if row < 0 or row >= len(visible_records):
            return
        try:
            store.forget(visible_records[row]["key"])
        except Exception:
            status.setText(
                "Impossibile salvare la revoca." if italian
                else "Could not save the revocation."
            )
            return
        dialog.accept()

    revoke.clicked.connect(revoke_selected)
    dialog.exec()
