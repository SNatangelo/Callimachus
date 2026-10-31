# tests/test_msix_build.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Microsoft Store MSIX builder regressions without invoking MakeAppx."""
from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_NS = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
UAP_NS = "http://schemas.microsoft.com/appx/manifest/uap/windows10"
RESCAP_NS = "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
STORE_PUBLISHER = "CN=Callimachus, O=Callimachus, L=Rome, S=RM, C=IT"
STORE_PUBLISHER_DISPLAY_NAME = "Callimachus"
FRAMEWORK_PUBLISHER = (
    "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, "
    "S=Washington, C=US"
)
FRAMEWORK_VERSION = "14.0.33728.0"
RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
TARGET_SIZES = (16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256)


def _expected_target_assets():
    return {
        f"Square44x44Logo.targetsize-{size}{suffix}.png": size
        for size in TARGET_SIZES
        for suffix in ("", "_altform-unplated", "_altform-lightunplated")
    }


def _fake_makepri(argv, *, omit_candidate=False, logical_name="Square44x44Logo.png"):
    if argv[0] != "MakePri.exe":
        return False
    command = argv[1]
    if command == "createconfig":
        assert argv[-2:] == ["/pv", "10.0.0"]
        Path(argv[argv.index("/cf") + 1]).write_text(
            '<resources><packaging><autoResourcePackage qualifier="Scale"/></packaging>'
            '<index root="" startIndexAt=""/></resources>', encoding="utf-8",
        )
    elif command == "new":
        resource_root = Path(argv[argv.index("/pr") + 1])
        assert {p.name for p in resource_root.iterdir()} == {"Assets", "AppxManifest.xml"}
        config = ET.parse(argv[argv.index("/cf") + 1]).getroot()
        assert config.find("packaging") is None
        assert Path(argv[argv.index("/mn") + 1]) == resource_root / "AppxManifest.xml"
        Path(argv[argv.index("/of") + 1]).write_bytes(b"synthetic PRI")
    elif command == "dump":
        assert argv[-2:] == ["/dt", "Detailed"]
        root = ET.Element("PriInfo")
        resource = ET.SubElement(root, "NamedResource", {"name": logical_name})
        for name in list(_expected_target_assets())[int(omit_candidate):]:
            candidate = ET.SubElement(resource, "Candidate")
            ET.SubElement(candidate, "Value").text = "Assets\\" + name
        ET.ElementTree(root).write(argv[argv.index("/of") + 1], encoding="utf-8")
    else:
        raise AssertionError(f"Unexpected MakePri command: {argv}")
    return True


def _load_builder():
    path = ROOT / "packaging" / "build_msix.py"
    spec = importlib.util.spec_from_file_location("msix_build_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _framework_manifest(
    *,
    name: str = "Microsoft.VCLibs.140.00.UWPDesktop",
    publisher: str = FRAMEWORK_PUBLISHER,
    architecture: str = "x64",
    version: str = FRAMEWORK_VERSION,
    framework: str = "true",
    resource_id: str = "",
) -> bytes:
    package = ET.Element(f"{{{FOUNDATION_NS}}}Package")
    ET.SubElement(package, f"{{{FOUNDATION_NS}}}Identity", {
        "Name": name,
        "Publisher": publisher,
        "Version": version,
        "ProcessorArchitecture": architecture,
        "ResourceId": resource_id,
    })
    properties = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Properties")
    ET.SubElement(properties, f"{{{FOUNDATION_NS}}}Framework").text = framework
    return ET.tostring(package, encoding="utf-8", xml_declaration=True)


def _make_framework(
    path: Path,
    *,
    manifest: bytes | None = None,
    dll_names: tuple[str, ...] = RUNTIME_DLLS,
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("AppxManifest.xml", manifest or _framework_manifest())
        for name in dll_names:
            package.writestr(name, f"framework-{name}".encode("ascii"))
        for member, contents in extra_members:
            package.writestr(member, contents)
    return path


def _make_distribution(dist: Path) -> None:
    executable = dist / "Callimachus" / "Callimachus.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"callimachus executable")
    (dist / "LICENSE").write_text("license text", encoding="utf-8")
    notices = dist / "THIRD-PARTY-NOTICES"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    (notices / "notice.txt").write_text("notice", encoding="utf-8")


def _configured_builder(monkeypatch, tmp_path: Path, framework_appx: Path):
    build = _load_builder()
    build_root = tmp_path / "build" / "desktop"
    dist = build_root / "dist"
    _make_distribution(dist)
    runtime_root = tmp_path / "externalized-runtime"
    runtime_root.mkdir()
    runtime_sources = []
    for name in RUNTIME_DLLS:
        source = runtime_root / name
        source.write_bytes(f"build-{name}".encode("ascii"))
        runtime_sources.append(source)

    monkeypatch.setattr(build, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(build, "BUILD_ROOT", build_root)
    monkeypatch.setattr(build, "DIST", dist)
    monkeypatch.setattr(build, "ARTIFACTS", build_root / "artifacts")
    monkeypatch.setattr(build, "OUTPUT", build_root / "artifacts" / "Callimachus.msix")
    monkeypatch.setattr(build, "_runtime_sources", lambda: runtime_sources)
    runtime_paths = {path.resolve() for path in runtime_sources}

    def version(path: Path) -> tuple[int, int, int, int]:
        if path.resolve() in runtime_paths:
            return (14, 44, 35211, 0)
        return (14, 44, 35211, 4)

    monkeypatch.setattr(build, "_file_version", version)
    monkeypatch.setattr(
        build.shutil, "which",
        lambda name: {"makeappx.exe": "MakeAppx.exe", "makepri.exe": "MakePri.exe"}.get(name.casefold()),
    )
    return build, framework_appx, runtime_sources


def _build_arguments(framework_appx: Path) -> dict[str, object]:
    return {
        "identity_name": "Callimachus.Store",
        "publisher": STORE_PUBLISHER,
        "version": "1.2.3.0",
        "publisher_display_name": STORE_PUBLISHER_DISPLAY_NAME,
        "framework_appx": framework_appx,
    }


def test_build_stages_dist_and_store_manifest_then_calls_makeappx(monkeypatch, tmp_path):
    build, framework_appx, _ = _configured_builder(
        monkeypatch, tmp_path, _make_framework(tmp_path / "vclibs-x64.appx"),
    )
    dist_before = {
        path.relative_to(build.DIST): path.read_bytes()
        for path in build.DIST.rglob("*") if path.is_file()
    }
    calls = []

    def fake_makeappx(argv, **kwargs):
        calls.append((argv, kwargs))
        if _fake_makepri(argv):
            return
        assert argv[:2] == ["MakeAppx.exe", "pack"]
        assert kwargs["cwd"] == build.ROOT
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 900
        package_root = Path(argv[argv.index("/d") + 1])
        output = Path(argv[argv.index("/p") + 1])
        assert argv[-1] == "/o"
        assert (package_root / "Callimachus" / "Callimachus.exe").read_bytes() == b"callimachus executable"
        assert (package_root / "LICENSE").read_text(encoding="utf-8") == "license text"
        assert (package_root / "THIRD-PARTY-NOTICES" / "notice.txt").read_text(encoding="utf-8") == "notice"
        assert not any(path.name in RUNTIME_DLLS for path in package_root.rglob("*"))
        assert not (package_root / framework_appx.name).exists()
        manifest_path = package_root / "AppxManifest.xml"
        manifest = ET.parse(manifest_path).getroot()
        identity = manifest.find(f"{{{FOUNDATION_NS}}}Identity")
        assert identity is not None
        assert identity.attrib == {
            "Name": "Callimachus.Store",
            "Publisher": STORE_PUBLISHER,
            "Version": "1.2.3.0",
            "ProcessorArchitecture": "x64",
        }
        publisher_display_name = manifest.findtext(f"{{{FOUNDATION_NS}}}Properties/{{{FOUNDATION_NS}}}PublisherDisplayName")
        assert publisher_display_name == STORE_PUBLISHER_DISPLAY_NAME
        dependency = manifest.find(f"{{{FOUNDATION_NS}}}Dependencies/{{{FOUNDATION_NS}}}PackageDependency")
        assert dependency is not None
        assert dependency.attrib["Name"] == "Microsoft.VCLibs.140.00.UWPDesktop"
        assert dependency.attrib["Publisher"] == FRAMEWORK_PUBLISHER
        # The framework package version stays 14.0.x even though its DLLs are 14.44.x.
        assert dependency.attrib["MinVersion"] == FRAMEWORK_VERSION
        assert dependency.attrib["MinVersion"].startswith("14.0.")
        target = manifest.find(f"{{{FOUNDATION_NS}}}Dependencies/{{{FOUNDATION_NS}}}TargetDeviceFamily")
        assert target is not None
        assert target.attrib == {
            "Name": "Windows.Desktop",
            "MinVersion": "10.0.17763.0",
            "MaxVersionTested": "10.0.17763.0",
        }
        # Windows SDK schema requires deployment targets before framework dependencies.
        assert list(manifest.find(f"{{{FOUNDATION_NS}}}Dependencies")) == [target, dependency]
        resource_languages = sorted(
            resource.attrib["Language"]
            for resource in manifest.findall(f"{{{FOUNDATION_NS}}}Resources/{{{FOUNDATION_NS}}}Resource")
        )
        assert resource_languages == ["en-us", "it-it"]
        application = manifest.find(f"{{{FOUNDATION_NS}}}Applications/{{{FOUNDATION_NS}}}Application")
        assert application is not None
        assert application.attrib == {
            "Id": "Callimachus",
            "Executable": r"Callimachus\Callimachus.exe",
            "EntryPoint": "Windows.FullTrustApplication",
        }
        visual_elements = application.find(f"{{{UAP_NS}}}VisualElements")
        assert visual_elements is not None
        assert visual_elements.attrib["Square44x44Logo"] == r"Assets\Square44x44Logo.png"
        assert visual_elements.attrib["Square150x150Logo"] == r"Assets\Square150x150Logo.png"
        assert visual_elements.attrib["BackgroundColor"] == "transparent"
        capability = manifest.find(f"{{{FOUNDATION_NS}}}Capabilities/{{{RESCAP_NS}}}Capability")
        assert capability is not None and capability.attrib == {"Name": "runFullTrust"}
        assert list(manifest.find(f"{{{FOUNDATION_NS}}}Capabilities")) == [capability]
        assert manifest.attrib["IgnorableNamespaces"] == "uap rescap"
        resources = manifest.findall(f"{{{FOUNDATION_NS}}}Resources/{{{FOUNDATION_NS}}}Resource")
        assert sorted(resource.attrib["Language"] for resource in resources) == ["en-us", "it-it"]
        assert (package_root / "resources.pri").read_bytes() == b"synthetic PRI"
        from PySide6.QtGui import QImage

        expected_assets = {"Square44x44Logo.png": 44, "Square150x150Logo.png": 150, "StoreLogo.png": 50}
        expected_assets.update(_expected_target_assets())
        assert {path.name for path in (package_root / "Assets").iterdir()} == set(expected_assets)
        for name, size in expected_assets.items():
            image = QImage(str(package_root / "Assets" / name))
            assert (image.width(), image.height()) == (size, size)
            assert image.hasAlphaChannel()
            # Keep the rounded N1 logo corners transparent in shell surfaces.
            for x, y in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
                assert image.pixelColor(x, y).alpha() == 0
            assert image.pixelColor(size // 2, size // 2).alpha() == 255
        output.write_bytes(b"mocked unsigned msix")

    monkeypatch.setattr(build.subprocess, "run", fake_makeappx)
    result = build.build(**_build_arguments(framework_appx))

    assert result == build.OUTPUT
    assert result.read_bytes() == b"mocked unsigned msix"
    assert [call[0][1] for call in calls] == ["createconfig", "new", "dump", "pack"]
    assert not framework_appx.is_relative_to(build.DIST)
    dist_after = {
        path.relative_to(build.DIST): path.read_bytes()
        for path in build.DIST.rglob("*") if path.is_file()
    }
    assert dist_after == dist_before
    assert not (build.DIST / "AppxManifest.xml").exists()
    assert not (build.ARTIFACTS / f".Callimachus-{ '0' * 32 }.msix").exists()
    assert not list(build.BUILD_ROOT.glob("msix-build-*"))


@pytest.mark.parametrize(
    ("manifest", "message"),
    (
        (_framework_manifest(name="Microsoft.VCLibs.140.00"), "identity must be"),
        (_framework_manifest(publisher="CN=Other, O=Other, L=Elsewhere, S=State, C=US"), "publisher is not Microsoft"),
        (_framework_manifest(architecture="arm64"), "architecture must be x64"),
        (_framework_manifest(framework="false"), "not declared as a framework"),
        (_framework_manifest(resource_id="en-us"), "resource package"),
    ),
)
def test_framework_package_identity_must_match_x64_microsoft_family(tmp_path, manifest, message):
    build = _load_builder()
    package = _make_framework(tmp_path / "framework.appx", manifest=manifest)

    with pytest.raises(RuntimeError, match=message):
        build._framework_manifest(package)


@pytest.mark.parametrize("version", ("1.2.3", "1.2.3.4", "0.2.3.0", "1.2.3.65536", "1.2.x.0"))
def test_store_package_version_requires_four_valid_components_and_zero_revision(version):
    build = _load_builder()

    with pytest.raises(ValueError):
        build._validate_store_version(version)


def test_cli_requires_real_store_identity_inputs():
    build = _load_builder()
    parser = build._build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args([])

    assert error.value.code == 2


def test_store_publisher_distinguished_name_accepts_single_partner_center_cn():
    build = _load_builder()
    publisher = "CN=01234567-89ab-cdef-0123-456789abcdef"

    assert build._validate_publisher_dn(publisher) == publisher


def test_framework_extraction_ignores_traversal_entries_and_only_writes_exact_dlls(tmp_path):
    build = _load_builder()
    package = _make_framework(
        tmp_path / "vclibs.appx",
        extra_members=(("VFS/SystemX64/../../outside.dll", b"must not escape"),),
    )
    destination = tmp_path / "build-temp" / "dlls"

    extracted = build._extract_framework_dlls(package, destination)

    assert set(extracted) == set(RUNTIME_DLLS)
    assert {path.name for path in destination.iterdir()} == set(RUNTIME_DLLS)
    assert not (tmp_path / "outside.dll").exists()


def test_framework_dlls_must_be_at_archive_root_not_under_vfs(tmp_path):
    build = _load_builder()
    package = _make_framework(
        tmp_path / "nested-vclibs.appx",
        dll_names=(),
        extra_members=tuple(
            (f"VFS/SystemX64/{name}", f"nested-{name}".encode("ascii"))
            for name in RUNTIME_DLLS
        ),
    )

    with pytest.raises(RuntimeError, match="exactly one root msvcp140.dll"):
        build._extract_framework_dlls(package, tmp_path / "build-temp" / "dlls")


def test_framework_extraction_rejects_duplicate_root_dll_members(tmp_path):
    build = _load_builder()
    with pytest.warns(UserWarning, match="Duplicate name: 'msvcp140.dll'"):
        package = _make_framework(
            tmp_path / "duplicate-vclibs.appx",
            extra_members=(("msvcp140.dll", b"duplicate"),),
        )

    with pytest.raises(RuntimeError, match="exactly one root msvcp140.dll"):
        build._extract_framework_dlls(package, tmp_path / "build-temp" / "dlls")


def test_missing_framework_dll_blocks_makeappx(monkeypatch, tmp_path):
    framework = _make_framework(tmp_path / "missing-dll.appx", dll_names=RUNTIME_DLLS[:2])
    build, framework, _ = _configured_builder(monkeypatch, tmp_path, framework)
    calls = []
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RuntimeError, match="exactly one root vcruntime140_1.dll"):
        build.build(**_build_arguments(framework))

    assert calls == []
    assert not build.OUTPUT.exists()


def test_older_framework_runtime_dll_blocks_makeappx(monkeypatch, tmp_path):
    framework = _make_framework(tmp_path / "old-vclibs.appx")
    build, framework, sources = _configured_builder(monkeypatch, tmp_path, framework)
    runtime_paths = {path.resolve() for path in sources}

    def version(path: Path) -> tuple[int, int, int, int]:
        if path.resolve() in runtime_paths:
            return (14, 44, 35211, 0)
        return (14, 42, 34433, 0)

    monkeypatch.setattr(build, "_file_version", version)
    calls = []
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RuntimeError, match="is older than the externalized build DLL"):
        build.build(**_build_arguments(framework))

    assert calls == []
    assert not build.OUTPUT.exists()


def test_distribution_rejects_installer_and_preflight_leaks(monkeypatch, tmp_path):
    build = _load_builder()
    dist = tmp_path / "dist"
    _make_distribution(dist)
    (dist / "install_vc_runtime.ps1").write_text("preflight", encoding="utf-8")
    monkeypatch.setattr(build, "DIST", dist)

    with pytest.raises(RuntimeError, match="installer or preflight artifacts"):
        build._validate_distribution()


def test_distribution_allows_application_preflight_code_and_installer_metadata(monkeypatch, tmp_path):
    build = _load_builder()
    dist = tmp_path / "dist"
    _make_distribution(dist)
    (dist / "Callimachus" / "startup_preflight.py").write_text("application code", encoding="utf-8")
    (dist / "INSTALLER.json").write_text("dependency metadata", encoding="utf-8")
    monkeypatch.setattr(build, "DIST", dist)

    build._validate_distribution()

    assert (dist / "Callimachus" / "startup_preflight.py").is_file()
    assert (dist / "INSTALLER.json").is_file()


def test_makeappx_failure_does_not_leave_a_package_or_build_stage(monkeypatch, tmp_path):
    framework = _make_framework(tmp_path / "vclibs.appx")
    build, framework, _ = _configured_builder(monkeypatch, tmp_path, framework)

    def failed_makeappx(argv, **kwargs):
        if _fake_makepri(argv):
            return
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(build.subprocess, "run", failed_makeappx)

    with pytest.raises(subprocess.CalledProcessError):
        build.build(**_build_arguments(framework))

    assert not build.OUTPUT.exists()
    assert not list(build.BUILD_ROOT.glob("msix-build-*"))
    assert not list(build.ARTIFACTS.glob(".Callimachus-*.msix"))


@pytest.mark.parametrize("failure", ("command", "absent-index", "empty-index", "missing-candidate", "wrong-logical-resource"))
def test_makepri_failures_preserve_existing_package_and_block_makeappx(monkeypatch, tmp_path, failure):
    framework = _make_framework(tmp_path / "vclibs.appx")
    build, framework, _ = _configured_builder(monkeypatch, tmp_path, framework)
    build.OUTPUT.parent.mkdir(parents=True)
    build.OUTPUT.write_bytes(b"previous qualified package")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv[1])
        assert argv[0] == "MakePri.exe", "MakeAppx must not run after a failed resource index"
        if argv[1] == "new" and failure == "command":
            raise subprocess.CalledProcessError(1, argv)
        if argv[1] == "new" and failure == "absent-index":
            return
        _fake_makepri(
            argv, omit_candidate=failure == "missing-candidate",
            logical_name="literal-targetsize-16.png" if failure == "wrong-logical-resource" else "Square44x44Logo.png",
        )
        if argv[1] == "new" and failure == "empty-index":
            Path(argv[argv.index("/of") + 1]).write_bytes(b"")

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    error = subprocess.CalledProcessError if failure == "command" else RuntimeError
    with pytest.raises(error):
        build.build(**_build_arguments(framework))
    assert build.OUTPUT.read_bytes() == b"previous qualified package"
    assert "pack" not in commands
    assert not list(build.BUILD_ROOT.glob("msix-build-*"))


def test_makepri_resolution_prefers_selected_sdk_then_path(monkeypatch, tmp_path):
    build = _load_builder()
    sdk = tmp_path / "selected-sdk"
    sdk.mkdir()
    makeappx = sdk / "MakeAppx.exe"
    makeappx.write_bytes(b"synthetic tool, never executed")
    sibling = sdk / "makepri.exe"
    sibling.write_bytes(b"synthetic tool, never executed")
    calls = []
    monkeypatch.setattr(build.shutil, "which", lambda name: calls.append(name) or "path-makepri.exe")
    assert build._resolve_makepri(None, str(makeappx)) == str(sibling.resolve())
    assert calls == []
    explicit = tmp_path / "explicit.exe"
    explicit.write_bytes(b"synthetic tool, never executed")
    assert build._resolve_makepri(explicit, str(makeappx)) == str(explicit.resolve())
    sibling.unlink()
    assert build._resolve_makepri(None, str(makeappx)) == "path-makepri.exe"
    with pytest.raises(RuntimeError, match="MakePri executable was not found"):
        build._resolve_makepri(tmp_path / "missing.exe", str(makeappx))
    monkeypatch.setattr(build.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="MakePri was not found"):
        build._resolve_makepri(None, str(makeappx))
