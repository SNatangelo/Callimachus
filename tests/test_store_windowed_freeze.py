import hashlib
import json
import runpy
import shutil
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZIP_STORED, ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'packaging/store/native-recipes/freeze-store-native.py'
MANIFEST = ROOT / 'packaging/store/native-recipes/framework-runtime-manifest.json'


def _prepare_driver(tmp_path, monkeypatch, *, bad_appx_hash=False,
                    bad_appx_dll=False, bad_staged_crt=False,
                    bad_bundled_crt=False, missing_bundled=()):
    project = tmp_path / 'project'
    native = project / 'packaging/store/native-recipes'
    native.mkdir(parents=True)
    driver = native / DRIVER.name
    shutil.copyfile(DRIVER, driver)

    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    runtime_bytes = {}
    for row in manifest['dlls']:
        leaf = Path(row['appx_path']).name.lower()
        content = f'framework runtime: {leaf}'.encode('ascii')
        runtime_bytes[leaf] = content
        row['sha256'] = hashlib.sha256(content).hexdigest()

    appx = native.parent / 'tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx'
    appx.parent.mkdir(parents=True)
    with ZipFile(appx, 'w', compression=ZIP_STORED) as archive:
        for row in manifest['dlls']:
            leaf = Path(row['appx_path']).name.lower()
            content = runtime_bytes[leaf]
            if bad_appx_dll and leaf == sorted(runtime_bytes)[0]:
                content += b'changed'
            archive.writestr(row['appx_path'], content)
    manifest['appx_sha256'] = hashlib.sha256(appx.read_bytes()).hexdigest()
    if bad_appx_hash:
        manifest['appx_sha256'] = '0' * 64
    (native / 'framework-runtime-manifest.json').write_text(
        json.dumps(manifest), encoding='utf-8'
    )

    staged = native / 'final-venv/Lib/site-packages/PySide6'
    staged.mkdir(parents=True)
    for leaf, content in runtime_bytes.items():
        if bad_staged_crt and leaf == sorted(runtime_bytes)[0]:
            content += b'changed'
        (staged / leaf).write_bytes(content)

    toc = []
    toc_root = project / 'analysis-toc'
    toc_root.mkdir()
    for leaf, content in runtime_bytes.items():
        if leaf in missing_bundled:
            continue
        source = toc_root / leaf
        if bad_bundled_crt and leaf == sorted(runtime_bytes)[0]:
            content += b'changed'
        source.write_bytes(content)
        toc.append((leaf, source))

    base_arguments = [
        '--noconfirm', '--hide-console', 'hide-early', '--icon', 'Callimachus.ico', 'run.py'
    ]
    captured = {}
    collect_notices = ModuleType('collect_notices')
    collect_notices.EXTERNAL_WINDOWS_RUNTIME = frozenset()
    collect_notices._bundled_binaries = lambda entries: entries

    build_desktop = ModuleType('build_desktop')
    build_desktop.EXTERNAL_WINDOWS_RUNTIME = frozenset()
    build_desktop.build_arguments = lambda: base_arguments
    build_desktop._externalize_windows_runtime = lambda _toc: frozenset(
        build_desktop.EXTERNAL_WINDOWS_RUNTIME
    )

    def main():
        captured['arguments'] = build_desktop.build_arguments()
        captured['externalized'] = build_desktop._externalize_windows_runtime(toc)

    build_desktop.main = main
    monkeypatch.setitem(sys.modules, 'collect_notices', collect_notices)
    monkeypatch.setitem(sys.modules, 'build_desktop', build_desktop)
    monkeypatch.setattr(sys, 'path', list(sys.path))

    return {
        'driver': driver,
        'runtime_names': tuple(sorted(runtime_bytes)),
        'staged': staged,
        'base_arguments': base_arguments,
        'captured': captured,
        'collect_notices': collect_notices,
        'build_desktop': build_desktop,
    }


def _run_driver(context):
    runpy.run_path(str(context['driver']), run_name='__main__')


def test_store_driver_uses_windowed_and_preserves_common_arguments_and_crt_checks(
    tmp_path, monkeypatch
):
    context = _prepare_driver(tmp_path, monkeypatch)
    _run_driver(context)

    arguments = context['captured']['arguments']
    projected = []
    index = 0
    while index < len(arguments):
        if arguments[index] == '--add-binary':
            index += 2
        else:
            projected.append(arguments[index])
            index += 1
    expected_common = list(context['base_arguments'])
    expected_common[1:3] = ['--windowed']
    assert projected == expected_common
    assert context['base_arguments'] == [
        '--noconfirm', '--hide-console', 'hide-early', '--icon', 'Callimachus.ico', 'run.py'
    ]

    assert arguments.count('--add-binary') == 8
    actual_crt_inputs = [
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == '--add-binary'
    ]
    assert actual_crt_inputs == [f'{context["staged"] / leaf};.' for leaf in context['runtime_names']]
    assert context['captured']['externalized'] == set(context['runtime_names'])
    assert context['collect_notices'].EXTERNAL_WINDOWS_RUNTIME == set(context['runtime_names'])


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'bad_appx_hash': True}, 'Selected framework Appx differs'),
        ({'bad_appx_dll': True}, 'Framework DLL manifest differs'),
        ({'bad_staged_crt': True}, 'Staged CRT differs from official framework'),
        ({'bad_bundled_crt': True}, 'CRT differs from the selected official framework'),
    ],
)
def test_store_driver_rejects_crt_hash_mismatches(
    tmp_path, monkeypatch, overrides, message
):
    context = _prepare_driver(tmp_path, monkeypatch, **overrides)
    with pytest.raises(RuntimeError, match=message):
        _run_driver(context)


def test_store_driver_requires_all_three_primary_runtime_libraries(tmp_path, monkeypatch):
    context = _prepare_driver(
        tmp_path, monkeypatch, missing_bundled={'vcruntime140_1.dll'}
    )
    with pytest.raises(RuntimeError, match='Missing mandatory framework CRT'):
        _run_driver(context)
