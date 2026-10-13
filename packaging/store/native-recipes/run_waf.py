"""Use the already activated portable Microsoft compiler for the upstream Waf build."""
import os
import sys
from pathlib import Path
native_root = Path(__file__).resolve().parent
bootloader = native_root / 'python-sources' / 'pyinstaller-source' / 'pyinstaller-6.22.3' / 'bootloader'
os.chdir(bootloader)
sys.path.insert(0, str(bootloader))
from waflib.Tools import msvc
from waflib.Configure import ConfigurationContext
from waflib import Scripting

def portable_msvc(self):
    return ('msvc', '17.9', os.environ['PATH'].split(';'),
            os.environ['INCLUDE'].split(';'), os.environ['LIB'].split(';'), 'amd64')

ConfigurationContext.detect_msvc = portable_msvc
Scripting.waf_entry_point(str(bootloader), '2.0.20', str(bootloader))
