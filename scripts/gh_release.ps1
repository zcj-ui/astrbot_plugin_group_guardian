# gh_release.ps1 - create GitHub release via direct IP (api.github.com)
param(
  [Parameter(Mandatory = $true)][string]$Owner,
  [Parameter(Mandatory = $true)][string]$Repo,
  [Parameter(Mandatory = $true)][string]$Tag,
  [Parameter(Mandatory = $true)][string]$Name,
  [Parameter(Mandatory = $true)][string]$BodyFile,
  [string]$ApiIp = '140.82.113.6'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path 'd:\Projects\group_guardian\scripts' 'gh.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== release " + (Get-Date -Format o) + " ===")

$cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
$pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
if (-not $pat) { Log 'NO_PAT'; exit 1 }
Log "PAT_OBTAINED len=$($pat.Length)"

$api = "https://$ApiIp/repos/$Owner/$Repo"
$payloadFile = Join-Path 'd:\Projects\group_guardian\scripts' 'release_payload.json'
$body = [System.IO.File]::ReadAllText($BodyFile, [System.Text.Encoding]::UTF8)
$payload = @{ tag_name = $Tag; name = $Name; body = $body; draft = $false; prerelease = $false } | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($payloadFile, $payload, (New-Object System.Text.UTF8Encoding($false)))

# create release
$createOut = & curl.exe -sk --connect-timeout 10 --max-time 60 -X POST "$api/releases" `
  -H "Host: api.github.com" -H "Authorization: Bearer $pat" `
  -H "Accept: application/vnd.github+json" -H "Content-Type: application/json" `
  --data-binary "@$payloadFile" 2>&1
foreach ($l in $createOut) { Log ("CREATE: " + $l.ToString()) }
$resp = ($createOut | Out-String)
$releaseId = $null
if ($resp -match '"id":\s*(\d+)') { $releaseId = $Matches[1] }
if (-not $releaseId) { Log 'RELEASE_FAILED'; exit 1 }
Log "RELEASE_ID=$releaseId"
exit 0
