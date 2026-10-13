import argparse, hashlib, json, shutil
from pathlib import Path

parser = argparse.ArgumentParser(description="Copy the already selected OmegaConf 2.0.6 package into a prepared Store build environment.")
parser.add_argument("--original-site-packages", type=Path, required=True)
parser.add_argument("--destination-site-packages", type=Path, required=True)
args = parser.parse_args()
original = args.original_site_packages.resolve()
dest = args.destination_site_packages.resolve()
dest.mkdir(parents=True, exist_ok=True)
entries = []
for name in ("omegaconf", "omegaconf-2.0.6.dist-info"):
    source = original / name
    if not source.is_dir():
        raise RuntimeError(f"Missing package source: {source}")
    if (dest / name).exists():
        raise RuntimeError(f"Refusing to overwrite existing OmegaConf: {dest / name}")
    if any(p.is_symlink() for p in source.rglob("*")):
        raise RuntimeError("Unexpected package symlink")
    shutil.copytree(source, dest / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for p in (dest / name).rglob("*"):
        if p.is_file():
            rel = p.relative_to(dest)
            value = hashlib.sha256(p.read_bytes()).hexdigest()
            if hashlib.sha256((original / rel).read_bytes()).hexdigest() != value:
                raise RuntimeError("Copy hash mismatch")
            entries.append({"file": rel.as_posix(), "sha256": value})
print(json.dumps({"version": "2.0.6", "files": entries}, indent=2))
