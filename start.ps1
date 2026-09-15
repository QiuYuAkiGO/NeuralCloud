param(
    [string]$ManifestPath,
    [string]$ProfilePath,
    [ValidateSet("demo", "live", "ollama")]
    [string]$Mode = "demo",
    [ValidateSet("auto", "none", "simulator", "mistyai")]
    [string]$EmbodimentProvider = "auto",
    [string]$MistyAIBaseUrl,
    [string]$MistyAITokenFile,
    [string]$StateRoot,
    [string]$HomeostasisStateDir,
    [string]$SharedLaunchConfigPath,
    [switch]$IgnoreSharedLaunchDefaults,
    [switch]$Development,
    [ValidateRange(10, 1800)][int]$StartupTimeoutSeconds = 180,
    [ValidateRange(1, 65535)][int]$GuiPort = 8790,
    [ValidateRange(1, 65535)][int]$RuntimePort = 8770,
    [ValidateRange(1, 65535)][int]$CognitivePort = 8771,
    [ValidateRange(1, 65535)][int]$HomeostasisPort = 8772
)

$ErrorActionPreference = "Stop"
# Load this PowerShell host's own ACL implementation (.NET/Framework differ).
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security') -ErrorAction Stop
$Root = $PSScriptRoot
if (-not $PSBoundParameters.ContainsKey('StartupTimeoutSeconds') -and $env:NC_STARTUP_TIMEOUT_SECONDS) {
    $configuredStartupTimeout = 0
    if (-not [int]::TryParse($env:NC_STARTUP_TIMEOUT_SECONDS, [ref]$configuredStartupTimeout) -or
        $configuredStartupTimeout -lt 10 -or $configuredStartupTimeout -gt 1800) {
        Write-Host 'Configuration error: startup timeout must be an integer from 10 to 1800 seconds.'
        exit 1
    }
    $StartupTimeoutSeconds = $configuredStartupTimeout
}
$sharedLaunchPathKeys = @(
    'NC_HOMEOSTASIS_STATE_DIR', 'NC_GUI_CONFIG', 'NC_QQ_SESSION_CONFIG',
    'NC_QQ_WATCHER_CONFIG', 'NC_QQ_DIRECT_CONFIG', 'NC_SHARED_MEMORY_DB', 'NC_CONTROL_TOKEN_FILE',
    'NC_SERVICE_TOKEN_FILE', 'NC_GUI_PRESETS', 'NC_RUNTIME_STATE_DIR', 'NC_BOARD_CONFIG', 'NC_VOICE_CONFIG', 'NC_WEB_CONFIG'
)
$sharedLaunchIntegerMinimums = @{
    NC_QQ_INPUT_TOKEN_BUDGET = 8192
    NC_QQ_MEMORY_RECALL_MAX_CHARACTERS = 1
}
$sharedLaunchKeys = @($sharedLaunchPathKeys) + @($sharedLaunchIntegerMinimums.Keys)
$sharedLaunchDefaults = @{}
if (-not $PSBoundParameters.ContainsKey('IgnoreSharedLaunchDefaults') -and $env:NC_IGNORE_SHARED_LAUNCH_DEFAULTS -eq '1') {
    $IgnoreSharedLaunchDefaults = $true
}
$sharedLaunchPath = Join-Path $Root 'data\shared-misty-launch.json'
if ($SharedLaunchConfigPath) {
    $sharedLaunchPath = if ([IO.Path]::IsPathRooted($SharedLaunchConfigPath)) {
        $SharedLaunchConfigPath
    } else { Join-Path $Root $SharedLaunchConfigPath }
}
if (-not $Development -and -not $IgnoreSharedLaunchDefaults -and
    ($SharedLaunchConfigPath -or (Test-Path -LiteralPath $sharedLaunchPath))) {
    try {
        $entry = Get-Item -LiteralPath $sharedLaunchPath -Force
        if ($entry.PSIsContainer -or $entry.Length -gt 65536 -or
            (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'invalid_shared_launch_file' }
        $launchConfig = Get-Content -Raw -LiteralPath $sharedLaunchPath | ConvertFrom-Json
        if ($launchConfig -isnot [pscustomobject]) { throw 'invalid_shared_launch_object' }
        foreach ($property in $launchConfig.PSObject.Properties) {
            if ($sharedLaunchIntegerMinimums.ContainsKey($property.Name)) {
                # Keep the same minimums as harness.config.Settings. Numeric
                # strings remain valid for older environment-binding files.
                if (($property.Value -isnot [string] -and $property.Value -isnot [ValueType]) -or
                    [string]$property.Value -notmatch '^[1-9][0-9]*$' -or
                    [bigint]$property.Value -lt $sharedLaunchIntegerMinimums[$property.Name]) {
                    throw 'invalid_shared_launch_integer'
                }
            } elseif ($property.Name -notin $sharedLaunchPathKeys -or $property.Value -isnot [string] -or
                -not [IO.Path]::IsPathRooted($property.Value) -or $property.Value -match '[\r\n\x00]') {
                throw 'invalid_shared_launch_binding'
            }
            $sharedLaunchDefaults[$property.Name] = [string]$property.Value
        }
    } catch {
        Write-Host 'Configuration error: shared launch configuration must contain only approved absolute path bindings and valid positive integer budgets (input tokens >= 8192; memory characters >= 1).'
        exit 1
    }
}
function Get-SharedLaunchValue([string]$Name) {
    $explicit = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($explicit) { return $explicit }
    return $sharedLaunchDefaults[$Name]
}
if ($StateRoot -and -not $Development) {
    Write-Host "Configuration error: -StateRoot creates an independent instance and requires -Development."
    exit 1
}
if (-not $Development -and -not $PSBoundParameters.ContainsKey('Mode')) {
    $modeConfigPath = Get-SharedLaunchValue 'NC_GUI_CONFIG'
    $explicitModeConfig = [bool]$modeConfigPath
    if (-not $modeConfigPath) { $modeConfigPath = Join-Path $Root 'data\gui_settings.json' }
    if ($explicitModeConfig -or (Test-Path -LiteralPath $modeConfigPath)) {
        try {
            if (-not (Test-Path -LiteralPath $modeConfigPath -PathType Leaf)) { throw 'mode_configuration_missing' }
            $modeConfig = Get-Content -Raw -LiteralPath $modeConfigPath | ConvertFrom-Json
            if ($modeConfig.mode -isnot [string] -or $modeConfig.mode -cnotin @('demo', 'live', 'ollama')) {
                throw 'invalid_saved_model_mode'
            }
            $Mode = [string]$modeConfig.mode
        } catch {
            Write-Host 'Configuration error: shared model configuration must declare demo, live, or ollama mode.'
            exit 1
        }
    }
}
$effectiveStateRoot = Join-Path $Root "data"
if ($StateRoot) {
    if ([System.IO.Path]::IsPathRooted($StateRoot)) {
        $effectiveStateRoot = [System.IO.Path]::GetFullPath($StateRoot)
    } else {
        $effectiveStateRoot = [System.IO.Path]::GetFullPath((Join-Path $Root $StateRoot))
    }
}
$effectiveHomeostasisStateDir = Join-Path $effectiveStateRoot "homeostasis"
$effectiveRuntimeStateDir = Join-Path $effectiveStateRoot "runtime"
if (-not ($Development -and $StateRoot)) {
    $runtimeStateOverride = Get-SharedLaunchValue 'NC_RUNTIME_STATE_DIR'
    if ($runtimeStateOverride) {
        if (-not [IO.Path]::IsPathRooted($runtimeStateOverride)) {
            Write-Host 'Configuration error: Runtime state path must be absolute.'
            exit 1
        }
        $effectiveRuntimeStateDir = [IO.Path]::GetFullPath($runtimeStateOverride)
        if (Test-Path -LiteralPath $effectiveRuntimeStateDir -PathType Leaf) {
            Write-Host 'Configuration error: Runtime state must be a directory.'
            exit 1
        }
    }
}
$homeostasisStateOverride = $HomeostasisStateDir
if (-not $homeostasisStateOverride -and -not ($Development -and $StateRoot)) {
    $homeostasisStateOverride = Get-SharedLaunchValue 'NC_HOMEOSTASIS_STATE_DIR'
}
if ($homeostasisStateOverride) {
    $effectiveHomeostasisStateDir = if ([System.IO.Path]::IsPathRooted($homeostasisStateOverride)) {
        [System.IO.Path]::GetFullPath($homeostasisStateOverride)
    } else { [System.IO.Path]::GetFullPath((Join-Path $Root $homeostasisStateOverride)) }
    if (Test-Path -LiteralPath $effectiveHomeostasisStateDir -PathType Leaf) {
        Write-Host "Configuration error: Homeostasis state must be a directory."
        exit 1
    }
}
$RegistryPath = Join-Path $Root ".neuralcloud.pids.json"
$LegacyPidPath = Join-Path $Root ".gui.pid"
if ($StateRoot) {
    $RegistryPath = Join-Path $effectiveStateRoot ".neuralcloud.pids.json"
    $LegacyPidPath = Join-Path $effectiveStateRoot ".gui.pid"
}
$LogDir = Join-Path $Root "logs"
if ($StateRoot) { $LogDir = Join-Path $effectiveStateRoot "logs" }
$PendingPath = Join-Path $effectiveRuntimeStateDir "pending-start.json"
$ActivePath = Join-Path $effectiveRuntimeStateDir "active-start.json"
$DefaultManifest = Join-Path $Root "presets\runtime\shared-misty-v1.json"
if ($Development) { $DefaultManifest = Join-Path $Root "presets\runtime\development-demo.json" }
$DefaultProfile = Join-Path $Root "presets\homeostasis\qq-social-drive-v1.json"
if ($Development) { $DefaultProfile = Join-Path $Root "presets\homeostasis\functional-proxy-v1.json" }
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
        state_root = $effectiveStateRoot
        homeostasis_state_dir = $effectiveHomeostasisStateDir
        runtime_state_dir = $effectiveRuntimeStateDir
        control_token_file = $controlTokenFile
        service_token_file = $serviceTokenFile
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

function Resolve-SharedTokenFile([string]$Override, [string]$Default) {
    $candidate = if ($Override) { $Override } else { $Default }
    if (-not [IO.Path]::IsPathRooted($candidate)) { throw 'shared_token_path_must_be_absolute' }
    $full = [IO.Path]::GetFullPath($candidate)
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'shared_token_path_reparse_point'
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($full)) -PathType Container)) {
        throw 'shared_token_parent_must_exist'
    }
    if ((Test-Path -LiteralPath $full) -and -not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw 'shared_token_path_must_be_file'
    }
    return $full
}

function Set-SharedTokenPrivateAcl([string]$Path) {
    $info = New-Object IO.FileInfo($Path)
    # Framework and modern .NET expose this API differently; preserve owner,
    # group and SACL while replacing only access rules before writing a secret.
    $acl = if ($PSVersionTable.PSVersion.Major -ge 6) {
        [IO.FileSystemAclExtensions]::GetAccessControl($info, [Security.AccessControl.AccessControlSections]::Access)
    } else { $info.GetAccessControl([Security.AccessControl.AccessControlSections]::Access) }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    foreach ($sid in @($userSid, $systemSid)) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')))
    }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        [IO.FileSystemAclExtensions]::SetAccessControl($info, $acl)
    } else { $info.SetAccessControl($acl) }
}

function Read-SharedToken([string]$Path) {
    if ((Get-Item -LiteralPath $Path -Force).Length -gt 65536) { throw 'shared_token_file_invalid' }
    $value = [IO.File]::ReadAllText($Path).Trim()
    if ($value.Length -lt 32 -or $value.Length -gt 4096 -or $value -notmatch '^[!-~]+$' -or
        $value -match '^(development|dummy|example|placeholder|replace|test|your|changeme|change-me)' -or
        (Test-UnsafeMistyAIToken $value)) {
        throw 'shared_token_file_invalid'
    }
    return $value
}

function Get-OrCreateSharedToken([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $value = Read-SharedToken $Path
        Set-SharedTokenPrivateAcl $Path
        return $value
    }
    $temporary = Join-Path ([IO.Path]::GetDirectoryName($Path)) ('.shared-token-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        # Empty file first: no secret exists until the explicit private DACL.
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Dispose()
        Set-SharedTokenPrivateAcl $temporary
        $bytes = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        $value = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
        [IO.File]::WriteAllText($temporary, $value, (New-Object Text.UTF8Encoding($false)))
        try { [IO.File]::Move($temporary, $Path) } catch {
            # Another authorized startup may have won creation; never replace it.
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw }
            [void](Resolve-SharedTokenFile $Path $Path)
        }
        return Read-SharedToken $Path
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
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

function Get-LocalMistyAIServiceToken([string]$BaseUrl) {
    # Only the local endpoint managed by dev.ps1 may reuse its WSL credential.
    # Explicit credentials and remote deployments keep their existing behavior.
    $origin = [uri]$BaseUrl
    if ($origin.Scheme -ne "http" -or $origin.Port -ne 8787 -or
        $origin.DnsSafeHost.ToLowerInvariant() -notin @("127.0.0.1", "localhost")) {
        return $null
    }
    try {
        $launcher = Import-Module -Name (Join-Path $Root "tools\launcher\Launcher.psm1") -PassThru -ErrorAction Stop
        $token = & $launcher {
            param([string]$LaunchRoot)
            $paths = Get-LauncherDataPaths
            $state = Read-LauncherState -Path $paths.state
            $distro = "Ubuntu-24.04"
            if ($state.neuralcloud_root -and $state.mistyai_distro -and
                [System.IO.Path]::GetFullPath([string]$state.neuralcloud_root).TrimEnd('\', '/').Equals(
                    $LaunchRoot.TrimEnd('\', '/'), [System.StringComparison]::OrdinalIgnoreCase
                )) {
                $distro = [string]$state.mistyai_distro
            }
            Read-MistyAIServiceToken -Distro $distro
        } $Root
    } catch {
        # Do not include subprocess output or credentials in configuration errors.
        Stop-Configuration (
            "MistyAI credentials could not be loaded automatically from WSL. " +
            "Prepare the local service with dev.ps1 up, or supply " +
            "-MistyAITokenFile/NC_MISTYAI_TOKEN_FILE or NC_MISTYAI_TOKEN."
        )
    }
    Write-Host "Using existing MistyAI service credentials from WSL."
    return $token
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
    if (-not $?) { throw 'port_inspection_failed' }
    foreach ($line in @($netstat)) {
        $text = "$line"
        if ($text -match "LISTENING" -and $text -match ":$Port\s") {
            $parts = @($text -split "\s+" | Where-Object { $_ })
            if ($parts[-1] -match "^\d+$") { return [int]$parts[-1] }
        }
    }
    return $null
}

function Remove-ConfirmedStaleSharedRegistry([int[]]$RequestedPorts) {
    # A dead PID alone is insufficient: retain incomplete identity data and
    # registries whose former child may still be listening under another PID.
    try {
        $file = Get-Item -LiteralPath $RegistryPath -Force -ErrorAction Stop
        if ($file.PSIsContainer -or $file.Length -gt 65536 -or
            (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
        $original = [IO.File]::ReadAllText($RegistryPath)
        $registry = $original | ConvertFrom-Json
        $expectedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        if ($registry -isnot [pscustomobject] -or
            $registry.schema_version -cne 'neuralcloud-processes-v2' -or
            $registry.root -isnot [string] -or -not [IO.Path]::IsPathRooted($registry.root) -or
            -not [IO.Path]::GetFullPath($registry.root).TrimEnd('\', '/').Equals(
                $expectedRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $registry.python_path -isnot [string] -or -not [IO.Path]::IsPathRooted($registry.python_path) -or
            $registry.services -isnot [array]) { return $false }
        $expectedModules = @{
            cognitive = 'harness.cognitive.service'
            homeostasis = 'harness.homeostasis.service'
            runtime = 'harness.runtime.service'
            gui = 'gui.runtime_server'
        }
        $entries = @($registry.services)
        if ($entries.Count -lt 1 -or $entries.Count -gt 4) { return $false }
        $names = @{}; $pids = @{}; $registeredPorts = @{}
        foreach ($entry in $entries) {
            $pidValue = 0; $portValue = 0
            if ($entry -isnot [pscustomobject] -or $entry.name -isnot [string] -or
                -not $expectedModules.ContainsKey($entry.name) -or
                $entry.module -cne $expectedModules[$entry.name] -or
                -not [int]::TryParse([string]$entry.pid, [ref]$pidValue) -or $pidValue -le 0 -or
                -not [int]::TryParse([string]$entry.port, [ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535 -or
                $entry.root -isnot [string] -or -not [IO.Path]::IsPathRooted($entry.root) -or
                -not [IO.Path]::GetFullPath($entry.root).TrimEnd('\', '/').Equals(
                    $expectedRoot, [StringComparison]::OrdinalIgnoreCase) -or
                $entry.python_path -isnot [string] -or -not [IO.Path]::IsPathRooted($entry.python_path) -or
                -not [IO.Path]::GetFullPath($entry.python_path).Equals(
                    [IO.Path]::GetFullPath($registry.python_path), [StringComparison]::OrdinalIgnoreCase) -or
                $names.ContainsKey($entry.name) -or $pids.ContainsKey($pidValue) -or
                $registeredPorts.ContainsKey($portValue)) { return $false }
            $recordedStart = [datetime]::MinValue
            if ($entry.process_start_time_utc -isnot [datetime] -and -not [datetime]::TryParse(
                    [string]$entry.process_start_time_utc, [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind, [ref]$recordedStart)) { return $false }
            $names[$entry.name] = $pidValue
            $pids[$pidValue] = $true
            $registeredPorts[$portValue] = $true
        }
        $legacyOriginal = $null
        if (Test-Path -LiteralPath $LegacyPidPath) {
            $legacy = Get-Item -LiteralPath $LegacyPidPath -Force
            if ($legacy.PSIsContainer -or $legacy.Length -gt 128 -or
                (($legacy.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
            $legacyOriginal = [IO.File]::ReadAllText($LegacyPidPath)
            if (-not $names.ContainsKey('gui') -or $legacyOriginal.Trim() -cne [string]$names.gui) { return $false }
        }
        foreach ($pidValue in $pids.Keys) {
            $processErrors = @()
            if (Get-Process -Id $pidValue -ErrorAction SilentlyContinue -ErrorVariable processErrors) { return $false }
            if (@($processErrors | Where-Object { $_.FullyQualifiedErrorId -notmatch '^NoProcessFoundForGivenId' }).Count) {
                return $false
            }
        }
        foreach ($port in @(@($registeredPorts.Keys) + $RequestedPorts | Select-Object -Unique)) {
            if ($null -ne (Get-PortOwner ([int]$port))) { return $false }
        }
        # Do not remove a registry replaced by another launcher during inspection.
        if ([IO.File]::ReadAllText($RegistryPath) -cne $original) { return $false }
        if ($null -ne $legacyOriginal) {
            if ([IO.File]::ReadAllText($LegacyPidPath) -cne $legacyOriginal) { return $false }
            Remove-Item -LiteralPath $LegacyPidPath -Force
        }
        Remove-Item -LiteralPath $RegistryPath -Force
        return $true
    } catch {
        # File/process inspection failures imply unknown ownership.
        return $false
    }
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
    function Test-StartedServicePortOwner([object]$Entry, [int]$OwnerPid) {
        if ($OwnerPid -eq [int]$Entry.pid) { return $true }
        # On Windows a venv python.exe can remain as a parent launcher while its
        # interpreter child owns the socket. Only that verified direct child belongs
        # to this startup; an unrelated owner must never be stopped.
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerPid" -ErrorAction SilentlyContinue
        $live = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue
        if (-not $owner -or -not $live -or -not $owner.CommandLine -or -not $owner.ExecutablePath -or
            -not $owner.PSObject.Properties['ParentProcessId']) { return $false }
        $parent = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
        return ($parent -and [int]$owner.ParentProcessId -eq [int]$Entry.pid -and
            $owner.Name -match '^python(w)?\.exe$' -and
            $owner.CommandLine -match ('(?:^|\s)-m\s+"?' + [regex]::Escape([string]$Entry.module) + '"?(?:\s|$)') -and
            $live.StartTime.ToUniversalTime() -ge $parent.StartTime.ToUniversalTime())
    }
    $identityMatched = $true
    $childPids = New-Object System.Collections.Generic.List[int]
    for ($index = $started.Count - 1; $index -ge 0; $index--) {
        $entry = $started[$index]
        $live = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($live) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)" -ErrorAction SilentlyContinue
            $recordedStart = [datetime]::MinValue
            $parsedStart = if ($entry.process_start_time_utc -is [datetime]) {
                $recordedStart = ([datetime]$entry.process_start_time_utc).ToUniversalTime()
                $true
            } else {
                [datetime]::TryParse([string]$entry.process_start_time_utc,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind, [ref]$recordedStart)
            }
            $startMatches = $parsedStart -and
                [math]::Abs(($live.StartTime.ToUniversalTime() - $recordedStart.ToUniversalTime()).TotalSeconds) -le 1
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
                ($null -eq $portOwner -or [int]$portOwner -eq [int]$entry.pid -or
                    (Test-StartedServicePortOwner $entry ([int]$portOwner)))
            if (-not $identityMatches) {
                $identityMatched = $false
                continue
            }
            if ($null -ne $portOwner -and [int]$portOwner -ne [int]$entry.pid) {
                if (-not (Test-StartedServicePortOwner $entry ([int]$portOwner))) {
                    $identityMatched = $false
                    continue
                }
                [void]$childPids.Add([int]$portOwner)
                Stop-Process -Id ([int]$portOwner) -Force -ErrorAction SilentlyContinue
            }
            Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue
        }
    }
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = @(@($started | ForEach-Object { [int]$_.pid }) + @($childPids) |
            Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    $occupied = @()
    if ($childPids.Count -gt 0) {
        $occupied = @($started | Where-Object { $null -ne (Get-PortOwner ([int]$_.port)) })
    }
    return $identityMatched -and $remaining.Count -eq 0 -and $occupied.Count -eq 0
}

$python = Get-PythonExe
if (-not $python) {
    Write-Host "Python was not found. Create the repository virtual environment with dev.ps1 install."
    exit 1
}

if ($Development -and -not $ManifestPath -and -not $ProfilePath -and (Test-Path -LiteralPath $PendingPath)) {
    $pending = Get-Content -Raw -LiteralPath $PendingPath | ConvertFrom-Json
    $ManifestPath = [string]$pending.manifest_path
    $ProfilePath = [string]$pending.profile_path
    $usingPending = $true
    Write-Host "Activating pending configuration: $($pending.profile_ref) / $($pending.manifest_ref)"
}
if ($Development -and -not $ManifestPath -and -not $ProfilePath -and (Test-Path -LiteralPath $ActivePath)) {
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
$controlMode = [string]$manifest.control_mode
if ($controlMode -notin @("study_runtime", "development_debug")) {
    Stop-Configuration "Manifest must declare a valid control_mode"
}
if (($controlMode -eq "development_debug") -ne [bool]$Development) {
    Stop-Configuration "Development manifests require -Development; normal startup requires study_runtime."
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
    } else {
        if (-not $effectiveMistyAIToken) {
            $effectiveMistyAIToken = Get-LocalMistyAIServiceToken $effectiveMistyAIBaseUrl
        }
        if (Test-UnsafeMistyAIToken $effectiveMistyAIToken) {
            Stop-Configuration (
                "MistyAI requires -MistyAITokenFile/NC_MISTYAI_TOKEN_FILE " +
                "or a non-placeholder NC_MISTYAI_TOKEN of at least 16 characters"
            )
        }
    }
}

$ports = @($GuiPort, $RuntimePort, $CognitivePort, $HomeostasisPort)
if (($ports | Select-Object -Unique).Count -ne $ports.Count) {
    Write-Host "All four service ports must be unique."
    exit 1
}
if ((Test-Path -LiteralPath $RegistryPath) -or (Test-Path -LiteralPath $LegacyPidPath)) {
    if (-not $Development) {
        if (-not (Remove-ConfirmedStaleSharedRegistry $ports)) {
            Write-Host "Shared NeuralCloud registry already exists; attach to its GUI or explicitly stop/restart it. No owner was stopped."
            exit 1
        }
        Write-Host "Removed a confirmed stale shared process registry; all recorded processes exited and all ports are free."
    } else {
        Write-Host "Existing development processes detected; restarting that instance."
        $stopArguments = @{ Quiet = $true }
        if ($StateRoot) { $stopArguments.StateRoot = $effectiveStateRoot }
        & (Join-Path $Root "stop.ps1") @stopArguments
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Could not stop existing NeuralCloud processes."
            exit 1
        }
    }
}

foreach ($port in $ports) {
    $owner = Get-PortOwner $port
    if ($owner) {
        Write-Host "Port $port is already owned by PID=$owner; no services were started."
        exit 1
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
New-Item -ItemType Directory -Path $effectiveRuntimeStateDir -Force | Out-Null
$defaultTokenDirectory = if ($StateRoot) { $effectiveStateRoot } else { $effectiveRuntimeStateDir }
try {
    $controlTokenFile = Resolve-SharedTokenFile (Get-SharedLaunchValue 'NC_CONTROL_TOKEN_FILE') (Join-Path $defaultTokenDirectory 'control.token')
    $serviceTokenFile = Resolve-SharedTokenFile (Get-SharedLaunchValue 'NC_SERVICE_TOKEN_FILE') (Join-Path $defaultTokenDirectory 'service.token')
    if ([string]::Equals($controlTokenFile, $serviceTokenFile, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'shared_token_paths_must_be_distinct'
    }
    $controlToken = Get-OrCreateSharedToken $controlTokenFile
    $serviceToken = Get-OrCreateSharedToken $serviceTokenFile
    if ($controlToken -eq $serviceToken) { throw 'shared_tokens_must_be_distinct' }
} catch {
    # Never print exception values, token bytes or file contents.
    Stop-Configuration 'Shared token files could not be validated or privately initialized.'
}
if ($StateRoot) {
    # The isolated acceptance directory is local private runtime state, not a report.
    $isolatedConfig = Join-Path $effectiveStateRoot "gui_settings.json"
    $sourceConfig = Join-Path $Root "data\gui_settings.json"
    if ($env:NC_GUI_CONFIG) { $sourceConfig = $env:NC_GUI_CONFIG }
    if (-not (Test-Path -LiteralPath $isolatedConfig) -and (Test-Path -LiteralPath $sourceConfig -PathType Leaf)) {
        Copy-Item -LiteralPath $sourceConfig -Destination $isolatedConfig
    }
}
$environment = @{
    NC_MODE = $Mode
    NC_CONTROL_MODE = $controlMode
    NC_ISOLATED_DEVELOPMENT = $(if ($Development -and $StateRoot) { "1" } else { "0" })
    NC_SERVICE_TOKEN = $serviceToken
    NC_CONTROL_TOKEN = $controlToken
    NC_SERVICE_TOKEN_FILE = $serviceTokenFile
    NC_CONTROL_TOKEN_FILE = $controlTokenFile
    NC_GUI_PORT = "$GuiPort"
    NC_RUNTIME_PORT = "$RuntimePort"
    NC_COGNITIVE_PORT = "$CognitivePort"
    NC_HOMEOSTASIS_PORT = "$HomeostasisPort"
    NC_RUNTIME_URL = "http://127.0.0.1:$RuntimePort"
    NC_COGNITIVE_URL = "http://127.0.0.1:$CognitivePort"
    NC_HOMEOSTASIS_URL = "http://127.0.0.1:$HomeostasisPort"
    NC_RUNTIME_MANIFEST_PATH = $ManifestPath
    NC_COGNITIVE_MANIFEST_PATH = $ManifestPath
    NC_EXPERIMENT_MANIFEST = $null
    NC_EMBODIMENT_PROVIDER = $effectiveProvider
    NC_HOMEOSTASIS_PROFILE_PATH = $ProfilePath
    NC_RUNTIME_STATE_DIR = $effectiveRuntimeStateDir
    NC_COGNITIVE_STATE_PATH = (Join-Path $effectiveStateRoot "cognitive\state.json")
    NC_HOMEOSTASIS_STATE_DIR = $effectiveHomeostasisStateDir
    # Provider credentials are injected only while the Runtime child is created.
    NC_MISTYAI_BASE_URL = $null
    NC_MISTYAI_URL = $null
    NC_MISTYAI_TOKEN = $null
    NC_MISTYAI_TOKEN_FILE = $null
    NC_MISTYAI_TIMEOUT_SECONDS = $null
    NC_MISTYAI_POLL_INTERVAL_SECONDS = $null
    NC_VOICE_CONFIG = $null
    NC_WEB_CONFIG = $null
}
foreach ($key in $sharedLaunchKeys) {
    if (-not $environment.ContainsKey($key)) {
        $binding = Get-SharedLaunchValue $key
        if ($binding) { $environment[$key] = $binding }
    }
}
if ($StateRoot) {
    $environment.NC_GUI_CONFIG = $isolatedConfig
    $environment.NC_GUI_PRESETS = Join-Path $effectiveStateRoot "presets"
    $environment.NC_TRACE_DIR = Join-Path $effectiveStateRoot "traces"
    $environment.NC_CALL_LOG_DIR = Join-Path $effectiveStateRoot "calls"
}
$runtimeMistyAIEnvironment = @{}
$voiceConfigBinding = Get-SharedLaunchValue 'NC_VOICE_CONFIG'
if ($voiceConfigBinding) {
    $runtimeMistyAIEnvironment.NC_VOICE_CONFIG = $voiceConfigBinding
}
$webConfigBinding = Get-SharedLaunchValue 'NC_WEB_CONFIG'
if ($webConfigBinding -and -not ($Development -and $StateRoot)) {
    $runtimeMistyAIEnvironment.NC_WEB_CONFIG = $webConfigBinding
}
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
    if (-not (Wait-Endpoint "http://127.0.0.1:$CognitivePort/v1/health" $serviceHeaders $StartupTimeoutSeconds { param($body) $body.healthy })) {
        throw "Cognitive service did not pass its health check"
    }
    if (-not (Wait-Endpoint "http://127.0.0.1:$HomeostasisPort/v1/health" $serviceHeaders $StartupTimeoutSeconds { param($body) $body.healthy })) {
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
        NC_VOICE_CONFIG = $null
        NC_WEB_CONFIG = $null
    }
    $runtime = Wait-Endpoint "http://127.0.0.1:$RuntimePort/v1/status" $controlHeaders $StartupTimeoutSeconds {
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
    if (-not (Wait-Endpoint "http://127.0.0.1:$GuiPort/api/status" @{} $StartupTimeoutSeconds { param($body) $null -ne $body.runtime })) {
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
