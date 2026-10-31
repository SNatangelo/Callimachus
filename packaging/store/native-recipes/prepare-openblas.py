# packaging/store/native-recipes/prepare-openblas.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import scipy_openblas64 as ob
root=Path(__file__).resolve().parent
out=root/'openblas-pkgconfig'
out.mkdir(exist_ok=True)
(out/'scipy-openblas.pc').write_text(ob.get_pkg_config(),encoding='utf-8')
print(ob.get_include_dir(), ob.get_lib_dir())
print((out/'scipy-openblas.pc').read_text())
