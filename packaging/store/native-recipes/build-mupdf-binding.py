"""Rebuild the MuPDF Python binding against the unchanged older-toolset engine."""
import os,sys,shutil,types
from pathlib import Path
root=Path('R:/')
source=root/'mupdf-1.28.0-source'
original=Path(os.environ['CALLIMACHUS_ORIGINAL_PYMUPDF'])
engine_dir=source/'platform/win32/x64/Release'
engine_dir.mkdir(parents=True,exist_ok=True)
for name in ('mupdfcpp64.lib','libmuthreads.lib'):
    shutil.copy2(original/'mupdf-devel/lib'/name,engine_dir/name)
build_dir=source/'build/shared-tesseract-release-Py_LIMITED_API_0x030d0000-x64-py3.13'
build_dir.mkdir(parents=True,exist_ok=True)
shutil.copy2(original/'mupdfcpp64.dll',build_dir/'mupdfcpp64.dll')
noop=root/'mupdf-portable-vcvars.cmd'
noop.write_text('@echo off\nexit /b 0\n')
vc=Path(os.environ['VCToolsInstallDir'])/'bin/Hostx64/x64'
vs=types.SimpleNamespace(vcvars=str(noop),cl=str(vc/'cl.exe'),link=str(vc/'link.exe'),devenv=str(root/'unused-devenv.exe'),year=2022,grade='Portable',version='14.39.33519')
sys.path.insert(0,str(source/'scripts'))
import wdev
wdev.WindowsVS=lambda *args,**kwargs:vs
import wrap.__main__ as upstream
os.chdir(source)
sys.argv=[str(source/'scripts/mupdfwrap.py'),'-d',str(build_dir),'--swig',str(root/'bootstrap/Scripts/swig.exe'),'-b','-f','023']
upstream.main()
print('MuPDF Python binding build completed:',build_dir)
