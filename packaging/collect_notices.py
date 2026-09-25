# packaging/collect_notices.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Collect the actual installed wheels' notices for a native release."""
from __future__ import annotations

import importlib.metadata as metadata
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]
DIRECT = (
    "certifi", "Brotli", "PyMuPDF", "pdfminer.six", "PySide6", "playwright",
    "pypdfium2", "rapidocr", "onnxruntime", "bm25s", "PyInstaller",
)
QT_TERMS = {
    "LGPL-3.0-only.txt": "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
    "GPL-3.0-only.txt": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    "GPL-2.0-only.txt": "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
    "Qt-GPL-exception-1.0.txt": "40678d338ce53cd93f8b22b281a2ecbcaa3ee65ce60b25ffb0c462b0530846b2",
}
QT_SOURCE = "https://raw.githubusercontent.com/qtproject/pyside-pyside-setup/v6.11.2/LICENSES/"
QT_VERSION = "6.11.2"
NCURSES_VERSION = "6.5"
NCURSES_SOURCE = "https://ftp.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz"
NCURSES_SHA256 = "136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6"
OPENSSL_VERSION = "3.0.21"
OPENSSL_SOURCE = "https://github.com/openssl/openssl/releases/download/openssl-3.0.21/openssl-3.0.21.tar.gz"
OPENSSL_SHA256 = "617e29af8e421f46649484a4937e48c685e47f46488167c982f88bc4ec1d522f"
ZLIB_VERSION = "1.3.1"
ZLIB_SOURCE = f"https://zlib.net/fossils/zlib-{ZLIB_VERSION}.tar.gz"
ZLIB_SHA256 = "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
EXTERNAL_WINDOWS_RUNTIME = frozenset({
    "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
})
LIBFFI_SOURCE = "https://github.com/libffi/libffi/releases/download/v3.4.4/libffi-3.4.4.tar.gz"
LIBFFI_SHA256 = "d66c56ad259a82cf2a9dfc408b32bf5da52371500b84745f7fb8b645712df676"
SQLITE_SOURCE = "https://www.sqlite.org/2025/sqlite-autoconf-3500400.tar.gz"
SQLITE_SHA256 = "a3db587a1b92ee5ddac2f66b3edb41b26f9c867275782d46c3a088977d6a5b18"
QT_DOC_SOURCE = "https://doc.qt.io/qt-6/"
QT_GFDL_SOURCE = "https://raw.githubusercontent.com/qt/qtbase/v6.11.2/LICENSES/GFDL-1.3-no-invariants-only.txt"
QT_ATTRIBUTION_MODULES = {
    "Core": "qt-core", "DBus": "qt-d-bus", "Gui": "qt-gui",
    "Network": "qt-network", "Pdf": "qt-pdf", "Qml": "qt-qml",
    "Quick": "qt-quick", "QuickControls2": "qt-quick-controls",
    "Sql": "qt-sql", "Svg": "qt-svg", "Test": "qt-test",
    "Positioning": "qt-positioning", "Multimedia": "qt-multimedia",
    "WebEngineCore": "qt-webengine", "WebEngineWidgets": "qt-webengine",
    "VirtualKeyboard": "qt-virtual-keyboard", "WaylandClient": "qt-wayland-compositor",
    "WlShellIntegration": "qt-wayland-compositor",
}
QT_NO_SEPARATE_ATTRIBUTIONS = {
    "Widgets", "OpenGL", "XcbQpa", "EglFSDeviceIntegration", "EglFsKmsSupport",
    "QmlMeta", "QmlModels", "QmlWorkerScript", "VirtualKeyboardQml",
    "PrintSupport", "Xml", "Concurrent", "OpenGLWidgets", "QuickWidgets",
}
EXTRA_TERMS = {
    "antlr4-python3-runtime": (
        "4.9.3", "https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt",
        "b1b379fcaf3219593a4c433feb1b35c780bed23fafaae440b1ae2771a9521e3a",
    ),
    "flatbuffers": (
        "25.12.19", "https://raw.githubusercontent.com/google/flatbuffers/v25.12.19/LICENSE",
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    ),
    "rapidocr": (
        "3.9.2", "https://raw.githubusercontent.com/RapidAI/RapidOCR/main/LICENSE",
        "3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333",
    ),
}
NOTICE_NAME = re.compile(r"(?:license|licence|copying|notice|authors|copyright)", re.IGNORECASE)
LICENSE_TEXT_NAME = re.compile(
    r"(?:licen[cs]e|copying|(?:^|/)(?:(?:a|l)?gpl|mit|apache|bsd|mpl)[-._/])",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distributions() -> dict[str, metadata.Distribution]:
    found: dict[str, metadata.Distribution] = {}
    pending = list(DIRECT)
    while pending:
        requested = pending.pop()
        key = _normalize(requested)
        if key in found:
            continue
        try:
            distribution = metadata.distribution(requested)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"release dependency is not installed: {requested}") from exc
        found[key] = distribution
        if key == "pyinstaller":
            continue  # The bootloader ships, not its build-time Python dependencies.
        for requirement in distribution.requires or ():
            # Some older wheels (notably omegaconf 2.0.6) publish invalid
            # comparison wildcards such as "PyYAML >=5.1.*". The wildcard
            # adds no meaning to a range bound; normalize it for traversal.
            normalized = re.sub(
                r"(?P<operator>>=|<=|>|<)\s*(?P<version>\d+(?:\.\d+)*)\.\*",
                r"\g<operator>\g<version>",
                requirement,
            )
            parsed = Requirement(normalized)
            if parsed.marker is None or parsed.marker.evaluate({"extra": ""}):
                pending.append(parsed.name)
    return found


def _notice_files(distribution: metadata.Distribution) -> list[tuple[Path, Path]]:
    candidates: list[tuple[Path, Path]] = []
    for relative in distribution.files or ():
        path = Path(str(relative))
        if "licenses" not in {part.lower() for part in path.parts} and not NOTICE_NAME.search(path.name):
            continue
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe wheel notice path: {path}")
        source = Path(distribution.locate_file(relative))
        if source.is_file():
            candidates.append((path, source))
    return candidates


def _download_checked(url: str, expected_hash: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise RuntimeError(f"license text checksum mismatch: {url}")
    return body


def _bundled_binaries(analysis_toc: Path) -> list[tuple[str, Path]]:
    """Read PyInstaller's analysis, including native dependencies it collected."""
    analysis = ast.literal_eval(analysis_toc.read_text(encoding="utf-8"))
    binaries: dict[str, Path] = {}

    def visit(value: object) -> None:
        if isinstance(value, (tuple, list)):
            if (len(value) == 3 and isinstance(value[0], str)
                    and isinstance(value[1], str)
                    and (value[2] == "BINARY" or value[2] == "EXTENSION")):
                binaries[value[0]] = Path(value[1]).resolve()
            else:
                for child in value:
                    visit(child)

    visit(analysis)
    if not binaries:
        raise RuntimeError("PyInstaller analysis has no bundled native binaries")
    return sorted(binaries.items())


def _python_license() -> tuple[bytes, str]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    for directory in (Path(sys.base_prefix), Path(sys.prefix)):
        for name in ("LICENSE.txt", "LICENSE"):
            candidate = directory / name
            if candidate.is_file():
                body = candidate.read_bytes()
                if b"PYTHON SOFTWARE FOUNDATION LICENSE" in body.upper():
                    return body, str(candidate)
    url = f"https://raw.githubusercontent.com/python/cpython/v{version}/LICENSE"
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    if b"PYTHON SOFTWARE FOUNDATION LICENSE" not in body.upper():
        raise RuntimeError(f"Python {version} license text is missing or unexpected: {url}")
    return body, url


def _dpkg_paths(source: Path) -> list[Path]:
    candidates = [source, source.resolve()]
    for candidate in tuple(candidates):
        if candidate.is_relative_to("/usr/lib"):
            candidates.append(Path("/lib") / candidate.relative_to("/usr/lib"))
    return list(dict.fromkeys(candidates))


def _debian_copyright(source: Path) -> tuple[str, str, Path]:
    for candidate in _dpkg_paths(source):
        result = subprocess.run(
            ["dpkg-query", "-S", str(candidate)], capture_output=True, text=True, check=False,
        )
        if result.returncode:
            continue
        for line in result.stdout.splitlines():
            if ": " not in line:
                continue
            package = line.split(": ", 1)[0].split(",", 1)[0].strip()
            name = package.split(":", 1)[0]
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
                continue
            notice = Path("/usr/share/doc") / name / "copyright"
            if not notice.is_file():
                continue
            version_result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", package],
                capture_output=True, text=True, check=False,
            )
            if version_result.returncode or not version_result.stdout.strip():
                continue
            return name, version_result.stdout.strip(), notice
    raise RuntimeError(f"no Debian copyright notice found for bundled system library: {source}")


def _is_debian_system_library(source: Path) -> bool:
    return sys.platform == "linux" and (source.is_relative_to("/usr/lib") or source.is_relative_to("/lib"))


def _debian_source_package(package: str) -> tuple[str, str]:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${source:Package}\t${source:Version}", package],
        capture_output=True, text=True, check=False,
    )
    fields = result.stdout.strip().split("\t")
    if (result.returncode or len(fields) != 2
            or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", fields[0])
            or not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", fields[1])):
        raise RuntimeError(f"source package could not be identified for bundled library: {package}")
    return fields[0], fields[1]


def _wheel_binary_owners(inventory: list[dict[str, object]], sources: set[Path]) -> dict[Path, str]:
    """Match bundled wheel binaries to the distributions in the notice index."""
    owners: dict[Path, str] = {}
    names = {source.name for source in sources}
    for item in inventory:
        name = str(item["name"])
        if name == "CPython" or name == "Qt third-party attributions":
            continue
        distribution = metadata.distribution(name)
        if distribution.files is None:
            raise RuntimeError(f"installed wheel has no file inventory: {name}")
        for relative in distribution.files:
            if Path(str(relative)).name not in names:
                continue
            source = Path(distribution.locate_file(relative)).resolve()
            if source in sources and source.is_file():
                owners[source] = name
    return owners


def _is_cpython_binary(source: Path, python_root: Path) -> bool:
    if not source.is_relative_to(python_root):
        return False
    if (sys.platform == "darwin" and source == python_root / "Python"
            and python_root.parent.name == "Versions"
            and python_root.parent.parent.name == "Python.framework"):
        return True
    name = source.name.lower()
    if re.fullmatch(r"(?:lib)?python\d+(?:\.\d+)*(?:m)?\.(?:so(?:\.\d+)*|dylib|dll)", name):
        return True
    relative = source.relative_to(python_root)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return ("lib-dynload" in relative.parts and version in relative.parts) or (
        sys.platform == "win32" and "dlls" in {part.lower() for part in relative.parts}
        and name.endswith(".pyd")
    )


def _is_mac_installer_ncurses(source: Path, python_root: Path) -> bool:
    return (sys.platform == "darwin"
            and source == python_root / "lib" / "libncurses.6.dylib"
            and python_root.parent.name == "Versions"
            and python_root.parent.parent.name == "Python.framework")


def _is_mac_installer_openssl(source: Path, python_root: Path) -> bool:
    return (sys.platform == "darwin"
            and source.parent == python_root / "lib"
            and source.name in {"libcrypto.3.dylib", "libssl.3.dylib"}
            and python_root.parent.name == "Versions"
            and python_root.parent.parent.name == "Python.framework")


def _mac_installer_license(component: str, installer_license: Path | None = None) -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    path = installer_license or Path("/Applications") / f"Python {version}" / "License.rtf"
    if not path.is_file() or component.encode("ascii") not in path.read_bytes():
        raise RuntimeError(f"macOS Python installer {component} notice is missing: {path}")
    return path


def _add_mac_ncurses_notice(
    destination: Path, bundled_names: list[str], installer_license: Path | None = None,
) -> dict[str, object]:
    installer_license = _mac_installer_license("NCurses 6.5", installer_license)
    source = _download_checked(NCURSES_SOURCE, NCURSES_SHA256)
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        member = archive.extractfile(f"ncurses-{NCURSES_VERSION}/COPYING")
        if member is None:
            raise RuntimeError("NCurses source archive has no COPYING notice")
        license_body = member.read()
    if not license_body.strip():
        raise RuntimeError("NCurses license text is empty")
    target = destination / f"mac-python-ncurses-{NCURSES_VERSION}"
    target.mkdir()
    shutil.copy2(installer_license, target / "Python-Installer-License.rtf")
    (target / "NCurses-COPYING.txt").write_bytes(license_body)
    return {
        "name": "macOS Python installer NCurses", "version": NCURSES_VERSION,
        "license": "See included NCurses-COPYING.txt",
        "directory": target.name,
        "files": ["Python-Installer-License.rtf", "NCurses-COPYING.txt"],
        "source": NCURSES_SOURCE, "sha256": NCURSES_SHA256,
        "binaries": bundled_names,
    }


def _add_mac_openssl_notice(
    destination: Path, bundled_names: list[str], installer_license: Path | None = None,
) -> dict[str, object]:
    installer_license = _mac_installer_license("OpenSSL 3.0.21", installer_license)
    source = _download_checked(OPENSSL_SOURCE, OPENSSL_SHA256)
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        member = archive.extractfile(f"openssl-{OPENSSL_VERSION}/LICENSE.txt")
        if member is None:
            raise RuntimeError("OpenSSL source archive has no LICENSE.txt")
        license_body = member.read()
    if not license_body.strip():
        raise RuntimeError("OpenSSL license text is empty")
    target = destination / f"mac-python-openssl-{OPENSSL_VERSION}"
    target.mkdir()
    shutil.copy2(installer_license, target / "Python-Installer-License.rtf")
    (target / "OpenSSL-LICENSE.txt").write_bytes(license_body)
    return {
        "name": "macOS Python installer OpenSSL", "version": OPENSSL_VERSION,
        "license": "Apache-2.0",
        "directory": target.name,
        "files": ["Python-Installer-License.rtf", "OpenSSL-LICENSE.txt"],
        "source": OPENSSL_SOURCE, "sha256": OPENSSL_SHA256,
        "binaries": bundled_names,
    }


def _windows_python_component(source: Path, python_root: Path) -> str | None:
    if sys.platform != "win32" or source.parent != python_root / "DLLs":
        return None
    return {
        "libcrypto-3.dll": "OpenSSL", "libssl-3.dll": "OpenSSL",
        "libffi-8.dll": "libffi", "sqlite3.dll": "SQLite",
        "zlib1.dll": "zlib",
    }.get(source.name.lower())


def _is_external_windows_runtime(source: Path, python_root: Path) -> bool:
    if sys.platform != "win32" or source.name.lower() not in EXTERNAL_WINDOWS_RUNTIME:
        return False
    if any(
        source.is_relative_to(root / "Lib" / "site-packages")
        for root in (python_root, Path(sys.prefix).resolve())
    ):
        return True
    if source.name.lower() == "msvcp140.dll":
        windows_root = os.environ.get("SystemRoot")
        return bool(windows_root and source.parent == Path(windows_root) / "System32")
    return source.parent == python_root


def _windows_python_notices(
    destination: Path, binaries: dict[str, list[str]],
) -> list[dict[str, object]]:
    specifications = {
        "OpenSSL": (OPENSSL_VERSION, OPENSSL_SOURCE, OPENSSL_SHA256,
                    f"openssl-{OPENSSL_VERSION}/LICENSE.txt", "Apache-2.0"),
        "libffi": ("3.4.4", LIBFFI_SOURCE, LIBFFI_SHA256,
                   "libffi-3.4.4/LICENSE", "MIT"),
        "SQLite": ("3.50.4", SQLITE_SOURCE, SQLITE_SHA256, None, "Public domain"),
        "zlib": (ZLIB_VERSION, ZLIB_SOURCE, ZLIB_SHA256,
                 f"zlib-{ZLIB_VERSION}/LICENSE", "Zlib"),
    }
    inventory = []
    for component, bundled_names in sorted(binaries.items()):
        version, url, expected_hash, license_member, license_name = specifications[component]
        target = destination / f"windows-python-{component.lower()}-{version}"
        target.mkdir()
        if license_member is None:
            filename = "PUBLIC-DOMAIN.txt"
            (target / filename).write_text(
                "SQLite deliverable code is dedicated to the public domain.\n"
                "Source: https://www.sqlite.org/copyright.html\n",
                encoding="utf-8",
            )
        else:
            filename = "LICENSE.txt"
            source_archive = _download_checked(url, expected_hash)
            with tarfile.open(fileobj=io.BytesIO(source_archive), mode="r:gz") as archive:
                member = archive.extractfile(license_member)
                if member is None:
                    raise RuntimeError(f"{component} source archive has no license text")
                body = member.read()
            if not body.strip():
                raise RuntimeError(f"{component} license text is empty")
            (target / filename).write_bytes(body)
        inventory.append({
            "name": f"Windows Python installer {component}", "version": version,
            "license": license_name, "directory": target.name, "files": [filename],
            "source": url, "sha256": expected_hash,
            "binaries": bundled_names,
        })
    return inventory


def _debian_common_licenses(
    copyright_file: Path, common_root: Path = Path("/usr/share/common-licenses"),
) -> list[Path]:
    text = copyright_file.read_text(encoding="utf-8", errors="replace")
    aliases = {"GPL-2.0": "GPL-2", "GPL-3.0": "GPL-3",
               "LGPL-3.0": "LGPL-3", "AGPL-3.0": "AGPL-3"}
    names = {
        aliases.get(name.rstrip(".,"), name.rstrip(".,"))
        for name in re.findall(r"/usr/share/common-licenses/([A-Za-z0-9+._-]+)", text)
    }
    result = [common_root / name for name in sorted(names)]
    missing = [str(path) for path in result if not path.is_file()]
    if missing:
        raise RuntimeError(f"referenced Debian license text is missing: {', '.join(missing)}")
    return result


def _qt_sections(binaries: list[tuple[str, Path]]) -> set[str]:
    sections: set[str] = set()
    modules: set[str] = set()
    for bundled_name, _ in binaries:
        normalized_name = bundled_name.replace("\\", "/")
        modules.update(re.findall(r"(?:lib)?Qt6([A-Z][A-Za-z0-9]+)", Path(normalized_name).name))
        modules.update(re.findall(r"/Qt([A-Z][A-Za-z0-9]+)\.framework/", normalized_name))
        if "PySide6/Qt/plugins/imageformats/" in normalized_name:
            sections.add("qt-image-formats")
    if not modules:
        raise RuntimeError("no Qt runtime libraries found in bundled native binaries")
    unknown = modules - QT_ATTRIBUTION_MODULES.keys() - QT_NO_SEPARATE_ATTRIBUTIONS
    if unknown:
        raise RuntimeError(f"Qt modules need attribution review: {', '.join(sorted(unknown))}")
    sections.update(QT_ATTRIBUTION_MODULES[module] for module in modules if module in QT_ATTRIBUTION_MODULES)
    return sections


def _fetch_qt_document(filename: str) -> bytes:
    if not re.fullmatch(r"[a-z0-9-]+\.html", filename):
        raise RuntimeError(f"unexpected Qt attribution path: {filename}")
    with urllib.request.urlopen(QT_DOC_SOURCE + filename, timeout=30) as response:
        body = response.read()
    if b"Qt 6.11.2" not in body or b"<h1" not in body:
        raise RuntimeError(f"Qt attribution page is not for {QT_VERSION}: {filename}")
    return body


def add_qt_attributions(destination: Path, analysis_toc: Path) -> None:
    """Bundle Qt's own component notices for the modules in the frozen app."""
    if metadata.distribution("PySide6").version != QT_VERSION:
        raise RuntimeError("PySide6 version changed; review Qt third-party attributions")
    sections = _qt_sections(_bundled_binaries(analysis_toc))
    index_body = _fetch_qt_document("licenses-used-in-qt.html")
    index_html = index_body.decode("utf-8")
    chunks = re.split(r'<h2 id="([^"]+)">', index_html)
    pages: set[str] = set()
    for position in range(1, len(chunks), 2):
        if chunks[position] not in sections:
            continue
        tables = re.findall(
            r'<div class="table"><table class="annotated">(.*?)</table>',
            chunks[position + 1], re.DOTALL,
        )
        pages.update(
            link for table in tables
            for link in re.findall(r'href="([a-z0-9-]+\.html)"', table)
        )
    if not pages or "qt-core" not in sections:
        raise RuntimeError("Qt attribution index did not identify the bundled modules")
    with urllib.request.urlopen(QT_GFDL_SOURCE, timeout=30) as response:
        gfdl = response.read()
    if b"GNU Free Documentation License" not in gfdl:
        raise RuntimeError("Qt documentation license text is missing")
    target = destination / "Qt-PySide6" / "attributions"
    target.mkdir(parents=True)
    (target / "licenses-used-in-qt.html").write_bytes(index_body)
    (target / "GFDL-1.3-no-invariants-only.txt").write_bytes(gfdl)
    with ThreadPoolExecutor(max_workers=8) as executor:
        documents = list(executor.map(_fetch_qt_document, sorted(pages)))
    hashes = {}
    for filename, body in zip(sorted(pages), documents):
        (target / filename).write_bytes(body)
        hashes[filename] = hashlib.sha256(body).hexdigest()
    (target / "manifest.json").write_text(json.dumps({
        "qt_version": QT_VERSION, "source": QT_DOC_SOURCE,
        "sections": sorted(sections), "sha256": hashes,
    }, indent=2) + "\n", encoding="utf-8")
    inventory_path = destination / "index.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory.append({
        "name": "Qt third-party attributions", "version": QT_VERSION,
        "license": "Component licenses in individual pages; documentation under GFDL 1.3",
        "directory": "Qt-PySide6/attributions",
        "files": ["GFDL-1.3-no-invariants-only.txt", "licenses-used-in-qt.html", "manifest.json", *sorted(pages)],
    })
    inventory_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (destination / "README.txt").open("a", encoding="utf-8") as readme:
        readme.write(
            f"Qt {QT_VERSION} component attribution pages are copied from {QT_DOC_SOURCE} "
            "for the Qt modules found in the frozen analysis. Their source URLs and "
            "SHA-256 digests are in Qt-PySide6/attributions/manifest.json.\n"
        )


def add_native_notices(
    destination: Path, analysis_toc: Path, external_binaries: frozenset[str] = frozenset(),
) -> None:
    """Add interpreter and native-system notices; refuse unidentified binaries."""
    inventory_path = destination / "index.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    python_body, python_source = _python_license()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_dir = destination / f"cpython-{python_version}"
    python_dir.mkdir()
    (python_dir / "LICENSE.txt").write_bytes(python_body)
    inventory.append({
        "name": "CPython", "version": python_version, "license": "PSF-2.0",
        "files": ["LICENSE.txt"], "source": python_source,
    })

    system_packages: dict[tuple[str, str], dict[str, object]] = {}
    mac_ncurses_binaries: list[str] = []
    mac_openssl_binaries: list[str] = []
    windows_python_binaries: dict[str, list[str]] = {}
    unknown: list[str] = []
    python_root = Path(sys.base_prefix).resolve()
    binaries = _bundled_binaries(analysis_toc)
    wheel_owners = _wheel_binary_owners(inventory, {source for _, source in binaries})
    for bundled_name, source in binaries:
        if not source.is_file():
            raise RuntimeError(f"bundled native binary source is missing: {source}")
        if bundled_name.lower() in external_binaries:
            if not _is_external_windows_runtime(source, python_root):
                raise RuntimeError(f"invalid external Windows runtime: {bundled_name} <- {source}")
            continue
        if "site-packages" in source.parts or "dist-packages" in source.parts:
            if source not in wheel_owners:
                raise RuntimeError(f"bundled wheel binary has no indexed notice: {bundled_name} <- {source}")
            continue
        if _is_debian_system_library(source):
            name, version, copyright_file = _debian_copyright(source)
            key = (name, version)
            if key not in system_packages:
                target = destination / "system-packages" / f"{name}-{version}"
                target.mkdir(parents=True)
                shutil.copy2(copyright_file, target / "copyright")
                files = ["copyright"]
                for common_license in _debian_common_licenses(copyright_file):
                    relative = f"common-licenses/{common_license.name}"
                    (target / "common-licenses").mkdir(exist_ok=True)
                    shutil.copy2(common_license, target / relative)
                    files.append(relative)
                source_package, source_version = _debian_source_package(name)
                system_packages[key] = {
                    "name": name, "version": version, "license": "See included copyright",
                    "source_package": source_package, "source_version": source_version,
                    "directory": f"system-packages/{name}-{version}",
                    "files": files,
                    "binaries": [],
                }
            system_packages[key]["binaries"].append(bundled_name)
        elif _is_cpython_binary(source, python_root):
            continue
        elif _is_mac_installer_ncurses(source, python_root):
            mac_ncurses_binaries.append(bundled_name)
        elif _is_mac_installer_openssl(source, python_root):
            mac_openssl_binaries.append(bundled_name)
        elif component := _windows_python_component(source, python_root):
            windows_python_binaries.setdefault(component, []).append(bundled_name)
        else:
            unknown.append(f"{bundled_name} <- {source}")
    if unknown:
        raise RuntimeError("unidentified bundled native binary sources:\n" + "\n".join(unknown))
    inventory.extend(system_packages[key] for key in sorted(system_packages))
    if mac_ncurses_binaries:
        inventory.append(_add_mac_ncurses_notice(destination, mac_ncurses_binaries))
    if mac_openssl_binaries:
        inventory.append(_add_mac_openssl_notice(destination, mac_openssl_binaries))
    if windows_python_binaries:
        inventory.extend(_windows_python_notices(destination, windows_python_binaries))
    inventory_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (destination / "README.txt").open("a", encoding="utf-8") as readme:
        readme.write(
            "CPython is listed separately. Bundled Debian shared libraries are mapped "
            "to their installed package copyright files. Windows Python installer "
            "libraries have separate component notices. An unknown native binary "
            "stops the build.\n"
        )


def verify_notice_inventory(destination: Path) -> None:
    """Require every indexed notice text to be present and nonempty."""
    inventory = json.loads((destination / "index.json").read_text(encoding="utf-8"))
    if not isinstance(inventory, list) or not inventory:
        raise RuntimeError(f"third-party notice inventory is empty: {destination}")
    for item in inventory:
        if not isinstance(item, dict) or not item.get("name") or not item.get("version"):
            raise RuntimeError(f"invalid third-party notice entry: {item!r}")
        directory = item.get("directory") or f"{_normalize(str(item['name']))}-{item['version']}"
        folder = Path(str(directory))
        files = item.get("files")
        if (folder.is_absolute() or ".." in folder.parts or not isinstance(files, list)
                or not files):
            raise RuntimeError(f"invalid third-party notice paths for {item['name']}")
        for name in files:
            relative = Path(str(name))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"unsafe third-party notice path: {name}")
            notice = destination / folder / relative
            if not notice.is_file() or notice.stat().st_size == 0:
                raise RuntimeError(f"indexed third-party notice is missing or empty: {notice}")


def collect(destination: Path) -> Path:
    """Return a complete notice directory, or fail the build."""
    destination.mkdir(parents=True, exist_ok=False)
    inventory: list[dict[str, object]] = []
    required = {"pdfium": False, "onnx": False, "playwright": False}
    for name, distribution in sorted(_distributions().items()):
        version = distribution.version
        target_root = destination / f"{name}-{version}"
        files = []
        for relative, source in _notice_files(distribution):
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            files.append(relative.as_posix())
            lower = relative.as_posix().lower()
            if name == "pypdfium2" and "build_licenses/pdfium.txt" in lower:
                required["pdfium"] = True
            if name == "onnxruntime" and lower.endswith("thirdpartynotices.txt"):
                required["onnx"] = True
            if name == "playwright" and lower.endswith("thirdpartynotices.txt"):
                required["playwright"] = True
        if name in EXTRA_TERMS:
            expected_version, url, expected_hash = EXTRA_TERMS[name]
            if version != expected_version:
                raise RuntimeError(f"{name} changed version; review its notices before release: {version}")
            target_root.mkdir(parents=True, exist_ok=True)
            (target_root / "UPSTREAM-LICENSE.txt").write_bytes(_download_checked(url, expected_hash))
            files.append("UPSTREAM-LICENSE.txt")
        inventory.append({
            "name": distribution.metadata.get("Name", name),
            "version": version,
            "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "See included notice files",
            "files": files,
        })
        if name == "pymupdf":
            target = target_root / "AGPL-3.0.txt"
            shutil.copy2(ROOT / "LICENSE", target)
            files.append(target.name)
    missing = [name for name, present in required.items() if not present]
    if missing:
        raise RuntimeError(f"required bundled third-party notices are missing: {', '.join(missing)}")

    qt_directory = destination / "Qt-PySide6"
    qt_directory.mkdir()
    for filename, expected_hash in QT_TERMS.items():
        url = QT_SOURCE + filename
        body = _download_checked(url, expected_hash)
        (qt_directory / filename).write_bytes(body)
    for item in inventory:
        if _normalize(str(item["name"])) in {"pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6"}:
            target = destination / f"{_normalize(str(item['name']))}-{item['version']}"
            target.mkdir(exist_ok=True)
            for filename in QT_TERMS:
                shutil.copy2(qt_directory / filename, target / filename)
                item["files"].append(filename)
    without_terms = [
        str(item["name"])
        for item in inventory
        if not any(LICENSE_TEXT_NAME.search(str(path)) for path in item["files"])
    ]
    if without_terms:
        raise RuntimeError(f"no license text found for release dependencies: {', '.join(without_terms)}")
    (qt_directory / "SOURCE.txt").write_text(
        "Qt for Python community packages are licensed under LGPLv3/GPLv3; "
        "the exact package metadata and version appear in index.json.\n"
        f"License texts: {QT_SOURCE}\n"
        "Qt licensing information: https://doc.qt.io/qtforpython-6/\n",
        encoding="utf-8",
    )
    (destination / "index.json").write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (destination / "README.txt").write_text(
        "Third-party notices for the package versions installed on this build runner.\n"
        "Each component retains its own license; the Callimachus project license "
        "does not replace those terms.\n"
        "See index.json for versions and corresponding notice files.\n"
        "Version-matched dependency source archives and their checksum manifest "
        "are published as separate assets in the same GitHub Release.\n"
        "This inventory is not a legal opinion.\n",
        encoding="utf-8",
    )
    return destination
