# API 与 SSE 契约

本文件描述浏览器和服务端的公开协议。实现来源为 `app/contracts.py` 和
`infrastructure/sse_contract.py`；路由清单由 FastAPI 的 `/openapi.json` 生成。

## HTTP 信封

成功：

```json
{"code": 200, "data": {}}
```

错误：

```json
{"code": 400, "message": "错误说明", "trace_id": "...", "details": {}}
```

## 对话请求

`POST /api/chat/send` 使用 SSE 返回本轮事件。请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `session_id` | string | 可选，缺省时创建会话 |
| `message` | string | 当前用户消息 |
| `client_request_id` | string | 可选重连键，最长 120 字符 |
| `images` | string[] | 可选图片 data URI |
| `regenerate_message_id` / `edit_message_id` | positive integer | 可选版本操作目标 |
| `location` | string | 可选位置摘要，最长 60 字符 |
| `handoff_path` | string | 可选会话交接摘要路径，最长 240 字符 |
| `reasoning_effort` | `off` / `low` / `high` / `max` | 单轮推理预算；缺省为 `high` |

同一会话的生成串行执行。断线重连使用相同 `client_request_id` 读取服务端缓冲；只有 `POST /api/chat/cancel` 会取消正在执行的任务。

## 任务与工具执行

正常对话使用短的事件化循环：宿主组装上下文和工具 schema，模型选择是否提出工具调用，宿主按工具策略执行，然后把工具结果事件投影回下一步模型上下文。`agent_events` 是单轮模型上下文的事实来源；`conversations` 保留用户可见消息历史。

系统提示词由 `agent/prompt_assembler.py` 统一组装：运行时契约、安全与权限、输出契约、工具规则、SOUL/画像/技能等静态 block 先输出；记忆、handoff、时间、位置和本轮约束等动态 block 始终位于 system prompt 尾部。之后才追加会话历史和本轮事件消息，工具 schema 通过请求的 `tools` 参数独立传递。

工具 schema 由宿主程序注册，前端只传用户消息与 `reasoning_effort`，不能指定工具或绕过策略。`read` 工具可直接执行；`write`、`destructive` 以及未审核 MCP 工具会创建持久化确认记录并等待用户。确认接口为 `POST /api/chat/turns/{turn_id}/approvals/{approval_id}`，请求体为 `{"approved": true|false}`。

## 记忆候选治理

非用户明确要求保存的内容先进入候选池，不直接写入长期记忆：

| 路由 | 方法 | 说明 |
| --- | --- | --- |
| `/api/memory/candidates` | GET | 按 `status` 查看候选，支持 `pending`、`approved`、`written`、`rejected`、`expired`、`all` |
| `/api/memory/candidates/{candidate_id}/confirm` | POST | 用户确认候选，达到写入门禁后进入 FileWriter |
| `/api/memory/candidates/{candidate_id}/reject` | POST | 拒绝候选并进入同指纹保护期，请求体可带 `reason` |
| `/api/memory/candidates/expire` | POST | 执行候选 TTL 清理 |

候选状态和长期记忆写入状态分离；客户端不得把 `pending` 或 `approved` 展示为已保存记忆。

## SSE 事件

契约版本：`2026-08-24`。每个事件使用：

```text
event: <event_name>
data: <JSON object>
```

| 事件 | 必填字段 | 用途 |
| --- | --- | --- |
| `queued` | `session_id` | 同会话排队提示 |
| `reasoning_delta` | `text`, `source` | Provider 明确返回的原生 reasoning 增量；没有该事件不代表宿主没有执行决策 |
| `decision_notice` | `stage`, `actor`, `source`, `reason_code`, `summary` | 宿主基于工具注册信息和能力目录推断的可验证摘要，不伪造隐藏思维链 |
| `turn_started` | `turn_id`, `reasoning_effort` | 已创建持久化任务轮次 |
| `step_started` | `turn_id`, `step` | 模型/工具循环的新步骤 |
| `tool_executing` | `tool_name`, `status` | 工具执行状态 |
| `tool_pending_approval` | `turn_id`, `approval_id`, `tool_name`, `risk_level` | 等待确认的副作用操作 |
| `tool_blocked` | `turn_id`, `tool_name`, `reason` | 被宿主策略阻断 |
| `tool_result` | `turn_id`, `tool_name`, `ok` | 工具结果摘要 |
| `tool_visual` | `type`, `data` | 工具生成的图形 |
| `content_delta` | `text` | 回复正文增量 |
| `citations` | `refs` | 回复引用 |
| `handoff_ready` | `status` | 会话交接摘要状态 |
| `mood_updated` | `ai_mood` | 人格情绪快照 |
| `turn_completed` | `message_id` | 本轮持久化完成，附安全 `analysis_metadata` |
| `error` | `code`, `message` | 本轮异常或取消 |

`turn_completed` 和 `error` 是生成终态。前端必须兼容未知的附加字段；后端新增事件时，先更新 `SSE_EVENT_SPECS`、本文件和前端处理逻辑。

## 开发者调试记录

Langfuse 的 `developer_trace` 元数据记录可验证的运行事实（任务 ID、推理等级、步骤数、调用数、耗时和结束原因），不记录模型隐藏推理。调用输入输出全量上报（本地自托管，无隐私外泄）。它是开发分析入口，不属于面向用户的 SSE 内容。
