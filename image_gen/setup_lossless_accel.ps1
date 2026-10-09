# Local ComfyUI lossless accel setup (SageAttention + lowvram + config)
# Usage:
#   powershell -ExecutionPolicy Bypass -File image_gen\setup_lossless_accel.ps1
#   powershell -ExecutionPolicy Bypass -File image_gen\setup_lossless_accel.ps1 -SkipPip
#   powershell -ExecutionPolicy Bypass -File image_gen\setup_lossless_accel.ps1 -WithWanWrapper

param(
  [switch]$SkipPip,
  [switch]$WithWanWrapper
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $Root "workflows\wan21_t2v_1_3b_lossless.json"))) {
  $Root = "D:\project\Second-Person"
}
$ComfyRoot = Join-Path $Root "image_gen\comfyui"
$Py = Join-Path $ComfyRoot "python_embeded\python.exe"
$Bat = Join-Path $ComfyRoot "run_nvidia_gpu.bat"
$BatLossless = Join-Path $ComfyRoot "run_nvidia_gpu_lossless.bat"
$CustomNodes = Join-Path $ComfyRoot "ComfyUI\custom_nodes"
$Cfg = Join-Path $Root "data\config.yaml"

Write-Host "Root=$Root"
Write-Host "ComfyRoot=$ComfyRoot"

if (-not (Test-Path $Py)) {
  throw "Portable Python not found: $Py. Install ComfyUI portable first (see image_gen/README.md)."
}

if (-not $SkipPip) {
  Write-Host "Installing sageattention + triton-windows..."
  & $Py -m pip install --upgrade pip
  & $Py -m pip install "triton-windows"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: triton-windows install failed; continue"
  }
  & $Py -m pip install "sageattention"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: sageattention install failed; ComfyUI can still run with default attention"
  } else {
    & $Py -c "import sageattention; print('sageattention import OK')"
    if ($LASTEXITCODE -ne 0) {
      Write-Host "WARN: sageattention import failed; check triton/CUDA wheels"
    }
  }
} else {
  Write-Host "SkipPip: skip pip installs"
}

$LaunchLine = ".\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 127.0.0.1 --port 8188 --lowvram --use-sage-attention"
@(
  "@echo off",
  "REM Second Person lossless accel launcher (SageAttention + lowvram)",
  $LaunchLine,
  "if errorlevel 1 (",
  "  echo If start failed, use run_nvidia_gpu.bat or re-run setup_lossless_accel.ps1",
  "  pause",
  ")"
) | Set-Content -Path $BatLossless -Encoding ASCII
Write-Host "Wrote $BatLossless"

if (Test-Path $Bat) {
  $raw = Get-Content $Bat -Raw
  if ($raw -notmatch "--lowvram") {
    $patched = $raw -replace "(ComfyUI\\main\.py[^\r\n]*)", "`$1 --lowvram --use-sage-attention"
    if ($patched -eq $raw) {
      $patched = $raw -replace "(\.\\python_embeded\\python\.exe -s ComfyUI\\main\.py[^\r\n]*)", $LaunchLine
    }
    Copy-Item $Bat "$Bat.bak_before_lossless" -Force
    Set-Content -Path $Bat -Value $patched -Encoding ASCII
    Write-Host "Patched $Bat (backup .bak_before_lossless)"
  } else {
    Write-Host "run_nvidia_gpu.bat already has --lowvram; skip patch"
  }
}

if ($WithWanWrapper) {
  New-Item -ItemType Directory -Force -Path $CustomNodes | Out-Null
  $Dest = Join-Path $CustomNodes "ComfyUI-WanVideoWrapper"
  if (Test-Path $Dest) {
    Write-Host "WanVideoWrapper already present: $Dest"
  } else {
    Write-Host "Cloning ComfyUI-WanVideoWrapper..."
    git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git $Dest
    if ($LASTEXITCODE -ne 0) {
      Write-Host "WARN: clone WanVideoWrapper failed (optional; default workflow does not need it)"
    }
  }
}

if (Test-Path $Cfg) {
  $tmpPy = Join-Path $env:TEMP "sp_setup_lossless_cfg.py"
  @"
from pathlib import Path
import sys
root = Path(r"$Root")
sys.path.insert(0, str(root))
from infrastructure.config_manager import ConfigManager
cfg = ConfigManager(root / "data" / "config.yaml")
cfg.set_raw("video_gen_comfyui_workflow", "./workflows/wan21_t2v_1_3b_lossless.json")
cfg.load()
cfg.update_params({
    "comfyui_quality_mode": "lossless",
    "comfyui_use_sage_attention": True,
    "comfyui_lowvram": True,
    "video_gen_t5_on_cpu": True,
    "video_gen_block_swap": 0,
})
print("config updated")
"@ | Set-Content -Path $tmpPy -Encoding UTF8
  $ok = $false
  try {
    python $tmpPy
    if ($LASTEXITCODE -eq 0) { $ok = $true }
  } catch { }
  if (-not $ok) {
    & $Py $tmpPy
  }
} else {
  Write-Host "WARN: no data\config.yaml; factory default already points to lossless workflow"
}

Write-Host ""
Write-Host "Done."
Write-Host "1) Restart ComfyUI with run_nvidia_gpu_lossless.bat (or start.py)"
Write-Host "2) Verify: python scripts/verify_local_gen_quality.py"
Write-Host "3) Rollback: restore .bak_before_lossless / set workflow to wan21_t2v_1_3b.json"
