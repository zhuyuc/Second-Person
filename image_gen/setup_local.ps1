# 本机文生图一键收尾：解压 ComfyUI、放置 SDXL、配置 Provider、启动服务
# 用法：在下载完成后运行
#   powershell -ExecutionPolicy Bypass -File image_gen\setup_local.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $Root "workflows\sdxl_txt2img.json"))) {
  $Root = "D:\project\Second-Person"
}
$Dl = Join-Path $Root "image_gen\_downloads"
$Target = Join-Path $Root "image_gen\comfyui"
$Archive = Join-Path $Dl "ComfyUI_windows_portable_nvidia.7z"
$SdxlSrc = Join-Path $Dl "sd_xl_base_1.0.safetensors"
$SdxlDstDir = Join-Path $Target "ComfyUI\models\checkpoints"
$SdxlDst = Join-Path $SdxlDstDir "sd_xl_base_1.0.safetensors"
$SevenZip = "C:\Program Files\7-Zip\7z.exe"

Write-Host "Root=$Root"

function Wait-FileSize($path, $minBytes, $label) {
  while ($true) {
    if (-not (Test-Path $path)) { Write-Host "[$label] waiting for file..."; Start-Sleep 10; continue }
    $len = (Get-Item $path).Length
    if ($len -ge $minBytes -and -not (Test-Path "$path.aria2")) {
      Write-Host "[$label] ready: $([math]::Round($len/1MB,1)) MB"
      return
    }
    Write-Host "[$label] $([math]::Round($len/1MB,1)) MB ..."
    Start-Sleep 15
  }
}

# ComfyUI portable ~2146721943 bytes
Wait-FileSize $Archive 2100000000 "ComfyUI"
# SDXL ~6938078334 bytes
Wait-FileSize $SdxlSrc 6900000000 "SDXL"

if (-not (Test-Path $SevenZip)) { throw "7-Zip not found: $SevenZip" }

$ExtractTmp = Join-Path $Dl "_extract"
if (Test-Path $ExtractTmp) { Remove-Item $ExtractTmp -Recurse -Force }
New-Item -ItemType Directory -Force -Path $ExtractTmp | Out-Null
Write-Host "Extracting ComfyUI..."
& $SevenZip x $Archive "-o$ExtractTmp" -y | Out-Host

# portable 通常解出 ComfyUI_windows_portable\
$inner = Get-ChildItem $ExtractTmp -Directory | Select-Object -First 1
if (-not $inner) { throw "extract produced no directory" }

New-Item -ItemType Directory -Force -Path $Target | Out-Null
# 清空目标（保留 downloads）
Get-ChildItem $Target -Force | Where-Object { $_.Name -ne '_keep' } | Remove-Item -Recurse -Force -EA SilentlyContinue
Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $Target -Recurse -Force

New-Item -ItemType Directory -Force -Path $SdxlDstDir | Out-Null
Copy-Item $SdxlSrc $SdxlDst -Force
Write-Host "SDXL placed at $SdxlDst"

if (-not (Test-Path (Join-Path $Target "run_nvidia_gpu.bat"))) {
  throw "run_nvidia_gpu.bat missing under $Target"
}

Write-Host "Configuring Second Person provider..."
python (Join-Path $Dl "configure_provider.py")

Write-Host "Starting ComfyUI..."
Start-Process -FilePath (Join-Path $Target "run_nvidia_gpu.bat") -WorkingDirectory $Target -WindowStyle Minimized

# wait health
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8188/system_stats" -TimeoutSec 3
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { Start-Sleep 5 }
}
if ($ok) { Write-Host "ComfyUI healthy on :8188" } else { Write-Host "WARN: ComfyUI not healthy yet; check the console window" }
