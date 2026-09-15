#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Check','Pause','Cognitive','Runtime','Resume')][string]$Phase = 'Check',
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory
)
# Load the new decision DTO in both peers, preserving the previous actor pause.
$ErrorActionPreference = 'Stop'
$qqMemoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Set-Location -LiteralPath $qqMemoryRoot
. (Join-Path $PSScriptRoot 'Stop-QQDirect.ps1')
$qqRegistryPath = Join-Path $qqMemoryRoot '.neuralcloud.pids.json'
$qqRegistry = Read-QQWJson $qqRegistryPath
if ($qqRegistry.root -cne $qqMemoryRoot) { throw 'qq_memory_repository_identity_mismatch' }
$qqBindings = Read-QQWJson (Join-Path $qqMemoryRoot 'data\shared-misty-launch.json')
$qqConfigPath = Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect\direct.json'
$qqConfig = Read-QQDConfiguration $qqConfigPath
$qqEvidence = [IO.Path]::GetFullPath($EvidenceDirectory)
if (-not (Test-Path -LiteralPath $qqEvidence -PathType Container)) { throw 'qq_memory_evidence_directory_missing' }
$qqStatePath = Join-Path $qqEvidence 'deployment.json'
$qqState = Read-QQWJson $qqStatePath
$qqServiceHeaders = @{Authorization='Bearer ' + [IO.File]::ReadAllText($qqRegistry.service_token_file).Trim()}
$qqControlHeaders = @{Authorization='Bearer ' + [IO.File]::ReadAllText($qqRegistry.control_token_file).Trim()}
$qqInterpreters = @(Get-QQWInterpreterPaths)

function Get-QQMemoryServiceIdentity([string]$Name) {
    $entry = @($qqRegistry.services | Where-Object name -eq $Name)
    if ($entry.Count -ne 1 -or $entry[0].root -cne $qqMemoryRoot) { throw 'qq_memory_registry_identity_invalid' }
    $entry = $entry[0]
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.pid)"
    if (-not $parent -or $parent.ExecutablePath -cne $qqRegistry.python_path -or
        [math]::Abs(($parent.CreationDate.ToUniversalTime() - ([datetimeoffset]$entry.process_start_time_utc).UtcDateTime).TotalSeconds) -gt 1) { throw 'qq_memory_service_identity_changed' }
    $args = @(Split-QQWCommandLine $parent.CommandLine)
    if ($args.Count -ne 3 -or $args[1] -cne '-m' -or $args[2] -cne $entry.module) { throw 'qq_memory_service_arguments_changed' }
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($entry.pid)" | Where-Object { $_.ExecutablePath -in $qqInterpreters })
    if ($children.Count -ne 1) { throw 'qq_memory_service_child_unverified' }
    $childArgs = @(Split-QQWCommandLine $children[0].CommandLine)
    if ($childArgs.Count -ne 3 -or $childArgs[1] -cne '-m' -or $childArgs[2] -cne $entry.module) { throw 'qq_memory_child_arguments_changed' }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $entry.port -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listeners.Count -ne 1 -or $listeners[0] -ne $children[0].ProcessId) { throw 'qq_memory_service_listener_changed' }
    return @{entry=$entry; parent=$parent; child=$children[0]}
}

if ($Phase -eq 'Check') {
    foreach ($name in @('cognitive','runtime')) { $null = Get-QQMemoryServiceIdentity $name }
    $direct = Invoke-QQDirectStop -ConfigPath $qqConfigPath -CheckOnly
    if ($direct.exit_code -ne 0) { throw 'qq_memory_direct_identity_unverified' }
    Write-Output 'QQ memory reload identities verified; no changes made.'
    return
}
if ($Phase -eq 'Pause') {
    foreach ($name in @('cognitive','runtime')) { $null = Get-QQMemoryServiceIdentity $name }
    if (-not $qqState) {
        $runtime = Invoke-RestMethod 'http://127.0.0.1:8770/v1/health' -Headers $qqControlHeaders -TimeoutSec 8
        $directControl = Read-QQWJson $qqConfig.ControlFile
        $qqState = @{schema_version='qq-memory-deployment-v1'; started_at=[datetime]::UtcNow.ToString('o'); phase='pausing'
            direct_paused_before=((Get-QQWProperty $directControl 'paused' $true) -ne $false)
            runtime_lifecycle_before=$runtime.lifecycle; reloaded=@()}
        Write-QQDJson $qqStatePath $qqState
    }
    $stopped = Invoke-QQDirectStop -ConfigPath $qqConfigPath -TimeoutSeconds 90
    if ($stopped.exit_code -ne 0) { throw 'qq_memory_direct_not_drained' }
    Invoke-RestMethod -Method Post 'http://127.0.0.1:8770/v1/pause' -Headers $qqControlHeaders -ContentType 'application/json' -Body '{"reason":"authorized scoped QQ memory update"}' -TimeoutSec 10 | Out-Null
    $drained = Invoke-RestMethod -Method Post 'http://127.0.0.1:8771/v1/maintenance/pause' -Headers $qqServiceHeaders -ContentType 'application/json' -Body '{"timeout":10}' -TimeoutSec 15
    if (-not $drained.drained) { throw 'qq_memory_cognitive_not_drained' }
    $qqState.phase='paused'; Write-QQDJson $qqStatePath $qqState
    Write-Output 'QQ actor and Cognitive maintenance drained; preserved prior pause state.'
    return
}
if (-not $qqState) { throw 'qq_memory_deployment_pause_record_missing' }
if ($Phase -in @('Cognitive','Runtime')) {
    if ($qqState.phase -notin @('paused','cognitive','runtime')) { throw 'qq_memory_deployment_not_paused' }
    $name = $Phase.ToLowerInvariant()
    $identity = Get-QQMemoryServiceIdentity $name
    foreach ($key in @(Get-ChildItem Env: | Where-Object Name -like 'NC_*' | Select-Object -ExpandProperty Name)) { [Environment]::SetEnvironmentVariable($key,$null,'Process') }
    foreach ($property in $qqBindings.PSObject.Properties) { [Environment]::SetEnvironmentVariable($property.Name,[string]$property.Value,'Process') }
    $environment = @{
        NC_MODE=(Read-QQWJson $qqBindings.NC_GUI_CONFIG).mode; NC_CONTROL_MODE='study_runtime'; NC_ISOLATED_DEVELOPMENT='0'; NC_EMBODIMENT_PROVIDER='none'
        NC_SERVICE_TOKEN=[IO.File]::ReadAllText($qqRegistry.service_token_file).Trim(); NC_CONTROL_TOKEN=[IO.File]::ReadAllText($qqRegistry.control_token_file).Trim()
        NC_RUNTIME_MANIFEST_PATH=$qqRegistry.manifest_path; NC_COGNITIVE_MANIFEST_PATH=$qqRegistry.manifest_path
        NC_HOMEOSTASIS_PROFILE_PATH=$qqRegistry.profile_path; NC_HOMEOSTASIS_STATE_DIR=$qqRegistry.homeostasis_state_dir
        NC_RUNTIME_STATE_DIR=$qqRegistry.runtime_state_dir; NC_COGNITIVE_STATE_PATH=(Join-Path $qqRegistry.state_root 'cognitive\state.json')
        NC_RUNTIME_PORT='8770'; NC_COGNITIVE_PORT='8771'; NC_HOMEOSTASIS_PORT='8772'; NC_GUI_PORT='8790'
        NC_RUNTIME_URL='http://127.0.0.1:8770'; NC_COGNITIVE_URL='http://127.0.0.1:8771'; NC_HOMEOSTASIS_URL='http://127.0.0.1:8772'
    }
    foreach ($key in $environment.Keys) { [Environment]::SetEnvironmentVariable($key,[string]$environment[$key],'Process') }
    # Revalidate creation timestamps immediately before acting on each exact PID.
    foreach ($process in @($identity.child,$identity.parent)) {
        $fresh = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
        if (-not $fresh) { continue }
        if ($fresh.CreationDate -ne $process.CreationDate -or $fresh.ExecutablePath -cne $process.ExecutablePath -or $fresh.CommandLine -cne $process.CommandLine) { throw 'qq_memory_pid_reused' }
        Stop-Process -Id $fresh.ProcessId -ErrorAction Stop
        Wait-Process -Id $fresh.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $stamp = [datetime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $stdout = Join-Path $qqEvidence "$name-$stamp.stdout.log"
    $stderr = Join-Path $qqEvidence "$name-$stamp.stderr.log"
    $replacement = Start-Process -FilePath $qqRegistry.python_path -ArgumentList @('-m',$identity.entry.module) -WorkingDirectory $qqMemoryRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $identity.entry.pid=$replacement.Id; $identity.entry.process_start_time_utc=$replacement.StartTime.ToUniversalTime().ToString('o')
    $identity.entry.started_at=[datetime]::UtcNow.ToString('o'); $identity.entry.out_log=$stdout; $identity.entry.err_log=$stderr
    Write-QQDJson $qqRegistryPath $qqRegistry
    $qqState.phase=$name; $qqState.reloaded=@($qqState.reloaded)+@($name)
    Write-QQDJson $qqStatePath $qqState
    Write-Output "$name replacement started; verify health before continuing."
    return
}
if ($Phase -eq 'Resume') {
    foreach ($name in @('cognitive','runtime')) { $null = Get-QQMemoryServiceIdentity $name }
    if ($qqState.runtime_lifecycle_before -eq 'RUNNING') {
        Invoke-RestMethod -Method Post 'http://127.0.0.1:8770/v1/resume' -Headers $qqControlHeaders -ContentType 'application/json' -Body '{"reason":"scoped QQ memory validation complete; restore prior operation"}' -TimeoutSec 10 | Out-Null
    }
    Invoke-RestMethod -Method Post 'http://127.0.0.1:8771/v1/maintenance/resume' -Headers $qqServiceHeaders -TimeoutSec 10 | Out-Null
    $started = Invoke-QQDirectStart -ConfigPath $qqConfigPath -TimeoutSeconds 90
    if ($started.exit_code -ne 0) { throw 'qq_memory_direct_restart_failed' }
    Write-QQDJson $qqConfig.ControlFile @{paused=[bool]$qqState.direct_paused_before;stop=$false}
    $qqState.phase='resumed'
    $qqState | Add-Member -NotePropertyName completed_at -NotePropertyValue ([datetime]::UtcNow.ToString('o')) -Force
    Write-QQDJson $qqStatePath $qqState
    Write-Output 'Prior QQ actor operation restored; history did not create or send messages.'
}
