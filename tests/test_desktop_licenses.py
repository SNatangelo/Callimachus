# tests/test_desktop_licenses.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Release notices must be copied from the actual installed distributions."""
from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("collect_notices", ROOT / "packaging" / "collect_notices.py")
assert SPEC is not None and SPEC.loader is not None
NOTICES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NOTICES)


def test_optional_and_build_only_requirements_are_not_in_release_inventory(monkeypatch):
    packages = {
        "runtime": SimpleNamespace(requires=["needed>=1", "pytest>=8; extra == 'test'"]),
        "needed": SimpleNamespace(requires=[]),
        "pyinstaller": SimpleNamespace(requires=["setuptools>=42"]),
    }
    monkeypatch.setattr(NOTICES, "DIRECT", ("runtime", "pyinstaller"))
    monkeypatch.setattr(NOTICES.metadata, "distribution", lambda name: packages[name.lower()])
    assert set(NOTICES._distributions()) == set(packages) - {"pytest"}


@pytest.mark.parametrize("distribution,expected", [
    ("pypdfium2", "build_licenses/pdfium.txt"),
    ("onnxruntime", "thirdpartynotices.txt"),
    ("playwright", "thirdpartynotices.txt"),
])
def test_critical_wheels_have_real_notice_files(distribution, expected):
    installed = metadata.distribution(distribution)
    files = [relative.as_posix().lower() for relative, _ in NOTICES._notice_files(installed)]
    assert any(expected in file for file in files)


def test_collect_copies_notices_and_records_versions(monkeypatch, tmp_path):
    packages = {
        name: metadata.distribution(name)
        for name in ("pypdfium2", "onnxruntime", "playwright")
    }
    monkeypatch.setattr(NOTICES, "_distributions", lambda: packages)
    license_body = b"license text\n" * 200
    monkeypatch.setattr(NOTICES, "QT_TERMS", {"LGPL-3.0-only.txt": hashlib.sha256(license_body).hexdigest()})
    monkeypatch.setattr(NOTICES.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(license_body))
    destination = NOTICES.collect(tmp_path / "notices")
    inventory = json.loads((destination / "index.json").read_text(encoding="utf-8"))
    assert {item["name"].lower() for item in inventory} == set(packages)
    assert (destination / "Qt-PySide6" / "LGPL-3.0-only.txt").is_file()
    assert list(destination.rglob("*pdfium.txt"))
    assert list(destination.rglob("ThirdPartyNotices.txt"))


def test_missing_pdfium_notices_fails_closed(monkeypatch, tmp_path):
    packages = {"onnxruntime": metadata.distribution("onnxruntime"),
                "playwright": metadata.distribution("playwright")}
    monkeypatch.setattr(NOTICES, "_distributions", lambda: packages)
    with pytest.raises(RuntimeError, match="pdfium"):
        NOTICES.collect(tmp_path / "notices")


def test_authors_file_alone_does_not_satisfy_license_requirement(monkeypatch, tmp_path):
    fake = SimpleNamespace(version="1.0", metadata={"Name": "fake"})
    packages = {
        name: metadata.distribution(name)
        for name in ("pypdfium2", "onnxruntime", "playwright")
    }
    packages["fake"] = fake
    authors = tmp_path / "AUTHORS"
    authors.write_text("Contributors\n", encoding="utf-8")
    actual_notice_files = NOTICES._notice_files

    def notice_files(distribution):
        if distribution is fake:
            return [(Path("fake-1.0.dist-info/AUTHORS"), authors)]
        return actual_notice_files(distribution)

    monkeypatch.setattr(NOTICES, "_distributions", lambda: packages)
    monkeypatch.setattr(NOTICES, "_notice_files", notice_files)
    monkeypatch.setattr(NOTICES, "QT_TERMS", {})
    with pytest.raises(RuntimeError, match="no license text"):
        NOTICES.collect(tmp_path / "notices")


def test_native_notices_include_python_and_bundled_system_library(monkeypatch, tmp_path):
    source = tmp_path / "libexample.so"
    source.write_bytes(b"binary")
    copyright_file = tmp_path / "copyright"
    copyright_file.write_text("Example license\n", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr([[("libexample.so", str(source), "BINARY")]]), encoding="utf-8")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    monkeypatch.setattr(NOTICES, "_debian_copyright", lambda _source: ("example", "1.0", copyright_file))
    monkeypatch.setattr(NOTICES, "_debian_source_package", lambda _name: ("example-source", "1.0"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [("libexample.so", source)])
    monkeypatch.setattr(NOTICES.sys, "platform", "linux")
    monkeypatch.setattr(NOTICES, "_is_debian_system_library", lambda _source: True)

    NOTICES.add_native_notices(notices, analysis)

    inventory = json.loads((notices / "index.json").read_text(encoding="utf-8"))
    assert any(item["name"] == "CPython" and item["files"] == ["LICENSE.txt"] for item in inventory)
    assert any(item["name"] == "example" and item["binaries"] == ["libexample.so"] for item in inventory)
    assert any(item.get("source_package") == "example-source" for item in inventory)
    assert (notices / "system-packages" / "example-1.0" / "copyright").read_text() == "Example license\n"


def test_wheel_binary_requires_a_matching_indexed_distribution(monkeypatch, tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "native.so"
    source.write_bytes(b"binary")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [("native.so", source)])
    with pytest.raises(RuntimeError, match="bundled wheel binary has no indexed notice"):
        NOTICES.add_native_notices(notices, tmp_path / "Analysis-00.toc")


def test_wheel_binary_owner_uses_installed_file_inventory(monkeypatch, tmp_path):
    site = tmp_path / "site-packages"
    source = site / "example" / "native.so"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"binary")
    distribution = SimpleNamespace(
        files=[Path("example/native.so")], locate_file=lambda relative: site / relative,
    )
    monkeypatch.setattr(NOTICES.metadata, "distribution", lambda _name: distribution)
    assert NOTICES._wheel_binary_owners([{"name": "example", "version": "1"}], {source.resolve()}) == {
        source.resolve(): "example",
    }


def test_extensionless_playwright_node_is_attributed_to_wheel(monkeypatch, tmp_path):
    site = tmp_path / "site-packages"
    source = site / "playwright" / "driver" / "node"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"binary")
    distribution = SimpleNamespace(
        files=[Path("playwright/driver/node")], locate_file=lambda relative: site / relative,
    )
    monkeypatch.setattr(NOTICES.metadata, "distribution", lambda _name: distribution)
    assert NOTICES._wheel_binary_owners([{"name": "playwright", "version": "1"}], {source.resolve()}) == {
        source.resolve(): "playwright",
    }


def test_non_python_library_in_python_prefix_is_not_attributed_to_cpython(monkeypatch, tmp_path):
    python_root = tmp_path / "python"
    python_root.mkdir()
    source = python_root / "libssl.so"
    source.write_bytes(b"binary")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES.sys, "base_prefix", str(python_root))
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [("libssl.so", source)])
    with pytest.raises(RuntimeError, match="unidentified bundled native binary"):
        NOTICES.add_native_notices(notices, tmp_path / "Analysis-00.toc")


def test_versioned_macos_python_framework_binary_is_cpython(monkeypatch, tmp_path):
    python_root = tmp_path / "Python.framework" / "Versions" / "3.13"
    source = python_root / "Python"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"interpreter")
    monkeypatch.setattr(NOTICES.sys, "platform", "darwin")
    assert NOTICES._is_cpython_binary(source, python_root)
    other = python_root / "Other"
    other.write_bytes(b"different library")
    assert not NOTICES._is_cpython_binary(other, python_root)


def test_macos_installer_ncurses_gets_its_own_notice(monkeypatch, tmp_path):
    python_root = tmp_path / "Python.framework" / "Versions" / "3.13"
    source = python_root / "lib" / "libncurses.6.dylib"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native")
    monkeypatch.setattr(NOTICES.sys, "platform", "darwin")
    assert NOTICES._is_mac_installer_ncurses(source, python_root)
    assert not NOTICES._is_mac_installer_ncurses(source.with_name("other.dylib"), python_root)

    installer_license = tmp_path / "License.rtf"
    installer_license.write_bytes(b"Python installer includes NCurses 6.5")
    body = io.BytesIO()
    license_text = b"Copyright (c) NCurses authors. Permission to use.\n"
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        info = tarfile.TarInfo("ncurses-6.5/COPYING")
        info.size = len(license_text)
        archive.addfile(info, io.BytesIO(license_text))
    monkeypatch.setattr(NOTICES, "_download_checked", lambda _url, _sha: body.getvalue())
    result = NOTICES._add_mac_ncurses_notice(tmp_path, ["libncurses.6.dylib"], installer_license)
    assert result["binaries"] == ["libncurses.6.dylib"]
    assert (tmp_path / result["directory"] / "NCurses-COPYING.txt").read_bytes() == license_text


def test_macos_installer_openssl_gets_its_own_notice(monkeypatch, tmp_path):
    python_root = tmp_path / "Python.framework" / "Versions" / "3.13"
    source = python_root / "lib" / "libcrypto.3.dylib"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native")
    monkeypatch.setattr(NOTICES.sys, "platform", "darwin")
    assert NOTICES._is_mac_installer_openssl(source, python_root)

    installer_license = tmp_path / "License.rtf"
    installer_license.write_bytes(b"Python installer includes OpenSSL 3.0.21")
    body = io.BytesIO()
    license_text = b"Apache License Version 2.0\n"
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        info = tarfile.TarInfo("openssl-3.0.21/LICENSE.txt")
        info.size = len(license_text)
        archive.addfile(info, io.BytesIO(license_text))
    monkeypatch.setattr(NOTICES, "_download_checked", lambda _url, _sha: body.getvalue())
    result = NOTICES._add_mac_openssl_notice(tmp_path, ["libcrypto.3.dylib"], installer_license)
    assert result["binaries"] == ["libcrypto.3.dylib"]
    assert (tmp_path / result["directory"] / "OpenSSL-LICENSE.txt").read_bytes() == license_text


def test_windows_named_folder_does_not_exempt_a_bundled_dll(monkeypatch, tmp_path):
    source = tmp_path / "windows" / "custom.dll"
    source.parent.mkdir()
    source.write_bytes(b"binary")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES.sys, "platform", "win32")
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [("custom.dll", source)])
    with pytest.raises(RuntimeError, match="unidentified bundled native binary"):
        NOTICES.add_native_notices(notices, tmp_path / "Analysis-00.toc")


def test_external_windows_runtime_is_not_licensed_as_bundled_code(monkeypatch, tmp_path):
    python_root = tmp_path / "python"
    python_root.mkdir()
    runtime = python_root / "vcruntime140.dll"
    runtime.write_bytes(b"native")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES.sys, "platform", "win32")
    monkeypatch.setattr(NOTICES.sys, "base_prefix", str(python_root))
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [(runtime.name, runtime)])

    NOTICES.add_native_notices(
        notices, tmp_path / "Analysis-00.toc", frozenset({runtime.name}),
    )

    inventory = json.loads((notices / "index.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in inventory] == ["CPython"]


def test_external_pyside_runtime_copy_is_omitted_from_native_notices(monkeypatch, tmp_path):
    python_root = tmp_path / "python"
    wheel = python_root / "Lib" / "site-packages" / "PySide6"
    wheel.mkdir(parents=True)
    runtime = wheel / "msvcp140.dll"
    runtime.write_bytes(b"native")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES.sys, "platform", "win32")
    monkeypatch.setattr(NOTICES.sys, "base_prefix", str(python_root))
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))
    destination = "PySide6\\MSVCP140.dll"
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [(destination, runtime)])

    NOTICES.add_native_notices(
        notices, tmp_path / "Analysis-00.toc", frozenset({destination.lower()}),
    )

    inventory = json.loads((notices / "index.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in inventory] == ["CPython"]


def test_windows_python_installer_libraries_get_separate_notices(monkeypatch, tmp_path):
    python_root = tmp_path / "Python313"
    dlls = python_root / "DLLs"
    dlls.mkdir(parents=True)
    monkeypatch.setattr(NOTICES.sys, "platform", "win32")
    assert NOTICES._windows_python_component(dlls / "libcrypto-3.dll", python_root) == "OpenSSL"
    assert NOTICES._windows_python_component(dlls / "libssl-3.dll", python_root) == "OpenSSL"
    assert NOTICES._windows_python_component(dlls / "libffi-8.dll", python_root) == "libffi"
    assert NOTICES._windows_python_component(dlls / "sqlite3.dll", python_root) == "SQLite"
    assert NOTICES._windows_python_component(dlls / "zlib1.dll", python_root) == "zlib"
    assert NOTICES._windows_python_component(dlls / "custom.dll", python_root) is None
    assert NOTICES._windows_python_component(tmp_path / "other" / "sqlite3.dll", python_root) is None

    def archive_for(url, _sha):
        if "openssl" in url:
            name = "openssl-3.0.21/LICENSE.txt"
            content = b"Apache License Version 2.0\n"
        elif "zlib" in url:
            name = "zlib-1.3.1/LICENSE"
            content = b"Copyright (C) 1995-2024 Jean-loup Gailly and Mark Adler\n"
        else:
            name = "libffi-3.4.4/LICENSE"
            content = b"Copyright libffi contributors\n"
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w:gz") as archive:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return body.getvalue()

    monkeypatch.setattr(NOTICES, "_download_checked", archive_for)
    inventory = NOTICES._windows_python_notices(tmp_path, {
        "OpenSSL": ["libcrypto-3.dll", "libssl-3.dll"],
        "libffi": ["libffi-8.dll"],
        "SQLite": ["sqlite3.dll"],
        "zlib": ["zlib1.dll"],
    })
    assert len(inventory) == 4
    assert {item["name"] for item in inventory} == {
        "Windows Python installer OpenSSL", "Windows Python installer libffi",
        "Windows Python installer SQLite",
        "Windows Python installer zlib",
    }
    assert all(item["sha256"] for item in inventory)
    assert (tmp_path / "windows-python-openssl-3.0.21" / "LICENSE.txt").read_bytes().startswith(b"Apache")
    assert (tmp_path / "windows-python-libffi-3.4.4" / "LICENSE.txt").read_bytes().startswith(b"Copyright")
    assert "sqlite.org/copyright.html" in (
        tmp_path / "windows-python-sqlite-3.50.4" / "PUBLIC-DOMAIN.txt"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "windows-python-zlib-1.3.1" / "LICENSE.txt").is_file()


def test_debian_copyright_reference_includes_full_common_license(tmp_path):
    copyright_file = tmp_path / "copyright"
    copyright_file.write_text(
        "The full text is in /usr/share/common-licenses/GPL-3.\n", encoding="utf-8",
    )
    common_root = tmp_path / "common-licenses"
    common_root.mkdir()
    license_file = common_root / "GPL-3"
    license_file.write_text("GNU GPL version 3", encoding="utf-8")
    assert NOTICES._debian_common_licenses(copyright_file, common_root) == [license_file]


def test_debian_common_license_version_aliases_use_canonical_files(tmp_path):
    copyright_file = tmp_path / "copyright"
    copyright_file.write_text(
        "See /usr/share/common-licenses/GPL-2.0 and /usr/share/common-licenses/GPL-3.0.\n",
        encoding="utf-8",
    )
    common_root = tmp_path / "common-licenses"
    common_root.mkdir()
    (common_root / "GPL-2").write_text("GPL version 2", encoding="utf-8")
    (common_root / "GPL-3").write_text("GPL version 3", encoding="utf-8")
    assert NOTICES._debian_common_licenses(copyright_file, common_root) == [
        common_root / "GPL-2", common_root / "GPL-3",
    ]


def test_ubuntu_usrmerge_library_tries_legacy_dpkg_path():
    source = Path("/usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4")
    assert Path("/lib/x86_64-linux-gnu/libbz2.so.1.0.4") in NOTICES._dpkg_paths(source)


def test_unidentified_native_binary_fails_closed(monkeypatch, tmp_path):
    source = tmp_path / "libunknown.so"
    source.write_bytes(b"binary")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr([[("libunknown.so", str(source), "BINARY")]]), encoding="utf-8")
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES, "_python_license", lambda: (b"PYTHON SOFTWARE FOUNDATION LICENSE", "test"))

    with pytest.raises(RuntimeError, match="unidentified bundled native binary"):
        NOTICES.add_native_notices(notices, analysis)


def test_pyinstaller_analysis_binary_inventory_is_not_empty(tmp_path):
    source = tmp_path / "native.so"
    source.write_bytes(b"binary")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(
        repr((["run.py"], [[("native.so", str(source), "BINARY")]], [])),
        encoding="utf-8",
    )
    assert NOTICES._bundled_binaries(analysis) == [("native.so", source.resolve())]


def test_qt_attributions_include_component_license_pages(monkeypatch, tmp_path):
    notices = tmp_path / "notices"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(NOTICES.metadata, "distribution", lambda _name: SimpleNamespace(version="6.11.2"))
    monkeypatch.setattr(NOTICES, "_bundled_binaries", lambda _toc: [
        ("PySide6/Qt/lib/libQt6Core.so.6", tmp_path / "libQt6Core.so.6"),
    ])
    index = (b'<h1>Qt 6.11.2</h1><h2 id="qt-core">Qt Core</h2>'
             b'<div class="table"><table class="annotated">'
             b'<a href="qtcore-attribution-zlib.html">zlib</a></table>')
    documents = {
        "licenses-used-in-qt.html": index,
        "qtcore-attribution-zlib.html": b"<h1>Qt 6.11.2 zlib license text</h1>",
    }
    monkeypatch.setattr(NOTICES, "_fetch_qt_document", documents.__getitem__)
    monkeypatch.setattr(NOTICES.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"GNU Free Documentation License"))

    NOTICES.add_qt_attributions(notices, analysis)

    inventory = json.loads((notices / "index.json").read_text(encoding="utf-8"))
    assert inventory[-1]["name"] == "Qt third-party attributions"
    assert "qtcore-attribution-zlib.html" in inventory[-1]["files"]
    assert (notices / "Qt-PySide6" / "attributions" / "qtcore-attribution-zlib.html").is_file()


def test_unreviewed_qt_module_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="Qt modules need attribution review"):
        NOTICES._qt_sections([("libQt6Unknown.so.6", tmp_path / "unknown")])


def test_macos_qt_framework_names_select_component_notices(tmp_path):
    sections = NOTICES._qt_sections([
        ("PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore", tmp_path / "QtCore"),
        ("PySide6/Qt/lib/QtGui.framework/Versions/A/QtGui", tmp_path / "QtGui"),
    ])
    assert sections == {"qt-core", "qt-gui"}


def test_indexed_notice_must_exist_and_be_nonempty(tmp_path):
    notices = tmp_path / "notices"
    component = notices / "component-1.0"
    component.mkdir(parents=True)
    (notices / "index.json").write_text(
        json.dumps([{"name": "component", "version": "1.0", "files": ["LICENSE.txt"]}]),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="missing or empty"):
        NOTICES.verify_notice_inventory(notices)
    license_file = component / "LICENSE.txt"
    license_file.write_text("Real notice text", encoding="utf-8")
    NOTICES.verify_notice_inventory(notices)
