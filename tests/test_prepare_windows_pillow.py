# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline tests for the pinned Windows Pillow source-build helper."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "prepare_windows_pillow.py"


def _load():
    spec = importlib.util.spec_from_file_location("prepare_windows_pillow_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACTUAL_RUNNER_PERL_BANNER = (
    "This is perl 5, version 32, subversion 1 "
    "(v5.32.1) built for MSWin32-x64-multi-thread"
)


def _mock_host_tools(helper, monkeypatch, perl_banner):
    outputs = {
        "ninja": "1.13.2\n",
        "perl": perl_banner,
        "git": "git version 2.56.0.windows.1\n",
    }
    calls = []
    monkeypatch.setattr(
        helper.shutil, "which", lambda name: str(Path('host-tools') / f'{name}.exe'),
    )

    def fake_output(args, **_kwargs):
        name = Path(args[0]).stem.lower()
        calls.append(name)
        return outputs[name]

    monkeypatch.setattr(helper, "_run_output", fake_output)
    return calls, outputs


def test_verify_host_tools_accepts_captured_runner_perl_banner(monkeypatch):
    helper = _load()
    calls, outputs = _mock_host_tools(helper, monkeypatch, ACTUAL_RUNNER_PERL_BANNER)

    result = helper.verify_host_tools()

    assert calls == ["ninja", "perl", "git"]
    assert result == {name: value.splitlines()[0] for name, value in outputs.items()}


@pytest.mark.parametrize(
    "perl_banner",
    [
        "This is perl 5, version 32, subversion 10 (v5.32.10) built for MSWin32-x64-multi-thread",
        "This is perl 5, version 34, subversion 0 (v5.34.0) built for MSWin32-x64-multi-thread",
        "This is perl 5, version 32, subversion 1 (v5.32.1.2) built for MSWin32-x64-multi-thread",
    ],
    ids=["5.32.10", "5.34.0", "5.32.1.2"],
)
def test_verify_host_tools_rejects_other_perl_versions(monkeypatch, perl_banner):
    helper = _load()
    calls, _outputs = _mock_host_tools(helper, monkeypatch, perl_banner)

    with pytest.raises(RuntimeError, match="perl differs from its pinned runner version"):
        helper.verify_host_tools()

    assert calls == ["ninja", "perl"]


def test_run_build_script_reports_bounded_failure_tail_and_reraises(tmp_path, monkeypatch, capsys):
    helper = _load()
    script = tmp_path / "build_dep_all.cmd"
    log = tmp_path / "dependency-build.log"
    script.write_text("@echo off\n", encoding="utf-8")
    log_bytes = b"EARLY-OMITTED-MARKER\n" + (b"x" * (20 * 1024)) + b"\nCOMPILER-TAIL-MARKER\n"
    failure = subprocess.CalledProcessError(1, ["cmd.exe", str(script)])

    expected_command = f'cmd.exe /d /s /c ""{script}""'

    def fail_after_logging(command, *, stdout, **_kwargs):
        assert command == expected_command
        stdout.write(log_bytes)
        raise failure

    monkeypatch.setattr(helper.subprocess, "run", fail_after_logging)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        helper.run_build_script(script, {}, log)

    captured = capsys.readouterr()
    assert caught.value is failure
    assert caught.value.returncode == 1
    assert "build_dep_all.cmd failed" in captured.err
    assert "dependency-build.log" in captured.err
    assert "COMPILER-TAIL-MARKER" in captured.err
    assert "EARLY-OMITTED-MARKER" not in captured.err
    assert len(captured.err.encode("utf-8")) < 17 * 1024
    assert log.read_bytes() == log_bytes


def test_run_build_script_log_read_error_does_not_mask_subprocess_error(
        tmp_path, monkeypatch, capsys):
    helper = _load()
    script = tmp_path / "build_dep_all.cmd"
    log = tmp_path / "dependency-build.log"
    script.write_text("@echo off\n", encoding="utf-8")
    failure = subprocess.CalledProcessError(1, ["cmd.exe", str(script)])
    original_open = Path.open

    expected_command = f'cmd.exe /d /s /c ""{script}""'

    def fail_and_leave_empty(command, **_kwargs):
        assert command == expected_command
        raise failure

    def fail_log_read(path, mode="r", *args, **kwargs):
        if path == log and mode == "rb":
            raise OSError("synthetic log read failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(helper.subprocess, "run", fail_and_leave_empty)
    monkeypatch.setattr(Path, "open", fail_log_read)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        helper.run_build_script(script, {}, log)

    assert caught.value is failure
    assert caught.value.returncode == 1
    assert capsys.readouterr().err == ""
    with original_open(log, "rb") as source:
        assert source.read() == b""

@pytest.mark.skipif(os.name != "nt", reason="requires native cmd.exe batch semantics")
def test_run_build_script_executes_batch_path_with_spaces_and_ampersand(tmp_path, capsys):
    helper = _load()
    batch_dir = tmp_path / "build & tools"
    batch_dir.mkdir()
    script = batch_dir / "test build.cmd"
    log = batch_dir / "batch-output.log"
    script.write_bytes(
        b"@echo off\r\necho NATIVE-CMD-PATH-MARKER\r\nexit /b 23\r\n"
    )
    env = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "COMSPEC")
        if key in os.environ
    }

    with pytest.raises(subprocess.CalledProcessError) as caught:
        helper.run_build_script(script, env, log)

    expected_command = f'cmd.exe /d /s /c ""{script}""'
    assert caught.value.cmd == expected_command
    assert caught.value.returncode == 23
    assert b"NATIVE-CMD-PATH-MARKER" in log.read_bytes()
    assert "NATIVE-CMD-PATH-MARKER" in capsys.readouterr().err


def test_manifest_pins_google_brotli_and_all_source_inputs():
    helper = _load()
    inputs = helper.load_inputs()
    recipe = helper.read_recipe()
    native = recipe["pillow_brotli_native_source"]
    source = inputs["brotli_source"]
    assert {key: source[key] for key in ("filename", "url", "sha256", "size_bytes")} == {
        key: native[key] for key in ("filename", "url", "sha256", "size_bytes")
    }
    assert "github.com/google/brotli" in source["url"]
    parent_brotli = next(row for row in inputs["python_tool_wheels"] if row["name"] == "brotli")
    assert parent_brotli == {
        "name": "brotli", "version": "1.2.1",
        "filename": "brotli-1.2.1-cp313-cp313-win_amd64.whl",
        "url": "https://files.pythonhosted.org/packages/96/c2/0c7abb25013043d4bcf7dd768d343bf7dee6ce99ac8c172f7ee4aef1c772/brotli-1.2.1-cp313-cp313-win_amd64.whl",
        "size_bytes": 371072,
        "sha256": "e6c4fa55eab6b5684b33239597876a18a35ce1280b63ab960164032afbb8783f",
    }
    assert len(inputs["dependency_archives"]) == 11
    assert len(inputs["avif_nested"]) == 4
    assert all(row["url"].startswith("https://") for row in [
        inputs["pillow_source"], source, *inputs["dependency_archives"],
        *[row for row in inputs["avif_nested"] if "url" in row],
        *inputs["python_tool_wheels"], inputs["host_tools"]["nasm"],
    ])
    assert all(len(row["sha256"]) == 64 for row in [
        inputs["pillow_source"], source, *inputs["dependency_archives"],
        *inputs["avif_nested"], *inputs["python_tool_wheels"],
        inputs["host_tools"]["nasm"],
    ])


def test_ensure_parent_packaging_uses_hash_verified_pinned_wheels(tmp_path, monkeypatch):
    helper = _load()
    inputs = helper.load_inputs()
    rows = [item for item in inputs["python_tool_wheels"]
            if item["name"] in {"packaging", "brotli"}]
    payloads = {}
    for row in rows:
        payload = ("fixture " + row["name"] + " wheel").encode()
        row["size_bytes"] = len(payload)
        row["sha256"] = hashlib.sha256(payload).hexdigest()
        payloads[row["filename"]] = payload
    manifest = tmp_path / "windows-pillow-inputs.json"
    manifest.write_text(json.dumps(inputs), encoding="utf-8")
    helper.INPUTS = manifest
    helper.ROOT = tmp_path

    calls = []

    class Bootstrap:
        def verify_installed_pip(self):
            calls.append(("verified-pip",))

        def download_verified_wheel(self, path, **kwargs):
            calls.append(("download", path.name, kwargs))
            path.write_bytes(payloads[path.name])
            return path

    monkeypatch.setattr(helper, "load_sibling", lambda _name: Bootstrap())

    def fake_run(args, **kwargs):
        calls.append(("run", [str(item) for item in args]))
        if "-c" in args:
            return json.dumps({row["name"]: row["version"] for row in rows}, sort_keys=True)
        return ""

    monkeypatch.setattr(helper, "_run_output", fake_run)

    wheel = helper.ensure_parent_packaging()

    assert wheel.read_bytes() == payloads[wheel.name]
    assert (wheel.parent / next(row["filename"] for row in rows if row["name"] == "brotli")).read_bytes() == payloads[
        next(row["filename"] for row in rows if row["name"] == "brotli")]
    assert calls[0] == ("verified-pip",)
    downloads = [call for call in calls if call[0] == "download"]
    assert {call[1] for call in downloads} == {row["filename"] for row in rows}
    for _, filename, kwargs in downloads:
        row = next(item for item in rows if item["filename"] == filename)
        assert kwargs["expected_size"] == row["size_bytes"]
        assert kwargs["expected_sha256"] == row["sha256"]
    install = next(call[1] for call in calls if call[0] == "run" and "install" in call[1])
    assert install[1:5] == ["-m", "pip", "--isolated", "install"]
    assert "--no-index" in install and "--require-hashes" in install
    assert "packaging==26.3" in (wheel.parent / "pinned-parent-tools.txt").read_text()
    assert "brotli==1.2.1" in (wheel.parent / "pinned-parent-tools.txt").read_text()

    wheel.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="size mismatch"):
        helper.ensure_parent_packaging()


class _Response(io.BytesIO):
    def __init__(self, payload, url, headers):
        super().__init__(payload)
        self.url = url
        self.headers = headers

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def test_download_archive_checks_tls_size_hash_and_cleans_failed_partial(tmp_path, monkeypatch):
    helper = _load()
    payload = b"known archive bytes"
    row = {
        "url": "https://example.invalid/source.tar.gz",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    calls = []

    def good_response(request, **kwargs):
        calls.append((request.full_url, kwargs))
        return _Response(payload, request.full_url, {"Content-Length": str(len(payload))})

    monkeypatch.setattr(helper.urllib.request, "urlopen", good_response)
    destination = tmp_path / "source.tar.gz"
    assert helper.download_archive(row, destination) == destination
    assert destination.read_bytes() == payload
    assert calls[0][0] == row["url"]
    assert calls[0][1]["context"].verify_mode == helper.ssl.CERT_REQUIRED

    bad = tmp_path / "bad.tar.gz"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        helper.download_archive({**row, "sha256": "0" * 64}, bad)
    assert not bad.exists()
    assert not bad.with_name(bad.name + ".partial").exists()


def test_safe_source_extraction_blocks_traversal_symlinks_and_reuse(tmp_path):
    helper = _load()
    valid_archive = tmp_path / "valid.tar.gz"
    with tarfile.open(valid_archive, "w:gz") as archive:
        data = b"verified source"
        item = tarfile.TarInfo("fixture-root/src/file.txt")
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    extracted = tmp_path / "valid"
    helper.safe_extract_archive(valid_archive, extracted, "fixture-root")
    assert (extracted / "src" / "file.txt").read_bytes() == b"verified source"
    with pytest.raises(RuntimeError, match="already exists"):
        helper.safe_extract_archive(valid_archive, extracted, "fixture-root")

    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        data = b"outside"
        item = tarfile.TarInfo("fixture-root/../escape.txt")
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    with pytest.raises(RuntimeError, match="unsafe archive member path"):
        helper.safe_extract_archive(traversal, tmp_path / "traversal", "fixture-root")
    assert not (tmp_path / "escape.txt").exists()

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as archive:
        link = tarfile.TarInfo("fixture-root/escape.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../escape.txt"
        archive.addfile(link)
    with pytest.raises(RuntimeError, match="unsafe archive symlink target"):
        helper.safe_extract_archive(linked, tmp_path / "linked", "fixture-root")
    assert not (tmp_path / "escape.txt").exists()


def _recipe_module():
    patch = {
        "CMakeLists.txt": {
            "old-freetype": "WindowsTargetPlatformVersion=$(WindowsSDKVersion)",
        }
    }
    return SimpleNamespace(DEPS={
        "freetype": {"build": ["cmake --build . --parallel"], "patch": patch},
        "lcms2": {"build": ["msbuild lcms.vcxproj /m"], "patch": {
            "CMakeLists.txt": {
                "old-lcms": "WindowsTargetPlatformVersion=$(WindowsSDKVersion)",
            }
        }},
        "zlib": {"build": ["cmake --build . --parallel", "msbuild zlib.vcxproj /m"]},
        "libavif": {"build": ["rustup update stable", "python -m pip install meson"]},
    })


def test_build_command_patches_pin_sdk_and_bound_all_jobs(tmp_path):
    helper = _load()
    module = _recipe_module()
    helper.patch_sdk_targets(module, "10.0.26100.0")
    build_dir = tmp_path / "pillow-build"
    helper.patch_build_commands(module, build_dir)

    assert module.DEPS["freetype"]["patch"]["CMakeLists.txt"]["old-freetype"].endswith(
        "10.0.26100.0"
    )
    assert module.DEPS["lcms2"]["patch"]["CMakeLists.txt"]["old-lcms"].endswith(
        "10.0.26100.0"
    )
    assert module.DEPS["zlib"]["build"] == [
        "cmake --build . --parallel 2", "msbuild zlib.vcxproj /m:2",
    ]
    avif = module.DEPS["libavif"]["build"]
    assert len(avif) == 4
    assert "-DCMAKE_SYSTEM_VERSION=10.0.26100.0" in avif[0]
    assert "-DFETCHCONTENT_SOURCE_DIR_LIBAOM=" in avif[0]
    assert "-DFETCHCONTENT_FULLY_DISCONNECTED=ON" in avif[0]
    assert "-DAVIF_CODEC_DAV1D=LOCAL" in avif[0]
    assert "--parallel 1" in avif[1]
    assert all("rustup update" not in row and "pip install meson" not in row for row in avif)


def test_pillow_prepare_adapter_disables_network_and_refuses_existing_outputs(tmp_path):
    helper = _load()
    module = _recipe_module()
    inputs = helper.load_inputs()
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    build_dir = work / "pillow-build"
    vcvars = tmp_path / "vcvarsall.bat"
    msbuild = tmp_path / "MSBuild.exe"
    toolchain = {
        "root": tmp_path / "vs", "msbuild": msbuild, "vcvars": vcvars,
        "vcvars_arch": "amd64", "vcvars_version": "14.44.35207",
    }

    def fake_main():
        with pytest.raises(RuntimeError, match="network acquisition is disabled"):
            module.download_dep("https://invalid.example/archive")
        with pytest.raises(RuntimeError, match="unpinned dependency archive"):
            module.extract_dep("https://invalid.example/archive", "not-pinned.tar.gz", {
                "src_dir": str(work / "depends-source"),
            })
        module.shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir()
        (build_dir / "build_env.cmd").write_text(
            'call "' + str(vcvars) + '" x86_amd64\n', encoding="utf-8",
        )

    module.main = fake_main
    built, depends = helper.create_dependency_scripts(module, source, work, inputs, toolchain)
    assert built == build_dir
    assert depends.is_dir()
    assert "amd64 10.0.26100.0 -vcvars_ver=14.44.35207" in (build_dir / "build_env.cmd").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="outputs must be fresh"):
        helper.create_dependency_scripts(module, source, work, inputs, toolchain)


def test_git_archive_fetch_is_public_exact_commit_and_ignores_user_git_auth(tmp_path, monkeypatch):
    helper = _load()
    payload = b"fixture git archive"
    commit = "a" * 40
    row = {
        "name": "fixture", "repository": "https://example.invalid/repository.git",
        "ref": commit, "commit": commit, "archive_prefix": "fixture-1.0",
        "filename": "fixture.tar", "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "user-config")
    monkeypatch.setenv("GIT_CREDENTIAL_HELPER", "user-helper")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-test-token")
    monkeypatch.setenv("GH_TOKEN", "fake-test-token")
    monkeypatch.setattr(helper.shutil, "which", lambda _name: "git")

    def fake_run_output(args, *, env=None, cwd=None, **_kwargs):
        calls.append((list(map(str, args)), dict(env or {})))
        return commit if "rev-parse" in args else ""

    def fake_subprocess(args, *, stdout, **_kwargs):
        calls.append((list(map(str, args)), {}))
        stdout.write(payload)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helper, "_run_output", fake_run_output)
    monkeypatch.setattr(helper.subprocess, "run", fake_subprocess)
    destination = work / row["filename"]
    helper.create_git_archive(row, destination, work)

    assert helper.sha256_file(destination) == row["sha256"]
    fetch = next(args for args, _env in calls if "fetch" in args)
    assert row["repository"] in fetch and row["ref"] in fetch
    archive = next(args for args, _env in calls if "archive" in args)
    assert row["commit"] in archive and "--format=tar" in archive
    assert archive[1:3] == ["-c", "core.autocrlf=true"]
    env = next(env for args, env in calls if "fetch" in args)
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env
    assert "GIT_CREDENTIAL_HELPER" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def _qualified_windows_wheel(helper, tmp_path):
    prepare = helper.load_sibling("prepare_pillow")
    recipe = helper.read_recipe()
    tree = tmp_path / "wheel-tree"
    info = tree / "Pillow-13.0.0.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: false\n",
        encoding="utf-8",
    )
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: Pillow\nVersion: 13.0.0\n",
        encoding="utf-8",
    )
    (info / "LICENSE").write_text("brotli-1.2.0\n", encoding="utf-8")
    source = tmp_path / "brotli"
    source.mkdir()
    (source / "LICENSE").write_text("Brotli 1.2.1\nMIT License\n", encoding="utf-8")
    binaries = []
    for name in sorted(("_imaging", "_imagingcms", "_imagingft", "_imagingmath",
                        "_imagingmorph", "_imagingtk", "_avif", "_webp")):
        path = tree / "PIL" / (name + ".pyd")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("fixture " + name).encode())
        binaries.append(path)
    prepare.write_provenance(tree, info, source, recipe, binaries, method="source-build")
    output = tmp_path / "wheel-output"
    output.mkdir()
    return prepare.pack_wheel(tree, info, output, "win_amd64")


def test_source_build_wheel_and_cache_restore_validate_hashes_receipt_and_record(tmp_path):
    helper = _load()
    wheel = _qualified_windows_wheel(helper, tmp_path)
    receipt = helper.verify_record_and_receipt(wheel)
    assert receipt["method"] == "source-build"
    cache = tmp_path / "cache"
    cache.mkdir()
    cached_wheel = cache / helper.WHEEL_NAME
    shutil.copyfile(wheel, cached_wheel)
    (cache / (helper.WHEEL_NAME + ".sha256")).write_text(
        helper.sha256_file(cached_wheel) + "  " + helper.WHEEL_NAME + "\n",
        encoding="ascii",
    )
    work = tmp_path / "validation-work"
    work.mkdir()
    assert helper.verify_cache(cache, work) == cached_wheel

    original = cached_wheel.read_bytes()
    cached_wheel.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        helper.verify_cache(cache, work)
    cached_wheel.write_bytes(original)
    (cache / "unexpected.txt").write_text("not a wheel", encoding="ascii")
    with pytest.raises(RuntimeError, match="only the wheel"):
        helper.verify_cache(cache, work)


def test_windows_workflows_qualify_before_audit_without_private_core_ci_build():
    if "citationverifier-release-sync-20261101" in str(ROOT):
        private_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        private_release = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
        assert "prepare_windows_pillow.py" not in private_ci
        header = private_release.split("\njobs:", 1)[0]
        assert "\n  workflow_dispatch:\n" in header
        assert "\n  pull_request:" not in header and "\n  push:" not in header
        assert "--generate-notes" in private_release
        assert private_release.index("--ensure-parent-packaging") < private_release.index(
            "Install runtime and build dependencies on the runner"
        )
        assert private_release.index("prepare_windows_pillow.py") < private_release.index(
            "dependency_audit.py"
        )
        return

    public_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    public_release = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
    assert public_ci.index("--ensure-parent-packaging") < public_ci.index("Install selected-suite dependencies")
    assert public_ci.index("prepare_windows_pillow.py") < public_ci.index("dependency_audit.py")
    assert public_release.index("--ensure-parent-packaging") < public_release.index(
        "Install runtime and build dependencies on the runner"
    )
    assert public_release.index("prepare_windows_pillow.py") < public_release.index("dependency_audit.py")



def test_ensure_parent_mode_returns_without_building_or_creating_workdir(tmp_path, monkeypatch):
    helper = _load()
    helper.ROOT = tmp_path
    cache = tmp_path / "cache"
    work = tmp_path / "must-not-exist"
    calls = []
    monkeypatch.setattr(helper, "ensure_parent_packaging", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(helper, "build_or_restore", lambda *_args, **_kwargs: pytest.fail("build dispatched"))
    monkeypatch.setattr(helper, "cache_fingerprint", lambda **_kwargs: pytest.fail("fingerprint dispatched"))

    assert helper.main([
        "--ensure-parent-packaging", "--cache-dir", str(cache), "--work-dir", str(work),
    ]) == 0

    assert calls == ["bootstrap"]
    assert not work.exists()
    assert not cache.exists()


def test_discover_vs_toolchain_uses_registered_x64_toolset_and_windows_path_compare(tmp_path, monkeypatch):
    helper = _load()
    program_files = tmp_path / "Program Files (x86)"
    vswhere = program_files / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    vswhere.parent.mkdir(parents=True)
    vswhere.write_bytes(b"fixture vswhere")
    vs_root = tmp_path / "VS 2022 with spaces"
    toolset = vs_root / "VC" / "Tools" / "MSVC" / "14.44.35207"
    compiler = toolset / "bin" / "Hostx64" / "x64" / "cl.exe"
    linker = toolset / "bin" / "Hostx64" / "x64" / "link.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"registered compiler")
    linker.write_bytes(b"registered linker")
    vcvars = vs_root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    vcvars.parent.mkdir(parents=True)
    vcvars.write_text("@echo off\n", encoding="ascii")
    msbuild = vs_root / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
    msbuild.parent.mkdir(parents=True)
    msbuild.write_bytes(b"registered msbuild")
    sdk = helper.load_inputs()["windows"]["sdk_version"]
    kits = program_files / "Windows Kits" / "10"
    for name in ("ucrt", "shared", "um"):
        (kits / "Include" / sdk / name).mkdir(parents=True)
    for path in (kits / "Lib" / sdk / "ucrt" / "x64", kits / "Lib" / sdk / "um" / "x64"):
        path.mkdir(parents=True)

    monkeypatch.setattr(helper.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files))

    def fake_output(args, **_kwargs):
        assert str(args[0]) == str(vswhere)
        if args[-1] == "installationPath":
            return str(vs_root)
        if args[-1] == "installationVersion":
            return "17.14.0"
        raise AssertionError(args)

    monkeypatch.setattr(helper, "_run_output", fake_output)
    selected = helper.discover_vs_toolchain(vs_root=str(vs_root), toolset="14.44.35207")

    assert selected["root"] == vs_root
    assert selected["toolset"] == "14.44.35207"
    assert selected["compiler"] == compiler
    assert selected["linker"] == linker
    assert selected["vcvars_arch"] == "amd64"
    assert selected["vcvars_version"] == "14.44.35207"
    assert selected["compiler_sha256"] == hashlib.sha256(b"registered compiler").hexdigest()


def test_generated_vcvars_activation_matches_selected_toolset_host_target_and_sdk(tmp_path):
    helper = _load()
    root = tmp_path / "VS selected"
    vcvars = root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    build_env = tmp_path / "build_env.cmd"
    build_env.write_text(
        'call "' + str(vcvars) + '" x86_amd64\nset DISTUTILS_USE_SDK=1\n',
        encoding="utf-8",
    )
    toolchain = {
        "vcvars": vcvars,
        "vcvars_arch": "amd64",
        "vcvars_version": "14.44.35207",
    }

    helper.configure_vcvars_environment(build_env, toolchain, "10.0.26100.0")

    lines = build_env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f'call "{vcvars}" amd64 10.0.26100.0 -vcvars_ver=14.44.35207'
    assert lines[1] == "set DISTUTILS_USE_SDK=1"


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows vcvarsall parser")
def test_vcvarsall_accepts_selected_target_sdk_and_toolset_without_compiling(tmp_path):
    helper = _load()
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not program_files:
        pytest.skip("Visual Studio is not installed")
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        pytest.skip("Visual Studio is not installed")
    query = [
        vswhere, "-latest", "-version", "[17.0,18.0)", "-products", "*",
        "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    ]
    parser_env = os.environ.copy()
    parser_env.setdefault("ProgramData", r"C:\ProgramData")
    process = subprocess.Popen(
        [str(vswhere), *[str(value) for value in query[1:]], "-property", "installationPath"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=parser_env,
    )
    located, _ = process.communicate(timeout=30)
    if process.returncode != 0:
        pytest.fail("vswhere could not identify registered Visual Studio 2022: " + located)
    assert located.strip(), "vswhere returned an empty installation path"
    vs_root = Path(located.strip()).resolve(strict=True)
    tools = vs_root / "VC" / "Tools" / "MSVC"
    candidates = sorted(
        (path for path in tools.iterdir() if path.is_dir() and path.name.startswith("14.")),
        key=lambda path: helper._version_tuple(path.name),
    )
    if not candidates:
        pytest.skip("no registered v143 toolset is installed")
    selected = candidates[-1]
    compiler = selected / "bin" / "Hostx64" / "x64" / "cl.exe"
    vcvars = vs_root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not compiler.is_file() or not vcvars.is_file():
        pytest.skip("registered v143 toolset is incomplete")
    toolchain = {
        "root": vs_root, "toolset": selected.name, "vcvars": vcvars,
        "vcvars_arch": "amd64", "vcvars_version": selected.name,
        "compiler": compiler, "sdk": helper.load_inputs()["windows"]["sdk_version"],
    }
    script = tmp_path / "validate-vcvars.cmd"
    log = tmp_path / "validate-vcvars.log"
    script.write_text(
        "@echo off\r\n"
        + 'call "' + str(toolchain["vcvars"]) + '" '
        + toolchain["vcvars_arch"] + " " + toolchain["sdk"]
        + " -vcvars_ver=" + toolchain["vcvars_version"] + " >nul\r\n"
        + "if errorlevel 1 exit /b 1\r\n"
        + "where cl.exe\r\n"
        + "if errorlevel 1 exit /b 1\r\n"
        + "echo __VC_TOOLS__=%VCToolsInstallDir%\r\n"
        + "echo __WINDOWS_SDK_VERSION__=%WindowsSDKVersion%\r\n",
        encoding="utf-8", newline="",
    )

    command = script.name
    process = subprocess.Popen(
        ["cmd.exe", "/d", "/c", command], cwd=script.parent, env=parser_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output, _ = process.communicate(timeout=120)
    log.write_text(output, encoding="utf-8", newline="\n")
    if process.returncode != 0:
        pytest.fail("vcvarsall rejected the selected architecture, SDK, or toolset: " + output)

    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    compiler_line = next(line.strip() for line in lines if line.strip().lower().endswith("\\cl.exe"))
    selected_compiler = os.path.normcase(os.path.normpath(compiler_line))
    expected_compiler = os.path.normcase(os.path.normpath(str(toolchain["compiler"])))
    assert selected_compiler == expected_compiler
    tool_root = next(line.split("=", 1)[1].strip() for line in lines if line.startswith("__VC_TOOLS__="))
    expected_root = str(toolchain["compiler"].parents[3])
    assert os.path.normcase(os.path.normpath(tool_root.rstrip("\\/"))) == os.path.normcase(os.path.normpath(expected_root))
    sdk_version = next(line.split("=", 1)[1].strip() for line in lines if line.startswith("__WINDOWS_SDK_VERSION__="))
    assert sdk_version.rstrip("\\/") == toolchain["sdk"]


def test_create_build_venv_requires_actual_precompile_audit_before_return(tmp_path, monkeypatch):
    helper = _load()
    rows = [
        {"name": "pip", "version": "26.2.2", "sha256": "a" * 64},
        {"name": "packaging", "version": "26.3", "sha256": "b" * 64},
    ]
    venv_dir = tmp_path / "tools-venv"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    calls = []

    class Builder:
        def __init__(self, **_kwargs):
            pass

        def create(self, path):
            scripts = path / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(b"python fixture")

    def fake_output(args, **_kwargs):
        calls.append(("pip", [str(item) for item in args]))
        if "-c" in args:
            return json.dumps({row["name"]: row["version"] for row in rows}, sort_keys=True)
        return ""

    def fail_audit(python, work):
        calls.append(("audit", python, work))
        raise RuntimeError("precompile audit failed")

    monkeypatch.setattr(helper.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(helper, "_run_output", fake_output)
    monkeypatch.setattr(helper, "run_prebuild_dependency_audit", fail_audit)

    with pytest.raises(RuntimeError, match="precompile audit failed"):
        helper.create_build_venv(wheelhouse, venv_dir, rows)

    assert [call[0] for call in calls] == ["pip", "pip", "pip", "audit"]
    assert calls[-1][1] == venv_dir / "Scripts" / "python.exe"
    assert calls[-1][2] == tmp_path


def test_failed_build_venv_audit_prevents_native_dependency_build(tmp_path, monkeypatch):
    helper = _load()
    fake_sys = SimpleNamespace(
        platform="win32", version_info=(3, 13, 15), maxsize=2**64,
        executable=str(tmp_path / "python.exe"),
    )
    monkeypatch.setattr(helper, "sys", fake_sys)
    monkeypatch.setattr(helper.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(helper.shutil, "disk_usage", lambda _path: SimpleNamespace(free=20 * 1024**3))
    toolchain = {
        "root": tmp_path / "VS", "version": "17.14.0", "toolset": "14.44.35207",
        "vcvars_arch": "amd64", "vcvars_version": "14.44.35207",
        "compiler": tmp_path / "cl.exe", "compiler_sha256": "c" * 64,
        "linker": tmp_path / "link.exe", "linker_sha256": "d" * 64,
        "sdk": "10.0.26100.0",
    }
    monkeypatch.setattr(helper, "discover_vs_toolchain", lambda **_kwargs: toolchain)
    monkeypatch.setattr(helper, "verify_host_tools", lambda: {})

    class Bootstrap:
        def verify_installed_pip(self):
            pass

    monkeypatch.setattr(helper, "load_sibling", lambda _name: Bootstrap())
    monkeypatch.setattr(helper, "download_tool_wheels", lambda *_args: {})
    calls = []

    def fail_venv(*_args, **_kwargs):
        calls.append("audit-failed")
        raise RuntimeError("precompile audit failed")

    monkeypatch.setattr(helper, "create_build_venv", fail_venv)
    monkeypatch.setattr(helper, "run_build_script", lambda *_args, **_kwargs: calls.append("native-build"))
    work = tmp_path / "work"
    cache = tmp_path / "cache"

    with pytest.raises(RuntimeError, match="precompile audit failed"):
        helper.build_or_restore(cache, work)

    assert calls == ["audit-failed"]
    assert not (work / "downloads").exists()


def test_run_prebuild_dependency_audit_executes_real_cli_and_requires_pass(tmp_path, monkeypatch):
    helper = _load()
    build_python = tmp_path / "tools venv" / "Scripts" / "python.exe"
    report = tmp_path / "prebuild-dependency-audit.json"
    expected = {"status": "passed", "inventory": {"brotli": "1.2.1"}}
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        report.write_text(json.dumps(expected), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)

    assert helper.run_prebuild_dependency_audit(build_python, tmp_path) == report

    command, kwargs = calls[0]
    assert command[0] == str(build_python)
    assert command[1] == str(helper.ROOT / "packaging" / "dependency_audit.py")
    assert command[2:] == ["--report", str(report)]
    assert kwargs["check"] is False and kwargs["timeout"] >= 600
    assert report.is_file()


def test_build_wheel_activates_generated_environment_in_checked_batch(tmp_path, monkeypatch):
    helper = _load()
    checkout = tmp_path / "checkout with spaces"
    source = checkout / "pillow source"
    source.mkdir(parents=True)
    build_dir = checkout / "build dir"
    build_dir.mkdir()
    build_env = build_dir / "build_env.cmd"
    build_env.write_text("@echo off\nset DISTUTILS_USE_SDK=1\n", encoding="utf-8")
    build_python = checkout / "tools venv" / "Scripts" / "python.exe"
    build_python.parent.mkdir(parents=True)
    build_python.write_bytes(b"python fixture")
    raw_dir = build_dir / "raw wheel"
    cache_dir = checkout / "wheel cache"
    cache_dir.mkdir()
    env = {"PILLOW_BUILD": str(build_dir)}
    observed = []

    class Prepare:
        @staticmethod
        def unpack_wheel(raw_wheel, tree):
            info = tree / "Pillow-13.0.0.dist-info"
            info.mkdir(parents=True)
            native = tree / "PIL"
            native.mkdir()
            names = ["_imagingft"] + [f"_native{index}" for index in range(7)]
            for name in names:
                (native / f"{name}.pyd").write_bytes(b"native")
            return info

        @staticmethod
        def write_provenance(*_args, **_kwargs):
            pass

        @staticmethod
        def pack_wheel(_tree, _info, destination, tag):
            assert tag == "win_amd64"
            path = destination / helper.WHEEL_NAME
            path.write_bytes(b"qualified fixture")
            return path

    def fake_run_script(script, child_env, log):
        observed.append((script, child_env, log))
        lines = script.read_text(encoding="utf-8").splitlines()
        call_index = next(i for i, line in enumerate(lines) if line.lower().startswith("call "))
        pip_index = next(i for i, line in enumerate(lines) if "-m pip" in line and "wheel" in line)
        assert call_index < pip_index
        assert lines[call_index] == f'call "{build_env}"'
        assert lines[call_index + 1] == "if errorlevel 1 exit /b 1"
        assert lines[pip_index + 1] == "if errorlevel 1 exit /b 1"
        cd_line = next(line for line in lines if line.startswith("cd /d "))
        assert cd_line == f'cd /d "{source}"'
        assert 'set "PIP_CONFIG_FILE=NUL"' in lines
        (raw_dir / "pillow-13.0.0-cp313-cp313-win_amd64.whl").write_bytes(b"raw wheel")

    monkeypatch.setattr(helper, "run_build_script", fake_run_script)
    monkeypatch.setattr(helper, "load_sibling", lambda name: Prepare() if name == "prepare_pillow" else None)

    wheel = helper.build_wheel(source, raw_dir, cache_dir, build_dir, build_python, env)

    assert wheel == cache_dir / helper.WHEEL_NAME
    assert observed and observed[0][1] is env
    assert observed[0][2].name == "pillow-wheel-build.log"


def test_acquire_sources_downloads_pillow_metadata_as_separate_pinned_input(tmp_path, monkeypatch):
    helper = _load()
    inputs = helper.load_inputs()
    recipe = helper.read_recipe()
    work = tmp_path / "work"
    work.mkdir()
    downloaded = []
    archived = []

    def fake_download(row, destination, *, optional_size=False):
        downloaded.append(dict(row))
        destination.write_bytes(b"verified fixture archive")
        return destination

    def fake_git_archive(row, destination, _work):
        archived.append(dict(row))
        destination.write_bytes(b"verified fixture Git archive")

    monkeypatch.setattr(helper, "download_archive", fake_download)
    monkeypatch.setattr(helper, "create_git_archive", fake_git_archive)

    pillow, downloads, metadata = helper.acquire_sources(inputs, work)

    pin = recipe["pillow_brotli_pin"]
    metadata_rows = [row for row in downloaded if row["filename"] == pin["filename"]]
    assert len(metadata_rows) == 1
    assert metadata_rows[0] == {
        "filename": pin["filename"], "url": pin["url"],
        "sha256": pin["original_sha256"], "size_bytes": pin["size_bytes"],
    }
    assert pillow == downloads / inputs["pillow_source"]["filename"]
    assert metadata == downloads / pin["filename"]
    assert metadata.is_file()
    assert {row["name"] for row in archived} == {"aom", "libyuv", "libwebp-avif"}


def test_pinned_pillow_sdist_receives_shared_metadata_overlay_from_fresh_tree(tmp_path):
    helper = _load()
    inputs = helper.load_inputs()
    recipe = helper.read_recipe()
    sdist = Path(os.environ.get(
        "CALLIMACHUS_PILLOW_SOURCE_ARCHIVE",
        str(helper.ROOT / "build" / "security-pillow" / "win" / "downloads" / inputs["pillow_source"]["filename"]),
    ))
    metadata = Path(os.environ.get(
        "CALLIMACHUS_PILLOW_METADATA_INPUT",
        str(helper.ROOT / "build" / "security-pillow" / "win" / "downloads" / recipe["pillow_brotli_pin"]["filename"]),
    ))
    if not sdist.is_file() or not metadata.is_file():
        pytest.skip("a cache-hit run has no source downloads; cache-miss CI exercises this sdist integration")
    helper.verify_file(sdist, inputs["pillow_source"])
    pin = recipe["pillow_brotli_pin"]
    helper.verify_file(metadata, {
        "filename": pin["filename"], "sha256": pin["original_sha256"],
        "size_bytes": pin["size_bytes"],
    })
    source = tmp_path / inputs["pillow_source"]["root"]
    helper.safe_extract_archive(sdist, source, inputs["pillow_source"]["root"])
    assert not (source / pin["path"]).exists()

    patched = helper.patch_pillow_dependencies(source, recipe, metadata, tmp_path)

    assert patched == source / pin["path"]
    assert helper.sha256_file(patched) == pin["patched_sha256"]


def test_windows_workflows_provision_parent_tools_before_cache_and_pillow_install():
    helper = _load()
    desktop = (helper.ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
    workflows = [desktop]
    ci_path = helper.ROOT / ".github" / "workflows" / "ci.yml"
    ci = ci_path.read_text(encoding="utf-8")
    if "prepare_windows_pillow.py --ensure-parent-packaging" in ci:
        workflows.append(ci)
    else:
        assert "prepare_windows_pillow.py" not in ci
    for workflow in workflows:
        bootstrap = workflow.index("Ensure pinned packaging and Brotli before Pillow validation")
        fingerprint = workflow.index("Fingerprint verified Windows Pillow toolchain")
        build = workflow.index("Build or restore verified Windows Pillow wheel")
        install = workflow.index("Install qualified Windows Pillow before runtime dependencies")
        runtime_name = (
            "Install runtime and build dependencies on the runner"
            if "Install runtime and build dependencies on the runner" in workflow
            else "Install selected-suite dependencies"
        )
        runtime = workflow.index(runtime_name)
        audit = workflow.index("Audit installed runtime and build dependencies") if "Audit installed runtime and build dependencies" in workflow else workflow.index("Audit installed Python dependencies")
        assert bootstrap < fingerprint < build < install < runtime < audit
        assert "prepare_windows_pillow.py --ensure-parent-packaging" in workflow
