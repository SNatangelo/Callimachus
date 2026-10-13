$ErrorActionPreference='Stop'
$taskExpected=Join-Path (Get-Location) 'build/desktop/native-14.40'
$taskRLine=@(& subst) | Where-Object { $_ -match '^R:' }
if (-not $taskRLine) { & subst R: $taskExpected; if ($LASTEXITCODE -ne 0) {throw 'Cannot map workspace'}; $taskRLine=@(& subst)|Where-Object {$_ -match '^R:'} }
if (@($taskRLine).Count -ne 1) {throw 'Ambiguous R mapping'}
$taskActual=($taskRLine -split '=>',2)[1].Trim()
if (-not [string]::Equals([IO.Path]::GetFullPath($taskActual),[IO.Path]::GetFullPath($taskExpected),[StringComparison]::OrdinalIgnoreCase)) {throw 'R: must target workspace native directory'}
. build/desktop/native-14.40/activate.ps1
$taskCMake=Join-Path $taskExpected 'bootstrap/Lib/site-packages/cmake/data/bin/cmake.exe'
$taskPython='R:/final-venv/Scripts/python.exe'
& $taskCMake -S R:/onnxruntime/cmake -B R:/onnxruntime-build/Release '-DCMAKE_MODULE_LINKER_FLAGS=/machine:x64 delayimp.lib' "-DPython_EXECUTABLE=$taskPython" '-DPython_INCLUDE_DIR=R:/Python-3.13.15/Include' '-DPython_LIBRARY=R:/Python-3.13.15/libs/python313.lib' '-DPython_RUNTIME_LIBRARY=R:/Python-3.13.15/PCbuild/amd64/python313.dll' 2>&1 | Tee-Object build/desktop/native-14.40/onnxruntime-configure-3.log
if ($LASTEXITCODE -ne 0) {exit $LASTEXITCODE}
& $taskCMake --build R:/onnxruntime-build/Release --config Release -- -j4 2>&1 | Tee-Object build/desktop/native-14.40/onnxruntime-build-3.log
if ($LASTEXITCODE -ne 0) {exit $LASTEXITCODE}
& R:/bootstrap/Scripts/python.exe R:/onnxruntime/tools/ci_build/build.py --build_dir R:/onnxruntime-build --config Release --build_wheel --skip_tests --skip_submodule_sync --skip_pip_install --cmake_generator Ninja 2>&1 | Tee-Object build/desktop/native-14.40/onnxruntime-wheel-build.log
exit $LASTEXITCODE
