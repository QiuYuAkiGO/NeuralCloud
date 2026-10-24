#Requires -Version 5.1
param(
    [ValidateSet('start', 'run', 'status', 'stop')][string]$Command = 'status',
    [string]$RemoteHost,
    [string]$User,
    [int]$SshPort = 22,
    [int]$RemotePort = 18080,
    [int]$LocalPort = 8080
)
$ErrorActionPreference = 'Stop'
$stateRoot = Join-Path $env:LOCALAPPDATA 'NeuralCloud\AgenticTunnel'
$stateFile = Join-Path $stateRoot 'process.json'
if ($Command -in @('status', 'stop')) {
    if (-not (Test-Path -LiteralPath $stateFile)) { Write-Output 'not running'; exit 0 }
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)" -ErrorAction SilentlyContinue
    $owned = $process -and $process.CommandLine -like '*agentic-tunnel.ps1*' -and $process.CommandLine -like '*-Command run*'
    if ($Command -eq 'status') { @{ running = [bool]$owned; pid = $state.pid; remote_port = $state.remote_port } | ConvertTo-Json; exit 0 }
    if ($owned) {
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$($state.pid)" | Where-Object { $_.Name -eq 'ssh.exe' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
        Stop-Process -Id $state.pid -ErrorAction SilentlyContinue
    }
    exit 0
}
if ($RemoteHost -notmatch '^[A-Za-z0-9.-]+$' -or $User -notmatch '^[A-Za-z0-9_-]+$') { throw 'A valid SSH host and user are required.' }
foreach ($port in @($SshPort, $RemotePort, $LocalPort)) { if ($port -lt 1024 -or $port -gt 65535) { if ($port -ne 22) { throw 'Invalid port.' } } }
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
if ($Command -eq 'start') {
    if (Test-Path -LiteralPath $stateFile) {
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)" -ErrorAction SilentlyContinue
        if ($process -and $process.CommandLine -like '*agentic-tunnel.ps1*') { throw 'An agentic tunnel worker is already running.' }
    }
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Command', 'run',
        '-RemoteHost', $RemoteHost, '-User', $User, '-SshPort', $SshPort, '-RemotePort', $RemotePort, '-LocalPort', $LocalPort)
    $worker = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
    @{ pid = $worker.Id; remote_port = $RemotePort; local_port = $LocalPort } | ConvertTo-Json | Set-Content -LiteralPath $stateFile
    Write-Output "Tunnel worker started: $($worker.Id)"
    exit 0
}
# Key authentication only. Both ends bind loopback; the model is never exposed
# on a public interface. Reconnect when SSH exits, including after LAN changes.
while ($true) {
    $arguments = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-R', "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}",
        '-p', $SshPort, "$User@$RemoteHost")
    $ssh = Start-Process ssh.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Wait-Process -Id $ssh.Id -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
}
