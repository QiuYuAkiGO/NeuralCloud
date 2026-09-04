#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "up",

    [string]$MistyAIPath,
    [string]$Distro = "Ubuntu-24.04",

    [string]$Mode = "demo",

    [switch]$NonInteractive,
    [switch]$NoOpen,
    [switch]$Json,
    [string]$Service = "all"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Exit-LauncherConfigurationError([string]$Message) {
    if ($Json) {
        [pscustomobject]@{
            schema_version = "neuralcloud-launcher-result-v1"
            command = $Command
            status = "failed"
            exit_code = 40
            error = $Message
        } | ConvertTo-Json -Depth 6
    } else {
        Write-Host $Message
    }
    exit 40
}

if ($Command -notin @("up", "down", "restart", "status", "doctor", "install", "logs")) {
    Exit-LauncherConfigurationError "Unknown launcher command '$Command'. Valid commands: up, down, restart, status, doctor, install, logs."
}
if ($Mode -notin @("demo", "live", "ollama")) {
    Exit-LauncherConfigurationError "Unknown NeuralCloud mode '$Mode'. Valid modes: demo, live, ollama."
}

if (-not $MistyAIPath) {
    $MistyAIPath = Join-Path $PSScriptRoot "..\MistyAI"
}

$modulePath = Join-Path $PSScriptRoot "tools\launcher\Launcher.psm1"
if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
    $message = "Launcher module is missing: $modulePath"
    if ($Json) {
        [pscustomobject]@{
            schema_version = "neuralcloud-launcher-result-v1"
            command = $Command
            status = "failed"
            exit_code = 40
            error = $message
        } | ConvertTo-Json -Depth 6
    } else {
        Write-Host $message
    }
    exit 40
}

try {
    Import-Module -Name $modulePath -Force -ErrorAction Stop
    $exitCode = Invoke-NeuralCloudLauncher `
        -Command $Command `
        -Root $PSScriptRoot `
        -MistyAIPath $MistyAIPath `
        -Distro $Distro `
        -Mode $Mode `
        -NonInteractive:$NonInteractive `
        -NoOpen:$NoOpen `
        -Json:$Json `
        -Service $Service
    exit [int]$exitCode
} catch {
    $message = $_.Exception.Message
    $redactor = Get-Command Protect-LauncherText -ErrorAction SilentlyContinue
    if ($redactor) {
        $message = Protect-LauncherText -Text $message
    }
    if ($Json) {
        [pscustomobject]@{
            schema_version = "neuralcloud-launcher-result-v1"
            command = $Command
            status = "failed"
            exit_code = 40
            error = $message
        } | ConvertTo-Json -Depth 6
    } else {
        Write-Host "Launcher failed: $message"
    }
    exit 40
}
