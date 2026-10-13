[CmdletBinding()]
param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    & python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
}
if ($InstallDependencies) {
    & $buildPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the build dependencies.' }
}
& $buildPython -m PyInstaller --noconfirm --onefile --name bilibili-live-bridge --distpath $PSScriptRoot --workpath (Join-Path $PSScriptRoot 'build') --specpath (Join-Path $PSScriptRoot 'build') --hidden-import bilibili_danmaku --hidden-import playback_progress --hidden-import web_playlist --collect-all brotli (Join-Path $PSScriptRoot 'bridge.py')
if ($LASTEXITCODE -ne 0) { throw 'Could not build bilibili-live-bridge.exe.' }
Write-Output (Join-Path $PSScriptRoot 'bilibili-live-bridge.exe')
