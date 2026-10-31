# tests/test_security_network_permissions.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_KEY = "a1" * 32


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load focused module source")
    module = importlib.util.module_from_spec(spec)
    # Register before execution so dataclasses can resolve the module namespace.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _scope(*, address: str = "198.51.100.7", via_proxy: bool = False) -> dict:
    return {
        "origin": "https://example.org:443",
        "connection_host": "proxy.example" if via_proxy else "example.org",
        "connection_port": 8080 if via_proxy else 443,
        "addresses": [address],
        "via_proxy": via_proxy,
    }


def _payload(scope: dict, request_id: str = "00000000000000000000000000000001") -> dict:
    return {"type": "network_request", "request_id": request_id, "scope": scope}


def _frame(wire, scope: dict, request_id: str = "00000000000000000000000000000001") -> str:
    return wire.encode_message(_KEY, _payload(scope, request_id))


def _responses(wire, frames: list[str]) -> list[dict]:
    decoded = [wire.decode_message(_KEY, frame) for frame in frames]
    return [payload for payload in decoded if payload is not None]


class NetworkPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._modules = ExitStack()
        self.addCleanup(self._modules.close)
        packages = {}
        for name in ("core", "core.fetch", "core.fetch.transport"):
            package = types.ModuleType(name)
            package.__path__ = []
            packages[name] = package
        # core.fetch.__init__ imports the full service.  Stub package objects
        # keep these tests scoped to the transport wire and GUI permission code.
        self._modules.enter_context(patch.dict(sys.modules, packages))
        self.wire = _load_module(
            "core.fetch.transport.network_consent",
            _ROOT / "core/fetch/transport/network_consent.py",
        )
        self.permissions = _load_module(
            "_network_permissions_under_test",
            _ROOT / "core/gui/network_permissions.py",
        )

    def test_failed_permanent_revocation_is_blocked_and_retry_survives_restart(self):
        state={"json":"[]", "fail":False, "writes":0}
        def save(value):
            state["writes"]+=1
            if state["fail"]:raise OSError("dummy storage unavailable")
            state["json"]=value
        store=self.permissions.PermissionStore(lambda:state["json"],save)
        scope=_scope();key=self.wire.scope_key(scope)
        self.assertTrue(store.decide(scope,"always"))
        state["fail"]=True
        with self.assertRaises(OSError):store.forget(key)
        self.assertIsNone(store.remembered(scope))
        self.assertEqual(len(store.entries()),1)
        state["fail"]=False
        store.forget(key)
        self.assertEqual(state["writes"],3)
        self.assertEqual(json.loads(state["json"]),[])
        reopened=self.permissions.PermissionStore(lambda:state["json"],save)
        self.assertIsNone(reopened.remembered(scope))

    def test_permanent_grant_over_storage_limit_is_not_published(self):
        state={"json":"[]"}
        store=self.permissions.PermissionStore(lambda:state["json"],lambda value:state.__setitem__("json",value))
        with patch.object(self.permissions,"_MAX_STORED_BYTES",1):
            self.assertFalse(store.decide(_scope(),"always"))
        self.assertIsNone(store.remembered(_scope()))
        self.assertEqual(state["json"],"[]")

    def test_store_persists_always_and_revokes_session_and_permanent(self) -> None:
        state = {"json": ""}
        store = self.permissions.PermissionStore(
            lambda: state["json"], lambda value: state.__setitem__("json", value)
        )
        permanent_scope = _scope()
        session_scope = _scope(address="2001:db8::5")

        self.assertIs(store.decide(permanent_scope, "always"), True)
        self.assertIs(store.decide(session_scope, "session"), True)
        rows = store.entries()
        self.assertEqual({row["choice"] for row in rows}, {"always", "session"})
        self.assertTrue(all(set(row) == {"key", "scope", "choice"} for row in rows))
        self.assertEqual(store.remembered(session_scope), "session")

        persisted = json.loads(state["json"])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0], {
            "key": self.wire.scope_key(permanent_scope),
            "scope": self.wire.canonical_scope(permanent_scope),
            "choice": "always",
        })
        self.assertNotIn("query", state["json"])
        self.assertNotIn("password", state["json"])

        reloaded = self.permissions.PermissionStore(
            lambda: state["json"], lambda value: state.__setitem__("json", value)
        )
        self.assertEqual(reloaded.remembered(permanent_scope), "always")
        self.assertIsNone(reloaded.remembered(session_scope))
        reloaded.forget(self.wire.scope_key(session_scope))
        reloaded.forget(self.wire.scope_key(permanent_scope))
        self.assertEqual(reloaded.entries(), [])
        self.assertEqual(json.loads(state["json"]), [])

    def test_malformed_persistent_records_are_ignored_fail_closed(self) -> None:
        invalid_scope = _scope()
        invalid_scope["origin"] = "https://user:password@example.org:443/private?token=secret"
        raw = json.dumps([{
            "key": "0" * 64,
            "scope": invalid_scope,
            "choice": "always",
        }])
        store = self.permissions.PermissionStore(lambda: raw, lambda _value: None)

        self.assertEqual(store.entries(), [])
        self.assertIsNone(store.remembered(_scope()))

    def test_store_rejects_persistent_record_count_over_bound(self) -> None:
        records = []
        for port in range(1, 258):
            scope = {
                "origin": "https://example.org:443",
                "connection_host": "example.org",
                "connection_port": port,
                "addresses": ["198.51.100.7"],
                "via_proxy": False,
            }
            records.append({
                "key": self.wire.scope_key(scope),
                "scope": self.wire.canonical_scope(scope),
                "choice": "always",
            })
        store = self.permissions.PermissionStore(lambda: json.dumps(records), lambda _value: None)

        self.assertEqual(store.entries(), [])

    def test_session_choice_is_reprompted_after_ip_change_and_app_restart(self) -> None:
        state = {"json": ""}
        store = self.permissions.PermissionStore(
            lambda: state["json"], lambda value: state.__setitem__("json", value)
        )
        prompts = []
        sent = []
        controller = self.permissions.PermissionController(
            _KEY, store, lambda scope: prompts.append(scope) or "session", sent.append,
        )

        first = _scope()
        changed = _scope(address="198.51.100.8")
        controller.feed(_frame(self.wire, first, "1" * 32) + "\n")
        controller.feed(_frame(self.wire, first, "2" * 32) + "\n")
        controller.feed(_frame(self.wire, changed, "3" * 32) + "\n")

        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0], self.wire.canonical_scope(first))
        self.assertEqual(prompts[1], self.wire.canonical_scope(changed))
        self.assertEqual(
            [response["choice"] for response in _responses(self.wire, sent)],
            ["session"] * 3,
        )

        restarted_store = self.permissions.PermissionStore(
            lambda: state["json"], lambda value: state.__setitem__("json", value)
        )
        restarted_prompts = []
        restarted = self.permissions.PermissionController(
            _KEY, restarted_store,
            lambda scope: restarted_prompts.append(scope) or "once",
            lambda _frame: None,
        )
        restarted.feed(_frame(self.wire, first, "4" * 32) + "\n")
        self.assertEqual(len(restarted_prompts), 1)

    def test_controller_buffers_chunks_filters_authenticated_frames_and_flushes_tail(self) -> None:
        sent = []
        prompts = []
        controller = self.permissions.PermissionController(
            _KEY,
            self.permissions.PermissionStore(lambda: "", lambda _value: None),
            lambda scope: prompts.append(scope) or "once",
            sent.append,
        )
        frame = _frame(self.wire, _scope())

        self.assertEqual(controller.feed("worker ready\n" + frame[:11]), "worker ready\n")
        self.assertEqual(sent, [])
        self.assertEqual(controller.feed(frame[11:] + "\ntrailing partial"), "")
        self.assertEqual(len(sent), 1)
        self.assertEqual(controller.flush(), "trailing partial")
        self.assertEqual(len(prompts), 1)
        self.assertIs(_responses(self.wire, sent)[0]["allowed"], True)

    def test_controller_passes_invalid_mac_and_suppresses_replayed_request_id(self) -> None:
        sent = []
        prompts = []
        controller = self.permissions.PermissionController(
            _KEY,
            self.permissions.PermissionStore(lambda: "", lambda _value: None),
            lambda _scope: prompts.append(True) or "once",
            sent.append,
        )
        frame = _frame(self.wire, _scope())
        envelope = json.loads(frame[len(self.wire.REQUEST_PREFIX):])
        envelope["signature"] = (
            "0" * 64 if envelope["signature"] != "0" * 64 else "1" * 64
        )
        invalid = self.wire.REQUEST_PREFIX + json.dumps(envelope, separators=(",", ":"))

        self.assertEqual(controller.feed(invalid + "\n"), invalid + "\n")
        self.assertEqual(sent, [])
        self.assertEqual(prompts, [])

        replay = _frame(self.wire, _scope(), "5" * 32) + "\n"
        self.assertEqual(controller.feed(replay + replay), "")
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(prompts), 1)

    def test_controller_denies_by_default_and_reports_persistence_failure(self) -> None:
        sent = []
        prompts = []
        errors = []

        def fail_save(_value):
            raise OSError("disk unavailable")

        store = self.permissions.PermissionStore(lambda: "", fail_save)
        controller = self.permissions.PermissionController(
            _KEY, store, lambda scope: prompts.append(scope) or "always",
            sent.append, errors.append,
        )

        self.assertEqual(controller.feed(_frame(self.wire, _scope(), "6" * 32) + "\n"), "")
        response = _responses(self.wire, sent)[0]
        self.assertIs(response["allowed"], False)
        self.assertEqual(response["choice"], "deny")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(errors, [
            "Network permission could not be saved; the request was denied."
        ])
        self.assertIsNone(store.remembered(_scope()))

        denied = self.permissions.PermissionController(
            _KEY,
            self.permissions.PermissionStore(lambda: "", lambda _value: None),
            lambda _scope: "deny",
            sent.append,
        )
        denied.feed(_frame(self.wire, _scope(), "7" * 32) + "\n")
        self.assertEqual(_responses(self.wire, sent)[-1]["choice"], "deny")

    def test_prompt_dialog_uses_plain_text_and_cancel_is_default(self) -> None:
        cases = (
            ("en", "Remember for this session", "session",
             "Resolved IP addresses: 198.51.100.7",
             ["Cancel", "Allow once", "Remember for this session", "Always remember"]),
            ("it", "Annulla", "deny", "Indirizzi IP risolti: 198.51.100.7",
             ["Annulla", "Consenti una volta", "Ricorda per questa sessione", "Ricorda sempre"]),
        )
        for language, button, expected, expected_line, expected_labels in cases:
            with self.subTest(language=language):
                state, widgets = _fake_widgets(button)
                self.assertEqual(
                    self.permissions.ask_permission(None, widgets, _scope(), language),
                    expected,
                )
                dialog = state.dialogs[0]
                details = next(child for child in dialog.children if hasattr(child, "plain_text"))
                buttons = []
                for child in dialog.children:
                    if hasattr(child, "default") and child not in buttons:
                        buttons.append(child)
                self.assertIn(expected_line, details.plain_text)
                self.assertIs(buttons[0].default, True)
                self.assertEqual(buttons[0].text, "Annulla" if language == "it" else "Cancel")
                self.assertEqual([item.text for item in buttons], expected_labels)

    def test_proxy_prompt_disclaims_final_destination_and_shows_endpoint(self) -> None:
        state, widgets = _fake_widgets("Cancel")

        self.assertEqual(
            self.permissions.ask_permission(None, widgets, _scope(via_proxy=True), "en"),
            "deny",
        )
        details = next(child for child in state.dialogs[0].children if hasattr(child, "plain_text"))
        self.assertIn("proxy.example:8080", details.plain_text)
        self.assertIn("A proxy may choose the final destination independently", details.plain_text)
        self.assertIn("Session grants last until the app closes", details.plain_text)


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback

    def emit(self, *args):
        if self.callback is not None:
            self.callback(*args)


def _fake_widgets(choice: str):
    state = types.SimpleNamespace(next_click=choice, dialogs=[])

    class Dialog:
        def __init__(self, parent=None):
            self.parent = parent
            self.children = []
            self.layouts = []
            self.accepted = False
            state.dialogs.append(self)

        def setObjectName(self, name):
            self.object_name = name

        def setWindowTitle(self, title):
            self.title = title

        def setModal(self, _modal):
            pass

        def accept(self):
            self.accepted = True

        def exec(self):
            for child in self.children:
                if isinstance(child, PushButton) and child.text == state.next_click:
                    child.click()
                    break
            return 1 if self.accepted else 0

    class Layout:
        def __init__(self, parent=None):
            self.parent = parent
            if parent is not None:
                parent.layouts.append(self)

        def addWidget(self, widget, *_args):
            self.parent.children.append(widget)

        def addLayout(self, layout):
            layout.parent = self.parent
            self.parent.layouts.append(layout)

    class PlainText:
        def __init__(self, parent=None):
            self.parent = parent
            self.plain_text = ""
            if parent is not None:
                parent.children.append(self)

        def setObjectName(self, _name):
            pass

        def setReadOnly(self, _read_only):
            pass

        def setPlainText(self, value):
            self.plain_text = value

    class PushButton:
        def __init__(self, text, parent=None):
            self.text = text
            self.parent = parent
            self.clicked = _Signal()
            self.default = False
            if parent is not None:
                parent.children.append(self)

        def setObjectName(self, _name):
            pass

        def setDefault(self, value):
            self.default = value

        def setAutoDefault(self, _value):
            pass

        def click(self):
            self.clicked.emit(False)

    return state, types.SimpleNamespace(
        QDialog=Dialog,
        QVBoxLayout=Layout,
        QHBoxLayout=Layout,
        QPlainTextEdit=PlainText,
        QPushButton=PushButton,
    )
