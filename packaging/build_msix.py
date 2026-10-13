# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build an unsigned Microsoft Store MSIX from the checked Windows x64 dist."""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "desktop"
DIST = BUILD_ROOT / "dist"
ARTIFACTS = BUILD_ROOT / "artifacts"
OUTPUT = ARTIFACTS / "Callimachus.msix"
LOGO_SOURCE = ROOT / "packaging" / "assets" / "Callimachus.png"

FRAMEWORK_NAME = "Microsoft.VCLibs.140.00.UWPDesktop"
FRAMEWORK_PUBLISHER = (
    "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, "
    "S=Washington, C=US"
)
FRAMEWORK_ARCHITECTURE = "x64"
FRAMEWORK_DLLS = (
    "msvcp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
FOUNDATION_NS = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
UAP_NS = "http://schemas.microsoft.com/appx/manifest/uap/windows10"
RESCAP_NS = "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
MIN_WINDOWS_VERSION = "10.0.17763.0"

_INSTALLER_HELPER_PATH = ROOT / "packaging" / "build_windows_installer.py"
_INSTALLER_HELPER_NAME = "callimachus_windows_installer_helpers"


def _load_installer_helpers():
    """Load the existing helper by path so the packaging directory cannot shadow it."""
    spec = importlib.util.spec_from_file_location(_INSTALLER_HELPER_NAME, _INSTALLER_HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load Windows installer helpers: {_INSTALLER_HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INSTALLER_HELPERS = _load_installer_helpers()


def _validate_distribution() -> None:
    """Apply the NSIS distribution contract and reject packaging-only files."""
    original_dist = _INSTALLER_HELPERS.DIST
    _INSTALLER_HELPERS.DIST = DIST
    try:
        _INSTALLER_HELPERS._validate_distribution()
    finally:
        _INSTALLER_HELPERS.DIST = original_dist

    forbidden_suffixes = {".appx", ".msix", ".msi", ".nsi"}
    forbidden_names = {
        "callimachus-setup.exe",
        "install.exe",
        "install_vc_runtime.ps1",
        "installer.exe",
        "nsis-3.10-license.txt",
        "setup.exe",
        "uninstall.exe",
        "uninstaller.exe",
        "windows-installer.nsi",
    }
    forbidden: list[Path] = []
    for path in DIST.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.casefold()
        if (path.suffix.casefold() in forbidden_suffixes
                or name in forbidden_names
                or name.startswith(("vc_redist", "vcredist")) and path.suffix.casefold() == ".exe"
                or name.startswith("uninstall-") and path.suffix.casefold() in {".exe", ".ps1"}):
            forbidden.append(path)
    if forbidden:
        formatted = ", ".join(str(path.relative_to(DIST)) for path in sorted(forbidden))
        raise RuntimeError(f"Windows distribution contains installer or preflight artifacts: {formatted}")

    for path in DIST.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Windows distribution contains a symbolic link: {path.relative_to(DIST)}")


def _runtime_sources() -> list[Path]:
    original_build_root = _INSTALLER_HELPERS.BUILD_ROOT
    _INSTALLER_HELPERS.BUILD_ROOT = BUILD_ROOT
    try:
        return _INSTALLER_HELPERS._runtime_sources()
    finally:
        _INSTALLER_HELPERS.BUILD_ROOT = original_build_root


def _file_version(path: Path) -> tuple[int, int, int, int]:
    return _INSTALLER_HELPERS._file_version(path)


def _validate_identity_name(value: str) -> str:
    value = _required_text("Store identity name", value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,48}[A-Za-z0-9]", value):
        raise ValueError("Store identity name must be the exact 3-50 character package name from Partner Center")
    return value


def _required_text(label: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    normalized = value.strip()
    if not normalized or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} is required and cannot contain control characters")
    return normalized


def _validate_publisher_dn(value: str) -> str:
    value = _required_text("Store publisher distinguished name", value)
    components = [component.strip() for component in value.split(",")]
    if (not components
            or not re.match(r"(?i)^CN\s*=\s*\S", components[0])
            or any("=" not in component or not component.split("=", 1)[1].strip()
                   for component in components)):
        raise ValueError("Store publisher must be the distinguished name copied from Partner Center")
    return value


def _version_parts(value: str, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value):
        raise ValueError(f"{label} must contain four numeric components")
    parts = tuple(int(component) for component in value.split("."))
    if any(component > 65535 for component in parts):
        raise ValueError(f"{label} components must not exceed 65535")
    return parts  # type: ignore[return-value]


def _validate_store_version(value: str) -> str:
    parts = _version_parts(value, "Store package version")
    if parts[0] == 0:
        raise ValueError("Store package version must have a nonzero major component")
    if parts[3] != 0:
        raise ValueError("Microsoft Store package versions must use zero as the fourth component")
    return value


def _framework_manifest(appx_path: Path) -> tuple[str, ET.Element]:
    if appx_path.suffix.casefold() != ".appx":
        raise RuntimeError("the selected x64 Microsoft.VCLibs framework input must be an .appx file")
    if not appx_path.is_file():
        raise RuntimeError(f"selected Microsoft.VCLibs framework package is missing: {appx_path}")
    try:
        with zipfile.ZipFile(appx_path) as package:
            manifests = [info for info in package.infolist()
                         if info.filename.casefold() == "appxmanifest.xml"]
            if len(manifests) != 1 or manifests[0].is_dir():
                raise RuntimeError("framework package must contain one root AppxManifest.xml")
            if manifests[0].file_size > 1024 * 1024:
                raise RuntimeError("framework AppxManifest.xml is unexpectedly large")
            root = ET.fromstring(package.read(manifests[0]))
    except (zipfile.BadZipFile, ET.ParseError, OSError) as error:
        raise RuntimeError(f"could not read selected Microsoft.VCLibs framework package: {error}") from error

    if root.tag != f"{{{FOUNDATION_NS}}}Package":
        raise RuntimeError("framework AppxManifest.xml does not use the Windows 10 package schema")
    identity = root.find(f"{{{FOUNDATION_NS}}}Identity")
    properties = root.find(f"{{{FOUNDATION_NS}}}Properties")
    framework = properties.find(f"{{{FOUNDATION_NS}}}Framework") if properties is not None else None
    if identity is None or properties is None or framework is None:
        raise RuntimeError("framework manifest is missing its identity or Framework property")

    attributes = identity.attrib
    if attributes.get("Name") != FRAMEWORK_NAME:
        raise RuntimeError(f"framework package identity must be {FRAMEWORK_NAME}")
    if _normalize_dn(attributes.get("Publisher", "")) != _normalize_dn(FRAMEWORK_PUBLISHER):
        raise RuntimeError("framework package publisher is not Microsoft Corporation")
    if attributes.get("ProcessorArchitecture", "").casefold() != FRAMEWORK_ARCHITECTURE.casefold():
        raise RuntimeError("framework package architecture must be x64")
    # Package family identity is the manifest Name plus the publisher-derived ID;
    # the exact Microsoft name and DN above prevent a similarly named package.
    if attributes.get("ResourceId", ""):
        raise RuntimeError("framework package must be the base package family, not a resource package")
    if framework.text is None or framework.text.strip().casefold() != "true":
        raise RuntimeError("selected Microsoft.VCLibs package is not declared as a framework package")
    framework_version = attributes.get("Version", "")
    _version_parts(framework_version, "Microsoft.VCLibs package version")
    return framework_version, root


def _normalize_dn(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _extract_framework_dlls(appx_path: Path, destination: Path) -> dict[str, Path]:
    """Copy only the three x64 VC runtime DLLs to fixed safe destination names."""
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    extracted: dict[str, Path] = {}
    try:
        with zipfile.ZipFile(appx_path) as package:
            infos = package.infolist()
            for dll_name in FRAMEWORK_DLLS:
                expected_member = dll_name.casefold()
                matches = [info for info in infos if info.filename.casefold() == expected_member]
                if len(matches) != 1:
                    raise RuntimeError(f"framework package must contain exactly one root {dll_name}")
                info = matches[0]
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_ISLNK(mode) or info.file_size == 0:
                    raise RuntimeError(f"framework package contains an invalid {dll_name} entry")
                target = destination_root / dll_name
                if target.parent != destination_root:
                    raise RuntimeError("framework DLL extraction target escaped the build temporary directory")
                with package.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted[dll_name] = target
    except (zipfile.BadZipFile, OSError) as error:
        if isinstance(error, FileExistsError):
            raise RuntimeError(f"framework extraction target already exists: {error}") from error
        raise RuntimeError(f"could not extract Microsoft.VCLibs runtime DLLs: {error}") from error
    return extracted


def _validate_framework_runtime(framework_dlls: dict[str, Path]) -> None:
    sources = _runtime_sources()
    by_name: dict[str, list[Path]] = {}
    for source in sources:
        name = Path(source).name.casefold()
        if name in {dll.casefold() for dll in FRAMEWORK_DLLS}:
            by_name.setdefault(name, []).append(Path(source))
    missing = sorted({dll.casefold() for dll in FRAMEWORK_DLLS} - by_name.keys())
    if missing:
        raise RuntimeError(f"externalized Windows build is missing runtime DLLs: {', '.join(missing)}")

    for dll_name in FRAMEWORK_DLLS:
        source_versions = [(_file_version(path), path) for path in by_name[dll_name.casefold()]]
        build_version, build_path = max(source_versions, key=lambda item: item[0][:3])
        framework_version = _file_version(framework_dlls[dll_name])
        if framework_version[:3] < build_version[:3]:
            required = ".".join(str(part) for part in build_version[:3])
            found = ".".join(str(part) for part in framework_version[:3])
            raise RuntimeError(
                f"Microsoft.VCLibs {dll_name} version {found} is older than the externalized "
                f"build DLL {build_path} version {required}"
            )


def _resolve_makeappx(value: str | Path | None) -> str:
    if value is not None:
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            raise RuntimeError(f"MakeAppx executable was not found: {candidate}")
        return str(candidate.resolve())
    executable = shutil.which("MakeAppx.exe") or shutil.which("makeappx")
    if executable is None:
        raise RuntimeError("MakeAppx was not found; supply --makeappx or install the Windows SDK")
    return executable


def _stage_logo_assets(destination: Path) -> None:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter
    except ImportError as error:
        raise RuntimeError("PySide6 is required to stage transparent MSIX logo assets") from error

    source = QImage(str(LOGO_SOURCE))
    if source.isNull():
        raise RuntimeError(f"MSIX logo source could not be read: {LOGO_SOURCE}")
    if not source.hasAlphaChannel():
        raise RuntimeError(f"MSIX logo source must preserve transparency: {LOGO_SOURCE}")

    destination.mkdir(parents=True, exist_ok=True)
    for name, size in (("Square44x44Logo.png", 44), ("Square150x150Logo.png", 150), ("StoreLogo.png", 50)):
        canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        scaled = source.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        painter.end()
        output = destination / name
        if not canvas.save(str(output), "PNG"):
            raise RuntimeError(f"could not write MSIX logo asset: {output}")


def _manifest_bytes(
    *,
    identity_name: str,
    publisher: str,
    version: str,
    publisher_display_name: str,
    framework_version: str,
) -> bytes:
    for prefix, namespace in (("", FOUNDATION_NS), ("uap", UAP_NS), ("rescap", RESCAP_NS)):
        ET.register_namespace(prefix, namespace)
    package = ET.Element(
        f"{{{FOUNDATION_NS}}}Package",
        {"IgnorableNamespaces": "uap rescap"},
    )
    ET.SubElement(package, f"{{{FOUNDATION_NS}}}Identity", {
        "Name": identity_name,
        "Publisher": publisher,
        "Version": version,
        "ProcessorArchitecture": "x64",
    })
    properties = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Properties")
    ET.SubElement(properties, f"{{{FOUNDATION_NS}}}DisplayName").text = "Callimachus"
    ET.SubElement(properties, f"{{{FOUNDATION_NS}}}PublisherDisplayName").text = publisher_display_name
    ET.SubElement(properties, f"{{{FOUNDATION_NS}}}Description").text = "Callimachus citation verification desktop application"
    ET.SubElement(properties, f"{{{FOUNDATION_NS}}}Logo").text = r"Assets\StoreLogo.png"

    dependencies = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Dependencies")
    ET.SubElement(dependencies, f"{{{FOUNDATION_NS}}}TargetDeviceFamily", {
        "Name": "Windows.Desktop",
        "MinVersion": MIN_WINDOWS_VERSION,
        # Keep the declared tested range at the specified deployment floor.
        "MaxVersionTested": MIN_WINDOWS_VERSION,
    })
    ET.SubElement(dependencies, f"{{{FOUNDATION_NS}}}PackageDependency", {
        "Name": FRAMEWORK_NAME,
        "Publisher": FRAMEWORK_PUBLISHER,
        "MinVersion": framework_version,
    })
    resources = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Resources")
    ET.SubElement(resources, f"{{{FOUNDATION_NS}}}Resource", {"Language": "en-us"})
    ET.SubElement(resources, f"{{{FOUNDATION_NS}}}Resource", {"Language": "it-it"})

    capabilities = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Capabilities")
    ET.SubElement(capabilities, f"{{{RESCAP_NS}}}Capability", {"Name": "runFullTrust"})
    applications = ET.SubElement(package, f"{{{FOUNDATION_NS}}}Applications")
    application = ET.SubElement(applications, f"{{{FOUNDATION_NS}}}Application", {
        "Id": "Callimachus",
        "Executable": r"Callimachus\Callimachus.exe",
        "EntryPoint": "Windows.FullTrustApplication",
    })
    ET.SubElement(application, f"{{{UAP_NS}}}VisualElements", {
        "DisplayName": "Callimachus",
        "Description": "Callimachus citation verification desktop application",
        "Square150x150Logo": r"Assets\Square150x150Logo.png",
        "Square44x44Logo": r"Assets\Square44x44Logo.png",
        "BackgroundColor": "transparent",
    })
    return ET.tostring(package, encoding="utf-8", xml_declaration=True)


def _validate_build_arguments(
    *, identity_name: str, publisher: str, version: str,
    publisher_display_name: str, framework_appx: str | Path,
) -> tuple[str, str, str, str, Path]:
    identity_name = _validate_identity_name(identity_name)
    publisher = _validate_publisher_dn(publisher)
    version = _validate_store_version(version)
    publisher_display_name = _required_text("Store publisher display name", publisher_display_name)
    if len(publisher_display_name) > 256:
        raise ValueError("Store publisher display name cannot exceed 256 characters")
    appx_path = Path(framework_appx).expanduser()
    return identity_name, publisher, version, publisher_display_name, appx_path


def build(
    *,
    identity_name: str,
    publisher: str,
    version: str,
    publisher_display_name: str,
    framework_appx: str | Path,
    makeappx: str | Path | None = None,
) -> Path:
    """Package the existing onedir distribution without signing or installing it."""
    if sys.platform != "win32":
        raise RuntimeError("the Microsoft Store MSIX must be built on Windows")
    identity_name, publisher, version, publisher_display_name, framework_appx = _validate_build_arguments(
        identity_name=identity_name,
        publisher=publisher,
        version=version,
        publisher_display_name=publisher_display_name,
        framework_appx=framework_appx,
    )
    _validate_distribution()
    framework_version, _ = _framework_manifest(framework_appx)
    makeappx_executable = _resolve_makeappx(makeappx)

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="msix-build-", dir=BUILD_ROOT) as temporary:
        work_root = Path(temporary)
        package_root = work_root / "package"
        shutil.copytree(DIST, package_root)
        framework_directory = work_root / "framework-dlls"
        framework_dlls = _extract_framework_dlls(framework_appx, framework_directory)
        _validate_framework_runtime(framework_dlls)
        _stage_logo_assets(package_root / "Assets")
        (package_root / "AppxManifest.xml").write_bytes(_manifest_bytes(
            identity_name=identity_name,
            publisher=publisher,
            version=version,
            publisher_display_name=publisher_display_name,
            framework_version=framework_version,
        ))

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        temporary_output = ARTIFACTS / f".Callimachus-{uuid.uuid4().hex}.msix"
        try:
            subprocess.run(
                [
                    makeappx_executable,
                    "pack",
                    "/d",
                    str(package_root),
                    "/p",
                    str(temporary_output),
                    "/o",
                ],
                cwd=ROOT,
                check=True,
                timeout=900,
            )
            if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
                raise RuntimeError(f"MakeAppx did not produce an MSIX package: {temporary_output}")
            temporary_output.replace(OUTPUT)
            return OUTPUT
        finally:
            temporary_output.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-name", required=True, help="exact reserved package identity name from Partner Center")
    parser.add_argument("--publisher", required=True, help="exact publisher distinguished name from Partner Center")
    parser.add_argument("--version", required=True, help="Store package version in major.minor.build.0 form")
    parser.add_argument("--publisher-display-name", required=True, help="publisher display name from Partner Center")
    parser.add_argument("--framework-appx", required=True, type=Path, help="selected x64 Microsoft.VCLibs framework .appx")
    parser.add_argument("--makeappx", type=Path, help="MakeAppx executable path; otherwise search PATH")
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    arguments = _build_parser().parse_args(argv)
    print(build(
        identity_name=arguments.identity_name,
        publisher=arguments.publisher,
        version=arguments.version,
        publisher_display_name=arguments.publisher_display_name,
        framework_appx=arguments.framework_appx,
        makeappx=arguments.makeappx,
    ))


if __name__ == "__main__":
    main()
