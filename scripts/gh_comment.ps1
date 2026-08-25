# gh_comment.ps1 - POST an issue/PR comment via direct IP
param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$IssueNumber,
  [Parameter(Mandatory = $true)][string]$BodyFile,
  [string]$ApiIp = '140.82.113.6'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path 'd:\Projects\group_guardian\scripts' 'gh.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== comment " + (Get-Date -Format o) + " ===")

$cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
$pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
if (-not $pat) { Log 'NO_PAT'; exit 1 }
Log "PAT_OBTAINED len=$($pat.Length)"

$payloadFile = Join-Path 'd:\Projects\group_guardian\scripts' 'comment_payload.json'
$body = [System.IO.File]::ReadAllText($BodyFile, [System.Text.Encoding]::UTF8)
$payload = @{ body = $body } | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($payloadFile, $payload, (New-Object System.Text.UTF8Encoding($false)))

$postOut = & curl.exe -sk --connect-timeout 10 --max-time 60 -X POST "https://$ApiIp/repos/$Owner/$Repo/issues/$IssueNumber/comments" `
  -H "Host: api.github.com" -H "Authorization: Bearer $pat" `
  -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" `
  --data-binary "@$payloadFile" 2>&1
foreach ($l in $postOut) { Log ("POST: " + $l.ToString()) }
if (($postOut | Out-String) -match '"html_url":') {
  Log 'COMMENT_OK'
} else {
  Log 'COMMENT_MAYBE_FAILED'
}
exit 0
