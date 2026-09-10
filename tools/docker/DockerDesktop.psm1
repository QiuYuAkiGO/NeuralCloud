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
        Journal = Join-Path $PSScriptRoot '..\..\logs\docker-recovery'
        Docker = Join-Path $install 'resources\bin\docker.exe'
        Desktop = Join-Path $install 'Docker Desktop.exe'
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
        [ValidateRange(1, 600)][int]$TimeoutSeconds = 120,
        [ValidateRange(1, 30)][int]$ProbeTimeoutSeconds = 5)
    $mutex = $null
    $owned = $false
    $journal = $null
    $isolationUsed = $false
    try {
        $mutex = [Threading.Mutex]::new($false, 'Global\NeuralCloud-DockerDesktop-Recovery-v1')
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw 'Another safe Docker Desktop startup is active.' }
        $paths = Get-DDPaths
        $probe = Invoke-DDProbe $paths ($ProbeTimeoutSeconds * 1000)
        if ($probe.Healthy) {
            return @{ status = 'healthy'; exit_code = 0; message = 'The local Docker Desktop Linux engine is healthy; no changes made.'; journal = $null }
        }
        $running = @(Get-DDProcesses).Count -gt 0
        if ($CheckOnly -or $WhatIf) {
            return @{ status = 'not_ready'; exit_code = 10; journal = $null
                message = "Read-only check: engine unavailable; Docker processes present=$running. No startup or filesystem changes performed." }
        }
        if ($running) {
            $state = Wait-DDState $paths $TimeoutSeconds $ProbeTimeoutSeconds $null $null
            if ($state.State -eq 'healthy') {
                return @{ status = 'healthy'; exit_code = 0; message = 'Existing Docker Desktop became healthy; no filesystem changes made.'; journal = $null }
            }
            throw 'Existing Docker processes did not become healthy before timeout. Close Docker Desktop fully and retry; no processes were killed or directories changed.'
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
                return @{ status = 'healthy'; exit_code = 0; message = 'Docker Desktop is healthy.'; journal = $journal }
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
        if ($owned) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

Export-ModuleMember -Function Start-DockerDesktopSafe
