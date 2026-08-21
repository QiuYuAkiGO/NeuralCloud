# 启动 NeuralCloud GUI 服务。若已在运行则先停止再启动（重启）。
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$PidFile = Join-Path $Root ".gui.pid"
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "gui.out.log"
$ErrLog = Join-Path $LogDir "gui.err.log"
$Port = 8765
$Url = "http://127.0.0.1:$Port"

Set-Location -LiteralPath $Root

function Test-ServiceRunning {
    if (Test-Path $PidFile) {
        $raw = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($raw -and ($raw.Trim() -match "^\d+$")) {
            if (Get-Process -Id ([int]$raw.Trim()) -ErrorAction SilentlyContinue) {
                return $true
            }
        }
    }

    $netstat = netstat -ano 2>$null
    if ($netstat) {
        foreach ($line in $netstat) {
            $text = "$line"
            if ($text -match "LISTENING" -and $text -match ":$Port\s") {
                return $true
            }
        }
    }

    $match = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "^python(w)?\.exe$" -and
            $_.CommandLine -and
            $_.CommandLine -match "gui\.server|-m gui"
        } |
        Select-Object -First 1
    return [bool]$match
}

function Get-PythonExe {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

function Wait-PortListening([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $netstat = netstat -ano 2>$null
        if ($netstat) {
            foreach ($line in $netstat) {
                $text = "$line"
                if ($text -match "LISTENING" -and $text -match ":$Port\s") {
                    return $true
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

if (Test-ServiceRunning) {
    Write-Host "检测到服务已启动，正在重启..."
    & (Join-Path $Root "stop.ps1") -Quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "重启失败：无法停止已有进程。"
        exit 1
    }
}

$python = Get-PythonExe
if (-not $python) {
    Write-Host "未找到 Python。请先创建虚拟环境：python -m venv .venv"
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

foreach ($log in @($OutLog, $ErrLog)) {
    if (Test-Path $log) {
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "正在启动服务: $python -m gui.server"
$proc = Start-Process -FilePath $python `
    -ArgumentList "-m", "gui.server" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding ascii

if (-not (Wait-PortListening -Seconds 15)) {
    $alive = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if (-not $alive) {
        Write-Host "启动失败，进程已退出。请查看日志："
        Write-Host "  $ErrLog"
        if (Test-Path $PidFile) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
        exit 1
    }
    Write-Host "进程已启动（PID=$($proc.Id)），但端口 $Port 尚未就绪，请稍后打开 $Url"
    Write-Host "日志: $OutLog"
    exit 0
}

Write-Host "服务已启动  PID=$($proc.Id)"
Write-Host "打开 $Url"
Write-Host "日志: $OutLog"
exit 0
