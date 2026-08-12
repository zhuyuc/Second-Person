# ============================================================
# Langfuse v2 本地部署脚本（从源码运行，仅依赖 PostgreSQL，无需 Docker）
# 在你自己的 PowerShell 里运行：  ./langfuse/deploy/deploy-langfuse.ps1
# 前提：已安装 PostgreSQL 并可连接（DATABASE_URL 见 langfuse.env）
# ============================================================
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
# langfuse/server 位于 langfuse/ 目录内（gitignored，不污染仓库/IDE 索引）
$ServerDir = Join-Path $ScriptDir "..\server"
$EnvFile = Join-Path $ScriptDir "langfuse.env"
$EnvExample = Join-Path $ScriptDir "langfuse.env.example"

Write-Host "=== [0/6] 准备 langfuse.env（首次从模板生成，密钥自动随机）===" -ForegroundColor Cyan
if (-not (Test-Path $EnvFile)) {
  if (-not (Test-Path $EnvExample)) {
    Write-Host "缺少 langfuse.env.example 模板，无法生成配置。" -ForegroundColor Red; exit 1
  }
  # 从模板复制，并把所有 __GENERATE__ 占位符替换为随机密钥
  function New-RandomHex([int]$Bytes) {
    $buf = New-Object byte[] $Bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return -join ($buf | ForEach-Object { $_.ToString("x2") })
  }
  $content = Get-Content $EnvExample -Raw
  $content = $content -replace 'NEXTAUTH_SECRET=__GENERATE__', "NEXTAUTH_SECRET=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((New-RandomHex 24))))"
  $content = $content -replace 'SALT=__GENERATE__', "SALT=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((New-RandomHex 24))))"
  $content = $content -replace 'ENCRYPTION_KEY=__GENERATE__', "ENCRYPTION_KEY=$(New-RandomHex 32)"
  $content = $content -replace 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY=__GENERATE__', "LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-sp-$(New-RandomHex 8)"
  $content = $content -replace 'LANGFUSE_INIT_PROJECT_SECRET_KEY=__GENERATE__', "LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-sp-$(New-RandomHex 16)"
  Set-Content -Path $EnvFile -Value $content -Encoding UTF8 -NoNewline
  Write-Host "已生成 langfuse.env（安全密钥已随机化）。" -ForegroundColor Green
  Write-Host "请编辑 $EnvFile：" -ForegroundColor Yellow
  Write-Host "  1. 把 DATABASE_URL/DIRECT_URL 中的 CHANGE_ME 改成你本机 PostgreSQL 密码" -ForegroundColor Yellow
  Write-Host "  2. 把 LANGFUSE_INIT_USER_PASSWORD 的 CHANGE_ME 改成你想要的登录密码" -ForegroundColor Yellow
  Write-Host "  3. 把 PUBLIC/SECRET KEY 同步填入 Second Person 设置页的 Langfuse 配置" -ForegroundColor Yellow
  Write-Host "改完后重新运行本脚本。" -ForegroundColor Yellow
  exit 0
}
if (Select-String -Path $EnvFile -Pattern 'CHANGE_ME' -Quiet) {
  Write-Host "langfuse.env 中仍有 CHANGE_ME 占位符，请先填入真实值后重试。" -ForegroundColor Red; exit 1
}

Write-Host "=== [1/6] 检查运行时 ===" -ForegroundColor Cyan
foreach ($c in @("node", "pnpm", "git")) {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
    Write-Host "缺少 $c，请先安装后重试。" -ForegroundColor Red; exit 1
  }
}
node --version; pnpm --version

Write-Host "=== [2/6] 拉取 Langfuse v2 源码 ===" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $ServerDir "package.json"))) {
  git clone --branch v2 --depth 1 https://github.com/langfuse/langfuse.git $ServerDir
}
else {
  Write-Host "已存在 langfuse/server，跳过克隆。"
}

Write-Host "=== [3/6] 写入 .env 并加载到进程环境 ===" -ForegroundColor Cyan
Copy-Item $EnvFile (Join-Path $ServerDir ".env") -Force
# 将 .env 逐行加载为进程环境变量（Prisma/turbo 未必自动读 .env，必须显式导入）
Get-Content $EnvFile | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
  }
}
Write-Host "已加载 .env（DATABASE_URL / NEXTAUTH_* / LANGFUSE_INIT_* 已注入环境）"

Push-Location $ServerDir
try {
  $env:npm_config_engine_strict = "false"   # 兼容 Node 22（仓库声明 engines node=20）

  Write-Host "=== [4/6] 安装依赖（monorepo，较慢，请耐心）===" -ForegroundColor Cyan
  pnpm install

  Write-Host "=== [5/6] 数据库迁移（Prisma migrate deploy @ shared）===" -ForegroundColor Cyan
  pnpm run db:migrate
  if ($LASTEXITCODE -ne 0) {
    Write-Host "turbo db:migrate 失败，回退到 shared 包直接迁移 ..." -ForegroundColor Yellow
    pnpm --filter=shared exec prisma migrate deploy
  }

  Write-Host "=== [6/6] 构建并启动（监听 http://localhost:3001）===" -ForegroundColor Cyan
  pnpm run build
  Write-Host "构建完成，正在启动 Langfuse……首次启动会自动初始化项目与 API Key。" -ForegroundColor Green
  Write-Host "启动后访问 http://localhost:3001 （登录账号见 langfuse.env 的 LANGFUSE_INIT_USER_*）" -ForegroundColor Green
  pnpm run start
}
finally {
  Pop-Location
}
