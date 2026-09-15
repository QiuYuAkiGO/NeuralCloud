#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Position = 0)][ValidateSet('up', 'down', 'restart', 'status', 'check')]
    [string]$Command = 'up',
    [string]$ConfigPath,
    [string]$Profile,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
try {
    if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'launch.json' }
    Import-Module (Join-Path $PSScriptRoot 'tools\launcher\OneClick.psm1') -Force
    $result = Invoke-OneClickLaunch -Root $PSScriptRoot -ConfigPath $ConfigPath -Profile $Profile `
        -Command $Command -NoOpen:$NoOpen
    Write-Host "NeuralCloud: $($result.status)"
    if ($result.url) { Write-Host "GUI: $($result.url)" }
    Write-Host "Configuration: $($result.config_path) [$($result.profile)]"
    exit 0
} catch {
    Write-Host "Launch failed: $($_.Exception.Message)"
    Write-Host 'Edit launch.json, then double-click start.cmd again. No system execution policy change is needed.'
    exit 1
}
