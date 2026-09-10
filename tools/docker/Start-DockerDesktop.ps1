#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$WhatIf,
    [switch]$RefreshRuntimeOnColdStart,
    [switch]$Json,
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 120,
    [ValidateRange(1, 30)][int]$ProbeTimeoutSeconds = 5
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'DockerDesktop.psm1') -Force
$result = Start-DockerDesktopSafe -CheckOnly:$CheckOnly -WhatIf:$WhatIf `
    -RefreshRuntimeOnColdStart:$RefreshRuntimeOnColdStart `
    -TimeoutSeconds $TimeoutSeconds -ProbeTimeoutSeconds $ProbeTimeoutSeconds
if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    Write-Host "Docker Desktop: $($result.status)"
    Write-Host $result.message
    if ($result.journal) { Write-Host "Recovery record: $($result.journal)" }
}
exit $result.exit_code
