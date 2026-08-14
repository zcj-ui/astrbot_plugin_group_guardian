# build_zip.ps1 - build AstrBot plugin install zip (first entry is top-level dir, compatible with unzip_file)
param(
  [Parameter(Mandatory = $true)][string]$SourceDir,
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][string]$OutputDir
)
$ErrorActionPreference = 'Stop'
$top = 'astrbot_plugin_group_guardian'
$zipPath = Join-Path $OutputDir "${top}-${Version}.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
  # AstrBot unzip_file requires the first zip entry to be the top-level directory entry
  $dirEntry = $zip.CreateEntry("$top/")
  $dirStream = $dirEntry.Open()
  $dirStream.Close()
  $excludeDirs = @('.git', '.github', '.sakura', 'tests', 'scripts')
  $excludeNames = @('.gitignore', '*.zip')
  $files = Get-ChildItem -Path $SourceDir -Recurse -File | Where-Object {
    $rel = $_.FullName.Substring($SourceDir.Length).TrimStart('\', '/')
    $parts = $rel -split '[\\/]'
    foreach ($p in $parts[0..([Math]::Max(0, $parts.Length - 2))]) {
      if ($excludeDirs -contains $p) { return $false }
    }
    if ($excludeNames -contains $_.Name) { return $false }
    if ($_.Extension -eq '.zip') { return $false }
    return $true
  }
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($SourceDir.Length).TrimStart('\', '/')
    $entryPath = "$top/$($rel -replace '\\', '/')"
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
      $zip, $f.FullName, $entryPath, [System.IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null
  }
}
finally {
  $zip.Dispose()
}
Write-Output $zipPath
