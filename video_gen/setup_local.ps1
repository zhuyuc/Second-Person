# Download Wan 2.1 T2V 1.3B (ComfyUI repack) into shared engine dir.
# Usage: powershell -ExecutionPolicy Bypass -File video_gen/setup_local.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Comfy = Join-Path $Root "image_gen\comfyui\ComfyUI"
$Models = Join-Path $Comfy "models"
$Dl = Join-Path $PSScriptRoot "_downloads"
New-Item -ItemType Directory -Force -Path $Dl | Out-Null

$Mirror = if ($env:HF_ENDPOINT) { $env:HF_ENDPOINT.TrimEnd("/") } else { "https://huggingface.co" }
$Repo = "Comfy-Org/Wan_2.1_ComfyUI_repackaged"

# Official Comfy-Org pack: 1.3B is bf16/fp16 (no fp8 in this repo).
$Files = @(
  @{ Rel = "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors"; DestDir = "diffusion_models" },
  @{ Rel = "split_files/vae/wan_2.1_vae.safetensors"; DestDir = "vae" },
  @{ Rel = "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"; DestDir = "text_encoders" }
)

function Get-File($Rel, $DestDir) {
  $name = Split-Path $Rel -Leaf
  $dest = Join-Path (Join-Path $Models $DestDir) $name
  if (Test-Path $dest) {
    Write-Host "EXISTS: $dest"
    return
  }
  New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
  $url = "$Mirror/$Repo/resolve/main/$Rel"
  $tmp = Join-Path $Dl "$name.partial"
  Write-Host "DOWNLOAD: $url"
  Write-Host " -> $dest"
  & curl.exe -L --fail --retry 5 --retry-delay 5 -C - -o $tmp $url
  if ($LASTEXITCODE -ne 0) { throw "curl failed exit=$LASTEXITCODE for $url" }
  if (-not (Test-Path $tmp) -or (Get-Item $tmp).Length -lt 1000000) {
    throw "Download failed or file too small: $tmp"
  }
  Move-Item -Force $tmp $dest
  $mb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
  Write-Host "OK: $dest ($mb MB)"
}

if (-not (Test-Path $Comfy)) {
  throw "ComfyUI not found: $Comfy. Install portable pack first (see image_gen/README.md)."
}

foreach ($f in $Files) {
  Get-File $f.Rel $f.DestDir
}

Write-Host ""
Write-Host "Models ready. Start ComfyUI, bind video_gen slot,"
Write-Host "model_id=wan2.1_t2v_1.3B_fp16.safetensors"
Write-Host "workflow: workflows/wan21_t2v_1_3b.json"
