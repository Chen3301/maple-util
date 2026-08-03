# MapleUtil 빌드 스크립트
# icon.png 또는 icon.ico 를 윈도우 아이콘 규격(app.ico)으로 변환해 사용한다.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Get-Process MapleUtil, MapleAuctionUtil -ErrorAction SilentlyContinue |
    Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep 1

& .\venv\Scripts\python.exe make_icon.py

$pyiArgs = @("--noconfirm", "--onedir", "--windowed", "--name", "MapleUtil",
             "--collect-all", "playwright")
if (Test-Path app.ico) {
    $pyiArgs += @("--icon", "app.ico", "--add-data", "app.ico;.")
} else {
    Write-Output "아이콘 없음 (icon.png 를 폴더에 넣으면 자동 적용됩니다)"
}
if (Test-Path avatar.png) { $pyiArgs += @("--add-data", "avatar.png;.") }
if (Test-Path notify.mp3) { $pyiArgs += @("--add-data", "notify.mp3;.") }
$pyiArgs += "gui.py"

$out = & .\venv\Scripts\pyinstaller.exe @pyiArgs
Write-Output ($out | Select-Object -Last 1)

$proc = Start-Process ".\dist\MapleUtil\MapleUtil.exe" -ArgumentList "--selftest" -PassThru -Wait
Write-Output ("SELFTEST exit=" + $proc.ExitCode)

# 배포용 zip (로그인 정보 제외)
$tgt = "dist_release\MapleUtil"
New-Item -ItemType Directory -Force dist_release | Out-Null
if (Test-Path $tgt) { Remove-Item -Recurse -Force $tgt }
Copy-Item -Recurse -Force dist\MapleUtil $tgt
Copy-Item -Force 사용법.txt dist_release\
foreach ($f in @("cookies.json", "profile")) {
    $p = Join-Path $tgt $f
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
if (Test-Path MapleUtil.zip) { Remove-Item MapleUtil.zip -Force }
Compress-Archive -Path $tgt, "dist_release\사용법.txt" -DestinationPath MapleUtil.zip -Force
Write-Output ("MapleUtil.zip {0:N0} MB" -f ((Get-Item MapleUtil.zip).Length / 1MB))
