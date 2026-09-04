param(
    [string]$ManifestPath,
    [string]$ProfilePath,
    [ValidateSet("demo", "live", "ollama")]
    [string]$Mode = "demo",
    [ValidateSet("auto", "none", "simulator", "mistyai")]
    [string]$EmbodimentProvider = "auto",
    [string]$MistyAIBaseUrl,
    [string]$MistyAITokenFile,
    [ValidateRange(1, 65535)][int]$GuiPort = 8766,
    [ValidateRange(1, 65535)][int]$RuntimePort = 8770,
    [ValidateRange(1, 65535)][int]$CognitivePort = 8771,
    [ValidateRange(1, 65535)][int]$HomeostasisPort = 8772
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
$launchStartedAt = (Get-Date).ToUniversalTime().ToString("o")
$savedEnvironment = @{}
$usingPending = $false

Set-Location -LiteralPath $Root

function Get-PythonExe {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($venvPython)
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return [System.IO.Path]::GetFullPath($command.Source) }
    return $null
}

function Write-ProcessRegistry {
    $registry = [pscustomobject]@{
        schema_version = "neuralcloud-processes-v2"
        root = [System.IO.Path]::GetFullPath($Root)
        python_path = [System.IO.Path]::GetFullPath($python)
        started_at = $launchStartedAt
        manifest_path = $ManifestPath
        profile_path = $ProfilePath
        services = @($started | ForEach-Object { $_ })
    }
    $temporary = "$RegistryPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            (($registry | ConvertTo-Json -Depth 6) + [Environment]::NewLine),
            (New-Object System.Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $RegistryPath -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Set-LaunchEnvironment([hashtable]$Values) {
    foreach ($key in $Values.Keys) {
        if (-not $savedEnvironment.ContainsKey($key)) {
            $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        }
        if ($null -eq $Values[$key]) {
            Remove-Item -LiteralPath "Env:\$key" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($key, [string]$Values[$key], "Process")
        }
    }
}

function Restore-LaunchEnvironment {
    foreach ($key in $savedEnvironment.Keys) {
        if ($null -eq $savedEnvironment[$key]) {
            Remove-Item -LiteralPath "Env:\$key" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($key, [string]$savedEnvironment[$key], "Process")
        }
    }
}

function Stop-Configuration([string]$Message) {
    Write-Host "Configuration error: $Message"
    exit 1
}

function Test-UnsafeMistyAIToken([AllowEmptyString()][string]$Token) {
    $value = "$Token".Trim().ToLowerInvariant()
    if (-not $value -or $value -match "\s") { return $true }
    if ($value.Length -lt 16) { return $true }
    $known = @(
        "changeme",
        "change-me",
        "development-misty-token",
        "dummy-token",
        "example-token",
        "mistyai-token",
        "placeholder",
        "replace-me",
        "replace-with-body-service-token",
        "test-token",
        "token",
        "your-token"
    )
    if ($value -in $known) { return $true }
    foreach ($prefix in @("development-", "dummy-", "example-", "placeholder-", "replace-", "test-", "your-")) {
        if ($value.StartsWith($prefix)) { return $true }
    }
    if (($value.StartsWith('${') -and $value.EndsWith('}')) -or
        ($value.StartsWith('<') -and $value.EndsWith('>'))) { return $true }
    return $false
}

function Test-ValidMistyAIBaseUrl([AllowEmptyString()][string]$Value) {
    $parsed = $null
    if (-not [System.Uri]::TryCreate($Value, [System.UriKind]::Absolute, [ref]$parsed)) {
        return $false
    }
    if ($parsed.Scheme -notin @("http", "https") -or -not $parsed.Host) { return $false }
    if ($parsed.UserInfo -or $parsed.Query -or $parsed.Fragment) { return $false }
    if ($parsed.AbsolutePath -notin @("", "/")) { return $false }
    if ($parsed.Scheme -eq "http") {
        $hostName = $parsed.DnsSafeHost.TrimEnd('.').ToLowerInvariant()
        $isLoopback = $hostName -eq "localhost"
        if (-not $isLoopback) {
            $address = $null
            if ([System.Net.IPAddress]::TryParse($hostName, [ref]$address)) {
                $isLoopback = [System.Net.IPAddress]::IsLoopback($address)
            }
        }
        if (-not $isLoopback) { return $false }
    }
    return $true
}

function Test-RequiredEmbodimentReady(
    [object]$Status,
    [string]$ExpectedProvider,
    [string]$ExpectedBodyInstanceId,
    [string[]]$RequiredCapabilities
) {
    if ($null -eq $Status -or "$($Status.lifecycle)" -ne "RUNNING") { return $false }
    $embodiment = $Status.embodiment
    if ($null -eq $embodiment -or "$($embodiment.state)" -ne "available") { return $false }
    if ("$($embodiment.provider_id)" -ne $ExpectedProvider) { return $false }
    if ($ExpectedBodyInstanceId -and "$($embodiment.body_instance_id)" -ne $ExpectedBodyInstanceId) {
        return $false
    }
    if ($null -eq $embodiment.negotiated) { return $false }
    if (@($embodiment.negotiated.missing_required).Count -ne 0) { return $false }
    if (@($embodiment.negotiated.incompatible_required).Count -ne 0) { return $false }
    $activated = @($embodiment.activated_capabilities)
    foreach ($capability in $RequiredCapabilities) {
        if ($capability -notin $activated) { return $false }
    }
    return $true
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
        root = [System.IO.Path]::GetFullPath($Root)
        python_path = [System.IO.Path]::GetFullPath($Python)
        process_start_time_utc = $process.StartTime.ToUniversalTime().ToString("o")
        started_at = (Get-Date).ToUniversalTime().ToString("o")
        out_log = $outLog
        err_log = $errLog
    }
    [void]$started.Add($entry)
    Write-ProcessRegistry
    return $entry
}

function Stop-StartedProcesses {
    $identityMatched = $true
    for ($index = $started.Count - 1; $index -ge 0; $index--) {
        $entry = $started[$index]
        $live = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($live) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)" -ErrorAction SilentlyContinue
            $recordedStart = [datetime]::MinValue
            $startMatches = [datetime]::TryParse(
                [string]$entry.process_start_time_utc,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$recordedStart
            ) -and [math]::Abs(($live.StartTime.ToUniversalTime() - $recordedStart.ToUniversalTime()).TotalSeconds) -le 1
            $expectedPython = [System.IO.Path]::GetFullPath([string]$entry.python_path)
            $entryRoot = [System.IO.Path]::GetFullPath([string]$entry.root).TrimEnd('\', '/')
            $expectedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
            $actualPython = if ($processInfo -and $processInfo.ExecutablePath) {
                [System.IO.Path]::GetFullPath([string]$processInfo.ExecutablePath)
            } else {
                ""
            }
            $commandLine = if ($processInfo) { [string]$processInfo.CommandLine } else { "" }
            $portOwner = Get-PortOwner -Port ([int]$entry.port)
            $identityMatches = $startMatches -and $processInfo -and
                $entryRoot.Equals($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
                $expectedPython.Equals([System.IO.Path]::GetFullPath($python), [System.StringComparison]::OrdinalIgnoreCase) -and
                $processInfo.Name -match "^python(w)?\.exe$" -and
                $actualPython.Equals($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -and
                $commandLine.IndexOf($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $commandLine -match [regex]::Escape([string]$entry.module) -and
                ($null -eq $portOwner -or [int]$portOwner -eq [int]$entry.pid)
            if (-not $identityMatches) {
                $identityMatched = $false
                continue
            }
            Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue
        }
    }
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = @($started | Where-Object { Get-Process -Id $_.pid -ErrorAction SilentlyContinue })
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $identityMatched -and $remaining.Count -eq 0
}

$python = Get-PythonExe
if (-not $python) {
    Write-Host "Python was not found. Create the repository virtual environment with dev.ps1 install."
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

try {
    $manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
} catch {
    Stop-Configuration "Manifest is not valid JSON: $ManifestPath"
}
$manifestProvider = if ($manifest.embodiment.body_provider) {
    "$($manifest.embodiment.body_provider)".Trim().ToLowerInvariant()
} else {
    "none"
}
$requestedProvider = $EmbodimentProvider
if ($requestedProvider -eq "auto" -and $env:NC_EMBODIMENT_PROVIDER) {
    $requestedProvider = "$env:NC_EMBODIMENT_PROVIDER".Trim().ToLowerInvariant()
}
$effectiveProvider = if ($requestedProvider -eq "auto") { $manifestProvider } else { $requestedProvider }
if ($effectiveProvider -notin @("none", "simulator", "mistyai")) {
    Stop-Configuration "Embodiment provider must be none, simulator, or mistyai"
}
$manifestEmbodimentMode = if ($manifest.embodiment.mode) {
    "$($manifest.embodiment.mode)".Trim().ToLowerInvariant()
} else {
    "off"
}
$embodimentRequired = $manifest.embodiment.required_for_runtime -eq $true
$expectedBodyInstanceId = "$($manifest.embodiment.body_instance_id)"
$requiredCapabilities = @(
    $manifest.embodiment.capability_requirements |
        Where-Object { $_.required -eq $true } |
        ForEach-Object { "$($_.capability)" }
)
$strictEmbodimentReadiness = (
    $manifestEmbodimentMode -ne "off" -and
    $embodimentRequired
)
if ($strictEmbodimentReadiness) {
    if ($effectiveProvider -eq "none") {
        Stop-Configuration "A required embodiment manifest cannot use provider none"
    }
    if ($manifestProvider -ne "none" -and $effectiveProvider -ne $manifestProvider) {
        Stop-Configuration (
            "Required embodiment provider mismatch: manifest requires $manifestProvider, " +
            "but effective provider is $effectiveProvider"
        )
    }
}

$resolvedMistyAITokenFile = $null
$effectiveMistyAIToken = "$env:NC_MISTYAI_TOKEN"
$effectiveMistyAIBaseUrl = $MistyAIBaseUrl
if (-not $effectiveMistyAIBaseUrl) { $effectiveMistyAIBaseUrl = $env:NC_MISTYAI_BASE_URL }
if (-not $effectiveMistyAIBaseUrl) { $effectiveMistyAIBaseUrl = $env:NC_MISTYAI_URL }
if (-not $effectiveMistyAIBaseUrl) { $effectiveMistyAIBaseUrl = "http://127.0.0.1:8787" }

if ($effectiveProvider -eq "mistyai") {
    if (-not (Test-ValidMistyAIBaseUrl $effectiveMistyAIBaseUrl)) {
        Stop-Configuration (
            "MistyAI base URL must be an HTTP(S) origin without credentials or a path; " +
            "non-loopback hosts require HTTPS"
        )
    }
    $tokenFileCandidate = $MistyAITokenFile
    if (-not $tokenFileCandidate) { $tokenFileCandidate = $env:NC_MISTYAI_TOKEN_FILE }
    if ($tokenFileCandidate) {
        if (-not (Test-Path -LiteralPath $tokenFileCandidate -PathType Leaf)) {
            Stop-Configuration "MistyAI token file does not exist or is not a file: $tokenFileCandidate"
        }
        $resolvedMistyAITokenFile = (Resolve-Path -LiteralPath $tokenFileCandidate).Path
        if ((Get-Item -LiteralPath $resolvedMistyAITokenFile).Length -gt 65536) {
            Stop-Configuration "MistyAI token file exceeds 65536 bytes"
        }
        try {
            $tokenCheck = [System.IO.File]::ReadAllText($resolvedMistyAITokenFile).Trim()
        } catch {
            Stop-Configuration "MistyAI token file could not be read: $resolvedMistyAITokenFile"
        }
        if (Test-UnsafeMistyAIToken $tokenCheck) {
            $tokenCheck = $null
            Stop-Configuration (
                "MistyAI token file must contain one non-placeholder token of at least 16 characters"
            )
        }
        $tokenCheck = $null
    } elseif (Test-UnsafeMistyAIToken "$env:NC_MISTYAI_TOKEN") {
        Stop-Configuration (
            "MistyAI requires -MistyAITokenFile/NC_MISTYAI_TOKEN_FILE " +
            "or a non-placeholder NC_MISTYAI_TOKEN of at least 16 characters"
        )
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
    NC_EMBODIMENT_PROVIDER = $effectiveProvider
    NC_HOMEOSTASIS_PROFILE_PATH = $ProfilePath
    NC_RUNTIME_STATE_DIR = (Join-Path $Root "data\runtime")
    NC_COGNITIVE_STATE_PATH = (Join-Path $Root "data\cognitive\state.json")
    NC_HOMEOSTASIS_STATE_DIR = (Join-Path $Root "data\homeostasis")
    # Provider credentials are injected only while the Runtime child is created.
    NC_MISTYAI_BASE_URL = $null
    NC_MISTYAI_URL = $null
    NC_MISTYAI_TOKEN = $null
    NC_MISTYAI_TOKEN_FILE = $null
    NC_MISTYAI_TIMEOUT_SECONDS = $null
    NC_MISTYAI_POLL_INTERVAL_SECONDS = $null
}
$runtimeMistyAIEnvironment = @{}
if ($effectiveProvider -eq "mistyai") {
    $runtimeMistyAIEnvironment.NC_MISTYAI_BASE_URL = $effectiveMistyAIBaseUrl
    if ($resolvedMistyAITokenFile) {
        $runtimeMistyAIEnvironment.NC_MISTYAI_TOKEN_FILE = $resolvedMistyAITokenFile
    } else {
        $runtimeMistyAIEnvironment.NC_MISTYAI_TOKEN = $effectiveMistyAIToken
    }
    if ($env:NC_MISTYAI_TIMEOUT_SECONDS) {
        $runtimeMistyAIEnvironment.NC_MISTYAI_TIMEOUT_SECONDS = $env:NC_MISTYAI_TIMEOUT_SECONDS
    }
    if ($env:NC_MISTYAI_POLL_INTERVAL_SECONDS) {
        $runtimeMistyAIEnvironment.NC_MISTYAI_POLL_INTERVAL_SECONDS = $env:NC_MISTYAI_POLL_INTERVAL_SECONDS
    }
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

    if ($runtimeMistyAIEnvironment.Count -gt 0) {
        Set-LaunchEnvironment $runtimeMistyAIEnvironment
    }
    Start-NeuralService "runtime" "harness.runtime.service" $RuntimePort $python | Out-Null
    Set-LaunchEnvironment @{
        NC_MISTYAI_BASE_URL = $null
        NC_MISTYAI_URL = $null
        NC_MISTYAI_TOKEN = $null
        NC_MISTYAI_TOKEN_FILE = $null
        NC_MISTYAI_TIMEOUT_SECONDS = $null
        NC_MISTYAI_POLL_INTERVAL_SECONDS = $null
    }
    $runtime = Wait-Endpoint "http://127.0.0.1:$RuntimePort/v1/status" $controlHeaders 40 {
        param($body)
        $body.lifecycle -notin @("booting", "recovering", "faulted")
    }
    if (-not $runtime) { throw "Runtime did not finish boot or recovery" }
    if ($strictEmbodimentReadiness -and
        "$($runtime.lifecycle)" -eq "PAUSED" -and
        "$($runtime.embodiment.state)" -eq "available") {
        # Running start.ps1 is an explicit request to bring the topology up. A
        # recovered pause (including one persisted after a failed body boot)
        # therefore needs an authenticated resume before strict readiness can pass.
        Invoke-RestMethod `
            -Uri "http://127.0.0.1:$RuntimePort/v1/resume" `
            -Headers $controlHeaders `
            -Method Post `
            -ContentType "application/json" `
            -Body (@{ reason = "start.ps1 requested a running topology" } | ConvertTo-Json) `
            -TimeoutSec 5 | Out-Null
        $runtime = Wait-Endpoint "http://127.0.0.1:$RuntimePort/v1/status" $controlHeaders 10 {
            param($body)
            $body.lifecycle -notin @("booting", "recovering", "PAUSED", "faulted")
        }
        if (-not $runtime) { throw "Runtime did not resume from its recovered pause" }
    }
    if ($strictEmbodimentReadiness -and -not (
        Test-RequiredEmbodimentReady `
            $runtime $effectiveProvider $expectedBodyInstanceId $requiredCapabilities
    )) {
        $embodimentState = if ($runtime.embodiment) { "$($runtime.embodiment.state)" } else { "missing" }
        $actualBodyInstanceId = if ($runtime.embodiment) {
            "$($runtime.embodiment.body_instance_id)"
        } else {
            "missing"
        }
        throw (
            "Required embodiment runtime is not ready " +
            "(lifecycle=$($runtime.lifecycle), embodiment=$embodimentState, " +
            "body_instance_id=$actualBodyInstanceId)"
        )
    }

    Start-NeuralService "gui" "gui.runtime_server" $GuiPort $python | Out-Null
    if (-not (Wait-Endpoint "http://127.0.0.1:$GuiPort/api/status" @{} 30 { param($body) $null -ne $body.runtime })) {
        throw "GUI did not pass its composite status check"
    }

    Write-ProcessRegistry
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
    $cleanupSucceeded = Stop-StartedProcesses
    if ($cleanupSucceeded -and (Test-Path -LiteralPath $RegistryPath)) {
        Remove-Item -LiteralPath $RegistryPath -Force
    } elseif (-not $cleanupSucceeded) {
        Write-Host "  Process cleanup was incomplete; the PID registry was retained for stop.ps1."
    }
    foreach ($entry in $started) {
        Write-Host "  $($entry.name) error log: $($entry.err_log)"
    }
    exit 1
} finally {
    Restore-LaunchEnvironment
}
