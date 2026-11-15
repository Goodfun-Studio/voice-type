# Voice Type (Windows) installer.
# Creates a venv, installs the client + engine deps, writes the config, and
# registers the app to start at login (HKCU Run key — no admin needed).
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# NOTE: this script has never been executed on a real Windows machine (see
# README "诚实的边界"). Treat it as a best-effort starting point.
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$Repo   = Split-Path -Parent $Here
$Support = Join-Path $env:APPDATA "VoiceType"
New-Item -ItemType Directory -Force -Path $Support | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Support "history") | Out-Null

Write-Host "==> 1/4 python 环境 (venv)"
$Venv = Join-Path $Here ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) { $base = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $base) { throw "找不到 python。请先安装 Python 3.10+ 并加入 PATH。" }
    & $base -m venv $Venv
}
$Py  = Join-Path $Venv "Scripts\python.exe"
$PyW = Join-Path $Venv "Scripts\pythonw.exe"   # no console window; for autostart

Write-Host "==> 2/4 安装依赖"
& $Py -m pip install --upgrade pip
& $Py -m pip install -r (Join-Path $Here "requirements.txt")
$EngineReq = Join-Path $Repo "engine\requirements-windows.txt"
if (Test-Path $EngineReq) {
    & $Py -m pip install -r $EngineReq
} else {
    Write-Host "    (未找到 $EngineReq，跳过引擎依赖)"
}

Write-Host "==> 3/4 写配置 $Support\config.json"
$Engine = Join-Path $Repo "engine\engine.py"
$Config = [ordered]@{
    python       = $PyW
    engine       = $Engine
    host         = "127.0.0.1"
    port         = 8765
    transport    = "tcp"
    model        = "small"     # CPU 默认；有 N 卡可改 large-v3-turbo + device=cuda
    device       = "cpu"
    compute_type = "int8"
    language     = "zh"
    beam_size    = 5           # 不要改 1：beam=1 时 whisper 会复读提示词
    sound        = $true
}
$Config | ConvertTo-Json | Set-Content -Path (Join-Path $Support "config.json") -Encoding UTF8

Write-Host "==> 4/4 注册开机自启 (HKCU\...\Run)"
$Entry = Join-Path $Here "voicetype_win\__main__.py"
$RunCmd = "`"$PyW`" `"$Entry`""
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
                 -Name "VoiceType" -Value $RunCmd
Write-Host "    已写入 Run 键: VoiceType = $RunCmd"

Write-Host ""
Write-Host "==> 自检"
& $Py -m voicetype_win --selftest

Write-Host ""
Write-Host "装好了。"
Write-Host "  启动：     .\run.bat        （带控制台，方便看日志）"
Write-Host "  双击右 Ctrl 开始 / 结束，Esc 取消"
Write-Host "  日志：     $Support\voicetype.log"
Write-Host "  历史：     $Support\history\"
Write-Host "  配置：     $Support\config.json"
Write-Host ""
Write-Host "卸载自启： Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name VoiceType"
