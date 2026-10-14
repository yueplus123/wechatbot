# Build a small deploy zip (no .venv / logs / caches). ASCII-only for PowerShell 5.1.
param([switch]$NoDb)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$Out = Join-Path (Split-Path -Parent $Root) "xinhuo-bot-deploy.zip"
$Stage = Join-Path $env:TEMP ("xh_pack_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Stage | Out-Null
$Dest = Join-Path $Stage "xinhuo-bot"
New-Item -ItemType Directory -Path $Dest | Out-Null

# dirs: code only
foreach ($d in @("apps", "gateway", "config", "templates", "static")) {
    if (Test-Path -LiteralPath $d) {
        & robocopy $d (Join-Path $Dest $d) /E /XD __pycache__ /XF *.pyc .env /NFL /NDL /NJH /NJS /NP | Out-Null
    }
}
# top-level files
$files = @(
    "manage.py", "requirements.txt", "requirements-mysql.txt", "requirements-dev.txt",
    "setup_env.ps1", "setup_env.bat", "fix_pip.bat", "deploy.ps1", "README.md",
    ".env.example"
)
Get-ChildItem -LiteralPath $Root -File | Where-Object {
    $files -contains $_.Name -or $_.Extension -eq ".bat"
} | Where-Object { $_.Name -ne "打包上传.bat" } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Dest -Force
}
# one-click launcher keeps its Chinese name
Copy-Item -LiteralPath (Join-Path $Root "一键部署.bat") -Destination $Dest -Force -ErrorAction SilentlyContinue

# data: only small config caches (no logs)
$dataDest = Join-Path $Dest "data"
New-Item -ItemType Directory -Path $dataDest -Force | Out-Null
foreach ($f in @("room_nicks.json", "room_my_alias.json", "panel_member_nicks.json", "room_avatar_map.json")) {
    $p = Join-Path $Root "data\$f"
    if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination $dataDest -Force }
}
# database (optional, default: include)
$withDb = -not $NoDb
$rtDest = Join-Path $Dest "runtime"
New-Item -ItemType Directory -Path $rtDest -Force | Out-Null
if ($withDb -and (Test-Path -LiteralPath "runtime\poc.sqlite3")) {
    Copy-Item -LiteralPath "runtime\poc.sqlite3" -Destination $rtDest -Force
}

# config/.env is NOT packed: deploy.ps1 generates a fresh one with new random secrets
if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Force }
Compress-Archive -Path (Join-Path $Stage "xinhuo-bot") -DestinationPath $Out -CompressionLevel Optimal
Remove-Item -LiteralPath $Stage -Recurse -Force

$mb = [math]::Round((Get-Item -LiteralPath $Out).Length / 1MB, 2)
Write-Host ""
Write-Host ("OK: " + $Out + "  (" + $mb + " MB)") -ForegroundColor Green
Write-Host "Upload this zip to the cloud PC, unzip, then double-click the one-click deploy bat."
