param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\NeuralCloud\llama.cpp",
    [int]$ContextSize = 131072,
    [int]$Workers = 16
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $sourceRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'NeuralCloud Python environment is missing.' }
if (!(Test-Path -LiteralPath (Join-Path $InstallRoot 'bin\llama-server.exe'))) { throw 'Install the verified llama.cpp CUDA runtime first.' }
Set-Location -LiteralPath $sourceRoot
$model = Join-Path $InstallRoot 'models\Qwen3.8-27B-Q4_K_M.gguf'
& $python -u (Join-Path $PSScriptRoot 'download_gguf.py') `
    --url 'https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF/resolve/71bc7b627595dc8a91039addd9c791ae548d6747/Qwen3.8-27B-Q4_K_M.gguf' `
    --output $model --size 18973870528 `
    --sha256 'c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747' --workers $Workers
if ($LASTEXITCODE -ne 0) { throw 'Model download or checksum verification failed.' }
& $python -u (Join-Path $PSScriptRoot 'download_gguf.py') `
    --url 'https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF/resolve/71bc7b627595dc8a91039addd9c791ae548d6747/mmproj-Qwen3.8-27B-Q8_0.gguf' `
    --output (Join-Path $InstallRoot 'models\mmproj-Qwen3.8-27B-Q8_0.gguf') --size 629247008 `
    --sha256 '2e968a6af97ce35d8971890b257b9b7edabf20ad91450501fa53162a19ee33eb' --workers 5
if ($LASTEXITCODE -ne 0) { throw 'Vision projector download or checksum verification failed.' }
$metadataPath = Join-Path $InstallRoot 'deployment.json'
if (Test-Path -LiteralPath $metadataPath) {
    $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    $metadata | Add-Member -NotePropertyName download_verified -NotePropertyValue $true -Force
    $metadata | Add-Member -NotePropertyName context_size -NotePropertyValue $ContextSize -Force
    $metadata | Add-Member -NotePropertyName parallel -NotePropertyValue 2 -Force
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
}
& (Join-Path $PSScriptRoot 'llama-local.ps1') start -InstallRoot $InstallRoot -ContextSize $ContextSize
$ready = $false
$deadline = (Get-Date).AddMinutes(10)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8080/health' -TimeoutSec 5
        if ($health.status -eq 'ok') { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 5
}
if (!$ready) { throw "Model server was not ready within 10 minutes. Check $InstallRoot\logs." }
& $python -u -m tools.deploy.verify_llama_local `
    --key-file (Join-Path $InstallRoot 'api-key.txt') --out (Join-Path $InstallRoot 'verification.json')
$verificationExit = $LASTEXITCODE
$report = Get-Content -LiteralPath (Join-Path $InstallRoot 'verification.json') -Raw | ConvertFrom-Json
if (Test-Path -LiteralPath $metadataPath) {
    $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    $metadata | Add-Member -NotePropertyName isolated_model_probes_passed -NotePropertyValue $report.all_probes_passed -Force
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
}
if ($verificationExit -ne 0 -or !$report.all_probes_passed) { throw 'One or more real-model probes failed; inspect verification.json.' }
Write-Output 'Download, checksum, server startup, and isolated real-model probes completed.'
