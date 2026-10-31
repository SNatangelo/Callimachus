# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""macOS consent, worker cache and complete transaction/lifetime regressions."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from core.app.settings import credentials as store
from core.app.settings import macos_keychain


@pytest.fixture
def vault(monkeypatch):
    values = {}
    reads = []
    grants = set()
    interactive = []
    backend = SimpleNamespace()
    def read(service, account):
        reads.append(account)
        if account in values and account not in grants:
            raise PermissionError("synthetic authorization required")
        return values.get(account)
    def authorize(service, account):
        if account in values:
            interactive.append(account)
        if account in values and account not in grants:
            raise RuntimeError("synthetic denial")
        return values.get(account)
    backend.get_password = authorize
    backend.set_password = lambda service, account, value: (values.__setitem__(account, value), grants.add(account))
    backend.delete_password = lambda service, account: values.pop(account, None)
    monkeypatch.setattr(store, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(store, "_trusted_backend", lambda: SimpleNamespace(get_password=authorize, set_password=backend.set_password, delete_password=backend.delete_password))
    from contextlib import nullcontext
    from core.app.settings import macos_vault
    monkeypatch.setattr(macos_keychain, "read_without_ui", read)
    def read_item(service, account, *, interactive=False, reference=None):
        value=store._trusted_backend().get_password(service, account) if interactive else read(service, account)
        return value, (account.encode() if value is not None else None)
    def write_item(service, account, value, *, reference=None):
        assert reference is None or reference == account.encode()
        backend.set_password(service, account, value)
        return account.encode()
    def delete_item(service, account, reference):
        assert reference == account.encode(); values.pop(account,None)
    monkeypatch.setattr(macos_keychain, "read_item", read_item)
    monkeypatch.setattr(macos_keychain, "write_value", write_item)
    monkeypatch.setattr(macos_keychain, "delete_item", delete_item)
    monkeypatch.setattr(macos_vault, "_writer_lock", lambda *_a: nullcontext())
    def delete_exact(service, account, expected):
        if values.get(account) == expected:
            values.pop(account); return True
        return False
    monkeypatch.setattr(macos_keychain, "delete_if_matches", delete_exact)
    monkeypatch.delenv("CALLIMACHUS_DATA_DIR", raising=False)
    return SimpleNamespace(values=values, reads=reads, grants=grants, interactive=interactive)


def test_workers_share_successful_and_absent_reads_for_entire_session(vault):
    account = store._username("OPENAI_API_KEY")
    vault.values[account] = "synthetic-key"
    vault.grants.add(account)
    with store._macos_desktop_secret_session() as session:
        assert store.load_secret_values({"OPENAI_API_KEY"}) == {"OPENAI_API_KEY": "synthetic-key"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: store.load_secret_values({"OPENAI_API_KEY", "GROQ_API_KEY"}), range(16)))
        assert all(r == {"OPENAI_API_KEY": "synthetic-key"} for r in results)
        assert vault.reads.count(account) == 1
        assert vault.reads.count(store._username("GROQ_API_KEY")) == 1
    assert session.values == {} and not session.active
    assert store._MACOS_SECRET_SESSION is None


def test_denial_is_optional_and_never_retried_automatically(vault):
    names = {"OPENAI_API_KEY", "GROQ_API_KEY"}
    for name in names:
        vault.values[store._username(name)] = "synthetic-key"
    with store._macos_desktop_secret_session():
        assert store.load_secret_values(names) == {}
        assert store.macos_credential_recovery_state() == {"pending": 2}
        assert vault.interactive == []
        assert store.recover_macos_credentials() == {"recovered": 0, "pending": 2}
        assert len(vault.interactive) == 1  # Stop on the first native refusal.
        for _ in range(8):
            assert store.load_secret_values(names) == {}
        assert len(vault.reads) == 3
        assert len(vault.interactive) == 1
        assert len(vault.values) == 2  # No item was deleted, replaced or copied.


def test_explicit_recovery_keeps_grants_and_updates_every_worker(vault):
    account = store._username("OPENAI_API_KEY")
    vault.values[account] = "synthetic-key"
    with store._macos_desktop_secret_session():
        assert store.load_secret_values({"OPENAI_API_KEY"}) == {}
        vault.grants.add(account)  # Native Always Allow granted following Confirm.
        assert store.recover_macos_credentials() == {"recovered": 1, "pending": 0}
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(store.load_secret_values, {"OPENAI_API_KEY"}).result() == {"OPENAI_API_KEY": "synthetic-key"}
        assert len(vault.interactive) == 1
        from core.app.settings import macos_vault
        assert account not in vault.values
        assert macos_vault.account("default") in vault.values
    # A new process/session has a cold cache; authorization stays on the item.
    with store._macos_desktop_secret_session():
        assert store.load_secret_values({"OPENAI_API_KEY"}) == {"OPENAI_API_KEY": "synthetic-key"}
        assert store.macos_credential_recovery_state() == {"pending": 0}
    assert len(vault.interactive) == 1


@pytest.mark.parametrize("rollback", [False, True])
def test_workers_cannot_observe_uncommitted_credentials(vault, rollback):
    name = "OPENAI_API_KEY"; account = store._username(name)
    vault.values[account] = "old"; vault.grants.add(account)
    started = Event()
    def worker():
        started.set()
        return store.load_secret_values({name})
    with store._macos_desktop_secret_session(), ThreadPoolExecutor(max_workers=1) as pool:
        assert store.load_secret_values({name}) == {name: "old"}
        try:
            with store.update_secret_values({name: "new"}):
                future = pool.submit(worker)
                assert started.wait(2)
                assert not future.done()
                if rollback:
                    raise OSError("synthetic paired settings failure")
        except OSError:
            assert rollback
        assert future.result(timeout=2) == {name: "old" if rollback else "new"}


def test_closed_session_cannot_be_repopulated_by_pending_worker(vault, monkeypatch):
    found = Event(); resume = Event()
    original = store._macos_session
    def delayed():
        session = original()
        found.set()
        assert resume.wait(3)
        return session
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._macos_desktop_secret_session() as session:
            monkeypatch.setattr(store, "_macos_session", delayed)
            future = pool.submit(store.load_secret_values, {"OPENAI_API_KEY"})
            assert found.wait(2)
        resume.set()
        with pytest.raises(store.CredentialStoreError, match="ended"):
            future.result(timeout=2)
        assert session.values == {} and vault.reads == []


def test_profile_and_nonsecret_settings_never_share_cached_secrets(vault, monkeypatch):
    with store._macos_desktop_secret_session():
        assert store.load_secret_values({"OPENAI_MODEL", "OPENAI_BASE_URL", "CALLIMACHUS_THEME"}) == {}
        assert vault.reads == []
        account = store._username("OPENAI_API_KEY")
        vault.values[account] = "first"; vault.grants.add(account)
        assert store.load_secret_values({"OPENAI_API_KEY"}) == {"OPENAI_API_KEY": "first"}
        monkeypatch.setenv("CALLIMACHUS_DATA_DIR", "/synthetic/other-profile")
        assert store.load_secret_values({"OPENAI_API_KEY"}) == {}
        assert vault.reads[-1] != account


def test_frozen_mac_scope_survives_splash_handoff_and_error(vault, monkeypatch):
    import run as entry
    from core.gui import startup_splash
    account = store._username("OPENAI_API_KEY")
    vault.values[account] = "synthetic"; vault.grants.add(account)
    monkeypatch.setattr(entry, "is_frozen", lambda: True)
    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setattr(entry.sys, "argv", ["Callimachus", "app"])
    monkeypatch.setattr(entry.importlib, "import_module", lambda _: object())
    monkeypatch.setattr(entry, "_dispatch", lambda *_: store.load_secret_values({"OPENAI_API_KEY"}) and 0)
    def splash(*, prepare, start):
        prepare(); assert start() == 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(store.load_secret_values, {"OPENAI_API_KEY"}).result() == {"OPENAI_API_KEY": "synthetic"}
        assert len(vault.reads) == 2
        raise RuntimeError("synthetic post-handoff error")
    monkeypatch.setattr(startup_splash, "run_with_splash", splash)
    monkeypatch.setattr(entry, "_main_impl", lambda: entry._launch_desktop("synthetic", [], "test"))
    with pytest.raises(RuntimeError, match="post-handoff"):
        entry._main()
    assert store._MACOS_SECRET_SESSION is None


@pytest.mark.parametrize("argv", [["--help"], ["--qt-probe"], ["--package-self-test"], ["parse", "synthetic.pdf"]])
def test_mac_non_gui_commands_do_not_create_persistent_cache(vault, monkeypatch, argv):
    import run as entry
    monkeypatch.setattr(entry, "is_frozen", lambda: True)
    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setattr(entry.sys, "argv", ["Callimachus", *argv])
    def impl():
        assert store._MACOS_SECRET_SESSION is None
        return 0
    monkeypatch.setattr(entry, "_main_impl", impl)
    assert entry._main() == 0


def test_worker_starting_after_gui_exit_cannot_reopen_native_prompts(vault):
    ready = Event(); resume = Event()
    account = store._username("OPENAI_API_KEY")
    vault.values[account] = "synthetic-protected"
    def late_worker():
        ready.set()
        assert resume.wait(3)
        return store.load_secret_values({"OPENAI_API_KEY"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._macos_desktop_secret_session():
            future = pool.submit(late_worker)
            assert ready.wait(2)
        resume.set()
        assert future.result(timeout=2) == {}
    assert vault.interactive == []
    assert store._MACOS_SECRET_SESSION is None


@pytest.mark.parametrize("refuse", [False, True])
@pytest.mark.parametrize("phase", ["check", "gui"])
def test_frozen_upgrade_probe_is_confined_to_synthetic_service(vault, monkeypatch, capsys, refuse, phase):
    import run as entry
    from types import SimpleNamespace
    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_ID", "18dfd723-d259-4c51-b326-450b8e0f5179")
    original_service = store._SERVICE_NAME
    backend = store._trusted_backend()
    accessed = []
    def wrap(method):
        def operation(service, *args, **kwargs):
            accessed.append(service)
            assert service == "Callimachus.PackageSmoke"
            return method(service, *args, **kwargs)
        return operation
    monkeypatch.setattr(store, "_trusted_backend", lambda: SimpleNamespace(**{name:wrap(getattr(backend,name)) for name in ("get_password","set_password","delete_password")}))
    original_read = macos_keychain.read_without_ui
    monkeypatch.setattr(macos_keychain,"read_without_ui",wrap(original_read))
    for method in ("read_item", "write_value", "delete_item", "delete_if_matches"):
        monkeypatch.setattr(macos_keychain, method, wrap(getattr(macos_keychain, method)))
    def gui(values):
        assert store._SERVICE_NAME == "Callimachus.PackageSmoke"
        assert entry.os.environ["CALLIMACHUS_DATA_DIR"] == "/CallimachusPackageSmoke/18dfd723-d259-4c51-b326-450b8e0f5179"
        with store._macos_desktop_secret_session():
            if store.load_secret_values(values) != values:
                raise store.CredentialStoreError("synthetic refusal")
    monkeypatch.setattr(entry, "_probe_macos_keychain_gui", gui)
    assert entry._probe_macos_keychain_update("create") == 0
    if refuse:
        vault.grants.clear()
    assert entry._probe_macos_keychain_update(phase) == int(refuse)
    assert store._SERVICE_NAME == original_service
    assert vault.interactive == []
    vault.grants.update(vault.values)  # Restore fixture creator access for cleanup.
    assert entry._probe_macos_keychain_update("delete") == 0
    assert not vault.values
    captured = capsys.readouterr()
    assert "synthetic-package-key" not in captured.out+captured.err
    assert set(accessed) == {"Callimachus.PackageSmoke"}


@pytest.mark.parametrize("phase", ["delete", "gui"])
def test_frozen_upgrade_probe_rejects_invalid_identifier_without_access(vault, monkeypatch, phase):
    import run as entry
    monkeypatch.setattr(entry.sys,"platform","darwin")
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_ID","a real account name")
    assert entry._probe_macos_keychain_update(phase) == 2
    assert vault.reads == vault.interactive == []


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_package_gui_phase_rejects_other_platforms_without_access(vault, monkeypatch, platform):
    import run as entry
    monkeypatch.setattr(entry.sys, "platform", platform)
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_ID", "18dfd723-d259-4c51-b326-450b8e0f5179")
    monkeypatch.setattr(entry, "_probe_macos_keychain_gui", lambda _: pytest.fail("GUI must not start"))
    assert entry._probe_macos_keychain_update("gui") == 2
    assert vault.reads == vault.interactive == []


def test_private_package_gui_dispatch_precedes_personal_environment_loading(vault, monkeypatch):
    import run as entry
    monkeypatch.setattr(entry, "is_frozen", lambda: True)
    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setattr(entry.sys, "argv", ["Callimachus", "--package-self-test"])
    monkeypatch.setattr(entry, "startup_compatibility_error", lambda: None)
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_ID", "18dfd723-d259-4c51-b326-450b8e0f5179")
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_PHASE", "gui")
    for name in ("_load_frozen_environment", "resource_root"):
        monkeypatch.setattr(entry, name, lambda: pytest.fail("No personal environment or generic smoke"))
    monkeypatch.setattr(store, "secret_names_from_template", lambda _: pytest.fail("No template access"))
    calls = []
    def gui(values):
        calls.append(store._SERVICE_NAME)
        assert store._MACOS_SECRET_SESSION is None  # The GUI probe owns its session.
        assert set(values) == {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CITATION_VERIFIER_SIGNING_KEY"}
    monkeypatch.setattr(entry, "_probe_macos_keychain_gui", gui)
    original_service = store._SERVICE_NAME
    assert entry._main() == 0
    assert calls == ["Callimachus.PackageSmoke"]
    assert store._SERVICE_NAME == original_service
    assert vault.reads == vault.interactive == []


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("refuse", [False, True])
def test_legacy_keychain_ui_is_disabled_and_restored(monkeypatch, initial, refuse):
    import ctypes
    state = {"allowed": initial}
    events = []
    def get(ptr):
        ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte))[0] = state["allowed"]
        return 0
    def set_(value):
        state["allowed"] = bool(value)
        events.append(bool(value))
        return 0
    def check(status):
        if status:
            raise RuntimeError("synthetic native failure")
    api = SimpleNamespace(_sec=SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=get,
        SecKeychainSetUserInteractionAllowed=set_), Error=SimpleNamespace(raise_for_status=check))
    monkeypatch.setattr(macos_keychain, "_native_api", lambda: api)
    def read(api, service, account):
        assert state["allowed"] is False
        if refuse:
            raise PermissionError("synthetic blocked legacy item")
        return "synthetic"
    monkeypatch.setattr(macos_keychain, "_read", read)
    if refuse:
        with pytest.raises(PermissionError):
            macos_keychain.read_without_ui("Synthetic", "one")
    else:
        assert macos_keychain.read_without_ui("Synthetic", "one") == "synthetic"
    assert events == [False, initial]
    assert state["allowed"] is initial


def test_native_writes_wait_until_quiet_read_restores_ui(vault):
    waiting = Event()
    entered = Event()
    backend = SimpleNamespace(set_password=lambda *args: entered.set())
    def write():
        waiting.set()
        return store._backend_operation(backend, "set_password", "Synthetic", "one", "secret")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with macos_keychain.interaction_lock:
            future = pool.submit(write)
            assert waiting.wait(2)
            assert not entered.is_set()
        future.result(timeout=2)
    assert entered.is_set()


def test_repeated_builds_keep_one_account_per_key_and_preserve_other_profiles(vault, monkeypatch):
    name = "OPENAI_API_KEY"
    other = "profile-unrelated:" + name
    vault.values[other] = "unrelated-profile"
    vault.values["default:DEEPSEEK_API_KEY"] = "other-provider"
    for version in ("2.0.0", "2.0.1", "2.0.2"):
        monkeypatch.setenv("CALLIMACHUS_BUILD_VERSION", version)
        with store._macos_desktop_secret_session():
            with store.update_secret_values({name: "updated-synthetic"}):
                pass
    from core.app.settings import macos_vault
    assert set(vault.values) == {other, "default:DEEPSEEK_API_KEY", macos_vault.account("default")}
    assert vault.values[other] == "unrelated-profile"
    assert vault.values["default:DEEPSEEK_API_KEY"] == "other-provider"
    assert store.load_secret_values({name}) == {name:"updated-synthetic"}



def test_synthetic_upgrade_recovery_uses_exact_existing_accounts(vault, monkeypatch):
    import run as entry
    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setenv("CALLIMACHUS_PACKAGE_KEYCHAIN_ID", "3ead31ec-f68a-4695-b040-2b55d91cc160")
    assert entry._probe_macos_keychain_update("create") == 0
    original = dict(vault.values)
    vault.grants.clear()
    backend = store._trusted_backend()
    def authorize(service, account):
        assert service == "Callimachus.PackageSmoke"
        vault.interactive.append(account)
        vault.grants.add(account)
        return vault.values.get(account)
    original_factory = store._trusted_backend
    def factory():
        item=original_factory(); item.get_password=authorize; return item
    monkeypatch.setattr(store,"_trusted_backend",factory)
    assert entry._probe_macos_keychain_update("recover") == 0
    assert vault.values == original and len(vault.interactive) == 1
    assert entry._probe_macos_keychain_update("check") == 0
    assert len(vault.interactive) == 1
