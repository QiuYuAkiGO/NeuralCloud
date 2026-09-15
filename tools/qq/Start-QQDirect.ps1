#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect\direct.json'),
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 45,
    [switch]$CheckOnly
)

# Reuse the existing, side-effect-free Windows path/argv/interpreter helpers.
. (Join-Path $PSScriptRoot 'Start-QQWatcher.ps1') -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds
$script:QQDRepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

function Read-QQDConfiguration {
    param([string]$Path)
    $root = Resolve-QQWPrivatePath (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect') $env:LOCALAPPDATA
    $path = Resolve-QQWPrivatePath $Path $root -ExistingFile
    $raw = Read-QQWJson $path
    if ($null -eq $raw -or (Get-QQWProperty $raw 'transport' 'onebot') -cne 'onebot') { throw 'direct_configuration_invalid' }
    $paths = @{}
    foreach ($field in @('state_db', 'status_file', 'control_file', 'token_file', 'evidence_dir', 'image_cache_dir')) {
        $paths[$field] = Resolve-QQWPrivatePath ([string](Get-QQWProperty $raw $field)) $root
    }
    $all = @($path) + @($paths.Values)
    if (@($all | Sort-Object -Unique).Count -ne $all.Count) { throw 'direct_private_paths_overlap' }
    $selfId = Get-QQWProperty $raw 'self_member_id'
    if ($selfId -isnot [string] -or $selfId -notmatch '^[1-9][0-9]{0,19}$') { throw 'direct_identity_invalid' }
    $enabled = Get-QQWProperty $raw 'enabled' $false
    if ($enabled -isnot [bool]) { throw 'direct_enabled_must_be_boolean' }
    $registry = Resolve-QQWPrivatePath (Join-Path $root 'direct.launch.json') $root
    $logs = Resolve-QQWPrivatePath (Join-Path $root 'logs') $root
    if ($registry -in $all -or $logs -in $all) { throw 'direct_private_paths_overlap' }
    return [pscustomobject]@{Path=$path; PrivateRoot=$root; StatusFile=$paths.status_file; ControlFile=$paths.control_file
        StateDatabase=$paths.state_db; SelfMemberId=$selfId; Enabled=$enabled; Registry=$registry; Logs=$logs}
}

function Get-QQDSourceHash {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        # Python sorted(Path) orders these lowercase ASCII names ordinally;
        # PowerShell's culture-sensitive Sort-Object treats underscores differently.
        [string[]]$files = @(Get-ChildItem -LiteralPath (Join-Path $script:QQDRepositoryRoot 'harness\qq') -Filter '*.py' -File | ForEach-Object { $_.FullName })
        [Array]::Sort($files, [StringComparer]::Ordinal)
        foreach ($file in $files) {
            $bytes = [IO.File]::ReadAllBytes($file)
            [void]$hash.TransformBlock($bytes, 0, $bytes.Length, $bytes, 0)
        }
        [void]$hash.TransformFinalBlock([byte[]]@(), 0, 0)
        return 'qq-direct:' + [BitConverter]::ToString($hash.Hash).Replace('-', '').ToLowerInvariant()
    } finally { $hash.Dispose() }
}

function Get-QQDProcesses {
    $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "(Name = 'python.exe' OR Name = 'pythonw.exe') AND CommandLine LIKE '%harness.qq.direct%'" -OperationTimeoutSec 3 -ErrorAction Stop)
    foreach ($row in $rows) {
        $arguments = @(Split-QQWCommandLine ([string]$row.CommandLine))
        if ($arguments.Count -ge 3 -and $arguments[1] -ceq '-m' -and $arguments[2] -ceq 'harness.qq.direct') {
            [pscustomobject]@{ProcessId=[int]$row.ProcessId; ParentProcessId=[int]$row.ParentProcessId
                ExecutablePath=[string]$row.ExecutablePath; CreatedAt=$row.CreationDate.ToUniversalTime(); Arguments=$arguments}
        }
    }
}

function Test-QQDProcess {
    param([object]$Process, [object]$Configuration, [string[]]$Interpreters)
    if ($null -eq $Process -or @($Process.Arguments).Count -ne 5 -or
        $Process.Arguments[1] -cne '-m' -or $Process.Arguments[2] -cne 'harness.qq.direct' -or
        $Process.Arguments[3] -cne '--config') { return $false }
    try {
        $config = [IO.Path]::GetFullPath($Process.Arguments[4])
        $exe = [IO.Path]::GetFullPath($Process.ExecutablePath)
        return [IO.Path]::IsPathRooted($Process.Arguments[4]) -and
            $config.Equals($Configuration.Path, [StringComparison]::OrdinalIgnoreCase) -and $exe -in $Interpreters
    } catch { return $false }
}

function Test-QQDRegistry {
    param([object]$Registry, [object]$Configuration, [object[]]$Processes, [string[]]$Interpreters)
    if ((Get-QQWProperty $Registry 'service') -cne 'qq-direct-launch-v1' -or
        (Get-QQWProperty $Registry 'repository_root') -cne $script:QQDRepositoryRoot -or
        (Get-QQWProperty $Registry 'config_path') -cne $Configuration.Path -or
        (Get-QQWProperty $Registry 'source_hash') -notmatch '^qq-direct:[a-f0-9]{64}$') { return $false }
    $records = @(Get-QQWProperty $Registry 'processes' @())
    if ($records.Count -lt 1 -or $records.Count -gt 2 -or $Processes.Count -gt 2) { return $false }
    $launchPid = Get-QQWProperty $Registry 'launch_pid' 0
    if ($launchPid -lt 1 -or @($records | Where-Object { (Get-QQWProperty $_ 'pid') -eq $launchPid }).Count -ne 1) { return $false }
    foreach ($process in $Processes) {
        if (-not (Test-QQDProcess $process $Configuration $Interpreters)) { return $false }
        if ($process.ProcessId -ne $launchPid -and $process.ParentProcessId -ne $launchPid) { return $false }
        $match = @($records | Where-Object { (Get-QQWProperty $_ 'pid') -eq $process.ProcessId })
        if ($match.Count -ne 1) { return $false }
        try {
            $created = ([datetimeoffset](Get-QQWProperty $match[0] 'created_at')).UtcDateTime
            if ($created -ne $process.CreatedAt -or (Get-QQWProperty $match[0] 'executable') -cne $process.ExecutablePath -or
                (Get-QQWProperty $match[0] 'parent_pid') -ne $process.ParentProcessId) { return $false }
        } catch { return $false }
    }
    return $true
}

function Test-QQDStatus {
    param([object]$Status, [object]$Configuration, [object[]]$Processes, [string]$SourceHash,
        [switch]$AllowStaleHeartbeat)
    if ((Get-QQWProperty $Status 'service') -cne 'qq-direct-onebot-v1' -or
        (Get-QQWProperty $Status 'source_hash') -cne $SourceHash -or
        (Get-QQWProperty $Status 'config_path') -cne $Configuration.Path -or
        (Get-QQWProperty $Status 'repository_root') -cne $script:QQDRepositoryRoot -or
        (Get-QQWProperty $Status 'state_db') -cne $Configuration.StateDatabase -or
        (Get-QQWProperty $Status 'self_member_id') -cne $Configuration.SelfMemberId -or
        (Get-QQWProperty $Status 'running') -ne $true) { return $false }
    $match = @($Processes | Where-Object { $_.ProcessId -eq (Get-QQWProperty $Status 'pid') })
    if ($match.Count -ne 1) { return $false }
    try {
        $started = ([datetimeoffset](Get-QQWProperty $Status 'started_at')).UtcDateTime
        $heartbeat = ([datetimeoffset](Get-QQWProperty $Status 'heartbeat_at')).UtcDateTime
        return $started -ge $match[0].CreatedAt -and ($started - $match[0].CreatedAt).TotalSeconds -le 120 -and
            $heartbeat -ge $started -and ($AllowStaleHeartbeat -or ([datetime]::UtcNow - $heartbeat).TotalSeconds -le 30) -and
            ($heartbeat - [datetime]::UtcNow).TotalSeconds -le 5
    } catch { return $false }
}

function Write-QQDJson {
    param([string]$Path, [object]$Value)
    $temp = $Path + '.tmp-' + [guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temp, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
}

function Set-QQDControl {
    param([object]$Configuration, [bool]$Stop)
    $control = Read-QQWJson $Configuration.ControlFile
    # Start never grants send ownership or implicitly resumes a paused actor.
    $paused = if ($Stop) { $true } else { (Get-QQWProperty $control 'paused' $true) -ne $false }
    Write-QQDJson $Configuration.ControlFile @{paused=$paused; stop=$Stop}
}

function New-QQDResult {
    param([string]$State, [int]$ExitCode, [string]$Reason)
    [pscustomobject]@{service='qq-direct'; state=$State; exit_code=$ExitCode; reason=$Reason}
}

function Invoke-QQDirectStart {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$ConfigPath, [int]$TimeoutSeconds=45, [switch]$CheckOnly)
    $mutex = $null; $locked = $false; $configuration = $null; $launched = $null
    try {
        $configuration = Read-QQDConfiguration $ConfigPath
        $interpreters = @(Get-QQWInterpreterPaths)
        $source = Get-QQDSourceHash
        $processes = @(Get-QQDProcesses)
        if ($processes.Count -gt 0) {
            $registry = Read-QQWJson $configuration.Registry
            if (-not (Test-QQDRegistry $registry $configuration $processes $interpreters) -or
                -not (Test-QQDStatus (Read-QQWJson $configuration.StatusFile) $configuration $processes $source)) {
                return New-QQDResult 'failed' 20 'existing_direct_process_identity_or_source_unverified'
            }
            return New-QQDResult 'already_running' 0 'matching_process_and_current_source'
        }
        if ($CheckOnly -or -not $PSCmdlet.ShouldProcess($configuration.Path, 'Start the QQDirect daemon hidden without changing paused ownership')) {
            return New-QQDResult 'checked' 0 'configuration_paths_and_interpreter_available_no_process_started'
        }
        $mutex = New-Object Threading.Mutex($false, 'Local\NeuralCloudQQDirectLauncher')
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked -or @(Get-QQDProcesses).Count -gt 0) { return New-QQDResult 'launch_in_progress' 10 'another_launcher_or_actor_exists' }
        [void][IO.Directory]::CreateDirectory($configuration.Logs)
        Set-QQDControl $configuration $false
        $stamp = [datetime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
        $stdout = Join-Path $configuration.Logs ($stamp + '.stdout.log')
        $stderr = Join-Path $configuration.Logs ($stamp + '.stderr.log')
        $line = (@('-m', 'harness.qq.direct', '--config', $configuration.Path) | ForEach-Object { ConvertTo-QQWArgument $_ }) -join ' '
        $launchTime = [datetime]::UtcNow
        $launched = Start-Process -FilePath $interpreters[0] -ArgumentList $line -WorkingDirectory $script:QQDRepositoryRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $processes = @(Get-QQDProcesses)
            $owned = @($processes | Where-Object {
                (Test-QQDProcess $_ $configuration $interpreters) -and $_.CreatedAt -ge $launchTime.AddSeconds(-1) -and
                ($_.ProcessId -eq $launched.Id -or $_.ParentProcessId -eq $launched.Id)
            })
            if ($owned.Count -gt 0 -and $owned.Count -eq $processes.Count -and $owned.Count -le 2) {
                $registry = @{service='qq-direct-launch-v1'; repository_root=$script:QQDRepositoryRoot; config_path=$configuration.Path
                    source_hash=$source; launch_pid=$launched.Id; stdout_log=$stdout; stderr_log=$stderr
                    processes=@($owned | ForEach-Object { @{pid=$_.ProcessId; parent_pid=$_.ParentProcessId; executable=$_.ExecutablePath; created_at=$_.CreatedAt.ToString('o')} })}
                Write-QQDJson $configuration.Registry $registry
                if (Test-QQDStatus (Read-QQWJson $configuration.StatusFile) $configuration $owned $source) {
                    return New-QQDResult 'started' 0 'daemon_running_connection_and_send_ownership_reported_separately'
                }
            }
            if ($launched.HasExited) { break }
            Start-Sleep -Milliseconds 200
        }
        Set-QQDControl $configuration $true
        return New-QQDResult 'stop_requested' 10 'startup_not_verified_stop_requested'
    } catch {
        if ($null -ne $launched -and $null -ne $configuration) { try { Set-QQDControl $configuration $true } catch { } }
        return New-QQDResult 'failed' 20 'direct_configuration_identity_or_launch_error'
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
        if ($null -ne $launched) { $launched.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQDirectStart -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds -CheckOnly:$CheckOnly -WhatIf:$WhatIfPreference
    $result | ConvertTo-Json -Compress
    exit $result.exit_code
}
