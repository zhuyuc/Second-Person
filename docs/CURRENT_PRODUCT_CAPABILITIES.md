# 当前产品能力索引

最后核对：2026-09-07。本文只描述已在生产代码中存在入口的能力；接口字段和
事件以 `docs/API_CONTRACT.md` 为准，路由完整清单以运行中的 `/openapi.json` 为准。

## 当前可用能力

| 能力域 | 当前行为 | 实现入口 |
| --- | --- | --- |
| 对话 | 同会话串行的事件化 Agent 循环，支持流式回复、工具调用、停止、重试、编辑/重新生成和会话交接 | `agent/core.py`、`agent/turn_runtime.py`、`app/routes/chat.py` |
| 上下文 | 稳定 system/tool 前缀 + messages 尾部 `context.*`（情绪状态/位置/约束/记忆/项目等）；token 压力触发摘要压缩；上下文超限且尚未输出时压缩并重试一次；MCP 默认仅项目会话注入 | `agent/prompt_assembler.py`、`agent/turn_events.py`、`agent/compaction_engine.py` |
| 记忆 | Hybrid 预筛、图扩展、LLM 精筛、记忆候选门禁、文件主副本和项目隔离 | `memory/retriever.py`、`memory/write_gate.py`、`memory/file_writer.py` |
| 项目工作区 | 项目 CRUD、归档、目录浏览、项目记忆隔离和四档文件工具权限 | `agent/projects.py`、`app/routes/projects.py` |
| 工具与连接器 | 内置工具和 MCP（stdio / Streamable HTTP）工具；本地单用户模式下由宿主直接执行并做参数、超时、脱敏与注入防护 | `tools/`、`connectors/` |
| 人格与设置 | SOUL、画像、输出风格、Provider/模型槽位、连接器、渠道、任务、备份和用量管理 | `app/routes/soul.py`、`app/routes/settings.py` |
| 可观测性 | Langfuse trace/span/generation、Prompt 前缀指纹和 provider cache usage；敏感字段在上报前脱敏 | `langfuse/integration/`、`infrastructure/prompt_cache.py` |

## 文档状态

| 文档 | 用途 | 是否定义当前实现 |
| --- | --- | --- |
| `README.md` | 安装、运行与高层能力说明 | 是 |
| `API_CONTRACT.md` | HTTP/SSE 公开契约 | 是 |
| `ARCHITECTURE_RULES.md` | 工程边界与开发规则 | 是 |
| `PROMPT_REGISTRY.md` | 已加载 Prompt 与 LLM 调用点 | 是 |
| `PROJECTS.md` | 项目工作区已交付能力 | 是 |
| `UI_UX_SPEC.md` | 前端实现规范 | 是 |
| `自动压缩-架构方案.md`、`精筛提速-决策记录.md` | 已落地优化的设计记录 | 是，具体契约仍以代码为准 |
| `SecondPerson-全系统产品方案.md`、`会话上下文管理方案-v2.md`、`SecondPerson-通用问题解决系统优化方案.md` | 产品目标与历史设计 | 否；仅用于理解目标和已明确标注的状态 |
| `参数收敛优化方案-v1.md`、`客户端化架构调研报告.md`、`DOC_REWRITE_V7.3.md` | 方案草稿或历史记录 | 否 |

历史/方案文档中的模块名、阈值、端点和功能清单不应直接作为开发或验收依据；应先以本表
列出的运行型文档和代码核对。
