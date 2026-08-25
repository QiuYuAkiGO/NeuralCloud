# 停止 NeuralCloud GUI 服务（python -m gui.server，默认端口 8765）
param(
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".gui.pid"
$Port = 8765

function Write-Info([string]$Message) {
    if (-not $Quiet) {
        Write-Host $Message
    }
}

function Get-ServicePids {
    $ids = New-Object "System.Collections.Generic.HashSet[int]"

    if (Test-Path $PidFile) {
        $raw = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($raw -and ($raw.Trim() -match '^\d+$')) {
            $candidate = [int]$raw.Trim()
            if (Get-Process -Id $candidate -ErrorAction SilentlyContinue) {
                [void]$ids.Add($candidate)
            }
        }
    }

    $netstat = netstat -ano 2>$null
    if ($netstat) {
        foreach ($line in $netstat) {
            $text = "$line"
            if ($text -notmatch "LISTENING") { continue }
            if ($text -notmatch ":$Port\s") { continue }
            $parts = @($text -split "\s+" | Where-Object { $_ })
            if ($parts.Count -lt 2) { continue }
            $owner = $parts[-1]
            if ($owner -match "^\d+$" -and [int]$owner -gt 4) {
                [void]$ids.Add([int]$owner)
            }
        }
    }

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "^python(w)?\.exe$" -and
            $_.CommandLine -and
            $_.CommandLine -match "gui\.server|-m gui"
        } |
        ForEach-Object { [void]$ids.Add([int]$_.ProcessId) }

    return @($ids)
}

$pids = @(Get-ServicePids)
if ($pids.Count -eq 0) {
    Write-Info "服务未在运行。"
    if (Test-Path $PidFile) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

foreach ($id in $pids) {
    Write-Info "正在停止进程 PID=$id ..."
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds(10)
do {
    Start-Sleep -Milliseconds 200
    $left = @(Get-ServicePids)
} while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)

if (Test-Path $PidFile) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$left = @(Get-ServicePids)
if ($left.Count -gt 0) {
    Write-Info "未能完全停止服务，残留 PID: $($left -join ', ')"
    exit 1
}

Write-Info "服务已停止。"
exit 0
