# Deletes only the 665 individually inventoried historical pytest output files.
# Without -Execute, validates every existing target and makes no changes.
[CmdletBinding()]
param([switch]$Execute)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$manifestPath = Join-Path $workspaceRoot 'docs\refactor\cleanup-safe-generated-20261116.json'
$expectedManifestHash = '573A11884BED03002C15F525F2724C05E527AFBA13386299392F88F29771000B'
if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash -ne $expectedManifestHash) {
    throw 'Manifest changed; review the file list before updating this script.'
}
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.file_count -ne 665 -or @($manifest.files).Count -ne 665) {
    throw 'Unexpected manifest file count.'
}
$allowedRoot = Join-Path $workspaceRoot 'eval\reports\homeostatic_decision\'
$targets = [Collections.Generic.List[string]]::new()
$seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$missingCount = 0
[long]$targetBytes = 0

# Validate the whole batch before deleting any file; never traverse links.
foreach ($entry in $manifest.files) {
    if ($entry.path -notmatch '^eval/reports/homeostatic_decision/.+/pytest-temp[^/]*/test_[^/]+/.+$') {
        throw "Unexpected path scope: $($entry.path)"
    }
    $targetPath = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $entry.path))
    if (-not $targetPath.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes approved root: $targetPath"
    }
    if (-not $seen.Add($targetPath)) { throw "Duplicate target: $targetPath" }
    $walkPath = $targetPath
    while ($walkPath -ne $workspaceRoot) {
        if (Test-Path -LiteralPath $walkPath) {
            $item = Get-Item -LiteralPath $walkPath -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing link or junction: $walkPath"
            }
        }
        $walkPath = [IO.Path]::GetDirectoryName($walkPath)
        if (-not $walkPath) { throw 'Invalid ancestor chain.' }
    }
    if (-not (Test-Path -LiteralPath $targetPath)) { $missingCount++; continue }
    $item = Get-Item -LiteralPath $targetPath -Force
    if ($item.PSIsContainer) { throw "Expected a file: $targetPath" }
    if ($item.Length -ne $entry.bytes -or
        (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "File differs from approved inventory: $targetPath"
    }
    $targets.Add($targetPath)
    $targetBytes += $item.Length
}

Write-Output "Validated: $($targets.Count) files, $targetBytes bytes; already absent: $missingCount."
if (-not $Execute) {
    Write-Output 'Validation only. Run again with -Execute to delete these files.'
    return
}

# Exact files only: no recursive removal, wildcard expansion, or directory removal.
$removed = 0
foreach ($targetPath in $targets) {
    Remove-Item -LiteralPath $targetPath -Force -ErrorAction Stop
    $removed++
}
$remaining = @($targets | Where-Object { Test-Path -LiteralPath $_ })
if ($remaining.Count) { throw "$($remaining.Count) targets still exist." }
Write-Output "Deleted: $removed files. Empty directories and all other files were retained."
