"""Validate the rebuilt Store payload with exact official framework CRT bytes in an isolated copy."""
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parent
WORK = BASE / ("probe-" + uuid.uuid4().hex[:10])
DIST = WORK / "dist"
TARGET = DIST / "Callimachus"
CRT = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
VARIANTS = ("concrt140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll")

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

helper = load("crt_installer_helper", ROOT / "packaging/build_windows_installer.py")
smoke = load("crt_frozen_smoke", ROOT / "packaging/smoke_desktop.py")
shutil.copytree(ROOT / "build/desktop/dist/Callimachus", TARGET)
for name in CRT:
    source = ROOT / "build/desktop/tools/vclibs-desktop-14.51/dll-probe" / name
    assert helper._file_version(source) == (14, 40, 33728, 0)
    for folder in (TARGET, TARGET / "_internal"):
        shutil.copy2(source, folder / name)
with zipfile.ZipFile(ROOT / "build/desktop/tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx") as archive:
    members = {name.lower(): name for name in archive.namelist()}
    for name in VARIANTS:
        source = WORK / name
        source.write_bytes(archive.read(members[name]))
        assert helper._file_version(source) == (14, 40, 33728, 0)
        for folder in (TARGET, TARGET / "_internal"):
            shutil.copy2(source, folder / name)
        for path in TARGET.rglob("*.dll"):
            if path.name.lower() == name:
                shutil.copy2(source, path)
    for path in TARGET.rglob("*.dll"):
        if path.name.lower().startswith("msvcp140-"):
            shutil.copy2(ROOT / "build/desktop/tools/vclibs-desktop-14.51/dll-probe/msvcp140.dll", path)
fixture = Path("tests/fixtures/parse_golden/inputs/pdf_author_year_basic.pdf")
(WORK / fixture).parent.mkdir(parents=True)
shutil.copy2(ROOT / fixture, WORK / fixture)
smoke.ROOT = WORK
smoke.DIST = DIST
temporary = WORK / "tmp"
temporary.mkdir()
os.environ.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                  QT_QPA_PLATFORM="offscreen", QT_QPA_FONTDIR="C:/Windows/Fonts",
                  PYTHONUTF8="1", PATH="")

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]
kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                     wintypes.LPWSTR, wintypes.DWORD]

def process_tree(root_pid):
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {root_pid}
    parents = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.pid] = entry.parent
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    result = {root_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in result}
        updated = result | children
        if updated == result:
            return result
        result = updated

def modules(pid):
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        return []
    try:
        buffer = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(handle, buffer, ctypes.sizeof(buffer), ctypes.byref(needed), 3):
            return []
        paths = []
        for index in range(min(2048, needed.value // ctypes.sizeof(wintypes.HMODULE))):
            value = ctypes.create_unicode_buffer(32768)
            if psapi.GetModuleFileNameExW(handle, buffer[index], value, len(value)):
                path = Path(value.value)
                if path.name.lower().startswith(("msvcp140", "vcruntime140", "concrt140")):
                    paths.append(path)
        return paths
    finally:
        kernel.CloseHandle(handle)

records = []
original_run = subprocess.run
def observed_run(command, **kwargs):
    capture = kwargs.pop("capture_output", False)
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    assert capture and not check
    observed = set()
    stopping = threading.Event()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as child:
        def watch():
            while not stopping.is_set():
                for pid in process_tree(child.pid):
                    for path in modules(pid):
                        observed.add((pid, str(path)))
                stopping.wait(0.01)
        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            raise
        finally:
            stopping.set()
            watcher.join()
        result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        records.append({"args": command[1:], "exit_code": child.returncode,
                        "loaded_runtime": [{"pid": pid, "path": path,
                                            "version": list(helper._file_version(Path(path)))}
                                           for pid, path in sorted(observed)],
                        "stderr_tail": stderr[-2500:]})
        return result

subprocess.run = observed_run
failure = None
try:
    smoke.main()
    paths = [Path(item["path"]) for record in records for item in record["loaded_runtime"]]
    assert set(CRT).issubset({path.name.lower() for path in paths}), "did not observe all core runtime DLLs"
    assert all(path.is_relative_to(TARGET) for path in paths), "runtime leaked from the system"
    assert all(helper._file_version(path) == (14, 40, 33728, 0) for path in paths)
except BaseException as error:
    failure = f"{type(error).__name__}: {error}"
finally:
    subprocess.run = original_run
    report = {"work": str(WORK), "tests": records, "failure": failure}
    (WORK / "runtime-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Report:", WORK / "runtime-report.json", flush=True)
if failure:
    raise SystemExit(failure)
print("All six frozen checks passed with observed CRT 14.40.33728 from isolated copy", flush=True)
