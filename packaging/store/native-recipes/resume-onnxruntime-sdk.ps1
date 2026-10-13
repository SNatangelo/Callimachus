$ErrorActionPreference='Stop'
$taskExpected=Join-Path (Get-Location) 'build/desktop/native-14.40'
$taskRLine=@(& subst) | Where-Object { $_ -match '^R:' }
if (-not $taskRLine) { & subst R: $taskExpected; if ($LASTEXITCODE -ne 0) {throw 'Cannot map workspace'}; $taskRLine=@(& subst)|Where-Object {$_ -match '^R:'} }
if (@($taskRLine).Count -ne 1) {throw 'Ambiguous R mapping'}
$taskActual=($taskRLine -split '=>',2)[1].Trim()
if (-not [string]::Equals([IO.Path]::GetFullPath($taskActual),[IO.Path]::GetFullPath($taskExpected),[StringComparison]::OrdinalIgnoreCase)) {throw 'R: must target workspace native directory'}
. build/desktop/native-14.40/activate.ps1
$env:CL='/wd4189 /external:env:INCLUDE'
$taskCMake=Join-Path $taskExpected 'bootstrap/Lib/site-packages/cmake/data/bin/cmake.exe'
& $taskCMake --build R:/onnxruntime-build/Release --config Release --target onnxruntime_pybind11_state -- -j4 2>&1 | Tee-Object build/desktop/native-14.40/onnxruntime-build-5.log
if ($LASTEXITCODE -ne 0) {exit $LASTEXITCODE}
Push-Location R:/onnxruntime-build/Release
try { & R:/bootstrap/Scripts/python.exe R:/onnxruntime/setup.py bdist_wheel 2>&1 | Tee-Object R:/onnxruntime-wheel-build.log; exit $LASTEXITCODE } finally { Pop-Location }
