#Requires -Version 5.1
Set-StrictMode -Version Latest
$script:CoreLauncher = Import-Module (Join-Path $PSScriptRoot 'Launcher.psm1') -PassThru
$script:PathBindings = @(
    'NC_HOMEOSTASIS_STATE_DIR', 'NC_GUI_CONFIG', 'NC_QQ_SESSION_CONFIG', 'NC_QQ_WATCHER_CONFIG',
    'NC_QQ_DIRECT_CONFIG', 'NC_SHARED_MEMORY_DB', 'NC_CONTROL_TOKEN_FILE', 'NC_SERVICE_TOKEN_FILE',
    'NC_GUI_PRESETS', 'NC_RUNTIME_STATE_DIR', 'NC_BOARD_CONFIG', 'NC_VOICE_CONFIG', 'NC_WEB_CONFIG',
    'NC_LLM_FALLBACK_CONFIG'
)

function Get-OneClickField($Object, [string]$Name, $Default = $null) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

function Assert-OneClickFields($Object, [string[]]$Allowed, [string]$Name) {
    if ($Object -isnot [pscustomobject]) { throw "$Name must be a JSON object." }
    foreach ($property in $Object.PSObject.Properties) {
        if ($property.Name -cnotin $Allowed) { throw "Unknown field in ${Name}: $($property.Name)" }
    }
}

function Resolve-OneClickPath([string]$Root, $Value, [string]$Name) {
    if ($Value -isnot [string] -or -not $Value.Trim() -or $Value -match '[\r\n\x00]') {
        throw "$Name must be a nonempty file or directory path."
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if ($expanded -match '%[^%]+%') { throw "$Name contains an undefined environment variable." }
    if (-not [IO.Path]::IsPathRooted($expanded)) { $expanded = Join-Path $Root $expanded }
    return [IO.Path]::GetFullPath($expanded)
}

function Read-OneClickConfiguration([string]$Root, [string]$ConfigPath, [string]$Profile) {
    $configFile = Resolve-OneClickPath $Root $ConfigPath 'ConfigPath'
    if (-not (Test-Path -LiteralPath $configFile -PathType Leaf) -or (Get-Item -LiteralPath $configFile).Length -gt 65536) {
        throw 'Launch configuration must be an existing JSON file under 64 KiB.'
    }
    try { $config = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json } catch { throw 'Launch configuration is not valid JSON.' }
    Assert-OneClickFields $config @('schema_version', 'profile', 'open_browser', 'prepare_environment', 'profiles') 'configuration'
    if (($config.schema_version -isnot [int] -and $config.schema_version -isnot [long]) -or $config.schema_version -ne 1) { throw 'schema_version must be 1.' }
    foreach ($flag in @('open_browser', 'prepare_environment')) {
        if ((Get-OneClickField $config $flag) -isnot [bool]) { throw "$flag must be true or false." }
    }
    if (-not $Profile) { $Profile = Get-OneClickField $config 'profile' }
    if ($Profile -notmatch '^[a-zA-Z][a-zA-Z0-9_-]{0,63}$') { throw 'A valid profile name is required.' }
    if ($config.profiles -isnot [pscustomobject]) { throw 'profiles must be a JSON object.' }
    $selected = Get-OneClickField $config.profiles $Profile
    Assert-OneClickFields $selected @('mode', 'development', 'state_root', 'manifest_path', 'profile_path', 'embodiment_provider',
        'mistyai_base_url', 'mistyai_token_file', 'startup_timeout_seconds', 'ports', 'bindings', 'mistyai') 'profile'
    if ($selected.mode -cnotin @('configured', 'demo', 'live', 'ollama')) { throw 'mode must be configured, demo, live, or ollama.' }
    if ($selected.development -isnot [bool]) { throw 'development must be true or false.' }
    $startupTimeout = Get-OneClickField $selected 'startup_timeout_seconds' 300
    if (($startupTimeout -isnot [int] -and $startupTimeout -isnot [long]) -or $startupTimeout -lt 10 -or $startupTimeout -gt 1800) {
        throw 'startup_timeout_seconds must be an integer from 10 to 1800.'
    }
    if ($selected.embodiment_provider -cnotin @('auto', 'none', 'simulator', 'mistyai')) { throw 'Invalid embodiment_provider.' }
    $stateRoot = Get-OneClickField $selected 'state_root'
    if ($selected.development) {
        $stateRoot = Resolve-OneClickPath $Root $stateRoot 'state_root'
        $normalizedState = $stateRoot.TrimEnd('\', '/')
        $normalizedRoot = $Root.TrimEnd('\', '/')
        if ($normalizedState -eq $normalizedRoot -or $normalizedState -eq (Join-Path $Root 'data') -or
            $normalizedRoot.StartsWith($normalizedState + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Development state_root must be separate from the repository root and shared data.'
        }
        $sharedDirectories = @('data/cognitive', 'data/homeostasis', 'data/runtime', 'data/presets') |
            ForEach-Object { [IO.Path]::GetFullPath((Join-Path $Root $_)).TrimEnd('\', '/') }
        foreach ($other in $config.profiles.PSObject.Properties) {
            if ((Get-OneClickField $other.Value 'development') -eq $false) {
                $otherBindings = Get-OneClickField $other.Value 'bindings'
                foreach ($name in @('NC_HOMEOSTASIS_STATE_DIR', 'NC_RUNTIME_STATE_DIR', 'NC_GUI_PRESETS')) {
                    $value = Get-OneClickField $otherBindings $name
                    if ($value) { $sharedDirectories += (Resolve-OneClickPath $Root $value $name).TrimEnd('\', '/') }
                }
            }
        }
        foreach ($shared in $sharedDirectories) {
            if ($normalizedState -eq $shared -or $normalizedState.StartsWith($shared + '\', [StringComparison]::OrdinalIgnoreCase) -or
                $shared.StartsWith($normalizedState + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Development state_root overlaps a shared state directory.'
            }
        }
        $cursor = $normalizedState
        while ($cursor) {
            if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Development state_root must not traverse a symbolic link or junction.'
            }
            $cursor = [IO.Path]::GetDirectoryName($cursor)
        }
    } elseif ($null -ne $stateRoot) { throw 'Shared profiles must use state_root: null to preserve their existing history.' }
    $manifest = Resolve-OneClickPath $Root $selected.manifest_path 'manifest_path'
    $homeostasis = Resolve-OneClickPath $Root $selected.profile_path 'profile_path'
    foreach ($path in @($manifest, $homeostasis)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Configuration file does not exist: $path" }
    }
    try { $manifestConfig = [IO.File]::ReadAllText($manifest) | ConvertFrom-Json } catch { throw 'manifest_path is not valid JSON.' }
    $expectedControl = if ($selected.development) { 'development_debug' } else { 'study_runtime' }
    if ((Get-OneClickField $manifestConfig 'control_mode') -ne $expectedControl) { throw 'Manifest control_mode does not match development.' }
    Assert-OneClickFields $selected.ports @('gui', 'runtime', 'cognitive', 'homeostasis') 'ports'
    $ports = @{}
    foreach ($name in @('gui', 'runtime', 'cognitive', 'homeostasis')) {
        $port = Get-OneClickField $selected.ports $name
        if (($port -isnot [int] -and $port -isnot [long]) -or $port -lt 1 -or $port -gt 65535) { throw "ports.$name must be an integer from 1 to 65535." }
        $ports[$name] = [int]$port
    }
    if (@($ports.Values | Select-Object -Unique).Count -ne 4) { throw 'All four ports must be unique.' }
    $numericBindings = @('NC_QQ_INPUT_TOKEN_BUDGET', 'NC_QQ_MEMORY_RECALL_MAX_CHARACTERS')
    Assert-OneClickFields $selected.bindings ($script:PathBindings + $numericBindings) 'bindings'
    $bindings = @{}
    foreach ($property in $selected.bindings.PSObject.Properties) {
        if ($null -eq $property.Value) { continue }
        if ($property.Name -in $numericBindings) {
            $minimum = if ($property.Name -eq 'NC_QQ_INPUT_TOKEN_BUDGET') { 8192 } else { 1 }
            if (($property.Value -isnot [int] -and $property.Value -isnot [long]) -or $property.Value -lt $minimum -or $property.Value -gt [int]::MaxValue) {
                throw "$($property.Name) must be an integer from $minimum to 2147483647."
            }
            $bindings[$property.Name] = [string]$property.Value
        } else { $bindings[$property.Name] = Resolve-OneClickPath $Root $property.Value $property.Name }
    }
    if ($selected.development -and $bindings.Count -gt 0) { throw 'Isolated development profiles must use empty bindings.' }
    $mode = $selected.mode
    if ($mode -eq 'configured') {
        if (-not $bindings.ContainsKey('NC_GUI_CONFIG') -or -not (Test-Path -LiteralPath $bindings.NC_GUI_CONFIG -PathType Leaf)) {
            throw 'Configured mode requires bindings.NC_GUI_CONFIG pointing to the existing model configuration.'
        }
        try { $modelConfig = [IO.File]::ReadAllText($bindings.NC_GUI_CONFIG) | ConvertFrom-Json } catch { throw 'The model configuration is not valid JSON.' }
        if ((Get-OneClickField $modelConfig 'mode') -cnotin @('demo', 'live', 'ollama')) { throw 'Saved model mode is invalid.' }
    }
    foreach ($name in @('NC_GUI_CONFIG', 'NC_QQ_SESSION_CONFIG', 'NC_QQ_WATCHER_CONFIG', 'NC_QQ_DIRECT_CONFIG', 'NC_BOARD_CONFIG', 'NC_VOICE_CONFIG', 'NC_WEB_CONFIG', 'NC_LLM_FALLBACK_CONFIG')) {
        if ($bindings.ContainsKey($name) -and -not (Test-Path -LiteralPath $bindings[$name] -PathType Leaf)) { throw "Bound configuration is missing: $name" }
    }
    $tokenFile = Get-OneClickField $selected 'mistyai_token_file'
    if ($null -ne $tokenFile) { $tokenFile = Resolve-OneClickPath $Root $tokenFile 'mistyai_token_file' }
    $baseUrl = Get-OneClickField $selected 'mistyai_base_url' 'http://127.0.0.1:8787'
    $uri = $null
    if ($baseUrl -isnot [string] -or -not [uri]::TryCreate($baseUrl, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -notin @('http', 'https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
        throw 'mistyai_base_url must be an HTTP(S) origin without credentials.'
    }
    Assert-OneClickFields $selected.mistyai @('enabled', 'source_path', 'distro') 'mistyai'
    if ($selected.mistyai.enabled -isnot [bool]) { throw 'mistyai.enabled must be true or false.' }
    $sourcePath = Resolve-OneClickPath $Root $selected.mistyai.source_path 'mistyai.source_path'
    if ($selected.mistyai.distro -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid mistyai.distro.' }
    if ($selected.mistyai.enabled) {
        if ($selected.development -or $manifest -ne (Join-Path $Root 'presets\runtime\shared-misty-v1.json') -or
            $homeostasis -ne (Join-Path $Root 'presets\homeostasis\qq-social-drive-v1.json') -or
            $ports.gui -ne 8790 -or $ports.runtime -ne 8770 -or $ports.cognitive -ne 8771 -or $ports.homeostasis -ne 8772 -or
            $selected.embodiment_provider -ne 'auto' -or $baseUrl -ne 'http://127.0.0.1:8787' -or $tokenFile) {
            throw 'Managed MistyAI requires shared-misty-v1.json, qq-social-drive-v1.json, standard ports 8790/8770/8771/8772, auto provider and local credentials. Other profiles can connect to an already running body service with mistyai.enabled=false.'
        }
    }
    $registryPath = if ($stateRoot) { Join-Path $stateRoot '.neuralcloud.pids.json' } else { Join-Path $Root '.neuralcloud.pids.json' }
    $result = [pscustomobject]@{
        root = $Root; config_path = $configFile; profile = $Profile; mode = $mode; development = $selected.development
        state_root = $stateRoot; manifest_path = $manifest; profile_path = $homeostasis; ports = $ports
        bindings = $bindings; embodiment_provider = $selected.embodiment_provider
        mistyai_base_url = $baseUrl; mistyai_token_file = $tokenFile; registry_path = $registryPath
        mistyai = [pscustomobject]@{enabled=$selected.mistyai.enabled;source_path=$sourcePath;distro=$selected.mistyai.distro}
        open_browser = $config.open_browser; prepare_environment = $config.prepare_environment
        runtime_lifecycle = $null
        startup_timeout_seconds = [int]$startupTimeout
    }
    return $result
}

function Get-OneClickFileHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '') }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Get-OneClickFingerprint($Settings) {
    $payload = [ordered]@{}
    foreach ($name in @('mode','development','state_root','manifest_path','profile_path',
        'embodiment_provider','mistyai_base_url','mistyai_token_file')) { $payload[$name] = $Settings.$name }
    foreach ($name in @('ports', 'bindings')) {
        foreach ($key in @($Settings.$name.Keys | Sort-Object)) { $payload["$name/$key"] = $Settings.$name[$key] }
    }
    foreach ($name in @('enabled','source_path','distro')) { $payload["mistyai/$name"] = $Settings.mistyai.$name }
    $payload.manifest_hash = Get-OneClickFileHash $Settings.manifest_path
    $payload.profile_hash = Get-OneClickFileHash $Settings.profile_path
    # Hex-encoded scalar records have identical bytes on PS 5.1 and 7;
    # ConvertTo-Json differs in its escaping of apostrophes and HTML characters.
    $records = foreach ($key in $payload.Keys) {
        $value = $payload[$key]
        $encoded = if ($null -eq $value) { 'null' } else {
            [BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes([string]$value)).Replace('-', '')
        }
        "$key=$encoded"
    }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes(($records -join "`n")))).Replace('-', '') }
    finally { $hash.Dispose() }
}

function Get-OneClickHealth($Settings) {
    return & $script:CoreLauncher {
        param($Root, $Registry, $Ports)
        Test-NeuralCloudRegistryHealth -Root $Root -RegistryPath $Registry -Ports $Ports
    } $Settings.root $Settings.registry_path $Settings.ports
}

function Test-OneClickConfigurationMatch($Settings) {
    $stamp = $Settings.registry_path + '.launch.json'
    try {
        $saved = [IO.File]::ReadAllText($stamp) | ConvertFrom-Json
        return $saved.fingerprint -eq (Get-OneClickFingerprint $Settings) -and
            $saved.registry_sha256 -eq (Get-OneClickFileHash $Settings.registry_path)
    } catch { return $false }
}

function Write-OneClickStamp($Settings) {
    $stamp = $Settings.registry_path + '.launch.json'
    $temporary = $stamp + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $data = @{fingerprint=(Get-OneClickFingerprint $Settings);registry_sha256=(Get-OneClickFileHash $Settings.registry_path)} | ConvertTo-Json
        [IO.File]::WriteAllText($temporary, $data, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $stamp -Force
    } finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
}

function Get-OneClickEnvironment($Settings) {
    # The selected configuration owns startup bindings; stale shell variables
    # cannot silently attach an isolated profile to shared state or credentials.
    $environment = @{}
    foreach ($entry in (Get-ChildItem Env: | Where-Object {$_.Name -like 'NC_*'})) { $environment[$entry.Name] = $null }
    foreach ($name in $Settings.bindings.Keys) { $environment[$name] = $Settings.bindings[$name] }
    $environment.NC_IGNORE_SHARED_LAUNCH_DEFAULTS = '1'
    $environment.NC_STARTUP_TIMEOUT_SECONDS = [string]$Settings.startup_timeout_seconds
    return $environment
}

function Invoke-OneClickChild([string]$Root, [string]$Script, [string[]]$Arguments, [hashtable]$Environment = @{}) {
    $shell = (Get-Command powershell.exe -ErrorAction Stop).Source
    Write-Host "Running $Script ..."
    $result = Invoke-LauncherExternalCommand -FilePath $shell -ArgumentList (@('-NoLogo', '-NoProfile', '-ExecutionPolicy',
        'Bypass', '-File', (Join-Path $Root $Script)) + $Arguments) -WorkingDirectory $Root -Environment $Environment -FileBackedOutput
    $outputText = [string]$result.stdout
    $errorText = [string]$result.stderr
    if ($outputText.Trim()) { Write-Host (Protect-LauncherText $outputText.Trim()) }
    if ($result.exit_code -ne 0) {
        throw "$Script exited with code $($result.exit_code). $(Protect-LauncherText $errorText.Trim())"
    }
}

function Test-OneClickPython([string]$Root) {
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    $probe = Invoke-LauncherExternalCommand -FilePath $python -ArgumentList @('-c',
        'import fastapi, uvicorn, pydantic, pydantic_settings, httpx, sqlalchemy, pgvector, numpy, websockets; import harness.runtime.service, harness.cognitive.service, harness.homeostasis.service, gui.runtime_server') -WorkingDirectory $Root -TimeoutSeconds 30
    return $probe.exit_code -eq 0
}

function Initialize-OneClickPython($Settings) {
    if (Test-OneClickPython $Settings.root) { return }
    if (-not $Settings.prepare_environment) { throw 'Python environment is incomplete; enable prepare_environment or prepare .venv with GUI dependencies.' }
    Write-Host 'Preparing the NeuralCloud Python environment...'
    & $script:CoreLauncher {
        param($Root)
        $python = Get-LauncherPythonCommand
        if (-not $python) { throw 'Install Python 3.12, then run start.cmd again.' }
        $probe = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @('-m', 'uv', '--version')) -TimeoutSeconds 30
        if ($probe.exit_code -ne 0 -or -not (Test-LauncherVersion -Current $probe.stdout -Required $script:RequiredUvVersion -Exact)) {
            $install = Invoke-LauncherExternalCommand -FilePath $python.file -ArgumentList (@($python.prefix_arguments) + @('-m', 'pip', 'install', "uv==$script:RequiredUvVersion"))
            if ($install.exit_code -ne 0) { throw 'Could not prepare uv. Check the Python package download connection.' }
        }
        $state = [pscustomobject]@{neural_dependency_fingerprint=$null}
        Initialize-NeuralCloudEnvironment -Root $Root -State $state | Out-Null
    } $Settings.root
    if (-not (Test-OneClickPython $Settings.root)) { throw 'Python dependency preparation finished, but service imports still fail.' }
}

function Assert-OneClickReady($Settings) {
    if (-not (Get-OneClickHealth $Settings).running) { throw 'The four registered processes and listening ports are not healthy.' }
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$($Settings.ports.gui)/api/status" -TimeoutSec 10
    if ($status.runtime.lifecycle -notin @('RUNNING', 'PAUSED') -or -not $status.runtime.health.cognitive_healthy -or -not $status.runtime.health.homeostasis_healthy) {
        throw 'The GUI is reachable, but the runtime or domain services are not ready.'
    }
    $Settings.runtime_lifecycle = [string]$status.runtime.lifecycle
}

function Invoke-OneClickLaunch {
    [CmdletBinding()]
    param([string]$Root, [string]$ConfigPath, [string]$Profile, [string]$Command = 'up', [switch]$NoOpen)
    $settings = Read-OneClickConfiguration -Root $Root -ConfigPath $ConfigPath -Profile $Profile
    $url = "http://127.0.0.1:$($settings.ports.gui)"
    if ($Command -eq 'check') {
        if (-not (Test-OneClickPython $Root)) { throw 'Configuration is valid; Python dependencies need preparation on the next up.' }
        return [pscustomobject]@{status='configuration and Python dependencies ready';url=$url;config_path=$settings.config_path;profile=$settings.profile}
    }
    if ($Command -eq 'status') {
        $health = Get-OneClickHealth $settings
        $state = if ($health.running) { 'running' } else { 'stopped or incomplete' }
        if ($health.running) {
            Assert-OneClickReady $settings
            if ($settings.runtime_lifecycle -eq 'PAUSED') { $state = 'paused (four services ready)' }
        }
        if ($health.running -and -not (Test-OneClickConfigurationMatch $settings)) { $state = 'running; configuration differs (use restart)' }
        return [pscustomobject]@{status=$state;url=$url;config_path=$settings.config_path;profile=$settings.profile}
    }
    if ($Command -notin @('up', 'down', 'restart')) { throw 'Unknown launch command.' }
    $lockDir = Join-Path $Root '.launcher-staging'
    [void](New-Item -ItemType Directory -Path $lockDir -Force)
    $lock = $null
    try {
        try { $lock = [IO.File]::Open((Join-Path $lockDir 'oneclick.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { throw 'Another one-click startup or shutdown is already in progress. Wait for it to finish.' }
        $environment = Get-OneClickEnvironment $settings
        if ($settings.mistyai.enabled) {
            if ($Command -eq 'up' -and (Get-OneClickHealth $settings).running -and -not (Test-OneClickConfigurationMatch $settings)) {
                throw 'An existing instance is running with unverified or changed configuration. Use launch.ps1 restart to apply this configuration.'
            }
            $launchArguments = @($Command, '-MistyAIPath', $settings.mistyai.source_path, '-Distro', $settings.mistyai.distro, '-NonInteractive', '-NoOpen')
            if ($settings.mode -ne 'configured') { $launchArguments += @('-Mode', $settings.mode) }
            Invoke-OneClickChild $Root 'dev.ps1' $launchArguments $environment
            if ($Command -ne 'down') { Assert-OneClickReady $settings; Write-OneClickStamp $settings }
        } else {
            if ($Command -in @('down', 'restart')) {
                $launchArguments = @()
                if ($settings.state_root) { $launchArguments += @('-StateRoot', $settings.state_root) }
                Invoke-OneClickChild $Root 'stop.ps1' $launchArguments $environment
                Remove-Item -LiteralPath ($settings.registry_path + '.launch.json') -Force -ErrorAction SilentlyContinue
            }
            if ($Command -ne 'down') {
                $health = Get-OneClickHealth $settings
                if ($health.running) {
                    if (-not (Test-OneClickConfigurationMatch $settings)) { throw 'An existing instance is running with unverified or changed configuration. Use launch.ps1 restart to apply this configuration.' }
                    Assert-OneClickReady $settings
                    Write-Host 'The configured instance is already running; reusing its existing processes.'
                } else {
                    Initialize-OneClickPython $settings
                    if ($settings.development) {
                        [void](New-Item -ItemType Directory -Path $settings.state_root -Force)
                        $guiConfig = Join-Path $settings.state_root 'gui_settings.json'
                        if (-not (Test-Path -LiteralPath $guiConfig)) {
                            [IO.File]::WriteAllText($guiConfig, '{"mode":"demo"}', (New-Object Text.UTF8Encoding($false)))
                        }
                    }
                    $launchArguments = @('-IgnoreSharedLaunchDefaults', '-ManifestPath', $settings.manifest_path, '-ProfilePath', $settings.profile_path,
                        '-EmbodimentProvider', $settings.embodiment_provider, '-MistyAIBaseUrl', $settings.mistyai_base_url,
                        '-GuiPort', [string]$settings.ports.gui, '-RuntimePort', [string]$settings.ports.runtime,
                        '-CognitivePort', [string]$settings.ports.cognitive, '-HomeostasisPort', [string]$settings.ports.homeostasis)
                    if ($settings.mode -ne 'configured') { $launchArguments += @('-Mode', $settings.mode) }
                    if ($settings.mistyai_token_file) { $launchArguments += @('-MistyAITokenFile', $settings.mistyai_token_file) }
                    if ($settings.development) { $launchArguments += @('-Development', '-StateRoot', $settings.state_root) }
                    Invoke-OneClickChild $Root 'start.ps1' $launchArguments $environment
                    Assert-OneClickReady $settings
                    Write-OneClickStamp $settings
                }
            }
        }
        if ($Command -ne 'down' -and $settings.open_browser -and -not $NoOpen) { Start-Process $url | Out-Null }
        $state = if ($Command -eq 'down') { 'stopped' } elseif ($settings.runtime_lifecycle -eq 'PAUSED') {
            'paused (four services ready; inspect the runtime pause reason in the GUI)'
        } else { 'running' }
        return [pscustomobject]@{status=$state;url=$(if($Command -eq 'down'){''}else{$url});config_path=$settings.config_path;profile=$settings.profile}
    } finally { if ($null -ne $lock) { $lock.Dispose() } }
}

Export-ModuleMember -Function Read-OneClickConfiguration, Invoke-OneClickLaunch
