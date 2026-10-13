"""Stage only the framework runtime pinned by the checked-in build receipt."""
import hashlib
import json
from pathlib import Path
import zipfile


def stage(root: Path):
    manifest = json.loads((root / "framework-runtime-manifest.json").read_text(encoding="utf-8"))
    appx = root.parent / "tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx"
    if hashlib.sha256(appx.read_bytes()).hexdigest() != manifest["appx_sha256"]:
        raise RuntimeError("Selected framework Appx differs from the pinned receipt")
    contents = []
    with zipfile.ZipFile(appx) as archive:
        for row in manifest["dlls"]:
            content = archive.read(row["appx_path"])
            if hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise RuntimeError(f"Framework DLL differs from the pinned receipt: {row['appx_path']}")
            contents.append((Path(row["appx_path"]).name, content))
    targets = [root / "framework-runtime", root / "Python-3.13.15/PCbuild/amd64"]
    for target in (root / "final-venv/Scripts", root / "final-venv/Lib/site-packages/PySide6"):
        if target.is_dir():
            targets.append(target)
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        for leaf, content in contents:
            (target / leaf).write_bytes(content)
    print("Staged pinned framework CRT DLLs:", [leaf for leaf, _ in contents])


if __name__ == "__main__":
    stage(Path(__file__).resolve().parent)
