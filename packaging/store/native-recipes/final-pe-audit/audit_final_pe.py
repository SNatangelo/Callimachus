"""Audit final frozen Store payload PE imports, toolset linkers, and runtime provenance."""
from __future__ import annotations
import ast
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path, PureWindowsPath
import xml.etree.ElementTree as ET
import pefile

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "packaging" / "build_windows_installer.py").is_file())
BUILD = ROOT / "build" / "desktop"
NATIVE = BUILD / "native-14.40"
DIST = BUILD / "dist" / "Callimachus"
FRAMEWORK = NATIVE / "framework-runtime"
FRAMEWORK_MANIFEST = NATIVE / "framework-runtime-manifest.json"
APPX = BUILD / "tools" / "vclibs-desktop-14.51" / "Microsoft.VCLibs.x64.14.00.Desktop.appx"
MSI = BUILD / "tools" / "vclibs-desktop-14.51" / "VC_CRT.Desktop.Appx.140.msi"
ANALYSIS_TOC = BUILD / "work" / "Callimachus" / "Analysis-00.toc"
FREEZE_DRIVER = NATIVE / "freeze-store-native.py"
FREEZE_LOG = NATIVE / "freeze-store-native-4.log"
RUNTIME_NAMES = (
    "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
)
RUNTIME_SET = {name.lower() for name in RUNTIME_NAMES}
CRT_FILE_RE = re.compile(r"^(?:concrt140|msvcp140.*|vcruntime140.*|ucrtbase|api-ms-win-crt-.*|msvcrt)\.dll$", re.I)
CRT_IMPORT_RE = re.compile(r"^(?:concrt140|msvcp140.*|vcruntime140.*|ucrtbase|api-ms-win-crt-.*|msvcrt)\.dll$", re.I)
PRIVATE_MSVC_ALIAS_RE = re.compile(r"^msvcp140-[^/\\]+\.dll$", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def exports(path: Path) -> set[tuple[str, int | str]]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
    found = set()
    for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []):
        found.add(("ordinal", symbol.ordinal))
        if symbol.name:
            found.add(("name", symbol.name.decode("ascii", "replace")))
    pe.close()
    return found


def imported_symbols(pe: pefile.PE, directory: str) -> list[tuple[str, tuple[str, int | str]]]:
    result = []
    for entry in getattr(pe, directory, []):
        dll = entry.dll.decode("ascii", "replace").lower()
        for symbol in entry.imports:
            value = ("ordinal", symbol.ordinal) if symbol.name is None else ("name", symbol.name.decode("ascii", "replace"))
            result.append((dll, value))
    return result


def normalize_runtime_name(name: str) -> str:
    name = name.lower()
    if name.startswith("msvcp140-"):
        return "msvcp140.dll"
    return name


def toc_runtime_inputs(toc) -> dict[str, list[dict[str, str]]]:
    found: dict[str, list[dict[str, str]]] = {name: [] for name in RUNTIME_NAMES}
    def visit(value):
        if isinstance(value, (tuple, list)):
            if len(value) == 3 and isinstance(value[0], str) and isinstance(value[1], str) and value[2] in {"BINARY", "EXTENSION"}:
                name = PureWindowsPath(value[0]).name.lower()
                if name in RUNTIME_SET:
                    source = Path(value[1]).resolve()
                    found[name].append({"dest": value[0], "type": value[2], "source": str(source), "sha256": sha256(source) if source.is_file() else "MISSING"})
            else:
                for child in value:
                    visit(child)
    visit(toc)
    return found


def system32_path(path: Path) -> bool:
    root = Path(os.environ.get("WINDIR", r"C:\Windows"), "System32").resolve()
    try:
        return os.path.commonpath([str(path.resolve()).casefold(), str(root).casefold()]) == str(root).casefold()
    except ValueError:
        return False


def msi_signature() -> dict:
    proof = json.loads((HERE / "msi-signature.json").read_text(encoding="utf-8"))
    proof["matches_input_file"] = (Path(proof.get("path", "")).resolve() == MSI.resolve() and proof.get("sha256") == sha256(MSI))
    return proof


def main() -> None:
    manifest = json.loads(FRAMEWORK_MANIFEST.read_text(encoding="utf-8"))
    msi_proof = msi_signature()
    appx_hash = sha256(APPX)
    appx_dlls = {row["appx_path"].lower(): row["sha256"].lower() for row in manifest["dlls"]}
    with zipfile.ZipFile(APPX) as archive:
        members = {name.lower(): name for name in archive.namelist()}
        signature_entry = any(Path(name).name.lower() == "appxsignature.p7x" for name in archive.namelist())
        manifest_name = next((name for name in archive.namelist() if Path(name).name.lower() == "appxmanifest.xml"), None)
        identity = {}
        if manifest_name:
            package = ET.fromstring(archive.read(manifest_name))
            node = next((element for element in package.iter() if element.tag.split("}")[-1] == "Identity"), None)
            if node is not None:
                identity = {key: node.attrib.get(key) for key in ("Name", "Version", "ProcessorArchitecture", "Publisher")}
        framework_validation = []
        target_exports = {}
        for row in manifest["dlls"]:
            name = Path(row["appx_path"]).name.lower()
            member = members.get(row["appx_path"].lower())
            if member is None:
                member = next((entry for entry in archive.namelist() if Path(entry).name.lower() == name), None)
            runtime_path = FRAMEWORK / name
            package_hash = hashlib.sha256(archive.read(member)).hexdigest() if member else "MISSING"
            file_hash = sha256(runtime_path) if runtime_path.is_file() else "MISSING"
            valid = package_hash == row["sha256"].lower() == file_hash
            framework_validation.append({"name": name, "appx_member": member, "appx_sha256": package_hash, "staged_path": str(runtime_path), "staged_sha256": file_hash, "manifest_sha256": row["sha256"].lower(), "matches_all": valid})
            if runtime_path.is_file():
                target_exports[name] = exports(runtime_path)
    driver_text = FREEZE_DRIVER.read_text(encoding="utf-8")
    toc = ast.literal_eval(ANALYSIS_TOC.read_text(encoding="utf-8"))
    runtime_inputs = toc_runtime_inputs(toc)
    expected_hashes = {Path(row["appx_path"]).name.lower(): row["sha256"].lower() for row in manifest["dlls"]}
    input_validation = {}
    for name in RUNTIME_NAMES:
        inputs = runtime_inputs[name.lower()]
        valid_inputs = [entry for entry in inputs if entry["sha256"] == expected_hashes[name.lower()] and not system32_path(Path(entry["source"]))]
        input_validation[name] = {"analysis_entries": inputs, "has_expected_hash_non_system32_input": bool(valid_inputs)}

    paths = sorted(path for path in DIST.rglob("*") if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"})
    scanned, errors, wrong_arch, newer_msvc = [], [], [], []
    missing_imports, outside_crt_imports, private_alias_imports = [], set(), []
    regular_total = delay_total = 0
    for path in paths:
        rel = path.relative_to(DIST).as_posix()
        try:
            pe = pefile.PE(str(path), fast_load=True)
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"], pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
            regular = imported_symbols(pe, "DIRECTORY_ENTRY_IMPORT")
            delay = imported_symbols(pe, "DIRECTORY_ENTRY_DELAY_IMPORT")
            regular_total += len(regular); delay_total += len(delay)
            machine = int(pe.FILE_HEADER.Machine)
            linker = (int(pe.OPTIONAL_HEADER.MajorLinkerVersion), int(pe.OPTIONAL_HEADER.MinorLinkerVersion))
            linker_text = f"{linker[0]}.{linker[1]}"
            scanned.append({"file": rel, "machine": f"0x{machine:04x}", "linker": linker_text, "regular_import_symbols": len(regular), "delay_import_symbols": len(delay)})
            if machine != 0x8664:
                wrong_arch.append({"file": rel, "machine": f"0x{machine:04x}"})
            if linker[0] == 14 and linker[1] > 39:
                newer_msvc.append({"file": rel, "linker": linker_text})
            for dll, symbol in regular + delay:
                canonical = normalize_runtime_name(dll)
                if PRIVATE_MSVC_ALIAS_RE.match(dll):
                    private_alias_imports.append({"file": rel, "dll": dll, "kind": symbol[0], "symbol": symbol[1], "delay": (dll, symbol) in delay})
                if canonical in target_exports and symbol not in target_exports[canonical]:
                    missing_imports.append({"file": rel, "dll": dll, "framework_dll": canonical, "kind": symbol[0], "symbol": symbol[1], "delay": (dll, symbol) in delay})
                elif canonical not in target_exports and CRT_IMPORT_RE.match(dll):
                    outside_crt_imports.add(dll)
            pe.close()
        except Exception as exc:
            errors.append({"file": rel, "error": str(exc)[:300]})

    bundled_crt_files = []
    private_crt_files = []
    for path in sorted(p for p in DIST.rglob("*") if p.is_file() and CRT_FILE_RE.match(p.name)):
        item = {"file": path.relative_to(DIST).as_posix(), "name": path.name, "sha256": sha256(path)}
        if path.name.lower() in RUNTIME_SET:
            item["matches_framework_hash"] = item["sha256"] == expected_hashes[path.name.lower()]
            bundled_crt_files.append(item)
        else:
            item["file_version"] = "unavailable"
            private_crt_files.append(item)

    linker_counts = {}
    for item in scanned:
        linker_counts[item["linker"]] = linker_counts.get(item["linker"], 0) + 1
    missing_runtime_inputs = [name for name, item in input_validation.items() if not item["has_expected_hash_non_system32_input"]]
    missing_framework_payload = [entry["name"] for entry in framework_validation if not entry["matches_all"]]
    log_text = FREEZE_LOG.read_text(encoding="utf-8", errors="replace") if FREEZE_LOG.is_file() else ""
    report = {
        "scope": "final Store frozen payload static PE audit; no application files modified",
        "payload": {"path": str(DIST), "native_pe_count": len(paths), "scanned_count": len(scanned), "parse_errors": errors, "wrong_architecture": wrong_arch, "linker_counts": linker_counts, "newer_than_msvc_14_39": newer_msvc, "per_file": scanned},
        "framework_provenance": {
            "appx_path": str(APPX), "appx_sha256": appx_hash, "manifest_appx_sha256": manifest["appx_sha256"].lower(), "appx_hash_matches_manifest": appx_hash == manifest["appx_sha256"].lower(), "appx_identity": identity, "appx_signature_entry_present": signature_entry,
            "manifest_path": str(FRAMEWORK_MANIFEST), "framework_runtime_path": str(FRAMEWORK), "framework_dll_count": len(manifest["dlls"]), "framework_dll_validation": framework_validation, "missing_or_mismatched_framework_dlls": missing_framework_payload,
            "signed_msi": {"path": str(MSI), **msi_proof},
        },
        "explicit_build_inputs": {"freeze_driver_path": str(FREEZE_DRIVER), "driver_has_add_binary": "--add-binary" in driver_text, "driver_hash_guards_appx_and_inputs": "appx_sha256" in driver_text and "framework-runtime-manifest.json" in driver_text and "Staged CRT differs from official framework" in driver_text, "analysis_toc_path": str(ANALYSIS_TOC), "runtime_inputs": input_validation, "missing_expected_hash_non_system32_inputs": missing_runtime_inputs, "freeze_log_path": str(FREEZE_LOG), "freeze_completion_marker": "Store native freeze completed with exact framework CRT externalization" in log_text},
        "crt_import_compatibility": {"framework_export_counts": {name: len(target_exports.get(name, set())) for name in RUNTIME_NAMES}, "regular_import_symbol_count_all_pe": regular_total, "delay_import_symbol_count_all_pe": delay_total, "missing_framework_import_count": len(missing_imports), "missing_framework_imports": missing_imports, "crt_import_names_not_in_framework": sorted(outside_crt_imports), "private_msvcp_alias_import_count": len(private_alias_imports), "private_msvcp_alias_imports": private_alias_imports},
        "crt_packaging": {"bundled_framework_crt_file_count": len(bundled_crt_files), "bundled_framework_crt_files": bundled_crt_files, "extra_private_or_system_crt_file_count": len(private_crt_files), "extra_private_or_system_crt_files": private_crt_files},
        "audit_pass": not (errors or wrong_arch or newer_msvc or missing_imports or missing_runtime_inputs or missing_framework_payload or bundled_crt_files or private_crt_files or not signature_entry or not identity or appx_hash != manifest["appx_sha256"].lower() or msi_proof.get("status") != "Valid" or not msi_proof.get("matches_input_file") or "Microsoft Corporation" not in msi_proof.get("signer", "")),
        "limits": ["Static import/export compatibility does not establish runtime behavior or Store certification.", "API-set and UCRT imports are Windows platform dependencies, not exports from the eight VCLibs framework files."],
    }
    out = HERE / "report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    summary = {"audit_pass": report["audit_pass"], "native_pe_count": len(paths), "scanned_count": len(scanned), "parse_errors": len(errors), "wrong_architecture": len(wrong_arch), "linker_counts": linker_counts, "newer_than_msvc_14_39": len(newer_msvc), "framework_dlls_verified": len(manifest["dlls"]) - len(missing_framework_payload), "explicit_runtime_inputs_verified": len(RUNTIME_NAMES) - len(missing_runtime_inputs), "regular_import_symbols": regular_total, "delay_import_symbols": delay_total, "missing_framework_imports": len(missing_imports), "outside_framework_crt_dll_names": sorted(outside_crt_imports), "private_alias_imports": len(private_alias_imports), "bundled_crt_files": len(bundled_crt_files), "private_crt_files": len(private_crt_files), "msi_signature": report["framework_provenance"]["signed_msi"]["status"], "appx_sha256": appx_hash}
    print(json.dumps(summary, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
