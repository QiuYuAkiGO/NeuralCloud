#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop\watcher.json'),
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
)

# Import only function definitions; the Start script never runs when dot-sourced.
. (Join-Path $PSScriptRoot 'Start-QQWatcher.ps1') -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds

function Invoke-QQWatcherStop {
    param([string]$ConfigPath, [ValidateRange(0.05, 120)][double]$TimeoutSeconds=20)
    $mutex = $null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $configuration = Read-QQWConfiguration $ConfigPath
        # Cancellation must reach the worker even while Start holds its mutex
        # waiting for a slow initial vision call, or a later identity probe fails.
        Set-QQWPaused $configuration $true
        $interpreters = @(Get-QQWInterpreterPaths)
        $mutex = New-QQWLauncherMutex
        while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            $candidates = @(Get-QQWProcesses)
            $sameConfig = @($candidates | Where-Object { Test-QQWProcessConfiguration $_ $configuration })
            $owned = @($candidates | Where-Object { Test-QQWProcessIdentity $_ $configuration $interpreters })
            if ($sameConfig.Count -ne $owned.Count) {
                return New-QQWResult 'pause_requested' 10 'matching_config_has_unverified_process_identity'
            }
            if ($owned.Count -eq 0) {
                # A launcher may be between clearing the old pause and spawning.
                # Probe its lock without delaying the already-written request.
                $locked = $false
                try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
                if ($locked) {
                    $mutex.ReleaseMutex()
                    return New-QQWResult 'stopped' 0 'no_matching_watcher_process'
                }
            }
            $status = Read-QQWJson $configuration.StatusFile
            if (Test-QQWStatus $status $owned $configuration $interpreters -Stopped) {
                return New-QQWResult 'paused' 0 'watcher_acknowledged_pause'
            }
            $remaining = [int]($TimeoutSeconds * 1000 - $clock.ElapsedMilliseconds)
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(200, $remaining)) }
        }
        return New-QQWResult 'pause_requested' 10 'watcher_has_not_acknowledged_pause'
    } catch {
        return New-QQWResult 'failed' 20 'pause_or_identity_check_error'
    } finally {
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-QQWatcherStop -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds
    $result | ConvertTo-Json -Depth 4 -Compress
    exit $result.exit_code
}
