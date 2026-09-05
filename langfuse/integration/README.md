# Langfuse 集成（对话全链路可观测性）

把 Second Person 的事件化 Agent 运行时从「黑盒」变为「可观测」：每一次对话轮次、每个模型步骤、每次工具执行和每一次大模型调用都会作为 trace / span / generation 上报到 [Langfuse](https://github.com/langfuse/langfuse)，可在 Langfuse UI 里查看调用树、输入输出、token 用量与耗时。

## 为什么是这种实现

本目录**故意命名为 `langfuse`**，但**不依赖** pip 的 `langfuse` SDK，而是直接对接 Langfuse 官方 **Ingestion REST API**（`POST /api/public/ingestion`，Basic Auth）。好处：

- 零依赖冲突：`import langfuse` 解析到本包，不会与 SDK 抢名字
- 零新增依赖：复用项目已有的 `httpx`
- 完全可控：上报为后台异步批量，失败只告警、绝不影响对话主链路
- 禁用态零开销：未配置密钥时全部为空实现

## 追踪层级（按时间线）

### 1. 对话主链 `agent.turn`

一次用户发消息 → 助手回复，按发生顺序大致如下：

```
trace: agent.turn
├─ span: agent.decision              （能力/路由决策，按需）
├─ span: context.assemble            （组装上下文；output.retrieval 含候选明细）
│  ├─ span: memory.skipped           （短查询短路时，仅此节点）
│  ├─ span: memory.embed             （检索向量 + FTS 并行）
│  ├─ span: memory.presearch         （Hybrid 预筛 / 可选 fallback）
│  ├─ span: memory.graph             （图扩展）
│  └─ span: memory.refine            （精筛；内嵌 generation: llm.*）
├─ span: context.compact             （step≥2 时检查/执行压缩；可含 LLM）
├─ span: agent.step                  （模型步骤，可多轮）
│  └─ generation: llm.*              （本步模型调用）
└─ span: tool_execute                （工具执行，按需；挂在当前活跃 observation 下）
```

`context.assemble` 的 `output.retrieval`（有记忆检索时）大致包含：

- `presearch_candidates`：Hybrid 预筛召回（id/title/summary/score）
- `refine_pool`：进入精筛的候选（带 selected 标记）
- `refine_rejected` / `selected_ids` / `injected`：落选与最终注入
- `vector_hits` / `fts_hits` / `refine_path` / `retrieval_time_ms` 等诊断量

前端记忆检索进度与上述 `memory.*` span 阶段对齐，便于对照 UI 与 Langfuse。

### 2. 回合旁路 / 后台（独立 trace）

这些任务不在 `agent.turn` 内（或 turn 已结束），各自 `trace_start`，其内 LLM 才能挂上 generation：

| Trace 名 | 触发时机 | 说明 |
|---|---|---|
| `title_generation` | 首条消息后异步 | span `title_generation` + `llm.title_gen` |
| `mood.after_turn` | 回合结束后异步 | span `mood.judge` + 情绪判定 LLM |
| `handoff.summary` | 跨会话交接 | span `handoff.summary_generation` 等 |
| `scheduler.{task_id}` | 定时/手动跑任务 | 回顾/Lint/画像/备份等整任务包一层 |
| `ingest.file` | 文档/图片导入 | Distiller 提炼挂在此 trace 下 |
| `user_feedback` / `attachment_upload` | 反馈、附件 | 路由侧轻量 trace |

> 机制提醒：`generation_start` / `span_start` 在没有活跃 `_active_trace` 时是 noop。后台 LLM 必须先 `trace_start`，否则会「调了模型但 Langfuse 空白」。

## 启用方式

### 1. 准备一个 Langfuse 实例

**自托管（推荐本地）**：

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d          # 默认监听 http://localhost:3000
```

打开 `http://localhost:3000` 注册 → 新建 Project → 在 Project Settings 里创建 API Keys，得到 `pk-lf-...` 与 `sk-lf-...`。

> 也可直接用云端：`https://cloud.langfuse.com`（EU）或 `https://us.cloud.langfuse.com`（US）。

### 2. 配置密钥（二选一）

**方式 A：环境变量**（优先级最高）

```bash
# Windows PowerShell
$env:LANGFUSE_ENABLED = "1"
$env:LANGFUSE_HOST = "http://localhost:3000"
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-xxxxxxxx"
$env:LANGFUSE_SECRET_KEY = "sk-lf-xxxxxxxx"
python start.py
```

**方式 B：`data/config.yaml`**

```yaml
langfuse_enabled: true
langfuse_host: "http://localhost:3000"
langfuse_public_key: "pk-lf-xxxxxxxx"
langfuse_secret_key: "sk-lf-xxxxxxxx"
```

> 只要提供了 public/secret key，即使不设 `langfuse_enabled` 也会**自动开启**；
> 不配置任何密钥时**默认禁用**，不产生任何网络请求。

### 3. 使用

正常发起对话即可。稍等几秒（后台批量上报间隔默认 3s），刷新 Langfuse UI 的 Tracing 页面，即可看到名为 `agent.turn` 的完整调用树。

## 可选环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `LANGFUSE_ENABLED` | 显式开关（`1/true/on`） | 有密钥则自动开 |
| `LANGFUSE_HOST` | Langfuse 地址 | `http://localhost:3000` |
| `LANGFUSE_PUBLIC_KEY` | 项目 Public Key | — |
| `LANGFUSE_SECRET_KEY` | 项目 Secret Key | — |
| `LANGFUSE_FLUSH_INTERVAL` | 后台上报间隔（秒） | `3` |
| `LANGFUSE_FLUSH_BATCH` | 批量阈值 | `20` |
| `LANGFUSE_RELEASE` | 版本标记（可选） | — |

## 代码结构

| 文件 | 职责 |
|---|---|
| `config.py` | 从环境变量 / config.yaml 读取配置 |
| `client.py` | Ingestion REST 客户端：内存队列 + 后台异步批量上报 |
| `tracer.py` | 高层追踪器 `PipelineTracer`：trace / span / generation，contextvars 传播父子关系，禁用态空实现 |
| `__init__.py` | 导出 `init_tracer` / `get_tracer` / `PipelineTracer` |

集成点（本目录之外）：

- `app/container`：`init_tracer()` 初始化，startup 启动、shutdown 停机
- `agent/turn_runtime.py`：`agent.turn` + `context.assemble` / `context.compact` / `agent.step` / `agent.decision`
- `memory/retriever.py`：`memory.embed` / `presearch` / `graph` / `refine`（挂在 assemble 下）
- `agent/tool_executor.py`：`tool_execute`
- `infrastructure/llm_provider.py`：`generation_start`（模型、输入输出、token）
- `app/services/chat_service.py`：`title_generation` / `handoff.summary`
- `agent/core.py`：`mood.after_turn`
- `scheduler/scheduler.py`：`scheduler.{task_id}`
- `scheduler/ingest.py`：`ingest.file`
