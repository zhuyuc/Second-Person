# Second Person 工程规则

本文件定义代码、接口、数据和可观测性的工程边界。它与
`docs/API_CONTRACT.md`、`docs/PROMPT_REGISTRY.md`、`docs/UI_UX_SPEC.md`
共同构成开发规则的唯一入口。

## 1. 规则优先级

1. 当前任务的用户要求和运行环境约束；
2. 本文件的目录职责和跨层规则；
3. 专项契约：API/SSE、Prompt、UI；
4. 可执行实现与自动化测试；
5. 产品方案和历史设计文档只解释产品目标，不定义接口字段或实现细节。

当文档与代码发生分歧时，先修复专项契约和测试，再更新说明文档。

## 2. 后端目录职责

| 目录 | 职责 | 允许依赖 |
| --- | --- | --- |
| `app/routes` | HTTP/SSE 适配、输入校验、响应封装 | `app/contracts` 与各业务服务 |
| `app/contracts.py` | 公开 HTTP 请求模型和规范化 | `agent/contracts` 中的共享枚举 |
| `agent` | 事件化任务编排、推理等级、工具调度、回复合成 | `memory`、`tools`、`infrastructure` |
| `memory` | 记忆写入、检索、压缩、索引和生命周期 | `infrastructure` |
| `tools` | 工具规格、参数校验和执行实现 | `infrastructure` |
| `connectors` | MCP 和外部连接器生命周期 | `tools`、`infrastructure` |
| `soul`、`user_profile` | 人格、用户画像及其演化 | `memory`、`infrastructure` |
| `gateway` | IM 渠道适配，不承载 Agent 决策 | `agent`、`app` 服务 |
| `scheduler` | 后台定时任务和恢复任务 | 各领域公开服务 |
| `infrastructure` | 日志、事件、配置、时间、数据库、SSE 契约等横切能力 | 不依赖业务层 |

路由层只处理传输协议和参数，不在路由中实现 Agent 决策、记忆检索或工具业务逻辑。

## 3. 前端目录职责

| 目录 | 职责 |
| --- | --- |
| `frontend/src/api` | 第一方 HTTP、SSE、上传等传输封装和协议解析 |
| `frontend/src/stores` | 跨页面共享状态 |
| `frontend/src/composables` | 可复用交互、浏览器能力和状态组合 |
| `frontend/src/views` | 页面级编排和展示状态 |
| `frontend/src/components` | 可复用 UI 组件 |
| `frontend/src/utils` | 无副作用的格式化、映射和纯函数 |

页面和组件通过 `api` 调用本应用接口，不直接拼接 `/api` 地址或调用 `fetch`。浏览器原生能力和独立第三方服务可以保留在专用 composable 中，例如定位和逆地理编码。

视觉、组件、可访问性和响应式规则以 `docs/UI_UX_SPEC.md` 为准；全部构建产物写入 `app/static`，不在其中手工修改业务代码。

## 4. 协议与事件

- JSON 成功响应使用 `{code: 200, data}`；错误响应使用 `{code, message, trace_id, details}`。
- 对话请求由 `app/contracts.py` 的 `ChatSendRequest` 规范化。
- 对话 SSE 事件由 `infrastructure/sse_contract.py` 注册并在路由出口验证；EventBus 事件只用于后端模块间通信。
- `off`、`low`、`high`、`max` 是唯一的公开推理等级，前后端分别从 `agent/contracts.py` 和 `frontend/src/utils/chatContract.js` 使用同一语义。
- 普通 Agent 轮次使用 `agent_turns` 和 `agent_events` 作为可恢复事实链：宿主构造上下文和 schema，模型提出工具调用，宿主执行或等待确认，再把结果事件回填给下一步模型。
- `agent_events` 中的 `decision.notice` 是宿主解释层，`context.notice` 是注入下一步模型的提醒；两者都不冒充模型原生 reasoning。
- 工具参数校验、超时、脱敏、注入防护和重试属于执行质量保障。`write`、`destructive`、`external_side_effect` 工具必须由 `ToolPolicy` 在执行前确认；前端无权直接声明工具或授权执行。

## 5. 数据与写入

- `data/` 是运行时用户数据目录，包含 Markdown 主副本、SQLite 派生索引和配置，不纳入源码提交。
- 记忆、画像、人格、技能等主副本写入通过 `memory/FileWriter` 编排；SQLite 索引由同一写入流程维护。
- 非用户明确保存的记忆必须先经过 `memory/MemoryWriteGate` 候选池、证据和敏感信息门禁；候选状态先于 FileWriter 写入状态推进。
- `app/static/` 是 `frontend` 的 Vite 构建目标；修改前端后必须重新构建并通过静态资源一致性检查。
- 所有持久化时间使用 `infrastructure/timeutil.py`。

## 6. 开发者观测

- 每个 LLM 调用显式声明 `source`，每轮 Agent 任务使用 Langfuse trace/span/generation 链路。
- 开发者通过 Langfuse 查看结构化调试记录：推理等级、上下文选择、检索、工具、确认、性能和结束原因。
- `infrastructure/developer_trace.py` 定义调试摘要字段；它只记录可验证的决策和执行证据，不保存原始隐藏推理 token 流。
- Langfuse 默认使用 `redacted` 内容模式；除非显式启用 `full`，调用输入输出只上传长度和结构元数据。
- 面向用户的处理面板只展示工具生命周期、宿主决策通知和可验证结论。
- `reasoning_delta` 只有在 Provider 明确返回 reasoning block 时才展示；工具选择原因使用 `decision_notice` 单独展示，避免把工具状态混入“模型思考”。

## 7. 自动化门禁

- `tests/test_prompt_registry.py`：Prompt 文件、注册表和 LLM 调用点一致。
- `tests/test_static_health.py`：后端静态检查、统一时间和 LLM source 约束。
- `tests/test_api_envelope.py`：错误信封、聊天输入和 SSE 输出契约。
- `tests/test_contract_rules.py`：目录、文档、SSE 注册和前端 API 分层。
- 前端执行 `npm run build` 以及既有前端语义测试；构建产物由 `tests/test_static_assets_consistency.py` 校验。
