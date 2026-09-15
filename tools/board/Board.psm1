#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-BoardPlainPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'board_path_reparse_point' }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Set-BoardPrivateAcl {
    param([Parameter(Mandatory=$true)][string]$Path, [switch]$Directory)
    $info = if ($Directory) { New-Object IO.DirectoryInfo($Path) } else { New-Object IO.FileInfo($Path) }
    # Change only DACL access rules, preserving owner/group/SACL. Replacing a
    # complete security descriptor can require unavailable SeSecurityPrivilege.
    $acl = if ($PSVersionTable.PSVersion.Major -ge 6) {
        [IO.FileSystemAclExtensions]::GetAccessControl($info, [Security.AccessControl.AccessControlSections]::Access)
    } else { $info.GetAccessControl([Security.AccessControl.AccessControlSections]::Access) }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    foreach ($sid in @($current, $system)) {
        if ($Directory) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
        }
        $acl.AddAccessRule($rule)
    }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        [IO.FileSystemAclExtensions]::SetAccessControl($info, $acl)
    } else { $info.SetAccessControl($acl) }
}

function Write-BoardAtomicText {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][string]$Text, [switch]$Private)
    Assert-BoardPlainPath $Path
    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    $temporary = Join-Path $directory ('.board-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Text, (New-Object Text.UTF8Encoding($false)))
        if ($Private) { Set-BoardPrivateAcl $temporary }
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
        if ($Private) { Set-BoardPrivateAcl $Path }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Get-BoardLinuxWorkspace {
    param([Parameter(Mandatory=$true)][string]$Distro, [Parameter(Mandatory=$true)][string]$Workspace)
    if ($Distro -notmatch '^[A-Za-z0-9._-]+$') { throw 'board_invalid_distro' }
    $full = [IO.Path]::GetFullPath($Workspace)
    Assert-BoardPlainPath $full
    $result = & wsl.exe --distribution $Distro --user root --exec wslpath -a -u $full 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'board_wslpath_failed' }
    $linux = ([string]($result -join "`n")).Trim()
    if (-not $linux.StartsWith('/') -or $linux -match '[\r\n\x00]') { throw 'board_linux_workspace_invalid' }
    return $linux
}

function Invoke-BoardLinux {
    param([Parameter(Mandatory=$true)][string]$Distro, [Parameter(Mandatory=$true)][string]$Workspace,
          [ValidateSet('start','stop','status')][string]$Action)
    $linux = Get-BoardLinuxWorkspace -Distro $Distro -Workspace $Workspace
    $helper = $linux.TrimEnd('/') + '/tools/board/linux_board.py'
    $result = & wsl.exe --distribution $Distro --user root --exec python3 $helper $Action --workspace $linux 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'board_linux_action_failed: inspect wsl -d Ubuntu-24.04 -- journalctl -u neuralcloud-board.service -n 40'
    }
    return ([string]($result -join "`n") | ConvertFrom-Json -ErrorAction Stop)
}

function Save-BoardClientConfiguration {
    param([Parameter(Mandatory=$true)][string]$Distro, [Parameter(Mandatory=$true)][object]$LinuxResult,
          [Parameter(Mandatory=$true)][string]$Workspace)
    if ($LinuxResult.owner -ne 'neuralcloud-board-launcher-v1' -or $LinuxResult.body_instance_id -ne 'misty-linux-board-01' -or
        $LinuxResult.board_id -ne 'misty-thoughts' -or $LinuxResult.port -ne 8793 -or
        $LinuxResult.token_file -ne '/root/.local/share/neuralcloud/board/service.token') { throw 'board_deployment_result_mismatch' }
    $privateRoot = Join-Path $env:LOCALAPPDATA 'NeuralCloud\Board'
    Assert-BoardPlainPath $privateRoot
    [void][IO.Directory]::CreateDirectory($privateRoot)
    Set-BoardPrivateAcl -Path $privateRoot -Directory
    $tokenPath = Join-Path $privateRoot 'service.token'
    # Capture privately. Never emit the token, put it in process arguments, or
    # save it in workspace configuration; only its protected path is persisted.
    $token = $null
    try {
        $result = & wsl.exe --distribution $Distro --user root --exec cat $LinuxResult.token_file 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'board_token_copy_failed' }
        $token = ([string]($result -join "`n")).Trim()
        if ($token.Length -lt 32 -or $token.Length -gt 4096 -or $token -match '\s') { throw 'board_token_invalid' }
        # Verify the Windows-to-WSL loopback route before binding the shared mind
        # to this endpoint. A healthy Linux socket alone is insufficient.
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:8793/health')
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 5000
        $request.ReadWriteTimeout = 5000
        $request.Headers['Authorization'] = 'Bearer ' + $token
        $response = $null
        $reader = $null
        try {
            $response = $request.GetResponse()
            if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt 65536) { throw 'board_windows_route_failed' }
            $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
            $buffer = New-Object char[] 65537
            $read = $reader.ReadBlock($buffer, 0, $buffer.Length)
            if ($read -gt 65536) { throw 'board_windows_health_too_large' }
            $health = (New-Object string($buffer, 0, $read)) | ConvertFrom-Json -ErrorAction Stop
            if ($health.provider_id -ne 'misty-linux-thought-board' -or $health.body_instance_id -ne 'misty-linux-board-01' -or $health.status -ne 'healthy') { throw 'board_windows_health_identity_mismatch' }
        } finally {
            if ($reader) { $reader.Dispose() }
            if ($response) { $response.Dispose() }
        }
        Write-BoardAtomicText -Path $tokenPath -Text ($token + "`n") -Private
    } finally { $token = $null; $result = $null }
    $configPath = Join-Path $privateRoot 'board-client.json'
    $configuration = [ordered]@{
        base_url = 'http://127.0.0.1:8793'; token_file = $tokenPath
        body_instance_id = 'misty-linux-board-01'; board_id = 'misty-thoughts'
    }
    Write-BoardAtomicText -Path $configPath -Text (($configuration | ConvertTo-Json) + "`n") -Private
    $launchPath = Join-Path $Workspace 'data\shared-misty-launch.json'
    Assert-BoardPlainPath $launchPath
    $launch = [ordered]@{}
    if (Test-Path -LiteralPath $launchPath) {
        $old = [IO.File]::ReadAllText($launchPath, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
        foreach ($property in $old.PSObject.Properties) { $launch[$property.Name] = $property.Value }
    } else {
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($launchPath))
    }
    $launch['NC_BOARD_CONFIG'] = $configPath
    Write-BoardAtomicText -Path $launchPath -Text (($launch | ConvertTo-Json -Depth 10) + "`n")
    return $configPath
}

Export-ModuleMember -Function Get-BoardLinuxWorkspace, Invoke-BoardLinux, Save-BoardClientConfiguration
