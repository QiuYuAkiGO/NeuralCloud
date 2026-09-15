#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect\direct.json'),
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 20,
    [switch]$CheckOnly
)
. (Join-Path $PSScriptRoot 'Start-QQDirect.ps1') -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds -CheckOnly:$CheckOnly -WhatIf:$WhatIfPreference

function Invoke-QQDirectStop {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$ConfigPath, [int]$TimeoutSeconds=20, [switch]$CheckOnly)
    $mutex = $null; $locked = $false
    try {
        $configuration = Read-QQDConfiguration $ConfigPath
        $interpreters = @(Get-QQWInterpreterPaths)
        $processes = @(Get-QQDProcesses)
        $registry = Read-QQWJson $configuration.Registry
        if ($processes.Count -gt 0 -and -not (Test-QQDRegistry $registry $configuration $processes $interpreters)) {
            return New-QQDResult 'failed' 20 'existing_direct_process_identity_unverified'
        }
        if ($CheckOnly -or -not $PSCmdlet.ShouldProcess($configuration.Path, 'Request graceful QQDirect exit, then stop only the verified owned process if needed')) {
            return New-QQDResult 'checked' 0 'no_control_or_process_changed'
        }
        Set-QQDControl $configuration $true
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $mutex = New-Object Threading.Mutex($false, 'Local\NeuralCloudQQDirectLauncher')
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
            if ($locked) { break }
            Start-Sleep -Milliseconds 100
        }
        # Reassert after a concurrent launch may have cleared its old stop flag.
        Set-QQDControl $configuration $true
        if (-not $locked) { return New-QQDResult 'stop_requested' 10 'launcher_still_busy' }
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $processes = @(Get-QQDProcesses)
            if ($processes.Count -eq 0) { return New-QQDResult 'stopped' 0 'graceful_exit_or_already_stopped' }
            $registry = Read-QQWJson $configuration.Registry
            if (-not (Test-QQDRegistry $registry $configuration $processes $interpreters)) {
                return New-QQDResult 'stop_requested' 10 'process_identity_changed_no_force_stop'
            }
            Start-Sleep -Milliseconds 200
        }
        $status = Read-QQWJson $configuration.StatusFile
        if (-not (Test-QQDStatus $status $configuration $processes ([string](Get-QQWProperty $registry 'source_hash')) -AllowStaleHeartbeat)) {
            return New-QQDResult 'stop_requested' 10 'runtime_source_or_root_identity_unverified_no_force_stop'
        }
        # Re-query every PID immediately before stopping it, including creation
        # time and parent relation, so a recycled PID is never targeted.
        foreach ($candidate in @($processes | Sort-Object CreatedAt -Descending)) {
            $fresh = @(Get-QQDProcesses)
            if (-not (Test-QQDRegistry $registry $configuration $fresh $interpreters)) {
                return New-QQDResult 'stop_requested' 10 'process_identity_changed_no_force_stop'
            }
            $match = @($fresh | Where-Object { $_.ProcessId -eq $candidate.ProcessId })
            if ($match.Count -eq 1 -and $match[0].CreatedAt -eq $candidate.CreatedAt) {
                Stop-Process -Id $candidate.ProcessId -Force -ErrorAction Stop
            }
        }
        if (@(Get-QQDProcesses).Count -eq 0) { return New-QQDResult 'stopped' 0 'verified_process_terminated_after_grace_period' }
        return New-QQDResult 'stop_requested' 10 'process_exit_not_yet_confirmed'
    } catch { return New-QQDResult 'failed' 20 'direct_stop_configuration_or_identity_error' }
    finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQDirectStop -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds -CheckOnly:$CheckOnly -WhatIf:$WhatIfPreference
    $result | ConvertTo-Json -Compress
    exit $result.exit_code
}
