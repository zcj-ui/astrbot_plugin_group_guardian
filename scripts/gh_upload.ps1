# gh_upload.ps1 - upload release asset to uploads.github.com via direct IP
param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$ReleaseId,
  [Parameter(Mandatory = $true)][string]$AssetPath,
  [string]$UploadIp = '140.82.112.5'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path 'd:\Projects\group_guardian\scripts' 'gh.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== upload " + (Get-Date -Format o) + " ===")

$cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
$pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
if (-not $pat) { Log 'NO_PAT'; exit 1 }
Log "PAT_OBTAINED len=$($pat.Length)"

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
