#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DDPaths {
    $local = [Environment]::GetFolderPath('LocalApplicationData')
    $install = Join-Path $env:ProgramFiles 'Docker\Docker'
    return @{
        Local = $local
        Run = Join-Path $local 'Docker\run'
        Secrets = Join-Path $local 'docker-secrets-engine'
        Logs = Join-Path $local 'Docker\log\host'
        Journal = Join-Path $local 'NeuralCloud\DockerRecovery\logs'
        Docker = Join-Path $install 'resources\bin\docker.exe'
        Desktop = Join-Path $install 'Docker Desktop.exe'
        Wsl = Join-Path ([Environment]::GetFolderPath('System')) 'wsl.exe'
    }
}

function Invoke-DDTimedProcess {
    param([string]$Executable, [string]$Arguments, [int]$TimeoutMilliseconds)
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]@{
        FileName = $Executable; Arguments = $Arguments
        UseShellExecute = $false; CreateNoWindow = $true
        RedirectStandardOutput = $true; RedirectStandardError = $true
    }
    $process.StartInfo.EnvironmentVariables.Remove('DOCKER_HOST')
    $process.StartInfo.EnvironmentVariables.Remove('DOCKER_CONTEXT')
    $process.StartInfo.EnvironmentVariables.Remove('DOCKER_TLS_VERIFY')
    $process.StartInfo.EnvironmentVariables.Remove('DOCKER_CERT_PATH')
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            # Kill only the probe we created, never Docker Desktop/backend or a process tree.
            try { $process.Kill() } catch [InvalidOperationException] { }
            [void]$process.WaitForExit(1000)
            return @{ Healthy = $false; TimedOut = $true; ExitCode = $null }
        }
        $complete = $stdout.Wait(1000) -and $stderr.Wait(1000)
        return @{
            Healthy = $complete -and $process.ExitCode -eq 0 -and $stdout.Result.Trim().Length -gt 0
            TimedOut = $false; ExitCode = $process.ExitCode
        }
    } catch {
        return @{ Healthy = $false; TimedOut = $false; ExitCode = $null }
    } finally { $process.Dispose() }
}

function Invoke-DDProbe {
    param($Paths, [int]$TimeoutMilliseconds)
    Invoke-DDTimedProcess $Paths.Docker '--host npipe:////./pipe/dockerDesktopLinuxEngine info --format "{{.ServerVersion}}"' $TimeoutMilliseconds
}

function ConvertTo-DDWindowsArgument {
    param([AllowEmptyString()][string]$Argument)
    # ProcessStartInfo.ArgumentList is unavailable in Windows PowerShell 5.1.
    # WSL parses option tokens before the Linux command; unnecessary quotes on flags
    # can make --distribution a Linux command. Quote only arguments that require it.
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
    $quoted = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}

function Invoke-DDWslProbe {
    param($Paths, [string]$Distro, [int]$TimeoutMilliseconds)
    # The Linux timeout survives a killed Windows wsl.exe probe. Reserve startup/kill margin.
    $linuxSeconds = [Math]::Max(0.001, $TimeoutMilliseconds * 0.7 / 1000.0)
    $killSeconds = [Math]::Max(0.001, [Math]::Min(0.5, $TimeoutMilliseconds * 0.1 / 1000.0))
    $duration = $linuxSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture) + 's'
    $killAfter = '--kill-after=' + $killSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture) + 's'
    $arguments = @(
        '--distribution', $Distro, '--exec', '/usr/bin/timeout', $killAfter, $duration,
        '/usr/bin/env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT',
        '-u', 'DOCKER_TLS_VERIFY', '-u', 'DOCKER_CERT_PATH',
        '/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info', '--format', '{{.ServerVersion}}'
    )
    $encoded = ($arguments | ForEach-Object { ConvertTo-DDWindowsArgument $_ }) -join ' '
    Invoke-DDTimedProcess $Paths.Wsl $encoded $TimeoutMilliseconds
}

function New-DDWslKeepAliveProcess {
    param($Paths, [string]$Distro, [int]$LifetimeSeconds)
    $arguments = @('--distribution', $Distro, '--exec', '/usr/bin/timeout',
        '--kill-after=1s', ($LifetimeSeconds.ToString([Globalization.CultureInfo]::InvariantCulture) + 's'), '/bin/cat')
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]@{
        FileName = $Paths.Wsl
        Arguments = (($arguments | ForEach-Object { ConvertTo-DDWindowsArgument $_ }) -join ' ')
        UseShellExecute = $false; CreateNoWindow = $true
        RedirectStandardInput = $true; RedirectStandardOutput = $true; RedirectStandardError = $true
    }
    return $process
}

function Stop-DDWslKeepAlive {
    param([AllowNull()]$KeepAlive)
    if ($null -eq $KeepAlive) { return }
    $process = $KeepAlive.Process
    try {
        if ($KeepAlive.Started) {
            try { $process.StandardInput.Close() } catch { }
            if (-not $process.WaitForExit(1500)) {
                # This is our keepalive wsl.exe only; never terminate a distribution or process tree.
                try { $process.Kill() } catch [InvalidOperationException] { }
                [void]$process.WaitForExit(1000)
            }
        }
    } catch {
        # Cleanup must not conceal the original startup/integration result.
    } finally { $process.Dispose() }
}

function Start-DDWslKeepAlive {
    param($Paths, [string]$Distro, [int]$ReadyTimeoutMilliseconds, [int]$LifetimeSeconds)
    $process = New-DDWslKeepAliveProcess $Paths $Distro $LifetimeSeconds
    $holder = @{ Process = $process; Started = $false; Stderr = $null }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $marker = 'NEURALCLOUD_DOCKER_WSL_READY_' + [guid]::NewGuid().ToString('N')
    try {
        [void]$process.Start()
        $holder.Started = $true
        $holder.Stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.AutoFlush = $true
        $write = $process.StandardInput.WriteLineAsync($marker)
        $remaining = [Math]::Max(1, $ReadyTimeoutMilliseconds - [int]$watch.ElapsedMilliseconds)
        if (-not $write.Wait($remaining)) { throw 'WSL keepalive marker write timed out.' }
        while ($watch.ElapsedMilliseconds -lt $ReadyTimeoutMilliseconds) {
            $line = $process.StandardOutput.ReadLineAsync()
            $remaining = [Math]::Max(1, $ReadyTimeoutMilliseconds - [int]$watch.ElapsedMilliseconds)
            if (-not $line.Wait($remaining)) { throw 'WSL keepalive readiness timed out.' }
            if ($null -eq $line.Result) { throw 'WSL keepalive exited before returning its readiness marker.' }
            if ($line.Result -eq $marker) {
                if ($process.HasExited) { throw 'WSL keepalive exited during readiness verification.' }
                return $holder
            }
        }
        throw 'WSL keepalive readiness timed out.'
    } catch {
        Stop-DDWslKeepAlive $holder
        throw "WSL prewarm failed for '${Distro}'; Docker startup and runtime recovery were not attempted: $($_.Exception.Message)"
    }
}

function Complete-DDHealthyResult {
    param($Paths, [string]$WslDistro, [int]$TimeoutSeconds, [int]$ProbeTimeoutSeconds,
        [string]$Message, [AllowNull()]$Journal, [switch]$ReadOnly)
    if ([string]::IsNullOrEmpty($WslDistro)) {
        return @{ status = 'healthy'; exit_code = 0; message = $Message; journal = $Journal }
    }
    if ($ReadOnly) {
        return @{ status = 'integration_not_checked'; exit_code = 10; journal = $Journal
            wsl_distro = $WslDistro; integration_status = 'not_checked'
            message = 'The local engine is healthy. WSL integration was not checked in read-only mode because invoking wsl.exe may start a stopped distribution. No startup or filesystem changes were performed.' }
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        do {
            $remaining = [Math]::Max(1, [int]($TimeoutSeconds * 1000 - $watch.ElapsedMilliseconds))
            $probe = Invoke-DDWslProbe $Paths $WslDistro ([Math]::Min($ProbeTimeoutSeconds * 1000, $remaining))
            if ($probe.Healthy) {
                return @{ status = 'healthy'; exit_code = 0; message = "$Message WSL Docker access is ready."
                    journal = $Journal; wsl_distro = $WslDistro; integration_status = 'ready' }
            }
            if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
            Start-Sleep -Milliseconds ([Math]::Min(500, $remaining))
        } while ($true)
    } catch {
        # An integration error must not reach the runtime-isolation/startup retry loop.
    }
    return @{ status = 'integration_not_ready'; exit_code = 30; journal = $Journal
        wsl_distro = $WslDistro; integration_status = 'not_ready'
        message = "The local engine passed its health check, but Docker access in WSL distribution '$WslDistro' did not become ready. Check Docker Desktop WSL integration and the distribution's docker/timeout commands. No additional startup or runtime recovery was attempted." }
}

function Get-DDProcesses {
    @(Get-Process -ErrorAction Stop | Where-Object {
        $_.ProcessName -like 'docker*' -or $_.ProcessName -like 'com.docker.*' -or
        $_.ProcessName -eq 'vpnkit'
    })
}

function Assert-DDPlainDirectoryChain {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $cursor = $full
    while ($cursor) {
        $item = $null
        try {
            $item = Get-Item -Force -LiteralPath $cursor -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] { }
        if ($null -ne $item) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a reparse-point path: $cursor"
            }
            if (-not $item.PSIsContainer) { throw "Expected a directory: $cursor" }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    return $full
}

function Get-DDLogSnapshot {
    param($Paths)
    $snapshot = @{}
    if (Test-Path -LiteralPath $Paths.Logs) {
        $null = Assert-DDPlainDirectoryChain $Paths.Logs
        foreach ($file in Get-ChildItem -LiteralPath $Paths.Logs -File -Filter '*.log') {
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
                $snapshot[$file.FullName] = $file.Length
            }
        }
    }
    return $snapshot
}

function Find-DDNewSocketError {
    param($Paths, [hashtable]$Snapshot, [DateTimeOffset]$StartedAt)
    if (-not (Test-Path -LiteralPath $Paths.Logs)) { return $null }
    $null = Assert-DDPlainDirectoryChain $Paths.Logs
    foreach ($file in Get-ChildItem -LiteralPath $Paths.Logs -File -Filter '*.log') {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $offset = 0L
        if ($Snapshot.ContainsKey($file.FullName) -and $file.Length -ge $Snapshot[$file.FullName]) {
            $offset = [long]$Snapshot[$file.FullName]
        }
        # Read at most the newest MiB; old offsets plus timestamps prevent stale-log repairs.
        $offset = [Math]::Max($offset, $file.Length - 1MB)
        $stream = [IO.File]::Open($file.FullName, 'Open', 'Read', 'ReadWrite, Delete')
        try {
            [void]$stream.Seek($offset, 'Begin')
            $buffer = [byte[]]::new([Math]::Min(1MB, [Math]::Max(0, $file.Length - $offset)))
            $count = $stream.Read($buffer, 0, $buffer.Length)
            $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $count)
        } finally { $stream.Dispose() }
        foreach ($line in $text -split "`n") {
            if ($line -notmatch '\[(?<stamp>\d{4}-\d\d-\d\dT[^\]]+)\]') { continue }
            $stampText = $Matches.stamp -replace '(\.\d{7})\d+', '$1'
            $stamp = [DateTimeOffset]::MinValue
            if (-not [DateTimeOffset]::TryParse($stampText, [ref]$stamp) -or $stamp -lt $StartedAt) { continue }
            $normalized = $line.Replace('\', '/').ToLowerInvariant()
            $approvedSocket = $false
            foreach ($socket in @((Join-Path $Paths.Run 'sailor-ingest.sock'), (Join-Path $Paths.Secrets 'engine.sock'))) {
                $expectedRename = 'rename ' + $socket.Replace('\', '/').ToLowerInvariant() + ' '
                if ($normalized.Contains($expectedRename)) { $approvedSocket = $true }
            }
            if ($approvedSocket -and $line -match 'cannot (be )?accessed|cannot access') {
                return @{ File = $file.FullName; Timestamp = $stamp.ToString('o'); Evidence = $line.Trim() }
            }
        }
    }
    return $null
}

function Start-DDHidden {
    param($Paths)
    if (-not (Test-Path -LiteralPath $Paths.Desktop -PathType Leaf)) {
        throw "Docker Desktop executable is unavailable: $($Paths.Desktop)"
    }
    Start-Process -FilePath $Paths.Desktop -WindowStyle Hidden | Out-Null
}

function Wait-DDState {
    param($Paths, [int]$TimeoutSeconds, [int]$ProbeTimeoutSeconds, $Snapshot, $StartedAt)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $remaining = [Math]::Max(1, [int]($TimeoutSeconds * 1000 - $watch.ElapsedMilliseconds))
        $probe = Invoke-DDProbe $Paths ([Math]::Min($ProbeTimeoutSeconds * 1000, $remaining))
        if ($probe.Healthy) { return @{ State = 'healthy'; Error = $null } }
        if ($null -ne $Snapshot) {
            $fresh = Find-DDNewSocketError $Paths $Snapshot $StartedAt
            if ($null -ne $fresh -and @(Get-DDProcesses).Count -eq 0) {
                return @{ State = 'repairable'; Error = $fresh }
            }
        }
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        Start-Sleep -Milliseconds ([Math]::Min(500, $remaining))
    } while ($true)
    return @{ State = 'timeout'; Error = $null }
}

function Write-DDJournal {
    param([string]$Path, $Record)
    $null = Assert-DDPlainDirectoryChain ([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path, ($Record | ConvertTo-Json -Depth 8))
}

function Backup-DDRuntime {
    param($Paths, $Evidence)
    $local = Assert-DDPlainDirectoryChain $Paths.Local
    $expected = @((Join-Path $local 'Docker\run'), (Join-Path $local 'docker-secrets-engine'))
    $actual = @($Paths.Run, $Paths.Secrets)
    $plan = @()
    # Validate BOTH roots and every ancestor before creating records or moving either root.
    for ($index = 0; $index -lt 2; $index++) {
        $source = Assert-DDPlainDirectoryChain $actual[$index]
        if ($source -ne [IO.Path]::GetFullPath($expected[$index])) { throw 'Runtime directory is not an exact approved root.' }
        $parent = [IO.Path]::GetDirectoryName($source)
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "Missing runtime parent: $parent" }
        $destination = $source + '.misty-backup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
        if ([IO.Path]::GetDirectoryName($destination) -ne $parent -or (Test-Path -LiteralPath $destination)) {
            throw 'Backup destination must be unique and in the same parent.'
        }
        $plan += @{ source = $source; backup = $destination; existed = (Test-Path -LiteralPath $source); phase = 'planned' }
    }
    if (@(Get-DDProcesses).Count -gt 0) { throw 'Docker processes are running; runtime directories were not changed.' }
    $journalDirectory = Assert-DDPlainDirectoryChain $Paths.Journal
    [IO.Directory]::CreateDirectory($journalDirectory) | Out-Null
    $journal = Join-Path $journalDirectory ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N') + '.json')
    $record = @{ schema = 'docker-desktop-recovery-v1'; status = 'in_progress'; evidence = $Evidence; directories = $plan }
    Write-DDJournal $journal $record
    try {
        foreach ($entry in $plan) {
            $null = Assert-DDPlainDirectoryChain $entry.source
            if (@(Get-DDProcesses).Count -gt 0) { throw 'Docker started during recovery; remaining operations stopped.' }
            if ((Test-Path -LiteralPath $entry.source) -ne $entry.existed) { throw 'Runtime directory changed after validation.' }
            if ($entry.existed) {
                Move-Item -LiteralPath $entry.source -Destination $entry.backup -ErrorAction Stop
                $entry.phase = 'backed_up'
                Write-DDJournal $journal $record
            }
            [IO.Directory]::CreateDirectory($entry.source) | Out-Null
            $entry.phase = 'recreated'
            Write-DDJournal $journal $record
        }
        $record.status = 'backed_up_and_recreated'
        Write-DDJournal $journal $record
    } catch {
        $record.status = 'failed_preserved'
        $record.error = $_.Exception.Message
        Write-DDJournal $journal $record
        throw "Recovery stopped; retained all backups and record at ${journal}: $($_.Exception.Message)"
    }
    return $journal
}

function Start-DockerDesktopSafe {
    [CmdletBinding()]
    param([switch]$CheckOnly, [switch]$WhatIf, [switch]$RefreshRuntimeOnColdStart,
        [string]$WslDistro = '',
        [ValidateRange(1, 600)][int]$TimeoutSeconds = 120,
        [ValidateRange(1, 30)][int]$ProbeTimeoutSeconds = 5)
    $mutex = $null
    $owned = $false
    $journal = $null
    $isolationUsed = $false
    $wslKeepAlive = $null
    try {
        $mutex = [Threading.Mutex]::new($false, 'Global\NeuralCloud-DockerDesktop-Recovery-v1')
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw 'Another safe Docker Desktop startup is active.' }
        $paths = Get-DDPaths
        $probe = Invoke-DDProbe $paths ($ProbeTimeoutSeconds * 1000)
        if ($probe.Healthy) {
            return Complete-DDHealthyResult $paths $WslDistro $TimeoutSeconds $ProbeTimeoutSeconds `
                'The local Docker Desktop Linux engine is healthy; no changes made.' $null -ReadOnly:($CheckOnly -or $WhatIf)
        }
        $running = @(Get-DDProcesses).Count -gt 0
        if ($CheckOnly -or $WhatIf) {
            return @{ status = 'not_ready'; exit_code = 10; journal = $null
                message = "Read-only check: engine unavailable; Docker processes present=$running. No startup or filesystem changes performed." }
        }
        if ($running) {
            $state = Wait-DDState $paths $TimeoutSeconds $ProbeTimeoutSeconds $null $null
            if ($state.State -eq 'healthy') {
                return Complete-DDHealthyResult $paths $WslDistro $TimeoutSeconds $ProbeTimeoutSeconds `
                    'Existing Docker Desktop became healthy; no filesystem changes made.' $null
            }
            throw 'Existing Docker processes did not become healthy before timeout. Close Docker Desktop fully and retry; no processes were killed or directories changed.'
        }
        if (-not [string]::IsNullOrEmpty($WslDistro)) {
            $readySeconds = [Math]::Min($TimeoutSeconds, 30)
            # Two startup attempts plus integration readiness, prewarm and bounded probe/cleanup margin.
            $lifetimeSeconds = 3 * $TimeoutSeconds + $readySeconds + 2 * $ProbeTimeoutSeconds + 30
            $wslKeepAlive = Start-DDWslKeepAlive $paths $WslDistro ($readySeconds * 1000) $lifetimeSeconds
        }
        if ($RefreshRuntimeOnColdStart) {
            $journal = Backup-DDRuntime $paths @{
                reason = 'Explicit cold-start workaround for previously verified runtime-socket recurrence'
                timestamp = [DateTimeOffset]::UtcNow.ToString('o')
            }
            $isolationUsed = $true
        }
        for ($attempt = 0; $attempt -le 1; $attempt++) {
            $snapshot = Get-DDLogSnapshot $paths
            $started = [DateTimeOffset]::UtcNow
            if (@(Get-DDProcesses).Count -gt 0) { throw 'Docker started concurrently; this launcher will not modify its runtime.' }
            Start-DDHidden $paths
            $state = Wait-DDState $paths $TimeoutSeconds $ProbeTimeoutSeconds $snapshot $started
            if ($state.State -eq 'healthy') {
                return Complete-DDHealthyResult $paths $WslDistro $TimeoutSeconds $ProbeTimeoutSeconds `
                    'Docker Desktop is healthy.' $journal
            }
            if ($state.State -ne 'repairable' -or $attempt -eq 1 -or $isolationUsed) {
                throw 'Docker startup failed or timed out. No further retry is allowed; current processes, data and backups are preserved.'
            }
            $journal = Backup-DDRuntime $paths $state.Error
            $isolationUsed = $true
        }
    } catch {
        return @{ status = 'needs_action'; exit_code = 20; message = $_.Exception.Message; journal = $journal }
    } finally {
        Stop-DDWslKeepAlive $wslKeepAlive
        if ($owned) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

Export-ModuleMember -Function Start-DockerDesktopSafe
