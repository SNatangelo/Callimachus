"""Freeze the Store channel with all framework CRT members externalized.

Use upstream application packaging unchanged. Extend its external runtime set
only for this build, and require exact bytes from the selected signed framework.
"""
import hashlib,sys,json,zipfile
from pathlib import Path
native=Path(__file__).resolve().parent
project=native.parents[2]
sys.path.insert(0,str(project/'packaging'))
import collect_notices
import build_desktop
framework=native/'framework-runtime'
manifest=json.loads((native/'framework-runtime-manifest.json').read_text())
expected={Path(row['appx_path']).name.lower():row['sha256'] for row in manifest['dlls']}
assert len(expected)==8
appx=native.parent/'tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx'
if hashlib.sha256(appx.read_bytes()).hexdigest()!=manifest['appx_sha256']:
    raise RuntimeError('Selected framework Appx differs from acquisition manifest')
with zipfile.ZipFile(appx) as archive:
    for row in manifest['dlls']:
        if hashlib.sha256(archive.read(row['appx_path'])).hexdigest()!=row['sha256']:
            raise RuntimeError('Framework DLL manifest differs from the selected Appx')
runtime=frozenset(expected)
collect_notices.EXTERNAL_WINDOWS_RUNTIME=runtime
build_desktop.EXTERNAL_WINDOWS_RUNTIME=runtime
upstream_arguments=build_desktop.build_arguments
def build_arguments():
    arguments=list(upstream_arguments())
    console_index=arguments.index('--hide-console')
    if console_index+1>=len(arguments) or arguments[console_index+1]!='hide-early':
        raise RuntimeError('Unexpected upstream Windows console arguments')
    arguments[console_index:console_index+2]=['--windowed']
    # Explicit genuine CRT inputs keep dependency discovery from selecting
    # a newer host System32 runtime before it visits the Qt package.
    for leaf in sorted(runtime):
        source=native/'final-venv/Lib/site-packages/PySide6'/leaf
        if hashlib.sha256(source.read_bytes()).hexdigest()!=expected[leaf]:
            raise RuntimeError(f'Staged CRT differs from official framework: {source}')
        arguments[-1:-1]=['--add-binary',f'{source};.']
    return arguments
build_desktop.build_arguments=build_arguments
upstream=build_desktop._externalize_windows_runtime

def externalize_checked(toc):
    seen=set()
    for name,source in collect_notices._bundled_binaries(toc):
        leaf=Path(name).name.lower()
        if leaf not in runtime:continue
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        if digest!=expected[leaf]:
            raise RuntimeError(f'CRT differs from the selected official framework: {source}')
        seen.add(leaf)
    # The three primary runtime libraries are mandatory; optional companions
    # are externalized only when dependency discovery actually included them.
    mandatory={'msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll'}
    if not mandatory<=seen:raise RuntimeError(f'Missing mandatory framework CRT: {mandatory-seen}')
    collect_notices.EXTERNAL_WINDOWS_RUNTIME=frozenset(seen)
    build_desktop.EXTERNAL_WINDOWS_RUNTIME=frozenset(seen)
    return upstream(toc)

build_desktop._externalize_windows_runtime=externalize_checked
build_desktop.main()
print('Store native freeze completed with exact framework CRT externalization')
