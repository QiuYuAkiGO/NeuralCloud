#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop\session.json'),
    [ValidateRange(1024, 65535)][int]$Port = 8789
)

Set-StrictMode -Version Latest
$script:QQSRepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

function Get-QQSProperty {
    param([object]$Value, [string]$Name, [object]$Default = $null)
    if ($null -eq $Value) { return $Default }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Resolve-QQSPrivatePath {
    param([string]$Path, [string]$PrivateRoot, [switch]$ExistingFile)
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'private_path_required' }
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($PrivateRoot).TrimEnd('\', '/')
    if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'path_outside_private_root'
    }
    # Inspect the private root and every existing component. Never follow a
    # junction/symlink into another directory for configuration, tokens or logs.
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

function Read-QQSConfiguration {
    param([string]$ConfigPath)
    $anchor = [IO.Path]::GetFullPath($env:LOCALAPPDATA)
    $root = Resolve-QQSPrivatePath -Path (Join-Path $anchor 'NeuralCloud\QQDesktop') -PrivateRoot $anchor
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'private_root_missing' }
    $path = Resolve-QQSPrivatePath -Path $ConfigPath -PrivateRoot $root -ExistingFile
    if ((Get-Item -LiteralPath $path).Length -gt 65536) { throw 'configuration_too_large' }
    $config = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
    $db = Resolve-QQSPrivatePath -Path ([string](Get-QQSProperty $config 'memory_db')) -PrivateRoot $root -ExistingFile
    $tokenPath = Resolve-QQSPrivatePath -Path ([string](Get-QQSProperty $config 'token_file')) -PrivateRoot $root -ExistingFile
    if ((Get-Item -LiteralPath $tokenPath).Length -gt 16384) { throw 'token_file_invalid' }
    $token = [IO.File]::ReadAllText($tokenPath, [Text.Encoding]::UTF8).Trim()
    if ($token.Length -lt 32 -or $token.Length -gt 4096 -or $token -match '\s') { throw 'token_file_invalid' }
    $groups = @(Get-QQSProperty $config 'allowed_group_ids')
    if ($groups.Count -lt 1 -or $groups.Count -gt 20) { throw 'group_scope_invalid' }
    foreach ($group in $groups) {
        if ($group -isnot [string] -or $group -notmatch '^[1-9][0-9]{0,19}$') { throw 'group_scope_invalid' }
    }
    $self = Get-QQSProperty $config 'self_member_id'
    if ($self -isnot [string] -or $self -notmatch '^[1-9][0-9]{0,19}$') { throw 'self_identity_invalid' }
    $decision = Get-QQSProperty $config 'decision_enabled' $true
    if ($decision -isnot [bool]) { throw 'decision_setting_invalid' }
    $development = Get-QQSProperty $config 'development_mode' $false
    if ($development -isnot [bool]) { throw 'development_setting_invalid' }
    # The token stays in this private local object. Never serialize this object.
    return [pscustomobject]@{
        PrivateRoot = $root; Path = $path; MemoryDb = $db; Token = $token
        AllowedGroupIds = @($groups | Sort-Object -Unique); SelfMemberId = $self; DecisionEnabled = $decision
        DevelopmentMode = $development
    }
}

function Get-QQSHealth {
    param([int]$Port, [string]$Token, [int]$TimeoutMilliseconds = 750)
    $request = $null
    $response = $null
    $stream = $null
    $buffer = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/v1/status")
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = [Math]::Max(1, $TimeoutMilliseconds)
        $request.ReadWriteTimeout = [Math]::Max(1, $TimeoutMilliseconds)
        $request.Headers['Authorization'] = 'Bearer ' + $Token
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt 65536) { return $null }
        $stream = $response.GetResponseStream()
        $buffer = New-Object IO.MemoryStream
        $bytes = New-Object byte[] 4096
        while ($true) {
            $remaining = $TimeoutMilliseconds - [int]$clock.ElapsedMilliseconds
            if ($remaining -le 0) { return $null }
            if ($stream.CanTimeout) { $stream.ReadTimeout = [Math]::Max(1, $remaining) }
            $count = $stream.Read($bytes, 0, $bytes.Length)
            if ($count -eq 0) { break }
            if ($buffer.Length + $count -gt 65536) { return $null }
            $buffer.Write($bytes, 0, $count)
        }
        return ([Text.Encoding]::UTF8.GetString($buffer.ToArray()) | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        # Authentication/HTTP/JSON errors can carry private data. Return only
        # absence of a verified status, never exception bodies or headers.
        return $null
    } finally {
        if ($null -ne $buffer) { $buffer.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Close() }
        if ($null -ne $request) { $request.Abort() }
    }
}

function Test-QQSMatchingHealth {
    param([object]$Health, [object]$Configuration)
    if ((Get-QQSProperty $Health 'service') -cne 'qq-memory-session-v1' -or
        (Get-QQSProperty $Health 'status') -cne 'healthy' -or
        (Get-QQSProperty $Health 'persistent_memory') -isnot [bool] -or
        (Get-QQSProperty $Health 'persistent_memory') -ne $true) { return $false }
    $decision = Get-QQSProperty $Health 'configured_decision_enabled'
    if ($decision -isnot [bool] -or $decision -ne $Configuration.DecisionEnabled) { return $false }
    if ((Get-QQSProperty $Health 'autonomous_decision_enabled') -isnot [bool]) { return $false }
    $development = Get-QQSProperty $Health 'development_mode'
    if ($development -isnot [bool] -or $development -ne $Configuration.DevelopmentMode) { return $false }
    $expectedOwner = if ($Configuration.DevelopmentMode) { 'isolated_development' } else { 'shared_runtime' }
    if ((Get-QQSProperty $Health 'cognitive_owner') -cne $expectedOwner) { return $false }
    if ((Get-QQSProperty $Health 'self_member_id') -cne $Configuration.SelfMemberId) { return $false }
    $healthDb = Get-QQSProperty $Health 'memory_db'
    if ($healthDb -isnot [string] -or -not [IO.Path]::IsPathRooted($healthDb)) { return $false }
    try { $healthDb = [IO.Path]::GetFullPath($healthDb) } catch { return $false }
    if (-not $healthDb.Equals($Configuration.MemoryDb, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    $groups = @(Get-QQSProperty $Health 'allowed_group_ids')
    foreach ($group in $groups) { if ($group -isnot [string]) { return $false } }
    $groups = @($groups | Sort-Object -Unique)
    if ($groups.Count -ne $Configuration.AllowedGroupIds.Count) { return $false }
    for ($index = 0; $index -lt $groups.Count; $index++) {
        if ($groups[$index] -cne $Configuration.AllowedGroupIds[$index]) { return $false }
    }
    foreach ($field in @('automatic_send', 'automatic_screen_capture')) {
        if ((Get-QQSProperty $Health $field) -isnot [bool]) { return $false }
    }
    foreach ($field in @('standard_runtime_started_by_this_service')) {
        $value = Get-QQSProperty $Health $field
        if ($value -isnot [bool] -or $value -ne $false) { return $false }
    }
    return $true
}

function Test-QQSPortListening {
    param([int]$Port)
    $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0
}

function Get-QQSListenerPid {
    param([int]$Port)
    try {
        $owners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -eq 1) { return [int]$owners[0] }
    } catch { }
    return $null
}

function ConvertTo-QQSArgument {
    param([string]$Value)
    # Windows C runtime argv quoting, including runs of backslashes before a
    # quote and before the closing quote. Start-Process otherwise joins args.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Start-QQSProcess {
    param([string]$PythonPath, [string]$ConfigPath, [int]$Port, [string]$StdoutPath, [string]$StderrPath)
    $arguments = @('-m', 'harness.qq.session', '--config', $ConfigPath, '--port', [string]$Port)
    $argumentLine = ($arguments | ForEach-Object { ConvertTo-QQSArgument $_ }) -join ' '
    # The caller's environment and working directory are never changed. The
    # child receives an explicit repository working directory, no token argv.
    return Start-Process -FilePath $PythonPath -ArgumentList $argumentLine `
        -WorkingDirectory $script:QQSRepositoryRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -ErrorAction Stop
}

function New-QQSResult {
    param([string]$Status, [int]$ExitCode, [int]$Port, [string]$Reason = '', [object]$Process = $null,
          [object]$Health = $null, [string]$StdoutPath = '', [string]$StderrPath = '')
    return [pscustomobject][ordered]@{
        service = 'qq-memory-session-v1'; status = $Status; exit_code = $ExitCode; port = $Port
        reason = $Reason; launch_pid = $(if ($null -ne $Process) { $Process.Id } else { $null })
        listener_pid = $(if ($Status -in @('started', 'already_running')) { Get-QQSListenerPid $Port } else { $null })
        started_at = $(if ($null -ne $Health) { Get-QQSProperty $Health 'started_at' } else { $null })
        stdout_log = $StdoutPath; stderr_log = $StderrPath
    }
}

function Invoke-QQSessionStart {
    [CmdletBinding()]
    param([string]$ConfigPath, [ValidateRange(1024, 65535)][int]$Port = 8789,
          [ValidateRange(0.01, 20.0)][double]$TimeoutSeconds = 20.0)
    $mutex = $null
    $locked = $false
    $process = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $stdoutPath = ''
    $stderrPath = ''
    try {
        $configuration = Read-QQSConfiguration -ConfigPath $ConfigPath
        $python = Join-Path $script:QQSRepositoryRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'repository_python_missing' }
        $mutex = New-Object Threading.Mutex($false, "Local\NeuralCloudQQSession-$Port")
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { return New-QQSResult 'launch_in_progress' 10 $Port 'another_launcher_owns_the_port_lock' }
        $remaining = [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds)
        if ($remaining -le 0) { return New-QQSResult 'failed' 20 $Port 'startup_deadline_reached' }
        $health = Get-QQSHealth -Port $Port -Token $configuration.Token -TimeoutMilliseconds ([Math]::Min(750, $remaining))
        if (Test-QQSMatchingHealth $health $configuration) {
            return New-QQSResult 'already_running' 0 $Port -Health $health
        }
        if (Test-QQSPortListening $Port) {
            return New-QQSResult 'failed' 20 $Port 'port_occupied_by_unverified_or_different_service'
        }
        $logs = Resolve-QQSPrivatePath -Path (Join-Path $configuration.PrivateRoot 'logs') -PrivateRoot $configuration.PrivateRoot
        [void][IO.Directory]::CreateDirectory($logs)
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
        $stdoutPath = Join-Path $logs ("session-$stamp.stdout.log")
        $stderrPath = Join-Path $logs ("session-$stamp.stderr.log")
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            return New-QQSResult 'failed' 20 $Port 'startup_deadline_reached'
        }
        $process = Start-QQSProcess -PythonPath $python -ConfigPath $configuration.Path -Port $Port `
            -StdoutPath $stdoutPath -StderrPath $stderrPath
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $remaining = [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds)
            if ($remaining -le 0) { break }
            $health = Get-QQSHealth -Port $Port -Token $configuration.Token -TimeoutMilliseconds ([Math]::Min(750, $remaining))
            if (Test-QQSMatchingHealth $health $configuration) {
                return New-QQSResult 'started' 0 $Port -Process $process -Health $health -StdoutPath $stdoutPath -StderrPath $stderrPath
            }
            if ($process.HasExited) {
                return New-QQSResult 'failed' 20 $Port 'launched_process_exited_before_authenticated_health' `
                    -Process $process -StdoutPath $stdoutPath -StderrPath $stderrPath
            }
            $remaining = [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds)
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(200, $remaining)) }
        }
        # A late process is reported for the operator; never stop a port owner or
        # guess that a failed health check authorizes a process termination.
        return New-QQSResult 'failed' 20 $Port 'authenticated_health_deadline_reached' `
            -Process $process -StdoutPath $stdoutPath -StderrPath $stderrPath
    } catch {
        # Do not reveal exception messages: JSON parsing can embed private input.
        return New-QQSResult 'failed' 20 $Port 'configuration_or_launch_error' `
            -Process $process -StdoutPath $stdoutPath -StderrPath $stderrPath
    } finally {
        if ($locked -and $null -ne $mutex) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
        if ($null -ne $process -and $process -is [Diagnostics.Process]) { $process.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQSessionStart -ConfigPath $ConfigPath -Port $Port
    $result | ConvertTo-Json -Depth 4 -Compress
    exit $result.exit_code
}
