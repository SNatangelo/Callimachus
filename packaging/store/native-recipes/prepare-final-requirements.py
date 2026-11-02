# packaging/store/native-recipes/prepare-final-requirements.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import re

SECURITY_OVERRIDE_PACKAGES={'certifi','pip','setuptools','wheel','brotli','pillow'}
SECURITY_MINIMUMS={'certifi':'2026.10.19','pip':'26.2.2','setuptools':'84.1.0','wheel':'0.46.2','brotli':'1.2.1','pillow':'13.0.0'}

def canonicalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()

def active_constraints(path):
    pins={}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line=raw.partition('#')[0].strip()
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line)
        if match:
            name=canonicalize(match.group(1))
            if name in SECURITY_OVERRIDE_PACKAGES:
                pins[name]=match.group(2)
    return pins

def validated_active_constraints(path):
    pins=active_constraints(path)
    invalid=[]
    for name,minimum in SECURITY_MINIMUMS.items():
        actual=pins.get(name)
        if actual is None or not re.fullmatch(r'\d+(?:\.\d+)*',actual):
            invalid.append(f'{name} is missing or has an unsupported version')
            continue
        if tuple(map(int,actual.split('.'))) < tuple(map(int,minimum.split('.'))):
            invalid.append(f'{name} {actual} is older than {minimum}')
    if invalid:
        raise ValueError('release constraints do not meet the active security minimums: '+'; '.join(invalid))
    return pins

def release_constraints_path(recipe_root):
    """Locate the checked-out project constraints in source or staged recipes."""
    return Path(recipe_root).resolve().parents[2] / 'packaging' / 'release-constraints.txt'

def render_requirements(versions, excluded, overrides):
    excluded_names={canonicalize(name) for name in excluded}
    secure_pins={
        canonicalize(name):version
        for name,version in overrides.items()
        if canonicalize(name) in SECURITY_OVERRIDE_PACKAGES
    }
    return [
        f'{name}=={secure_pins.get(canonicalize(name), version)}'
        for name,version in versions.items()
        if canonicalize(name) not in excluded_names
    ]

def main():
    root=Path(__file__).resolve().parent
    versions=json.loads((root/'original-distributions.json').read_text(encoding='utf-8'))
    rebuilt={'brotli','cffi','charset-normalizer','cryptography','greenlet','numpy','onnxruntime','opencv-python','pillow','pyclipper','pyinstaller','pymupdf','pyside6','pyside6_addons','pyside6_essentials','pyyaml','shapely','shiboken6'}
    overrides=validated_active_constraints(release_constraints_path(root))
    lines=render_requirements(versions, rebuilt, overrides)
    (root/'final-pure-requirements.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(len(lines),'original package versions retained with active release-constraint overrides; rebuilt native and Qt wheels excluded')

if __name__ == '__main__':
    main()
