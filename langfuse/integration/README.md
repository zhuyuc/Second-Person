# Langfuse 集成（对话全链路可观测性）

把 Second Person 的事件化 Agent 运行时从「黑盒」变为「可观测」：每一次对话轮次、每个模型步骤、每次工具执行和每一次大模型调用都会作为 trace / span / generation 上报到 [Langfuse](https://github.com/langfuse/langfuse)，可在 Langfuse UI 里查看调用树、输入输出、token 用量与耗时。

## 为什么是这种实现

本目录**故意命名为 `langfuse`**，但**不依赖** pip 的 `langfuse` SDK，而是直接对接 Langfuse 官方 **Ingestion REST API**（`POST /api/public/ingestion`，Basic Auth）。好处：

- 零依赖冲突：`import langfuse` 解析到本包，不会与 SDK 抢名字
- 零新增依赖：复用项目已有的 `httpx`
- 完全可控：上报为后台异步批量，失败只告警、绝不影响对话主链路
- 禁用态零开销：未配置密钥时全部为空实现

## 追踪层级

```
trace: agent.turn               （一次对话轮次，带 session_id / 用户输入 / 最终回复）
├─ span: context.assemble       （宿主上下文、静态 prompt 与动态 prompt）
├─ span: agent.step              （一次模型/工具循环步骤）
│  └─ generation: llm.agent_step （模型调用，含 token 用量）
├─ span: agent.decision          （宿主能力选择和决策摘要）
├─ span: tool_execute            （工具权限、执行、脱敏与结果回填）
└─ span: handoff.summary_generation（跨会话摘要生成，按需出现）
```

此外，所有非对话链路的模型调用（标题生成、压缩、回顾、画像、记忆提炼和 handoff 摘要）也会被记录为 generation。

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

正常发起对话即可。稍等几秒（后台批量上报间隔默认 3s），刷新 Langfuse UI 的 Tracing 页面，即可看到名为 `chat.turn` 的完整调用树。

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

集成点（本目录之外的最小改动）：

- `app/container.py`：`init_tracer()` 初始化，startup 启动、shutdown 停机
- `agent/turn_runtime.py`：`agent.turn` trace，各模型/工具步骤 span
- `infrastructure/llm_provider.py`：`chat()` / `stream()` 记录 generation（模型、输入输出、token）
