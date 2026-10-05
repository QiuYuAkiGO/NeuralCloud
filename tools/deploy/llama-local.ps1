param(
    [ValidateSet('start','stop','status')][string]$Command = 'status',
    [string]$InstallRoot = "$env:LOCALAPPDATA\NeuralCloud\llama.cpp",
    [string]$ListenAddress = '0.0.0.0',
    [int]$Port = 8080,
    [int]$ContextSize = 131072,
    [int]$Parallel = 2
)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $InstallRoot 'bin\llama-server.exe'
$model = Join-Path $InstallRoot 'models\Qwen3.8-27B-Q4_K_M.gguf'
$projector = Join-Path $InstallRoot 'models\mmproj-Qwen3.8-27B-Q8_0.gguf'
$pidFile = Join-Path $InstallRoot 'server-process.json'
$keyFile = Join-Path $InstallRoot 'api-key.txt'
$owned = $null
if (Test-Path -LiteralPath $pidFile) {
    $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $candidate = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Path -eq $exe -and
        $candidate.StartTime.ToUniversalTime().Ticks -eq ([datetime]$record.started_at).ToUniversalTime().Ticks) { $owned = $candidate }
}
if ($Command -eq 'stop') {
    if ($owned) { Stop-Process -Id $owned.Id; $owned.WaitForExit(10000) | Out-Null; Remove-Item -LiteralPath $pidFile; Write-Output 'Stopped owned llama-server.' }
    else { Write-Output 'No matching owned llama-server process.' }
    exit
}
if ($Command -eq 'status') {
    if ($owned) {
        $activePort = $record.port
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$activePort/health" -TimeoutSec 5
        [pscustomobject]@{ pid=$owned.Id; health=$health.status; base_url="http://127.0.0.1:$activePort/v1" }
    } else { Write-Output 'Stopped' }
    exit
}
if ($owned) { Write-Output "Already running: $($owned.Id)"; exit }
if (!(Test-Path -LiteralPath $exe)) { throw "Missing runtime: $exe" }
$verification = Get-Content -LiteralPath "$model.ranges.json" -Raw | ConvertFrom-Json
if ($verification.verified_sha256 -ne 'c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747' -or
    (Get-Item -LiteralPath $model).Length -ne 18973870528) { throw 'Model download is not verified.' }
if (Test-Path -LiteralPath $projector) {
    $projectorVerification = Get-Content -LiteralPath "$projector.ranges.json" -Raw | ConvertFrom-Json
    if ($projectorVerification.verified_sha256 -ne '2e968a6af97ce35d8971890b257b9b7edabf20ad91450501fa53162a19ee33eb' -or
        (Get-Item -LiteralPath $projector).Length -ne 629247008) { throw 'Vision projector download is not verified.' }
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { throw "Port $Port is occupied." }
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'logs') | Out-Null
if (!(Test-Path -LiteralPath $keyFile)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    [IO.File]::WriteAllText($keyFile, 'nc-' + ([BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant()))
}
$templateSource = Join-Path $PSScriptRoot 'templates\qwen38-neuralcloud.jinja'
if (!(Test-Path -LiteralPath $templateSource)) { throw 'NeuralCloud-compatible Qwen template is missing.' }
$template = Join-Path $InstallRoot 'templates\qwen38-neuralcloud.jinja'
New-Item -ItemType Directory -Force -Path (Split-Path $template) | Out-Null
Copy-Item -LiteralPath $templateSource -Destination $template -Force
$serverArgs = @('-m', ('"' + $model + '"'), '--alias', 'qwen3.8-27b',
    '--host', $ListenAddress, '--port', $Port, '--api-key-file', ('"' + $keyFile + '"'),
    '--n-gpu-layers', 'all', '--flash-attn', 'on', '--ctx-size', $ContextSize,
    '--parallel', $Parallel, '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0',
    '--batch-size', '2048', '--ubatch-size', '512', '--threads', '16',
    '--n-predict', '4096',
    '--jinja', '--chat-template-file', ('"' + $template + '"'),
    '--reasoning', 'off', '--no-reasoning-preserve', '--reasoning-effort', 'low', '--reasoning-budget', '512',
    '--temp', '0.7', '--top-p', '0.8', '--top-k', '20', '--min-p', '0',
    '--no-context-shift', '--metrics', '--log-file', ('"' + (Join-Path $InstallRoot 'logs\server.log') + '"'))
if (Test-Path -LiteralPath $projector) { $serverArgs += @('--mmproj', ('"' + $projector + '"')) }
# WMI starts outside the caller's terminal job, so terminal cleanup cannot kill the server.
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = ('"' + $exe + '" ' + ($serverArgs -join ' '))
    CurrentDirectory = (Split-Path $exe)
    ProcessStartupInformation = $startup
}
if ($created.ReturnValue -ne 0) { throw "Detached server startup failed: $($created.ReturnValue)" }
$process = Get-Process -Id $created.ProcessId
@{ pid=$process.Id; started_at=$process.StartTime.ToUniversalTime().ToString('o'); port=$Port } |
    ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
Write-Output "Started llama-server PID $($process.Id); logs: $InstallRoot\logs"
