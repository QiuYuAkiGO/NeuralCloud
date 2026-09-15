param(
    [switch]$Quiet,
    [string]$StateRoot
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$RegistryPath = Join-Path $Root ".neuralcloud.pids.json"
$LegacyPidPath = Join-Path $Root ".gui.pid"
if ($StateRoot) {
    $resolvedState = if ([System.IO.Path]::IsPathRooted($StateRoot)) {
        [System.IO.Path]::GetFullPath($StateRoot)
    } else { [System.IO.Path]::GetFullPath((Join-Path $Root $StateRoot)) }
    $RegistryPath = Join-Path $resolvedState ".neuralcloud.pids.json"
    $LegacyPidPath = Join-Path $resolvedState ".gui.pid"
}

function Write-Info([string]$Message) {
    if (-not $Quiet) { Write-Host $Message }
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    foreach ($line in @(netstat -ano 2>$null)) {
        $text = "$line"
        if ($text -match "LISTENING" -and $text -match ":$Port\s") {
            $parts = @($text -split "\s+" | Where-Object { $_ })
            if ($parts[-1] -match "^\d+$") { return [int]$parts[-1] }
        }
    }
    return $null
}

function Test-PortOwnerChild([object]$Entry, [int]$OwnerPid) {
    if ($OwnerPid -eq [int]$Entry.pid) { return $true }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerPid" -ErrorAction SilentlyContinue
    $live = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue
    if (-not $owner -or -not $live -or -not $owner.CommandLine) { return $false }
    return (
        [int]$owner.ParentProcessId -eq [int]$Entry.pid -and
        $owner.Name -match "^python(w)?\.exe$" -and
        $owner.CommandLine -match [regex]::Escape([string]$Entry.module)
    )
}

function Test-ProcessIdentity([object]$Entry) {
    $expected = @{
        cognitive = "harness.cognitive.service"
        homeostasis = "harness.homeostasis.service"
        runtime = "harness.runtime.service"
        gui = "gui.runtime_server"
    }
    $name = [string]$Entry.name
    if (-not $expected.ContainsKey($name)) { return $false }
    $expectedModule = $expected[$name]
    $pidValue = 0
    $port = 0
    if (-not [int]::TryParse([string]$Entry.pid, [ref]$pidValue) -or $pidValue -le 0) { return $false }
    if (-not [int]::TryParse([string]$Entry.port, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { return $false }
    if ([string]$Entry.module -ne $expectedModule) { return $false }

    $expectedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $recordedRoot = [string]$Entry.root
    $recordedPython = [string]$Entry.python_path
    if (-not $recordedRoot -or -not $recordedPython -or
        -not $recordedRoot.TrimEnd('\', '/').Equals($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $expectedPython = [System.IO.Path]::GetFullPath($recordedPython)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
    $live = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if (-not $process -or -not $live -or -not $process.CommandLine -or -not $process.ExecutablePath) { return $false }
    $actualPython = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
    if ($process.Name -notmatch "^python(w)?\.exe$" -or
        -not $actualPython.Equals($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -or
        $process.CommandLine.IndexOf($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        $process.CommandLine -notmatch [regex]::Escape($expectedModule)) {
        return $false
    }

    $recordedStart = [datetime]::MinValue
    $rawRecordedStart = $Entry.process_start_time_utc
    $parsedStart = if ($rawRecordedStart -is [datetime]) {
        $recordedStart = ([datetime]$rawRecordedStart).ToUniversalTime()
        $true
    } else {
        [datetime]::TryParse(
            [string]$rawRecordedStart,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$recordedStart
        )
    }
    if (-not $parsedStart) { return $false }
    $actualStart = $live.StartTime.ToUniversalTime()
    if ([math]::Abs(($actualStart - $recordedStart.ToUniversalTime()).TotalSeconds) -gt 1) { return $false }

    $portOwner = Get-PortOwner -Port $port
    if ($null -ne $portOwner -and -not (Test-PortOwnerChild -Entry $Entry -OwnerPid ([int]$portOwner))) {
        return $false
    }
    return $true
}

$entries = @()
if (Test-Path -LiteralPath $RegistryPath) {
    try {
        $registry = Get-Content -Raw -LiteralPath $RegistryPath | ConvertFrom-Json
        if ([string]$registry.schema_version -ne "neuralcloud-processes-v2") {
            throw "unsupported process registry schema"
        }
        $recordedRegistryRoot = [System.IO.Path]::GetFullPath([string]$registry.root).TrimEnd('\', '/')
        $expectedRegistryRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        if (-not $recordedRegistryRoot.Equals($expectedRegistryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "process registry belongs to another checkout"
        }
        $entries = @($registry.services)
        if (@($entries | Group-Object name | Where-Object { $_.Count -ne 1 }).Count -gt 0 -or
            @($entries | Group-Object pid | Where-Object { $_.Count -ne 1 }).Count -gt 0 -or
            @($entries | Group-Object port | Where-Object { $_.Count -ne 1 }).Count -gt 0) {
            throw "process registry contains duplicate identities"
        }
    } catch {
        Write-Info "The PID registry is corrupt. Unknown port owners were not stopped: $RegistryPath"
        exit 1
    }
}

if ($entries.Count -eq 0 -and (Test-Path -LiteralPath $LegacyPidPath)) {
    $raw = Get-Content -LiteralPath $LegacyPidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($raw -and "$raw" -match "^\d+$") {
        if (Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue) {
            Write-Info "The legacy PID file lacks enough identity data to stop PID=$raw safely. Stop it manually, then remove $LegacyPidPath."
            exit 1
        }
    }
    Remove-Item -LiteralPath $LegacyPidPath -Force -ErrorAction SilentlyContinue
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
    if (-not (Test-ProcessIdentity $entry)) {
        [void]$failures.Add("$($entry.name): PID=$pidValue command identity mismatch")
        continue
    }
    Write-Info "Stopping $($entry.name) PID=$pidValue ..."
    $portOwner = Get-PortOwner -Port ([int]$entry.port)
    if ($null -ne $portOwner -and [int]$portOwner -ne $pidValue) {
        if (-not (Test-PortOwnerChild -Entry $entry -OwnerPid ([int]$portOwner))) {
            [void]$failures.Add("$($entry.name): port owner changed during shutdown")
            continue
        }
        Stop-Process -Id ([int]$portOwner) -Force -ErrorAction SilentlyContinue
    }
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
foreach ($entry in $entries) {
    $portOwner = Get-PortOwner -Port ([int]$entry.port)
    if ($null -ne $portOwner) {
        [void]$failures.Add("$($entry.name): port=$($entry.port) is still owned by PID=$portOwner")
    }
}

if ($failures.Count -eq 0) {
    Remove-Item -LiteralPath $RegistryPath, $LegacyPidPath -Force -ErrorAction SilentlyContinue
    Write-Info "NeuralCloud four-process runtime stopped."
    exit 0
}

Write-Info "Some services could not be stopped: $($failures -join '; ')"
exit 1
