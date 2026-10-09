# Local System One (jev-style) — 与 embedding 同思路：
#   system_one/venv     隔离依赖（gitignore）
#   system_one/models   权重缓存 HF_HOME（gitignore）
#   hf-mirror           国内镜像
#   setup 后写 services + 自动绑 mood_fast
param(
    [ValidateSet("0.8b", "2b")]
    [string]$Release = "0.8b",
    [int]$Port = 8765
)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Base = Join-Path $Root "system_one"
$Venv = Join-Path $Base "venv"
$Models = Join-Path $Base "models"
$EnvFile = Join-Path $Base "hf.env"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$JevExe = Join-Path $Venv "Scripts\jev-style.exe"
$MainPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $MainPy)) { $MainPy = "python" }

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg) { Write-Host $msg -ForegroundColor Green }
function Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

# 与 embedding/download_model.py 一致：项目内缓存 + 镜像
New-Item -ItemType Directory -Force -Path $Models | Out-Null
$env:HF_HOME = $Models
$env:HF_ENDPOINT = "https://hf-mirror.com"
# 部分环境 xet CAS 会 401；走传统 HTTP 下载更稳（与镜像兼容）
$env:HF_HUB_DISABLE_XET = "1"
@"
HF_HOME=$Models
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1
"@ | Set-Content -Path $EnvFile -Encoding utf8
Ok "HF_HOME=$Models"
Ok "HF_ENDPOINT=$($env:HF_ENDPOINT)"
Ok "HF_HUB_DISABLE_XET=1"

Step "Create isolated venv (system_one/venv) — prefer Python 3.11 like embedding"
# CUDA wheel 对 3.13 支持不完整；与 embedding 对齐优先 3.11
$PyForVenv = $null
foreach ($c in @(
    "D:\installation\Python3.11.9\python.exe",
    "D:\installation\Python3.11\python.exe"
)) {
    if (Test-Path $c) { $PyForVenv = $c; break }
}
if (-not $PyForVenv) {
    $pyList = & py -0p 2>$null
    if ($pyList -match "3\.11\s+(\S+python\.exe)") { $PyForVenv = $Matches[1] }
}
if (-not $PyForVenv) { $PyForVenv = (Get-Command python).Source }
Ok "venv base interpreter: $PyForVenv"
if (-not (Test-Path $VenvPy)) {
    & $PyForVenv -m venv $Venv
} else {
    $ver = & $VenvPy -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    if ([version]$ver -ge [version]"3.12" -and $PyForVenv -match "3\.11") {
        Warn "Existing venv is Python $ver; recreating with 3.11 for CUDA"
        Remove-Item -Recurse -Force $Venv
        & $PyForVenv -m venv $Venv
    }
}
& $VenvPy -m pip install --upgrade pip -q

Step "Install jev-style[torch] into isolated venv"
$hasGpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($hasGpu) {
    # transformers 5.x 需要较新的 torch.accelerator；cu126/torch2.14 可用
    # （embedding 仍用 cu121；本服务单独对齐 jev-style 依赖）
    Ok "NVIDIA GPU detected — install CUDA torch (cu126, transformers-compatible)"
    & $VenvPy -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu126
    if ($LASTEXITCODE -ne 0) {
        Warn "cu126 failed, fallback cu124"
        & $VenvPy -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu124
    }
} else {
    Warn "No nvidia-smi; will pin --device cpu"
}
& $VenvPy -m pip install "jev-style[torch]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "jev-style install failed" -ForegroundColor Red
    exit 1
}

Step "Download weights into system_one/models (~1.5GB for 0.8b)"
& $JevExe download --release $Release
if ($LASTEXITCODE -ne 0) {
    Warn "download failed or skipped; first serve will retry under same HF_HOME"
}

Step "Write services.system_one into data/config.yaml"
$writeArgs = @(
    (Join-Path $Base "write_service_config.py"),
    "--exe", $JevExe,
    "--port", "$Port",
    "--release", $Release,
    "--env-file", $EnvFile
)
if (-not $hasGpu) { $writeArgs += "--cpu" }
& $MainPy @writeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "config write failed" -ForegroundColor Red
    exit 1
}

Step "Auto-add Provider + bind mood_fast slot"
& $MainPy (Join-Path $Base "ensure_provider.py") --force-bind
if ($LASTEXITCODE -ne 0) {
    Warn "Provider auto-bind failed; start.py will retry on next launch"
}

Ok "Done."
Write-Host "  Weights : $Models"
Write-Host "  Venv    : $Venv"
Write-Host "  Restart : python start.py restart"
Write-Host "  Manual  : $JevExe serve --backend torch --port $Port --release $Release"
