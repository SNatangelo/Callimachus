"""Fail-closed recovery of the inputs for the Store build."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

import pytest


def load_recovery():
    path = Path(__file__).resolve().parents[1] / "packaging/store/prepare_sources.py"
    spec = importlib.util.spec_from_file_location("store_source_recovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_tools():
    path = Path(__file__).resolve().parents[1] / "packaging/store/prepare_tools.py"
    spec = importlib.util.spec_from_file_location("store_tool_recovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_input_plan_covers_sources_and_upstream_wheels():
    rows = load_recovery().source_inputs()
    names = [item["filename"] for item in rows]
    assert len(names) == len(set(names))
    assert "Python-3.13.15.tar.xz" in names
    assert "qt-everywhere-src-6.11.2.tar.xz" in names
    assert "pyside-setup-everywhere-src-6.11.2.tar.xz" in names
    assert "mupdf-1.28.0-source.tar.gz" in names
    assert "omegaconf-2.0.6-py3-none-any.whl" in names
    assert not any("output/" in row["url"] for row in rows)
    assert all(row["url"].startswith("https://") and len(row["sha256"]) == 64 for row in rows)


def test_existing_wrong_hash_is_rejected_without_overwrite(tmp_path):
    recovery = load_recovery()
    target = tmp_path / "source.tar.gz"
    target.write_bytes(b"wrong input")
    row = {"filename": target.name, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="wrong SHA256"):
        recovery.fetch(row, tmp_path)
    assert target.read_bytes() == b"wrong input"


def test_download_wrong_hash_is_not_promoted(tmp_path, monkeypatch):
    recovery = load_recovery()
    monkeypatch.setattr(recovery.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"wrong input"))
    row = {"filename": "input.tar.gz", "url": "https://example.invalid/input", "sha256": "0" * 64}
    with pytest.raises(ValueError, match="wrong SHA256"):
        recovery.fetch(row, tmp_path)
    assert not (tmp_path / row["filename"]).exists()


def test_verified_download_can_be_reused_offline(tmp_path, monkeypatch):
    recovery = load_recovery()
    data = b"verified input"
    row = {"filename": "input.tar.gz", "url": "https://example.invalid/input",
           "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(recovery.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data))
    target = recovery.fetch(row, tmp_path)
    monkeypatch.setattr(recovery.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    assert recovery.fetch(row, tmp_path) == target


def test_source_extraction_preserves_existing_tree(tmp_path):
    recovery = load_recovery()
    archive = tmp_path / "input.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("source-1.0/example.txt")
        member.size = 3
        output.addfile(member, io.BytesIO(b"abc"))
    native = tmp_path / "native"
    recovery.extract({"destination": "."}, archive, native)
    extracted = native / "source-1.0/example.txt"
    extracted.write_text("existing edit")
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        recovery.extract({"destination": "."}, archive, native)
    assert extracted.read_text() == "existing edit"


def test_source_traversal_is_rejected(tmp_path):
    recovery = load_recovery()
    archive = tmp_path / "input.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escape.txt")
        member.size = 3
        output.addfile(member, io.BytesIO(b"abc"))
    with pytest.raises(ValueError, match="Unsafe source root"):
        recovery.extract({"destination": "."}, archive, tmp_path / "native")
    assert not (tmp_path / "escape.txt").exists()


def test_framework_staging_checks_receipt_and_populates_qt(tmp_path):
    path = Path(__file__).resolve().parents[1] / "packaging/store/native-recipes/stage-framework-runtime.py"
    spec = importlib.util.spec_from_file_location("store_framework_stage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = tmp_path / "native-14.40"
    qt = native / "final-venv/Lib/site-packages/PySide6"
    qt.mkdir(parents=True)
    appx = tmp_path / "tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx"
    appx.parent.mkdir(parents=True)
    with zipfile.ZipFile(appx, "w") as archive:
        archive.writestr("vcruntime140.dll", b"pinned runtime")
    receipt = {"appx_sha256": hashlib.sha256(appx.read_bytes()).hexdigest(), "dlls": [
        {"appx_path": "vcruntime140.dll", "sha256": hashlib.sha256(b"pinned runtime").hexdigest()}]}
    manifest = native / "framework-runtime-manifest.json"
    manifest.write_text(json.dumps(receipt))
    module.stage(native)
    assert (qt / "vcruntime140.dll").read_bytes() == b"pinned runtime"
    assert json.loads(manifest.read_text()) == receipt
    with appx.open("ab") as stream:
        stream.write(b"changed input")
    with pytest.raises(RuntimeError, match="differs from the pinned receipt"):
        module.stage(native)
    assert json.loads(manifest.read_text()) == receipt


def test_tool_manifest_pins_framework_and_has_public_recovery_inputs():
    tools = load_tools()
    manifest = tools.read_json(tools.MANIFEST)
    framework = json.loads((tools.STORE / "native-recipes/framework-runtime-manifest.json").read_text())
    assert manifest["vclibs"]["appx_sha256"] == framework["appx_sha256"]
    for row in tools.rows(manifest):
        assert row["url"].startswith("https://")
        assert len(row["sha256"]) == 64
    assert manifest["visual_studio"]["expected_compiler_version"] == "19.39.33523"
    assert manifest["qt_libclang"]["version"] == "18.1.7"


def test_sdk_merge_rejects_conflicting_contents(tmp_path):
    tools = load_tools()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "header.h").write_text("new SDK")
    (target / "header.h").write_text("existing SDK")
    with pytest.raises(ValueError, match="Conflicting SDK package member"):
        tools.merge_sdk_payload(source, target)
    assert (target / "header.h").read_text() == "existing SDK"


def test_tool_archive_traversal_is_rejected_without_output(tmp_path):
    tools = load_tools()
    archive = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", b"unsafe")
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="Unsafe archive member"):
        tools.extract_zip(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_tool_download_wrong_hash_is_not_promoted(tmp_path, monkeypatch):
    tools = load_tools()
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"wrong package"))
    destination = tmp_path / "input.nupkg"
    with pytest.raises(ValueError, match="wrong sha256"):
        tools.download("https://example.invalid/input.nupkg", destination,
                       algorithm="sha256", expected="0" * 64)
    assert not destination.exists()


def test_rust_components_create_the_driver_prefix(tmp_path):
    tools = load_tools()
    distribution = tmp_path / "rust"
    for name, relative in (("rustc", "bin/rustc.exe"), ("cargo", "bin/cargo.exe"),
                           ("rust-std-x86_64-pc-windows-msvc", "lib/rustlib/test.rlib")):
        component = distribution / name
        target = component / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(name.encode())
        (component / "manifest.in").write_text(f"file:{relative}\n")
    prefix = tools.materialize_rust(distribution)
    assert (prefix / "bin/rustc.exe").read_bytes() == b"rustc"
    assert (prefix / "bin/cargo.exe").read_bytes() == b"cargo"
    assert (prefix / "lib/rustlib/test.rlib").is_file()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        tools.materialize_rust(distribution)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell build recipe")
def test_native_plan_needs_no_existing_inputs_and_writes_nothing(tmp_path):
    shell = shutil.which("powershell.exe")
    assert shell, "Windows PowerShell is required for the Store recipe"
    project = Path(__file__).resolve().parents[1]
    recipe = project / "packaging/store/native-recipes/rebuild-native.ps1"
    native = tmp_path / "empty-native"
    tools = tmp_path / "empty-tools"
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(recipe),
         "-NativeRoot", str(native), "-ToolsRoot", str(tools)],
        cwd=project, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "PLAN ONLY" in result.stdout
    assert result.stdout.count("=== Stage:") == 10
    assert "onnx-fresh-cmake-configure" in result.stdout
    assert "pyinstaller-bootloader" in result.stdout
    assert not native.exists()
    assert not tools.exists()
