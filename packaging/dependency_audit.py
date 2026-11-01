# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail closed before building or repackaging an affected Python runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import venv

from packaging.version import InvalidVersion, Version

AUDITOR_VERSION = "2.10.1"
REPORT_NAME = "dependency-audit.json"
RECEIPT_NAME = "_dependency-audit-receipt.json"


class DependencyAuditError(RuntimeError):
    """A dependency scan or a frozen-distribution check could not pass."""


def _canonical(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise DependencyAuditError("invalid distribution name in audit input")
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(value: str) -> Version:
    try:
        return Version(value)
    except (InvalidVersion, TypeError) as exc:
        raise DependencyAuditError("invalid distribution version in audit input") from exc


def installed_inventory() -> dict[str, str]:
    inventory: dict[str, str] = {}
    # setuptools can add its private vendor directory to sys.path on import.
    # Its nested metadata is not a second installation of those distributions.
    # Keep normal sys.path/.pth installations, excluding only these private roots.
    search_paths = []
    for entry in sys.path:
        location = Path(entry or os.curdir).resolve()
        if location.name == "_vendor" and location.parent.name.lower() in {"pip", "setuptools"}:
            continue
        search_paths.append(str(location))
    for distribution in metadata.distributions(path=search_paths):
        name = _canonical(distribution.metadata.get("Name"))
        version = distribution.version
        _version(version)
        if name in inventory and _version(inventory[name]) != _version(version):
            raise DependencyAuditError(f"ambiguous installed versions for {name}")
        inventory[name] = version
    if not inventory:
        raise DependencyAuditError("the installed dependency inventory is empty")
    return dict(sorted(inventory.items()))



def _active_exact_pin(root: Path, package: str) -> str:
    constraints = root / "packaging" / "release-constraints.txt"
    matches = []
    for raw in constraints.read_text(encoding="utf-8").splitlines():
        line = raw.partition("#")[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+_-]+)", line)
        if match and _canonical(match.group(1)) == _canonical(package):
            matches.append(match.group(2))
    if len(matches) != 1:
        raise DependencyAuditError(f"release constraints need one unconditional {package}== pin")
    _version(matches[0])
    return matches[0]


def _source_security_inputs(root: Path) -> tuple[dict, dict, dict, dict, str]:
    path = root / "packaging" / "store" / "native-recipes" / "security-source-inputs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DependencyAuditError("verified Pillow/Brotli source inputs are missing or invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise DependencyAuditError("unsupported verified security source-input schema")
    rows = raw.get("source_overrides")
    if not isinstance(rows, list):
        raise DependencyAuditError("security source overrides are incomplete")
    by_name = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DependencyAuditError("malformed security source override")
        name = _canonical(row.get("name"))
        if name in by_name:
            raise DependencyAuditError("duplicate security source override")
        required = ("historical_version", "version", "filename", "url", "sha256", "size_bytes")
        if any(key not in row for key in required):
            raise DependencyAuditError(f"incomplete {name} source override")
        _version(row["historical_version"])
        _version(row["version"])
        if (not isinstance(row["filename"], str) or Path(row["filename"]).name != row["filename"]
                or not isinstance(row["url"], str)
                or not row["url"].startswith("https://files.pythonhosted.org/packages/")
                or row["url"].rsplit("/", 1)[-1] != row["filename"]
                or not isinstance(row["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or type(row["size_bytes"]) is not int or row["size_bytes"] <= 0):
            raise DependencyAuditError(f"invalid verified {name} source coordinates")
        by_name[name] = row
    if set(by_name) != {"brotli", "pillow"}:
        raise DependencyAuditError("security source overrides must contain exactly Brotli and Pillow")
    native_source = raw.get("pillow_brotli_native_source")
    expected_native_source = {
        "filename": "brotli-1.2.1-github.tar.gz",
        "version": "1.2.1",
        "url": "https://github.com/google/brotli/archive/refs/tags/v1.2.1.tar.gz",
        "sha256": "98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f",
        "size_bytes": 646598,
    }
    if native_source != expected_native_source:
        raise DependencyAuditError("verified native Pillow Brotli source archive is missing or changed")
    if native_source["version"] != by_name["brotli"]["version"]:
        raise DependencyAuditError("native Pillow Brotli source version differs from the active pin")
    pin = raw.get("pillow_brotli_pin")
    if (not isinstance(pin, dict)
            or pin.get("upstream_commit") != "293218ad059d994baf1777d882911bed6a49704a"
            or pin.get("path") != ".github/dependencies.json"
            or pin.get("filename") != "pillow-13.0.0-dependencies.json"
            or pin.get("url") != "https://raw.githubusercontent.com/python-pillow/Pillow/293218ad059d994baf1777d882911bed6a49704a/.github/dependencies.json"
            or pin.get("size_bytes") != 376
            or pin.get("from_version") != "1.2.0"
            or pin.get("to_version") != by_name["brotli"]["version"]
            or not isinstance(pin.get("original_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", pin["original_sha256"])
            or not isinstance(pin.get("patched_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", pin["patched_sha256"])):
        raise DependencyAuditError("Pillow's verified Brotli source-pin patch is incomplete")
    return by_name["brotli"], by_name["pillow"], pin, native_source, _sha256(path)


def _safe_recorded_file(site_root: Path, recorded: set[str], relative: object,
                        label: str, max_size: int) -> tuple[Path, str, int]:
    if (not isinstance(relative, str) or not relative or "\\" in relative):
        raise DependencyAuditError(f"invalid {label} path in Pillow provenance")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise DependencyAuditError(f"unsafe {label} path in Pillow provenance")
    normalized = rel.as_posix()
    if normalized not in recorded:
        raise DependencyAuditError(f"{label} is absent from Pillow RECORD")
    path = site_root.joinpath(*rel.parts)
    try:
        if path.is_symlink():
            raise DependencyAuditError(f"{label} is a symlink")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(site_root) or not resolved.is_file():
            raise DependencyAuditError(f"{label} escapes the installed Pillow directory")
        size = resolved.stat().st_size
    except OSError as exc:
        raise DependencyAuditError(f"{label} is missing from the installed Pillow directory") from exc
    if size > max_size:
        raise DependencyAuditError(f"{label} exceeds the bounded metadata size")
    return resolved, normalized, size


def _pillow_brotli_vendor(
    root: Path,
    inventory: dict[str, str],
    *,
    distribution: metadata.Distribution | None = None,
) -> dict | None:
    if "pillow" not in inventory:
        return None
    expected_brotli = _active_exact_pin(root, "Brotli")
    expected_pillow = _active_exact_pin(root, "Pillow")
    if inventory.get("brotli") != expected_brotli or inventory["pillow"] != expected_pillow:
        raise DependencyAuditError("installed Pillow/Brotli versions do not match active release constraints")
    brotli_source, pillow_source, pin, native_source, source_manifest_sha = _source_security_inputs(root)
    if brotli_source["version"] != expected_brotli or pillow_source["version"] != expected_pillow:
        raise DependencyAuditError("verified Pillow/Brotli source versions do not match active release constraints")
    if distribution is None:
        try:
            distribution = metadata.distribution("Pillow")
        except metadata.PackageNotFoundError as exc:
            raise DependencyAuditError("Pillow inventory has no installed distribution metadata") from exc
    if (_canonical(distribution.metadata.get("Name")) != "pillow"
            or distribution.version != expected_pillow
            or not distribution.files):
        raise DependencyAuditError("Pillow installed metadata is missing or mismatched")
    site_root = Path(distribution.locate_file("")).resolve()
    if not site_root.is_dir():
        raise DependencyAuditError("Pillow install root is unavailable")
    recorded = {Path(item).as_posix() for item in distribution.files}
    metadata_files = [
        item for item in recorded
        if PurePosixPath(item).name == "METADATA"
        and any(part.lower().endswith(".dist-info") for part in PurePosixPath(item).parts[:-1])
        and PurePosixPath(item).parts[-2].lower().startswith("pillow-")
    ]
    if len(metadata_files) != 1:
        raise DependencyAuditError("Pillow RECORD must identify exactly one dist-info directory")
    info_rel = PurePosixPath(metadata_files[0]).parent.as_posix()
    info_path, _, _ = _safe_recorded_file(
        site_root, recorded, metadata_files[0], "Pillow METADATA", 1024 * 1024,
    )
    if info_path.parent != site_root.joinpath(*PurePosixPath(info_rel).parts).resolve():
        raise DependencyAuditError("Pillow dist-info path is inconsistent")
    receipt_rel = f"{info_rel}/callimachus-pillow-native-security.json"
    receipt_path, _, _ = _safe_recorded_file(
        site_root, recorded, receipt_rel, "Pillow Brotli provenance", 128 * 1024,
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DependencyAuditError("Pillow Brotli provenance is unreadable") from exc
    expected_pin = {
        "commit": pin["upstream_commit"],
        "path": pin["path"],
        "upstream_sha256": pin["original_sha256"],
        "patched_sha256": pin["patched_sha256"],
        "from_version": pin["from_version"],
        "to_version": pin["to_version"],
    }
    expected_source = {
        key: brotli_source[key]
        for key in ("filename", "url", "sha256", "size_bytes")
    }
    if (not isinstance(receipt, dict)
            or receipt.get("format") != "callimachus.pillow-native-security.v1"
            or receipt.get("owner") != {"name": "Pillow", "version": expected_pillow}):
        raise DependencyAuditError("Pillow Brotli provenance has an unsupported owner or format")
    component = receipt.get("component")
    expected_native = {
        key: native_source[key]
        for key in ("filename", "url", "sha256", "size_bytes")
    }
    method = receipt.get("method")
    component_source = component.get("source") if isinstance(component, dict) else None
    source_matches_method = (
        method == "shared-library-repair" and component_source == expected_source
    ) or (
        method == "source-build" and component_source == expected_native
    )
    if (not isinstance(component, dict)
            or str(component.get("name", "")).casefold() != "brotli"
            or component.get("version") != expected_brotli
            or not source_matches_method
            or component.get("upstream_pin") != expected_pin):
        raise DependencyAuditError("Pillow Brotli method, version, or source provenance differs from verified inputs")
    license_row = receipt.get("license")
    if not isinstance(license_row, dict) or set(("path", "sha256")) - set(license_row):
        raise DependencyAuditError("Pillow Brotli license provenance is missing")
    license_path, license_rel, _ = _safe_recorded_file(
        site_root, recorded, license_row["path"], "Brotli license", 512 * 1024,
    )
    license_bytes = license_path.read_bytes()
    license_sha = _sha256(license_path)
    if (license_sha != license_row["sha256"]
            or b"brotli" not in license_bytes.lower()
            or b"mit license" not in license_bytes.lower()):
        raise DependencyAuditError("Pillow Brotli license is unverified or is not the MIT license")
    marker_versions = set()
    license_prefix = info_rel.lower() + "/"
    license_entries = [
        relative for relative in recorded
        if relative.lower().startswith(license_prefix)
        and ("/licenses/" in relative.lower()
             or PurePosixPath(relative).name.lower() in {"license", "license.txt", "copying", "copying.txt"})
    ]
    if len(license_entries) > 64:
        raise DependencyAuditError("Pillow has too many license files to validate safely")
    for relative in license_entries:
        license_entry, normalized, _ = _safe_recorded_file(
            site_root, recorded, relative, "Pillow license", 512 * 1024,
        )
        content = license_entry.read_bytes()
        for match in re.finditer(rb"(?i)brotli[-_ ]+(\d+\.\d+\.\d+)", content):
            marker_versions.add(match.group(1).decode("ascii"))
        filename_match = re.search(r"(?i)brotli[-_](\d+\.\d+\.\d+)", PurePosixPath(normalized).name)
        if filename_match:
            marker_versions.add(filename_match.group(1))
    library_paths = {}
    for relative in recorded:
        path = PurePosixPath(relative)
        base = path.name
        if not re.match(r"(?i)^libbrotli(?:common|dec|enc)", base):
            continue
        if "pillow.libs" not in {part.lower() for part in path.parts} and not (
                len(path.parts) >= 3 and path.parts[0].lower() == "pil"
                and path.parts[1].lower() == ".dylibs"):
            continue
        version_match = re.search(r"(?i)\.so\.(\d+\.\d+\.\d+)$", base)
        if not version_match:
            version_match = re.search(r"(?i)\.(\d+\.\d+\.\d+)\.dylib$", base)
        if version_match:
            marker_versions.add(version_match.group(1))
            library_paths[relative] = version_match.group(1)
    if not marker_versions or marker_versions != {expected_brotli}:
        observed = ", ".join(sorted(marker_versions)) if marker_versions else "unknown"
        raise DependencyAuditError(
            f"Pillow bundled Brotli version is {observed}; expected active pin {expected_brotli}"
        )
    binary_rows = receipt.get("binary_files")
    if not isinstance(binary_rows, list) or not binary_rows or len(binary_rows) > 64:
        raise DependencyAuditError("Pillow Brotli binary provenance is missing or excessive")
    binaries = {}
    for row in binary_rows:
        if not isinstance(row, dict) or set(("path", "sha256")) - set(row):
            raise DependencyAuditError("malformed Pillow Brotli binary provenance")
        binary_path, relative, size = _safe_recorded_file(
            site_root, recorded, row["path"], "Pillow native binary", 1024 * 1024 * 1024,
        )
        binary_name = PurePosixPath(relative).name
        suffix = PurePosixPath(relative).suffix.lower()
        if (suffix not in {".dylib", ".dll", ".pyd"}
                and not re.search(r"(?i)\.so(?:\.\d+)*$", binary_name)):
            raise DependencyAuditError("Pillow Brotli provenance names a non-binary file")
        digest = _sha256(binary_path)
        if not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])) or digest != row["sha256"]:
            raise DependencyAuditError("Pillow native binary hash differs from its provenance")
        if relative in binaries:
            raise DependencyAuditError("duplicate Pillow native binary provenance")
        binaries[relative] = {"path": relative, "sha256": digest, "size_bytes": size}
    if not set(library_paths).issubset(binaries):
        raise DependencyAuditError("Pillow provenance omits bundled Brotli shared libraries")
    for relative, version in library_paths.items():
        if version != expected_brotli:
            raise DependencyAuditError("Pillow bundles a shared Brotli library outside the active pin")
    return {
        "pillow_version": expected_pillow,
        "brotli_version": expected_brotli,
        "method": method,
        "source": component_source,
        "upstream_pin": expected_pin,
        "source_inputs_sha256": source_manifest_sha,
        "provenance_path": receipt_rel,
        "provenance_sha256": _sha256(receipt_path),
        "license_path": license_rel,
        "license_sha256": license_sha,
        "binary_files": [binaries[key] for key in sorted(binaries)],
    }



def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verify_bootstrap(root: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "_callimachus_audit_bootstrap", root / "packaging" / "bootstrap_pip.py",
    )
    if spec is None or spec.loader is None:
        raise DependencyAuditError("the verified pip bootstrap is missing")
    helper = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(helper)
        helper.verify_installed_pip()
    except Exception as exc:
        raise DependencyAuditError(
            "Unqualified pip or vendored certificates; run packaging/bootstrap_pip.py first"
        ) from exc


def _environment(work: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"):
        env.pop(key, None)
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home), "APPDATA": str(home),
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
    })
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 600):
    try:
        return subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DependencyAuditError("dependency audit command failed or timed out") from exc


def _auditor_python(root: Path, work: Path) -> Path:
    tool = work / "tool-venv"
    python = tool / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(tool)
    env = _environment(work)
    bootstrap = _run([str(python), str(root / "packaging/bootstrap_pip.py")], cwd=root, env=env)
    if bootstrap.returncode:
        raise DependencyAuditError("could not qualify the isolated auditor's pip")
    install = _run([
        str(python), "-m", "pip", "--isolated", "install", "--only-binary=:all:",
        "--index-url", "https://pypi.org/simple", "-c",
        str(root / "packaging/release-constraints.txt"), f"pip-audit=={AUDITOR_VERSION}",
    ], cwd=root, env=env)
    if install.returncode:
        raise DependencyAuditError("could not install the pinned isolated pip-audit tool")
    version = _run([
        str(python), "-I", "-c",
        "from importlib.metadata import version; print(version('pip-audit'))",
    ], cwd=root, env=env, timeout=30)
    if version.returncode or version.stdout.strip() != AUDITOR_VERSION:
        raise DependencyAuditError("the isolated audit tool version is not qualified")
    return python


def validate_report(raw: object, inventory: dict[str, str], returncode: int) -> tuple[list, list]:
    if returncode not in (0, 1) or not isinstance(raw, dict):
        raise DependencyAuditError("dependency auditor failed or returned a malformed report")
    dependencies = raw.get("dependencies")
    if not isinstance(dependencies, list):
        raise DependencyAuditError("dependency audit report has no complete package inventory")
    expected = {(name, _version(version)) for name, version in inventory.items()}
    observed = set()
    fixed, unfixed = [], []
    for item in dependencies:
        if not isinstance(item, dict) or item.get("skip_reason"):
            raise DependencyAuditError("a dependency was skipped or could not be checked")
        name = _canonical(item.get("name"))
        version = item.get("version")
        pair = (name, _version(version))
        if pair not in expected or pair in observed:
            raise DependencyAuditError("auditor package inventory differs from the requested versions")
        observed.add(pair)
        vulnerabilities = item.get("vulns")
        if not isinstance(vulnerabilities, list):
            raise DependencyAuditError("dependency vulnerabilities could not be checked")
        for finding in vulnerabilities:
            if (not isinstance(finding, dict) or not isinstance(finding.get("id"), str)
                    or not finding["id"] or not isinstance(finding.get("fix_versions"), list)):
                raise DependencyAuditError("malformed vulnerability data")
            versions = finding["fix_versions"]
            if any(not isinstance(value, str) or not value for value in versions):
                raise DependencyAuditError("malformed patched-version data")
            for value in versions:
                _version(value)
            entry = {"name": name, "version": version, "id": finding["id"], "fix_versions": versions}
            (fixed if versions else unfixed).append(entry)
    if observed != expected or bool(fixed or unfixed) != (returncode == 1):
        raise DependencyAuditError("dependency audit is incomplete or has an inconsistent exit status")
    return fixed, unfixed


def audit_inventory(root: Path, inventory: dict[str, str], report_path: Path, *, pillow_vendor: dict | None = None) -> dict:
    work = root / "build/security-audit"
    work.mkdir(parents=True, exist_ok=True)
    python = _auditor_python(root, work)
    with tempfile.TemporaryDirectory(prefix="scan-", dir=work) as directory:
        scan = Path(directory)
        requirements = scan / "requirements.txt"
        requirements.write_text("".join(f"{name}=={version}\n" for name, version in sorted(inventory.items())), encoding="utf-8")
        output = scan / "report.json"
        command = [
            str(python), "-I", "-m", "pip_audit", "--disable-pip", "--no-deps",
            "--vulnerability-service", "pypi", "--format", "json", "--progress-spinner", "off",
            "--cache-dir", str(scan / "cache"), "--timeout", "30", "-r", str(requirements), "-o", str(output),
        ]
        completed = _run(command, cwd=root, env=_environment(work))
        try:
            raw = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DependencyAuditError("dependency advisory service did not return a usable report") from exc
        fixed, unfixed = validate_report(raw, inventory, completed.returncode)
    report = {
        "format": "callimachus.dependency-audit.v1", "tool_version": AUDITOR_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "inventory": inventory, "inventory_sha256": _digest(inventory),
        "pillow_vendor": pillow_vendor,
        "status": "blocked" if fixed else "passed", "fixed_findings": fixed,
        "unfixed_findings": unfixed, "dependencies": raw["dependencies"],
    }
    _write(report_path, report)
    if fixed:
        details = "; ".join(f"{item['name']} {item['version']}: {item['id']} -> {', '.join(item['fix_versions'])}" for item in fixed)
        raise DependencyAuditError(f"Build blocked: patched dependency versions are available: {details}")
    if unfixed:
        print(f"Dependency audit: {len(unfixed)} known finding(s) have no published fix; see the report.", file=sys.stderr)
    return report


def run_prebuild_audit(root: Path, *, report_path: Path | None = None) -> dict:
    root = Path(root).resolve()
    _verify_bootstrap(root)
    inventory = installed_inventory()
    pillow_vendor = _pillow_brotli_vendor(root, inventory)
    report = audit_inventory(
        root, inventory, report_path or root / "build/desktop" / REPORT_NAME,
        pillow_vendor=pillow_vendor,
    )
    if installed_inventory() != inventory:
        raise DependencyAuditError("installed dependencies changed during the security scan")
    return report


def _revision(root: Path) -> str:
    root = Path(root).resolve()

    def git_output(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-c", "core.bare=false", *arguments], cwd=root,
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DependencyAuditError("a clean source Git checkout is required") from exc
        if completed.returncode:
            raise DependencyAuditError("a clean source Git checkout is required")
        return completed.stdout.strip()

    top_level = Path(git_output("rev-parse", "--show-toplevel")).resolve()
    if top_level != root:
        raise DependencyAuditError("the source root must be the top-level Git checkout")
    status = git_output("status", "--porcelain", "--untracked-files=normal")
    if status:
        entries = status.splitlines()
        rendered = []
        used_chars = 0
        max_entries = 40
        max_line_chars = 512
        max_detail_chars = 3_600
        for entry in entries:
            safe_entry = "".join(
                char if char.isprintable() else f"\\u{ord(char):04x}"
                for char in entry
            )
            if len(safe_entry) > max_line_chars:
                safe_entry = safe_entry[:max_line_chars - 3] + "..."
            if len(rendered) >= max_entries or used_chars + len(safe_entry) + 1 > max_detail_chars:
                break
            rendered.append(safe_entry)
            used_chars += len(safe_entry) + 1
        omitted = len(entries) - len(rendered)
        if omitted:
            rendered.append(f"... {omitted} additional dirty path(s) omitted")
        raise DependencyAuditError(
            "a clean source Git checkout is required; git status --porcelain reported:\n"
            + "\n".join(rendered)
        )
    revision = git_output("rev-parse", "--verify", "HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DependencyAuditError("a complete source revision is required")
    return revision


def _manifest(distribution: Path) -> dict:
    base = distribution.resolve(strict=True)
    files = {}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base).as_posix()
        if relative == RECEIPT_NAME:
            continue
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(base):
                raise DependencyAuditError("unsafe link in the frozen distribution")
            files[relative] = {"kind": "symlink", "target": target}
        elif path.is_file():
            files[relative] = {"kind": "file", "sha256": _sha256(path), "size": path.stat().st_size}
    if not files:
        raise DependencyAuditError("the frozen distribution is empty")
    return files


def bind_frozen_audit(
    root: Path, distribution: Path, report: dict, *, source_revision: str,
) -> None:
    revision = _revision(root)
    if source_revision != revision:
        raise DependencyAuditError("the source revision changed while the frozen distribution was built")
    inventory = installed_inventory()
    if report.get("status") != "passed" or report.get("inventory") != inventory:
        raise DependencyAuditError("the frozen runtime does not match a successful dependency audit")
    if report.get("pillow_vendor") != _pillow_brotli_vendor(root, inventory):
        raise DependencyAuditError("Pillow Brotli vendor metadata changed after its audit")
    if report.get("inventory_sha256") != _digest(report["inventory"]):
        raise DependencyAuditError("the audited inventory is inconsistent")
    # These files sit beside the .app, never inside an already-signed macOS bundle.
    _write(distribution / REPORT_NAME, report)
    receipt = {
        "format": "callimachus.frozen-dependency-audit.v1", "revision": source_revision,
        "version": (root / "VERSION").read_text(encoding="utf-8").strip(),
        "inventory_sha256": report["inventory_sha256"], "files": _manifest(distribution),
    }
    _write(distribution / RECEIPT_NAME, receipt)


def verify_frozen_dependencies(root: Path, distribution: Path) -> dict:
    try:
        receipt = json.loads((distribution / RECEIPT_NAME).read_text(encoding="utf-8"))
        report = json.loads((distribution / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DependencyAuditError("rebuild this distribution: its dependency audit receipt is missing or invalid") from exc
    if (not isinstance(receipt, dict) or not isinstance(report, dict)
            or receipt.get("format") != "callimachus.frozen-dependency-audit.v1"
            or receipt.get("revision") != _revision(root)
            or receipt.get("version") != (root / "VERSION").read_text(encoding="utf-8").strip()
            or receipt.get("files") != _manifest(distribution)):
        raise DependencyAuditError("the frozen distribution or its source revision changed after its audit")
    inventory = installed_inventory()
    if (report.get("status") != "passed" or report.get("inventory") != inventory
            or report.get("inventory_sha256") != _digest(inventory)
            or receipt.get("inventory_sha256") != _digest(inventory)
            or report.get("pillow_vendor") != _pillow_brotli_vendor(root, inventory)):
        raise DependencyAuditError("the installed runtime differs from the audited frozen runtime")
    # Re-query advisories; a previous passing receipt does not authorize packaging today.
    return run_prebuild_audit(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_prebuild_audit(Path(__file__).resolve().parents[1], report_path=args.report)
    except (DependencyAuditError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Dependency security gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"Dependency security gate passed: {len(report['inventory'])} distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
