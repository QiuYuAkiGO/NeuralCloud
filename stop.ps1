param(
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$RegistryPath = Join-Path $Root ".neuralcloud.pids.json"
$LegacyPidPath = Join-Path $Root ".gui.pid"

function Write-Info([string]$Message) {
    if (-not $Quiet) { Write-Host $Message }
}

function Test-ProcessIdentity([int]$PidValue, [string]$Module) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$PidValue" -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.CommandLine) { return $false }
    return $process.Name -match "^python(w)?\.exe$" -and $process.CommandLine -match [regex]::Escape($Module)
}

$entries = @()
if (Test-Path -LiteralPath $RegistryPath) {
    try {
        $registry = Get-Content -Raw -LiteralPath $RegistryPath | ConvertFrom-Json
        $entries = @($registry.services)
    } catch {
        Write-Info "The PID registry is corrupt. Unknown port owners were not stopped: $RegistryPath"
        exit 1
    }
}

if ($entries.Count -eq 0 -and (Test-Path -LiteralPath $LegacyPidPath)) {
    $raw = Get-Content -LiteralPath $LegacyPidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($raw -and "$raw" -match "^\d+$") {
        $entries = @([pscustomobject]@{ name = "legacy-gui"; pid = [int]$raw; module = "gui" })
    }
}

if ($entries.Count -eq 0) {
    Write-Info "NeuralCloud services are not running."
    Remove-Item -LiteralPath $RegistryPath, $LegacyPidPath -Force -ErrorAction SilentlyContinue
    exit 0
}

$failures = New-Object System.Collections.Generic.List[string]
$ordered = @($entries | Sort-Object { switch ($_.name) { "gui" { 0 } "runtime" { 1 } "homeostasis" { 2 } default { 3 } } })
foreach ($entry in $ordered) {
    $pidValue = [int]$entry.pid
    if (-not (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) { continue }
    if (-not (Test-ProcessIdentity $pidValue ([string]$entry.module))) {
        [void]$failures.Add("$($entry.name): PID=$pidValue command identity mismatch")
        continue
    }
    Write-Info "Stopping $($entry.name) PID=$pidValue ..."
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds(10)
$remaining = @()
do {
    $remaining = @($entries | Where-Object { Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue })
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $deadline)

foreach ($entry in $remaining) {
    [void]$failures.Add("$($entry.name): PID=$($entry.pid) did not stop")
}

if ($failures.Count -eq 0) {
    Remove-Item -LiteralPath $RegistryPath, $LegacyPidPath -Force -ErrorAction SilentlyContinue
    Write-Info "NeuralCloud four-process runtime stopped."
    exit 0
}

Write-Info "Some services could not be stopped: $($failures -join '; ')"
exit 1
