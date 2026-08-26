param(
    [string]$ManifestPath,
    [string]$ProfilePath,
    [ValidateSet("demo", "live", "ollama")]
    [string]$Mode = "demo",
    [int]$GuiPort = 8765,
    [int]$RuntimePort = 8770,
    [int]$CognitivePort = 8771,
    [int]$HomeostasisPort = 8772
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$RegistryPath = Join-Path $Root ".neuralcloud.pids.json"
$LegacyPidPath = Join-Path $Root ".gui.pid"
$LogDir = Join-Path $Root "logs"
$PendingPath = Join-Path $Root "data\runtime\pending-start.json"
$ActivePath = Join-Path $Root "data\runtime\active-start.json"
$DefaultManifest = Join-Path $Root "presets\runtime\development-demo.json"
$DefaultProfile = Join-Path $Root "presets\homeostasis\functional-proxy-v1.json"
$started = New-Object System.Collections.Generic.List[object]
$savedEnvironment = @{}
$usingPending = $false

Set-Location -LiteralPath $Root

function Get-PythonExe {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Set-LaunchEnvironment([hashtable]$Values) {
    foreach ($key in $Values.Keys) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, [string]$Values[$key], "Process")
    }
}

function Restore-LaunchEnvironment {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], "Process")
    }
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    $netstat = netstat -ano 2>$null
    foreach ($line in @($netstat)) {
        $text = "$line"
        if ($text -match "LISTENING" -and $text -match ":$Port\s") {
            $parts = @($text -split "\s+" | Where-Object { $_ })
            if ($parts[-1] -match "^\d+$") { return [int]$parts[-1] }
        }
    }
    return $null
}

function Wait-Endpoint(
    [string]$Url,
    [hashtable]$Headers,
    [int]$Seconds,
    [scriptblock]$Accept
) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec 2 -Method Get
            if (-not $Accept -or (& $Accept $response)) { return $response }
        } catch {
            Start-Sleep -Milliseconds 300
            continue
        }
        Start-Sleep -Milliseconds 300
    }
    return $null
}

function Start-NeuralService(
    [string]$Name,
    [string]$Module,
    [int]$Port,
    [string]$Python
) {
    $outLog = Join-Path $LogDir "$Name.out.log"
    $errLog = Join-Path $LogDir "$Name.err.log"
    foreach ($log in @($outLog, $errLog)) {
        if (Test-Path -LiteralPath $log) { Clear-Content -LiteralPath $log -ErrorAction SilentlyContinue }
    }
    Write-Host "Starting ${Name}: $Python -m $Module (port $Port)"
    $process = Start-Process -FilePath $Python `
        -ArgumentList "-m", $Module `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -WindowStyle Hidden `
        -PassThru
    $entry = [pscustomobject]@{
        name = $Name
        pid = $process.Id
        port = $Port
        module = $Module
        started_at = (Get-Date).ToString("o")
        out_log = $outLog
        err_log = $errLog
    }
    [void]$started.Add($entry)
    return $entry
}

function Stop-StartedProcesses {
    for ($index = $started.Count - 1; $index -ge 0; $index--) {
        $entry = $started[$index]
        if (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue) {
            Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue
        }
    }
}

if ((Test-Path -LiteralPath $RegistryPath) -or (Test-Path -LiteralPath $LegacyPidPath)) {
    Write-Host "Existing NeuralCloud processes detected; restarting them."
    & (Join-Path $Root "stop.ps1") -Quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Could not stop existing NeuralCloud processes."
        exit 1
    }
}

$python = Get-PythonExe
if (-not $python) {
    Write-Host "Python was not found. Create a virtual environment with: python -m venv .venv"
    exit 1
}

if (-not $ManifestPath -and -not $ProfilePath -and (Test-Path -LiteralPath $PendingPath)) {
    $pending = Get-Content -Raw -LiteralPath $PendingPath | ConvertFrom-Json
    $ManifestPath = [string]$pending.manifest_path
    $ProfilePath = [string]$pending.profile_path
    $usingPending = $true
    Write-Host "Activating pending configuration: $($pending.profile_ref) / $($pending.manifest_ref)"
}
if (-not $ManifestPath -and -not $ProfilePath -and (Test-Path -LiteralPath $ActivePath)) {
    $active = Get-Content -Raw -LiteralPath $ActivePath | ConvertFrom-Json
    $ManifestPath = [string]$active.manifest_path
    $ProfilePath = [string]$active.profile_path
}
if (-not $ManifestPath) { $ManifestPath = $DefaultManifest }
if (-not $ProfilePath) { $ProfilePath = $DefaultProfile }
$ManifestPath = (Resolve-Path -LiteralPath $ManifestPath).Path
$ProfilePath = (Resolve-Path -LiteralPath $ProfilePath).Path

$ports = @($GuiPort, $RuntimePort, $CognitivePort, $HomeostasisPort)
if (($ports | Select-Object -Unique).Count -ne $ports.Count) {
    Write-Host "All four service ports must be unique."
    exit 1
}
foreach ($port in $ports) {
    $owner = Get-PortOwner $port
    if ($owner) {
        Write-Host "Port $port is already owned by PID=$owner; no services were started."
        exit 1
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$serviceToken = ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
$controlToken = ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
$environment = @{
    NC_MODE = $Mode
    NC_SERVICE_TOKEN = $serviceToken
    NC_CONTROL_TOKEN = $controlToken
    NC_GUI_PORT = "$GuiPort"
    NC_RUNTIME_PORT = "$RuntimePort"
    NC_COGNITIVE_PORT = "$CognitivePort"
    NC_HOMEOSTASIS_PORT = "$HomeostasisPort"
    NC_RUNTIME_URL = "http://127.0.0.1:$RuntimePort"
    NC_COGNITIVE_URL = "http://127.0.0.1:$CognitivePort"
    NC_HOMEOSTASIS_URL = "http://127.0.0.1:$HomeostasisPort"
    NC_RUNTIME_MANIFEST_PATH = $ManifestPath
    NC_HOMEOSTASIS_PROFILE_PATH = $ProfilePath
    NC_RUNTIME_STATE_DIR = (Join-Path $Root "data\runtime")
    NC_COGNITIVE_STATE_PATH = (Join-Path $Root "data\cognitive\state.json")
    NC_HOMEOSTASIS_STATE_DIR = (Join-Path $Root "data\homeostasis")
}

try {
    Set-LaunchEnvironment $environment
    $serviceHeaders = @{ Authorization = "Bearer $serviceToken" }
    $controlHeaders = @{ Authorization = "Bearer $controlToken" }

    Start-NeuralService "cognitive" "harness.cognitive.service" $CognitivePort $python | Out-Null
    Start-NeuralService "homeostasis" "harness.homeostasis.service" $HomeostasisPort $python | Out-Null
    if (-not (Wait-Endpoint "http://127.0.0.1:$CognitivePort/v1/health" $serviceHeaders 30 { param($body) $body.healthy })) {
        throw "Cognitive service did not pass its health check"
    }
    if (-not (Wait-Endpoint "http://127.0.0.1:$HomeostasisPort/v1/health" $serviceHeaders 30 { param($body) $body.healthy })) {
        throw "Homeostasis service did not pass its health check"
    }

    Start-NeuralService "runtime" "harness.runtime.service" $RuntimePort $python | Out-Null
    $runtime = Wait-Endpoint "http://127.0.0.1:$RuntimePort/v1/status" $controlHeaders 40 {
        param($body)
        $body.lifecycle -notin @("booting", "recovering", "faulted")
    }
    if (-not $runtime) { throw "Runtime did not finish boot or recovery" }

    Start-NeuralService "gui" "gui.runtime_server" $GuiPort $python | Out-Null
    if (-not (Wait-Endpoint "http://127.0.0.1:$GuiPort/api/status" @{} 30 { param($body) $null -ne $body.runtime })) {
        throw "GUI did not pass its composite status check"
    }

    $registry = [pscustomobject]@{
        schema_version = "neuralcloud-processes-v1"
        started_at = (Get-Date).ToString("o")
        manifest_path = $ManifestPath
        profile_path = $ProfilePath
        services = @($started | ForEach-Object { $_ })
    }
    $registry | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $RegistryPath -Encoding UTF8
    [pscustomobject]@{
        manifest_path = $ManifestPath
        profile_path = $ProfilePath
        activated_at = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $ActivePath -Encoding UTF8
    if ($usingPending -and (Test-Path -LiteralPath $PendingPath)) {
        Remove-Item -LiteralPath $PendingPath -Force
    }

    Write-Host "NeuralCloud four-process runtime is ready:"
    foreach ($entry in $started) {
        Write-Host "  $($entry.name) PID=$($entry.pid) http://127.0.0.1:$($entry.port)"
    }
    Write-Host "GUI: http://127.0.0.1:$GuiPort"
    Write-Host "Manifest: $ManifestPath"
    Write-Host "Profile:  $ProfilePath"
    Write-Host "Logs: $LogDir"
    exit 0
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)"
    Stop-StartedProcesses
    if (Test-Path -LiteralPath $RegistryPath) { Remove-Item -LiteralPath $RegistryPath -Force }
    foreach ($entry in $started) {
        Write-Host "  $($entry.name) error log: $($entry.err_log)"
    }
    exit 1
} finally {
    Restore-LaunchEnvironment
}
