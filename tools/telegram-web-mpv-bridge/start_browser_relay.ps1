# Used by ush://BrowserRelay; paths are relative to this file, not the browser.
[CmdletBinding()]
param(
    [switch]$Source,
    [switch]$Bilibili,
    [int]$HttpPort = 0,
    [int]$WsPort = 0,
    [int]$TimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'
$bridgeRoot = $PSScriptRoot
$mpvRoot = [IO.Path]::GetFullPath((Join-Path $bridgeRoot '..\..'))
if ($HttpPort -eq 0) { $HttpPort = if ($Bilibili) { 9002 } else { 8999 } }
if ($WsPort -eq 0) { $WsPort = if ($Bilibili) { 9001 } else { 9000 } }

function Test-BridgePort {
    $bridgeClient = New-Object Net.Sockets.TcpClient
    try {
        $bridgeConnect = $bridgeClient.ConnectAsync('127.0.0.1', $HttpPort)
        if (-not $bridgeConnect.Wait(250)) { return $false }
        return $bridgeClient.Connected
    } catch { return $false } finally { $bridgeClient.Dispose() }
}

if (Test-BridgePort) { exit 0 }
$bridgeExe = Join-Path $bridgeRoot $(if ($Bilibili) { 'bilibili-live-bridge.exe' } else { 'browser-media-bridge.exe' })
$bridgePython = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$bridgeScript = Join-Path $bridgeRoot 'bridge.py'
$bridgeMpv = Join-Path $mpvRoot 'mpv.exe'
$bridgeArgs = @('--host', '127.0.0.1', '--http-port', "$HttpPort", '--ws-port', "$WsPort", '--mpv-path', "`"$bridgeMpv`"")
if (-not $Source -and (Test-Path -LiteralPath $bridgeExe -PathType Leaf)) {
    $bridgeLauncher = $bridgeExe
} elseif ((Test-Path -LiteralPath $bridgePython -PathType Leaf) -and (Test-Path -LiteralPath $bridgeScript -PathType Leaf)) {
    $bridgeLauncher = $bridgePython
    $bridgeArgs = @("`"$bridgeScript`"") + $bridgeArgs
} else {
    throw 'Browser relay is unavailable. Build the updated EXE or install requirements.txt in tools\telegram-web-mpv-bridge\.venv.'
}

$bridgeLogPrefix = if ($Bilibili) { 'bilibili-bridge' } else { 'bridge' }
$bridgeProcess = Start-Process -WindowStyle Hidden -FilePath $bridgeLauncher -ArgumentList $bridgeArgs -WorkingDirectory $bridgeRoot -RedirectStandardOutput (Join-Path $bridgeRoot "$bridgeLogPrefix.out.log") -RedirectStandardError (Join-Path $bridgeRoot "$bridgeLogPrefix.err.log") -PassThru
$bridgeDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
while ([DateTime]::UtcNow -lt $bridgeDeadline) {
    if (Test-BridgePort) { exit 0 }
    if ($bridgeProcess.HasExited) { throw "Browser relay exited during startup; see $bridgeLogPrefix.err.log." }
    Start-Sleep -Milliseconds 150
}
throw "Browser relay did not become ready; see $bridgeLogPrefix.err.log."
