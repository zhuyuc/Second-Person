# 独立会话启动 Langfuse（由计划任务 SecondPersonLangfuse 调用，也可手动执行）
# 与任何终端会话隔离，避免外部控制台中断信号误杀 web（next start 收到 Ctrl+C 会优雅退出）
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent  # langfuse/deploy/ → langfuse/ → 项目根
$envFile = Join-Path $PSScriptRoot 'langfuse.env'
$logFile = Join-Path $root 'data\logs\langfuse.log'

# 已在运行则直接退出（幂等）
try {
    $r = Invoke-WebRequest -Uri 'http://localhost:3001/api/public/health' -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) { exit 0 }
}
catch {}

# 注入 langfuse.env 环境变量
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
    }
}

# WMI/计划任务上下文不继承用户级 PATH（pnpm 在 D:\npm-global 仅登记于用户级），
# 裸调 pnpm 会 CommandNotFound + ErrorActionPreference=Stop 静默退出，此处显式补齐
$env:PATH = 'D:\npm-global;C:\Program Files\nodejs;' + $env:PATH
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
    Add-Content $logFile "[run-langfuse] FATAL: 找不到 pnpm（PATH=$env:PATH）"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
Set-Location (Join-Path $PSScriptRoot '..\server')
$env:npm_config_engine_strict = 'false'   # 兼容 Node 22（仓库 engines 声明 node=20）
Add-Content $logFile "[run-langfuse] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 启动（pid=$PID，独立会话）"
pnpm run start *>> $logFile
