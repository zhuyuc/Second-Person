# Second Person

本地运行的个人智能体助手。核心差异化能力是**记忆宫殿**：三层记忆系统 + 八步对话流水线 +
自我进化的 Agent 人格。所有数据以 md 文件为主副本存本地，SQLite 仅作派生索引，复制 `data/` 即完整备份。

- Python 3.10+ / FastAPI / SQLite（标准库，含 FTS5）/ numpy / Vue 3
- 零数据库安装：不需要 PostgreSQL / Redis / Docker
- 本地单用户，无需认证

## 快速开始

### 一键部署（推荐）

**前置要求：仅 Python 3.10+**（其他全部可选，见下方分层说明）

```bash
# 1) 拉取代码
git clone https://github.com/zhuyuc/Second-Person.git && cd Second-Person

# 2) 一键自举：创建主 venv + Embedding 隔离 venv + 下载 BGE-M3 本地模型（~2.3GB）
.\setup.ps1            # Windows
./setup.sh             # Linux / macOS

# 3) 启动，浏览器自动打开 http://localhost:8000
python start.py
```

不想装本地 Embedding 模型？用 `.\setup.ps1 -NoEmbedding`（或 `./setup.sh --no-embedding`），
记忆检索自动降级为 FTS5 全文搜索，其余功能不受影响。

### 前置要求分层

| 层级 | 依赖 | 用途 |
| --- | --- | --- |
| **必需** | Python 3.10+ | 核心应用（前端构建产物已入库，免装 Node） |
| 可选 | NVIDIA GPU | 本地 Embedding 加速（无 GPU 自动用 CPU） |
| 可选 | Node 18+ | 仅修改前端时需要（`cd frontend && npm install && npm run build`） |
| 可选 | PostgreSQL / Redis / pnpm | 仅自托管 Langfuse 链路观测时需要，参照 `langfuse/deploy/README.md` |

首次启动进入引导：配置对话模型（必填，测试通过才继续）→ 配置 Embedding（可跳过，先用全文搜索）
→ 欢迎对话 → 确认初始人格。首启会自动生成 `data/` 目录骨架与默认配置，无需手动准备。

> 敏感信息隔离：`data/`（含密钥与全部用户数据）、`embedding/models/`、所有 venv、
> `langfuse/deploy/langfuse.env`（真实密钥）均已被 `.gitignore` 排除，不会随仓库传播；
> Langfuse 配置只入库脱敏模板 `langfuse.env.example`。

## 命令

```bash
python start.py                 # 启动（默认 8000，被占用依次尝试 8001-8010）
python start.py --port 8001     # 指定端口
python start.py --data-dir D:/second-person-data  # 指定运行数据目录
python start.py --rebuild-index # 从 md 文件重建 SQLite 索引
python start.py --recompile     # 从 raw_docs + 对话原文重建记忆 md（停机）
```

## 架构

五层分层 + 四横切基础设施：

- **用户交互层**：Web UI（Vue 3）+ IM 接入（飞书 / 钉钉 / Telegram）
- **Agent Core**：Soul 人格 → 意图解析 → DAG 调度 → 工具执行 → 响应合成（八步流水线）
- **工具执行层**：内置工具（Path A 进程内）+ MCP 协议工具（Path B 外部）
- **三层记忆系统**：L1 工作记忆 → L2 会话记忆 → L3 记忆宫殿
- **存储层**：SQLite palace.db + numpy 向量 + FTS5 + md 文件系统
- **横切**：EventBus / LLM Provider 抽象 / ConfigManager / 可观测性

## 目录结构

```
start.py / requirements.txt / pyproject.toml
app/            Web 应用（FastAPI + 路由 + 容器）
agent/          Agent Core（core / intent_parser / dag_scheduler / tool_executor / ...）
memory/         记忆宫殿（palace / retriever / distiller / linker / lint / vector_store / ...）
tools/          工具系统（base / builtin / sandbox / hooks / web_fetch）
connectors/     MCP 连接器（mcp_client / credential_store / manager）
soul/           人格系统（soul_manager / skill_manager / constants）
user_profile/   用户画像
scheduler/      调度 / 备份 / 文档导入
gateway/        多端接入（platforms / notifications）
infrastructure/ event_bus / llm_provider / config_manager / observability / db / context_entry
plugins/        插件扩展（工具 / Provider / EventBus 三条扩展路径）
migrations/     SQLite 迁移脚本
frontend/       Vue 3 + Vite 前端源码（构建产物输出到 app/static/）
data/           所有用户数据（md 主副本 + palace.db + config.yaml）
```

工程边界和文件放置规则见 `docs/ARCHITECTURE_RULES.md`；公开接口和 SSE
事件见 `docs/API_CONTRACT.md`；Prompt 与 UI 分别遵循
`docs/PROMPT_REGISTRY.md` 和 `docs/UI_UX_SPEC.md`。

## 数据与备份

`data/` 目录即全部数据。每天 02:00 自动备份，保留最近 3 份。`palace.db` 可从 md 文件用
`--rebuild-index` 完全重建；即使记忆 md 全丢，也可用 `--recompile` 从对话原文兜底恢复。
