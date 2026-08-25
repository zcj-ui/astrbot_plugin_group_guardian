# gh_fix.ps1 - PATCH release body (from file, avoids cmdline encoding issues) + upload asset via --resolve
param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$ReleaseId,
  [Parameter(Mandatory = $true)][string]$BodyFile,
  [Parameter(Mandatory = $true)][string]$AssetPath,
  [string]$ApiIp = '140.82.113.6',
  [string]$UploadIp = '140.82.112.3'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path 'd:\Projects\group_guardian\scripts' 'gh.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== fix " + (Get-Date -Format o) + " ===")

$cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
$pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
if (-not $pat) { Log 'NO_PAT'; exit 1 }
Log "PAT_OBTAINED len=$($pat.Length)"

# 1) PATCH release body (read file as UTF-8, write payload as UTF-8 no BOM)
$body = [System.IO.File]::ReadAllText($BodyFile, [System.Text.Encoding]::UTF8)
$payloadFile = Join-Path 'd:\Projects\group_guardian\scripts' 'release_payload.json'
$payload = @{ body = $body } | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($payloadFile, $payload, (New-Object System.Text.UTF8Encoding($false)))
$patchOut = & curl.exe -sk --connect-timeout 10 --max-time 60 -X PATCH "https://$ApiIp/repos/$Owner/$Repo/releases/$ReleaseId" `
  -H "Host: api.github.com" -H "Authorization: Bearer $pat" `
  -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" `
  --data-binary "@$payloadFile" 2>&1
foreach ($l in $patchOut) { Log ("PATCH: " + $l.ToString()) }

# 2) upload asset via --resolve
$assetName = Split-Path -Leaf $AssetPath
$uploadOut = & curl.exe -sk --connect-timeout 15 --max-time 150 `
  --resolve "uploads.github.com:443:$UploadIp" `
  -X POST "https://uploads.github.com/repos/$Owner/$Repo/releases/$ReleaseId/assets?name=$assetName" `
  -H "Authorization: Bearer $pat" `
  -H "Accept: application/vnd.github+json" -H "Content-Type: application/zip" `
  --data-binary "@$AssetPath" 2>&1
foreach ($l in $uploadOut) { Log ("UPLOAD: " + $l.ToString()) }
if (($uploadOut | Out-String) -match '"state":\s*"uploaded"') {
  Log 'UPLOAD_OK'
} else {
  Log 'UPLOAD_MAYBE_FAILED'
}
exit 0
