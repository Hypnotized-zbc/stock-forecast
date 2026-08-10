
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$edgePath = $null
$pf86 = ${env:ProgramFiles(x86)}
$pf = $env:ProgramFiles
$la = $env:LOCALAPPDATA
$candidates = @(
  "$pf86\Microsoft\Edge\Application\msedge.exe",
  "$pf\Microsoft\Edge\Application\msedge.exe",
  "$la\Microsoft\Edge\Application\msedge.exe"
)
foreach ($c in $candidates) {
  if (Test-Path $c) { $edgePath = $c; break }
}
if (-not $edgePath) {
  try {
    $reg = Get-Item "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" -ErrorAction SilentlyContinue
    if ($reg) {
      $v = $reg.GetValue("")
      if ($v -and (Test-Path $v)) { $edgePath = $v }
    }
  } catch {}
}
if (-not $edgePath) {
  Write-Output "EDGE_NOT_FOUND"
  exit 1
}

Get-Process msedge -ErrorAction SilentlyContinue | Where-Object {
  $_.Path -eq $edgePath -and $_.CommandLine -match "remote-debugging-port=9222"
} | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

$profile = Join-Path $env:TEMP "stock_edge_profile"
if (Test-Path $profile) { Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue }
Start-Process $edgePath -ArgumentList @(
  "--headless", "--disable-gpu", "--no-first-run",
  "--user-data-dir=$profile",
  "--remote-debugging-port=9222", "--remote-debugging-address=0.0.0.0",
  "about:blank"
)
Start-Sleep 6
try {
  $v = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 5
  Write-Output ("CDP OK: " + $v.Browser)
} catch {
  Write-Output ("CDP FAIL: " + $_.Exception.Message)
}
