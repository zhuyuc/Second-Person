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
| `think_mode` | `auto` / `quick` / `deep` | 缺失或未知值按 `auto` 处理 |

同一会话的生成串行执行。断线重连使用相同 `client_request_id` 读取服务端缓冲；只有 `POST /api/chat/cancel` 会取消正在执行的任务。

## 工具执行

Agent 规划后直接执行工具，不设置工具确认接口。每个调用仍经过参数校验、超时、结果脱敏、外部内容注入防护、空结果重试和 Replan。用户在设置或记忆页面主动发起的删除、恢复等界面操作，继续由页面确认组件保护。

## SSE 事件

契约版本：`2026-08-21`。每个事件使用：

```text
event: <event_name>
data: <JSON object>
```

| 事件 | 必填字段 | 用途 |
| --- | --- | --- |
| `queued` | `session_id` | 同会话排队提示 |
| `thinking_delta` | `text` | 处理进度摘要 |
| `mode_decision` | `requested_mode`, `effective_mode`, `reason` | 思考模式路由 |
| `memory_retrieved` | `count`, `titles` | 已选择的记忆摘要 |
| `analysis_progress` | `stage`, `status` | 问题建模和交付进度 |
| `delivery_progress` | `status`, `current`, `total` | 长文交付进度 |
| `quality_status` | - | 需求覆盖质量结果 |
| `tool_executing` | `tool_name`, `status` | 工具执行状态 |
| `tool_visual` | `type`, `data` | 工具生成的图形 |
| `content_delta` | `text` | 回复正文增量 |
| `citations` | `refs` | 回复引用 |
| `elicitation` | `tool_use_id`, `questions` | 需要用户补充的信息 |
| `elicitation_status` | `status` | 补充信息状态 |
| `handoff_ready` | `status` | 会话交接摘要状态 |
| `mood_updated` | `ai_mood` | 人格情绪快照 |
| `turn_completed` | `message_id` | 本轮持久化完成，附安全 `analysis_metadata` |
| `error` | `code`, `message` | 本轮异常或取消 |

`turn_completed` 和 `error` 是生成终态。前端必须兼容未知的附加字段；后端新增事件时，先更新 `SSE_EVENT_SPECS`、本文件和前端处理逻辑。

## 开发者调试记录

Langfuse 的 `developer_trace` 元数据包含模式决策、被选上下文、检索诊断、意图、工具结果、问题模型、质量结果及耗时。它是开发分析入口，不属于面向用户的 SSE 内容。
