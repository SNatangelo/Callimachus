import json
from pathlib import Path
root=Path(__file__).resolve().parent
versions=json.loads((root/'original-distributions.json').read_text())
rebuilt={'brotli','cffi','charset-normalizer','cryptography','greenlet','numpy','onnxruntime','opencv-python','pillow','pyclipper','pyinstaller','pymupdf','pyside6','pyside6_addons','pyside6_essentials','pyyaml','shapely','shiboken6'}
lines=[f'{name}=={version}' for name,version in versions.items() if name.lower() not in rebuilt]
(root/'final-pure-requirements.txt').write_text('\n'.join(lines)+'\n')
print(len(lines),'original package versions retained; rebuilt native and Qt wheels excluded')
