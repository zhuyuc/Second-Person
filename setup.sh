#!/usr/bin/env bash
# ============================================================
# Second Person 一键环境自举脚本（Linux / macOS）
# 用法：
#   ./setup.sh                  完整安装（主环境 + Embedding 环境 + 模型下载）
#   ./setup.sh --no-embedding   只装主环境（跳过本地模型，检索降级 FTS5）
# 每步失败只警告不中断（与 start.py 的可选服务理念一致），
# 装完后运行：.venv/bin/python start.py
# ============================================================
set -u
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
NO_EMBEDDING=0
[ "${1:-}" = "--no-embedding" ] && NO_EMBEDDING=1

step() { printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m%s\033[0m\n' "$1"; }

# ---- [1/4] 检查 Python ----
step "[1/4] 检查 Python（需 3.10+）"
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then echo "未找到 python3，请先安装 Python 3.10+。" >&2; exit 1; fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 版本过低，需要 3.10+。" >&2; exit 1
fi
ok "$("$PY" --version)"

# ---- [2/4] 主环境 .venv + requirements.txt ----
step "[2/4] 创建主环境 .venv 并安装依赖"
VENV_PY="$BASE_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || "$PY" -m venv "$BASE_DIR/.venv"
"$VENV_PY" -m pip install --upgrade pip -q
if "$VENV_PY" -m pip install -r "$BASE_DIR/requirements.txt"; then
    ok "主环境就绪"
else
    warn "主环境依赖安装存在错误，请检查上方输出"
fi

# ---- [3/4] Embedding 隔离环境（可选）----
if [ "$NO_EMBEDDING" = "1" ]; then
    warn "[3/4] 跳过 Embedding 环境（--no-embedding），记忆检索将降级为 FTS5 全文检索"
else
    step "[3/4] 创建 Embedding 隔离环境 embedding/venv（torch + sentence-transformers）"
    EMB_PY="$BASE_DIR/embedding/venv/bin/python"
    [ -x "$EMB_PY" ] || "$PY" -m venv "$BASE_DIR/embedding/venv"
    "$EMB_PY" -m pip install --upgrade pip -q
    # 有 NVIDIA GPU 装 CUDA 版 torch，否则装 CPU 版（macOS 直接走默认源，自动带 MPS 支持）
    if command -v nvidia-smi >/dev/null 2>&1; then
        ok "检测到 NVIDIA GPU，安装 CUDA 版 torch"
        "$EMB_PY" -m pip install torch --index-url https://download.pytorch.org/whl/cu121
    elif [ "$(uname)" = "Darwin" ]; then
        ok "macOS：安装默认版 torch（自动支持 MPS 加速）"
        "$EMB_PY" -m pip install torch
    else
        warn "未检测到 NVIDIA GPU，安装 CPU 版 torch（Embedding 速度较慢但可用）"
        "$EMB_PY" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    fi
    "$EMB_PY" -m pip install -r "$BASE_DIR/embedding/requirements.txt" \
        || warn "Embedding 依赖安装存在错误，可稍后重跑本脚本"

    # ---- [4/4] 下载 BGE-M3 模型（~2.3GB，hf-mirror 镜像）----
    step "[4/4] 下载 BGE-M3 模型到 embedding/models/（约 2.3GB，已存在则秒过）"
    if "$EMB_PY" "$BASE_DIR/embedding/download_model.py"; then
        ok "模型就绪"
    else
        warn "模型下载失败，可稍后重跑本脚本；期间检索自动降级 FTS5"
    fi
fi

# ---- 收尾 ----
echo ""
ok "安装完成。启动方式："
echo "  .venv/bin/python start.py      # 一键启动（自动拉起 Embedding 服务并打开浏览器）"
echo ""
echo "可选增强（不影响核心功能）："
echo "  - Langfuse 链路观测：需 PostgreSQL/Redis/Node/pnpm，参照 langfuse-deploy/README.md 部署"
echo "  - 修改前端：需 Node 18+，cd frontend && npm install && npm run build"
