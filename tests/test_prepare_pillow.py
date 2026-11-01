# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline tests for the patched Pillow wheel preparation helper."""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "prepare_pillow.py"


def _load():
    spec = importlib.util.spec_from_file_location("prepare_pillow_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path


def _metadata():
    return b"Metadata-Version: 2.4\nName: Pillow\nVersion: 13.0.0\n"


def test_unpack_wheel_accepts_expected_pillow_metadata(tmp_path):
    helper = _load()
    wheel = _wheel(tmp_path / "pillow.whl", [
        ("PIL/__init__.py", b""),
        ("Pillow-13.0.0.dist-info/METADATA", _metadata()),
        ("Pillow-13.0.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
    ])
    destination = tmp_path / "unpacked"
    destination.mkdir()

    info = helper.unpack_wheel(wheel, destination)

    assert info == destination / "Pillow-13.0.0.dist-info"
    assert (destination / "PIL" / "__init__.py").is_file()


class _ZipMember:
    def __init__(self, filename, file_size=0, external_attr=0):
        self.filename = filename
        self.file_size = file_size
        self.external_attr = external_attr

    def is_dir(self):
        return self.filename.endswith("/")


class _FakeZip:
    def __init__(self, members):
        self.members = members
        self.extracted = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def infolist(self):
        return self.members

    def extractall(self, _destination):
        self.extracted = True


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", r"\\\\server\\share"])
def test_unpack_wheel_rejects_unsafe_member_paths_before_writing(tmp_path, monkeypatch, name):
    helper = _load()
    archive = _FakeZip([_ZipMember(name)])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


def test_unpack_wheel_rejects_symlink_members_before_writing(tmp_path, monkeypatch):
    helper = _load()
    archive = _FakeZip([_ZipMember("PIL/escape", external_attr=0o120777 << 16)])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


def test_unpack_wheel_rejects_duplicate_member_names_before_writing(tmp_path, monkeypatch):
    helper = _load()
    archive = _FakeZip([_ZipMember("PIL/file"), _ZipMember("PIL/file")])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


def test_unpack_wheel_rejects_normalized_duplicate_paths_before_writing(tmp_path, monkeypatch):
    helper = _load()
    archive = _FakeZip([_ZipMember("PIL/file"), _ZipMember("PIL//file")])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


@pytest.mark.parametrize(
    "names",
    [
        ("PIL/file", "PIL//file"),
        ("PIL/./file", "PIL/file"),
        ("PIL", "PIL/file"),
        ("PIL/", "PIL"),
    ],
)
def test_unpack_wheel_rejects_normalized_or_file_directory_collisions(
    tmp_path, monkeypatch, names,
):
    helper = _load()
    archive = _FakeZip([_ZipMember(name) for name in names])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member|wheel file/directory collision"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


@pytest.mark.parametrize("sizes", [(100 * 1024**2 + 1,), (80 * 1024**2, 80 * 1024**2)])
def test_unpack_wheel_rejects_member_and_total_expansion_bounds(tmp_path, monkeypatch, sizes):
    helper = _load()
    archive = _FakeZip([
        _ZipMember(f"PIL/member-{index}", file_size=size)
        for index, size in enumerate(sizes)
    ])
    monkeypatch.setattr(helper.zipfile, "ZipFile", lambda _path: archive)

    with pytest.raises(RuntimeError, match="unsafe wheel archive member"):
        helper.unpack_wheel(tmp_path / "unused.whl", tmp_path / "out")

    assert not archive.extracted


def _add_tar_file(tar, name, content):
    item = tarfile.TarInfo(name)
    item.size = len(content)
    tar.addfile(item, io.BytesIO(content))


def _version_header():
    return (
        b"#define BROTLI_VERSION_MAJOR 1\n"
        b"#define BROTLI_VERSION_MINOR 2\n"
        b"#define BROTLI_VERSION_PATCH 1\n"
    )


def test_extract_brotli_accepts_one_pinned_source_root(tmp_path):
    helper = _load()
    archive = tmp_path / "brotli.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_tar_file(tar, "brotli-1.2.1/c/common/version.h", _version_header())

    work = tmp_path / "work"
    work.mkdir()
    source = helper.extract_brotli(archive, work)

    assert source.name == "brotli-1.2.1"
    assert (source / "c/common/version.h").is_file()


@pytest.mark.parametrize("kind", ["traversal", "noncanonical", "escaping-symlink", "internal-symlink"])
def test_extract_brotli_rejects_escaping_tar_members(tmp_path, kind):
    helper = _load()
    archive = tmp_path / "brotli.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_tar_file(tar, "brotli-1.2.1/c/common/version.h", _version_header())
        if kind == "traversal":
            _add_tar_file(tar, "brotli-1.2.1/../../escape.txt", b"escape")
        elif kind == "noncanonical":
            _add_tar_file(tar, "brotli-1.2.1/c/common/./version.h", _version_header())
        else:
            link = tarfile.TarInfo("brotli-1.2.1/escape-link")
            link.type = tarfile.SYMTYPE
            link.linkname = (
                "c/common/version.h" if kind == "internal-symlink"
                else "../../../escape.txt"
            )
            tar.addfile(link)

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises((RuntimeError, tarfile.TarError, OSError, ValueError)):
        helper.extract_brotli(archive, work)

    assert not (work / "escape.txt").exists()


def test_extract_brotli_rejects_duplicate_tar_members(tmp_path):
    helper = _load()
    archive = tmp_path / "brotli.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        _add_tar_file(tar, "brotli-1.2.1/c/common/version.h", _version_header())
        _add_tar_file(tar, "brotli-1.2.1/c/common/version.h", _version_header())

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match="unsafe|duplicate"):
        helper.extract_brotli(archive, work)


def test_extract_brotli_checks_unpacked_size_before_extraction(tmp_path, monkeypatch):
    helper = _load()
    member = tarfile.TarInfo("brotli-1.2.1/payload")
    member.size = 150 * 1024**2 + 1

    class FakeTar:
        extracted = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([member])

        def extractall(self, *_args, **_kwargs):
            self.extracted = True

    archive = FakeTar()
    monkeypatch.setattr(helper.tarfile, "open", lambda _path: archive)

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match="oversized Brotli source"):
        helper.extract_brotli(tmp_path / "unused.tar.gz", work)

    assert not archive.extracted


def test_extract_brotli_rejects_too_many_members_before_extraction(tmp_path, monkeypatch):
    helper = _load()
    members = [
        tarfile.TarInfo(f"brotli-1.2.1/member-{index}")
        for index in range(10001)
    ]

    class FakeTar:
        extracted = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(members)

        def extractall(self, *_args, **_kwargs):
            self.extracted = True

    archive = FakeTar()
    monkeypatch.setattr(helper.tarfile, "open", lambda _path: archive)

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match="member"):
        helper.extract_brotli(tmp_path / "unused.tar.gz", work)

    assert not archive.extracted


@pytest.mark.parametrize(
    ("system", "machine", "tag"),
    [
        ("Linux", "x86_64", "linux_x86_64"),
        ("Darwin", "x86_64", "macosx_14_0_x86_64"),
        ("Darwin", "arm64", "macosx_14_0_arm64"),
    ],
)
def test_prepare_selects_the_pinned_platform_wheel_without_native_compilation(
    tmp_path, monkeypatch, system, machine, tag,
):
    helper = _load()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(helper.platform, "system", lambda: system)
    monkeypatch.setattr(helper.platform, "machine", lambda: machine)
    monkeypatch.setattr(helper, "sys", SimpleNamespace(version_info=(3, 13, 0)))
    if system == "Linux":
        monkeypatch.setattr(helper.platform, "libc_ver", lambda: ("glibc", "2.35"))
    calls = []
    source_archive = work / "brotli.tar.gz"
    original_wheel = work / "pillow.whl"

    def fake_download(row, _work):
        calls.append(row)
        return source_archive if row["filename"].startswith("brotli-") else original_wheel

    monkeypatch.setattr(helper, "download", fake_download)
    monkeypatch.setattr(helper, "extract_brotli", lambda _archive, _tmp: tmp_path / "source")
    monkeypatch.setattr(helper, "unpack_wheel", lambda _wheel, tree: tree / "Pillow-13.0.0.dist-info")
    monkeypatch.setattr(helper, "replace_libraries", lambda *_args: [])
    monkeypatch.setattr(helper, "write_provenance", lambda *_args, **_kwargs: None)
    packed = work / "qualified.whl"

    def fake_pack(_tree, _info, _destination, actual_tag):
        assert actual_tag == tag
        packed.write_bytes(b"wheel fixture")
        return packed

    monkeypatch.setattr(helper, "pack_wheel", fake_pack)
    expected = json.loads(helper.WHEEL_INPUTS.read_text(encoding="utf-8"))[system + "-" + machine]

    result = helper.prepare(work)

    assert result == packed
    assert calls[0]["filename"] == "brotli-1.2.1.tar.gz"
    assert calls[1] == expected
    assert calls[1]["sha256"] and calls[1]["size_bytes"] > 0


@pytest.mark.parametrize(
    ("system", "machine", "common_name", "decoder_name"),
    [
        ("Linux", "x86_64", "libbrotlicommon-callimachus.so.1.2.1",
         "libbrotlidec-callimachus.so.1.2.1"),
        ("Darwin", "arm64", "libbrotlicommon.1.2.1.dylib",
         "libbrotlidec.1.2.1.dylib"),
    ],
)
def test_compile_brotli_uses_only_the_reviewed_common_and_decoder_source_lists(
    tmp_path, monkeypatch, system, machine, common_name, decoder_name,
):
    helper = _load()
    source = tmp_path / "brotli"
    common_dir, decoder_dir = source / "c" / "common", source / "c" / "dec"
    common_dir.mkdir(parents=True)
    decoder_dir.mkdir(parents=True)
    common_sources = [common_dir / f"common-{index}.c" for index in range(6)]
    decoder_sources = [decoder_dir / f"decoder-{index}.c" for index in range(6)]
    for path in common_sources + decoder_sources:
        path.write_text("/* source fixture */\n", encoding="ascii")
    output = tmp_path / "native"
    output.mkdir()
    calls = []

    class _Version:
        restype = None

        def __call__(self):
            return 0x1002001

    monkeypatch.setattr(helper.shutil, "which", lambda _name: "cc")
    monkeypatch.setattr(helper, "run", lambda args, **_kwargs: calls.append([str(arg) for arg in args]))
    monkeypatch.setattr(helper.ctypes, "CDLL", lambda _path: SimpleNamespace(BrotliDecoderVersion=_Version()))

    common, decoder = helper.compile_brotli(source, output, system, machine)

    compiler_calls = [call for call in calls if "-o" in call]
    assert len(compiler_calls) == 2
    common_call, decoder_call = compiler_calls
    common_output = Path(common_call[common_call.index("-o") + 1])
    decoder_output = Path(decoder_call[decoder_call.index("-o") + 1])
    assert common_output.name == common_name and decoder_output.name == decoder_name
    assert {Path(value) for value in common_call[common_call.index("-o") + 2:]} == set(common_sources)
    decoder_inputs = decoder_call[decoder_call.index("-o") + 2:]
    assert set(map(Path, decoder_inputs[:-2])) == set(decoder_sources)
    assert Path(decoder_inputs[-2]) == common
    assert decoder_inputs[-1] == "-lm"
    assert common == common_output and decoder == decoder_output
    assert all("-mmacosx-version-min=14.4" in call for call in compiler_calls) == (system == "Darwin")


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_replace_libraries_uses_pinned_names_and_rewrites_native_load_commands(
    tmp_path, monkeypatch, system,
):
    helper = _load()
    tree = tmp_path / "wheel"
    if system == "Linux":
        library_dir = tree / "pillow.libs"
        old_common = library_dir / "libbrotlicommon-abcd.so.1.2.0"
        old_decoder = library_dir / "libbrotlidec-efgh.so.1.2.0"
        extension = tree / "PIL" / "_imaging.so"
        expected_common = "libbrotlicommon-callimachus.so.1.2.1"
        expected_decoder = "libbrotlidec-callimachus.so.1.2.1"
        old_dependency = old_decoder.name
        new_dependency = expected_decoder
    else:
        library_dir = tree / "PIL" / ".dylibs"
        old_common = library_dir / "libbrotlicommon.1.2.0.dylib"
        old_decoder = library_dir / "libbrotlidec.1.2.0.dylib"
        extension = tree / "PIL" / "_imaging.so"
        expected_common = "libbrotlicommon.1.2.1.dylib"
        expected_decoder = "libbrotlidec.1.2.1.dylib"
        old_dependency = "@rpath/" + old_decoder.name
        new_dependency = "@loader_path/.dylibs/" + expected_decoder
    for path in (old_common, old_decoder, extension):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"native fixture")
    dependency_map = {extension: [old_dependency]}
    calls = []

    def fake_compile(_source, directory, compile_system, _machine):
        assert compile_system == system
        common = directory / expected_common
        decoder = directory / expected_decoder
        common.write_bytes(b"new common")
        decoder.write_bytes(b"new decoder")
        dependency_map[common] = []
        dependency_map[decoder] = [common.name if system == "Linux" else "@loader_path/" + common.name]
        return common, decoder

    def fake_run(arguments, **kwargs):
        args = [str(item) for item in arguments]
        calls.append(args)
        if system == "Linux":
            if args[:2] == ["patchelf", "--replace-needed"]:
                old, new, binary = args[2], args[3], Path(args[4])
                dependency_map[binary] = [new if item == old else item for item in dependency_map.get(binary, [])]
                return SimpleNamespace(stdout="", returncode=0)
            if args[:2] == ["patchelf", "--print-needed"]:
                return SimpleNamespace(stdout="\n".join(dependency_map.get(Path(args[2]), [])), returncode=0)
        else:
            if args[:2] == ["install_name_tool", "-change"]:
                old, new, binary = args[2], args[3], Path(args[4])
                dependency_map[binary] = [new if item == old else item for item in dependency_map.get(binary, [])]
                return SimpleNamespace(stdout="", returncode=0)
            if args[:2] == ["codesign", "--force"]:
                return SimpleNamespace(stdout="", returncode=0)
            if args[:2] == ["otool", "-L"]:
                binary = Path(args[2])
                lines = [str(binary) + ":"]
                lines.extend(f"\t{item} (compatibility version 1.0.0, current version 1.2.0)"
                             for item in dependency_map.get(binary, []))
                return SimpleNamespace(stdout="\n".join(lines), returncode=0)
        raise AssertionError(args)

    monkeypatch.setattr(helper, "compile_brotli", fake_compile)
    monkeypatch.setattr(helper, "run", fake_run)

    changed = helper.replace_libraries(tree, tmp_path / "source", system, "x86_64")

    assert {path.name for path in changed} >= {expected_common, expected_decoder, extension.name}
    rewrites = [args for args in calls if args[:2] in (["patchelf", "--replace-needed"], ["install_name_tool", "-change"])]
    assert any(old_dependency in args and new_dependency in args for args in rewrites)
    assert not old_common.exists() and not old_decoder.exists()
    assert (library_dir / expected_common).is_file()
    assert (library_dir / expected_decoder).is_file()


def test_write_provenance_and_pack_wheel_regenerate_record_hashes(tmp_path, monkeypatch):
    helper = _load()
    recipe = json.loads(helper.RECIPE.read_text(encoding="utf-8"))
    tree = tmp_path / "wheel"
    info = tree / "Pillow-13.0.0.dist-info"
    licenses = info / "licenses"
    licenses.mkdir(parents=True)
    (info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: false\nTag: cp313-cp313-any\n",
        encoding="utf-8",
    )
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: Pillow\nVersion: 13.0.0\n",
        encoding="utf-8",
    )
    (info / "LICENSE").write_text("===== brotli-1.2.0 =====\n", encoding="utf-8")
    source = tmp_path / "brotli-source"
    source.mkdir()
    (source / "LICENSE").write_text("Brotli upstream license text\n", encoding="utf-8")
    binary = tree / "pillow.libs" / "libbrotlidec-callimachus.so.1.2.1"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"repaired Brotli library")
    common = tree / "pillow.libs" / "libbrotlicommon-callimachus.so.1.2.1"
    common.write_bytes(b"repaired Brotli common")

    helper.write_provenance(tree, info, source, recipe, [common, binary])

    receipt_path = info / helper.RECEIPT
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["format"] == "callimachus.pillow-native-security.v1"
    assert receipt["owner"] == {"name": "Pillow", "version": "13.0.0"}
    assert receipt["component"]["version"] == "1.2.1"
    assert receipt["component"]["source"]["sha256"] == next(
        row["sha256"] for row in recipe["source_overrides"] if row["name"] == "Brotli"
    )
    assert receipt["component"]["upstream_pin"]["patched_sha256"] == recipe["pillow_brotli_pin"]["patched_sha256"]
    assert all(not Path(row["path"]).is_absolute() for row in receipt["binary_files"])
    assert "brotli-1.2.1" in (info / "LICENSE").read_text(encoding="utf-8").lower()
    assert "MIT License" in (info / "licenses" / "Brotli-1.2.1.txt").read_text(encoding="utf-8")

    wheel = helper.pack_wheel(tree, info, tmp_path, "linux_x86_64")

    assert wheel.name == "pillow-13.0.0-1callimachus-cp313-cp313-linux_x86_64.whl"
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert receipt_path.relative_to(tree).as_posix() in members
        recorded = list(csv.reader(io.StringIO(archive.read(
            "Pillow-13.0.0.dist-info/RECORD"
        ).decode("utf-8"))))
        record_rows = {row[0]: row[1:] for row in recorded}
        assert set(record_rows) == members
        assert record_rows["Pillow-13.0.0.dist-info/RECORD"] == ["", ""]
        for name in members - {"Pillow-13.0.0.dist-info/RECORD"}:
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(archive.read(name)).digest()
            ).rstrip(b"=").decode("ascii")
            assert record_rows[name] == [
                "sha256=" + digest, str(len(archive.read(name))),
            ]
        wheel_metadata = archive.read("Pillow-13.0.0.dist-info/WHEEL").decode("utf-8")
        assert "Build: 1callimachus" in wheel_metadata
        assert "Tag: cp313-cp313-linux_x86_64" in wheel_metadata

    audit = helper.load_sibling("dependency_audit")
    observed = []
    validate_vendor = audit._pillow_brotli_vendor

    def validate_static_distribution(root, inventory, *, distribution=None):
        assert distribution is not None
        observed.append(distribution)
        return validate_vendor(root, inventory, distribution=distribution)

    monkeypatch.setattr(audit, "_pillow_brotli_vendor", validate_static_distribution)
    monkeypatch.setattr(
        audit.metadata, "distribution",
        lambda _name: pytest.fail("static wheel validation must not inspect installed packages"),
    )

    monkeypatch.setattr(helper, "load_sibling", lambda name: audit if name == "dependency_audit" else None)
    helper.validate_qualified_wheel(wheel, tmp_path)
    assert len(observed) == 1



def test_write_provenance_uses_native_google_source_for_source_build(tmp_path):
    helper = _load()
    recipe = json.loads(helper.RECIPE.read_text(encoding="utf-8"))
    tree = tmp_path / "source-wheel"
    info = tree / "Pillow-13.0.0.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "WHEEL").write_text("Wheel-Version: 1.0\n", encoding="utf-8")
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: Pillow\nVersion: 13.0.0\n",
        encoding="utf-8",
    )
    (info / "LICENSE").write_text("brotli-1.2.0\n", encoding="utf-8")
    source = tmp_path / "google-brotli-1.2.1"
    source.mkdir()
    (source / "LICENSE").write_text("Brotli 1.2.1\nMIT License\n", encoding="utf-8")
    binaries = []
    for index in range(8):
        binary = tree / "PIL" / f"_native{index}.pyd"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(f"native extension {index}".encode())
        binaries.append(binary)

    helper.write_provenance(tree, info, source, recipe, binaries, method="source-build")

    receipt = json.loads((info / helper.RECEIPT).read_text(encoding="utf-8"))
    native = recipe["pillow_brotli_native_source"]
    assert receipt["method"] == "source-build"
    assert receipt["component"]["source"] == {
        key: native[key] for key in ("filename", "url", "sha256", "size_bytes")
    }
    assert receipt["license"]["path"] == "Pillow-13.0.0.dist-info/licenses/Brotli-1.2.1.txt"
    assert [row["path"] for row in receipt["binary_files"]] == [
        path.relative_to(tree).as_posix() for path in binaries
    ]
    with pytest.raises(ValueError, match="unsupported Pillow Brotli provenance method"):
        helper.write_provenance(tree, info, source, recipe, binaries, method="arbitrary")

def test_main_qualifies_wheel_before_installing_it(tmp_path, monkeypatch):
    helper = _load()
    work = tmp_path / "work"
    work.mkdir()
    wheel = tmp_path / "pillow-qualified.whl"
    wheel.write_bytes(b"wheel fixture")
    events = []

    def validate(path, target_work):
        assert path == wheel.resolve()
        assert target_work == work
        events.append("validate")

    def install(path):
        assert path == wheel.resolve()
        events.append("install")

    monkeypatch.setattr(helper, "validate_qualified_wheel", validate)
    monkeypatch.setattr(helper, "install", install)

    assert helper.main(["--work-dir", str(work), "--wheel", str(wheel), "--install"]) == 0
    assert events == ["validate", "install"]
