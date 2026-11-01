from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    path = ROOT / "packaging" / "windows_gui.py"
    spec = importlib.util.spec_from_file_location("windows_gui_binary_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pe_image(*, magic: int = 0x10B, subsystem: int = 2) -> bytes:
    pe_offset = 0x80
    optional_size = 224 if magic == 0x10B else 240
    image = bytearray(pe_offset + 4 + 20 + optional_size)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset:pe_offset + 4] = b"PE\0\0"
    coff_offset = pe_offset + 4
    struct.pack_into("<H", image, coff_offset + 16, optional_size)
    optional_offset = coff_offset + 20
    struct.pack_into("<H", image, optional_offset, magic)
    struct.pack_into("<H", image, optional_offset + 68, subsystem)
    return bytes(image)


def _pe_with_bytes(offset: int, value: bytes) -> bytes:
    image = bytearray(_pe_image())
    image[offset:offset + len(value)] = value
    return bytes(image)


def _pe_with_optional_size(size: int) -> bytes:
    image = bytearray(_pe_image())
    struct.pack_into("<H", image, 0x94, size)
    return bytes(image)


def _pe_with_optional_magic(magic: int) -> bytes:
    image = bytearray(_pe_image())
    struct.pack_into("<H", image, 0x98, magic)
    return bytes(image)


def _write_image(tmp_path: Path, image: bytes) -> Path:
    path = tmp_path / "Callimachus.exe"
    path.write_bytes(image)
    return path


@pytest.mark.parametrize("magic", [0x10B, 0x20B], ids=["pe32", "pe32-plus"])
def test_accepts_windows_gui_pe32_and_pe32_plus(tmp_path, magic):
    validator = _load_validator()
    validator.verify_windows_gui_binary(_write_image(tmp_path, _pe_image(magic=magic)))


def test_rejects_windows_console_subsystem(tmp_path):
    validator = _load_validator()
    path = _write_image(tmp_path, _pe_image(subsystem=3))
    with pytest.raises(ValueError, match="IMAGE_SUBSYSTEM_WINDOWS_GUI"):
        validator.verify_windows_gui_binary(path)


@pytest.mark.parametrize(
    ("image", "message"),
    [
        pytest.param(b"not a PE", "truncated DOS header", id="short-dos-header"),
        pytest.param(_pe_with_bytes(0, b"NO"), "invalid DOS signature", id="bad-dos-signature"),
        pytest.param(
            _pe_with_bytes(0x80, b"NOPE"),
            "invalid PE signature",
            id="bad-pe-signature",
        ),
        pytest.param(
            _pe_with_optional_size(69),
            "invalid optional header size",
            id="short-optional-header",
        ),
        pytest.param(
            _pe_with_optional_size(0),
            "invalid optional header size",
            id="zero-optional-header-size",
        ),
        pytest.param(
            _pe_with_optional_magic(0x1234),
            "invalid PE optional header magic",
            id="unknown-optional-header-magic",
        ),
        pytest.param(
            _pe_image()[:-1],
            "truncated optional header",
            id="truncated-optional-header",
        ),
        pytest.param(
            _pe_with_bytes(0x3C, b"\x20\x00\x00\x00"),
            "invalid PE header offset",
            id="pe-header-overlaps-dos-header",
        ),
    ],
)
def test_rejects_malformed_or_truncated_pe_headers(tmp_path, image, message):
    validator = _load_validator()
    with pytest.raises(ValueError, match=message):
        validator.verify_windows_gui_binary(_write_image(tmp_path, image))

def test_frozen_smoke_rejects_console_exe_before_launch(monkeypatch, tmp_path):
    smoke_path = ROOT / "packaging" / "smoke_desktop.py"
    spec = importlib.util.spec_from_file_location("desktop_smoke_pe_gate", smoke_path)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    executable = _write_image(tmp_path, _pe_image(subsystem=3))
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    monkeypatch.setattr(smoke, "executable_path", lambda: executable)
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("frozen executable launched before PE validation"),
    )

    with pytest.raises(SystemExit, match="not a Windows GUI binary"):
        smoke.main()


def test_installer_smoke_rejects_console_exe_before_launch(monkeypatch, tmp_path):
    smoke_path = ROOT / "packaging" / "smoke_windows_installer.py"
    spec = importlib.util.spec_from_file_location("installer_smoke_pe_gate", smoke_path)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    setup = tmp_path / "Callimachus-Setup.exe"
    setup.write_bytes(b"synthetic installer")
    install_root = tmp_path / "install"
    monkeypatch.setattr(smoke, "SETUP", setup)
    monkeypatch.setattr(smoke, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        executable = install_root / "Callimachus" / "Callimachus.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(_pe_image(subsystem=3))
        notices = install_root / "THIRD-PARTY-NOTICES"
        notices.mkdir(exist_ok=True)
        (notices / "index.json").write_text("{}", encoding="utf-8")
        return smoke.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    with pytest.raises(SystemExit, match="not a Windows GUI binary"):
        smoke.main()
    assert calls == [[str(setup), "/S", f"/D={install_root}"]]