# 本地部署 Langfuse v2（对接 Second Person）

目标：在**本机**从源码跑起 Langfuse v2（仅依赖 PostgreSQL，**无需 Docker、无需联网托管**），让 Second Person 把对话流转上报过去，在 Langfuse UI 看调用树。

## 分工

- **数据库（你在做）**：安装并启动本机 PostgreSQL。
- **其余（已由本仓库准备好）**：源码拉取脚本 + `langfuse.env.example` 模板（首次运行部署脚本自动生成 `langfuse.env` 并随机化密钥）+ 一键部署脚本 + 应用侧接入配置。

## 你需要做的 3 步

### 1. 建好数据库

PostgreSQL 装好后，创建一个空库（名字用 `langfuse`）：

```powershell
createdb -U postgres langfuse
# 或用 pgAdmin 图形界面新建数据库 langfuse
```

然后运行一次 `./langfuse/deploy/deploy-langfuse.ps1`，首次会从模板生成 `langfuse.env`（安全密钥自动随机）；
按提示编辑其中的 **DATABASE_URL / DIRECT_URL**（改成你本机的用户名/密码/端口）与 **LANGFUSE_INIT_USER_PASSWORD**（登录密码）。

### 2. 一键部署并启动 Langfuse

```powershell
./langfuse/deploy/deploy-langfuse.ps1
```

脚本会：克隆 v2 源码 → 写入 `.env` → `pnpm install` → 数据库迁移 → 构建 → 启动在 **<http://localhost:3001**。>
首次启动**自动创建**组织/项目/管理员/API Key（无需手动点 UI）。

- Langfuse 登录：账号密码见你本地 `langfuse.env` 的 `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`

### 3. 打开 Second Person 的上报开关

在 Second Person 设置页启用 Langfuse，并填入接入信息（均来自你本地 `langfuse.env`）：

| 项 | 取值 |
| --- | --- |
| Host | <http://localhost:3001> |
| Public Key | `langfuse.env` 的 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` |
| Secret Key | `langfuse.env` 的 `LANGFUSE_INIT_PROJECT_SECRET_KEY` |

重启 Second Person（`python start.py`）后发起对话，几秒后在 `http://localhost:3001` 的 Tracing 页即可看到 `chat.turn` 完整流转树。

> 说明：Second Person 用的是与 Langfuse v2 完全匹配的 Ingestion API（`/api/public/ingestion`）上报，
> 埋点已覆盖对话八步（context_load / memory_retrieval / intent_parse / tool_execution /
> response_synthesis / post_process）与每次 LLM 调用（generation，含 token 用量）。

## 常见问题

- **端口冲突**：默认用 3001（3000 已被占用）。如需改端口，改 `langfuse.env` 的 `PORT` 与 `NEXTAUTH_URL`，并同步改 `data/config.yaml` 的 `langfuse_host`。
- **`db:migrate` 脚本名不同**：脚本会自动回退到 `prisma migrate deploy`；若仍失败，把报错发我，我按克隆到的 v2 实际脚本适配。
- **Node 版本**：本机 Node 22 已通过 `engine_strict=false` 兼容；若安装阶段报引擎错误，可用 `nvm use 20`。
