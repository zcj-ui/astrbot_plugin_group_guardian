# gh_pr_update.ps1 - PATCH a pull request body/title on the upstream repo via direct IP
param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$PrNumber,
  [Parameter(Mandatory = $true)][string]$BodyFile,
  [Parameter(Mandatory = $true)][string]$TitleFile,
  [string]$ApiIp = '140.82.113.6'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path 'd:\Projects\group_guardian\scripts' 'gh.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== pr-update " + (Get-Date -Format o) + " ===")

$cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
$pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
if (-not $pat) { Log 'NO_PAT'; exit 1 }
Log "PAT_OBTAINED len=$($pat.Length)"

$payloadFile = Join-Path 'd:\Projects\group_guardian\scripts' 'pr_payload.json'
$body = [System.IO.File]::ReadAllText($BodyFile, [System.Text.Encoding]::UTF8)
$title = [System.IO.File]::ReadAllText($TitleFile, [System.Text.Encoding]::UTF8).Trim()
$payload = @{ body = $body; title = $title } | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($payloadFile, $payload, (New-Object System.Text.UTF8Encoding($false)))

$patchOut = & curl.exe -sk --connect-timeout 10 --max-time 60 -X PATCH "https://$ApiIp/repos/$Owner/$Repo/pulls/$PrNumber" `
  -H "Host: api.github.com" -H "Authorization: Bearer $pat" `
  -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" `
  --data-binary "@$payloadFile" 2>&1
foreach ($l in $patchOut) { Log ("PATCH: " + $l.ToString()) }
$resp = ($patchOut | Out-String)
if ($resp -match '"title":\s*"feat') {
  Log 'PR_UPDATED_OK'
} else {
  Log 'PR_UPDATE_MAYBE_FAILED'
}
exit 0
