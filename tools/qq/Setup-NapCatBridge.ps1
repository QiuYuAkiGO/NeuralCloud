#Requires -Version 5.1
# Creates an isolated, pinned NapCat bridge. -CheckOnly / -WhatIf show a plan.
# Existing containers must match the labels, mounts, ports and image exactly.
# This script never touches the desktop QQ container or its login directory.
# First QQ login requires the operator's phone; OneBot starts after login.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$PrivateRoot = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect'),
    [ValidateRange(1024, 65535)][int]$WebSocketPort = 6301,
    [ValidateRange(1024, 65535)][int]$WebUiPort = 6799,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$bridgeName = 'neuralcloud-qq-onebot'
$bridgeVersion = 'v4.18.28'
$bridgeDigest = 'sha256:2cc70b45244ae3fabc657ffc3aaa4de8fb893518171e519659ab83d80e6d36b5'
$bridgeImage = 'mlikiowa/napcat-docker@' + $bridgeDigest
$bridgeLabel = 'io.neuralcloud.component'

function Resolve-NapCatPrivatePath {
    param([string]$Path, [string]$Anchor)
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $allowed = [IO.Path]::GetFullPath($Anchor).TrimEnd('\', '/')
    if (-not $full.StartsWith($allowed + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'bridge_path_outside_private_root'
    }
    $cursor = $full
    while ($cursor.Length -ge $allowed.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'bridge_path_reparse_point'
            }
        }
        if ($cursor.Equals($allowed, [StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

function Invoke-NapCatDocker {
    param([string[]]$Arguments)
    $result = @(& docker @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'bridge_docker_command_failed' }
    return ($result -join "`n")
}

function Get-NapCatContainer {
    $names = Invoke-NapCatDocker @('ps', '-a', '--filter', ('name=^/' + $bridgeName + '$'), '--format', '{{.Names}}')
    if ([string]::IsNullOrWhiteSpace($names)) { return $null }
    if ($names.Trim() -ne $bridgeName) { throw 'bridge_container_name_collision' }
    return @(ConvertFrom-Json (Invoke-NapCatDocker @('inspect', $bridgeName)))[0]
}

function Assert-NapCatContainer {
    param([object]$Container)
    if ($Container.Config.Image -ne $bridgeImage -or
        $Container.Config.Labels.$bridgeLabel -ne 'qq-onebot-bridge' -or
        $Container.Config.Labels.'io.neuralcloud.private-root' -ne $bridgeRoot) {
        throw 'bridge_existing_container_identity_mismatch'
    }
    if ($Container.HostConfig.NetworkMode -eq 'host' -or $Container.HostConfig.Privileged -or
        $Container.HostConfig.RestartPolicy.Name -ne 'unless-stopped') {
        throw 'bridge_existing_container_isolation_mismatch'
    }
    $expectedMounts = @{ '/app/napcat/config' = $configRoot; '/app/.config/QQ' = $qqRoot }
    if (@($Container.Mounts).Count -ne 2) { throw 'bridge_existing_container_mount_mismatch' }
    foreach ($mount in $Container.Mounts) {
        if ($mount.Type -ne 'bind' -or -not $mount.RW -or -not $expectedMounts.ContainsKey($mount.Destination)) {
            throw 'bridge_existing_container_mount_mismatch'
        }
        # Docker Desktop can express a Windows bind source with either separator.
        $actual = ([string]$mount.Source).Replace('/', '\').TrimEnd('\')
        $expected = ([string]$expectedMounts[$mount.Destination]).Replace('/', '\').TrimEnd('\')
        if (-not $actual.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'bridge_existing_container_mount_mismatch'
        }
    }
    $ports = $Container.HostConfig.PortBindings
    if (@($ports.PSObject.Properties).Count -ne 2) { throw 'bridge_existing_container_port_mismatch' }
    foreach ($mapping in @(@('3001/tcp', $WebSocketPort), @('6099/tcp', $WebUiPort))) {
        $binding = @($ports.($mapping[0]))
        if ($binding.Count -ne 1 -or $binding[0].HostIp -ne '127.0.0.1' -or
            [int]$binding[0].HostPort -ne [int]$mapping[1]) { throw 'bridge_existing_container_port_mismatch' }
    }
}

function Write-NapCatJson {
    param([string]$Path, [object]$Value)
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
}

function Get-OrCreate-NapCatToken {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        $bytes = New-Object byte[] 48
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        [IO.File]::WriteAllText($Path, ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()), (New-Object Text.UTF8Encoding($false)))
    }
    if ((Get-Item -LiteralPath $Path).Length -gt 8192) { throw 'bridge_token_invalid' }
    $token = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8).Trim()
    if ($token.Length -lt 32 -or $token.Length -gt 4096 -or $token -match '\s') { throw 'bridge_token_invalid' }
    return $token
}

$root = Resolve-NapCatPrivatePath $PrivateRoot $env:LOCALAPPDATA
$bridgeRoot = Resolve-NapCatPrivatePath (Join-Path $root 'bridge') $root
$configRoot = Resolve-NapCatPrivatePath (Join-Path $bridgeRoot 'config') $bridgeRoot
$qqRoot = Resolve-NapCatPrivatePath (Join-Path $bridgeRoot 'qq') $bridgeRoot
$tokenPath = Resolve-NapCatPrivatePath (Join-Path $root 'onebot.token') $root
$webUiTokenPath = Resolve-NapCatPrivatePath (Join-Path $bridgeRoot 'webui.token') $bridgeRoot
$reportPath = Resolve-NapCatPrivatePath (Join-Path $bridgeRoot 'deployment-report.json') $bridgeRoot
if ($WebSocketPort -eq $WebUiPort) { throw 'bridge_ports_must_differ' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'bridge_docker_unavailable' }
$existing = Get-NapCatContainer
if ($null -ne $existing) { Assert-NapCatContainer $existing }
else {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
        $_.LocalPort -in @($WebSocketPort, $WebUiPort)
    })
    if ($listeners.Count -gt 0) { throw 'bridge_port_in_use' }
}

$plan = [ordered]@{
    container = $bridgeName; version = $bridgeVersion; image = $bridgeImage
    private_root = $bridgeRoot; token_file = $tokenPath
    websocket_url = ('ws://127.0.0.1:' + $WebSocketPort)
    webui_url = ('http://127.0.0.1:' + $WebUiPort + '/webui')
    existing_container = ($null -ne $existing)
    operation = 'create_or_verify_and_start'; original_qq_state_used = $false
}
if ($CheckOnly -or -not $PSCmdlet.ShouldProcess($bridgeName, 'Create or verify isolated pinned NapCat bridge and start it')) {
    $plan | ConvertTo-Json -Depth 5
    return
}

foreach ($directory in @($root, $bridgeRoot, $configRoot, $qqRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
# Private state stays accessible to this operator, administrators and SYSTEM.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$allowedSids = @($identity, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')), (New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
$acl = Get-Acl -LiteralPath $root
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
$allowedSidValues = @($allowedSids | ForEach-Object { $_.Value })
$privateAcl = $acl.AreAccessRulesProtected -and $rules.Count -eq 3
foreach ($rule in $rules) {
    $privateAcl = $privateAcl -and $rule.IdentityReference.Value -in $allowedSidValues -and
        $rule.AccessControlType -eq 'Allow' -and $rule.FileSystemRights -eq 'FullControl' -and
        $rule.InheritanceFlags -eq 'ContainerInherit, ObjectInherit'
}
if (-not $privateAcl) {
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    foreach ($sid in $allowedSids) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $root -AclObject $acl
}
$oneBotToken = Get-OrCreate-NapCatToken $tokenPath
$webUiToken = Get-OrCreate-NapCatToken $webUiTokenPath
$oneBotConfigPath = Resolve-NapCatPrivatePath (Join-Path $configRoot 'onebot11.json') $configRoot
if (Test-Path -LiteralPath $oneBotConfigPath) {
    $savedConfig = [IO.File]::ReadAllText($oneBotConfigPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $ws = @($savedConfig.network.websocketServers)
    if ($ws.Count -ne 1 -or $ws[0].token -cne $oneBotToken -or $ws[0].host -ne '0.0.0.0' -or
        $ws[0].port -ne 3001 -or -not $ws[0].enable -or $ws[0].messagePostFormat -ne 'array' -or
        -not $savedConfig.enableLocalFile2Url -or @($savedConfig.network.httpServers).Count -ne 0 -or
        @($savedConfig.network.httpClients).Count -ne 0 -or @($savedConfig.network.websocketClients).Count -ne 0) {
        throw 'bridge_existing_onebot_configuration_mismatch'
    }
} else {
    if ($null -ne $existing) { throw 'bridge_existing_onebot_configuration_missing' }
    Write-NapCatJson $oneBotConfigPath @{
        network = @{
            httpServers = @(); httpClients = @(); websocketClients = @()
            websocketServers = @(@{
                name = 'misty'; enable = $true; host = '0.0.0.0'; port = 3001
                messagePostFormat = 'array'; reportSelfMessage = $true; token = $oneBotToken
                enableForcePushEvent = $true; debug = $false; heartInterval = 30000
            })
        }
        enableLocalFile2Url = $true; parseMultMsg = $true; musicSignUrl = ''
    }
}
$webUiConfigPath = Resolve-NapCatPrivatePath (Join-Path $configRoot 'webui.json') $configRoot
if (-not (Test-Path -LiteralPath $webUiConfigPath)) {
    Write-NapCatJson $webUiConfigPath @{
        host = '0.0.0.0'; port = 6099; token = $webUiToken; loginRate = 3
        accessControlMode = 'none'; ipWhitelist = @(); ipBlacklist = @(); enableXForwardedFor = $false
    }
}
$napCatConfigPath = Resolve-NapCatPrivatePath (Join-Path $configRoot 'napcat.json') $configRoot
if (-not (Test-Path -LiteralPath $napCatConfigPath)) {
    # Creating this file also prevents the image entrypoint replacing our config.
    Write-NapCatJson $napCatConfigPath @{ fileLog = $false; consoleLog = $false; fileLogLevel = 'error'; consoleLogLevel = 'error'; packetServer = '' }
}
$oneBotToken = $null
$webUiToken = $null
if ($null -eq $existing) {
    Write-Verbose 'Pulling the pinned NapCat image.'
    $null = Invoke-NapCatDocker @('pull', $bridgeImage)
    $null = Invoke-NapCatDocker @(
        'run', '-d', '--name', $bridgeName, '--restart', 'unless-stopped', '--hostname', 'misty-qq-direct',
        '--label', ($bridgeLabel + '=qq-onebot-bridge'), '--label', ('io.neuralcloud.private-root=' + $bridgeRoot),
        '-e', 'NAPCAT_UID=2001', '-e', 'NAPCAT_GID=2000',
        '-p', ('127.0.0.1:' + $WebSocketPort + ':3001'), '-p', ('127.0.0.1:' + $WebUiPort + ':6099'),
        '--mount', ('type=bind,source=' + $configRoot + ',target=/app/napcat/config'),
        '--mount', ('type=bind,source=' + $qqRoot + ',target=/app/.config/QQ'), $bridgeImage
    )
} elseif (-not $existing.State.Running) {
    $null = Invoke-NapCatDocker @('start', $bridgeName)
}
$deployed = Get-NapCatContainer
Assert-NapCatContainer $deployed
$report = [ordered]@{
    schema_version = 1; checked_at = [datetime]::UtcNow.ToString('o')
    container = $bridgeName; version = $bridgeVersion; image = $bridgeImage; image_id = $deployed.Image
    running = [bool]$deployed.State.Running; restart_policy = 'unless-stopped'; loopback_only = $true
    websocket_url = $plan.websocket_url; webui_url = $plan.webui_url
    message_format = 'array'; image_base64_enabled = $true; authentication_configured = $true
    private_state_isolated = $true; original_qq_state_used = $false; qq_login_verified = $false
    login_state = 'not_checked'; message_sending_tested = $false
}
Write-NapCatJson $reportPath $report
$report | ConvertTo-Json -Depth 5
