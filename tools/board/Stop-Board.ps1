#Requires -Version 5.1
[CmdletBinding()]
param([string]$Distro = 'Ubuntu-24.04')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Board.psm1') -Force
$boardWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Invoke-BoardLinux -Distro $Distro -Workspace $boardWorkspace -Action stop | ConvertTo-Json -Depth 8
