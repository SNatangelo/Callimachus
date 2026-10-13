from __future__ import annotations
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import zipfile

native = Path(os.environ["CALLIMACHUS_NATIVE_ROOT"])
source = native / "python-sources" / "pymupdf-source" / "pymupdf-1.28.0"
mupdf_source = native / "mupdf-1.28.0-source"
prebuilt = mupdf_source / "build" / "shared-tesseract-release-Py_LIMITED_API_0x030d0000-x64-py3.13"
wheel_dir = native / "build-pymupdf-wheel" / "wheels"
expected_source = os.path.normcase(os.path.normpath(str(mupdf_source)))
engine = prebuilt / "mupdfcpp64.dll"
engine_hash_before = hashlib.sha256(engine.read_bytes()).hexdigest()

sys.path.insert(0, str(native / "bootstrap" / "Lib" / "site-packages"))
sys.path.insert(0, str(source))
import pipcl
# setup.py's show_system() dumps inherited environment values; this is diagnostic only.
pipcl.show_system = lambda *args, **kwargs: None
spec = importlib.util.spec_from_file_location("pymupdf_setup_for_native_wheel", source / "setup.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load upstream setup.py")
setup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = setup
spec.loader.exec_module(setup)

def reuse_existing_binding(mupdf_local, build_type, overwrite_config, g_py_limited_api,
                           refcheck_if, trace_if, fake_nogil):
    actual_source = os.path.normcase(os.path.normpath(str(Path(mupdf_local))))
    if actual_source != expected_source:
        raise RuntimeError(f"Unexpected MuPDF source: {actual_source}")
    if build_type != "release" or not g_py_limited_api:
        raise RuntimeError(f"Unexpected ABI/build mode: {build_type=}, {g_py_limited_api=}")
    inputs = {
        "_mupdf.pyd": prebuilt / "_mupdf.pyd",
        "mupdfcpp64.dll": engine,
        "mupdfcpp64.lib": mupdf_source / "platform" / "win32" / "x64" / "Release" / "mupdfcpp64.lib",
    }
    for name, path in inputs.items():
        if not path.is_file():
            raise RuntimeError(f"Missing verified binding input {name}: {path}")
    print(f"REUSE_PREBUILT_BINDING={prebuilt}", flush=True)
    return str(prebuilt)

setup.build_mupdf_windows = reuse_existing_binding
wheel_directory = str(wheel_dir) + os.sep
wheel_name = setup.build_wheel(wheel_directory)
wheel_path = wheel_dir / wheel_name
if not wheel_path.is_file():
    raise RuntimeError(f"Upstream returned missing wheel: {wheel_path}")
if not wheel_name.startswith("pymupdf-1.28.0-cp313-abi3-win_amd64"):
    raise RuntimeError(f"Unexpected wheel name/tag: {wheel_name}")
with zipfile.ZipFile(wheel_path) as zf:
    entries = set(zf.namelist())
    required = {"pymupdf/_mupdf.pyd", "pymupdf/mupdfcpp64.dll"}
    if not required.issubset(entries):
        raise RuntimeError(f"Wheel lacks required MuPDF runtime files: {required - entries}")
    extras = sorted(name for name in entries if name.startswith("pymupdf/_extra"))
    if not extras:
        raise RuntimeError("Wheel lacks the _extra extension")
    wheel_engine = "pymupdf/mupdfcpp64.dll"
    if hashlib.sha256(zf.read(wheel_engine)).hexdigest() != engine_hash_before:
        raise RuntimeError("Wheel's engine DLL differs from the prebuilt source DLL")
if hashlib.sha256(engine.read_bytes()).hexdigest() != engine_hash_before:
    raise RuntimeError("Prebuilt engine DLL changed during wheel generation")
print(f"WHEEL={wheel_path}", flush=True)
print(f"WHEEL_SIZE={wheel_path.stat().st_size}", flush=True)
print(f"WHEEL_EXTENSIONS={extras}", flush=True)
print(f"ENGINE_SHA256={engine_hash_before}", flush=True)
