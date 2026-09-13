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
├─ span: agent.step                  （模型步骤，可多轮；覆盖本步 LLM + 本步工具）
│  ├─ generation: llm.agent_step     （本步对话模型调用）
│  └─ span: tool_execute             （本步工具；可含 llm.video_gen / llm.image_gen）
└─ span: mood.judge                  （最终回复后异步续写；同 trace，排序在末尾是预期）
   └─ generation: llm.mood_judge
```

> 时间线说明：`mood.judge` 故意挂在最终 `agent.step` 之后（`phase=after_turn`），
> 不是乱序。工具必须是对应 `agent.step` 的子节点；若看到 `tool_execute` 与 step 平级，属回归缺陷。


`context.assemble` 的 `output.retrieval`（有记忆检索时）大致包含：

- `presearch_candidates`：Hybrid 预筛召回（id/title/summary/score）
- `refine_pool`：进入精筛的候选（带 selected 标记）
- `refine_rejected` / `selected_ids` / `injected`：落选与最终注入
- `vector_hits` / `fts_hits` / `refine_path` / `retrieval_time_ms` 等诊断量

前端记忆检索进度与上述 `memory.*` span 阶段对齐，便于对照 UI 与 Langfuse。

### 2. 同轮续写 vs 独立操作

**同一轮用户消息（一次 `agent.turn`）只对应一条 Langfuse 记录。**  
回合结束后的情绪判定等后台续写，通过 `attach_trace(langfuse_trace_id)` 挂到该 turn，只增加节点（如 `mood.judge`），**禁止**再开 `mood.after_turn` 之类的第二条 trace。

下列属于**另一类操作**（不是同一次用户回合的处理树），各自独立 trace，并用 `sessionId` 归到同一会话下对照：

| Trace 名 | 触发时机 | 说明 |
|---|---|---|
| `title_generation` | 会话首条消息后异步 | 标题生成，与当轮 agent 树并列但不同操作 |
| `handoff.summary` | 跨会话交接 | 摘要生成 |
| `scheduler.{task_id}` | 定时/手动跑任务 | 回顾/Lint/画像/备份等 |
| `ingest.file` | 文档/图片导入 | Distiller 提炼 |
| `user_feedback` / `attachment_upload` | 反馈、附件 | 路由侧轻量 trace |
| `workshop.render` | 视频工坊点「生成视频」 | 与聊天出片共用执行体；独立于对话 turn |

> 机制提醒：`generation_start` / `span_start` 在没有活跃 `_active_trace` 时是 noop。同轮后台必须 `attach_trace`；其它后台操作才 `trace_start`，否则会「调了模型但 Langfuse 空白」。

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

### Prompt 缓存观测

展开 `agent.turn > agent.step > llm.agent_step`，可在 metadata 中查看
`prompt_cache`：`change_reason` 说明本步前缀为何变化，`prefix_reused` 表示
system prompt 与工具 schema 是否复用，四个 `*_hash` 用于关联同一前缀而不暴露
原文。`llm.agent_step` 完成后还会记录 `cache_read_tokens`、
`cache_write_tokens` 和 `cache_hit_rate`，可与首 token 和整体耗时一起分析。

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
