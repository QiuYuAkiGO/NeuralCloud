#Requires -Version 5.1

Set-StrictMode -Version Latest

$script:LauncherSchemaVersion = "neuralcloud-launcher-result-v1"
$script:LauncherStateSchemaVersion = "neuralcloud-launcher-state-v4"
$script:RequiredUvVersion = "0.12.9"
$script:ExitSuccess = 0
$script:ExitNeedsAction = 10
$script:ExitMistyAI = 20
$script:ExitNeuralCloud = 30
$script:ExitConfiguration = 40
$script:ExitLock = 50
$script:LauncherJsonOutput = $false
$script:ReportedRootOperations = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)

function Write-LauncherRootOperation {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$Description
    )
    if ($script:LauncherJsonOutput) { return }
    if ($script:ReportedRootOperations.Add($Description)) {
        Write-Host "[privileged] WSL root ($Distro): $Description"
    }
}

function Get-LauncherObjectProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][object]$Default = $null
    )
    if ($null -eq $InputObject) { return $Default }
    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) { return $InputObject[$Name] }
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function New-LauncherCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [AllowNull()][object]$Current,
        [Parameter(Mandatory = $true)][string]$Required,
        [Parameter(Mandatory = $true)]
        [ValidateSet("pass", "warning", "missing", "blocked", "action_required")]
        [string]$Status,
        [AllowEmptyString()][string]$Remediation = "",
        [switch]$RestartRequired,
        [string]$Category = "general"
    )

    return [pscustomobject][ordered]@{
        id = $Id
        category = $Category
        current = $Current
        required = $Required
        status = $Status
        remediation = $Remediation
        restart_required = [bool]$RestartRequired
    }
}

function Test-LauncherVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Current,
        [Parameter(Mandatory = $true)][string]$Required,
        [switch]$Exact
    )

    $currentMatch = [regex]::Match($Current, '(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)')
    $requiredMatch = [regex]::Match($Required, '(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)')
    if (-not $currentMatch.Success -or -not $requiredMatch.Success) { return $false }
    try {
        $currentVersion = [version]$currentMatch.Groups[1].Value
        $requiredVersion = [version]$requiredMatch.Groups[1].Value
    } catch {
        return $false
    }
    if ($Exact) { return $currentVersion -eq $requiredVersion }
    return $currentVersion -ge $requiredVersion
}

function Test-SensitiveRelativePath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)

    $normalized = $Path.Replace('\', '/') -replace '^(?:\./)+', ''
    if (-not $normalized) { return $false }
    $parts = @($normalized -split '/')
    $name = $parts[-1].ToLowerInvariant()
    $directories = @($parts | Select-Object -SkipLast 1 | ForEach-Object { $_.ToLowerInvariant() })

    if ($name -in @('.env.example', '.env.sample', '.env.template')) { return $false }
    if ($name -eq '.env' -or $name.StartsWith('.env.')) { return $true }
    if ($name -in @('.envrc', '.npmrc', '.pypirc', '.netrc', '_netrc', '.dockercfg', '.git-credentials', 'application_default_credentials.json', 'kubeconfig')) { return $true }
    if ($name -match '^(?:client[-_]?secret|oauth[-_]?credentials)(?:\..+)?$') { return $true }
    if ($name -match '\.(?:token|pem|key|ppk|pfx|p12|jks|keystore)$') { return $true }
    if ($name -match '\.tfstate(?:\.backup)?$') { return $true }
    if ($name -match '^(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\.pub)?$') { return $true }
    if ($name -match '^(?:credentials?|secrets?|service-account)(?:\..+)?$') { return $true }
    if ($name -match '(?:^|[-_.])(?:access[-_.]?token|refresh[-_.]?token|service[-_.]?token)(?:[-_.]|$)') {
        return $true
    }
    if ($directories -contains '.ssh' -or $directories -contains '.gnupg') { return $true }
    if (($directories -contains '.docker' -and $name -eq 'config.json') -or
        ($directories -contains '.kube' -and $name -eq 'config') -or
        ($directories -contains '.aws' -and $name -in @('credentials', 'config'))) { return $true }
    return $false
}

function Protect-LauncherText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [AllowNull()][string[]]$Secrets = @()
    )

    $protected = $Text
    foreach ($secret in @($Secrets)) {
        if (-not [string]::IsNullOrWhiteSpace($secret)) {
            $protected = $protected.Replace($secret, '[REDACTED]')
        }
    }
    $protected = [regex]::Replace(
        $protected,
        '(?i)(Authorization\s*:\s*Bearer\s+)"[^"\r\n]*"',
        '$1"[REDACTED]"'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(Authorization\s*:\s*Bearer\s+)''[^''\r\n]*''',
        '$1''[REDACTED]'''
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;"''}]+',
        '$1[REDACTED]'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(\b(?:NC_MISTYAI_TOKEN|MISTYAI_SERVICE_TOKEN|MISTYAI_AGENT_TOKEN|service_token|agent_token|access_token|refresh_token)\b\s*[=:]\s*)"[^"\r\n]*"',
        '$1"[REDACTED]"'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(\b(?:NC_MISTYAI_TOKEN|MISTYAI_SERVICE_TOKEN|MISTYAI_AGENT_TOKEN|service_token|agent_token|access_token|refresh_token)\b\s*[=:]\s*)''[^''\r\n]*''',
        '$1''[REDACTED]'''
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(\b(?:NC_MISTYAI_TOKEN|MISTYAI_SERVICE_TOKEN|MISTYAI_AGENT_TOKEN|service_token|agent_token|access_token|refresh_token)\b\s*[=:]\s*)[^\s,;"''}]+',
        '$1[REDACTED]'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)("(?:service_token|agent_token|access_token|refresh_token|token)"\s*:\s*")[^"]*(")',
        '$1[REDACTED]$2'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----)[\s\S]*?(-----END(?: [A-Z0-9]+)? PRIVATE KEY-----)',
        '$1[REDACTED]$2'
    )
    return $protected
}

function ConvertTo-LauncherCommandLineArgument {
    param([AllowEmptyString()][string]$Value)

    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-LauncherFileBackedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [AllowEmptyString()][string]$WorkingDirectory = '',
        [hashtable]$Environment = @{},
        [AllowNull()][string[]]$SensitiveValues = @(),
        [ValidateRange(0, 86400)][int]$TimeoutSeconds = 0
    )
    $process = $null
    $outputDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('neuralcloud-command-' + [guid]::NewGuid().ToString('N'))
    $stdoutPath = Join-Path $outputDirectory 'stdout.log'
    $stderrPath = Join-Path $outputDirectory 'stderr.log'
    $savedEnvironment = @{}
    try {
        [void][System.IO.Directory]::CreateDirectory($outputDirectory)
        $startParameters = @{
            FilePath = $FilePath
            RedirectStandardOutput = $stdoutPath
            RedirectStandardError = $stderrPath
            WindowStyle = 'Hidden'
            PassThru = $true
            ErrorAction = 'Stop'
        }
        if ($ArgumentList.Count -gt 0) {
            $startParameters.ArgumentList = (@($ArgumentList | ForEach-Object {
                ConvertTo-LauncherCommandLineArgument -Value ([string]$_)
            }) -join ' ')
        }
        if ($WorkingDirectory) { $startParameters.WorkingDirectory = $WorkingDirectory }
        # Start-Process in Windows PowerShell 5.1 has no -Environment option.
        # Save/restore only these keys around creation, before waiting on the child.
        try {
            foreach ($key in $Environment.Keys) {
                $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable([string]$key, 'Process')
                [Environment]::SetEnvironmentVariable([string]$key, $Environment[$key], 'Process')
            }
            $process = Start-Process @startParameters
            [void]$process.Handle
        } finally {
            foreach ($key in $savedEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable([string]$key, $savedEnvironment[$key], 'Process')
            }
        }
        # Windows passes file handles directly to the process. Its descendants
        # may keep them open without preventing us from observing the parent's
        # exit. Never use Start-Process -Wait (which waits for the process tree),
        # nor ReadToEndAsync (which waits for every inherited pipe to close).
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $timedOut = $false
        while (-not $process.WaitForExit(250)) {
            if ($TimeoutSeconds -gt 0 -and $watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $timedOut = $true
                break
            }
        }
        $stopError = ''
        if ($timedOut -and -not $process.HasExited) {
            try {
                $process.Kill()
                [void]$process.WaitForExit(1000)
            } catch { $stopError = '; parent termination failed: ' + $_.Exception.Message }
        }
        $stdout = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) { [string](Get-Content -LiteralPath $stdoutPath -Raw) } else { '' }
        $stderr = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) { [string](Get-Content -LiteralPath $stderrPath -Raw) } else { '' }
        if ($timedOut) {
            return [pscustomobject]@{
                exit_code = 124; stdout = $stdout; timed_out = $true
                stderr = Protect-LauncherText -Text ("Command timed out after $TimeoutSeconds seconds: $([System.IO.Path]::GetFileName($FilePath))$stopError`n$stderr").Trim() -Secrets $SensitiveValues
            }
        }
        return [pscustomobject]@{ exit_code = [int]$process.ExitCode; stdout = $stdout; stderr = $stderr }
    } catch {
        return [pscustomobject]@{ exit_code = 127; stdout = ''; stderr = Protect-LauncherText -Text $_.Exception.Message -Secrets $SensitiveValues }
    } finally {
        if ($process) { $process.Dispose() }
        # An inherited log handle may still be open. Preserve it rather than
        # stopping a child just to delete a temporary output file.
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
        }
        try { [System.IO.Directory]::Delete($outputDirectory, $false) } catch { }
    }
}

function Invoke-LauncherExternalCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [AllowEmptyString()][string]$WorkingDirectory = "",
        [hashtable]$Environment = @{},
        [AllowNull()][string[]]$SensitiveValues = @(),
        [ValidateRange(0, 86400)][int]$TimeoutSeconds = 0,
        [switch]$FileBackedOutput,
        [scriptblock]$CommandInvoker
    )

    $request = [pscustomobject][ordered]@{
        FilePath = $FilePath
        ArgumentList = @($ArgumentList)
        WorkingDirectory = $WorkingDirectory
        Environment = $Environment
        SensitiveValues = @($SensitiveValues)
        TimeoutSeconds = $TimeoutSeconds
        FileBackedOutput = [bool]$FileBackedOutput
    }
    if ($CommandInvoker) {
        $customResult = & $CommandInvoker $request
        if ($null -eq $customResult) {
            throw "The launcher command invoker returned no result for $FilePath"
        }
        $snakeExitCode = Get-LauncherObjectProperty $customResult 'exit_code'
        $pascalExitCode = Get-LauncherObjectProperty $customResult 'ExitCode'
        $snakeStdout = Get-LauncherObjectProperty $customResult 'stdout'
        $pascalStdout = Get-LauncherObjectProperty $customResult 'StdOut'
        $snakeStderr = Get-LauncherObjectProperty $customResult 'stderr'
        $pascalStderr = Get-LauncherObjectProperty $customResult 'StdErr'
        $customExitCode = if ($null -ne $snakeExitCode) {
            [int]$snakeExitCode
        } elseif ($null -ne $pascalExitCode) {
            [int]$pascalExitCode
        } else {
            0
        }
        $customStdout = if ($null -ne $snakeStdout) {
            [string]$snakeStdout
        } elseif ($null -ne $pascalStdout) {
            [string]$pascalStdout
        } else {
            ""
        }
        $customStderr = if ($null -ne $snakeStderr) {
            [string]$snakeStderr
        } elseif ($null -ne $pascalStderr) {
            [string]$pascalStderr
        } else {
            ""
        }
        $normalizedResult = [ordered]@{
            exit_code = $customExitCode
            stdout = $customStdout
            stderr = $customStderr
        }
        if ($customExitCode -eq 124 -or (Get-LauncherObjectProperty $customResult 'timed_out' $false)) {
            $normalizedResult['timed_out'] = $true
        }
        return [pscustomobject]$normalizedResult
    }

    if ($FileBackedOutput) {
        return Invoke-LauncherFileBackedCommand -FilePath $FilePath -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory -Environment $Environment -SensitiveValues $SensitiveValues -TimeoutSeconds $TimeoutSeconds
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@($ArgumentList | ForEach-Object {
        ConvertTo-LauncherCommandLineArgument -Value ([string]$_)
    }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if ($WorkingDirectory) { $startInfo.WorkingDirectory = $WorkingDirectory }
    foreach ($key in $Environment.Keys) {
        $value = $Environment[$key]
        if ($null -eq $value) {
            [void]$startInfo.EnvironmentVariables.Remove([string]$key)
        } else {
            $startInfo.EnvironmentVariables[[string]$key] = [string]$value
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $timedOut = $false
        if ($TimeoutSeconds -gt 0) {
            $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
            $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
            # A child may inherit a redirected pipe after the probe exits. Bound
            # output collection too, instead of blocking again in GetResult().
            foreach ($readTask in @($stdoutTask, $stderrTask)) {
                if ($timedOut) { break }
                $remainingMs = [int][math]::Max(0, ($deadline - [datetime]::UtcNow).TotalMilliseconds)
                if (-not $readTask.Wait($remainingMs)) { $timedOut = $true }
            }
        } else {
            $process.WaitForExit()
        }
        if ($timedOut) {
            # This is the single process created above, never a process tree,
            # Docker backend, service, or WSL distribution.
            $stopError = ''
            if (-not $process.HasExited) {
                try {
                    $process.Kill()
                    [void]$process.WaitForExit(1000)
                } catch {
                    $stopError = '; probe termination failed: ' + (Protect-LauncherText -Text $_.Exception.Message -Secrets $SensitiveValues)
                }
            }
            $partialOutput = if ($stdoutTask.Status -eq 'RanToCompletion') { $stdoutTask.GetAwaiter().GetResult() } else { '' }
            $partialError = if ($stderrTask.Status -eq 'RanToCompletion') { $stderrTask.GetAwaiter().GetResult() } else { '' }
            $message = "Command timed out after $TimeoutSeconds seconds: $([System.IO.Path]::GetFileName($FilePath))$stopError"
            return [pscustomobject][ordered]@{
                exit_code = 124
                stdout = [string]$partialOutput
                stderr = Protect-LauncherText -Text (($message + "`n" + $partialError).Trim()) -Secrets $SensitiveValues
                timed_out = $true
            }
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = [string]$stdout
            stderr = [string]$stderr
        }
    } catch {
        $message = Protect-LauncherText -Text $_.Exception.Message -Secrets $SensitiveValues
        return [pscustomobject][ordered]@{
            exit_code = 127
            stdout = ""
            stderr = $message
        }
    } finally {
        $process.Dispose()
    }
}

function Quote-LauncherBashLiteral {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    $singleQuoteEscape = "'" + [char]34 + "'" + [char]34 + "'"
    return "'" + $Value.Replace("'", $singleQuoteEscape) + "'"
}

function ConvertTo-WslPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Distro = "Ubuntu-24.04"
    )

    $unusedDistro = $Distro
    if ($Path -match '^([A-Za-z]):[\\/](.*)$') {
        $drive = $matches[1].ToLowerInvariant()
        $tail = $matches[2].Replace('\', '/')
        return "/mnt/$drive/$tail"
    }
    if ($Path -match '^\\\\wsl(?:\.localhost)?\\[^\\]+\\(.*)$') {
        return '/' + $matches[1].Replace('\', '/')
    }
    return $Path.Replace('\', '/')
}

function Invoke-LauncherWslCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$Script,
        [AllowEmptyString()][string]$WorkingDirectory = "",
        [AllowEmptyString()][string]$User = "",
        [string]$Description = 'execute a launcher-managed WSL command',
        [scriptblock]$CommandInvoker,
        [ValidateRange(0, 86400)][int]$TimeoutSeconds = 0,
        [AllowNull()][string[]]$SensitiveValues = @()
    )

    if ($Distro -notmatch '^[A-Za-z0-9._-]+$') {
        throw "Invalid WSL distribution name: $Distro"
    }
    if ($User -eq 'root') {
        Write-LauncherRootOperation -Distro $Distro -Description $Description
    }
    # PowerShell here-strings retain the checkout's CRLF line endings. Bash
    # treats the carriage return as part of tokens (including set options).
    $effectiveScript = $Script.Replace("`r`n", "`n").Replace("`r", "`n")
    if ($WorkingDirectory) {
        $effectiveScript = "cd -- $(Quote-LauncherBashLiteral $WorkingDirectory) && $effectiveScript"
    }
    $arguments = @('--distribution', $Distro)
    if ($User) { $arguments += @('--user', $User) }
    $arguments += @('--exec', 'bash', '--noprofile', '--norc', '-c', $effectiveScript)
    return Invoke-LauncherExternalCommand `
        -FilePath 'wsl.exe' `
        -ArgumentList $arguments `
        -SensitiveValues $SensitiveValues `
        -TimeoutSeconds $TimeoutSeconds `
        -CommandInvoker $CommandInvoker
}

function Get-LauncherWorkspaceFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [scriptblock]$CommandInvoker
    )

    $result = Invoke-LauncherExternalCommand `
        -FilePath 'git' `
        -ArgumentList @('-C', $Root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard') `
        -CommandInvoker $CommandInvoker
    if ($result.exit_code -ne 0) {
        throw "Could not enumerate the Git workspace: $(Protect-LauncherText $result.stderr)"
    }
    $raw = $result.stdout.Replace([string][char]0xFEFF, '')
    $files = @($raw -split ([string][char]0) | Where-Object { $_ })
    $excludedPattern = '(?i)(?:(?:^|/)(?:\.git|\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|\.tox|\.nox|node_modules|dist|build|logs?|htmlcov|\.coverage|\.mistyai)(?:/|$)|(?:^|/)[^/]+\.(?:pyc|pyo|log)$)'
    $filtered = New-Object System.Collections.Generic.List[string]
    foreach ($file in $files) {
        $normalized = ([string]$file).Replace('\', '/') -replace '^(?:\./)+', ''
        if (-not $normalized -or $normalized -match $excludedPattern) { continue }
        $fullPath = Join-Path $Root ($normalized.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
        if (Test-Path -LiteralPath $fullPath -PathType Leaf) { [void]$filtered.Add($normalized) }
    }
    $array = $filtered.ToArray()
    [Array]::Sort($array, [System.StringComparer]::Ordinal)
    return @($array)
}

function Get-WorkspaceFingerprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$Files,
        [scriptblock]$CommandInvoker
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "Workspace does not exist: $Root"
    }
    $effectiveFiles = $Files
    if ($null -eq $effectiveFiles) {
        $effectiveFiles = Get-LauncherWorkspaceFiles -Root $resolvedRoot -CommandInvoker $CommandInvoker
    }
    $normalizedFiles = @($effectiveFiles | ForEach-Object { ([string]$_).Replace('\', '/') -replace '^(?:\./)+', '' })
    [Array]::Sort($normalizedFiles, [System.StringComparer]::Ordinal)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($relativePath in $normalizedFiles) {
            if (-not $relativePath) { continue }
            $nativeRelative = $relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
            $fullPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot $nativeRelative))
            $boundary = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
            if (-not $fullPath.StartsWith($boundary, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Workspace file escapes the root: $relativePath"
            }
            if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { continue }
            if (Test-LauncherPathHasReparsePoint -Root $resolvedRoot -Path $fullPath) {
                throw "Workspace file traverses a symbolic link or reparse point: $relativePath"
            }
            $pathBytes = [System.Text.Encoding]::UTF8.GetBytes($relativePath + "`n")
            [void]$sha.TransformBlock($pathBytes, 0, $pathBytes.Length, $pathBytes, 0)
            $stream = [System.IO.File]::OpenRead($fullPath)
            try {
                $buffer = New-Object byte[] 65536
                while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    [void]$sha.TransformBlock($buffer, 0, $count, $buffer, 0)
                }
            } finally {
                $stream.Dispose()
            }
            $separator = [System.Text.Encoding]::UTF8.GetBytes("`n--launcher-file-boundary--`n")
            [void]$sha.TransformBlock($separator, 0, $separator.Length, $separator, 0)
        }
        [void]$sha.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        return ([System.BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Test-LauncherPathHasReparsePoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $boundary = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if ($resolvedPath -ne $resolvedRoot -and -not $resolvedPath.StartsWith($boundary, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $rootItem = Get-Item -LiteralPath $resolvedRoot -Force -ErrorAction SilentlyContinue
    if ($rootItem -and (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $true }
    if ($resolvedPath -eq $resolvedRoot) { return $false }
    $relative = $resolvedPath.Substring($boundary.Length)
    $current = $resolvedRoot
    foreach ($segment in @($relative -split '[\\/]')) {
        if (-not $segment) { continue }
        $current = Join-Path $current $segment
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($item -and (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $true }
    }
    return $false
}

function Get-LauncherDataPaths {
    $base = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if ([string]::IsNullOrWhiteSpace($base)) {
        $base = Join-Path ([System.IO.Path]::GetTempPath()) 'NeuralCloudLocal'
    }
    $directory = Join-Path $base 'NeuralCloud\launcher'
    $stagingOverride = [Environment]::GetEnvironmentVariable('NC_LAUNCHER_STAGING_DIR', 'Process')
    $staging = if ([string]::IsNullOrWhiteSpace($stagingOverride)) {
        Join-Path $directory 'staging'
    } else {
        if (-not [System.IO.Path]::IsPathRooted($stagingOverride)) {
            throw 'NC_LAUNCHER_STAGING_DIR must be an absolute Windows path.'
        }
        [System.IO.Path]::GetFullPath($stagingOverride)
    }
    return [pscustomobject][ordered]@{
        directory = $directory
        state = Join-Path $directory 'state.json'
        lock = Join-Path $directory 'launcher.lock'
        staging = $staging
    }
}

function Get-LauncherDefaultState {
    return [pscustomobject][ordered]@{
        schema_version = $script:LauncherStateSchemaVersion
        install_phase = 'not_started'
        neural_dependency_fingerprint = $null
        neuralcloud_root = $null
        mistyai_deployment_fingerprint = $null
        mistyai_source_path = $null
        mistyai_distro = $null
        mistyai_previous_available = $false
        owned = [pscustomobject][ordered]@{
            neuralcloud = $false
            mistyai = [pscustomobject][ordered]@{
                body = $false
                operator = $false
                compose = $false
            }
        }
        mistyai_unit_enablement_owned = [pscustomobject][ordered]@{
            body = $false
            operator = $false
        }
        last_command = $null
        last_success_at = $null
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Read-LauncherState {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return Get-LauncherDefaultState }
    try {
        $state = Get-Content -Raw -LiteralPath $Path -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $stateSchema = [string](Get-LauncherObjectProperty $state 'schema_version' '')
        if ($stateSchema -notin @('neuralcloud-launcher-state-v3', $script:LauncherStateSchemaVersion)) {
            return Get-LauncherDefaultState
        }
        $state.schema_version = $script:LauncherStateSchemaVersion
        $defaults = Get-LauncherDefaultState
        foreach ($property in $defaults.PSObject.Properties) {
            if ($null -eq $state.PSObject.Properties[$property.Name]) {
                $state | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value
            }
        }
        if ($null -eq $state.owned.PSObject.Properties['neuralcloud']) {
            $state.owned | Add-Member -NotePropertyName 'neuralcloud' -NotePropertyValue $false
        }
        if ($null -eq $state.owned.PSObject.Properties['mistyai']) {
            $state.owned | Add-Member -NotePropertyName 'mistyai' -NotePropertyValue $defaults.owned.mistyai
        }
        foreach ($component in @('body', 'operator', 'compose')) {
            if ($null -eq $state.owned.mistyai.PSObject.Properties[$component]) {
                $state.owned.mistyai | Add-Member -NotePropertyName $component -NotePropertyValue $false
            }
        }
        if ($null -eq (Get-LauncherObjectProperty $state 'mistyai_unit_enablement_owned')) {
            if ($null -ne $state.PSObject.Properties['mistyai_unit_enablement_owned']) {
                $state.mistyai_unit_enablement_owned = $defaults.mistyai_unit_enablement_owned
            } else {
                $state | Add-Member -NotePropertyName 'mistyai_unit_enablement_owned' -NotePropertyValue $defaults.mistyai_unit_enablement_owned
            }
        }
        foreach ($component in @('body', 'operator')) {
            if ($null -eq $state.mistyai_unit_enablement_owned.PSObject.Properties[$component]) {
                $state.mistyai_unit_enablement_owned | Add-Member -NotePropertyName $component -NotePropertyValue $false
            }
        }
        return $state
    } catch {
        return Get-LauncherDefaultState
    }
}

function Write-LauncherState {
    param(
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $directory = Split-Path -Parent $Path
    [void](New-Item -ItemType Directory -Path $directory -Force)
    $State.updated_at = (Get-Date).ToUniversalTime().ToString('o')
    $json = $State | ConvertTo-Json -Depth 12
    if ((Protect-LauncherText -Text $json) -ne $json) {
        throw 'Refusing to persist launcher state containing a credential'
    }
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [System.IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Enter-LauncherLock {
    param([Parameter(Mandatory = $true)][string]$Path)
    $directory = Split-Path -Parent $Path
    [void](New-Item -ItemType Directory -Path $directory -Force)
    try {
        return [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        return $null
    }
}

function Throw-LauncherFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [AllowNull()][object]$Details = $null
    )
    $exception = New-Object System.InvalidOperationException($Message)
    $exception.Data['LauncherExitCode'] = $ExitCode
    if ($null -ne $Details) { $exception.Data['LauncherDetails'] = $Details }
    throw $exception
}

function Get-LauncherFailureCode {
    param(
        [Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord,
        [int]$Default = 40
    )
    if ($null -ne $ErrorRecord.Exception.Data['LauncherExitCode']) {
        return [int]$ErrorRecord.Exception.Data['LauncherExitCode']
    }
    return $Default
}

function Test-LauncherCommandAvailable {
    param([Parameter(Mandatory = $true)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-LauncherPythonCommand {
    param([scriptblock]$CommandInvoker)
    $candidates = @(
        [pscustomobject]@{ file = 'py.exe'; args = @('-3.12') },
        [pscustomobject]@{ file = 'python.exe'; args = @() },
        [pscustomobject]@{ file = 'python'; args = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not $CommandInvoker -and -not (Test-LauncherCommandAvailable $candidate.file)) { continue }
        $result = Invoke-LauncherExternalCommand `
            -FilePath $candidate.file `
            -ArgumentList (@($candidate.args) + @('-c', 'import sys; print(".".join(map(str, sys.version_info[:3])))')) `
            -CommandInvoker $CommandInvoker
        if ($result.exit_code -eq 0 -and (Test-LauncherVersion -Current $result.stdout -Required '3.12.0')) {
            $versionMatch = [regex]::Match($result.stdout, '\d+\.\d+\.\d+')
            if ($versionMatch.Success -and ([version]$versionMatch.Value).Major -eq 3 -and ([version]$versionMatch.Value).Minor -eq 12) {
                return [pscustomobject][ordered]@{
                    file = $candidate.file
                    prefix_arguments = @($candidate.args)
                    version = $versionMatch.Value
                }
            }
        }
    }
    return $null
}

function Get-LauncherWindowsVersion {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        return [string]$os.Version
    } catch {
        return [Environment]::OSVersion.Version.ToString()
    }
}

function Get-LauncherPortOwners {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int[]]$Ports)

    $requestedPorts = @($Ports | Where-Object { $_ -ge 1 -and $_ -le 65535 } | Sort-Object -Unique)
    if ($requestedPorts.Count -eq 0) { return @() }
    $owners = @{}
    $netTcpQuerySucceeded = $false
    try {
        $connections = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { [int]$_.LocalPort -in $requestedPorts })
        $netTcpQuerySucceeded = $true
        foreach ($connection in $connections) {
            $port = [int]$connection.LocalPort
            if ($owners.ContainsKey($port)) { continue }
            $process = Get-Process -Id ([int]$connection.OwningProcess) -ErrorAction SilentlyContinue
            $owners[$port] = [pscustomobject][ordered]@{
                port = $port
                pid = [int]$connection.OwningProcess
                process_name = if ($process) { [string]$process.ProcessName } else { 'unknown' }
                command_line = Get-LauncherProcessCommandLine -ProcessId ([int]$connection.OwningProcess)
            }
        }
    } catch {
        # Fall through to netstat for older Windows builds.
    }
    if (-not $netTcpQuerySucceeded) {
        try {
            foreach ($line in @(netstat -ano -p tcp 2>$null)) {
                $text = [string]$line
                if ($text -notmatch '(?i)LISTENING') { continue }
                $parts = @($text -split '\s+' | Where-Object { $_ })
                if ($parts.Count -lt 2 -or $parts[1] -notmatch ':(\d+)$') { continue }
                $port = [int]$matches[1]
                if ($port -notin $requestedPorts -or $owners.ContainsKey($port) -or $parts[-1] -notmatch '^\d+$') { continue }
                $pidValue = [int]$parts[-1]
                $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
                $owners[$port] = [pscustomobject][ordered]@{
                    port = $port
                    pid = $pidValue
                    process_name = if ($process) { [string]$process.ProcessName } else { 'unknown' }
                    command_line = Get-LauncherProcessCommandLine -ProcessId $pidValue
                }
            }
        } catch {
            return @()
        }
    }
    return @($owners.Values)
}

function Get-LauncherPortOwner {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Port)

    return Get-LauncherPortOwners -Ports @($Port) |
        Where-Object { $_.port -eq $Port } |
        Select-Object -First 1
}

function Get-LauncherProcessCommandLine {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        return [string]$process.CommandLine
    } catch {
        return ''
    }
}

function Get-NeuralCloudRegistryEntries {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$RegistryPath = ''
    )
    $path = if ($RegistryPath) { $RegistryPath } else { Join-Path $Root '.neuralcloud.pids.json' }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @() }
    try {
        $registry = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        return @((Get-LauncherObjectProperty $registry 'services' @()))
    } catch {
        return @()
    }
}

function Get-MistyAIDeploymentOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $script = @'
set +e
classify() {
  path="$1"
  label="$2"
  if ! test -e "$path" && ! test -L "$path"; then
    printf '%s=absent\n' "$label"
  elif test -L "$path"; then
    printf '%s=symlink\n' "$label"
  elif test -d "$path" && grep -qx 'neuralcloud-launcher-v1' "$path/.neuralcloud-launcher-deployment" 2>/dev/null; then
    printf '%s=managed\n' "$label"
  else
    printf '%s=unmanaged\n' "$label"
  fi
}
classify /opt/mistyai current
classify /opt/mistyai.previous previous
for item in current previous; do
  if test "$item" = current; then path=/opt/mistyai; else path=/opt/mistyai.previous; fi
  fingerprint=$(sed -n '1p' "$path/.neuralcloud-launcher-fingerprint" 2>/dev/null || true)
  image=$(sed -n '1p' "$path/.neuralcloud-launcher-image-id" 2>/dev/null || true)
  previous_image=$(sed -n '1p' "$path/.neuralcloud-launcher-previous-image" 2>/dev/null || true)
  printf '%s_fingerprint=%s\n' "$item" "$fingerprint"
  printf '%s_image=%s\n' "$item" "$image"
  printf '%s_previous_image=%s\n' "$item" "$previous_image"
done
'@
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description 'inspect MistyAI deployment ownership' -CommandInvoker $CommandInvoker
    $text = $result.stdout
    $current = if ($text -match '(?m)^current=(absent|managed|unmanaged|symlink)\s*$') { $matches[1] } else { 'unknown' }
    $previous = if ($text -match '(?m)^previous=(absent|managed|unmanaged|symlink)\s*$') { $matches[1] } else { 'unknown' }
    $currentFingerprintMatch = [regex]::Match($text, '(?m)^current_fingerprint=([0-9a-fA-F]{64})\s*$')
    $previousFingerprintMatch = [regex]::Match($text, '(?m)^previous_fingerprint=([0-9a-fA-F]{64})\s*$')
    $currentDeployedImageMatch = [regex]::Match($text, '(?m)^current_image=(sha256:[0-9a-fA-F]{64})\s*$')
    $currentImageMatch = [regex]::Match($text, '(?m)^current_previous_image=(sha256:[0-9a-fA-F]{64})\s*$')
    return [pscustomobject][ordered]@{
        current = $current
        previous = $previous
        current_fingerprint = if ($currentFingerprintMatch.Success) { $currentFingerprintMatch.Groups[1].Value.ToLowerInvariant() } else { '' }
        previous_fingerprint = if ($previousFingerprintMatch.Success) { $previousFingerprintMatch.Groups[1].Value.ToLowerInvariant() } else { '' }
        current_image_id = if ($currentDeployedImageMatch.Success) { $currentDeployedImageMatch.Groups[1].Value } else { '' }
        current_previous_image = if ($currentImageMatch.Success) { $currentImageMatch.Groups[1].Value } else { '' }
        probe_succeeded = $result.exit_code -eq 0
    }
}

function Get-MistyAIUnitIdentityBash {
    return @'
verify_mistyai_unit_identity() {
  unit="$1"
  deployment_root="${2:-/opt/mistyai}"
  case "$unit" in
    mistyai-body.service) expected_exec=/opt/mistyai/.venv/bin/mistyai-body ;;
    mistyai-operator.service) expected_exec=/opt/mistyai/.venv/bin/mistyai-operator-api ;;
    *) echo "unexpected MistyAI unit name: $unit" >&2; return 64 ;;
  esac
  unit_file="/etc/systemd/system/$unit"
  expected_file="$deployment_root/deploy/systemd/$unit"
  test -f "$unit_file" && ! test -L "$unit_file"
  test -f "$expected_file" && ! test -L "$expected_file"
  cmp -s -- "$unit_file" "$expected_file"
  test "$(systemctl show "$unit" -p FragmentPath --value 2>/dev/null)" = "$unit_file"
  test -z "$(systemctl show "$unit" -p DropInPaths --value 2>/dev/null)"
  systemctl show "$unit" -p ExecStart --value 2>/dev/null | grep -Fq "path=$expected_exec ;"
}
verify_mistyai_owned_unit_identity() {
  unit="$1"
  if verify_mistyai_unit_identity "$unit" /opt/mistyai; then return 0; fi
  grep -qx 'neuralcloud-launcher-v1' /opt/mistyai.previous/.neuralcloud-launcher-deployment 2>/dev/null &&
    verify_mistyai_unit_identity "$unit" /opt/mistyai.previous
}
verify_mistyai_unit_absent() {
  unit="$1"
  unit_file="/etc/systemd/system/$unit"
  ! test -e "$unit_file" && ! test -L "$unit_file"
  load_state=$(systemctl show "$unit" -p LoadState --value 2>/dev/null) || return 1
  test "$load_state" = not-found
  ! systemctl is-active --quiet "$unit"
  ! systemctl is-enabled --quiet "$unit"
}
verify_mistyai_unit_owned_or_absent() {
  unit="$1"
  unit_file="/etc/systemd/system/$unit"
  if test -e "$unit_file" || test -L "$unit_file"; then
    verify_mistyai_owned_unit_identity "$unit"
  else
    verify_mistyai_unit_absent "$unit"
  fi
}
'@
}

function Get-MistyAIManagedPorts {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $script = (Get-MistyAIUnitIdentityBash) + "`n" + @'
set +e
marker=/opt/mistyai/.neuralcloud-launcher-deployment
compose=/opt/mistyai/deploy/compose.yaml
if ! grep -qx 'neuralcloud-launcher-v1' "$marker" 2>/dev/null; then
  exit 0
fi
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in ''|/mnt/[a-zA-Z]/*) exit 0 ;; esac
if systemctl is-active --quiet mistyai-body.service && \
   verify_mistyai_unit_identity mistyai-body.service; then
  printf 'port=8787\n'
fi
compose_ready=yes
for service in desktop agent loopback-gateway; do
  if ! timeout --signal=TERM --kill-after=1s 5s "$docker_bin" compose -f "$compose" ps --services --filter status=running 2>/dev/null | grep -Fxq "$service"; then
    compose_ready=no
  fi
done
if test "$compose_ready" = yes; then
  if timeout --signal=TERM --kill-after=1s 5s "$docker_bin" compose -f "$compose" port loopback-gateway 6080 2>/dev/null | grep -Eq '(^|:)6080$'; then
    printf 'port=6080\n'
  fi
  if timeout --signal=TERM --kill-after=1s 5s "$docker_bin" compose -f "$compose" port loopback-gateway 8765 2>/dev/null | grep -Eq '(^|:)8765$'; then
    printf 'port=8765\n'
  fi
fi
'@
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description 'inspect launcher-managed MistyAI ports' -TimeoutSeconds 30 -CommandInvoker $CommandInvoker
    if ($result.exit_code -ne 0) { return @() }
    return @([regex]::Matches($result.stdout, '(?m)^port=(\d+)\s*$') | ForEach-Object {
        [int]$_.Groups[1].Value
    } | Sort-Object -Unique)
}

function Test-LauncherKnownPortOwner {
    param(
        [Parameter(Mandatory = $true)][object]$Owner,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Root,
        [int[]]$KnownMistyPorts = @()
    )
    $pidValue = [int](Get-LauncherObjectProperty $Owner 'pid' 0)
    $processName = ([string](Get-LauncherObjectProperty $Owner 'process_name' '')).ToLowerInvariant()
    $commandLine = [string](Get-LauncherObjectProperty $Owner 'command_line' '')
    $expectedModules = @{
        8790 = 'gui.runtime_server'
        8770 = 'harness.runtime.service'
        8771 = 'harness.cognitive.service'
        8772 = 'harness.homeostasis.service'
    }
    foreach ($entry in @(Get-NeuralCloudRegistryEntries -Root $Root)) {
        if ($expectedModules.ContainsKey($Port) -and
            [int](Get-LauncherObjectProperty $entry 'pid' 0) -eq $pidValue -and
            [int](Get-LauncherObjectProperty $entry 'port' 0) -eq $Port) {
            $module = [string](Get-LauncherObjectProperty $entry 'module' '')
            $expectedPython = Join-Path $Root '.venv\Scripts\python.exe'
            if ($module -eq $expectedModules[$Port] -and
                $processName -match '^pythonw?$' -and
                $commandLine -match [regex]::Escape($module) -and
                $commandLine.IndexOf($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                return $true
            }
        }
    }
    if ($Port -in $KnownMistyPorts -and
        $processName -match '^(?:wsl|wslhost|wslrelay|com\.docker\.backend|docker|vpnkit)(?:\.exe)?$') {
        return $true
    }
    return $false
}

function Get-LauncherDockerFailureSummary {
    param([Parameter(Mandatory = $true)][object]$Result)

    $detail = Protect-LauncherText -Text (([string]$Result.stderr + "`n" + [string]$Result.stdout).Trim())
    if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) + '...' }
    $reason = if ($Result.exit_code -eq 124 -or (Get-LauncherObjectProperty $Result 'timed_out' $false)) {
        'Docker engine probe timed out'
    } elseif ($detail -match '(?i)(?:sailor-ingest|engine)\.sock') {
        'Docker Desktop socket startup failure'
    } else {
        'installed; Linux engine unavailable'
    }
    return [pscustomobject]@{
        current = if ($detail) { "$reason`: $detail" } else { $reason }
        remediation = 'Use tools/docker/Start-DockerDesktop.ps1 for bounded Docker Desktop startup/recovery, then re-run dev.ps1 doctor. Do not delete live sockets or force-stop the Docker backend. If initial setup is incomplete, open Docker Desktop, accept its agreement, and select Linux containers.'
    }
}

function Get-LauncherPrerequisiteChecks {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker,
        [switch]$IncludeRuntimePorts
    )

    $checks = New-Object System.Collections.Generic.List[object]
    $rootReady = (Test-Path -LiteralPath (Join-Path $Root 'start.ps1') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root 'uv.lock') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root 'presets\runtime\grounded-mistyai-v1.json') -PathType Leaf)
    [void]$checks.Add((New-LauncherCheck -Id 'repo.neuralcloud' -Category 'repository' `
        -Current $Root -Required 'NeuralCloud repository with start.ps1, uv.lock, and grounded manifest' `
        -Status $(if ($rootReady) { 'pass' } else { 'blocked' }) `
        -Remediation 'Run dev.ps1 from the NeuralCloud repository root.'))

    $mistyReady = (Test-Path -LiteralPath (Join-Path $MistyAIPath 'pyproject.toml') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $MistyAIPath 'uv.lock') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $MistyAIPath 'deploy\compose.yaml') -PathType Leaf)
    [void]$checks.Add((New-LauncherCheck -Id 'repo.mistyai' -Category 'repository' `
        -Current $MistyAIPath -Required 'MistyAI repository with uv.lock and deploy/compose.yaml' `
        -Status $(if ($mistyReady) { 'pass' } else { 'blocked' }) `
        -Remediation 'Pass -MistyAIPath with the local MistyAI repository path.'))

    $windowsVersion = Get-LauncherWindowsVersion
    $windowsReady = Test-LauncherVersion -Current $windowsVersion -Required '10.0.22000'
    [void]$checks.Add((New-LauncherCheck -Id 'windows.version' -Category 'windows' `
        -Current $windowsVersion -Required 'Windows 11 build 22000 or newer' `
        -Status $(if ($windowsReady) { 'pass' } else { 'blocked' }) `
        -Remediation 'Upgrade this computer to a supported Windows 11 release.'))

    $gitAvailable = (Test-LauncherCommandAvailable 'git.exe') -or [bool]$CommandInvoker
    $gitVersion = ''
    if ($gitAvailable) {
        $gitResult = Invoke-LauncherExternalCommand -FilePath 'git.exe' -ArgumentList @('--version') -CommandInvoker $CommandInvoker
        $gitAvailable = $gitResult.exit_code -eq 0
        $gitVersion = $gitResult.stdout.Trim()
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.git' -Category 'windows' `
        -Current $(if ($gitAvailable) { $gitVersion } else { 'not found' }) -Required 'Git for Windows' `
        -Status $(if ($gitAvailable) { 'pass' } else { 'missing' }) `
        -Remediation 'Install with: winget install --id Git.Git --exact'))

    $python = Get-LauncherPythonCommand -CommandInvoker $CommandInvoker
    [void]$checks.Add((New-LauncherCheck -Id 'windows.python' -Category 'windows' `
        -Current $(if ($python) { $python.version } else { 'Python 3.12 not found' }) -Required 'Python 3.12.x' `
        -Status $(if ($python) { 'pass' } else { 'missing' }) `
        -Remediation 'Install with: winget install --id Python.Python.3.12 --exact'))

    $windowsUvReady = $false
    $windowsUvVersion = 'not found'
    if ($python) {
        $windowsUvProbe = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @('-m', 'uv', '--version')) -CommandInvoker $CommandInvoker
        $windowsUvMatch = [regex]::Match($windowsUvProbe.stdout, '(?i)uv\s+([^\s]+)')
        $windowsUvReady = $windowsUvProbe.exit_code -eq 0 -and $windowsUvMatch.Success -and (Test-LauncherVersion -Current $windowsUvMatch.Groups[1].Value -Required $script:RequiredUvVersion -Exact)
        if ($windowsUvMatch.Success) { $windowsUvVersion = $windowsUvMatch.Groups[1].Value }
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.uv' -Category 'windows' `
        -Current $windowsUvVersion -Required "uv $($script:RequiredUvVersion)" `
        -Status $(if ($windowsUvReady) { 'pass' } else { 'missing' }) `
        -Remediation "Install with: py -3.12 -m pip install uv==$($script:RequiredUvVersion)"))

    $wslAvailable = (Test-LauncherCommandAvailable 'wsl.exe') -or [bool]$CommandInvoker
    $wslStatus = $null
    if ($wslAvailable) {
        $wslStatus = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--status') -TimeoutSeconds 10 -CommandInvoker $CommandInvoker
        $wslAvailable = $wslStatus.exit_code -eq 0
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.wsl2' -Category 'windows' `
        -Current $(if ($wslAvailable) { 'available' } else { 'not available' }) -Required 'WSL2' `
        -Status $(if ($wslAvailable) { 'pass' } else { 'missing' }) `
        -Remediation 'Open an elevated terminal and run: wsl --install --no-distribution' `
        -RestartRequired:$(!$wslAvailable)))

    $distroAvailable = $false
    $distroVersion2 = $false
    if ($wslAvailable) {
        $distroResult = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--list', '--quiet') -TimeoutSeconds 10 -CommandInvoker $CommandInvoker
        $distroText = $distroResult.stdout.Replace([string][char]0, '')
        $distroAvailable = $distroResult.exit_code -eq 0 -and @($distroText -split '[\r\n]+' | ForEach-Object { $_.Trim() }) -contains $Distro
        if ($distroAvailable) {
            $verboseResult = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--list', '--verbose') -TimeoutSeconds 10 -CommandInvoker $CommandInvoker
            $ansiPattern = [regex]::Escape([string][char]27) + '\[[0-9;?]*[ -/]*[@-~]'
            $verboseText = $verboseResult.stdout.Replace([string][char]0, '') -replace $ansiPattern, ''
            $distroPattern = '(?mi)^\s*\*?\s*' + [regex]::Escape($Distro) + '\s+\S+\s+2\s*$'
            $distroVersion2 = $verboseResult.exit_code -eq 0 -and $verboseText -match $distroPattern
        }
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.wsl_distro' -Category 'windows' `
        -Current $(if ($distroVersion2) { "$Distro (WSL2)" } elseif ($distroAvailable) { "$Distro (not WSL2)" } else { 'not installed' }) -Required "$Distro on WSL2" `
        -Status $(if ($distroVersion2) { 'pass' } elseif ($distroAvailable) { 'blocked' } else { 'missing' }) `
        -Remediation $(if ($distroAvailable) { "Convert with: wsl --set-version $Distro 2" } else { "Install with: wsl --install --distribution $Distro" }) `
        -RestartRequired:$(!$distroAvailable)))

    $virtualizationReady = $wslAvailable
    if (-not $virtualizationReady) {
        try {
            $computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
            $virtualizationReady = [bool]$computer.HypervisorPresent
        } catch { $virtualizationReady = $false }
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.virtualization' -Category 'windows' `
        -Current $(if ($virtualizationReady) { 'enabled' } else { 'not detected' }) `
        -Required 'Hardware virtualization enabled for WSL2' `
        -Status $(if ($virtualizationReady) { 'pass' } else { 'blocked' }) `
        -Remediation 'Enable CPU virtualization in firmware, then enable Virtual Machine Platform.'))

    $dockerAvailable = (Test-LauncherCommandAvailable 'docker.exe') -or [bool]$CommandInvoker
    $dockerEngine = $false
    $dockerVersion = ''
    $dockerRemediation = 'Install with: winget install --id Docker.DockerDesktop --exact'
    if ($dockerAvailable) {
        $dockerResult = Invoke-LauncherExternalCommand -FilePath 'docker.exe' -ArgumentList @('info', '--format', '{{.OSType}}') -TimeoutSeconds 10 -CommandInvoker $CommandInvoker
        $dockerEngine = $dockerResult.exit_code -eq 0 -and $dockerResult.stdout.Trim() -eq 'linux'
        $dockerFailure = Get-LauncherDockerFailureSummary -Result $dockerResult
        $dockerVersion = if ($dockerEngine) { 'Linux engine' } else { $dockerFailure.current }
        $dockerRemediation = $dockerFailure.remediation
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.docker_desktop' -Category 'windows' `
        -Current $(if ($dockerAvailable) { $dockerVersion } else { 'not found' }) -Required 'Docker Desktop with running Linux engine' `
        -Status $(if (-not $dockerAvailable) { 'missing' } elseif ($dockerEngine) { 'pass' } else { 'action_required' }) `
        -Remediation $dockerRemediation))

    $composeReady = $false
    if ($dockerAvailable) {
        $composeResult = Invoke-LauncherExternalCommand -FilePath 'docker.exe' -ArgumentList @('compose', 'version') -TimeoutSeconds 10 -CommandInvoker $CommandInvoker
        $composeReady = $composeResult.exit_code -eq 0
    }
    [void]$checks.Add((New-LauncherCheck -Id 'windows.docker_compose' -Category 'windows' `
        -Current $(if ($composeReady) { 'available' } else { 'unavailable' }) -Required 'Docker Compose v2' `
        -Status $(if ($composeReady) { 'pass' } else { 'missing' }) `
        -Remediation 'Update or repair Docker Desktop to install Docker Compose v2.'))

    if ($distroAvailable) {
        $wslProbe = @'
set +e
source /etc/os-release 2>/dev/null
printf 'ubuntu=%s\n' "${ID:-}:${VERSION_ID:-}"
test -d /run/systemd/system && printf 'systemd=yes\n' || printf 'systemd=no\n'
python_path=$(command -v python3.12 2>/dev/null || true)
uv_path=$(command -v uv 2>/dev/null || true)
docker_path=$(command -v docker 2>/dev/null || true)
printf 'python_path=%s\n' "$python_path"
printf 'uv_path=%s\n' "$uv_path"
printf 'docker_path=%s\n' "$docker_path"
if test -n "$python_path"; then "$python_path" --version 2>&1 | sed 's/^/python=/'; fi
if test -n "$uv_path"; then "$uv_path" --version 2>&1 | sed 's/^/uv=/'; fi
if test -n "$docker_path"; then timeout --signal=TERM --kill-after=1s 10s "$docker_path" info --format '{{.OSType}}' 2>/dev/null | sed 's/^/docker=/'; fi
if test -n "$docker_path"; then timeout --signal=TERM --kill-after=1s 10s "$docker_path" compose version 2>/dev/null | sed 's/^/compose=/'; fi
'@
        $probe = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $wslProbe -Description 'check Ubuntu, systemd, Python, uv, and Docker integration' -TimeoutSeconds 30 -CommandInvoker $CommandInvoker
        $probeText = $probe.stdout + "`n" + $probe.stderr
        $ubuntuReady = $probeText -match '(?m)^ubuntu=ubuntu:24\.04\s*$'
        $systemdReady = $probeText -match '(?m)^systemd=yes\s*$'
        $wslPythonPathMatch = [regex]::Match($probeText, '(?m)^python_path=([^\r\n]*)')
        $wslPythonPath = if ($wslPythonPathMatch.Success) { $wslPythonPathMatch.Groups[1].Value.Trim() } else { '' }
        $wslPythonNative = $wslPythonPath.StartsWith('/') -and $wslPythonPath -notmatch '^/mnt/[a-zA-Z]/'
        $wslPythonMatch = [regex]::Match($probeText, '(?m)^python=Python\s+([^\r\n]+)')
        $wslPythonReady = $wslPythonNative -and $wslPythonMatch.Success -and $wslPythonMatch.Groups[1].Value -match '^3\.12(?:\.|$)'
        $uvPathMatch = [regex]::Match($probeText, '(?m)^uv_path=([^\r\n]*)')
        $uvPath = if ($uvPathMatch.Success) { $uvPathMatch.Groups[1].Value.Trim() } else { '' }
        $uvNative = $uvPath.StartsWith('/') -and $uvPath -notmatch '^/mnt/[a-zA-Z]/'
        $uvMatch = [regex]::Match($probeText, '(?m)^uv=uv\s+([^\s\r\n]+)')
        $uvReady = $uvNative -and $uvMatch.Success -and (Test-LauncherVersion -Current $uvMatch.Groups[1].Value -Required $script:RequiredUvVersion -Exact)
        $dockerPathMatch = [regex]::Match($probeText, '(?m)^docker_path=([^\r\n]*)')
        $dockerPath = if ($dockerPathMatch.Success) { $dockerPathMatch.Groups[1].Value.Trim() } else { '' }
        $dockerNative = $dockerPath.StartsWith('/') -and $dockerPath -notmatch '^/mnt/[a-zA-Z]/'
        $wslDockerReady = $dockerNative -and $probeText -match '(?m)^docker=linux\s*$'
        $wslComposeReady = $dockerNative -and $probeText -match '(?m)^compose=Docker Compose version'
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.ubuntu' -Category 'wsl' -Current $(if ($ubuntuReady) { 'ubuntu:24.04' } else { 'wrong or unreadable release' }) -Required 'Ubuntu 24.04' -Status $(if ($ubuntuReady) { 'pass' } else { 'blocked' }) -Remediation "Install and select the $Distro Ubuntu 24.04 distribution."))
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.systemd' -Category 'wsl' -Current $(if ($systemdReady) { 'running' } else { 'not running' }) -Required 'systemd enabled' -Status $(if ($systemdReady) { 'pass' } else { 'action_required' }) -Remediation 'Enable systemd in /etc/wsl.conf, then run wsl --shutdown and restart the distribution.' -RestartRequired:$(!$systemdReady)))
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.python' -Category 'wsl' -Current $(if ($wslPythonMatch.Success) { "$($wslPythonMatch.Groups[1].Value) at $wslPythonPath" } else { 'not found' }) -Required 'native WSL Python 3.12.x' -Status $(if ($wslPythonReady) { 'pass' } else { 'missing' }) -Remediation 'Install python3.12, python3.12-venv, and python3-pip with apt; a Windows executable under /mnt/<drive> is not accepted.'))
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.uv' -Category 'wsl' -Current $(if ($uvMatch.Success) { "$($uvMatch.Groups[1].Value) at $uvPath" } else { 'not found' }) -Required "native WSL uv $($script:RequiredUvVersion)" -Status $(if ($uvReady) { 'pass' } else { 'missing' }) -Remediation "As WSL root, run: python3.12 -m pip install --break-system-packages uv==$($script:RequiredUvVersion); a Windows executable under /mnt/<drive> is not accepted."))
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.docker_integration' -Category 'wsl' -Current $(if ($wslDockerReady) { "Linux engine reachable via $dockerPath" } else { 'not reachable through a native WSL integration path' }) -Required 'Docker Desktop WSL integration enabled' -Status $(if ($wslDockerReady) { 'pass' } else { 'action_required' }) -Remediation "In Docker Desktop, enable Resources > WSL Integration for $Distro; a Windows docker.exe under /mnt/<drive> is not accepted."))
        [void]$checks.Add((New-LauncherCheck -Id 'wsl.docker_compose' -Category 'wsl' -Current $(if ($wslComposeReady) { 'available' } else { 'unavailable' }) -Required 'Docker Compose v2 in WSL' -Status $(if ($wslComposeReady) { 'pass' } else { 'action_required' }) -Remediation "Enable Docker Desktop integration for $Distro and restart the distribution."))
    }

    if ($IncludeRuntimePorts) {
        $knownMistyPorts = if ($distroAvailable) { @(Get-MistyAIManagedPorts -Distro $Distro -CommandInvoker $CommandInvoker) } else { @() }
        $runtimePorts = @(6080, 8765, 8790, 8767, 8768, 8769, 8770, 8771, 8772, 8787)
        $portOwners = @(Get-LauncherPortOwners -Ports $runtimePorts)
        foreach ($port in $runtimePorts) {
            $owner = $portOwners | Where-Object { $_.port -eq $port } | Select-Object -First 1
            $known = $null -ne $owner -and (Test-LauncherKnownPortOwner -Owner $owner -Port $port -Root $Root -KnownMistyPorts $knownMistyPorts)
            $status = if ($null -eq $owner -or $known) { 'pass' } else { 'blocked' }
            $current = if ($null -eq $owner) { 'free' } elseif ($known) { "known launcher component PID=$($owner.pid)" } else { "PID=$($owner.pid) $($owner.process_name)" }
            [void]$checks.Add((New-LauncherCheck -Id "runtime.port.$port" -Category 'runtime' -Current $current -Required 'free or owned by the matching NeuralCloud/MistyAI component' -Status $status -Remediation "Stop the unrelated process that listens on port $port, then retry."))
        }
    }
    return $checks.ToArray()
}

function Test-LauncherChecksReady {
    param([object[]]$Checks)
    return @($Checks | Where-Object { $_.status -in @('missing', 'blocked', 'action_required') }).Count -eq 0
}

function Confirm-LauncherAction {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [switch]$NonInteractive,
        [switch]$Json
    )
    if ($NonInteractive -or $Json) { return $false }
    $answer = Read-Host "$Prompt [y/N]"
    return $answer -match '^(?i)y(?:es)?$'
}

function Invoke-LauncherInstallPrerequisites {
    param(
        [Parameter(Mandatory = $true)][object[]]$Checks,
        [Parameter(Mandatory = $true)][string]$Distro,
        [switch]$NonInteractive,
        [switch]$Json,
        [scriptblock]$CommandInvoker
    )

    $actions = New-Object System.Collections.Generic.List[object]
    $needsRestart = $false
    $byId = @{}
    foreach ($check in $Checks) { $byId[$check.id] = $check }
    $wingetAvailable = (Test-LauncherCommandAvailable 'winget.exe') -or [bool]$CommandInvoker

    foreach ($definition in @(
        [pscustomobject]@{ id = 'windows.git'; package = 'Git.Git'; label = 'Git for Windows' },
        [pscustomobject]@{ id = 'windows.python'; package = 'Python.Python.3.12'; label = 'Python 3.12' },
        [pscustomobject]@{ id = 'windows.docker_desktop'; package = 'Docker.DockerDesktop'; label = 'Docker Desktop' }
    )) {
        if (-not $byId.ContainsKey($definition.id) -or $byId[$definition.id].status -ne 'missing') { continue }
        if (-not $wingetAvailable) {
            [void]$actions.Add([pscustomobject]@{ id = $definition.id; status = 'manual'; detail = 'winget is unavailable' })
            continue
        }
        if (-not (Confirm-LauncherAction -Prompt "Install $($definition.label) with winget?" -NonInteractive:$NonInteractive -Json:$Json)) {
            [void]$actions.Add([pscustomobject]@{ id = $definition.id; status = 'declined'; detail = $byId[$definition.id].remediation })
            continue
        }
        $result = Invoke-LauncherExternalCommand -FilePath 'winget.exe' -ArgumentList @(
            'install', '--id', $definition.package, '--exact',
            '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
        ) -CommandInvoker $CommandInvoker
        if ($result.exit_code -ne 0) {
            $detail = Protect-LauncherText -Text (($result.stderr + "`n" + $result.stdout).Trim())
            [void]$actions.Add([pscustomobject]@{ id = $definition.id; status = 'failed'; detail = $detail })
        } else {
            [void]$actions.Add([pscustomobject]@{ id = $definition.id; status = 'installed'; detail = 'Re-run the launcher so the new command is discovered.' })
            $needsRestart = $true
        }
    }

    if ($byId.ContainsKey('windows.uv') -and $byId['windows.uv'].status -eq 'missing') {
        if (Confirm-LauncherAction -Prompt "Install uv $($script:RequiredUvVersion) for Windows Python 3.12?" -NonInteractive:$NonInteractive -Json:$Json) {
            $python = Get-LauncherPythonCommand -CommandInvoker $CommandInvoker
            if (-not $python) {
                [void]$actions.Add([pscustomobject]@{ id = 'windows.uv'; status = 'failed'; detail = 'Python 3.12 is not available yet; re-run after Python installation.' })
            } else {
                $result = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @('-m', 'pip', 'install', "uv==$($script:RequiredUvVersion)")) -CommandInvoker $CommandInvoker
                $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
                [void]$actions.Add([pscustomobject]@{ id = 'windows.uv'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
            }
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'windows.uv'; status = 'declined'; detail = $byId['windows.uv'].remediation })
        }
    }

    if ($byId.ContainsKey('windows.wsl2') -and $byId['windows.wsl2'].status -eq 'missing') {
        if (Confirm-LauncherAction -Prompt 'Enable WSL2? This requires an elevated Windows operation and usually a restart.' -NonInteractive:$NonInteractive -Json:$Json) {
            $result = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--install', '--no-distribution') -CommandInvoker $CommandInvoker
            $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl2'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
            if ($result.exit_code -eq 0) { $needsRestart = $true }
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl2'; status = 'declined'; detail = $byId['windows.wsl2'].remediation })
        }
    } elseif ($byId.ContainsKey('windows.wsl_distro') -and $byId['windows.wsl_distro'].status -eq 'missing') {
        if (Confirm-LauncherAction -Prompt "Install the $Distro WSL distribution? This may require a restart or first-run setup." -NonInteractive:$NonInteractive -Json:$Json) {
            $result = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--install', '--distribution', $Distro) -CommandInvoker $CommandInvoker
            $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl_distro'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
            if ($result.exit_code -eq 0) { $needsRestart = $true }
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl_distro'; status = 'declined'; detail = $byId['windows.wsl_distro'].remediation })
        }
    } elseif ($byId.ContainsKey('windows.wsl_distro') -and $byId['windows.wsl_distro'].status -eq 'blocked') {
        if (Confirm-LauncherAction -Prompt "Convert $Distro to WSL2?" -NonInteractive:$NonInteractive -Json:$Json) {
            $result = Invoke-LauncherExternalCommand -FilePath 'wsl.exe' -ArgumentList @('--set-version', $Distro, '2') -CommandInvoker $CommandInvoker
            $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl_distro'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'windows.wsl_distro'; status = 'declined'; detail = $byId['windows.wsl_distro'].remediation })
        }
    }

    $needsWslPackages = ($byId.ContainsKey('wsl.python') -and $byId['wsl.python'].status -eq 'missing')
    if ($needsWslPackages) {
        if (Confirm-LauncherAction -Prompt "Install Python 3.12 support inside $Distro with apt as root?" -NonInteractive:$NonInteractive -Json:$Json) {
            $script = 'set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y python3.12 python3.12-venv python3-pip unzip ca-certificates'
            $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description 'install Ubuntu Python packages with apt' -CommandInvoker $CommandInvoker
            $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
            [void]$actions.Add([pscustomobject]@{ id = 'wsl.python'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'wsl.python'; status = 'declined'; detail = $byId['wsl.python'].remediation })
        }
    }

    $needsUv = ($byId.ContainsKey('wsl.uv') -and $byId['wsl.uv'].status -eq 'missing')
    if ($needsUv) {
        if (Confirm-LauncherAction -Prompt "Install uv $($script:RequiredUvVersion) to /usr/local inside $Distro as root?" -NonInteractive:$NonInteractive -Json:$Json) {
            $script = "set -e; python3.12 -m pip install --break-system-packages uv==$($script:RequiredUvVersion)"
            $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description "install uv $($script:RequiredUvVersion) with pip" -CommandInvoker $CommandInvoker
            $status = if ($result.exit_code -eq 0) { 'installed' } else { 'failed' }
            [void]$actions.Add([pscustomobject]@{ id = 'wsl.uv'; status = $status; detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) })
        } else {
            [void]$actions.Add([pscustomobject]@{ id = 'wsl.uv'; status = 'declined'; detail = $byId['wsl.uv'].remediation })
        }
    }

    foreach ($manualId in @('windows.virtualization', 'windows.docker_desktop', 'wsl.systemd', 'wsl.docker_integration', 'wsl.docker_compose')) {
        if ($byId.ContainsKey($manualId) -and $byId[$manualId].status -in @('blocked', 'action_required')) {
            [void]$actions.Add([pscustomobject]@{
                id = $manualId
                status = 'manual'
                detail = $byId[$manualId].remediation
            })
            if ($byId[$manualId].restart_required) { $needsRestart = $true }
        }
    }

    return [pscustomobject][ordered]@{
        actions = $actions.ToArray()
        changed = @($actions | Where-Object { $_.status -eq 'installed' }).Count -gt 0
        restart_required = $needsRestart
        unresolved = @($actions | Where-Object { $_.status -ne 'installed' }).Count -gt 0
    }
}

function Get-NeuralDependencyFingerprint {
    param([Parameter(Mandatory = $true)][string]$Root)
    $files = @('pyproject.toml', 'uv.lock')
    return Get-WorkspaceFingerprint -Root $Root -Files $files
}

function Initialize-NeuralCloudEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][object]$State,
        [scriptblock]$CommandInvoker
    )

    $python = Get-LauncherPythonCommand -CommandInvoker $CommandInvoker
    if (-not $python) {
        Throw-LauncherFailure -Message 'Python 3.12 is required to prepare NeuralCloud.' -ExitCode $script:ExitNeedsAction
    }
    $uvProbe = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @('-m', 'uv', '--version')) -CommandInvoker $CommandInvoker
    $uvReady = $uvProbe.exit_code -eq 0 -and (Test-LauncherVersion -Current $uvProbe.stdout -Required $script:RequiredUvVersion -Exact)
    if (-not $uvReady) {
        Throw-LauncherFailure -Message "uv $($script:RequiredUvVersion) is required for NeuralCloud. Run the reported install remediation first." -ExitCode $script:ExitNeedsAction
    }

    $fingerprint = Get-NeuralDependencyFingerprint -Root $Root
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    $recordedFingerprint = [string](Get-LauncherObjectProperty $State 'neural_dependency_fingerprint' '')
    $venvPythonReady = $false
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvProbe = Invoke-LauncherExternalCommand -FilePath $venvPython -ArgumentList @(
            '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
        ) -CommandInvoker $CommandInvoker
        $venvPythonReady = $venvProbe.exit_code -eq 0 -and $venvProbe.stdout.Trim() -eq '3.12'
    }
    if ($recordedFingerprint -eq $fingerprint -and $venvPythonReady) {
        return [pscustomobject]@{ changed = $false; fingerprint = $fingerprint }
    }
    $sync = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @(
        '-m', 'uv', 'sync', '--frozen', '--python', '3.12', '--extra', 'dev', '--extra', 'gui'
    )) -WorkingDirectory $Root -CommandInvoker $CommandInvoker
    if ($sync.exit_code -ne 0) {
        Throw-LauncherFailure -Message "NeuralCloud dependency sync failed: $(Protect-LauncherText (($sync.stderr + ' ' + $sync.stdout).Trim()))" -ExitCode $script:ExitNeuralCloud
    }
    $State.neural_dependency_fingerprint = $fingerprint
    return [pscustomobject]@{ changed = $true; fingerprint = $fingerprint }
}

function Test-LauncherPrivateKeyContent {
    param([Parameter(Mandatory = $true)][string]$Path)
    $reader = $null
    try {
        $reader = New-Object System.IO.StreamReader($Path, [System.Text.Encoding]::UTF8, $true, 8192)
        $buffer = New-Object char[] 8192
        $carry = ''
        while (($count = $reader.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $text = $carry + (New-Object string($buffer, 0, $count))
            if ($text -match '-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----' -or
                $text -match '(?im)^PuTTY-User-Key-File-[23]:') { return $true }
            $carry = if ($text.Length -gt 128) { $text.Substring($text.Length - 128) } else { $text }
        }
        return $false
    } catch {
        return $false
    } finally {
        if ($reader) { $reader.Dispose() }
    }
}

function New-MistyAIWorkspaceSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [scriptblock]$CommandInvoker
    )

    $files = @(Get-LauncherWorkspaceFiles -Root $Root -CommandInvoker $CommandInvoker)
    $reserved = @($files | Where-Object { $_ -match '(?i)^\.neuralcloud-launcher-' })
    if ($reserved.Count -gt 0) {
        Throw-LauncherFailure -Message "MistyAI workspace uses launcher-reserved deployment metadata paths: $($reserved -join ', ')" -ExitCode $script:ExitConfiguration -Details $reserved
    }
    $sensitive = New-Object System.Collections.Generic.List[string]
    foreach ($relativePath in $files) {
        $fullPath = Join-Path $Root ($relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
        if ((Test-SensitiveRelativePath -Path $relativePath) -or
            (Test-LauncherPrivateKeyContent -Path $fullPath) -or
            (Test-LauncherPathHasReparsePoint -Root $Root -Path $fullPath)) {
            [void]$sensitive.Add($relativePath)
        }
    }
    if ($sensitive.Count -gt 0) {
        Throw-LauncherFailure -Message "MistyAI snapshot contains sensitive candidate files: $($sensitive -join ', ')" -ExitCode $script:ExitConfiguration -Details $sensitive.ToArray()
    }
    $fingerprint = Get-WorkspaceFingerprint -Root $Root -Files $files
    [void](New-Item -ItemType Directory -Path $StagingDirectory -Force)
    $archivePath = Join-Path $StagingDirectory "mistyai-$fingerprint.zip"
    $existingArchive = Get-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    if ($existingArchive -and (($existingArchive.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        Throw-LauncherFailure -Message "Refusing to overwrite a reparse-point snapshot archive: $archivePath" -ExitCode $script:ExitConfiguration
    }
    Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $temporary = $archivePath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $fileStream = $null
    $archive = $null
    try {
        $fileStream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $archive = New-Object System.IO.Compression.ZipArchive($fileStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
        foreach ($relativePath in $files) {
            $fullPath = Join-Path $Root ($relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
            $entry = $archive.CreateEntry($relativePath, [System.IO.Compression.CompressionLevel]::Optimal)
            $input = [System.IO.File]::OpenRead($fullPath)
            $output = $entry.Open()
            try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
        }
        $archive.Dispose()
        $archive = $null
        $fileStream.Dispose()
        $fileStream = $null
        $verificationFiles = @(Get-LauncherWorkspaceFiles -Root $Root -CommandInvoker $CommandInvoker)
        $fileListMatches = $verificationFiles.Count -eq $files.Count -and
            [string]::Join([string][char]0, $verificationFiles) -ceq [string]::Join([string][char]0, $files)
        $verificationFingerprint = if ($fileListMatches) {
            Get-WorkspaceFingerprint -Root $Root -Files $verificationFiles
        } else {
            ''
        }
        if (-not $fileListMatches -or $verificationFingerprint -ne $fingerprint) {
            Throw-LauncherFailure -Message 'MistyAI workspace changed while its deployment snapshot was being created. Retry after the working tree is stable.' -ExitCode $script:ExitConfiguration
        }
        Move-Item -LiteralPath $temporary -Destination $archivePath -Force
    } finally {
        if ($archive) { $archive.Dispose() }
        if ($fileStream) { $fileStream.Dispose() }
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    $archiveSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    return [pscustomobject][ordered]@{
        archive = $archivePath
        archive_sha256 = $archiveSha256
        fingerprint = $fingerprint
        files = $files
    }
}

function Invoke-CheckedLauncherWslCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$Script,
        [AllowEmptyString()][string]$WorkingDirectory = '',
        [AllowEmptyString()][string]$User = '',
        [scriptblock]$CommandInvoker,
        [AllowNull()][string[]]$SensitiveValues = @(),
        [int]$ExitCode = 20,
        [string]$Description = 'WSL command'
    )
    $result = Invoke-LauncherWslCommand -Distro $Distro -Script $Script -WorkingDirectory $WorkingDirectory -User $User -Description $Description -CommandInvoker $CommandInvoker -SensitiveValues $SensitiveValues
    if ($result.exit_code -ne 0) {
        $detail = Protect-LauncherText -Text (($result.stderr + "`n" + $result.stdout).Trim()) -Secrets $SensitiveValues
        Throw-LauncherFailure -Message "$Description failed: $detail" -ExitCode $ExitCode
    }
    return $result
}

function Get-MistyAIServiceState {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $script = @'
set +e
for unit in mistyai-body.service mistyai-operator.service; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf '%s=%s\n' "$unit" "${state:-unknown}"
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
  case "$enabled" in
    enabled|disabled) ;;
    ''|not-found) enabled=missing ;;
  esac
  printf '%s-enabled=%s\n' "$unit" "$enabled"
done
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in
  ''|/mnt/[a-zA-Z]/*) printf 'compose=unavailable\n' ;;
  *)
    compose_state=stopped
    container_count=0
    running_count=0
    healthy_count=0
    for service in desktop agent loopback-gateway; do
      if ! container_ids=$(timeout --signal=TERM --kill-after=1s 5s "$docker_bin" compose -f /opt/mistyai/deploy/compose.yaml ps -q "$service" 2>/dev/null); then
        compose_state=unavailable
        break
      fi
      container_id=$(printf '%s\n' "$container_ids" | head -n 1)
      if test -z "$container_id"; then continue; fi
      container_count=$((container_count + 1))
      if ! running=$(timeout --signal=TERM --kill-after=1s 5s "$docker_bin" inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null); then
        compose_state=unavailable
        break
      fi
      if ! health=$(timeout --signal=TERM --kill-after=1s 5s "$docker_bin" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id" 2>/dev/null); then
        compose_state=unavailable
        break
      fi
      if test "$running" = true; then running_count=$((running_count + 1)); fi
      if test "$running" = true && test "$health" = healthy; then healthy_count=$((healthy_count + 1)); fi
    done
    if test "$compose_state" = unavailable; then
      :
    elif test "$container_count" -eq 3 && test "$running_count" -eq 3 && test "$healthy_count" -eq 3; then
      compose_state=running
    elif test "$running_count" -gt 0; then
      compose_state=partial
    fi
    printf 'compose=%s\n' "$compose_state"
    ;;
esac
'@
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description 'inspect MistyAI systemd and Compose state' -TimeoutSeconds 30 -CommandInvoker $CommandInvoker
    $text = $result.stdout
    $body = if ($text -match '(?m)^mistyai-body\.service=([^\r\n]+)') { $matches[1].Trim() } else { 'unknown' }
    $operator = if ($text -match '(?m)^mistyai-operator\.service=([^\r\n]+)') { $matches[1].Trim() } else { 'unknown' }
    $bodyEnabled = if ($text -match '(?m)^mistyai-body\.service-enabled=([^\r\n]+)') { $matches[1].Trim() } else { 'unknown' }
    $operatorEnabled = if ($text -match '(?m)^mistyai-operator\.service-enabled=([^\r\n]+)') { $matches[1].Trim() } else { 'unknown' }
    $compose = if ($text -match '(?m)^compose=([^\r\n]+)') { $matches[1].Trim() } else { 'unknown' }
    if ($result.exit_code -ne 0) { $compose = 'unavailable' }
    return [pscustomobject][ordered]@{
        body = $body
        operator = $operator
        body_enabled = $bodyEnabled
        operator_enabled = $operatorEnabled
        compose = $compose
        running = $body -eq 'active' -and $operator -eq 'active' -and $compose -eq 'running'
    }
}

function Assert-MistyAIStableServiceState {
    param([Parameter(Mandatory = $true)][object]$State)

    $unstable = New-Object System.Collections.Generic.List[string]
    foreach ($component in @('body', 'operator')) {
        $value = [string](Get-LauncherObjectProperty $State $component 'unknown')
        if ($value -notin @('active', 'inactive', 'failed')) {
            [void]$unstable.Add("$component=$value")
        }
        $enabledValue = [string](Get-LauncherObjectProperty $State "${component}_enabled" 'unknown')
        if ($enabledValue -notin @('enabled', 'disabled', 'missing')) {
            [void]$unstable.Add("${component}_enabled=$enabledValue")
        }
    }
    $compose = [string](Get-LauncherObjectProperty $State 'compose' 'unknown')
    if ($compose -notin @('running', 'stopped')) {
        [void]$unstable.Add("compose=$compose")
    }
    if ($unstable.Count -gt 0) {
        Throw-LauncherFailure `
            -Message "MistyAI has a partial or transitional pre-existing state ($($unstable -join ', ')); no components were changed. Stabilize or stop it explicitly, then retry." `
            -ExitCode $script:ExitMistyAI
    }
}

function Deploy-MistyAIWorkspace {
    param(
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [scriptblock]$CommandInvoker
    )

    $snapshot = New-MistyAIWorkspaceSnapshot -Root $MistyAIPath -StagingDirectory $StagingDirectory -CommandInvoker $CommandInvoker
    $recorded = [string](Get-LauncherObjectProperty $State 'mistyai_deployment_fingerprint' '')
    $recordedSource = [string](Get-LauncherObjectProperty $State 'mistyai_source_path' '')
    $recordedDistro = [string](Get-LauncherObjectProperty $State 'mistyai_distro' '')
    $resolvedSource = [System.IO.Path]::GetFullPath($MistyAIPath).TrimEnd('\', '/')
    $ownership = Get-MistyAIDeploymentOwnership -Distro $Distro -CommandInvoker $CommandInvoker
    if (-not $ownership.probe_succeeded -or $ownership.current -eq 'unknown' -or $ownership.previous -eq 'unknown') {
        Throw-LauncherFailure -Message 'MistyAI deployment ownership could not be verified inside WSL.' -ExitCode $script:ExitMistyAI
    }
    if ($ownership.current -notin @('absent', 'managed')) {
        Throw-LauncherFailure -Message "Refusing to replace $($ownership.current) /opt/mistyai. Move or back up that directory manually, then re-run; the launcher never adopts an unmarked deployment." -ExitCode $script:ExitConfiguration
    }
    if ($ownership.previous -notin @('absent', 'managed')) {
        Throw-LauncherFailure -Message "Refusing to remove $($ownership.previous) /opt/mistyai.previous. Move or back up that path manually, then re-run." -ExitCode $script:ExitConfiguration
    }
    if ($ownership.current -eq 'absent' -and $ownership.previous -eq 'managed') {
        Throw-LauncherFailure -Message 'An interrupted managed deployment was found at /opt/mistyai.previous while /opt/mistyai is absent. Restore it with: sudo mv /opt/mistyai.previous /opt/mistyai, then re-run.' -ExitCode $script:ExitConfiguration
    }
    if ($ownership.current -eq 'managed' -and
        (-not $ownership.current_fingerprint -or -not $ownership.current_image_id)) {
        Throw-LauncherFailure -Message 'The managed /opt/mistyai deployment lacks fingerprint or image identity metadata. Preserve it and repair or move it manually before retrying.' -ExitCode $script:ExitConfiguration
    }
    if ($ownership.previous -eq 'managed' -and -not $ownership.previous_fingerprint) {
        Throw-LauncherFailure -Message 'The managed /opt/mistyai.previous deployment lacks fingerprint metadata. Preserve it and repair or move it manually before retrying.' -ExitCode $script:ExitConfiguration
    }
    if ($ownership.current -eq 'managed' -and $ownership.current_fingerprint -eq $snapshot.fingerprint) {
        $imageIdLiteral = Quote-LauncherBashLiteral $ownership.current_image_id
        $probeScript = "set -e; grep -qx 'neuralcloud-launcher-v1' /opt/mistyai/.neuralcloud-launcher-deployment; test -x /opt/mistyai/.venv/bin/mistyctl; docker_bin=`$(command -v docker 2>/dev/null || true); case `"`$docker_bin`" in ''|/mnt/[a-zA-Z]/*) exit 67 ;; esac; `"`$docker_bin`" image inspect $imageIdLiteral >/dev/null; `"`$docker_bin`" image tag $imageIdLiteral mistyai-desktop:m0"
        $probe = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $probeScript -Description 'validate the existing MistyAI deployment and image' -CommandInvoker $CommandInvoker
        if ($probe.exit_code -eq 0) {
            return [pscustomobject][ordered]@{
                changed = $false
                fingerprint = $snapshot.fingerprint
                previous_available = $ownership.previous -eq 'managed'
                previous_image_id = $ownership.current_previous_image
            }
        }
        Throw-LauncherFailure -Message 'The deployed MistyAI source matches, but its virtual environment or Docker image identity is unavailable.' -ExitCode $script:ExitMistyAI
    }
    if ($ownership.current -eq 'managed' -and
        ($recorded -ne $ownership.current_fingerprint -or $recordedSource -ne $resolvedSource -or $recordedDistro -ne $Distro)) {
        Throw-LauncherFailure -Message 'MistyAI launcher state does not match the managed current deployment. The previous version was preserved; inspect the interrupted deployment before retrying.' -ExitCode $script:ExitConfiguration
    }

    $uvResult = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script 'command -v uv' -Description 'locate the trusted WSL uv executable' -CommandInvoker $CommandInvoker
    $uvPath = $uvResult.stdout.Trim()
    if ($uvResult.exit_code -ne 0 -or -not $uvPath.StartsWith('/') -or $uvPath.StartsWith('/mnt/') -or $uvPath.StartsWith('/opt/mistyai')) {
        Throw-LauncherFailure -Message "uv $($script:RequiredUvVersion) is unavailable inside WSL." -ExitCode $script:ExitNeedsAction
    }

    $archiveWsl = ConvertTo-WslPath -Path $snapshot.archive -Distro $Distro
    $shortFingerprint = $snapshot.fingerprint.Substring(0, 16)
    $transactionId = [guid]::NewGuid().ToString('N')
    $stage = "/opt/mistyai.stage-$shortFingerprint-$transactionId"
    $candidateTag = "mistyai-desktop:launcher-$shortFingerprint-$transactionId"
    $expectedCurrentImage = if ($ownership.current -eq 'managed') { $ownership.current_image_id } else { '' }
    $prepareScript = @'
set -euo pipefail
archive=__ARCHIVE__
archive_sha256=__ARCHIVE_SHA256__
stage=__STAGE__
uv_bin=__UV_BIN__
fingerprint=__FINGERPRINT__
candidate_tag=__CANDIDATE_TAG__
expected_current_image=__EXPECTED_CURRENT_IMAGE__
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in ''|/mnt/[a-zA-Z]/*) echo 'native WSL Docker integration is unavailable' >&2; exit 67 ;; esac
case "$stage" in /opt/mistyai.stage-*) ;; *) echo 'unsafe deployment stage' >&2; exit 64 ;; esac
if test -e "$stage" || test -L "$stage"; then
  echo 'refusing to reuse an existing deployment stage' >&2
  exit 65
fi
actual_archive_sha256=$(sha256sum -- "$archive" | awk '{print $1}')
test "$actual_archive_sha256" = "$archive_sha256"
install -d -m 0755 "$stage" /var/cache/mistyai-uv
printf '%s\n' 'neuralcloud-launcher-v1' > "$stage/.neuralcloud-launcher-deployment"
candidate_ready=no
cleanup_stage() {
  status=$?
  trap - EXIT
  if test "$status" -ne 0; then
    if test "$candidate_ready" = yes; then "$docker_bin" image rm "$candidate_tag" >/dev/null 2>&1 || true; fi
    if test -d "$stage" && ! test -L "$stage" && grep -qx 'neuralcloud-launcher-v1' "$stage/.neuralcloud-launcher-deployment" 2>/dev/null; then
      rm -rf -- "$stage"
    fi
  fi
  exit "$status"
}
trap cleanup_stage EXIT
old_image=$("$docker_bin" image inspect --format '{{.Id}}' mistyai-desktop:m0 2>/dev/null || true)
if test -n "$expected_current_image"; then
  if test "$old_image" != "$expected_current_image"; then
    echo 'the canonical MistyAI image no longer matches the managed deployment metadata' >&2
    exit 68
  fi
  "$docker_bin" image inspect "$expected_current_image" >/dev/null
fi
python3.12 - "$archive" "$stage" <<'PY'
import pathlib
import sys
import zipfile
archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(archive) as bundle:
    root = target.resolve()
    for member in bundle.infolist():
        destination = (target / member.filename).resolve()
        if root != destination and root not in destination.parents:
            raise RuntimeError(f"unsafe archive member: {member.filename}")
    bundle.extractall(target)
PY
printf '%s\n' 'neuralcloud-launcher-v1' > "$stage/.neuralcloud-launcher-deployment"
printf '%s\n' "$fingerprint" > "$stage/.neuralcloud-launcher-fingerprint"
cd -- "$stage"
env UV_CACHE_DIR=/var/cache/mistyai-uv "$uv_bin" sync --frozen --extra desktop
override="$stage/.neuralcloud-launcher-compose.override.yaml"
printf 'services:\n  desktop:\n    image: %s\n' "$candidate_tag" > "$override"
"$docker_bin" compose -f "$stage/deploy/compose.yaml" -f "$override" build --pull desktop
rm -f -- "$override"
candidate_image=$("$docker_bin" image inspect --format '{{.Id}}' "$candidate_tag" 2>/dev/null || true)
if ! printf '%s' "$candidate_image" | grep -Eq '^sha256:[0-9a-fA-F]{64}$'; then
  echo 'candidate image identity is unavailable after build' >&2
  exit 69
fi
candidate_ready=yes
printf '%s\n' "$candidate_image" > "$stage/.neuralcloud-launcher-image-id"
printf '%s\n' "$old_image" > "$stage/.neuralcloud-launcher-previous-image"
trap - EXIT
printf 'previous_image=%s\n' "$old_image"
printf 'candidate_image=%s\n' "$candidate_image"
'@
    $prepareScript = $prepareScript.Replace('__ARCHIVE__', (Quote-LauncherBashLiteral $archiveWsl))
    $prepareScript = $prepareScript.Replace('__ARCHIVE_SHA256__', (Quote-LauncherBashLiteral $snapshot.archive_sha256))
    $prepareScript = $prepareScript.Replace('__STAGE__', (Quote-LauncherBashLiteral $stage))
    $prepareScript = $prepareScript.Replace('__UV_BIN__', (Quote-LauncherBashLiteral $uvPath))
    $prepareScript = $prepareScript.Replace('__FINGERPRINT__', (Quote-LauncherBashLiteral $snapshot.fingerprint))
    $prepareScript = $prepareScript.Replace('__CANDIDATE_TAG__', (Quote-LauncherBashLiteral $candidateTag))
    $prepareScript = $prepareScript.Replace('__EXPECTED_CURRENT_IMAGE__', (Quote-LauncherBashLiteral $expectedCurrentImage))
    $prepare = Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -Script $prepareScript -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI staged dependency sync and image build'
    $oldImageMatch = [regex]::Match($prepare.stdout, '(?m)^previous_image=(sha256:[0-9a-fA-F]{64})\s*$')
    $previousImageId = if ($oldImageMatch.Success) { $oldImageMatch.Groups[1].Value } else { '' }
    $candidateImageMatch = [regex]::Match($prepare.stdout, '(?m)^candidate_image=(sha256:[0-9a-fA-F]{64})\s*$')
    if (-not $candidateImageMatch.Success) {
        Throw-LauncherFailure -Message 'MistyAI candidate image build did not return a valid image identity.' -ExitCode $script:ExitMistyAI
    }
    $candidateImageId = $candidateImageMatch.Groups[1].Value

    $retired = "/opt/mistyai.retired-$transactionId"
    $failedNew = "/opt/mistyai.failed-swap-$transactionId"
    $hadManagedCurrent = if ($ownership.current -eq 'managed') { 'yes' } else { 'no' }
    $hadManagedPrevious = if ($ownership.previous -eq 'managed') { 'yes' } else { 'no' }
    $swapScript = (Get-MistyAIUnitIdentityBash) + "`n" + @'
set -euo pipefail
stage=__STAGE__
retired=__RETIRED__
failed_new=__FAILED_NEW__
uv_bin=__UV_BIN__
fingerprint=__FINGERPRINT__
candidate_tag=__CANDIDATE_TAG__
candidate_image=__CANDIDATE_IMAGE__
old_image=__OLD_IMAGE__
had_current=__HAD_CURRENT__
expected_current_fingerprint=__EXPECTED_CURRENT_FINGERPRINT__
expected_current_image=__EXPECTED_CURRENT_IMAGE__
had_previous=__HAD_PREVIOUS__
expected_previous_fingerprint=__EXPECTED_PREVIOUS_FINGERPRINT__
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in ''|/mnt/[a-zA-Z]/*) echo 'native WSL Docker integration is unavailable' >&2; exit 67 ;; esac
case "$stage" in /opt/mistyai.stage-*) ;; *) echo 'unsafe deployment stage' >&2; exit 64 ;; esac
case "$retired" in /opt/mistyai.retired-*) ;; *) echo 'unsafe retired deployment path' >&2; exit 64 ;; esac
case "$failed_new" in /opt/mistyai.failed-swap-*) ;; *) echo 'unsafe failed deployment path' >&2; exit 64 ;; esac
previous=no
retired_moved=no
old_moved=no
new_installed=no
tag_changed=no
body_was_active=no
operator_was_active=no
restore_old() {
  status=$?
  trap - EXIT
  if test "$status" -eq 0; then exit 0; fi
  recovery_failed=no
  if test "$new_installed" = yes; then
    if test -d /opt/mistyai && ! test -L /opt/mistyai && \
       grep -qx 'neuralcloud-launcher-v1' /opt/mistyai/.neuralcloud-launcher-deployment 2>/dev/null && \
       grep -qx "$fingerprint" /opt/mistyai/.neuralcloud-launcher-fingerprint 2>/dev/null && \
       grep -qx "$candidate_image" /opt/mistyai/.neuralcloud-launcher-image-id 2>/dev/null; then
      mv -- /opt/mistyai "$failed_new" || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$old_moved" = yes; then
    if ! test -e /opt/mistyai && test -d /opt/mistyai.previous && ! test -L /opt/mistyai.previous && \
       grep -qx "$expected_current_fingerprint" /opt/mistyai.previous/.neuralcloud-launcher-fingerprint 2>/dev/null; then
      mv -- /opt/mistyai.previous /opt/mistyai || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$retired_moved" = yes; then
    if ! test -e /opt/mistyai.previous && test -d "$retired" && ! test -L "$retired" && \
       grep -qx "$expected_previous_fingerprint" "$retired/.neuralcloud-launcher-fingerprint" 2>/dev/null; then
      mv -- "$retired" /opt/mistyai.previous || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$tag_changed" = yes; then
    if test -n "$old_image"; then
      "$docker_bin" image inspect "$old_image" >/dev/null 2>&1 && \
        "$docker_bin" image tag "$old_image" mistyai-desktop:m0 >/dev/null 2>&1 || recovery_failed=yes
    else
      current_tag=$("$docker_bin" image inspect --format '{{.Id}}' mistyai-desktop:m0 2>/dev/null || true)
      if test "$current_tag" = "$candidate_image"; then
        "$docker_bin" image rm mistyai-desktop:m0 >/dev/null 2>&1 || recovery_failed=yes
      elif test -n "$current_tag"; then
        recovery_failed=yes
      fi
    fi
  fi
  if test "$body_was_active" = yes; then
    systemctl start mistyai-body.service >/dev/null 2>&1 || recovery_failed=yes
    systemctl is-active --quiet mistyai-body.service || recovery_failed=yes
  fi
  if test "$operator_was_active" = yes; then
    systemctl start mistyai-operator.service >/dev/null 2>&1 || recovery_failed=yes
    systemctl is-active --quiet mistyai-operator.service || recovery_failed=yes
  fi
  if test "$recovery_failed" = yes; then
    echo 'deployment swap failed and automatic recovery was incomplete' >&2
    exit 70
  fi
  if test -d "$failed_new" && ! test -L "$failed_new" && \
     grep -qx "$fingerprint" "$failed_new/.neuralcloud-launcher-fingerprint" 2>/dev/null; then
    rm -rf -- "$failed_new" || { echo 'recovered layout but could not remove failed candidate' >&2; exit 70; }
  fi
  if test -d "$stage" && ! test -L "$stage" && \
     grep -qx "$fingerprint" "$stage/.neuralcloud-launcher-fingerprint" 2>/dev/null; then
    rm -rf -- "$stage" || { echo 'recovered layout but could not remove deployment stage' >&2; exit 70; }
  fi
  "$docker_bin" image rm "$candidate_tag" >/dev/null 2>&1 || true
  exit "$status"
}
trap restore_old EXIT
test -d "$stage"
test ! -L "$stage"
grep -qx 'neuralcloud-launcher-v1' "$stage/.neuralcloud-launcher-deployment"
grep -qx "$fingerprint" "$stage/.neuralcloud-launcher-fingerprint"
grep -qx "$candidate_image" "$stage/.neuralcloud-launcher-image-id"
actual_candidate=$("$docker_bin" image inspect --format '{{.Id}}' "$candidate_tag" 2>/dev/null || true)
test "$actual_candidate" = "$candidate_image"
canonical_image=$("$docker_bin" image inspect --format '{{.Id}}' mistyai-desktop:m0 2>/dev/null || true)
test "$canonical_image" = "$old_image"
if test -e "$retired" || test -L "$retired" || test -e "$failed_new" || test -L "$failed_new"; then
  echo 'refusing to reuse an existing transaction path' >&2
  exit 65
fi
if test "$had_current" = yes; then
  test -d /opt/mistyai
  test ! -L /opt/mistyai
  grep -qx 'neuralcloud-launcher-v1' /opt/mistyai/.neuralcloud-launcher-deployment
  grep -qx "$expected_current_fingerprint" /opt/mistyai/.neuralcloud-launcher-fingerprint
  grep -qx "$expected_current_image" /opt/mistyai/.neuralcloud-launcher-image-id
else
  if test -e /opt/mistyai || test -L /opt/mistyai; then
    echo 'a deployment appeared while the candidate was being built' >&2
    exit 65
  fi
fi
if test "$had_previous" = yes; then
  test -d /opt/mistyai.previous
  test ! -L /opt/mistyai.previous
  grep -qx 'neuralcloud-launcher-v1' /opt/mistyai.previous/.neuralcloud-launcher-deployment
  grep -qx "$expected_previous_fingerprint" /opt/mistyai.previous/.neuralcloud-launcher-fingerprint
else
  if test -e /opt/mistyai.previous || test -L /opt/mistyai.previous; then
    echo 'a previous deployment appeared while the candidate was being built' >&2
    exit 65
  fi
fi
for unit in mistyai-body.service mistyai-operator.service; do
  if ! verify_mistyai_unit_owned_or_absent "$unit"; then
    echo "refusing deployment swap because $unit has an unknown identity" >&2
    exit 65
  fi
done
systemctl is-active --quiet mistyai-body.service && body_was_active=yes || true
systemctl is-active --quiet mistyai-operator.service && operator_was_active=yes || true
if test "$had_current" = no && { test "$body_was_active" = yes || test "$operator_was_active" = yes; }; then
  echo 'MistyAI units are active without the expected managed deployment' >&2
  exit 65
fi
if test "$body_was_active" = yes; then
  verify_mistyai_unit_identity mistyai-body.service
fi
if test "$operator_was_active" = yes; then
  verify_mistyai_unit_identity mistyai-operator.service
fi
if test "$operator_was_active" = yes; then
  systemctl stop mistyai-operator.service
  if systemctl is-active --quiet mistyai-operator.service; then echo 'mistyai-operator.service did not stop' >&2; exit 71; fi
fi
if test "$body_was_active" = yes; then
  systemctl stop mistyai-body.service
  if systemctl is-active --quiet mistyai-body.service; then echo 'mistyai-body.service did not stop' >&2; exit 71; fi
fi
"$docker_bin" image tag "$candidate_image" mistyai-desktop:m0
tag_changed=yes
if test "$had_previous" = yes; then
  mv -- /opt/mistyai.previous "$retired"
  retired_moved=yes
fi
if test "$had_current" = yes; then
  mv -- /opt/mistyai /opt/mistyai.previous
  previous=yes
  old_moved=yes
fi
mv -- "$stage" /opt/mistyai
new_installed=yes
rm -rf -- /opt/mistyai/.venv
cd -- /opt/mistyai
env UV_CACHE_DIR=/var/cache/mistyai-uv "$uv_bin" sync --frozen --extra desktop
test -x /opt/mistyai/.venv/bin/mistyctl
if test "$retired_moved" = yes; then
  grep -qx 'neuralcloud-launcher-v1' "$retired/.neuralcloud-launcher-deployment"
  grep -qx "$expected_previous_fingerprint" "$retired/.neuralcloud-launcher-fingerprint"
  rm -rf -- "$retired"
fi
"$docker_bin" image rm "$candidate_tag" >/dev/null 2>&1 || true
trap - EXIT
printf 'previous=%s\n' "$previous"
'@
    $swapScript = $swapScript.Replace('__STAGE__', (Quote-LauncherBashLiteral $stage))
    $swapScript = $swapScript.Replace('__RETIRED__', (Quote-LauncherBashLiteral $retired))
    $swapScript = $swapScript.Replace('__FAILED_NEW__', (Quote-LauncherBashLiteral $failedNew))
    $swapScript = $swapScript.Replace('__UV_BIN__', (Quote-LauncherBashLiteral $uvPath))
    $swapScript = $swapScript.Replace('__FINGERPRINT__', (Quote-LauncherBashLiteral $snapshot.fingerprint))
    $swapScript = $swapScript.Replace('__CANDIDATE_TAG__', (Quote-LauncherBashLiteral $candidateTag))
    $swapScript = $swapScript.Replace('__CANDIDATE_IMAGE__', (Quote-LauncherBashLiteral $candidateImageId))
    $swapScript = $swapScript.Replace('__OLD_IMAGE__', (Quote-LauncherBashLiteral $previousImageId))
    $swapScript = $swapScript.Replace('__HAD_CURRENT__', (Quote-LauncherBashLiteral $hadManagedCurrent))
    $swapScript = $swapScript.Replace('__EXPECTED_CURRENT_FINGERPRINT__', (Quote-LauncherBashLiteral $ownership.current_fingerprint))
    $swapScript = $swapScript.Replace('__EXPECTED_CURRENT_IMAGE__', (Quote-LauncherBashLiteral $ownership.current_image_id))
    $swapScript = $swapScript.Replace('__HAD_PREVIOUS__', (Quote-LauncherBashLiteral $hadManagedPrevious))
    $swapScript = $swapScript.Replace('__EXPECTED_PREVIOUS_FINGERPRINT__', (Quote-LauncherBashLiteral $ownership.previous_fingerprint))
    $swap = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $swapScript -Description 'activate the staged MistyAI deployment atomically' -CommandInvoker $CommandInvoker
    if ($swap.exit_code -ne 0) {
        $detail = Protect-LauncherText -Text (($swap.stderr + "`n" + $swap.stdout).Trim())
        if ($swap.exit_code -eq 70) {
            Throw-LauncherFailure -Message "MistyAI deployment requires manual recovery: $detail" -ExitCode $script:ExitMistyAI
        }
        Throw-LauncherFailure -Message "MistyAI atomic deployment swap failed: $detail" -ExitCode $script:ExitMistyAI
    }
    $previousAvailable = $swap.stdout -match '(?m)^previous=yes\s*$'
    return [pscustomobject][ordered]@{
        changed = $true
        fingerprint = $snapshot.fingerprint
        previous_available = $previousAvailable
        previous_image_id = $previousImageId
    }
}

function Restore-MistyAIDeployment {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker,
        [switch]$RestartBody,
        [switch]$RestartOperator,
        [switch]$RestartCompose,
        [AllowEmptyString()][string]$PreviousImageId = ''
    )
    if ($PreviousImageId -and $PreviousImageId -notmatch '^sha256:[0-9a-fA-F]{64}$') {
        return $false
    }
    $failed = "/opt/mistyai.failed-$([guid]::NewGuid().ToString('N'))"
    $rollbackOld = "/opt/mistyai.rollback-old-$([guid]::NewGuid().ToString('N'))"
    $restartBodyValue = if ($RestartBody) { 'yes' } else { 'no' }
    $restartOperatorValue = if ($RestartOperator) { 'yes' } else { 'no' }
    $restartComposeValue = if ($RestartCompose) { 'yes' } else { 'no' }
    $restoreScript = (Get-MistyAIUnitIdentityBash) + "`n" + @'
set -euo pipefail
failed=__FAILED__
rollback_old=__ROLLBACK_OLD__
old_image=__OLD_IMAGE__
restart_body=__RESTART_BODY__
restart_operator=__RESTART_OPERATOR__
restart_compose=__RESTART_COMPOSE__
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in ''|/mnt/[a-zA-Z]/*) echo 'native WSL Docker integration is unavailable' >&2; exit 67 ;; esac
case "$failed" in /opt/mistyai.failed-*) ;; *) echo 'unsafe failed deployment path' >&2; exit 64 ;; esac
case "$rollback_old" in /opt/mistyai.rollback-old-*) ;; *) echo 'unsafe rollback path' >&2; exit 64 ;; esac
if test -L /opt/mistyai || test -L /opt/mistyai.previous; then
  echo 'refusing to restore a symbolic-link deployment' >&2
  exit 65
fi
test -d /opt/mistyai.previous
grep -qx 'neuralcloud-launcher-v1' /opt/mistyai.previous/.neuralcloud-launcher-deployment
test -d /opt/mistyai
grep -qx 'neuralcloud-launcher-v1' /opt/mistyai/.neuralcloud-launcher-deployment
if test -e "$failed" || test -L "$failed" || test -e "$rollback_old" || test -L "$rollback_old"; then
  echo 'refusing to reuse an existing rollback path' >&2
  exit 65
fi
previous_fingerprint=$(sed -n '1p' /opt/mistyai.previous/.neuralcloud-launcher-fingerprint)
previous_metadata_image=$(sed -n '1p' /opt/mistyai.previous/.neuralcloud-launcher-image-id)
new_fingerprint=$(sed -n '1p' /opt/mistyai/.neuralcloud-launcher-fingerprint)
new_image=$(sed -n '1p' /opt/mistyai/.neuralcloud-launcher-image-id)
printf '%s' "$previous_fingerprint" | grep -Eq '^[0-9a-fA-F]{64}$'
printf '%s' "$new_fingerprint" | grep -Eq '^[0-9a-fA-F]{64}$'
printf '%s' "$previous_metadata_image" | grep -Eq '^sha256:[0-9a-fA-F]{64}$'
printf '%s' "$new_image" | grep -Eq '^sha256:[0-9a-fA-F]{64}$'
if test -n "$old_image" && test "$old_image" != "$previous_metadata_image"; then
  echo 'rollback image identity does not match previous deployment metadata' >&2
  exit 68
fi
old_image="$previous_metadata_image"
canonical_image=$("$docker_bin" image inspect --format '{{.Id}}' mistyai-desktop:m0 2>/dev/null || true)
if test "$canonical_image" != "$new_image"; then
  echo 'canonical image changed before rollback' >&2
  exit 68
fi
new_moved=no
old_restored=no
tag_changed=no
body_was_active=no
operator_was_active=no
for unit in mistyai-body.service mistyai-operator.service; do
  if ! verify_mistyai_unit_owned_or_absent "$unit"; then
    echo "refusing rollback because $unit has an unknown identity" >&2
    exit 65
  fi
done
systemctl is-active --quiet mistyai-body.service && body_was_active=yes || true
systemctl is-active --quiet mistyai-operator.service && operator_was_active=yes || true
if test "$body_was_active" = yes; then
  verify_mistyai_owned_unit_identity mistyai-body.service
fi
if test "$operator_was_active" = yes; then
  verify_mistyai_owned_unit_identity mistyai-operator.service
fi
restore_failed_layout() {
  status=$?
  trap - EXIT
  if test "$status" -eq 0; then exit 0; fi
  recovery_failed=no
  if test "$old_restored" = yes; then
    if test -d /opt/mistyai && ! test -L /opt/mistyai && \
       grep -qx "$previous_fingerprint" /opt/mistyai/.neuralcloud-launcher-fingerprint 2>/dev/null; then
      mv -- /opt/mistyai "$rollback_old" || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$new_moved" = yes; then
    if ! test -e /opt/mistyai && test -d "$failed" && ! test -L "$failed" && \
       grep -qx "$new_fingerprint" "$failed/.neuralcloud-launcher-fingerprint" 2>/dev/null; then
      mv -- "$failed" /opt/mistyai || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$old_restored" = yes; then
    if ! test -e /opt/mistyai.previous && test -d "$rollback_old" && ! test -L "$rollback_old" && \
       grep -qx "$previous_fingerprint" "$rollback_old/.neuralcloud-launcher-fingerprint" 2>/dev/null; then
      mv -- "$rollback_old" /opt/mistyai.previous || recovery_failed=yes
    else
      recovery_failed=yes
    fi
  fi
  if test "$tag_changed" = yes; then
    "$docker_bin" image inspect "$new_image" >/dev/null 2>&1 && \
      "$docker_bin" image tag "$new_image" mistyai-desktop:m0 >/dev/null 2>&1 || recovery_failed=yes
  fi
  units_safe=yes
  for unit in mistyai-body.service mistyai-operator.service; do
    if ! verify_mistyai_unit_owned_or_absent "$unit"; then
      units_safe=no
      recovery_failed=yes
    fi
  done
  if test "$units_safe" = yes && test -d /opt/mistyai/deploy/systemd; then
    install -m 0644 /opt/mistyai/deploy/systemd/mistyai-body.service /etc/systemd/system/mistyai-body.service >/dev/null 2>&1 || recovery_failed=yes
    install -m 0644 /opt/mistyai/deploy/systemd/mistyai-operator.service /etc/systemd/system/mistyai-operator.service >/dev/null 2>&1 || recovery_failed=yes
    systemctl daemon-reload >/dev/null 2>&1 || recovery_failed=yes
    verify_mistyai_unit_identity mistyai-body.service >/dev/null 2>&1 || recovery_failed=yes
    verify_mistyai_unit_identity mistyai-operator.service >/dev/null 2>&1 || recovery_failed=yes
  else
    recovery_failed=yes
  fi
  if test "$restart_compose" = yes; then
    if test -x /opt/mistyai/.venv/bin/mistyctl; then
      (cd /opt/mistyai && .venv/bin/mistyctl env up --json) >/dev/null 2>&1 || recovery_failed=yes
      for service in desktop agent loopback-gateway; do
        container_id=$("$docker_bin" compose -f /opt/mistyai/deploy/compose.yaml ps -q "$service" 2>/dev/null | head -n 1)
        test -n "$container_id" || recovery_failed=yes
        if test -n "$container_id"; then
          test "$("$docker_bin" inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" = true || recovery_failed=yes
          test "$("$docker_bin" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id" 2>/dev/null || true)" = healthy || recovery_failed=yes
        fi
      done
    else
      recovery_failed=yes
    fi
  fi
  if test "$body_was_active" = yes; then
    systemctl start mistyai-body.service >/dev/null 2>&1 || recovery_failed=yes
    systemctl is-active --quiet mistyai-body.service || recovery_failed=yes
  fi
  if test "$operator_was_active" = yes; then
    systemctl start mistyai-operator.service >/dev/null 2>&1 || recovery_failed=yes
    systemctl is-active --quiet mistyai-operator.service || recovery_failed=yes
  fi
  if test "$recovery_failed" = yes; then
    echo 'rollback failed and the candidate deployment could not be fully restored' >&2
    exit 70
  fi
  exit "$status"
}
trap restore_failed_layout EXIT
if test "$operator_was_active" = yes; then
  systemctl stop mistyai-operator.service
  if systemctl is-active --quiet mistyai-operator.service; then echo 'mistyai-operator.service did not stop for rollback' >&2; exit 71; fi
fi
if test "$body_was_active" = yes; then
  systemctl stop mistyai-body.service
  if systemctl is-active --quiet mistyai-body.service; then echo 'mistyai-body.service did not stop for rollback' >&2; exit 71; fi
fi
mv -- /opt/mistyai "$failed"
new_moved=yes
mv -- /opt/mistyai.previous /opt/mistyai
old_restored=yes
for unit in mistyai-body.service mistyai-operator.service; do
  unit_file="/etc/systemd/system/$unit"
  if test -e "$unit_file" || test -L "$unit_file"; then
    if ! verify_mistyai_unit_identity "$unit" /opt/mistyai && \
       ! verify_mistyai_unit_identity "$unit" "$failed"; then
      echo "refusing to overwrite an unexpected $unit during rollback" >&2
      exit 65
    fi
  elif ! verify_mistyai_unit_absent "$unit"; then
    echo "cannot prove $unit is absent during rollback" >&2
    exit 65
  fi
done
install -m 0644 /opt/mistyai/deploy/systemd/mistyai-body.service /etc/systemd/system/mistyai-body.service
install -m 0644 /opt/mistyai/deploy/systemd/mistyai-operator.service /etc/systemd/system/mistyai-operator.service
systemctl daemon-reload
verify_mistyai_unit_identity mistyai-body.service
verify_mistyai_unit_identity mistyai-operator.service
cd -- /opt/mistyai
tag_changed=yes
if "$docker_bin" image inspect "$old_image" >/dev/null 2>&1; then
  "$docker_bin" image tag "$old_image" mistyai-desktop:m0
else
  .venv/bin/mistyctl image build --json
fi
restored_image=$("$docker_bin" image inspect --format '{{.Id}}' mistyai-desktop:m0 2>/dev/null || true)
if ! printf '%s' "$restored_image" | grep -Eq '^sha256:[0-9a-fA-F]{64}$'; then
  echo 'rollback image identity is unavailable' >&2
  exit 68
fi
if "$docker_bin" image inspect "$old_image" >/dev/null 2>&1 && test "$restored_image" != "$old_image"; then
  echo 'rollback did not restore the expected image' >&2
  exit 68
fi
printf '%s\n' "$restored_image" > /opt/mistyai/.neuralcloud-launcher-image-id
if test "$restart_compose" = yes; then
  .venv/bin/mistyctl env up --json
  for service in desktop agent loopback-gateway; do
    container_id=$("$docker_bin" compose -f /opt/mistyai/deploy/compose.yaml ps -q "$service" | head -n 1)
    test -n "$container_id"
    test "$("$docker_bin" inspect --format '{{.State.Running}}' "$container_id")" = true
    test "$("$docker_bin" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")" = healthy
  done
fi
if test "$restart_body" = yes; then systemctl start mistyai-body.service; fi
if test "$restart_operator" = yes; then systemctl start mistyai-operator.service; fi
if test "$restart_body" = yes; then systemctl is-active --quiet mistyai-body.service; fi
if test "$restart_operator" = yes; then systemctl is-active --quiet mistyai-operator.service; fi
grep -qx 'neuralcloud-launcher-v1' "$failed/.neuralcloud-launcher-deployment"
grep -qx "$new_fingerprint" "$failed/.neuralcloud-launcher-fingerprint"
rm -rf -- "$failed"
trap - EXIT
'@
    $restoreScript = $restoreScript.Replace('__FAILED__', (Quote-LauncherBashLiteral $failed))
    $restoreScript = $restoreScript.Replace('__ROLLBACK_OLD__', (Quote-LauncherBashLiteral $rollbackOld))
    $restoreScript = $restoreScript.Replace('__OLD_IMAGE__', (Quote-LauncherBashLiteral $PreviousImageId))
    $restoreScript = $restoreScript.Replace('__RESTART_BODY__', (Quote-LauncherBashLiteral $restartBodyValue))
    $restoreScript = $restoreScript.Replace('__RESTART_OPERATOR__', (Quote-LauncherBashLiteral $restartOperatorValue))
    $restoreScript = $restoreScript.Replace('__RESTART_COMPOSE__', (Quote-LauncherBashLiteral $restartComposeValue))
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $restoreScript -Description 'roll back the MistyAI deployment and restore service state' -CommandInvoker $CommandInvoker
    return $result.exit_code -eq 0
}

function Initialize-MistyAIConfiguration {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][bool]$DeploymentChanged,
        [scriptblock]$CommandInvoker,
        [switch]$StartEnvironment,
        [switch]$PreserveBody,
        [switch]$PreserveOperator,
        [switch]$PreserveCompose
    )
    $pathSafetyScript = @'
set -euo pipefail
for path in \
  /etc/mistyai \
  /etc/mistyai/m0.yaml \
  /etc/mistyai/body-service.env \
  /etc/mistyai/body-service.token \
  /etc/mistyai/instances \
  /etc/mistyai/instances/misty-body-01 \
  /etc/mistyai/instances/misty-body-01/agent.token \
  /etc/mistyai/instances/misty-body-01/novnc.password \
  /var/lib/mistyai \
  /var/lib/mistyai/state \
  /var/lib/mistyai/evidence \
  /run/mistyai; do
  if test -L "$path"; then
    echo "refusing symbolic-link configuration or data path: $path" >&2
    exit 66
  fi
done
'@
    [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -Script $pathSafetyScript -CommandInvoker $CommandInvoker -ExitCode $script:ExitConfiguration -Description 'verify MistyAI configuration and data paths are not symbolic links')
    [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -WorkingDirectory '/opt/mistyai' -Script '.venv/bin/mistyctl config init --json' -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI configuration initialization')

    $unitProbeScript = (Get-MistyAIUnitIdentityBash) + "`n" + @'
set +e
probe_unit() {
  unit="$1"
  if systemctl cat "$unit" >/dev/null 2>&1; then
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    case "$enabled" in enabled|disabled) ;; *) enabled=unsupported ;; esac
    if verify_mistyai_unit_identity "$unit"; then
      printf '%s=valid\n' "$unit"
    elif grep -qx 'neuralcloud-launcher-v1' /opt/mistyai.previous/.neuralcloud-launcher-deployment 2>/dev/null && \
         verify_mistyai_unit_identity "$unit" /opt/mistyai.previous; then
      printf '%s=stale\n' "$unit"
    else
      printf '%s=mismatch\n' "$unit"
    fi
  elif verify_mistyai_unit_absent "$unit"; then
    printf '%s=missing\n' "$unit"
    enabled=missing
  else
    printf '%s=mismatch\n' "$unit"
    enabled=unsupported
  fi
  printf '%s-enabled=%s\n' "$unit" "$enabled"
}
probe_unit mistyai-body.service
probe_unit mistyai-operator.service
if test -f /etc/mistyai/body-service.env && ! test -L /etc/mistyai/body-service.env && \
   test "$(stat -c '%a:%U:%G' /etc/mistyai/body-service.env 2>/dev/null)" = '640:root:mistyai-svc' && \
   test -f /etc/mistyai/body-service.token && ! test -L /etc/mistyai/body-service.token && \
   test "$(stat -c '%a:%U:%G' /etc/mistyai/body-service.token 2>/dev/null)" = '640:root:misty-operator' && \
   test -f /etc/mistyai/instances/misty-body-01/agent.token && ! test -L /etc/mistyai/instances/misty-body-01/agent.token && \
   test "$(stat -c '%a:%u:%g' /etc/mistyai/instances/misty-body-01/agent.token 2>/dev/null)" = '400:2002:0' && \
   test -f /etc/mistyai/instances/misty-body-01/novnc.password && ! test -L /etc/mistyai/instances/misty-body-01/novnc.password && \
   test "$(stat -c '%a:%u:%G' /etc/mistyai/instances/misty-body-01/novnc.password 2>/dev/null)" = '440:2001:misty-operator' && \
   test -d /etc/mistyai/instances/misty-body-01 && ! test -L /etc/mistyai/instances/misty-body-01 && \
   test "$(stat -c '%a:%u:%G' /etc/mistyai/instances/misty-body-01 2>/dev/null)" = '2770:0:misty-operator'; then
  printf 'secrets=valid\n'
else
  printf 'secrets=repair\n'
fi
'@
    $unitProbe = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $unitProbeScript -Description 'check MistyAI units and token permissions' -CommandInvoker $CommandInvoker
    if ($unitProbe.exit_code -ne 0) {
        Throw-LauncherFailure -Message 'MistyAI systemd unit identity could not be inspected.' -ExitCode $script:ExitMistyAI
    }
    $bodyUnitMatch = [regex]::Match($unitProbe.stdout, '(?m)^mistyai-body\.service=(valid|stale|missing|mismatch)\s*$')
    $operatorUnitMatch = [regex]::Match($unitProbe.stdout, '(?m)^mistyai-operator\.service=(valid|stale|missing|mismatch)\s*$')
    $bodyEnabledMatch = [regex]::Match($unitProbe.stdout, '(?m)^mistyai-body\.service-enabled=(enabled|disabled|missing|unsupported)\s*$')
    $operatorEnabledMatch = [regex]::Match($unitProbe.stdout, '(?m)^mistyai-operator\.service-enabled=(enabled|disabled|missing|unsupported)\s*$')
    $secretsMatch = [regex]::Match($unitProbe.stdout, '(?m)^secrets=(valid|repair)\s*$')
    if (-not $bodyUnitMatch.Success -or -not $operatorUnitMatch.Success -or
        -not $bodyEnabledMatch.Success -or -not $operatorEnabledMatch.Success -or -not $secretsMatch.Success) {
        Throw-LauncherFailure -Message 'MistyAI systemd unit identity probe returned incomplete data.' -ExitCode $script:ExitMistyAI
    }
    if ($bodyUnitMatch.Groups[1].Value -eq 'mismatch' -or $operatorUnitMatch.Groups[1].Value -eq 'mismatch' -or
        $bodyEnabledMatch.Groups[1].Value -eq 'unsupported' -or $operatorEnabledMatch.Groups[1].Value -eq 'unsupported') {
        Throw-LauncherFailure -Message 'A same-named MistyAI systemd unit has an unexpected file, ExecStart, or enablement state; refusing to overwrite or start it.' -ExitCode $script:ExitConfiguration
    }
    $bodyWasEnabled = $bodyEnabledMatch.Groups[1].Value -eq 'enabled'
    $operatorWasEnabled = $operatorEnabledMatch.Groups[1].Value -eq 'enabled'
    $serviceInstallNeeded = ($bodyUnitMatch.Groups[1].Value -in @('missing', 'stale')) -or
        ($operatorUnitMatch.Groups[1].Value -in @('missing', 'stale')) -or
        $secretsMatch.Groups[1].Value -eq 'repair'
    $serviceInstallWillRun = $DeploymentChanged -or $serviceInstallNeeded
    $configurationSucceeded = $false
    try {
        $shouldStartEnvironment = if ($StartEnvironment) {
            -not $PreserveCompose -or $DeploymentChanged
        } else {
            $PreserveCompose -and $DeploymentChanged
        }
        if ($shouldStartEnvironment) {
            [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -WorkingDirectory '/opt/mistyai' -Script '.venv/bin/mistyctl env up --json' -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI container environment startup')
        }
        if ($serviceInstallWillRun) {
            [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -WorkingDirectory '/opt/mistyai' -Script '.venv/bin/mistyctl service install --json' -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI systemd service installation')
        } elseif ($StartEnvironment) {
            $unitsToStart = New-Object System.Collections.Generic.List[string]
            if (-not $PreserveBody) { [void]$unitsToStart.Add('mistyai-body.service') }
            if (-not $PreserveOperator) { [void]$unitsToStart.Add('mistyai-operator.service') }
            if ($unitsToStart.Count -gt 0) {
                $unitStartCommands = New-Object System.Collections.Generic.List[string]
                [void]$unitStartCommands.Add('set -euo pipefail')
                [void]$unitStartCommands.Add((Get-MistyAIUnitIdentityBash))
                foreach ($unit in $unitsToStart) {
                    [void]$unitStartCommands.Add("verify_mistyai_unit_identity $unit")
                }
                [void]$unitStartCommands.Add("systemctl start $($unitsToStart -join ' ')")
                foreach ($unit in $unitsToStart) {
                    [void]$unitStartCommands.Add("systemctl is-active --quiet $unit")
                }
                [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -Script ($unitStartCommands -join "`n") -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI inactive systemd service startup')
            }
        }
        $configurationSucceeded = $true
    } finally {
        $restoreOriginalServiceState = -not $configurationSucceeded -or (-not $StartEnvironment -and $serviceInstallWillRun)
        if ($restoreOriginalServiceState -or $serviceInstallWillRun) {
            $stateCommands = New-Object System.Collections.Generic.List[string]
            [void]$stateCommands.Add('set -euo pipefail')
            [void]$stateCommands.Add((Get-MistyAIUnitIdentityBash))
            if ($restoreOriginalServiceState) {
                if (-not $PreserveOperator) {
                    [void]$stateCommands.Add("if systemctl cat mistyai-operator.service >/dev/null 2>&1; then verify_mistyai_unit_identity mistyai-operator.service; systemctl stop mistyai-operator.service; if systemctl is-active --quiet mistyai-operator.service; then exit 71; fi; fi")
                }
                if (-not $PreserveBody) {
                    [void]$stateCommands.Add("if systemctl cat mistyai-body.service >/dev/null 2>&1; then verify_mistyai_unit_identity mistyai-body.service; systemctl stop mistyai-body.service; if systemctl is-active --quiet mistyai-body.service; then exit 71; fi; fi")
                }
                if ($PreserveBody) {
                    [void]$stateCommands.Add("verify_mistyai_unit_identity mistyai-body.service; systemctl start mistyai-body.service; systemctl is-active --quiet mistyai-body.service")
                }
                if ($PreserveOperator) {
                    [void]$stateCommands.Add("verify_mistyai_unit_identity mistyai-operator.service; systemctl start mistyai-operator.service; systemctl is-active --quiet mistyai-operator.service")
                }
            }
            if ($serviceInstallWillRun) {
                if ($bodyWasEnabled) {
                    [void]$stateCommands.Add("verify_mistyai_unit_identity mistyai-body.service; systemctl enable mistyai-body.service; systemctl is-enabled --quiet mistyai-body.service")
                } else {
                    [void]$stateCommands.Add("if systemctl cat mistyai-body.service >/dev/null 2>&1; then verify_mistyai_unit_identity mistyai-body.service; systemctl disable mistyai-body.service; fi; if systemctl is-enabled --quiet mistyai-body.service; then exit 72; fi")
                }
                if ($operatorWasEnabled) {
                    [void]$stateCommands.Add("verify_mistyai_unit_identity mistyai-operator.service; systemctl enable mistyai-operator.service; systemctl is-enabled --quiet mistyai-operator.service")
                } else {
                    [void]$stateCommands.Add("if systemctl cat mistyai-operator.service >/dev/null 2>&1; then verify_mistyai_unit_identity mistyai-operator.service; systemctl disable mistyai-operator.service; fi; if systemctl is-enabled --quiet mistyai-operator.service; then exit 72; fi")
                }
            }
            [void](Invoke-CheckedLauncherWslCommand -Distro $Distro -User 'root' -Script ($stateCommands -join "`n") -CommandInvoker $CommandInvoker -ExitCode $script:ExitMistyAI -Description 'MistyAI original service state restoration')
        }
    }
    if ($serviceInstallWillRun) {
        $postInstallProbe = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $unitProbeScript -Description 'verify installed MistyAI units and credential permissions' -CommandInvoker $CommandInvoker
        $expectedBodyEnablement = if ($bodyWasEnabled) { 'enabled' } else { 'disabled' }
        $expectedOperatorEnablement = if ($operatorWasEnabled) { 'enabled' } else { 'disabled' }
        $postInstallValid = $postInstallProbe.exit_code -eq 0 -and
            $postInstallProbe.stdout -match '(?m)^mistyai-body\.service=valid\s*$' -and
            $postInstallProbe.stdout -match '(?m)^mistyai-operator\.service=valid\s*$' -and
            $postInstallProbe.stdout -match '(?m)^secrets=valid\s*$' -and
            $postInstallProbe.stdout -match "(?m)^mistyai-body\.service-enabled=$expectedBodyEnablement\s*`$" -and
            $postInstallProbe.stdout -match "(?m)^mistyai-operator\.service-enabled=$expectedOperatorEnablement\s*`$"
        if (-not $postInstallValid) {
            Throw-LauncherFailure -Message 'MistyAI service installation did not leave the expected unit identity, enablement, and exact credential permissions.' -ExitCode $script:ExitMistyAI
        }
    }
    return [pscustomobject]@{ units_installed = $serviceInstallWillRun; enablement_restored = $true }
}

function Read-MistyAIServiceToken {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $readScript = @'
set -euo pipefail
token=/etc/mistyai/body-service.token
test -f "$token"
test ! -L "$token"
test "$(stat -c '%a:%U:%G' "$token")" = '640:root:misty-operator'
cat -- "$token"
'@
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $readScript -Description 'validate and read the MistyAI service token into launcher memory' -CommandInvoker $CommandInvoker
    if ($result.exit_code -ne 0) {
        Throw-LauncherFailure -Message 'MistyAI service token is missing, unsafe, or could not be read from WSL.' -ExitCode $script:ExitMistyAI
    }
    $token = $result.stdout.Trim()
    if ($token.Length -lt 16 -or $token -match '\s') {
        Throw-LauncherFailure -Message 'MistyAI service token is empty, too short, or malformed.' -ExitCode $script:ExitMistyAI
    }
    return $token
}

function Get-MistyAIRequiredCapabilities {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    $manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
    return @((Get-LauncherObjectProperty (Get-LauncherObjectProperty $manifest 'embodiment') 'capability_requirements' @()) |
        Where-Object { (Get-LauncherObjectProperty $_ 'required' $false) -eq $true } |
        ForEach-Object { [string](Get-LauncherObjectProperty $_ 'capability' '') })
}

function Get-MistyAIProvidedCapabilities {
    param(
        [Parameter(Mandatory = $true)][object]$Health,
        [Parameter(Mandatory = $true)][object]$Capabilities
    )
    $provided = New-Object System.Collections.Generic.List[string]
    $actionStatus = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $Health 'action') 'status' '')
    $observationStatus = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $Health 'observation') 'status' '')
    $environmentStatus = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $Health 'environment') 'status' '')
    $evidenceStatus = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $Health 'evidence_store') 'status' '')
    $bodyStateStatus = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $Health 'body_state') 'status' '')
    $agencyState = [string](Get-LauncherObjectProperty $Health 'agency_state' '')
    $manualTakeover = (Get-LauncherObjectProperty $Health 'manual_takeover_active' $false) -eq $true
    $restricted = $agencyState -ne 'AVAILABLE' -or $manualTakeover
    if ($actionStatus -eq 'healthy') {
        foreach ($name in @('action.verify.v1', 'action.safe-point.v1', 'action.reconcile.v1')) { [void]$provided.Add($name) }
        if (-not $restricted) { [void]$provided.Add('action.execute.v1') }
    }
    if (-not $manualTakeover -and $observationStatus -eq 'healthy' -and $environmentStatus -eq 'healthy') { [void]$provided.Add('observation.snapshot.v1') }
    if ($evidenceStatus -eq 'healthy') { [void]$provided.Add('evidence.reference.v1') }
    $extensions = Get-LauncherObjectProperty $Capabilities 'extensions'
    if ($bodyStateStatus -eq 'healthy' -and (Get-LauncherObjectProperty $extensions 'body_state_v1' $false) -eq $true) { [void]$provided.Add('physiology.body-state.v1') }
    if ($bodyStateStatus -eq 'healthy' -and (Get-LauncherObjectProperty $extensions 'virtual_resource_ledger' $false) -eq $true) { [void]$provided.Add('physiology.virtual-resource-ledger.v1') }
    return $provided.ToArray()
}

function Wait-MistyAIReady {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [scriptblock]$CommandInvoker,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $headers = @{ Authorization = "Bearer $Token" }
    $lastReason = 'not yet checked'
    do {
        $serviceState = Get-MistyAIServiceState -Distro $Distro -CommandInvoker $CommandInvoker
        if ($serviceState.running) {
            try {
                $health = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8787/v1/health' -Headers $headers -TimeoutSec 3
                $capabilities = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8787/v1/capabilities' -Headers $headers -TimeoutSec 3
                $expectedManifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
                $expectedBody = [string](Get-LauncherObjectProperty (Get-LauncherObjectProperty $expectedManifest 'embodiment') 'body_instance_id' '')
                $healthBody = [string](Get-LauncherObjectProperty $health 'body_instance_id' '')
                $capabilityBody = [string](Get-LauncherObjectProperty $capabilities 'body_instance_id' '')
                $contractVersions = @((Get-LauncherObjectProperty $capabilities 'contract_versions' @()))
                if ((Get-LauncherObjectProperty $health 'schema_version') -ne 'body-contract-v1' -or
                    (Get-LauncherObjectProperty $capabilities 'schema_version') -ne 'body-contract-v1' -or
                    'body-contract-v1' -notin $contractVersions) { $lastReason = 'body contract identity is incompatible' }
                elseif ((Get-LauncherObjectProperty $health 'status') -ne 'healthy') { $lastReason = 'body health is not healthy' }
                elseif ((Get-LauncherObjectProperty $health 'agency_state') -ne 'AVAILABLE') { $lastReason = 'body agency is terminal or unavailable' }
                elseif ((Get-LauncherObjectProperty $health 'manual_takeover_active' $false) -eq $true) { $lastReason = 'manual takeover is active' }
                elseif ($healthBody -ne $capabilityBody -or ($expectedBody -and $healthBody -ne $expectedBody)) { $lastReason = "body identity mismatch (expected=$expectedBody health=$healthBody capabilities=$capabilityBody)" }
                else {
                    $required = @(Get-MistyAIRequiredCapabilities -ManifestPath $ManifestPath)
                    $provided = @(Get-MistyAIProvidedCapabilities -Health $health -Capabilities $capabilities)
                    $missing = @($required | Where-Object { $_ -notin $provided })
                    if ($missing.Count -eq 0) {
                        return [pscustomobject][ordered]@{
                            health = $health
                            capabilities = $capabilities
                            body_instance_id = $healthBody
                            provided_capabilities = $provided
                        }
                    }
                    $lastReason = "missing required capabilities: $($missing -join ', ')"
                }
            } catch {
                $lastReason = Protect-LauncherText -Text $_.Exception.Message -Secrets @($Token)
            }
        } else {
            $lastReason = "services are not ready (body=$($serviceState.body), operator=$($serviceState.operator), compose=$($serviceState.compose))"
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    Throw-LauncherFailure -Message "MistyAI did not become ready: $lastReason" -ExitCode $script:ExitMistyAI
}

function Stop-MistyAI {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker,
        [switch]$StopBody,
        [switch]$StopOperator,
        [switch]$DisableBody,
        [switch]$DisableOperator,
        [switch]$StopCompose,
        [AllowEmptyString()][string]$ExpectedFingerprint = ''
    )
    if (-not $StopBody -and -not $StopOperator -and -not $DisableBody -and -not $DisableOperator -and -not $StopCompose) {
        return [pscustomobject][ordered]@{
            success = $true
            skipped = $true
            reason = 'not_owned'
            service_error = ''
            environment_error = ''
        }
    }
    $ownership = Get-MistyAIDeploymentOwnership -Distro $Distro -CommandInvoker $CommandInvoker
    $fingerprintMatches = -not $ExpectedFingerprint -or $ownership.current_fingerprint -eq $ExpectedFingerprint
    if (-not $ownership.probe_succeeded -or $ownership.current -ne 'managed' -or -not $fingerprintMatches) {
        return [pscustomobject][ordered]@{
            success = $false
            skipped = $false
            reason = 'deployment_ownership_mismatch'
            service_error = "Refusing to stop MistyAI because the managed deployment identity does not match launcher state (current=$($ownership.current), fingerprint=$($ownership.current_fingerprint))."
            environment_error = ''
        }
    }
    $unitCommands = New-Object System.Collections.Generic.List[string]
    [void]$unitCommands.Add('set -euo pipefail')
    [void]$unitCommands.Add((Get-MistyAIUnitIdentityBash))
    if ($StopOperator -or $DisableOperator) {
        [void]$unitCommands.Add('if test -e /etc/systemd/system/mistyai-operator.service || test -L /etc/systemd/system/mistyai-operator.service; then')
        [void]$unitCommands.Add('  verify_mistyai_owned_unit_identity mistyai-operator.service')
        if ($StopOperator) {
            [void]$unitCommands.Add('  systemctl stop mistyai-operator.service')
            [void]$unitCommands.Add("  if systemctl is-active --quiet mistyai-operator.service; then echo 'mistyai-operator.service did not stop' >&2; exit 71; fi")
        }
        if ($DisableOperator) {
            [void]$unitCommands.Add('  systemctl disable mistyai-operator.service')
            [void]$unitCommands.Add("  if systemctl is-enabled --quiet mistyai-operator.service; then echo 'mistyai-operator.service did not disable' >&2; exit 72; fi")
        }
        [void]$unitCommands.Add("elif ! verify_mistyai_unit_absent mistyai-operator.service; then echo 'mistyai-operator.service identity is unavailable or unexpected' >&2; exit 73; fi")
    }
    if ($StopBody -or $DisableBody) {
        [void]$unitCommands.Add('if test -e /etc/systemd/system/mistyai-body.service || test -L /etc/systemd/system/mistyai-body.service; then')
        [void]$unitCommands.Add('  verify_mistyai_owned_unit_identity mistyai-body.service')
        if ($StopBody) {
            [void]$unitCommands.Add('  systemctl stop mistyai-body.service')
            [void]$unitCommands.Add("  if systemctl is-active --quiet mistyai-body.service; then echo 'mistyai-body.service did not stop' >&2; exit 71; fi")
        }
        if ($DisableBody) {
            [void]$unitCommands.Add('  systemctl disable mistyai-body.service')
            [void]$unitCommands.Add("  if systemctl is-enabled --quiet mistyai-body.service; then echo 'mistyai-body.service did not disable' >&2; exit 72; fi")
        }
        [void]$unitCommands.Add("elif ! verify_mistyai_unit_absent mistyai-body.service; then echo 'mistyai-body.service identity is unavailable or unexpected' >&2; exit 73; fi")
    }
    $serviceResult = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script ($unitCommands -join "`n") -Description 'stop or disable launcher-owned MistyAI systemd units' -CommandInvoker $CommandInvoker
    if ($serviceResult.exit_code -ne 0) {
        $envResult = [pscustomobject]@{ exit_code = 0; stdout = ''; stderr = 'Compose stop was skipped because a launcher-owned systemd unit could not be stopped safely.' }
    } elseif ($StopCompose) {
        $composeStopScript = @'
set -euo pipefail
docker_bin=$(command -v docker 2>/dev/null || true)
case "$docker_bin" in ''|/mnt/[a-zA-Z]/*) echo 'native WSL Docker integration is unavailable' >&2; exit 67 ;; esac
test -f /opt/mistyai/deploy/compose.yaml
test ! -L /opt/mistyai/deploy/compose.yaml
if ! test -x /opt/mistyai/.venv/bin/mistyctl; then
  echo 'managed MistyAI mistyctl is unavailable; refusing to forget Compose ownership' >&2
  exit 68
fi
/opt/mistyai/.venv/bin/mistyctl env down --json
for service in desktop agent loopback-gateway; do
  if ! remaining_containers=$(timeout --signal=TERM --kill-after=1s 5s "$docker_bin" compose -f /opt/mistyai/deploy/compose.yaml ps -aq "$service" 2>/dev/null); then
    echo "Cannot verify MistyAI Compose shutdown; Docker query failed: $service" >&2
    exit 70
  fi
  if test -n "$remaining_containers"; then
    echo "MistyAI Compose service still has containers after down: $service" >&2
    exit 69
  fi
done
'@
        $envResult = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -WorkingDirectory '/opt/mistyai' -Script $composeStopScript -Description 'stop launcher-owned MistyAI Compose services' -CommandInvoker $CommandInvoker
    } else {
        $envResult = [pscustomobject]@{ exit_code = 0; stdout = ''; stderr = '' }
    }
    return [pscustomobject][ordered]@{
        success = $serviceResult.exit_code -eq 0 -and $envResult.exit_code -eq 0
        skipped = $false
        reason = ''
        service_error = Protect-LauncherText $serviceResult.stderr
        environment_error = Protect-LauncherText $envResult.stderr
    }
}

function Get-MistyAIInstallationChecks {
    param(
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $script = (Get-MistyAIUnitIdentityBash) + "`n" + @'
set +e
test -x /opt/mistyai/.venv/bin/mistyctl && printf 'deployment=yes\n' || printf 'deployment=no\n'
test -f /etc/mistyai/m0.yaml -a -f /etc/mistyai/body-service.env && printf 'config=yes\n' || printf 'config=no\n'
unsafe_paths=no
for path in \
  /etc/mistyai \
  /etc/mistyai/m0.yaml \
  /etc/mistyai/body-service.env \
  /etc/mistyai/body-service.token \
  /etc/mistyai/instances \
  /etc/mistyai/instances/misty-body-01 \
  /etc/mistyai/instances/misty-body-01/agent.token \
  /etc/mistyai/instances/misty-body-01/novnc.password \
  /var/lib/mistyai \
  /var/lib/mistyai/state \
  /var/lib/mistyai/evidence \
  /run/mistyai; do
  if test -L "$path"; then unsafe_paths=yes; fi
done
printf 'unsafe_paths=%s\n' "$unsafe_paths"
probe_unit() {
  unit="$1"
  if ! systemctl cat "$unit" >/dev/null 2>&1; then
    if verify_mistyai_unit_absent "$unit"; then printf 'missing'; else printf 'mismatch'; fi
    return
  fi
  if verify_mistyai_unit_identity "$unit"; then
    printf 'valid'
  elif grep -qx 'neuralcloud-launcher-v1' /opt/mistyai.previous/.neuralcloud-launcher-deployment 2>/dev/null && \
       verify_mistyai_unit_identity "$unit" /opt/mistyai.previous; then
    printf 'stale'
  else
    printf 'mismatch'
  fi
}
body_unit=$(probe_unit mistyai-body.service)
operator_unit=$(probe_unit mistyai-operator.service)
if test "$body_unit" = valid && test "$operator_unit" = valid; then
  printf 'units=valid\n'
elif test "$body_unit" = mismatch || test "$operator_unit" = mismatch; then
  printf 'units=mismatch\n'
elif test "$body_unit" = stale || test "$operator_unit" = stale; then
  printf 'units=stale\n'
else
  printf 'units=missing\n'
fi
print_file_stat() {
  label="$1"
  format="$2"
  path="$3"
  if test -f "$path" && ! test -L "$path"; then
    printf '%s=' "$label"
    stat -c "$format" "$path" 2>/dev/null || printf 'unreadable\n'
  else
    printf '%s=missing\n' "$label"
  fi
}
print_file_stat body_env '%a:%U:%G' /etc/mistyai/body-service.env
print_file_stat body_token '%a:%U:%G' /etc/mistyai/body-service.token
print_file_stat agent_token_file '%a:%u:%g' /etc/mistyai/instances/misty-body-01/agent.token
print_file_stat novnc_password '%a:%u:%G' /etc/mistyai/instances/misty-body-01/novnc.password
if test -d /etc/mistyai/instances/misty-body-01 && ! test -L /etc/mistyai/instances/misty-body-01; then
  stat -c 'instance_dir=%a:%u:%G' /etc/mistyai/instances/misty-body-01 2>/dev/null || printf 'instance_dir=unreadable\n'
else
  printf 'instance_dir=missing\n'
fi
'@
    $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description 'inspect the installed MistyAI configuration' -CommandInvoker $CommandInvoker
    $text = $result.stdout
    $ownership = Get-MistyAIDeploymentOwnership -Distro $Distro -CommandInvoker $CommandInvoker
    $ownershipReady = $ownership.probe_succeeded -and
        $ownership.current -in @('absent', 'managed') -and
        $ownership.previous -in @('absent', 'managed') -and
        -not ($ownership.current -eq 'absent' -and $ownership.previous -eq 'managed')
    $deployment = $ownership.current -eq 'managed' -and $text -match '(?m)^deployment=yes\s*$'
    $config = $text -match '(?m)^config=yes\s*$'
    $pathsSafe = $text -match '(?m)^unsafe_paths=no\s*$'
    $unitMatch = [regex]::Match($text, '(?m)^units=(valid|stale|missing|mismatch)\s*$')
    $unitState = if ($unitMatch.Success) { $unitMatch.Groups[1].Value } else { 'mismatch' }
    $expectedSecretPermissions = [ordered]@{
        body_env = '640:root:mistyai-svc'
        body_token = '640:root:misty-operator'
        agent_token_file = '400:2002:0'
        novnc_password = '440:2001:misty-operator'
        instance_dir = '2770:0:misty-operator'
    }
    $secretPermissionValues = [ordered]@{}
    $secretPermissionsReady = $true
    foreach ($name in $expectedSecretPermissions.Keys) {
        $match = [regex]::Match($text, "(?m)^$([regex]::Escape($name))=([^\r\n]+)")
        $value = if ($match.Success) { $match.Groups[1].Value.Trim() } else { 'missing' }
        $secretPermissionValues[$name] = $value
        if ($value -ne $expectedSecretPermissions[$name]) { $secretPermissionsReady = $false }
    }
    $secretPermissionSummary = @($secretPermissionValues.Keys | ForEach-Object { "$_=$($secretPermissionValues[$_])" }) -join ', '
    return @(
        (New-LauncherCheck -Id 'mistyai.deployment_ownership' -Category 'mistyai' -Current "current=$($ownership.current), previous=$($ownership.previous)" -Required 'launcher-managed deployment marker, or both paths absent before first install' -Status $(if ($ownershipReady) { 'pass' } else { 'blocked' }) -Remediation 'Back up or move any unmarked /opt/mistyai path manually. If only managed /opt/mistyai.previous exists, restore it to /opt/mistyai before retrying.'),
        (New-LauncherCheck -Id 'mistyai.deployment' -Category 'mistyai' -Current $(if ($deployment) { '/opt/mistyai' } else { 'missing' }) -Required 'deployed MistyAI runtime at /opt/mistyai' -Status $(if ($deployment) { 'pass' } else { 'missing' }) -Remediation 'Run dev.ps1 install or dev.ps1 up to deploy the local workspace.'),
        (New-LauncherCheck -Id 'mistyai.path_safety' -Category 'mistyai' -Current $(if ($pathsSafe) { 'no symbolic links' } else { 'unsafe or unreadable path detected' }) -Required 'configuration, credential, runtime, database, and evidence paths must not be symbolic links' -Status $(if ($pathsSafe) { 'pass' } else { 'blocked' }) -Remediation 'Inspect /etc/mistyai, /var/lib/mistyai, and /run/mistyai manually; replace unsafe symbolic links without deleting their targets.'),
        (New-LauncherCheck -Id 'mistyai.configuration' -Category 'mistyai' -Current $(if ($config) { '/etc/mistyai' } else { 'missing' }) -Required 'persistent config in /etc/mistyai' -Status $(if ($config) { 'pass' } else { 'missing' }) -Remediation 'The launcher will run mistyctl config init during install.'),
        (New-LauncherCheck -Id 'mistyai.systemd_units' -Category 'mistyai' -Current $unitState -Required 'exact launcher-managed units without systemd drop-ins' -Status $(if ($unitState -eq 'valid') { 'pass' } elseif ($unitState -in @('missing', 'stale')) { 'missing' } else { 'blocked' }) -Remediation $(if ($unitState -eq 'mismatch') { 'Inspect the same-named units, FragmentPath, and DropInPaths manually; the launcher will not overwrite an unexpected unit identity.' } else { 'The launcher will run mistyctl service install as WSL root.' })),
        (New-LauncherCheck -Id 'mistyai.token_permissions' -Category 'mistyai' -Current $secretPermissionSummary -Required 'exact non-symlink permissions for body env/token, agent token, noVNC password, and instance directory' -Status $(if ($secretPermissionsReady) { 'pass' } else { 'blocked' }) -Remediation 'Re-run mistyctl config init and mistyctl service install; do not copy the credentials to Windows.')
    )
}

function Get-LauncherPowerShellExecutable {
    foreach ($candidate in @('powershell.exe', 'pwsh.exe', 'pwsh')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) { return [string]$command.Source }
    }
    return 'powershell.exe'
}

function Stop-NeuralCloud {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$StateRoot = '',
        [scriptblock]$CommandInvoker
    )
    $shell = Get-LauncherPowerShellExecutable
    $arguments = @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'stop.ps1'), '-Quiet'
    )
    if ($StateRoot) { $arguments += @('-StateRoot', $StateRoot) }
    $result = Invoke-LauncherExternalCommand -FilePath $shell -ArgumentList $arguments -WorkingDirectory $Root -CommandInvoker $CommandInvoker
    return [pscustomobject][ordered]@{
        success = $result.exit_code -eq 0
        output = Protect-LauncherText (($result.stdout + "`n" + $result.stderr).Trim())
    }
}

function Stop-LauncherNewNeuralCloudProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [AllowEmptyCollection()][object[]]$BeforeEntries = @(),
        [Parameter(Mandatory = $true)][datetime]$StartedAt,
        [scriptblock]$CommandInvoker
    )
    $registryPath = Join-Path $Root '.neuralcloud.pids.json'
    if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
        return [pscustomobject]@{ success = $true; skipped = $true; output = '' }
    }
    $originalText = Get-Content -LiteralPath $registryPath -Raw -ErrorAction Stop
    $registry = $originalText | ConvertFrom-Json -ErrorAction Stop
    $newEntries = New-Object System.Collections.Generic.List[object]
    $keptEntries = New-Object System.Collections.Generic.List[object]
    foreach ($entry in @((Get-LauncherObjectProperty $registry 'services' @()))) {
        $pidValue = [int](Get-LauncherObjectProperty $entry 'pid' 0)
        $rawStamp = Get-LauncherObjectProperty $entry 'process_start_time_utc' ''
        $stamp = if ($rawStamp -is [datetime]) { $rawStamp.ToUniversalTime().ToString('o') } else { [string]$rawStamp }
        $wasPresent = @($BeforeEntries | Where-Object {
            $beforeStamp = Get-LauncherObjectProperty $_ 'process_start_time_utc' ''
            if ($beforeStamp -is [datetime]) { $beforeStamp = $beforeStamp.ToUniversalTime().ToString('o') }
            [int](Get-LauncherObjectProperty $_ 'pid' 0) -eq $pidValue -and
            [string]$beforeStamp -eq $stamp
        }).Count -gt 0
        $entryTime = [datetime]::MinValue
        $timeKnown = [datetime]::TryParse($stamp, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$entryTime)
        $entryRoot = [string](Get-LauncherObjectProperty $entry 'root' '')
        if (-not $wasPresent -and $pidValue -gt 0 -and $timeKnown -and
            $entryTime.ToUniversalTime() -ge $StartedAt.ToUniversalTime().AddSeconds(-1) -and
            $entryRoot.TrimEnd('\', '/') -eq $Root.TrimEnd('\', '/')) {
            [void]$newEntries.Add($entry)
        } else {
            [void]$keptEntries.Add($entry)
        }
    }
    if ($newEntries.Count -eq 0) {
        return [pscustomobject]@{ success = $true; skipped = $true; output = '' }
    }
    # stop.ps1 rechecks executable, module and start time for this filtered
    # registry. It never receives entries that existed before this invocation.
    $cleanupRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('neuralcloud-rollback-' + [guid]::NewGuid().ToString('N'))
    [void][System.IO.Directory]::CreateDirectory($cleanupRoot)
    $cleanupRegistry = Join-Path $cleanupRoot '.neuralcloud.pids.json'
    $registry.services = @($newEntries.ToArray())
    [System.IO.File]::WriteAllText($cleanupRegistry, ($registry | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding($false)))
    $cleanup = Stop-NeuralCloud -Root $Root -StateRoot $cleanupRoot -CommandInvoker $CommandInvoker
    if ($cleanup.success) {
        # Do not overwrite a registry changed by a concurrent launcher.
        if ((Test-Path -LiteralPath $registryPath -PathType Leaf) -and
            (Get-Content -LiteralPath $registryPath -Raw) -ceq $originalText) {
            if ($keptEntries.Count -eq 0) {
                Remove-Item -LiteralPath $registryPath -Force -ErrorAction Stop
            } else {
                $registry.services = @($keptEntries.ToArray())
                [System.IO.File]::WriteAllText($registryPath, ($registry | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding($false)))
            }
        }
        if (Test-Path -LiteralPath $cleanupRegistry) { Remove-Item -LiteralPath $cleanupRegistry -Force }
        # Only remove this newly-created empty directory, never recursively.
        [System.IO.Directory]::Delete($cleanupRoot, $false)
    } else {
        $cleanup.output += " Recovery registry retained at $cleanupRegistry"
    }
    return $cleanup
}

function Start-NeuralCloud {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [AllowEmptyString()][ValidateSet('', 'demo', 'live', 'ollama')][string]$Mode = '',
        [Parameter(Mandatory = $true)][string]$Token,
        [scriptblock]$CommandInvoker
    )
    $shell = Get-LauncherPowerShellExecutable
    $manifest = Join-Path $Root 'presets\runtime\shared-misty-v1.json'
    $environment = @{
        NC_MISTYAI_TOKEN = $Token
        NC_MISTYAI_TOKEN_FILE = $null
        NC_MISTYAI_BASE_URL = 'http://127.0.0.1:8787'
    }
    $arguments = @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'start.ps1'),
        '-ManifestPath', $manifest,
        '-EmbodimentProvider', 'auto',
        '-MistyAIBaseUrl', 'http://127.0.0.1:8787'
    )
    if ($Mode) { $arguments += @('-Mode', $Mode) }
    $result = Invoke-LauncherExternalCommand -FilePath $shell -ArgumentList $arguments `
        -WorkingDirectory $Root -Environment $environment -SensitiveValues @($Token) -FileBackedOutput -CommandInvoker $CommandInvoker
    if ($result.exit_code -ne 0) {
        $detail = Protect-LauncherText (($result.stderr + "`n" + $result.stdout).Trim()) @($Token)
        Throw-LauncherFailure -Message "NeuralCloud startup failed: $detail" -ExitCode $script:ExitNeuralCloud
    }
    return [pscustomobject][ordered]@{
        output = Protect-LauncherText $result.stdout @($Token)
        manifest = $manifest
    }
}

function Test-NeuralCloudRegistryHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$RegistryPath = '',
        [hashtable]$Ports = @{}
    )
    $entries = @(Get-NeuralCloudRegistryEntries -Root $Root -RegistryPath $RegistryPath)
    $services = New-Object System.Collections.Generic.List[object]
    $expected = @{
        cognitive = [pscustomobject]@{ module = 'harness.cognitive.service'; port = 8771 }
        homeostasis = [pscustomobject]@{ module = 'harness.homeostasis.service'; port = 8772 }
        runtime = [pscustomobject]@{ module = 'harness.runtime.service'; port = 8770 }
        gui = [pscustomobject]@{ module = 'gui.runtime_server'; port = 8790 }
    }
    foreach ($name in $Ports.Keys) {
        if (-not $expected.ContainsKey($name) -or [int]$Ports[$name] -lt 1 -or [int]$Ports[$name] -gt 65535) {
            throw "Invalid NeuralCloud service port: $name"
        }
        $expected[$name].port = [int]$Ports[$name]
    }
    $expectedPython = Join-Path $Root '.venv\Scripts\python.exe'
    foreach ($entry in $entries) {
        $name = [string](Get-LauncherObjectProperty $entry 'name' 'unknown')
        $pidValue = [int](Get-LauncherObjectProperty $entry 'pid' 0)
        $module = [string](Get-LauncherObjectProperty $entry 'module' '')
        $port = [int](Get-LauncherObjectProperty $entry 'port' 0)
        $entryRoot = [string](Get-LauncherObjectProperty $entry 'root' '')
        $entryPython = [string](Get-LauncherObjectProperty $entry 'python_path' '')
        $entryStart = Get-LauncherObjectProperty $entry 'process_start_time_utc' ''
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        $commandLine = if ($process) { Get-LauncherProcessCommandLine -ProcessId $pidValue } else { '' }
        $spec = if ($expected.ContainsKey($name)) { $expected[$name] } else { $null }
        $processPathMatches = $false
        $processStartMatches = $false
        if ($process) {
            try {
                $processPathMatches = [System.IO.Path]::GetFullPath([string]$process.Path).Equals(
                    [System.IO.Path]::GetFullPath($expectedPython),
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            } catch { $processPathMatches = $false }
            $recordedStart = [datetime]::MinValue
            $parsedStart = if ($entryStart -is [datetime]) {
                $recordedStart = ([datetime]$entryStart).ToUniversalTime()
                $true
            } else {
                [datetime]::TryParse(
                    [string]$entryStart,
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [System.Globalization.DateTimeStyles]::RoundtripKind,
                    [ref]$recordedStart
                )
            }
            if ($parsedStart) {
                try {
                    $processStartMatches = [math]::Abs(
                        ($process.StartTime.ToUniversalTime() - $recordedStart.ToUniversalTime()).TotalSeconds
                    ) -le 1
                } catch { $processStartMatches = $false }
            }
        }
        $portOwner = Get-LauncherPortOwner -Port $port
        $portMatches = $null -ne $portOwner -and $portOwner.pid -eq $pidValue
        if ($null -ne $portOwner -and $portOwner.pid -ne $pidValue) {
            $ownerPid = [int]$portOwner.pid
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
            $ownerCommandLine = Get-LauncherProcessCommandLine -ProcessId $ownerPid
            $portMatches = $null -ne $owner -and
                [int]$owner.ParentProcessId -eq $pidValue -and
                $owner.Name -match '^python(w)?\.exe$' -and
                $ownerCommandLine -match [regex]::Escape($module)
        }
        $identityMatches = $null -ne $process -and $null -ne $spec -and
            $process.ProcessName -match '^pythonw?$' -and
            $module -eq $spec.module -and $port -eq $spec.port -and
            $entryRoot.TrimEnd('\', '/') -eq $Root.TrimEnd('\', '/') -and
            $entryPython -eq $expectedPython -and
            $processPathMatches -and $processStartMatches -and $portMatches -and
            $commandLine -match [regex]::Escape($module) -and
            $commandLine.IndexOf($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        [void]$services.Add([pscustomobject][ordered]@{
            name = $name
            pid = $pidValue
            port = $port
            running = $null -ne $process
            identity_matches = [bool]$identityMatches
        })
    }
    $completeSet = $entries.Count -eq $expected.Count
    foreach ($name in $expected.Keys) {
        if (@($services | Where-Object { $_.name -eq $name }).Count -ne 1) { $completeSet = $false }
    }
    return [pscustomobject][ordered]@{
        running = $completeSet -and @($services | Where-Object { -not $_.running -or -not $_.identity_matches }).Count -eq 0
        services = $services.ToArray()
    }
}

function Wait-NeuralCloudReady {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [int]$TimeoutSeconds = 30
    )
    $manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
    $embodimentSpec = Get-LauncherObjectProperty $manifest 'embodiment'
    $expectedProvider = [string](Get-LauncherObjectProperty $embodimentSpec 'body_provider' 'none')
    $expectedMode = [string](Get-LauncherObjectProperty $embodimentSpec 'mode' 'off')
    $expectedBody = [string](Get-LauncherObjectProperty $embodimentSpec 'body_instance_id' '')
    $required = @((Get-LauncherObjectProperty $embodimentSpec 'capability_requirements' @()) |
        Where-Object { (Get-LauncherObjectProperty $_ 'required' $false) -eq $true } |
        ForEach-Object { [string](Get-LauncherObjectProperty $_ 'capability' '') })
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastReason = 'GUI status not available'
    do {
        try {
            $status = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8790/api/status' -TimeoutSec 3
            $runtime = Get-LauncherObjectProperty $status 'runtime'
            $embodiment = Get-LauncherObjectProperty $runtime 'embodiment'
            $negotiated = Get-LauncherObjectProperty $embodiment 'negotiated'
            $activated = @((Get-LauncherObjectProperty $embodiment 'activated_capabilities' @()))
            $missing = @($required | Where-Object { $_ -notin $activated })
            if ((Get-LauncherObjectProperty $runtime 'lifecycle') -ne 'RUNNING') { $lastReason = 'Runtime lifecycle is not RUNNING' }
            elseif ($expectedMode -ne 'off' -and (Get-LauncherObjectProperty $embodiment 'state') -ne 'available') { $lastReason = 'Embodiment state is not available' }
            elseif ((Get-LauncherObjectProperty $embodiment 'provider_id') -ne $expectedProvider) { $lastReason = 'Embodiment provider does not match the manifest' }
            elseif ($expectedBody -and (Get-LauncherObjectProperty $embodiment 'body_instance_id') -ne $expectedBody) { $lastReason = 'Embodiment body identity does not match the manifest' }
            elseif (@((Get-LauncherObjectProperty $negotiated 'missing_required' @())).Count -ne 0) { $lastReason = 'Negotiation reports missing required capabilities' }
            elseif (@((Get-LauncherObjectProperty $negotiated 'incompatible_required' @())).Count -ne 0) { $lastReason = 'Negotiation reports incompatible required capabilities' }
            elseif ($missing.Count -gt 0) { $lastReason = "Runtime did not activate: $($missing -join ', ')" }
            else { return $status }
        } catch {
            $lastReason = Protect-LauncherText $_.Exception.Message
        }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    Throw-LauncherFailure -Message "NeuralCloud did not pass final readiness verification: $lastReason" -ExitCode $script:ExitNeuralCloud
}

function Invoke-LauncherDoctorCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $checks = @(Get-LauncherPrerequisiteChecks -Root $Root -MistyAIPath $MistyAIPath -Distro $Distro -CommandInvoker $CommandInvoker -IncludeRuntimePorts)
    $installationProbeReady = @($checks | Where-Object {
        $_.id -in @('windows.wsl_distro', 'wsl.ubuntu', 'wsl.systemd') -and $_.status -eq 'pass'
    }).Count -eq 3
    if ($installationProbeReady) { $checks += @(Get-MistyAIInstallationChecks -Distro $Distro -CommandInvoker $CommandInvoker) }
    $ready = Test-LauncherChecksReady $checks
    $configurationConflicts = @($checks | Where-Object {
        $_.status -eq 'blocked' -and (
            $_.category -in @('repository', 'runtime') -or
            $_.id -in @('mistyai.deployment_ownership', 'mistyai.path_safety', 'mistyai.systemd_units')
        )
    })
    $exitCode = if ($ready) {
        $script:ExitSuccess
    } elseif ($configurationConflicts.Count -gt 0) {
        $script:ExitConfiguration
    } else {
        $script:ExitNeedsAction
    }
    return [pscustomobject][ordered]@{
        status = if ($ready) { 'ready' } elseif ($exitCode -eq $script:ExitConfiguration) { 'blocked' } else { 'action_required' }
        exit_code = $exitCode
        checks = $checks
    }
}

function Invoke-LauncherStatusCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Distro,
        [scriptblock]$CommandInvoker
    )
    $neural = Test-NeuralCloudRegistryHealth -Root $Root
    try { $misty = Get-MistyAIServiceState -Distro $Distro -CommandInvoker $CommandInvoker } catch {
        $misty = [pscustomobject]@{ body = 'unavailable'; operator = 'unavailable'; compose = 'unavailable'; running = $false }
    }
    $mistyHealth = $null
    $mistyHealthError = $null
    $token = $null
    if ($misty.running) {
        try {
            $token = Read-MistyAIServiceToken -Distro $Distro -CommandInvoker $CommandInvoker
            $mistyHealth = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8787/v1/health' `
                -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
        } catch {
            $mistyHealthError = Protect-LauncherText -Text $_.Exception.Message -Secrets @($token)
        } finally {
            $token = $null
        }
    }
    $gui = $null
    if ($neural.running) {
        try { $gui = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8790/api/status' -TimeoutSec 2 } catch { $gui = $null }
    }
    $runtime = if ($gui) { Get-LauncherObjectProperty $gui 'runtime' } else { $null }
    $embodiment = Get-LauncherObjectProperty $runtime 'embodiment'
    $runtimeReady = $null -ne $runtime -and
        (Get-LauncherObjectProperty $runtime 'lifecycle') -eq 'RUNNING' -and
        (Get-LauncherObjectProperty $embodiment 'state') -eq 'available' -and
        (Get-LauncherObjectProperty $embodiment 'provider_id') -eq 'mistyai'
    $bodyReady = $null -ne $mistyHealth -and (Get-LauncherObjectProperty $mistyHealth 'status') -eq 'healthy'
    $ready = $neural.running -and $misty.running -and $bodyReady -and $runtimeReady
    $anyNeuralProcess = @($neural.services | Where-Object { $_.running }).Count -gt 0
    $activeUnitStates = @('active', 'activating', 'reloading', 'deactivating')
    $anythingRunning = $neural.running -or $anyNeuralProcess -or $misty.running -or $misty.body -in $activeUnitStates -or
        $misty.operator -in $activeUnitStates -or $misty.compose -in @('running', 'partial')
    $stateUnavailable = $misty.compose -in @('unknown', 'unavailable') -or
        $misty.body -in @('unknown', 'unavailable') -or $misty.operator -in @('unknown', 'unavailable')
    return [pscustomobject][ordered]@{
        status = if ($ready) { 'running' } elseif ($anythingRunning) { 'partial' } elseif ($stateUnavailable) { 'unavailable' } else { 'stopped' }
        exit_code = if ($stateUnavailable) { $script:ExitNeedsAction } else { $script:ExitSuccess }
        neuralcloud = $neural
        mistyai = $misty
        mistyai_health = $mistyHealth
        mistyai_health_error = $mistyHealthError
        runtime = $runtime
        urls = [pscustomobject][ordered]@{
            gui = 'http://127.0.0.1:8790'
            operations = 'http://127.0.0.1:8790/ops'
            novnc = 'http://127.0.0.1:6080'
        }
    }
}

function Start-LauncherDockerDesktop {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Distro,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = 120,
        [scriptblock]$CommandInvoker
    )
    $scriptPath = Join-Path $Root 'tools\docker\Start-DockerDesktop.ps1'
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        return [pscustomobject]@{ id = 'windows.docker_desktop'; status = 'manual'; detail = "Docker startup helper is missing: $scriptPath" }
    }
    # The helper starts only an installed Desktop, preserves running backends,
    # and bounds each probe/retry. It does not install packages or shut down WSL.
    $result = Invoke-LauncherExternalCommand -FilePath (Get-LauncherPowerShellExecutable) -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $scriptPath, '-WslDistro', $Distro, '-Json', '-TimeoutSeconds', [string]$TimeoutSeconds
    ) -WorkingDirectory $Root -TimeoutSeconds (3 * $TimeoutSeconds + 100) -CommandInvoker $CommandInvoker
    $detail = Protect-LauncherText (($result.stdout + "`n" + $result.stderr).Trim())
    try {
        $payload = $result.stdout | ConvertFrom-Json -ErrorAction Stop
        $detail = [string](Get-LauncherObjectProperty $payload 'message' $detail)
    } catch { }
    return [pscustomobject]@{
        id = 'windows.docker_desktop'
        status = if ($result.exit_code -eq 0) { 'started' } else { 'manual' }
        detail = $detail
    }
}

function Assert-LauncherPrerequisites {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [switch]$IncludeRuntimePorts,
        [switch]$NonInteractive,
        [switch]$Json,
        [bool]$AutoStartDocker = $false,
        [ValidateRange(1, 600)][int]$DockerStartupTimeoutSeconds = 120,
        [scriptblock]$CommandInvoker
    )
    $checkArguments = @{
        Root = $Root
        MistyAIPath = $MistyAIPath
        Distro = $Distro
        CommandInvoker = $CommandInvoker
        IncludeRuntimePorts = $IncludeRuntimePorts
    }
    $checks = @(Get-LauncherPrerequisiteChecks @checkArguments)
    $conflicts = @($checks | Where-Object {
        $_.status -eq 'blocked' -and ($_.category -eq 'repository' -or $_.category -eq 'runtime')
    })
    if ($conflicts.Count -gt 0) {
        Throw-LauncherFailure -Message 'Repository validation or a runtime port ownership check failed.' -ExitCode $script:ExitConfiguration -Details $checks
    }
    $startupActions = @()
    if ($AutoStartDocker -and @($checks | Where-Object {
        $_.id -eq 'windows.docker_desktop' -and $_.status -eq 'action_required'
    }).Count -gt 0) {
        if (-not $Json) { Write-Host 'Starting installed Docker Desktop in the background and waiting for its Linux engine...' }
        $startupActions = @(Start-LauncherDockerDesktop -Root $Root -Distro $Distro -TimeoutSeconds $DockerStartupTimeoutSeconds -CommandInvoker $CommandInvoker)
        $checks = @(Get-LauncherPrerequisiteChecks @checkArguments)
    }
    if (Test-LauncherChecksReady $checks) {
        return [pscustomobject]@{ checks = $checks; actions = $startupActions }
    }
    $installation = Invoke-LauncherInstallPrerequisites -Checks $checks -Distro $Distro -NonInteractive:$NonInteractive -Json:$Json -CommandInvoker $CommandInvoker
    $checks = @(Get-LauncherPrerequisiteChecks @checkArguments)
    $conflicts = @($checks | Where-Object {
        $_.status -eq 'blocked' -and ($_.category -eq 'repository' -or $_.category -eq 'runtime')
    })
    if ($conflicts.Count -gt 0) {
        Throw-LauncherFailure -Message 'Repository validation or a runtime port ownership check failed.' -ExitCode $script:ExitConfiguration -Details $checks
    }
    if (-not (Test-LauncherChecksReady $checks)) {
        $message = if ($installation.restart_required) {
            'Prerequisite changes require a restart or a fresh terminal. Re-run the same launcher command afterwards.'
        } else {
            'Prerequisites are not ready. Complete the reported remediation and re-run the launcher.'
        }
        Throw-LauncherFailure -Message $message -ExitCode $script:ExitNeedsAction -Details ([pscustomobject]@{ checks = $checks; actions = @($startupActions) + @($installation.actions) })
    }
    return [pscustomobject]@{ checks = $checks; actions = @($startupActions) + @($installation.actions) }
}

function Invoke-LauncherInstallCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][object]$Paths,
        [switch]$NonInteractive,
        [switch]$Json,
        [scriptblock]$CommandInvoker
    )
    $ownedState = Get-LauncherObjectProperty $State 'owned'
    $neuralOwned = [bool](Get-LauncherObjectProperty $ownedState 'neuralcloud' $false)
    $mistyOwnedState = Get-LauncherObjectProperty $ownedState 'mistyai'
    $priorMistyOwned = [pscustomobject][ordered]@{
        body = [bool](Get-LauncherObjectProperty $mistyOwnedState 'body' $false)
        operator = [bool](Get-LauncherObjectProperty $mistyOwnedState 'operator' $false)
        compose = [bool](Get-LauncherObjectProperty $mistyOwnedState 'compose' $false)
    }
    $enablementOwnedState = Get-LauncherObjectProperty $State 'mistyai_unit_enablement_owned'
    $priorEnablementOwned = [pscustomobject][ordered]@{
        body = [bool](Get-LauncherObjectProperty $enablementOwnedState 'body' $false)
        operator = [bool](Get-LauncherObjectProperty $enablementOwnedState 'operator' $false)
    }
    $mistyOwned = $priorMistyOwned.body -or $priorMistyOwned.operator -or $priorMistyOwned.compose -or
        $priorEnablementOwned.body -or $priorEnablementOwned.operator
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $resolvedMistySource = [System.IO.Path]::GetFullPath($MistyAIPath).TrimEnd('\', '/')
    $recordedRoot = [string](Get-LauncherObjectProperty $State 'neuralcloud_root' '')
    $recordedDistro = [string](Get-LauncherObjectProperty $State 'mistyai_distro' '')
    $recordedMistySource = [string](Get-LauncherObjectProperty $State 'mistyai_source_path' '')
    $recordedDeploymentFingerprint = [string](Get-LauncherObjectProperty $State 'mistyai_deployment_fingerprint' '')
    if ($neuralOwned -and $recordedRoot -ne $resolvedRoot) {
        Throw-LauncherFailure -Message "Launcher state belongs to a different NeuralCloud checkout (recorded root: $recordedRoot). Run down from that checkout first." -ExitCode $script:ExitConfiguration
    }
    if ($mistyOwned -and ($recordedDistro -ne $Distro -or $recordedMistySource -ne $resolvedMistySource)) {
        Throw-LauncherFailure -Message 'Launcher state owns MistyAI components for a different source path or WSL distribution. Stop them with the recorded launcher configuration first.' -ExitCode $script:ExitConfiguration
    }
    $State.neuralcloud_root = $resolvedRoot
    $State.mistyai_source_path = $resolvedMistySource
    $State.mistyai_distro = $Distro
    $State.install_phase = 'checking_prerequisites'
    $State.last_command = 'install'
    Write-LauncherState -State $State -Path $Paths.state
    $preflight = Assert-LauncherPrerequisites -Root $Root -MistyAIPath $MistyAIPath -Distro $Distro -NonInteractive:$NonInteractive -Json:$Json -CommandInvoker $CommandInvoker
    $State.install_phase = 'preparing_neuralcloud'
    Write-LauncherState -State $State -Path $Paths.state
    $neural = Initialize-NeuralCloudEnvironment -Root $Root -State $State -CommandInvoker $CommandInvoker
    $initialMisty = Get-MistyAIServiceState -Distro $Distro -CommandInvoker $CommandInvoker
    Assert-MistyAIStableServiceState -State $initialMisty
    $State.install_phase = 'deploying_mistyai'
    Write-LauncherState -State $State -Path $Paths.state
    $deployment = $null
    try {
        $deployment = Deploy-MistyAIWorkspace -MistyAIPath $MistyAIPath -Distro $Distro -State $State -StagingDirectory $Paths.staging -CommandInvoker $CommandInvoker
        $State.mistyai_deployment_fingerprint = $deployment.fingerprint
        $State.mistyai_previous_available = $deployment.previous_available
        # service install is allowed to start units. Persist conservative ownership
        # before invoking it so an interrupted install can be recovered with down.
        $State.owned.mistyai = [pscustomobject][ordered]@{
            body = $priorMistyOwned.body -or ($initialMisty.body -in @('inactive', 'failed'))
            operator = $priorMistyOwned.operator -or ($initialMisty.operator -in @('inactive', 'failed'))
            compose = $priorMistyOwned.compose
        }
        $State.mistyai_unit_enablement_owned = [pscustomobject][ordered]@{
            body = $priorEnablementOwned.body -or ($initialMisty.body_enabled -ne 'enabled')
            operator = $priorEnablementOwned.operator -or ($initialMisty.operator_enabled -ne 'enabled')
        }
        $State.install_phase = 'configuring_mistyai'
        Write-LauncherState -State $State -Path $Paths.state
        [void](Initialize-MistyAIConfiguration -Distro $Distro -DeploymentChanged $deployment.changed `
            -PreserveBody:($initialMisty.body -eq 'active') `
            -PreserveOperator:($initialMisty.operator -eq 'active') `
            -PreserveCompose:($initialMisty.compose -eq 'running') `
            -CommandInvoker $CommandInvoker)
    } catch {
        $originalError = $_
        $deploymentRecoveryRequired = $originalError.Exception.Message -match 'deployment requires manual recovery'
        $serviceStateRestored = $false
        if ($deployment -and $deployment.changed -and $deployment.previous_available) {
            $rollbackSucceeded = Restore-MistyAIDeployment -Distro $Distro -CommandInvoker $CommandInvoker `
                -RestartBody:($initialMisty.body -eq 'active') `
                -RestartOperator:($initialMisty.operator -eq 'active') `
                -RestartCompose:($initialMisty.compose -eq 'running') `
                -PreviousImageId ([string](Get-LauncherObjectProperty $deployment 'previous_image_id' ''))
            if (-not $rollbackSucceeded) {
                $State.install_phase = 'recovery_required'
                try { Write-LauncherState -State $State -Path $Paths.state } catch { }
                $originalCode = Get-LauncherFailureCode -ErrorRecord $originalError -Default $script:ExitMistyAI
                Throw-LauncherFailure -Message "$($originalError.Exception.Message) Deployment rollback also failed; inspect /opt/mistyai and /opt/mistyai.previous before retrying." -ExitCode $originalCode
            }
            $State.mistyai_deployment_fingerprint = $recordedDeploymentFingerprint
            $State.mistyai_previous_available = $false
        }
        if ($deployment) {
            try {
                $currentMisty = Get-MistyAIServiceState -Distro $Distro -CommandInvoker $CommandInvoker
                $currentUnitsStable = $currentMisty.body -in @('active', 'inactive', 'failed') -and
                    $currentMisty.operator -in @('active', 'inactive', 'failed')
                $serviceStateRestored = $currentUnitsStable -and
                    (($currentMisty.body -eq 'active') -eq ($initialMisty.body -eq 'active')) -and
                    (($currentMisty.operator -eq 'active') -eq ($initialMisty.operator -eq 'active')) -and
                    $currentMisty.body_enabled -eq $initialMisty.body_enabled -and
                    $currentMisty.operator_enabled -eq $initialMisty.operator_enabled -and
                    $currentMisty.compose -eq $initialMisty.compose
            } catch {
                $serviceStateRestored = $false
            }
        }
        if ($serviceStateRestored) {
            $State.owned.mistyai = $priorMistyOwned
            $State.mistyai_unit_enablement_owned = $priorEnablementOwned
        }
        $serviceRecoveryRequired = $deployment -and -not $serviceStateRestored
        $State.install_phase = if ($deploymentRecoveryRequired -or $serviceRecoveryRequired) { 'recovery_required' } else { 'failed' }
        try { Write-LauncherState -State $State -Path $Paths.state } catch { }
        throw $originalError
    }
    $State.mistyai_deployment_fingerprint = $deployment.fingerprint
    $State.neuralcloud_root = $resolvedRoot
    $State.mistyai_source_path = $resolvedMistySource
    $State.mistyai_distro = $Distro
    $State.mistyai_previous_available = $deployment.previous_available
    $State.owned.mistyai = $priorMistyOwned
    $State.mistyai_unit_enablement_owned = $priorEnablementOwned
    $State.install_phase = 'ready'
    $State.last_command = 'install'
    $State.last_success_at = (Get-Date).ToUniversalTime().ToString('o')
    Write-LauncherState -State $State -Path $Paths.state
    return [pscustomobject][ordered]@{
        status = 'installed'
        exit_code = $script:ExitSuccess
        checks = $preflight.checks
        actions = $preflight.actions
        neuralcloud = $neural
        mistyai = $deployment
    }
}

function Invoke-LauncherUpCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [AllowEmptyString()][ValidateSet('', 'demo', 'live', 'ollama')][string]$Mode = '',
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][object]$Paths,
        [switch]$NonInteractive,
        [switch]$NoOpen,
        [switch]$Json,
        [bool]$AutoStartDocker = $true,
        [ValidateRange(1, 600)][int]$DockerStartupTimeoutSeconds = 120,
        [scriptblock]$CommandInvoker
    )
    $ownedState = Get-LauncherObjectProperty $State 'owned'
    $priorNeuralOwned = [bool](Get-LauncherObjectProperty $ownedState 'neuralcloud' $false)
    $priorMistyState = Get-LauncherObjectProperty $ownedState 'mistyai'
    $priorMistyOwned = [pscustomobject][ordered]@{
        body = [bool](Get-LauncherObjectProperty $priorMistyState 'body' $false)
        operator = [bool](Get-LauncherObjectProperty $priorMistyState 'operator' $false)
        compose = [bool](Get-LauncherObjectProperty $priorMistyState 'compose' $false)
    }
    $priorEnablementState = Get-LauncherObjectProperty $State 'mistyai_unit_enablement_owned'
    $priorEnablementOwned = [pscustomobject][ordered]@{
        body = [bool](Get-LauncherObjectProperty $priorEnablementState 'body' $false)
        operator = [bool](Get-LauncherObjectProperty $priorEnablementState 'operator' $false)
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $resolvedMistySource = [System.IO.Path]::GetFullPath($MistyAIPath).TrimEnd('\', '/')
    $recordedRoot = [string](Get-LauncherObjectProperty $State 'neuralcloud_root' '')
    $recordedDistro = [string](Get-LauncherObjectProperty $State 'mistyai_distro' '')
    $recordedMistySource = [string](Get-LauncherObjectProperty $State 'mistyai_source_path' '')
    $recordedDeploymentFingerprint = [string](Get-LauncherObjectProperty $State 'mistyai_deployment_fingerprint' '')
    if ($priorNeuralOwned -and $recordedRoot -ne $resolvedRoot) {
        Throw-LauncherFailure -Message "Launcher state belongs to a different NeuralCloud checkout (recorded root: $recordedRoot). Run down from that checkout first." -ExitCode $script:ExitConfiguration
    }
    if (($priorMistyOwned.body -or $priorMistyOwned.operator -or $priorMistyOwned.compose -or
        $priorEnablementOwned.body -or $priorEnablementOwned.operator) -and
        ($recordedDistro -ne $Distro -or $recordedMistySource -ne $resolvedMistySource)) {
        Throw-LauncherFailure -Message 'Launcher state owns MistyAI components for a different source path or WSL distribution. Stop them with the recorded launcher configuration first.' -ExitCode $script:ExitConfiguration
    }
    $State.neuralcloud_root = $resolvedRoot
    $State.mistyai_source_path = $resolvedMistySource
    $State.mistyai_distro = $Distro
    $State.install_phase = 'checking_prerequisites'
    $State.last_command = 'up'
    Write-LauncherState -State $State -Path $Paths.state
    $preflight = Assert-LauncherPrerequisites -Root $Root -MistyAIPath $MistyAIPath -Distro $Distro -IncludeRuntimePorts -NonInteractive:$NonInteractive -Json:$Json -AutoStartDocker:$AutoStartDocker -DockerStartupTimeoutSeconds $DockerStartupTimeoutSeconds -CommandInvoker $CommandInvoker
    $manifest = Join-Path $Root 'presets\runtime\shared-misty-v1.json'
    $beforeNeuralEntries = @(Get-NeuralCloudRegistryEntries -Root $Root)
    if ($beforeNeuralEntries.Count -gt 0) {
        $existingNeural = Test-NeuralCloudRegistryHealth -Root $Root
        if ($existingNeural.running) {
            $existingRegistry = Get-Content -LiteralPath (Join-Path $Root '.neuralcloud.pids.json') -Raw | ConvertFrom-Json
            $existingManifest = [string](Get-LauncherObjectProperty $existingRegistry 'manifest_path' '')
            if (-not $existingManifest -or -not [System.IO.Path]::GetFullPath($existingManifest).Equals(
                [System.IO.Path]::GetFullPath($manifest), [System.StringComparison]::OrdinalIgnoreCase)) {
                Throw-LauncherFailure -Message 'A healthy NeuralCloud instance is already running with a different manifest. Its services were preserved; use its matching launcher configuration or stop it explicitly first.' -ExitCode $script:ExitConfiguration
            }
            $existingReady = Wait-NeuralCloudReady -ManifestPath $manifest
            $existingMode = [string](Get-LauncherObjectProperty $existingReady 'mode' '')
            if ($Mode -and $existingMode -ne $Mode) {
                Throw-LauncherFailure -Message 'The existing NeuralCloud instance uses a different mode. Its services were preserved; stop it explicitly before changing mode.' -ExitCode $script:ExitConfiguration
            }
            $State.install_phase = 'running'
            $State.last_success_at = (Get-Date).ToUniversalTime().ToString('o')
            Write-LauncherState -State $State -Path $Paths.state
            if (-not $NoOpen -and -not $Json) {
                try { Start-Process 'http://127.0.0.1:8790' | Out-Null } catch { }
            }
            return [pscustomobject][ordered]@{
                status = 'running'; exit_code = $script:ExitSuccess; reused = $true
                mode = $(if ($existingMode) { $existingMode } else { 'configured' })
                deployment_fingerprint = $recordedDeploymentFingerprint
                neuralcloud = $existingNeural
                runtime = Get-LauncherObjectProperty $existingReady 'runtime'
                urls = [pscustomobject]@{ gui = 'http://127.0.0.1:8790'; operations = 'http://127.0.0.1:8790/ops'; novnc = 'http://127.0.0.1:6080' }
                logs = Join-Path $Root 'logs'
                checks = $preflight.checks
            }
        }
        if (@($existingNeural.services | Where-Object { $_.running }).Count -gt 0) {
            Throw-LauncherFailure -Message 'An existing NeuralCloud process registry is incomplete or does not match its live processes. Existing services were preserved; inspect or stop the recorded instance first.' -ExitCode $script:ExitConfiguration
        }
    }
    $initialMisty = Get-MistyAIServiceState -Distro $Distro -CommandInvoker $CommandInvoker
    Assert-MistyAIStableServiceState -State $initialMisty
    $deployment = $null
    $mistyStartedThisRun = [pscustomobject][ordered]@{ body = $false; operator = $false; compose = $false }
    $unitEnablementChangedThisRun = [pscustomobject][ordered]@{ body = $false; operator = $false }
    $neuralStartSucceeded = $false
    $neuralStartedAt = [datetime]::MinValue
    $token = $null
    try {
        $State.install_phase = 'preparing_neuralcloud'
        Write-LauncherState -State $State -Path $Paths.state
        $neuralEnvironment = Initialize-NeuralCloudEnvironment -Root $Root -State $State -CommandInvoker $CommandInvoker

        $State.install_phase = 'deploying_mistyai'
        Write-LauncherState -State $State -Path $Paths.state
        $deployment = Deploy-MistyAIWorkspace -MistyAIPath $MistyAIPath -Distro $Distro -State $State -StagingDirectory $Paths.staging -CommandInvoker $CommandInvoker
        $mistyStartedThisRun.body = $initialMisty.body -in @('inactive', 'failed')
        $mistyStartedThisRun.operator = $initialMisty.operator -in @('inactive', 'failed')
        $mistyStartedThisRun.compose = $initialMisty.compose -eq 'stopped'
        $unitEnablementChangedThisRun.body = $initialMisty.body_enabled -ne 'enabled'
        $unitEnablementChangedThisRun.operator = $initialMisty.operator_enabled -ne 'enabled'
        $State.mistyai_deployment_fingerprint = $deployment.fingerprint
        $State.mistyai_previous_available = $deployment.previous_available
        $State.owned.mistyai = [pscustomobject][ordered]@{
            body = $priorMistyOwned.body -or $mistyStartedThisRun.body
            operator = $priorMistyOwned.operator -or $mistyStartedThisRun.operator
            compose = $priorMistyOwned.compose -or $mistyStartedThisRun.compose
        }
        $State.mistyai_unit_enablement_owned = [pscustomobject][ordered]@{
            body = $priorEnablementOwned.body -or $unitEnablementChangedThisRun.body
            operator = $priorEnablementOwned.operator -or $unitEnablementChangedThisRun.operator
        }
        $State.install_phase = 'starting_mistyai'
        Write-LauncherState -State $State -Path $Paths.state
        [void](Initialize-MistyAIConfiguration `
            -Distro $Distro `
            -DeploymentChanged $deployment.changed `
            -StartEnvironment `
            -PreserveBody:($initialMisty.body -eq 'active') `
            -PreserveOperator:($initialMisty.operator -eq 'active') `
            -PreserveCompose:($initialMisty.compose -eq 'running') `
            -CommandInvoker $CommandInvoker)
        $token = Read-MistyAIServiceToken -Distro $Distro -CommandInvoker $CommandInvoker
        $manifest = Join-Path $Root 'presets\runtime\shared-misty-v1.json'
        $mistyReady = Wait-MistyAIReady -Distro $Distro -Token $token -ManifestPath $manifest -CommandInvoker $CommandInvoker

        $beforeNeuralEntries = @(Get-NeuralCloudRegistryEntries -Root $Root)
        $neuralStartedAt = [datetime]::UtcNow
        $State.owned.neuralcloud = $true
        $State.install_phase = 'starting_neuralcloud'
        Write-LauncherState -State $State -Path $Paths.state
        $neuralStart = Start-NeuralCloud -Root $Root -Mode $Mode -Token $token -CommandInvoker $CommandInvoker
        $neuralStartSucceeded = $true
        $neuralReady = Wait-NeuralCloudReady -ManifestPath $manifest
        $neuralRegistry = Test-NeuralCloudRegistryHealth -Root $Root
        if (-not $neuralRegistry.running) {
            Throw-LauncherFailure -Message 'NeuralCloud readiness was reported, but the four launcher-owned processes and ports no longer match their PID registry.' -ExitCode $script:ExitNeuralCloud
        }

        $State.mistyai_deployment_fingerprint = $deployment.fingerprint
        $State.neuralcloud_root = $resolvedRoot
        $State.mistyai_source_path = $resolvedMistySource
        $State.mistyai_distro = $Distro
        $State.mistyai_previous_available = $deployment.previous_available
        $State.install_phase = 'running'
        $State.owned.neuralcloud = $true
        $State.owned.mistyai = [pscustomobject][ordered]@{
            body = $priorMistyOwned.body -or $mistyStartedThisRun.body
            operator = $priorMistyOwned.operator -or $mistyStartedThisRun.operator
            compose = $priorMistyOwned.compose -or $mistyStartedThisRun.compose
        }
        $State.mistyai_unit_enablement_owned = $priorEnablementOwned
        $State.last_command = 'up'
        $State.last_success_at = (Get-Date).ToUniversalTime().ToString('o')
        Write-LauncherState -State $State -Path $Paths.state
        if (-not $NoOpen -and -not $Json) {
            try { Start-Process 'http://127.0.0.1:8790' | Out-Null } catch { }
        }
        return [pscustomobject][ordered]@{
            status = 'running'
            exit_code = $script:ExitSuccess
            mode = $(if ($Mode) { $Mode } else { Get-LauncherObjectProperty $neuralReady 'mode' 'configured' })
            deployment_fingerprint = $deployment.fingerprint
            body_instance_id = $mistyReady.body_instance_id
            neuralcloud = $neuralRegistry
            runtime = Get-LauncherObjectProperty $neuralReady 'runtime'
            urls = [pscustomobject][ordered]@{
                gui = 'http://127.0.0.1:8790'
                operations = 'http://127.0.0.1:8790/ops'
                novnc = 'http://127.0.0.1:6080'
            }
            logs = Join-Path $Root 'logs'
            checks = $preflight.checks
        }
    } catch {
        $originalError = $_
        $deploymentRecoveryRequired = $originalError.Exception.Message -match 'deployment requires manual recovery'
        $cleanupErrors = New-Object System.Collections.Generic.List[string]
        if ($neuralStartSucceeded) {
            try {
                $neuralCleanup = Stop-LauncherNewNeuralCloudProcesses -Root $Root -BeforeEntries $beforeNeuralEntries -StartedAt $neuralStartedAt -CommandInvoker $CommandInvoker
            } catch {
                $neuralCleanup = [pscustomobject]@{ success = $false; output = $_.Exception.Message }
            }
            if ($neuralCleanup.success) {
                $State.owned.neuralcloud = $priorNeuralOwned
            } else {
                $State.owned.neuralcloud = $true
                [void]$cleanupErrors.Add("NeuralCloud cleanup failed: $($neuralCleanup.output)")
            }
        } else {
            $State.owned.neuralcloud = $priorNeuralOwned
        }
        $mistyCleanupSucceeded = $true
        if ($mistyStartedThisRun.body -or $mistyStartedThisRun.operator -or $mistyStartedThisRun.compose -or
            $unitEnablementChangedThisRun.body -or $unitEnablementChangedThisRun.operator) {
            $mistyCleanup = Stop-MistyAI -Distro $Distro -CommandInvoker $CommandInvoker `
                -StopBody:$mistyStartedThisRun.body `
                -StopOperator:$mistyStartedThisRun.operator `
                -DisableBody:$unitEnablementChangedThisRun.body `
                -DisableOperator:$unitEnablementChangedThisRun.operator `
                -StopCompose:$mistyStartedThisRun.compose `
                -ExpectedFingerprint ([string](Get-LauncherObjectProperty $deployment 'fingerprint' ''))
            $mistyCleanupSucceeded = $mistyCleanup.success
            if (-not $mistyCleanupSucceeded) {
                [void]$cleanupErrors.Add("MistyAI cleanup failed: $($mistyCleanup.service_error) $($mistyCleanup.environment_error)")
            }
        }
        if ($deployment -and $deployment.changed -and $deployment.previous_available) {
            $rollbackSucceeded = Restore-MistyAIDeployment -Distro $Distro -CommandInvoker $CommandInvoker `
                -RestartBody:($initialMisty.body -eq 'active') `
                -RestartOperator:($initialMisty.operator -eq 'active') `
                -RestartCompose:($initialMisty.compose -eq 'running') `
                -PreviousImageId ([string](Get-LauncherObjectProperty $deployment 'previous_image_id' ''))
            if ($rollbackSucceeded) {
                $State.mistyai_deployment_fingerprint = $recordedDeploymentFingerprint
                $State.mistyai_previous_available = $false
            } else {
                [void]$cleanupErrors.Add('MistyAI deployment rollback did not complete; inspect /opt/mistyai and /opt/mistyai.previous before retrying.')
            }
        }
        $State.owned.mistyai = if ($mistyCleanupSucceeded) {
            $priorMistyOwned
        } else {
            [pscustomobject][ordered]@{
                body = $priorMistyOwned.body -or $mistyStartedThisRun.body
                operator = $priorMistyOwned.operator -or $mistyStartedThisRun.operator
                compose = $priorMistyOwned.compose -or $mistyStartedThisRun.compose
            }
        }
        $State.mistyai_unit_enablement_owned = if ($mistyCleanupSucceeded) {
            $priorEnablementOwned
        } else {
            [pscustomobject][ordered]@{
                body = $priorEnablementOwned.body -or $unitEnablementChangedThisRun.body
                operator = $priorEnablementOwned.operator -or $unitEnablementChangedThisRun.operator
            }
        }
        $State.install_phase = if ($deploymentRecoveryRequired -or $cleanupErrors.Count -gt 0) { 'recovery_required' } else { 'failed' }
        try { Write-LauncherState -State $State -Path $Paths.state } catch {
            [void]$cleanupErrors.Add("Launcher state update failed: $($_.Exception.Message)")
        }
        if ($cleanupErrors.Count -gt 0) {
            $originalCode = Get-LauncherFailureCode -ErrorRecord $originalError -Default $script:ExitConfiguration
            $originalMessage = Protect-LauncherText -Text $originalError.Exception.Message -Secrets @($token)
            $cleanupMessage = Protect-LauncherText -Text ($cleanupErrors -join '; ') -Secrets @($token)
            Throw-LauncherFailure -Message "$originalMessage Cleanup/rollback also needs attention: $cleanupMessage" -ExitCode $originalCode
        }
        throw $originalError
    } finally {
        $token = $null
        [GC]::Collect()
    }
}

function Invoke-LauncherDownCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][object]$State,
        [Parameter(Mandatory = $true)][object]$Paths,
        [scriptblock]$CommandInvoker
    )
    $owned = Get-LauncherObjectProperty $State 'owned'
    $neuralOwned = [bool](Get-LauncherObjectProperty $owned 'neuralcloud' $false)
    $mistyOwnedState = Get-LauncherObjectProperty $owned 'mistyai'
    $mistyBodyOwned = [bool](Get-LauncherObjectProperty $mistyOwnedState 'body' $false)
    $mistyOperatorOwned = [bool](Get-LauncherObjectProperty $mistyOwnedState 'operator' $false)
    $mistyComposeOwned = [bool](Get-LauncherObjectProperty $mistyOwnedState 'compose' $false)
    $enablementOwnedState = Get-LauncherObjectProperty $State 'mistyai_unit_enablement_owned'
    $mistyBodyEnablementOwned = [bool](Get-LauncherObjectProperty $enablementOwnedState 'body' $false)
    $mistyOperatorEnablementOwned = [bool](Get-LauncherObjectProperty $enablementOwnedState 'operator' $false)
    $recordedRoot = [string](Get-LauncherObjectProperty $State 'neuralcloud_root' '')
    $recordedDistro = [string](Get-LauncherObjectProperty $State 'mistyai_distro' '')
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if ($neuralOwned -and $recordedRoot -ne $resolvedRoot) {
        Throw-LauncherFailure -Message "Refusing to stop launcher-owned NeuralCloud processes from a different checkout (recorded root: $recordedRoot)." -ExitCode $script:ExitConfiguration
    }
    if (($mistyBodyOwned -or $mistyOperatorOwned -or $mistyComposeOwned -or
        $mistyBodyEnablementOwned -or $mistyOperatorEnablementOwned) -and $recordedDistro -ne $Distro) {
        Throw-LauncherFailure -Message "Refusing to stop launcher-owned MistyAI components from a different WSL distribution (recorded distro: $recordedDistro)." -ExitCode $script:ExitConfiguration
    }

    $neural = if ($neuralOwned) {
        Stop-NeuralCloud -Root $Root -CommandInvoker $CommandInvoker
    } else {
        [pscustomobject][ordered]@{ success = $true; skipped = $true; reason = 'not_owned'; output = '' }
    }
    if (-not $neural.success) {
        $State.install_phase = 'stop_failed'
        $State.last_command = 'down'
        Write-LauncherState -State $State -Path $Paths.state
        Throw-LauncherFailure -Message "NeuralCloud could not be stopped safely: $($neural.output)" -ExitCode $script:ExitNeuralCloud
    }
    if ($neuralOwned) { $State.owned.neuralcloud = $false }

    $misty = Stop-MistyAI -Distro $Distro -CommandInvoker $CommandInvoker `
        -StopBody:$mistyBodyOwned `
        -StopOperator:$mistyOperatorOwned `
        -DisableBody:$mistyBodyEnablementOwned `
        -DisableOperator:$mistyOperatorEnablementOwned `
        -StopCompose:$mistyComposeOwned `
        -ExpectedFingerprint ([string](Get-LauncherObjectProperty $State 'mistyai_deployment_fingerprint' ''))
    $State.install_phase = if ($misty.success) { 'ready' } else { 'stop_failed' }
    if ($misty.success) {
        $State.owned.mistyai = [pscustomobject][ordered]@{ body = $false; operator = $false; compose = $false }
        $State.mistyai_unit_enablement_owned = [pscustomobject][ordered]@{ body = $false; operator = $false }
    }
    $State.last_command = 'down'
    if ($misty.success) { $State.last_success_at = (Get-Date).ToUniversalTime().ToString('o') }
    Write-LauncherState -State $State -Path $Paths.state
    if (-not $misty.success) {
        Throw-LauncherFailure -Message "MistyAI could not be stopped safely: $($misty.service_error) $($misty.environment_error)" -ExitCode $script:ExitMistyAI
    }
    return [pscustomobject][ordered]@{
        status = 'stopped'
        exit_code = $script:ExitSuccess
        neuralcloud = $neural
        mistyai = $misty
    }
}

function Get-LauncherLogTail {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Lines = 200,
        [AllowNull()][string[]]$SensitiveValues = @()
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    return Protect-LauncherText `
        -Text ((Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue) -join [Environment]::NewLine) `
        -Secrets $SensitiveValues
}

function Invoke-LauncherLogsCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Distro,
        [Parameter(Mandatory = $true)][string]$Service,
        [scriptblock]$CommandInvoker
    )
    $valid = @('all', 'neuralcloud', 'mistyai', 'cognitive', 'homeostasis', 'runtime', 'gui', 'body', 'operator', 'desktop', 'agent', 'loopback-gateway')
    if ($Service -notin $valid) {
        Throw-LauncherFailure -Message "Unknown log service '$Service'. Valid values: $($valid -join ', ')." -ExitCode $script:ExitConfiguration
    }
    $token = $null
    try { $token = Read-MistyAIServiceToken -Distro $Distro -CommandInvoker $CommandInvoker } catch { $token = $null }
    $sensitiveValues = if ($token) { @($token) } else { @() }
    $logs = New-Object System.Collections.Generic.List[object]
    $neuralNames = if ($Service -in @('all', 'neuralcloud')) { @('cognitive', 'homeostasis', 'runtime', 'gui') } elseif ($Service -in @('cognitive', 'homeostasis', 'runtime', 'gui')) { @($Service) } else { @() }
    foreach ($name in $neuralNames) {
        foreach ($stream in @('out', 'err')) {
            $path = Join-Path $Root "logs\$name.$stream.log"
            [void]$logs.Add([pscustomobject][ordered]@{ service = $name; stream = $stream; source = $path; text = Get-LauncherLogTail -Path $path -SensitiveValues $sensitiveValues })
        }
    }
    if ($Service -in @('all', 'mistyai', 'body', 'operator', 'desktop', 'agent', 'loopback-gateway')) {
        if ($Service -in @('body', 'operator')) {
            $unit = if ($Service -eq 'body') { 'mistyai-body.service' } else { 'mistyai-operator.service' }
            $script = "journalctl -u $(Quote-LauncherBashLiteral $unit) -n 200 --no-pager -o short-iso"
            $source = $unit
        } elseif ($Service -in @('desktop', 'agent', 'loopback-gateway')) {
            $script = "docker compose -f /opt/mistyai/deploy/compose.yaml logs --no-color --tail 200 $(Quote-LauncherBashLiteral $Service)"
            $source = "compose:$Service"
        } else {
            $script = "set -euo pipefail; test -x /opt/mistyai/.venv/bin/mistyctl; cd /opt/mistyai; .venv/bin/mistyctl env logs --tail 200; journalctl -u mistyai-body.service -u mistyai-operator.service -n 200 --no-pager -o short-iso"
            $source = 'MistyAI compose and systemd'
        }
        $result = Invoke-LauncherWslCommand -Distro $Distro -User 'root' -Script $script -Description "read $Service MistyAI logs" -CommandInvoker $CommandInvoker
        if ($result.exit_code -ne 0) {
            $detail = Protect-LauncherText -Text (($result.stderr + "`n" + $result.stdout).Trim()) -Secrets $sensitiveValues
            if ([string]::IsNullOrWhiteSpace($detail)) { $detail = "WSL command exited with code $($result.exit_code)." }
            $token = $null
            Throw-LauncherFailure -Message "Unable to read $Service MistyAI logs: $detail" -ExitCode $script:ExitMistyAI
        }
        [void]$logs.Add([pscustomobject][ordered]@{ service = 'mistyai'; stream = 'combined'; source = $source; text = Protect-LauncherText -Text (($result.stdout + "`n" + $result.stderr).Trim()) -Secrets $sensitiveValues })
    }
    $token = $null
    return [pscustomobject][ordered]@{ status = 'ok'; exit_code = $script:ExitSuccess; service = $Service; logs = $logs.ToArray() }
}

function Write-LauncherResult {
    param(
        [Parameter(Mandatory = $true)][object]$Result,
        [switch]$Json
    )
    if ($Json) {
        $safeJson = Protect-LauncherText ($Result | ConvertTo-Json -Depth 20)
        [Console]::Out.WriteLine($safeJson)
        return
    }
    $status = [string](Get-LauncherObjectProperty $Result 'status' 'unknown')
    $command = [string](Get-LauncherObjectProperty $Result 'command' '')
    Write-Host "NeuralCloud launcher $command`: $status"
    $errorText = Get-LauncherObjectProperty $Result 'error'
    if ($errorText) { Write-Host ("  " + (Protect-LauncherText ([string]$errorText))) }
    foreach ($check in @((Get-LauncherObjectProperty $Result 'checks' @()))) {
        $marker = if ($check.status -eq 'pass') { '[OK]' } else { "[$($check.status.ToUpperInvariant())]" }
        Write-Host "  $marker $($check.id): $($check.current)"
        if ($check.status -ne 'pass' -and $check.remediation) { Write-Host "       $($check.remediation)" }
    }
    foreach ($action in @((Get-LauncherObjectProperty $Result 'actions' @()))) {
        Write-Host "  [$($action.status)] $($action.id): $($action.detail)"
    }
    $urls = Get-LauncherObjectProperty $Result 'urls'
    if ($urls) {
        Write-Host "  GUI:   $($urls.gui)"
        Write-Host "  Ops:   $($urls.operations)"
        Write-Host "  noVNC: $($urls.novnc)"
    }
    $fingerprint = [string](Get-LauncherObjectProperty $Result 'deployment_fingerprint' '')
    if (-not [string]::IsNullOrWhiteSpace($fingerprint)) {
        Write-Host "  Deployment fingerprint: $fingerprint"
    }
    $neural = Get-LauncherObjectProperty $Result 'neuralcloud'
    if ($neural) {
        $pidParts = New-Object System.Collections.Generic.List[string]
        foreach ($serviceEntry in @((Get-LauncherObjectProperty $neural 'services' @()))) {
            $serviceName = [string](Get-LauncherObjectProperty $serviceEntry 'name' '')
            $servicePid = [int](Get-LauncherObjectProperty $serviceEntry 'pid' 0)
            if (-not [string]::IsNullOrWhiteSpace($serviceName) -and $servicePid -gt 0) {
                [void]$pidParts.Add("$serviceName=$servicePid")
            }
        }
        if ($pidParts.Count -gt 0) {
            Write-Host "  NeuralCloud PIDs: $($pidParts -join ', ')"
        }
    }
    $logValue = Get-LauncherObjectProperty $Result 'logs'
    if ($null -eq $logValue) {
        return
    } elseif ($logValue -is [string]) {
        Write-Host "  Logs:  $logValue"
    } else {
        foreach ($entry in @($logValue | Where-Object { $null -ne $_ })) {
            Write-Host "`n--- $($entry.service) $($entry.stream) ($($entry.source)) ---"
            if ($entry.text) { Write-Host $entry.text } else { Write-Host '(no log output)' }
        }
    }
}

function Invoke-NeuralCloudLauncher {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('up', 'down', 'restart', 'status', 'doctor', 'install', 'logs')]
        [string]$Command,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$MistyAIPath,
        [Parameter(Mandatory = $true)][string]$Distro,
        [AllowEmptyString()][ValidateSet('', 'demo', 'live', 'ollama')][string]$Mode = '',
        [switch]$NonInteractive,
        [switch]$NoOpen,
        [switch]$Json,
        [string]$Service = 'all',
        [bool]$AutoStartDocker = $true,
        [ValidateRange(1, 600)][int]$DockerStartupTimeoutSeconds = 120,
        [scriptblock]$CommandInvoker
    )
    $result = $null
    $lockHandle = $null
    $previousJsonOutput = $script:LauncherJsonOutput
    $previousRootOperations = $script:ReportedRootOperations
    $script:LauncherJsonOutput = [bool]$Json
    $script:ReportedRootOperations = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    try {
        $resolvedRoot = [System.IO.Path]::GetFullPath($Root)
        $resolvedMistyAIPath = [System.IO.Path]::GetFullPath($MistyAIPath)
        $paths = Get-LauncherDataPaths
        if ($Command -in @('up', 'down', 'restart', 'install')) {
            $lockHandle = Enter-LauncherLock -Path $paths.lock
            if ($null -eq $lockHandle) {
                Throw-LauncherFailure -Message 'Another NeuralCloud launcher operation is already in progress.' -ExitCode $script:ExitLock
            }
        }
        $state = Read-LauncherState -Path $paths.state
        switch ($Command) {
            'doctor' {
                $payload = Invoke-LauncherDoctorCommand -Root $resolvedRoot -MistyAIPath $resolvedMistyAIPath -Distro $Distro -CommandInvoker $CommandInvoker
            }
            'status' {
                $payload = Invoke-LauncherStatusCommand -Root $resolvedRoot -Distro $Distro -CommandInvoker $CommandInvoker
            }
            'logs' {
                $payload = Invoke-LauncherLogsCommand -Root $resolvedRoot -Distro $Distro -Service $Service -CommandInvoker $CommandInvoker
            }
            'install' {
                $payload = Invoke-LauncherInstallCommand -Root $resolvedRoot -MistyAIPath $resolvedMistyAIPath -Distro $Distro -State $state -Paths $paths -NonInteractive:$NonInteractive -Json:$Json -CommandInvoker $CommandInvoker
            }
            'down' {
                $payload = Invoke-LauncherDownCommand -Root $resolvedRoot -Distro $Distro -State $state -Paths $paths -CommandInvoker $CommandInvoker
            }
            'restart' {
                [void](Invoke-LauncherDownCommand -Root $resolvedRoot -Distro $Distro -State $state -Paths $paths -CommandInvoker $CommandInvoker)
                $payload = Invoke-LauncherUpCommand -Root $resolvedRoot -MistyAIPath $resolvedMistyAIPath -Distro $Distro -Mode $Mode -State $state -Paths $paths -NonInteractive:$NonInteractive -NoOpen:$NoOpen -Json:$Json -AutoStartDocker:$AutoStartDocker -DockerStartupTimeoutSeconds $DockerStartupTimeoutSeconds -CommandInvoker $CommandInvoker
            }
            default {
                $payload = Invoke-LauncherUpCommand -Root $resolvedRoot -MistyAIPath $resolvedMistyAIPath -Distro $Distro -Mode $Mode -State $state -Paths $paths -NonInteractive:$NonInteractive -NoOpen:$NoOpen -Json:$Json -AutoStartDocker:$AutoStartDocker -DockerStartupTimeoutSeconds $DockerStartupTimeoutSeconds -CommandInvoker $CommandInvoker
            }
        }
        $properties = [ordered]@{
            schema_version = $script:LauncherSchemaVersion
            command = $Command
        }
        foreach ($property in $payload.PSObject.Properties) { $properties[$property.Name] = $property.Value }
        $result = [pscustomobject]$properties
    } catch {
        $exitCode = Get-LauncherFailureCode -ErrorRecord $_ -Default $script:ExitConfiguration
        $details = $_.Exception.Data['LauncherDetails']
        $properties = [ordered]@{
            schema_version = $script:LauncherSchemaVersion
            command = $Command
            status = if ($exitCode -eq $script:ExitNeedsAction) { 'action_required' } else { 'failed' }
            exit_code = $exitCode
            error = Protect-LauncherText $_.Exception.Message
        }
        if ($details) {
            if ($details.PSObject.Properties['checks']) {
                $properties['checks'] = $details.checks
                $properties['actions'] = $details.actions
            } elseif ($details -is [System.Collections.IEnumerable] -and $details -isnot [string]) {
                $properties['checks'] = @($details)
            } else {
                $properties['details'] = $details
            }
        }
        $result = [pscustomobject]$properties
    } finally {
        if ($lockHandle) { $lockHandle.Dispose() }
        $script:LauncherJsonOutput = $previousJsonOutput
        $script:ReportedRootOperations = $previousRootOperations
    }
    Write-LauncherResult -Result $result -Json:$Json
    return [int](Get-LauncherObjectProperty $result 'exit_code' $script:ExitConfiguration)
}

Export-ModuleMember -Function @(
    'Invoke-NeuralCloudLauncher',
    'New-LauncherCheck',
    'Test-SensitiveRelativePath',
    'Protect-LauncherText',
    'Get-WorkspaceFingerprint',
    'Test-LauncherVersion',
    'ConvertTo-WslPath',
    'Invoke-LauncherWslCommand',
    'Invoke-LauncherExternalCommand',
    'Test-NeuralCloudRegistryHealth',
    'Get-LauncherPortOwner',
    'Test-LauncherPathHasReparsePoint'
)
