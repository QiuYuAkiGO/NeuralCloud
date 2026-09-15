#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PrivateRoot = (Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDirect'),
    [ValidateRange(1024, 65535)][int]$WebUiPort = 6799,
    [switch]$Refresh
)

# API credentials and the QR URL stay in memory; output contains only bounded
# status, the account number after login, and the private QR PNG path.
. (Join-Path $PSScriptRoot 'Start-QQWatcher.ps1')
$ErrorActionPreference = 'Stop'
$process = $null
try {
    $private = Resolve-QQWPrivatePath $PrivateRoot $env:LOCALAPPDATA
    $bridge = Resolve-QQWPrivatePath (Join-Path $private 'bridge') $private
    $configPath = Resolve-QQWPrivatePath (Join-Path $bridge 'config\webui.json') $bridge -ExistingFile
    $config = Read-QQWJson $configPath
    $token = [string](Get-QQWProperty $config 'token')
    if ($token.Length -lt 32 -or $token.Length -gt 4096 -or $token -match '\s') { throw 'webui_token_invalid' }
    $baseUrl = 'http://127.0.0.1:' + $WebUiPort + '/api'
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($token + '.napcat'))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $auth = Invoke-RestMethod -Uri ($baseUrl + '/auth/login') -Method Post -ContentType 'application/json' -Body (@{hash=$hash} | ConvertTo-Json -Compress) -TimeoutSec 10
    if ((Get-QQWProperty $auth 'code') -ne 0) { throw 'webui_auth_failed' }
    $credential = [string](Get-QQWProperty (Get-QQWProperty $auth 'data') 'Credential')
    if (-not $credential) { throw 'webui_auth_failed' }
    $headers = @{Authorization='Bearer ' + $credential}
    $status = Invoke-RestMethod -Uri ($baseUrl + '/QQLogin/CheckLoginStatus') -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 10
    if ((Get-QQWProperty $status 'code') -ne 0) { throw 'webui_status_failed' }
    $data = Get-QQWProperty $status 'data'
    $loggedIn = (Get-QQWProperty $data 'isLogin') -eq $true
    $phase = [string](Get-QQWProperty $data 'loginPhase' 'unknown')
    if ($phase -notmatch '^[a-z_]{1,40}$') { $phase = 'unknown' }
    $account = $null
    $qrPath = $null
    if ($loggedIn) {
        $login = Invoke-RestMethod -Uri ($baseUrl + '/QQLogin/GetQQLoginInfo') -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 10
        foreach ($field in @('uin', 'user_id', 'qq')) {
            $candidate = [string](Get-QQWProperty (Get-QQWProperty $login 'data') $field)
            if ($candidate -match '^[1-9][0-9]{4,19}$') { $account = $candidate; break }
        }
    } else {
        $qrValue = [string](Get-QQWProperty $data 'qrcodeurl')
        if ($Refresh -or -not $qrValue -or $phase -match 'expired|error') {
            $refreshResult = Invoke-RestMethod -Uri ($baseUrl + '/QQLogin/RefreshQRcode') -Method Post -ContentType 'application/json' -Body '{}' -Headers $headers -TimeoutSec 15
            if ((Get-QQWProperty $refreshResult 'code') -ne 0) { throw 'qq_qrcode_refresh_failed' }
            $qrValue = [string](Get-QQWProperty (Get-QQWProperty $refreshResult 'data') 'qrcodeurl')
            $phase = 'waiting_qrcode'
        }
        if (-not $qrValue -or $qrValue.Length -gt 8192) { throw 'qq_qrcode_unavailable' }
        $qrPath = Resolve-QQWPrivatePath (Join-Path $bridge 'qq-login.png') $bridge
        $python = @(Get-QQWInterpreterPaths)[0]
        $code = @'
import json, sys
import qrcode
from qrcode.image.pil import PilImage
value = json.load(sys.stdin)
qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
qr.add_data(value['qr'])
qr.make(fit=True)
qr.make_image(image_factory=PilImage, fill_color='black', back_color='white').save(value['path'])
'@
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $python
        $info.Arguments = '-c ' + (ConvertTo-QQWArgument $code)
        $info.WorkingDirectory = $script:QQWRepositoryRoot
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardInput = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info
        [void]$process.Start()
        $process.StandardInput.WriteLine((@{qr=$qrValue;path=$qrPath} | ConvertTo-Json -Compress))
        $process.StandardInput.Close()
        $null = $process.StandardOutput.ReadToEnd()
        $null = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $qrPath -PathType Leaf)) { throw 'qq_qrcode_render_failed' }
    }
    [pscustomobject]@{logged_in=$loggedIn;account=$account;login_phase=$phase;qr_png=$qrPath;webui_url=('http://127.0.0.1:' + $WebUiPort + '/webui')} | ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{logged_in=$false;error='napcat_login_helper_failed';qr_png=$null} | ConvertTo-Json -Compress
    exit 20
} finally {
    $token=$null; $hash=$null; $credential=$null; $headers=$null; $auth=$null; $status=$null; $data=$null; $qrValue=$null
    if ($null -ne $process) { $process.Dispose() }
}
