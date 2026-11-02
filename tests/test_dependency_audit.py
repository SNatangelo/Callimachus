# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline tests for dependency advisory qualification and frozen build gates."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "packaging" / "dependency_audit.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("dependency_audit_under_test", AUDIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.bare=false", *arguments], cwd=root,
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def _clean_git_checkout(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "--quiet")
    _git(root, "config", "core.longpaths", "true")
    _git(root, "config", "user.name", "Dependency Audit Tests")
    _git(root, "config", "user.email", "dependency-audit-tests@example.invalid")
    (root / ".gitignore").write_text("/build/\n", encoding="utf-8")
    (root / "VERSION").write_text("2.0.1\n", encoding="utf-8")
    (root / "tracked.txt").write_text("committed source\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "VERSION", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "initial source")
    return root


class _Distribution:
    def __init__(self, name: str, version: str):
        self.metadata = {"Name": name}
        self.version = version


def test_installed_inventory_canonicalizes_and_sorts_names(monkeypatch):
    audit = _load_audit()
    monkeypatch.setattr(audit.metadata, "distributions", lambda path=None: [
        _Distribution("Zed_Name", "2.0"),
        _Distribution("Alpha.Name", "1.1"),
    ])

    assert audit.installed_inventory() == {
        "alpha-name": "1.1",
        "zed-name": "2.0",
    }


def test_installed_inventory_rejects_ambiguous_versions(monkeypatch):
    audit = _load_audit()
    monkeypatch.setattr(audit.metadata, "distributions", lambda path=None: [
        _Distribution("demo_lib", "1.0"),
        _Distribution("Demo-Lib", "2.0"),
    ])

    with pytest.raises(audit.DependencyAuditError, match="ambiguous installed versions"):
        audit.installed_inventory()


def test_installed_inventory_keeps_pth_site_and_excludes_private_vendors(monkeypatch, tmp_path):
    audit = _load_audit()
    site_packages = tmp_path / "site-packages"
    pth_added_path = tmp_path / "path-added-by-pth"
    pip_vendor = site_packages / "pip" / "_vendor"
    setuptools_vendor = site_packages / "setuptools" / "_vendor"
    monkeypatch.setattr(audit.sys, "path", [
        str(site_packages), str(pth_added_path), str(pip_vendor), str(setuptools_vendor),
    ])

    def distributions(*, path):
        included = {Path(item).resolve() for item in path}
        assert site_packages.resolve() in included
        assert pth_added_path.resolve() in included
        assert pip_vendor.resolve() not in included
        assert setuptools_vendor.resolve() not in included
        available = []
        if site_packages.resolve() in included:
            available.append(_Distribution("ordinary-lib", "1.0"))
        if pth_added_path.resolve() in included:
            available.append(_Distribution("pth-lib", "2.0"))
        if pip_vendor.resolve() in included:
            available.append(_Distribution("pip-private-lib", "90.0"))
        if setuptools_vendor.resolve() in included:
            available.append(_Distribution("setuptools-private-lib", "91.0"))
        return available

    monkeypatch.setattr(audit.metadata, "distributions", distributions)

    assert audit.installed_inventory() == {
        "ordinary-lib": "1.0",
        "pth-lib": "2.0",
    }


def test_validate_report_separates_fixed_and_unfixed_findings():
    audit = _load_audit()
    inventory = {"clean-lib": "1.0", "fixed-lib": "1.0", "unfixed-lib": "1.0"}
    raw = {"dependencies": [
        {"name": "clean-lib", "version": "1.0", "vulns": []},
        {"name": "fixed-lib", "version": "1.0", "vulns": [
            {"id": "GHSA-fixed", "fix_versions": ["2.0"]},
        ]},
        {"name": "unfixed-lib", "version": "1.0", "vulns": [
            {"id": "GHSA-unfixed", "fix_versions": []},
        ]},
    ]}

    fixed, unfixed = audit.validate_report(raw, inventory, 1)

    assert fixed == [{
        "name": "fixed-lib", "version": "1.0",
        "id": "GHSA-fixed", "fix_versions": ["2.0"],
    }]
    assert unfixed == [{
        "name": "unfixed-lib", "version": "1.0",
        "id": "GHSA-unfixed", "fix_versions": [],
    }]


@pytest.mark.parametrize(
    ("raw", "returncode", "message"),
    [
        pytest.param(
            {"dependencies": [{
                "name": "demo-lib", "version": "1.0", "skip_reason": "not checked", "vulns": [],
            }]},
            0, "skipped", id="skipped-dependency",
        ),
        pytest.param({"dependencies": []}, 0, "incomplete", id="missing-package"),
        pytest.param(
            {"dependencies": [{
                "name": "demo-lib", "version": "1.0",
                "vulns": [{"id": "GHSA-demo", "fix_versions": ["2.0"]}],
            }]},
            0, "incomplete or has an inconsistent exit status", id="inconsistent-exit",
        ),
        pytest.param(
            {"dependencies": [{
                "name": "demo-lib", "version": "1.0",
                "vulns": [{"id": "", "fix_versions": []}],
            }]},
            1, "malformed vulnerability data", id="malformed-advisory",
        ),
    ],
)
def test_validate_report_rejects_incomplete_or_inconsistent_data(raw, returncode, message):
    audit = _load_audit()

    with pytest.raises(audit.DependencyAuditError, match=message):
        audit.validate_report(raw, {"demo-lib": "1.0"}, returncode)


@pytest.mark.parametrize(
    ("findings", "returncode", "expected_status", "blocks", "warns"),
    [
        pytest.param([], 0, "passed", False, False, id="patched-pass"),
        pytest.param(
            [{"id": "GHSA-fixed", "fix_versions": ["2.0"]}],
            1, "blocked", True, False, id="fixed-version-blocks",
        ),
        pytest.param(
            [{"id": "GHSA-unfixed", "fix_versions": []}],
            1, "passed", False, True, id="unfixed-version-warns",
        ),
    ],
)
def test_audit_inventory_classifies_findings_without_running_commands(
    monkeypatch, tmp_path, capsys, findings, returncode, expected_status, blocks, warns,
):
    audit = _load_audit()
    inventory = {"demo-lib": "1.0"}
    fake_python = tmp_path / "tool-venv" / "python.exe"
    commands = []

    def fake_auditor_python(root, work):
        assert root == tmp_path
        assert work == tmp_path / "build" / "security-audit"
        return fake_python

    def fake_run(command, *, cwd, env, timeout=600):
        commands.append(command)
        assert cwd == tmp_path
        assert command[0] == str(fake_python)
        for option in ("--disable-pip", "--no-deps"):
            assert option in command
        assert command[command.index("--vulnerability-service") + 1] == "pypi"
        assert command[command.index("--format") + 1] == "json"
        assert command[command.index("--progress-spinner") + 1] == "off"
        requirements = Path(command[command.index("-r") + 1])
        assert requirements.read_text(encoding="utf-8") == "demo-lib==1.0\n"
        output = Path(command[command.index("-o") + 1])
        output.write_text(json.dumps({"dependencies": [{
            "name": "demo-lib", "version": "1.0", "vulns": findings,
        }]}), encoding="utf-8")
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(audit, "_auditor_python", fake_auditor_python)
    monkeypatch.setattr(audit, "_run", fake_run)
    report_path = tmp_path / "dependency-audit.json"

    if blocks:
        with pytest.raises(audit.DependencyAuditError, match="Build blocked"):
            audit.audit_inventory(tmp_path, inventory, report_path)
    else:
        result = audit.audit_inventory(tmp_path, inventory, report_path)
        assert result["status"] == expected_status

    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["status"] == expected_status
    assert saved["inventory"] == inventory
    assert saved["inventory_sha256"] == audit._digest(inventory)
    assert commands and len(commands) == 1
    assert ("no published fix" in capsys.readouterr().err) is warns


def test_audit_inventory_rejects_malformed_json(monkeypatch, tmp_path):
    audit = _load_audit()
    monkeypatch.setattr(audit, "_auditor_python", lambda *_args: tmp_path / "python.exe")

    def fake_run(command, **_kwargs):
        Path(command[command.index("-o") + 1]).write_text("{broken", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(audit, "_run", fake_run)
    with pytest.raises(audit.DependencyAuditError, match="did not return a usable report"):
        audit.audit_inventory(tmp_path, {"demo-lib": "1.0"}, tmp_path / "report.json")


def test_audit_subprocess_timeout_fails_closed(monkeypatch, tmp_path):
    audit = _load_audit()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 600))

    monkeypatch.setattr(audit.subprocess, "run", timeout)
    with pytest.raises(audit.DependencyAuditError, match="failed or timed out"):
        audit._run(["fake-python", "-m", "pip_audit"], cwd=tmp_path, env={}, timeout=1)


def test_unqualified_pip_fails_before_inventory_or_auditor(monkeypatch, tmp_path):
    audit = _load_audit()
    root = tmp_path / "checkout"
    bootstrap = root / "packaging" / "bootstrap_pip.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        "def verify_installed_pip():\n"
        "    raise RuntimeError('pip is not qualified')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        audit, "installed_inventory",
        lambda: pytest.fail("inventory collection ran before pip qualification"),
    )
    monkeypatch.setattr(
        audit, "audit_inventory",
        lambda *_args: pytest.fail("auditor setup ran before pip qualification"),
    )

    with pytest.raises(audit.DependencyAuditError, match="Unqualified pip"):
        audit.run_prebuild_audit(root)


def test_inventory_change_during_scan_blocks_report(monkeypatch, tmp_path):
    audit = _load_audit()
    inventories = iter([
        {"demo-lib": "1.0"},
        {"demo-lib": "1.1"},
    ])
    monkeypatch.setattr(audit, "_verify_bootstrap", lambda _root: None)
    monkeypatch.setattr(audit, "installed_inventory", lambda: next(inventories))
    monkeypatch.setattr(
        audit, "audit_inventory",
        lambda _root, _inventory, _report, **_kwargs: {"status": "passed"},
    )

    with pytest.raises(audit.DependencyAuditError, match="changed during the security scan"):
        audit.run_prebuild_audit(tmp_path)



class _PillowDistribution:
    def __init__(self, site_root, files):
        self.site_root = site_root
        self.files = files
        self.metadata = {"Name": "Pillow"}
        self.version = "13.0.0"

    def locate_file(self, relative):
        return self.site_root / Path(str(relative))


def _pillow_vendor_fixture(tmp_path, monkeypatch, *, library_version="1.2.1", platform="linux"):
    audit = _load_audit()
    root = tmp_path / "checkout"
    constraints = root / "packaging" / "release-constraints.txt"
    constraints.parent.mkdir(parents=True)
    constraints.write_text("Brotli==1.2.1\nPillow==13.0.0\n", encoding="utf-8")
    source_manifest = root / "packaging" / "store" / "native-recipes" / "security-source-inputs.json"
    source_manifest.parent.mkdir(parents=True)
    brotli_source = {
        "name": "Brotli", "historical_version": "1.2.0", "version": "1.2.1",
        "filename": "brotli-1.2.1.tar.gz",
        "url": "https://files.pythonhosted.org/packages/example/brotli-1.2.1.tar.gz",
        "sha256": "02b2f7b5a83485efe448a44ddf7983def05c3bb1dcf917d4f7154070260e13de",
        "size_bytes": 7389447,
    }
    pillow_source = {
        "name": "Pillow", "historical_version": "12.3.0", "version": "13.0.0",
        "filename": "pillow-13.0.0.tar.gz",
        "url": "https://files.pythonhosted.org/packages/example/pillow-13.0.0.tar.gz",
        "sha256": "cdcd1ba258178f7356d74170c9a81ba4eab6a5d55748bc402a5a2a6fe6be3510",
        "size_bytes": 47058631,
    }
    pin = {
        "upstream_commit": "293218ad059d994baf1777d882911bed6a49704a",
        "path": ".github/dependencies.json",
        "filename": "pillow-13.0.0-dependencies.json",
        "url": "https://raw.githubusercontent.com/python-pillow/Pillow/293218ad059d994baf1777d882911bed6a49704a/.github/dependencies.json",
        "size_bytes": 376,
        "original_sha256": "63413d6dccf72367fc4c04c2560999e7a40ea14cf226dcc995d238cf3d25533b",
        "patched_sha256": "1ad5089c5f81f70e75b17653ea621dd9c07d6836b69a0a695ac7c113a2861606",
        "from_version": "1.2.0", "to_version": "1.2.1",
    }
    native_source = {
        "filename": "brotli-1.2.1-github.tar.gz", "version": "1.2.1",
        "url": "https://github.com/google/brotli/archive/refs/tags/v1.2.1.tar.gz",
        "sha256": "98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f",
        "size_bytes": 646598,
    }
    source_manifest.write_text(json.dumps({
        "schema_version": 1, "source_overrides": [brotli_source, pillow_source],
        "pillow_brotli_pin": pin, "pillow_brotli_native_source": native_source,
    }), encoding="utf-8")
    site = tmp_path / "site-packages"
    info = site / "Pillow-13.0.0.dist-info"
    license_file = info / "licenses" / "Brotli-1.2.1-LICENSE"
    license_file.parent.mkdir(parents=True)
    (info / "METADATA").write_text("Name: Pillow\nVersion: 13.0.0\n", encoding="utf-8")
    license_file.write_text("Brotli 1.2.1\nMIT License\nCopyright 2026\n", encoding="ascii")
    general_license = info / "licenses" / "LICENSE"
    general_license.write_text("Pillow third-party notices\n", encoding="ascii")
    if platform == "linux":
        binary_paths = [
            site / "pillow.libs" / f"libbrotlicommon-test.so.{library_version}",
            site / "pillow.libs" / f"libbrotlidec-test.so.{library_version}",
        ]
    elif platform == "macos":
        binary_paths = [
            site / "PIL" / ".dylibs" / f"libbrotlicommon.{library_version}.dylib",
            site / "PIL" / ".dylibs" / f"libbrotlidec.{library_version}.dylib",
        ]
    elif platform == "windows":
        binary_paths = [site / "PIL" / "_webp.cp313-win_amd64.pyd"]
    else:
        raise AssertionError(platform)
    for index, path in enumerate(binary_paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"binary-{index}".encode("ascii"))
    relative = lambda path: path.relative_to(site).as_posix()
    receipt = {
        "format": "callimachus.pillow-native-security.v1",
        "owner": {"name": "Pillow", "version": "13.0.0"},
        "method": "shared-library-repair",
        "component": {
            "name": "Brotli", "version": "1.2.1",
            "source": {key: brotli_source[key] for key in ("filename", "url", "sha256", "size_bytes")},
            "upstream_pin": {
                "commit": pin["upstream_commit"], "path": pin["path"],
                "upstream_sha256": pin["original_sha256"], "patched_sha256": pin["patched_sha256"],
                "from_version": pin["from_version"], "to_version": pin["to_version"],
            },
        },
        "license": {
            "path": relative(license_file),
            "sha256": hashlib.sha256(license_file.read_bytes()).hexdigest(),
        },
        "binary_files": [
            {"path": relative(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in binary_paths
        ],
    }
    receipt_path = info / "callimachus-pillow-native-security.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    files = [
        "Pillow-13.0.0.dist-info/METADATA",
        "Pillow-13.0.0.dist-info/licenses/Brotli-1.2.1-LICENSE",
        "Pillow-13.0.0.dist-info/licenses/LICENSE",
        "Pillow-13.0.0.dist-info/callimachus-pillow-native-security.json",
        *(relative(path) for path in binary_paths),
    ]
    distribution = _PillowDistribution(site, files)
    monkeypatch.setattr(audit.metadata, "distribution", lambda name: distribution)
    return audit, root, distribution, receipt, license_file, binary_paths


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_pillow_brotli_vendor_gate_accepts_verified_patched_metadata(tmp_path, monkeypatch, platform):
    audit, root, _distribution, _receipt, _license, binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch, platform=platform,
    )

    result = audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})

    assert result["pillow_version"] == "13.0.0"
    assert result["brotli_version"] == "1.2.1"
    assert result["source"]["sha256"] == "02b2f7b5a83485efe448a44ddf7983def05c3bb1dcf917d4f7154070260e13de"
    assert result["upstream_pin"]["patched_sha256"] == "1ad5089c5f81f70e75b17653ea621dd9c07d6836b69a0a695ac7c113a2861606"
    assert result["license_path"].endswith("Brotli-1.2.1-LICENSE")
    assert len(result["binary_files"]) == len(binaries)
    assert all(not Path(item["path"]).is_absolute() for item in result["binary_files"])
    assert result["source_inputs_sha256"]
    assert result["method"] == "shared-library-repair"


def test_pillow_brotli_vendor_gate_accepts_verified_native_source_build(tmp_path, monkeypatch):
    audit, root, distribution, receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch, platform="windows",
    )
    native_source = json.loads((root / "packaging/store/native-recipes/security-source-inputs.json").read_text(
        encoding="utf-8"
    ))["pillow_brotli_native_source"]
    receipt["method"] = "source-build"
    receipt["component"]["source"] = {
        key: native_source[key] for key in ("filename", "url", "sha256", "size_bytes")
    }
    receipt_path = distribution.site_root / "Pillow-13.0.0.dist-info" / "callimachus-pillow-native-security.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})

    assert result["method"] == "source-build"
    assert result["source"]["filename"] == "brotli-1.2.1-github.tar.gz"


def test_pillow_brotli_vendor_gate_rejects_method_source_mismatch(tmp_path, monkeypatch):
    audit, root, distribution, receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch,
    )
    receipt["method"] = "source-build"
    receipt_path = distribution.site_root / "Pillow-13.0.0.dist-info" / "callimachus-pillow-native-security.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="method, version, or source provenance"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_rejects_old_shared_library(tmp_path, monkeypatch):
    audit, root, *_ = _pillow_vendor_fixture(tmp_path, monkeypatch, library_version="1.2.0")

    with pytest.raises(audit.DependencyAuditError, match="bundled Brotli version"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_requires_recorded_provenance(tmp_path, monkeypatch):
    audit, root, distribution, _receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch,
    )
    distribution.files = [item for item in distribution.files if not item.endswith("native-security.json")]

    with pytest.raises(audit.DependencyAuditError, match="absent from Pillow RECORD"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_rejects_stale_license_marker(tmp_path, monkeypatch):
    audit, root, distribution, _receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch, platform="windows",
    )
    license_file = distribution.site_root / "Pillow-13.0.0.dist-info" / "licenses" / "LICENSE"
    license_file.write_text("===== brotli-1.2.0 =====\nMIT License\n", encoding="ascii")

    with pytest.raises(audit.DependencyAuditError, match="bundled Brotli version"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_rejects_source_hash_mismatch(tmp_path, monkeypatch):
    audit, root, distribution, receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch,
    )
    receipt["component"]["source"]["sha256"] = "0" * 64
    receipt_path = distribution.site_root / "Pillow-13.0.0.dist-info" / "callimachus-pillow-native-security.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="source provenance differs"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_rejects_escaping_license_path(tmp_path, monkeypatch):
    audit, root, distribution, receipt, _license, _binaries = _pillow_vendor_fixture(
        tmp_path, monkeypatch,
    )
    receipt["license"]["path"] = "../outside-license"
    receipt_path = distribution.site_root / "Pillow-13.0.0.dist-info" / "callimachus-pillow-native-security.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="unsafe Brotli license path"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_brotli_vendor_gate_tracks_active_brotli_pin(tmp_path, monkeypatch):
    audit, root, *_ = _pillow_vendor_fixture(tmp_path, monkeypatch)
    (root / "packaging" / "release-constraints.txt").write_text(
        "Brotli==1.2.2\nPillow==13.0.0\n", encoding="utf-8",
    )

    with pytest.raises(audit.DependencyAuditError, match="active release constraints"):
        audit._pillow_brotli_vendor(root, {"brotli": "1.2.1", "pillow": "13.0.0"})


def test_pillow_vendor_is_optional_when_runtime_does_not_install_pillow(tmp_path):
    audit = _load_audit()

    assert audit._pillow_brotli_vendor(tmp_path, {"brotli": "1.2.1"}) is None


def test_prepare_sources_overlays_secure_sdists_without_rewriting_history(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "prepare_sources_security_test", ROOT / "packaging" / "store" / "prepare_sources.py",
    )
    assert spec is not None and spec.loader is not None
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    rows = prepare.source_inputs()
    monkeypatch.setattr(prepare, "fetch", lambda *_args, **_kwargs: pytest.fail("list mode attempted a download"))
    prepare.main(["--list"])
    listed = json.loads(capsys.readouterr().out)
    assert any(row.get("metadata_only") for row in listed)
    source_rows = {
        row["name"]: row for row in rows
        if row.get("destination", "").startswith("python-sources/")
    }
    assert source_rows["brotli"]["version"] == "1.2.1"
    assert source_rows["pillow"]["version"] == "13.0.0"
    assert source_rows["brotli"]["size_bytes"] == 7389447
    assert source_rows["pillow"]["size_bytes"] == 47058631
    assert not any(row["filename"] in {"brotli-1.2.0.tar.gz", "pillow-12.3.0.tar.gz"} for row in rows)
    metadata = next(row for row in rows if row.get("metadata_only"))
    assert metadata["filename"] == "pillow-13.0.0-dependencies.json"
    assert metadata["size_bytes"] == 376
    native_source = next(row for row in rows if row.get("source_kind") == "pillow-brotli-native")
    assert native_source["filename"] == "brotli-1.2.1-github.tar.gz"
    assert native_source["url"] == "https://github.com/google/brotli/archive/refs/tags/v1.2.1.tar.gz"
    assert native_source["sha256"] == "98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f"
    assert native_source["size_bytes"] == 646598
    assert native_source["destination"] == "security-native-source"
    history = prepare.read_json(ROOT / "packaging/store/native-recipes/python-sources/manifest.json")
    assert {row["name"]: row["version"] for row in history if row["name"] in {"brotli", "pillow"}} == {
        "brotli": "1.2.0", "pillow": "12.3.0",
    }


def test_prepare_sources_rejects_security_override_outside_active_constraints(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "prepare_sources_stale_overlay_test", ROOT / "packaging" / "store" / "prepare_sources.py",
    )
    assert spec is not None and spec.loader is not None
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    constraints = tmp_path / "packaging" / "release-constraints.txt"
    constraints.parent.mkdir(parents=True)
    constraints.write_text("Brotli==1.2.2\nPillow==13.0.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="brotli override does not match"):
        prepare._security_overrides(ROOT / "packaging" / "store", tmp_path)


def test_prepare_sources_patches_only_verified_pillow_pin(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "prepare_sources_pin_patch_test", ROOT / "packaging" / "store" / "prepare_sources.py",
    )
    assert spec is not None and spec.loader is not None
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    store = tmp_path / "store"
    (store / "native-recipes").mkdir(parents=True)
    original = b'{\n  "brotli": "1.2.0"\n}\n'
    patched = b'{\n  "brotli": "1.2.1"\n}\n'
    pin = {
        "path": ".github/dependencies.json",
        "filename": "pillow-13.0.0-dependencies.json",
        "size_bytes": len(original),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "from_version": "1.2.0", "to_version": "1.2.1",
    }
    config = {
        "source_overrides": [{"name": "Pillow", "version": "13.0.0"}],
        "pillow_brotli_pin": pin,
    }
    (store / "native-recipes" / "security-source-inputs.json").write_text(
        json.dumps(config), encoding="utf-8",
    )
    native = tmp_path / "native"
    source_root = native / "python-sources" / "pillow-source" / "pillow-13.0.0"
    source_root.mkdir(parents=True)
    metadata_input = native / "inputs" / pin["filename"]
    metadata_input.parent.mkdir(parents=True)
    metadata_input.write_bytes(original)

    target = prepare.apply_pillow_brotli_source_pin(native, store)

    assert target.read_bytes() == patched
    assert prepare.apply_pillow_brotli_source_pin(native, store) == target
    target.write_bytes(b'{"brotli":"1.2.0","other":"changed"}')
    with pytest.raises(ValueError, match="differs from the verified upstream bytes"):
        prepare.apply_pillow_brotli_source_pin(native, store)

def test_prebuild_audit_carries_pillow_vendor_provenance(monkeypatch, tmp_path):
    audit = _load_audit()
    inventory = {"brotli": "1.2.1", "pillow": "13.0.0"}
    vendor = {
        "pillow_version": "13.0.0", "brotli_version": "1.2.1",
        "source_inputs_sha256": "a" * 64, "binary_files": [],
    }
    monkeypatch.setattr(audit, "_verify_bootstrap", lambda _root: None)
    monkeypatch.setattr(audit, "installed_inventory", lambda: dict(inventory))
    monkeypatch.setattr(audit, "_pillow_brotli_vendor", lambda _root, _inventory: vendor)

    def report(root, actual_inventory, report_path, *, pillow_vendor):
        assert root == tmp_path
        assert actual_inventory == inventory
        assert pillow_vendor == vendor
        return {"status": "passed", "inventory": inventory, "pillow_vendor": pillow_vendor}

    monkeypatch.setattr(audit, "audit_inventory", report)

    result = audit.run_prebuild_audit(tmp_path)

    assert result["pillow_vendor"] == vendor




def test_source_revision_accepts_clean_checkout_and_ignored_build_outputs(tmp_path):
    audit = _load_audit()
    root = _clean_git_checkout(tmp_path / "source")
    build_output = root / "build" / "desktop" / "dependency-audit.json"
    build_output.parent.mkdir(parents=True)
    build_output.write_text("ignored build report", encoding="utf-8")

    revision = audit._revision(root)

    assert len(revision) == 40
    assert revision == _git(root, "rev-parse", "HEAD")


def test_source_revision_accepts_only_generated_cryptography_build_outputs(tmp_path):
    audit = _load_audit()
    root = _clean_git_checkout(tmp_path / "source")
    repository_ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    generated_rule = "/build/security-cryptography/"
    assert repository_ignore.count(generated_rule) == 1
    legacy_ignore = repository_ignore.replace(generated_rule + "\n", "", 1)
    assert legacy_ignore != repository_ignore
    (root / ".gitignore").write_text(legacy_ignore, encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "--quiet", "-m", "use prior generated-output ignore rules")

    output_root = root / "build" / "security-cryptography"
    generated_outputs = (
        "build-venv/pyvenv.cfg",
        "wheelhouse/cryptography-50.0.1-cp313-abi3-macosx_14_4_x86_64.whl",
        "build-dependency-audit.json",
        "OpenSSL-LICENSE.txt",
        "cryptography-dependency-audit.json",
        "build-environment.txt",
    )
    for relative_path in generated_outputs:
        output = output_root / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("synthetic generated build output\n", encoding="utf-8")
    provenance = root / "build" / "desktop" / "macos-cryptography-provenance-x86_64.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text("synthetic provenance\n", encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="clean source Git checkout") as error:
        audit._revision(root)
    assert "?? build/" in str(error.value)

    (root / ".gitignore").write_text(repository_ignore, encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "--quiet", "-m", "use repository generated-output ignore rules")

    revision = audit._revision(root)
    assert revision == _git(root, "rev-parse", "HEAD")

    unexpected = root / "build" / "unexpected.py"
    unexpected.write_text("untracked source\n", encoding="utf-8")
    untracked_status = _git(root, "status", "--porcelain", "--untracked-files=all")
    assert untracked_status.splitlines() == ["?? build/unexpected.py"]
    with pytest.raises(audit.DependencyAuditError, match="clean source Git checkout") as error:
        audit._revision(root)
    assert "?? build/" in str(error.value)

    unexpected.unlink()
    (root / "tracked.txt").write_text("modified source\n", encoding="utf-8")
    with pytest.raises(audit.DependencyAuditError, match="clean source Git checkout") as error:
        audit._revision(root)
    assert "tracked.txt" in str(error.value)

@pytest.mark.parametrize("dirty_path", ["tracked", "untracked"])
def test_source_revision_rejects_tracked_and_untracked_changes(tmp_path, dirty_path):
    audit = _load_audit()
    root = _clean_git_checkout(tmp_path / "source")
    if dirty_path == "tracked":
        (root / "tracked.txt").write_text("modified source\n", encoding="utf-8")
    else:
        (root / "untracked.txt").write_text("new source\n", encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="clean source Git checkout") as error:
        audit._revision(root)

    expected_name = "tracked.txt" if dirty_path == "tracked" else "untracked.txt"
    assert expected_name in str(error.value)


def test_source_revision_reports_bounded_dirty_paths(tmp_path):
    audit = _load_audit()
    root = _clean_git_checkout(tmp_path / "source")
    for index in range(80):
        (root / f"untracked-{index:03}.txt").write_text("new source\n", encoding="utf-8")

    with pytest.raises(audit.DependencyAuditError, match="clean source Git checkout") as error:
        audit._revision(root)

    message = str(error.value)
    assert "untracked-000.txt" in message
    assert "additional dirty path(s) omitted" in message
    assert len(message) < 4_100


def test_source_revision_rejects_nested_archive_inside_another_repository(tmp_path):
    audit = _load_audit()
    outer = tmp_path / "outer"
    inner = outer / "source-archive"
    outer.mkdir()
    inner.mkdir()
    _git(outer, "init", "--quiet")
    _git(outer, "config", "core.longpaths", "true")
    _git(outer, "config", "user.name", "Dependency Audit Tests")
    _git(outer, "config", "user.email", "dependency-audit-tests@example.invalid")
    (inner / "VERSION").write_text("2.0.1\n", encoding="utf-8")
    _git(outer, "add", "source-archive/VERSION")
    _git(outer, "commit", "--quiet", "-m", "archive payload")

    with pytest.raises(audit.DependencyAuditError, match="top-level Git checkout"):
        audit._revision(inner)


def test_frozen_audit_rejects_committed_head_change_since_freeze_started(monkeypatch, tmp_path):
    audit = _load_audit()
    root = _clean_git_checkout(tmp_path / "source")
    source_revision = audit._revision(root)
    distribution = tmp_path / "dist"
    app_file = distribution / "Callimachus" / "Callimachus"
    app_file.parent.mkdir(parents=True)
    app_file.write_bytes(b"synthetic frozen executable")
    inventory = {"demo-lib": "1.0"}
    vendor = {"pillow_version": "13.0.0", "brotli_version": "1.2.1", "binary_files": []}
    report = {
        "status": "passed", "inventory": inventory,
        "inventory_sha256": audit._digest(inventory), "pillow_vendor": vendor,
    }
    monkeypatch.setattr(audit, "installed_inventory", lambda: dict(inventory))
    monkeypatch.setattr(audit, "_pillow_brotli_vendor", lambda *_args: vendor)

    (root / "tracked.txt").write_text("new committed source\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "source changed during freeze")

    with pytest.raises(audit.DependencyAuditError, match="source revision changed"):
        audit.bind_frozen_audit(
            root, distribution, report, source_revision=source_revision,
        )
    assert not (distribution / audit.RECEIPT_NAME).exists()


def _frozen_fixture(audit, monkeypatch, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "VERSION").write_text("2.0.1\n", encoding="utf-8")
    distribution = tmp_path / "dist"
    app_file = distribution / "Callimachus.app" / "Contents" / "Info.plist"
    app_file.parent.mkdir(parents=True)
    app_file.write_bytes(b"signed app bytes")
    inventory = {"demo-lib": "1.0"}
    report = {
        "format": "callimachus.dependency-audit.v1",
        "status": "passed",
        "inventory": inventory,
        "inventory_sha256": audit._digest(inventory),
    }
    monkeypatch.setattr(audit, "installed_inventory", lambda: dict(inventory))
    monkeypatch.setattr(audit, "_revision", lambda _root: "a" * 40)
    return root, distribution, app_file, inventory, report


def test_frozen_audit_serializes_files_and_rechecks_advisories(monkeypatch, tmp_path):
    audit = _load_audit()
    root, distribution, app_file, _inventory, report = _frozen_fixture(audit, monkeypatch, tmp_path)
    vendor = {"pillow_version": "13.0.0", "brotli_version": "1.2.1", "binary_files": []}
    report["pillow_vendor"] = vendor
    monkeypatch.setattr(audit, "_pillow_brotli_vendor", lambda *_args: vendor)
    audit.bind_frozen_audit(root, distribution, report, source_revision="a" * 40)
    receipt = json.loads((distribution / audit.RECEIPT_NAME).read_text(encoding="utf-8"))
    report_file = distribution / audit.REPORT_NAME

    assert receipt["format"] == "callimachus.frozen-dependency-audit.v1"
    assert receipt["revision"] == "a" * 40
    assert receipt["version"] == "2.0.1"
    assert "Callimachus.app/Contents/Info.plist" in receipt["files"]
    assert "dependency-audit.json" in receipt["files"]
    assert audit.RECEIPT_NAME not in receipt["files"]
    assert report_file.parent == distribution
    assert receipt["files"]["dependency-audit.json"]["sha256"] == audit._sha256(report_file)
    assert json.loads(report_file.read_text(encoding="utf-8"))["pillow_vendor"] == vendor

    reaudits = []
    monkeypatch.setattr(
        audit, "run_prebuild_audit",
        lambda current_root: reaudits.append(current_root) or {"status": "passed", "fresh": True},
    )

    assert audit.verify_frozen_dependencies(root, distribution) == {"status": "passed", "fresh": True}
    assert reaudits == [root]
    assert app_file.is_file()


def test_frozen_audit_rejects_changed_pillow_vendor_metadata(monkeypatch, tmp_path):
    audit = _load_audit()
    root, distribution, _app_file, _inventory, report = _frozen_fixture(audit, monkeypatch, tmp_path)
    current = [{"pillow_version": "13.0.0", "brotli_version": "1.2.1", "binary_files": []}]
    report["pillow_vendor"] = current[0]
    monkeypatch.setattr(audit, "_pillow_brotli_vendor", lambda *_args: current[0])
    audit.bind_frozen_audit(root, distribution, report, source_revision="a" * 40)
    current[0] = {"pillow_version": "13.0.0", "brotli_version": "1.2.0", "binary_files": []}
    monkeypatch.setattr(
        audit, "run_prebuild_audit",
        lambda *_args: pytest.fail("changed Pillow vendor metadata reached the advisory re-query"),
    )

    with pytest.raises(audit.DependencyAuditError, match="installed runtime differs"):
        audit.verify_frozen_dependencies(root, distribution)


def test_frozen_audit_rejects_changed_distribution_bytes(monkeypatch, tmp_path):
    audit = _load_audit()
    root, distribution, app_file, _inventory, report = _frozen_fixture(audit, monkeypatch, tmp_path)
    audit.bind_frozen_audit(root, distribution, report, source_revision="a" * 40)
    app_file.write_bytes(b"changed app bytes")
    monkeypatch.setattr(
        audit, "run_prebuild_audit",
        lambda *_args: pytest.fail("changed frozen bytes reached the advisory re-query"),
    )

    with pytest.raises(audit.DependencyAuditError, match="distribution or its source revision changed"):
        audit.verify_frozen_dependencies(root, distribution)


def test_frozen_audit_rejects_changed_source_revision(monkeypatch, tmp_path):
    audit = _load_audit()
    root, distribution, _app_file, _inventory, report = _frozen_fixture(audit, monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "_revision", lambda _root: "a" * 40)
    audit.bind_frozen_audit(root, distribution, report, source_revision="a" * 40)
    monkeypatch.setattr(audit, "_revision", lambda _root: "b" * 40)
    monkeypatch.setattr(
        audit, "run_prebuild_audit",
        lambda *_args: pytest.fail("changed source reached the advisory re-query"),
    )

    with pytest.raises(audit.DependencyAuditError, match="distribution or its source revision changed"):
        audit.verify_frozen_dependencies(root, distribution)


def test_frozen_audit_requires_receipt(tmp_path):
    audit = _load_audit()
    root = tmp_path / "source"
    root.mkdir()
    (root / "VERSION").write_text("2.0.1\n", encoding="utf-8")
    distribution = tmp_path / "dist"
    distribution.mkdir()

    with pytest.raises(audit.DependencyAuditError, match="receipt is missing or invalid"):
        audit.verify_frozen_dependencies(root, distribution)


def _create_symlink_or_skip(link: Path, target: str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"filesystem does not permit symlinks here: {exc}")


def test_frozen_manifest_serializes_safe_internal_symlink_portably(tmp_path):
    audit = _load_audit()
    distribution = tmp_path / "dist"
    nested = distribution / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.bin").write_bytes(b"payload")
    _create_symlink_or_skip(nested / "alias.bin", "payload.bin")

    manifest = audit._manifest(distribution)

    assert manifest["nested/alias.bin"] == {"kind": "symlink", "target": "payload.bin"}
    assert manifest["nested/payload.bin"]["kind"] == "file"


def test_frozen_manifest_rejects_external_symlink(tmp_path):
    audit = _load_audit()
    distribution = tmp_path / "dist"
    distribution.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    target = os.path.relpath(outside, distribution)
    _create_symlink_or_skip(distribution / "external.bin", target)

    with pytest.raises(audit.DependencyAuditError, match="unsafe link"):
        audit._manifest(distribution)
