#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop\watcher.json'),
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 300
)

Set-StrictMode -Version Latest
$script:QQWRepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

function Get-QQWProperty {
    param([object]$Value, [string]$Name, [object]$Default = $null)
    if ($null -eq $Value) { return $Default }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Resolve-QQWPrivatePath {
    param([string]$Path, [string]$PrivateRoot, [switch]$ExistingFile)
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'private_path_required' }
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($PrivateRoot).TrimEnd('\', '/')
    if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'path_outside_private_root'
    }
    $cursor = $full
    while ($cursor.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'private_path_reparse_point' }
        }
        if ($cursor.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    if ($ExistingFile -and -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw 'private_file_missing' }
    return $full
}

function Read-QQWJson {
    param([string]$Path, [int]$MaximumBytes = 65536)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $Path).Length -gt $MaximumBytes) { throw 'json_file_too_large' }
    $json = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    # PS7.5+ otherwise changes ISO timestamps into DateTime objects, losing the
    # original string representation before our explicit timezone validation.
    if ((Get-Command ConvertFrom-Json -CommandType Cmdlet).Parameters.ContainsKey('DateKind')) {
        return ($json | ConvertFrom-Json -DateKind String -ErrorAction Stop)
    }
    return ($json | ConvertFrom-Json -ErrorAction Stop)
}

function Read-QQWGroupRoster {
    param([object]$Config, [bool]$AutomaticSend)
    $qqHomeGroup = Get-QQWProperty $Config 'group_id'
    $property = $Config.PSObject.Properties['group_roster']
    if ($null -eq $property -or $null -eq $property.Value) {
        return [pscustomobject]@{group_id=$qqHomeGroup; label=''; observe=$true; send=$AutomaticSend}
    }
    $raw = $property.Value
    if ($raw -isnot [Array] -or $raw.Count -lt 1 -or $raw.Count -gt 16) { throw 'qq_group_roster_size_invalid' }
    $seen = @{}
    $homeObserved = $false
    $entries = @()
    foreach ($entry in $raw) {
        if ($null -eq $entry -or $entry -isnot [pscustomobject]) { throw 'qq_group_roster_entry_invalid' }
        foreach ($name in $entry.PSObject.Properties.Name) {
            if ($name -notin @('group_id', 'label', 'observe', 'send')) { throw 'qq_group_roster_entry_invalid' }
        }
        $id = Get-QQWProperty $entry 'group_id'
        if ($id -isnot [string] -or $id -notmatch '^[1-9][0-9]{0,19}$') { throw 'qq_group_roster_identity_invalid' }
        if ($seen.ContainsKey($id)) { throw 'qq_group_roster_duplicate' }
        $seen[$id] = $true
        $label = Get-QQWProperty $entry 'label' ''
        if ($label -isnot [string] -or $label.Length -gt 128 -or $label -match '[\x00-\x1f\x7f]') { throw 'qq_group_roster_label_invalid' }
        $observe = Get-QQWProperty $entry 'observe' $true
        $send = Get-QQWProperty $entry 'send' $false
        if ($observe -isnot [bool] -or $send -isnot [bool]) { throw 'qq_group_roster_flags_invalid' }
        if ($send -and ($id -cne $qqHomeGroup -or -not $AutomaticSend)) { throw 'qq_group_roster_send_scope_invalid' }
        if ($id -ceq $qqHomeGroup -and $observe) { $homeObserved = $true }
        $entries += [pscustomobject]@{group_id=$id; label=$label; observe=$observe; send=$send}
    }
    if (-not $homeObserved) { throw 'qq_group_roster_home_observation_required' }
    return $entries
}

function Read-QQWConfiguration {
    param([string]$ConfigPath)
    $anchor = [IO.Path]::GetFullPath($env:LOCALAPPDATA)
    $root = Resolve-QQWPrivatePath (Join-Path $anchor 'NeuralCloud\QQDesktop') $anchor
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'private_root_missing' }
    $path = Resolve-QQWPrivatePath $ConfigPath $root -ExistingFile
    $config = Read-QQWJson $path
    $paths = @{}
    foreach ($field in @('session_config_path', 'state_db', 'status_file', 'control_file', 'body_token_file', 'agent_token_file')) {
        $existing = $field -in @('session_config_path', 'body_token_file', 'agent_token_file')
        $paths[$field] = Resolve-QQWPrivatePath ([string](Get-QQWProperty $config $field)) $root -ExistingFile:$existing
    }
    $distinct = @($path, $paths.session_config_path, $paths.state_db, $paths.status_file, $paths.control_file,
                  $paths.body_token_file, $paths.agent_token_file)
    $trace = Get-QQWProperty $config 'trace_file'
    if ($null -eq $trace) { $trace = Join-Path ([IO.Path]::GetDirectoryName($paths.status_file)) 'watcher-trace.jsonl' }
    if ($trace -isnot [string]) { throw 'trace_path_invalid' }
    $trace = Resolve-QQWPrivatePath $trace $root
    if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($trace)) -PathType Container)) { throw 'trace_parent_missing' }
    $distinct += $trace
    $baseline = Get-QQWProperty $config 'initial_baseline_observation'
    if ($null -ne $baseline) {
        if ($baseline -isnot [string]) { throw 'baseline_path_invalid' }
        $distinct += Resolve-QQWPrivatePath $baseline $root -ExistingFile
    }
    if (@($distinct | Sort-Object -Unique).Count -ne $distinct.Count) { throw 'overlapping_private_paths' }
    foreach ($field in @('group_id', 'self_member_id')) {
        $value = Get-QQWProperty $config $field
        if ($value -isnot [string] -or $value -notmatch '^[1-9][0-9]{0,19}$') { throw 'identity_scope_invalid' }
    }
    foreach ($field in @('body_url', 'agent_url')) {
        $uri = $null
        if (-not [Uri]::TryCreate([string](Get-QQWProperty $config $field), [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -cne 'http' -or $uri.Host -notin @('127.0.0.1', '[::1]', '::1') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
            $uri.AbsolutePath -ne '/') { throw 'loopback_url_required' }
    }
    $send = Get-QQWProperty $config 'automatic_send'
    if ($send -isnot [bool]) { throw 'automatic_send_must_be_explicit' }
    $groupRoster = @(Read-QQWGroupRoster $config $send)
    $accessibility = Get-QQWProperty $config 'accessibility_enabled' $false
    if ($accessibility -isnot [bool]) { throw 'accessibility_must_be_boolean' }
    $qqBody = Get-QQWProperty $config 'qq_body_enabled' $false
    $qqIdle = Get-QQWProperty $config 'homeostatic_idle_enabled' $false
    if ($qqBody -isnot [bool] -or $qqIdle -isnot [bool]) { throw 'qq_body_flags_must_be_boolean' }
    if ($qqBody -and -not $accessibility) { throw 'qq_body_requires_accessibility' }
    if ($qqIdle) {
        if (-not $qqBody) { throw 'qq_idle_requires_negotiated_body' }
        $domainPath = Resolve-QQWPrivatePath ([string](Get-QQWProperty $config 'homeostasis_state_dir')) $root
        if ($domainPath -in $distinct -or (Test-Path -LiteralPath $domainPath -PathType Leaf)) { throw 'qq_homeostasis_state_overlap' }
        $profile = [string](Get-QQWProperty $config 'homeostasis_profile_path')
        $expectedProfile = [IO.Path]::GetFullPath((Join-Path $script:QQWRepositoryRoot 'presets\homeostasis\qq-social-drive-v1.json'))
        if (-not [IO.Path]::IsPathRooted($profile) -or
            -not [IO.Path]::GetFullPath($profile).Equals($expectedProfile, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $expectedProfile -PathType Leaf)) { throw 'qq_homeostasis_profile_invalid' }
    }
    foreach ($bound in @(@('observation_interval_seconds', 1, 0.25, 2), @('turn_deadline_seconds', 60, 1, 60),
                         @('homeostasis_tick_seconds', 5, 1, 30), @('homeostatic_quiet_seconds', 120, 60, 3600),
                         @('homeostatic_opportunity_seconds', 1800, 300, 86400),
                         @('group_observation_seconds', 300, 60, 3600))) {
        $number = Get-QQWProperty $config $bound[0] $bound[1]
        if (($number -isnot [int] -and $number -isnot [long] -and $number -isnot [double] -and $number -isnot [decimal]) -or
            [double]::IsNaN([double]$number) -or [double]::IsInfinity([double]$number) -or
            [double]$number -lt $bound[2] -or [double]$number -gt $bound[3]) { throw 'observation_or_deadline_interval_invalid' }
    }
    $poll = Get-QQWProperty $config 'poll_seconds'
    $cooldown = Get-QQWProperty $config 'cooldown_seconds'
    foreach ($number in @($poll, $cooldown)) {
        if ($number -isnot [int] -and $number -isnot [long] -and $number -isnot [double] -and $number -isnot [decimal]) {
            throw 'numeric_interval_required'
        }
        if ([double]::IsNaN([double]$number) -or [double]::IsInfinity([double]$number)) { throw 'finite_interval_required' }
    }
    if ([double]$poll -lt 5 -or [double]$poll -gt 300 -or [double]$cooldown -lt 30 -or [double]$cooldown -gt 3600) {
        throw 'interval_out_of_range'
    }
    $revision = Get-QQWProperty $config 'expected_revision'
    if (($revision -isnot [int] -and $revision -isnot [long]) -or $revision -lt 1) { throw 'revision_invalid' }
    foreach ($field in @('expected_instance_id', 'vision_model')) {
        $value = Get-QQWProperty $config $field
        if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 256) {
            throw 'identity_or_model_missing'
        }
    }
    # Only paths are retained. Never read or print service token contents.
    return [pscustomobject]@{
        PrivateRoot=$root; Path=$path; StatusFile=$paths.status_file; ControlFile=$paths.control_file
        StateDatabase=$paths.state_db; GroupId=$config.group_id; SelfMemberId=$config.self_member_id
        TraceFile=$trace; AccessibilityEnabled=$accessibility
        GroupRoster=$groupRoster
        AutomaticSend=$send; HeartbeatMaxAgeSeconds=[Math]::Max(60.0, [double]$poll * 3.0)
    }
}

function Get-QQWInterpreterPaths {
    $python = Join-Path $script:QQWRepositoryRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'repository_python_missing' }
    $paths = @($python)
    $venv = Join-Path $script:QQWRepositoryRoot '.venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $venv -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($venv)) {
            if ($line -match '^home\s*=\s*(.+)$' -and [IO.Path]::IsPathRooted($Matches[1])) {
                $base = [IO.Path]::GetFullPath((Join-Path $Matches[1] 'python.exe'))
                if (Test-Path -LiteralPath $base -PathType Leaf) { $paths += $base }
            }
        }
    }
    return $paths
}

function Split-QQWCommandLine {
    param([string]$CommandLine)
    if ($null -eq ('QQWatcherCommandLine' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class QQWatcherCommandLine {
    [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr value);
    public static string[] Split(string command) {
        int count; IntPtr memory = CommandLineToArgvW(command, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("command_line_parse_failed");
        try {
            var values = new string[count];
            for (int i = 0; i < count; i++) values[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
            return values;
        } finally { LocalFree(memory); }
    }
}
'@ -ErrorAction Stop
    }
    return [QQWatcherCommandLine]::Split($CommandLine)
}

function Get-QQWProcesses {
    # Query only possible watcher processes, never inventory unrelated argv.
    $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "(Name = 'python.exe' OR Name = 'pythonw.exe') AND CommandLine LIKE '%harness.qq.watcher%'" `
        -OperationTimeoutSec 2 -ErrorAction Stop)
    foreach ($row in $rows) {
        $arguments = @(Split-QQWCommandLine ([string]$row.CommandLine))
        if ($arguments.Count -ge 3 -and $arguments[1] -ceq '-m' -and $arguments[2] -ceq 'harness.qq.watcher') {
            [pscustomobject]@{ProcessId=[int]$row.ProcessId; ExecutablePath=[string]$row.ExecutablePath
                CreatedAt=$row.CreationDate.ToUniversalTime(); Arguments=$arguments}
        }
    }
}

function Test-QQWProcessConfiguration {
    param([object]$Process, [object]$Configuration)
    if ($null -eq $Process) { return $false }
    $arguments = @($Process.Arguments)
    if ($arguments.Count -lt 4 -or $arguments[1] -cne '-m' -or $arguments[2] -cne 'harness.qq.watcher') { return $false }
    $configArguments = @()
    for ($index = 3; $index -lt $arguments.Count; $index++) {
        if ($arguments[$index] -ceq '--config' -and $index + 1 -lt $arguments.Count) {
            $index++; $configArguments += $arguments[$index]
        } elseif ($arguments[$index].StartsWith('--config=', [StringComparison]::Ordinal)) {
            $configArguments += $arguments[$index].Substring(9)
        }
    }
    if ($configArguments.Count -ne 1) { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($configArguments[0])) { return $false }
        $config = [IO.Path]::GetFullPath($configArguments[0])
        return $config.Equals($Configuration.Path, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Test-QQWProcessIdentity {
    param([object]$Process, [object]$Configuration, [string[]]$InterpreterPaths)
    if (-not (Test-QQWProcessConfiguration $Process $Configuration)) { return $false }
    if (@($Process.Arguments).Count -ne 5 -or $Process.Arguments[3] -cne '--config') { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($Process.ExecutablePath)) { return $false }
        $executable = [IO.Path]::GetFullPath($Process.ExecutablePath)
        return @($InterpreterPaths | Where-Object { $_.Equals($executable, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
    } catch { return $false }
}

function Test-QQWStatus {
    param([object]$Status, [object[]]$Processes, [object]$Configuration, [string[]]$InterpreterPaths,
          [switch]$Stopped)
    if ((Get-QQWProperty $Status 'service') -cne 'qq-desktop-watcher-v1' -or
        (Get-QQWProperty $Status 'group_id') -cne $Configuration.GroupId -or
        (Get-QQWProperty $Status 'self_member_id') -cne $Configuration.SelfMemberId) { return $false }
    foreach ($binding in @(@('config_path', $Configuration.Path), @('state_db', $Configuration.StateDatabase))) {
        $value = Get-QQWProperty $Status $binding[0]
        if ($value -isnot [string] -or -not $value.Equals($binding[1], [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    $processId = Get-QQWProperty $Status 'pid'
    if (($processId -isnot [int] -and $processId -isnot [long]) -or $processId -lt 1) { return $false }
    $process = @($Processes | Where-Object { $_.ProcessId -eq $processId })
    if ($process.Count -ne 1 -or -not (Test-QQWProcessIdentity $process[0] $Configuration $InterpreterPaths)) { return $false }
    try {
        $startedValue = Get-QQWProperty $Status 'started_at'
        $heartbeatValue = Get-QQWProperty $Status 'heartbeat_at'
        # Older PS Core has date coercion but no -DateKind. Round-trip formatting
        # retains its timezone; Unspecified dates still fail the suffix check.
        $startedText = if ($startedValue -is [DateTime]) { $startedValue.ToString('o') } else { [string]$startedValue }
        $heartbeatText = if ($heartbeatValue -is [DateTime]) { $heartbeatValue.ToString('o') } else { [string]$heartbeatValue }
        if ($startedText -notmatch '(Z|[+-][0-9]{2}:[0-9]{2})$' -or
            $heartbeatText -notmatch '(Z|[+-][0-9]{2}:[0-9]{2})$') { return $false }
        $started = [DateTimeOffset]::Parse($startedText).UtcDateTime
        $heartbeat = [DateTimeOffset]::Parse($heartbeatText).UtcDateTime
        $now = [DateTime]::UtcNow
        if ($started -lt $process[0].CreatedAt -or ($started - $process[0].CreatedAt).TotalSeconds -gt 120 -or
            $heartbeat -lt $started -or ($heartbeat - $now).TotalSeconds -gt 5 -or
            ($now - $heartbeat).TotalSeconds -gt $Configuration.HeartbeatMaxAgeSeconds) { return $false }
    } catch { return $false }
    $running = Get-QQWProperty $Status 'running'
    if ($running -isnot [bool]) { return $false }
    $state = Get-QQWProperty $Status 'state'
    if ($Stopped) { return (-not $running) -and $state -in @('paused', 'stopped') }
    if (-not $running -or $state -isnot [string] -or [string]::IsNullOrWhiteSpace($state) -or
        $state -in @('paused', 'stopped', 'failed', 'error', 'starting', 'initializing', 'reading_initial_baseline')) { return $false }
    $capture = Get-QQWProperty $Status 'automatic_screen_capture'
    $send = Get-QQWProperty $Status 'automatic_send'
    return $capture -is [bool] -and $capture -eq $true -and $send -is [bool] -and $send -eq $Configuration.AutomaticSend
}

function Set-QQWPaused {
    param([object]$Configuration, [bool]$Paused)
    $path = Resolve-QQWPrivatePath $Configuration.ControlFile $Configuration.PrivateRoot
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
    $temporary = $path + '.tmp-' + [Guid]::NewGuid().ToString('N')
    try {
        $text = @{paused=$Paused} | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($temporary, $text, (New-Object Text.UTF8Encoding($false)))
        # PS5.1 converts $null to an empty string for this .NET overload.
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $path) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
}

function ConvertTo-QQWArgument {
    param([string]$Value)
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Start-QQWProcess {
    param([string]$PythonPath, [string]$ConfigPath, [string]$StdoutPath, [string]$StderrPath)
    $line = (@('-m', 'harness.qq.watcher', '--config', $ConfigPath) | ForEach-Object { ConvertTo-QQWArgument $_ }) -join ' '
    return Start-Process -FilePath $PythonPath -ArgumentList $line -WorkingDirectory $script:QQWRepositoryRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -ErrorAction Stop
}

function New-QQWResult {
    param([string]$Status, [int]$ExitCode, [string]$Reason='', [object]$Process=$null)
    return [pscustomobject][ordered]@{service='qq-desktop-watcher'; status=$Status; exit_code=$ExitCode; reason=$Reason
        launch_pid=$(if ($null -ne $Process) {$Process.Id} else {$null})}
}

function New-QQWLauncherMutex {
    return New-Object Threading.Mutex($false, 'Local\NeuralCloudQQWatcherLauncher')
}

function Invoke-QQWatcherStart {
    param([string]$ConfigPath, [ValidateRange(0.05, 600)][double]$TimeoutSeconds=300)
    $mutex = $null; $locked = $false; $process = $null; $configuration = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $configuration = Read-QQWConfiguration $ConfigPath
        $interpreters = @(Get-QQWInterpreterPaths)
        $mutex = New-QQWLauncherMutex
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { return New-QQWResult 'launch_in_progress' 10 'another_launcher_owns_the_lock' }
        $existing = @(Get-QQWProcesses)
        if ($existing.Count -gt 0) {
            $status = Read-QQWJson $configuration.StatusFile
            $control = Read-QQWJson $configuration.ControlFile
            if ((Get-QQWProperty $control 'paused' $false) -ne $true -and
                (Test-QQWStatus $status $existing $configuration $interpreters)) {
                return New-QQWResult 'already_running' 0
            }
            return New-QQWResult 'failed' 20 'existing_watcher_has_no_matching_fresh_running_status'
        }
        $logs = Resolve-QQWPrivatePath (Join-Path $configuration.PrivateRoot 'logs') $configuration.PrivateRoot
        [void][IO.Directory]::CreateDirectory($logs)
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            return New-QQWResult 'failed' 20 'startup_deadline_reached_before_launch'
        }
        Set-QQWPaused $configuration $false
        $process = Start-QQWProcess -PythonPath $interpreters[0] -ConfigPath $configuration.Path `
            -StdoutPath (Join-Path $logs "watcher-$stamp.stdout.log") -StderrPath (Join-Path $logs "watcher-$stamp.stderr.log")
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            if ((Get-QQWProperty (Read-QQWJson $configuration.ControlFile) 'paused' $true) -ne $false) {
                return New-QQWResult 'pause_requested' 10 'startup_cancelled_by_pause_request' $process
            }
            $candidates = @(Get-QQWProcesses)
            $status = Read-QQWJson $configuration.StatusFile
            if ((Get-QQWProperty (Read-QQWJson $configuration.ControlFile) 'paused' $true) -ne $false) {
                return New-QQWResult 'pause_requested' 10 'startup_cancelled_by_pause_request' $process
            }
            if (Test-QQWStatus $status $candidates $configuration $interpreters) {
                return New-QQWResult 'started' 0 -Process $process
            }
            $remaining = [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds)
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(200, $remaining)) }
        }
        Set-QQWPaused $configuration $true
        return New-QQWResult 'failed' 20 'startup_deadline_reached_pause_requested' $process
    } catch {
        if ($null -ne $process -and $null -ne $configuration) {
            try { Set-QQWPaused $configuration $true } catch { }
        }
        return New-QQWResult 'failed' 20 'configuration_identity_or_launch_error' $process
    } finally {
        if ($locked -and $null -ne $mutex) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
        if ($process -is [Diagnostics.Process]) { $process.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQWatcherStart -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds
    $result | ConvertTo-Json -Depth 4 -Compress
    exit $result.exit_code
}
