"""Run upstream OpenCV packaging with the activated portable Ninja compiler."""
import os,runpy,sys
from pathlib import Path
import skbuild
from skbuild.platform_specifics import abstract, windows

def portable_platform(self):
    abstract.CMakePlatform.__init__(self)
    self.default_generators=[abstract.CMakeGenerator('Ninja',env=dict(os.environ))]
    self._vs_help='Use the activated portable Microsoft compiler.'
windows.WindowsPlatform.__init__=portable_platform
upstream_setup=skbuild.setup

def portable_setup(*args,**kwargs):
    kwargs['cmake_args']=[x for x in kwargs.get('cmake_args',[]) if not x.startswith('-DCMAKE_GENERATOR_PLATFORM=')]
    kwargs['cmake_args'].append('-DCMAKE_GENERATOR_PLATFORM=')
    return upstream_setup(*args,**kwargs)
skbuild.setup=portable_setup
source=Path(sys.argv[1])
sys.argv=[str(source/'setup.py'),'bdist_wheel','-G','Ninja','--dist-dir',str(Path('R:/wheels'))]
runpy.run_path(str(source/'setup.py'),run_name='__main__')
