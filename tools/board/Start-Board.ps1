#Requires -Version 5.1
[CmdletBinding()]
param([string]$Distro = 'Ubuntu-24.04')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Board.psm1') -Force
$boardWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$boardResult = Invoke-BoardLinux -Distro $Distro -Workspace $boardWorkspace -Action start
$boardConfig = Save-BoardClientConfiguration -Distro $Distro -LinuxResult $boardResult -Workspace $boardWorkspace
[pscustomobject]@{
    action = $boardResult.action; active = $boardResult.active; board_id = $boardResult.board_id
    body_instance_id = $boardResult.body_instance_id; base_url = 'http://127.0.0.1:8793'
    config_path = $boardConfig; state_dir = $boardResult.state_dir
} | ConvertTo-Json
