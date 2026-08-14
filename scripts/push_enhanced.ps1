# push_github.ps1 - push a refspec to a GitHub repo via direct IP (140.82.112.3) + Basic auth
param(
  [Parameter(Mandatory = $true)][string]$RepoUrl,
  [Parameter(Mandatory = $true)][string]$Refspec,
  [string]$RepoDir = 'd:\Projects\group_guardian'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path $RepoDir 'scripts\push.log'
function Log($msg) { Add-Content -Path $log -Value $msg }
Set-Content -Path $log -Value ("=== push " + (Get-Date -Format o) + " -> $RepoUrl $Refspec ===")
try {
  $cred = ("protocol=https`nhost=github.com`n`n" | git credential fill 2>$null) | Out-String
  $pat = ($cred -split "`n" | Where-Object { $_ -like 'password=*' } | ForEach-Object { $_ -replace '^password=', '' }).Trim()
  if (-not $pat) { Log 'NO_PAT'; exit 1 }
  $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$pat"))
  Log "PAT_OBTAINED len=$($pat.Length)"
  $out = & git -C $RepoDir -c "http.extraHeader=Authorization: Basic $b64" -c "http.extraHeader=Host: github.com" `
      -c http.sslVerify=false -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=90 `
      push "$RepoUrl" "$Refspec" 2>&1
  foreach ($line in $out) { Log ("  " + $line.ToString()) }
  Log "PUSH_DONE exit=$LASTEXITCODE"
  exit $LASTEXITCODE
}
catch {
  Log "ERROR: $_"
  exit 1
}
