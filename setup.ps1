# ============================================================
# Second Person 一键环境自举脚本（Windows / PowerShell 7+）
# 用法：
#   .\setup.ps1                 完整安装（主环境 + Embedding 环境 + 模型下载）
#   .\setup.ps1 -NoEmbedding    只装主环境（跳过本地模型，检索降级 FTS5）
# 每步失败只警告不中断（与 start.py 的可选服务理念一致），
# 装完后运行：python start.py
# ============================================================
param(
    [switch]$NoEmbedding
)
$ErrorActionPreference = "Continue"
$BaseDir = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
function Ok($msg) { Write-Host $msg -ForegroundColor Green }

# ---- [1/4] 检查 Python ----
Step "[1/4] 检查 Python（需 3.10+）"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "未找到 python，请先安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red; exit 1 }
$ver = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
if ([version]$ver -lt [version]"3.10") {
    Write-Host "Python 版本 $ver 过低，需要 3.10+。" -ForegroundColor Red; exit 1
}
Ok "Python $ver"

# ---- [2/4] 主环境 .venv + requirements.txt ----
Step "[2/4] 创建主环境 .venv 并安装依赖"
$venvPy = Join-Path $BaseDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & python -m venv (Join-Path $BaseDir ".venv") }
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r (Join-Path $BaseDir "requirements.txt")
if ($LASTEXITCODE -eq 0) { Ok "主环境就绪" } else { Warn "主环境依赖安装存在错误，请检查上方输出" }

# ---- [3/4] Embedding 隔离环境（可选）----
if ($NoEmbedding) {
    Warn "[3/4] 跳过 Embedding 环境（--NoEmbedding），记忆检索将降级为 FTS5 全文检索"
}
else {
    Step "[3/4] 创建 Embedding 隔离环境 embedding/venv（torch + sentence-transformers）"
    $embVenvPy = Join-Path $BaseDir "embedding\venv\Scripts\python.exe"
    if (-not (Test-Path $embVenvPy)) { & python -m venv (Join-Path $BaseDir "embedding\venv") }
    & $embVenvPy -m pip install --upgrade pip -q
    # 有 NVIDIA GPU 装 CUDA 版 torch，否则装 CPU 版
    $hasGpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if ($hasGpu) {
        Ok "检测到 NVIDIA GPU，安装 CUDA 版 torch"
        & $embVenvPy -m pip install torch --index-url https://download.pytorch.org/whl/cu121
    }
    else {
        Warn "未检测到 NVIDIA GPU，安装 CPU 版 torch（Embedding 速度较慢但可用）"
        & $embVenvPy -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    }
    & $embVenvPy -m pip install -r (Join-Path $BaseDir "embedding\requirements.txt")
    if ($LASTEXITCODE -ne 0) { Warn "Embedding 依赖安装存在错误，可稍后重跑本脚本" }

    # ---- [4/4] 下载 BGE-M3 模型（~2.3GB，hf-mirror 镜像）----
    Step "[4/4] 下载 BGE-M3 模型到 embedding/models/（约 2.3GB，已存在则秒过）"
    & $embVenvPy (Join-Path $BaseDir "embedding\download_model.py")
    if ($LASTEXITCODE -eq 0) { Ok "模型就绪" } else { Warn "模型下载失败，可稍后重跑本脚本；期间检索自动降级 FTS5" }
}

# ---- 收尾 ----
Write-Host ""
Ok "安装完成。启动方式："
Write-Host "  .\.venv\Scripts\python.exe start.py      # 一键启动（自动拉起 Embedding 服务并打开浏览器）"
Write-Host ""
Write-Host "可选增强（不影响核心功能）：" -ForegroundColor DarkGray
Write-Host "  - Langfuse 链路观测：需 PostgreSQL/Redis/Node/pnpm，参照 langfuse/deploy/README.md 部署" -ForegroundColor DarkGray
Write-Host "  - 修改前端：需 Node 18+，cd frontend && npm install && npm run build" -ForegroundColor DarkGray
