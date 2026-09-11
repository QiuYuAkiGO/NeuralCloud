#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
)

Set-StrictMode -Version Latest
# Reuse only path, JSON, argv and interpreter helpers; dot-sourcing starts nothing.
. (Join-Path $PSScriptRoot 'Start-QQWatcher.ps1') -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds

function Read-QQAConfiguration {
    param([string]$Path)
    $anchor = [IO.Path]::GetFullPath($env:LOCALAPPDATA)
    $root = Resolve-QQWPrivatePath (Join-Path $anchor 'NeuralCloud\QQDesktop') $anchor
    $path = Resolve-QQWPrivatePath $Path $root -ExistingFile
    $value = Read-QQWJson $path
    $required = @('run_dir','watcher_status_path','trace_file','ledger_db','memory_db','worker_identity','build_hash')
    $optional = @('sample_interval_seconds','max_heartbeat_age_seconds','max_observation_gap_seconds')
    if ($null -eq $value -or @($value.PSObject.Properties.Name | Where-Object { $_ -notin ($required + $optional) }).Count) {
        throw 'acceptance_configuration_invalid'
    }
    $paths = @{}
    foreach ($field in @('run_dir','watcher_status_path','trace_file','ledger_db','memory_db')) {
        $entry = Get-QQWProperty $value $field
        if ($entry -isnot [string]) { throw 'acceptance_path_invalid' }
        $paths[$field] = Resolve-QQWPrivatePath $entry $root -ExistingFile:($field -in @('watcher_status_path','ledger_db','memory_db'))
        if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($paths[$field])) -PathType Container)) {
            throw 'acceptance_parent_missing'
        }
    }
    $distinct = @($path) + @($paths.Values)
    if (@($distinct | Sort-Object -Unique).Count -ne $distinct.Count) { throw 'acceptance_paths_overlap' }
    foreach ($other in $distinct) {
        if ($other.StartsWith($paths.run_dir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'acceptance_sources_inside_run_directory'
        }
    }
    if ($paths.run_dir.Equals((Join-Path $root 'acceptance-launches'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'acceptance_run_directory_reserved'
    }
    $worker = Get-QQWProperty $value 'worker_identity'
    if ($null -eq $worker -or @($worker.PSObject.Properties.Name | Where-Object { $_ -notin @('pid','started_at') }).Count) {
        throw 'acceptance_worker_invalid'
    }
    $workerId = Get-QQWProperty $worker 'pid'
    if (($workerId -isnot [int] -and $workerId -isnot [long]) -or $workerId -lt 1) { throw 'acceptance_worker_invalid' }
    $started = Get-QQAInstant (Get-QQWProperty $worker 'started_at')
    $build = Get-QQWProperty $value 'build_hash'
    if ($build -isnot [string] -or $build -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.:-]{6,159}$') { throw 'acceptance_build_invalid' }
    $normalized = [ordered]@{}
    foreach ($field in @('run_dir','watcher_status_path','trace_file','ledger_db','memory_db')) { $normalized[$field] = $paths[$field] }
    $normalized.worker_identity = @{pid=$workerId; started_at=$started.ToString('o')}
    $normalized.build_hash = $build
    foreach ($pair in @(@('sample_interval_seconds',15), @('max_heartbeat_age_seconds',30), @('max_observation_gap_seconds',10))) {
        $number = Get-QQWProperty $value $pair[0] $pair[1]
        if (($number -isnot [int] -and $number -isnot [long] -and $number -isnot [double] -and $number -isnot [decimal]) -or
            [double]::IsNaN([double]$number) -or [double]::IsInfinity([double]$number) -or
            [double]$number -le 0 -or [double]$number -gt $pair[1]) { throw 'acceptance_interval_invalid' }
        $normalized[$pair[0]] = $number
    }
    return [pscustomobject]@{Path=$path; PrivateRoot=$root; RunDirectory=$paths.run_dir; StatusFile=$paths.watcher_status_path
        WorkerId=$workerId; WorkerStarted=$started; BuildHash=$build; Value=[pscustomobject]$normalized}
}

function Get-QQAInstant {
    param([object]$Value)
    $text = if ($Value -is [DateTime]) { $Value.ToString('o') } else { [string]$Value }
    if ($text -notmatch '(Z|[+-][0-9]{2}:[0-9]{2})$') { throw 'acceptance_timestamp_invalid' }
    return [DateTimeOffset]::Parse($text).ToUniversalTime()
}

function Test-QQAWorker {
    param([object]$Configuration)
    $status = Read-QQWJson $Configuration.StatusFile
    if ((Get-QQWProperty $status 'service') -cne 'qq-desktop-watcher-v1' -or
        (Get-QQWProperty $status 'pid') -ne $Configuration.WorkerId -or
        (Get-QQWProperty $status 'build_hash') -cne $Configuration.BuildHash -or
        (Get-QQAInstant (Get-QQWProperty $status 'started_at')) -ne $Configuration.WorkerStarted) { return $false }
    foreach ($name in @('running','automatic_screen_capture','automatic_send')) {
        $flag = Get-QQWProperty $status $name
        if ($flag -isnot [bool] -or -not $flag) { return $false }
    }
    $age = ([DateTimeOffset]::UtcNow - (Get-QQAInstant (Get-QQWProperty $status 'heartbeat_at'))).TotalSeconds
    return $age -ge -2 -and $age -le $Configuration.Value.max_heartbeat_age_seconds
}

function Get-QQAProcesses {
    $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "(Name = 'python.exe' OR Name = 'pythonw.exe') AND CommandLine LIKE '%harness.qq.acceptance%'" `
        -OperationTimeoutSec 2 -ErrorAction Stop)
    foreach ($row in $rows) {
        $arguments = @(Split-QQWCommandLine ([string]$row.CommandLine))
        if ($arguments.Count -ge 4 -and $arguments[1] -ceq '-m' -and $arguments[2] -ceq 'harness.qq.acceptance' -and $arguments[3] -ceq 'start') {
            [pscustomobject]@{ProcessId=[int]$row.ProcessId; ExecutablePath=[string]$row.ExecutablePath; Arguments=$arguments}
        }
    }
}

function Test-QQAMonitor {
    param([object]$Process, [object]$Configuration, [string[]]$Interpreters)
    try {
        $arguments = @($Process.Arguments)
        if ($arguments.Count -ne 6 -or $arguments[1] -cne '-m' -or $arguments[2] -cne 'harness.qq.acceptance' -or
            $arguments[3] -cne 'start' -or $arguments[4] -cne '--config' -or
            $Interpreters -notcontains $Process.ExecutablePath) { return $false }
        $snapshot = Read-QQAConfiguration $arguments[5]
        foreach ($field in @('run_dir','watcher_status_path','trace_file','ledger_db','memory_db','build_hash',
                            'sample_interval_seconds','max_heartbeat_age_seconds','max_observation_gap_seconds')) {
            if ($snapshot.Value.$field -cne $Configuration.Value.$field) { return $false }
        }
        return $snapshot.WorkerId -eq $Configuration.WorkerId -and $snapshot.WorkerStarted -eq $Configuration.WorkerStarted
    } catch { return $false }
}

function Read-QQAReadyManifest {
    param([object]$Configuration, [object[]]$Processes, [string[]]$Interpreters)
    $manifestPath = Resolve-QQWPrivatePath (Join-Path $Configuration.RunDirectory 'manifest.json') $Configuration.PrivateRoot
    $samplesPath = Resolve-QQWPrivatePath (Join-Path $Configuration.RunDirectory 'samples.jsonl') $Configuration.PrivateRoot
    $manifest = Read-QQWJson $manifestPath -MaximumBytes 16777216
    if ($null -eq $manifest -or (Get-QQWProperty $manifest 'schema_version') -cne 'qq-acceptance-manifest-v1' -or
        (Get-QQWProperty $manifest 'duration_seconds') -ne 28800 -or -not (Test-Path -LiteralPath $samplesPath -PathType Leaf)) { return $null }
    $found = @($Processes | Where-Object { $_.ProcessId -eq (Get-QQWProperty $manifest 'monitor_pid') -and
                  (Test-QQAMonitor $_ $Configuration $Interpreters) })
    if ($found.Count -ne 1) { return $null }
    $stored = Get-QQWProperty $manifest 'config'
    if ((Get-QQWProperty $stored 'build_hash') -cne $Configuration.BuildHash -or
        (Get-QQWProperty $stored 'run_dir') -cne $Configuration.RunDirectory) { return $null }
    $storedWorker = Get-QQWProperty $stored 'worker_identity'
    if ((Get-QQWProperty $storedWorker 'pid') -ne $Configuration.WorkerId -or
        (Get-QQAInstant (Get-QQWProperty $storedWorker 'started_at')) -ne $Configuration.WorkerStarted -or
        (Test-Path -LiteralPath (Join-Path $Configuration.RunDirectory 'monitor_failure.json'))) { return $null }
    $sample = [IO.File]::ReadLines($samplesPath) | Select-Object -First 1
    if (-not $sample) { return $null }
    $first = $sample | ConvertFrom-Json -ErrorAction Stop
    if (@(Get-QQWProperty $first 'reasons').Count -gt 0 -or (Get-QQWProperty $first 'elapsed_seconds') -gt 1) { return $null }
    return $manifest
}

function Start-QQAProcess {
    param([string]$PythonPath, [string]$SnapshotPath, [string]$StdoutPath, [string]$StderrPath)
    $line = (@('-m','harness.qq.acceptance','start','--config',$SnapshotPath) | ForEach-Object { ConvertTo-QQWArgument $_ }) -join ' '
    return Start-Process -FilePath $PythonPath -ArgumentList $line -WorkingDirectory $script:QQWRepositoryRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -ErrorAction Stop
}

function New-QQALauncherMutex { return New-Object Threading.Mutex($false, 'Local\NeuralCloudQQAcceptanceLauncher') }

function Invoke-QQAcceptanceStart {
    param([string]$ConfigPath, [ValidateRange(0.05,120)][double]$TimeoutSeconds=30)
    $mutex=$null; $locked=$false; $process=$null
    $result = [ordered]@{service='qq-acceptance-launcher'; status='failed'; exit_code=20; reason='acceptance_configuration_or_launch_error'
        launch_pid=$null; monitor_pid=$null; worker_pid=$null; started_at=$null; stdout_path=$null; stderr_path=$null
        fully_automatic_e2e_pass=$false}
    try {
        $config = Read-QQAConfiguration $ConfigPath
        $interpreters = @(Get-QQWInterpreterPaths)
        $mutex = New-QQALauncherMutex
        try { $locked=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked=$true }
        if (-not $locked) { $result.status='launch_in_progress'; $result.exit_code=10; return [pscustomobject]$result }
        if (-not (Test-QQAWorker $config)) { $result.reason='fixed_worker_not_ready'; return [pscustomobject]$result }
        $existing = @(Get-QQAProcesses)
        if ($existing.Count -gt 0) {
            if (@($existing | Where-Object { -not (Test-QQAMonitor $_ $config $interpreters) }).Count -gt 0) {
                $result.reason='another_acceptance_monitor_exists'; return [pscustomobject]$result
            }
            $manifest = Read-QQAReadyManifest $config $existing $interpreters
            if ($null -ne $manifest) {
                $result.status='already_running'; $result.exit_code=0; $result.reason=''
                $result.monitor_pid=$manifest.monitor_pid; $result.worker_pid=$config.WorkerId; $result.started_at=$manifest.started_at
            } else { $result.status='startup_pending'; $result.exit_code=10; $result.reason='existing_monitor_not_yet_ready' }
            return [pscustomobject]$result
        }
        if (Test-Path -LiteralPath $config.RunDirectory) { $result.reason='run_directory_already_exists'; return [pscustomobject]$result }
        $logs = Resolve-QQWPrivatePath (Join-Path $config.PrivateRoot 'acceptance-launches') $config.PrivateRoot
        [void][IO.Directory]::CreateDirectory($logs)
        $launch = Join-Path $logs ([Guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($launch)
        $snapshot = Join-Path $launch 'config.json'
        [IO.File]::WriteAllText($snapshot, ($config.Value | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
        $result.stdout_path=Join-Path $launch 'stdout.log'; $result.stderr_path=Join-Path $launch 'stderr.log'
        $process = Start-QQAProcess $interpreters[0] $snapshot $result.stdout_path $result.stderr_path
        $result.launch_pid=$process.Id; $result.worker_pid=$config.WorkerId
        $clock=[Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $candidates = @(Get-QQAProcesses)
            $manifest = Read-QQAReadyManifest $config $candidates $interpreters
            if ($null -ne $manifest) {
                $result.status='started'; $result.exit_code=0; $result.reason=''
                $result.monitor_pid=$manifest.monitor_pid; $result.started_at=$manifest.started_at
                break
            }
            if ($process.HasExited) { $result.reason='monitor_exited_before_readiness'; break }
            Start-Sleep -Milliseconds 100
        }
        if ($result.status -eq 'failed' -and -not $process.HasExited) {
            $result.status='startup_pending'; $result.exit_code=10; $result.reason='monitor_readiness_timeout_no_restart'
        }
        [IO.File]::WriteAllText((Join-Path $launch 'launch.json'), ($result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
        return [pscustomobject]$result
    } catch { return [pscustomobject]$result }
    finally {
        if ($locked -and $null -ne $mutex) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
        if ($process -is [Diagnostics.Process]) { $process.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQAcceptanceStart -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds
    $result | ConvertTo-Json -Depth 5 -Compress
    exit $result.exit_code
}
