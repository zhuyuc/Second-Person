# Second Person 全系统产品方案

> 本方案由当前系统代码（v1.0.0）逆向提炼生成，与实际实现逐一对齐。
> 生成日期：2026-07-28

---

## 一、产品定位

**Second Person（第二人格）** 是一个**本地运行的个人智能体助手**，核心理念是"记忆宫殿 + 八步对话流水线"：

- **有记忆**：三层记忆架构（工作记忆 / 会话记忆 / 记忆宫殿），越用越懂用户；
- **有人格**：SOUL 双层人格系统（稳定核 + 演化风格），根据用户反馈持续演化；
- **有能力**：内置 11 个工具 + MCP 连接器无限扩展 + 联网搜索 + 文档生成；
- **本地优先**：所有数据（记忆 md、SQLite、向量、凭证）留在本机；LLM 可接任意云端 / 本地供应商；
- **多端接入**：Web 主界面 + Telegram / 飞书 / 钉钉 IM 渠道；
- **自我维护**：5 类系统 Agent 在夜间自动回顾、体检、去重、重建画像，无需人工干预。

**目标用户**：需要一个长期陪伴、持续积累个人知识与偏好的私人 AI 顾问 / 知识管理伙伴的个人用户（单用户、本机部署）。

---

## 二、系统架构总览

### 2.1 分层架构

```text
┌────────────────────────────────────────────────────────────┐
│  接入层  Web UI (Vue3) │ Telegram │ 飞书 │ 钉钉 │ REST API │
├────────────────────────────────────────────────────────────┤
│  应用层  FastAPI 路由 (chat/memory/settings/soul/misc)      │
│          AppContainer 依赖注入容器 · SSE 流式 · 断线重连缓冲 │
├────────────────────────────────────────────────────────────┤
│  编排层  AgentCore 八步对话流水线                            │
│          意图识别 → DAG 调度 → 工具执行 → 响应合成 → 后置处理│
├──────────────┬──────────────┬──────────────┬───────────────┤
│  记忆系统     │  人格系统     │  工具系统     │  系统 Agent   │
│  提炼/检索/   │  SOUL/画像/  │  内置+MCP/   │  回顾/Lint/   │
│  生命周期/图谱│  输出画像/技能│  沙箱/Hook    │  画像/输出画像 │
├──────────────┴──────────────┴──────────────┴───────────────┤
│  基础设施  SQLite(单写线程) · FileWriter(单写者队列) ·       │
│           LLM Provider(熔断/重试) · EventBus · 调度器 ·     │
│           配置管理 · 凭证加密 · Langfuse 可观测 · 备份       │
├────────────────────────────────────────────────────────────┤
│  存储层  md 文件(唯一真相源) + SQLite 派生索引 + numpy 向量  │
└────────────────────────────────────────────────────────────┘
```

### 2.2 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | Python ≥3.10、FastAPI + Uvicorn、sse-starlette（SSE）、httpx |
| 前端 | Vue 3.4 + Vite 5 + Pinia + vue-router（hash 路由）、marked、mermaid、sigma 3 + graphology（图谱 WebGL 渲染） |
| 存储 | SQLite（WAL + 单写线程 + 写队列 + 组提交）、md 文件为唯一真相源、numpy 内存向量矩阵 |
| 向量 | 本地 BGE-M3 微服务（隔离 venv，OpenAI 兼容接口，端口 8100）；可切换任意云端 embedding |
| LLM | openai_compatible / anthropic / google / custom 四类供应商，按模型独立熔断 |
| 文档解析 | pdfplumber / pypdf / python-docx / beautifulsoup4 / readability-lxml / RapidOCR（可选） |
| 可观测 | 自研 Langfuse 轻量客户端（批量协议）+ 本地自托管 Langfuse 服务器（端口 3001） |
| 加密 | cryptography Fernet + data/.master_key（权限锁定） |

### 2.3 核心设计原则

1. **md 即一等公民**：记忆、人格、画像、技能、压缩摘要、系统 prompt 全部为 md 文件；SQLite 只是派生索引，可随时从 md 重建（`--rebuild-index`）。
2. **统一单写者**：所有 md 与索引写入只经 FileWriter 队列串行执行；所有 SQLite 写只经单写线程串行执行——写竞争从机制上不存在。
3. **对话零阻塞铁律**：事件循环禁止任何同步重操作（备份 / 布局重算 / DOCX 构建 / 附件解析等一律 `asyncio.to_thread`），EventLoopMonitor 哨兵每秒探测卡顿作为回归防线。
4. **处处降级不中断**：Embedding 挂 → FTS5 单路；LLM 精筛挂 → top-3 直注；md 损坏 → summary 兜底；压缩失败 → Head+Tail 退化；模型熔断 → 提示切换。任何子系统失败都不阻断对话主链路。
5. **事件驱动解耦**：EventBus 11 个预置事件，新功能只写订阅者不改发布方。

---

## 三、核心功能域

### 3.1 对话调度引擎（八步流水线）

一次用户消息的完整处理链路（`agent/core.py`），全程 Langfuse trace 包裹：

| 步骤 | 内容 | 关键机制 |
| --- | --- | --- |
| 1 输入预处理 | 清洗控制字符；图片经视觉模型解析；URL 自动 web_fetch 预加载（最多 2 个）；信号采集阶段二回填 | 重新生成时先删除旧轮次 |
| 2 上下文加载 | 冻结快照 = Protected Head(3 条) + 压缩摘要 + Protected Tail(20 轮)；SOUL + 时间 + 位置 + 画像 + 技能目录组装 system prompt | 估算超 80000 token 触发压缩 |
| 3 记忆检索 | 三级联动混合检索（详见 3.3.4），命中即发 `memory_retrieved` 事件；stale 命中自动恢复 | 检索 query 剥离附件正文（保护本地 embedding） |
| 4 意图识别 | LLM 结构化输出拆解多意图（11 种枚举），JSON 修复链 + 重试 3 次 | 失败降级单 chat 意图 |
| 5 流程编排 | DAG 拓扑分层（Kahn），同层并行；环检测降级单意图直出；技能按需注入 | 依赖容错：非法依赖丢弃、未注册工具剔除 |
| 6 工具执行 | 按层 asyncio 并行；LLM function_call 推断参数；破坏性操作弹确认（300s 超时）；失败 Replan 补救（每请求 1 次） | 空结果重试 1 次；凭证脱敏 |
| 7 响应合成 | `auto` 路由到 quick/deep；deep 先建立任务合同与问题模型，逐项覆盖质量门通过后交付；长文按可恢复章节生成 | 单次模型物理上限只影响单节，不截短整篇交付 |
| 8 后置处理 | 回复落库（安全分析元数据、策略、骨架）；信号采集阶段一；主动记忆检测；频次更新；引用溯源；预算告警 | fire-and-forget，不阻塞回复 |

**意图类型枚举（11 种）**：`query_memory` / `query_knowledge` / `query_external` / `compute` / `file_op` / `remember_intent`（明确记忆指令，直接写入）/ `remember_confirm`（重要性表态，需确认）/ `soul_feedback` / `output_preference_feedback` / `meta` / `chat`。

**SSE 事件类型全集**：
`queued`（排队）、`error`、`memory_retrieved`、`thinking_delta`（安全分析摘要）、`mode_decision`（自动路由结果）、`analysis_progress`、`delivery_progress`、`quality_status`、`degrade`（降级提示）、`tool_executing`、`tool_confirm`（破坏性待确认）、`tool_confirm_timeout`、`content_delta`、`citations`、`turn_completed`。不传输或保存模型原生链式思维。

**并发与可靠性**：

- 同会话串行处理，排队上限 `session_queue_limit=3`；模型与工具保留各自超时，整轮任务不以固定总时长截断长文交付；
- SSE 断线重连：按 `client_request_id` 缓冲事件 5 分钟 / 1MB，重连断点续推；
- 浏览器断开只断开 SSE 读者，后台生成继续并可重连；仅用户点击停止才取消 worker，深度/长文任务使用较长的可恢复缓冲窗口；
- mimo 模型内置联网搜索：query_external 意图 + mimo 模型 + 开关开启时由模型端搜索（博查源，带结构化引用，回复尾部自动附"联网来源"列表），否则回退自研 web_search（Bing 优先 / DuckDuckGo 兜底）。

**延迟写入**：`file_write` 内容为回复正文时（`__FROM_RESPONSE__` 标记），登记延迟写，回复流式生成完成后自动写入。

**文档生成兜底**：`generate_document` 产物的下载链接若未出现在正文，自动追加文件卡片，保证下载入口必然可见。

### 3.2 上下文与会话管理

#### 3.2.1 L1 上下文压缩（五段式）

- 触发：上下文估算超 `compression_threshold_tokens`（默认 80000）；
- 结构：只压 Middle 段，Head（前 3 条）与 Tail（最近 3 轮）原文保护；
- 五段输出：S1 决策记录（绝对日期防重复执行）/ S2 话题栈（当前+挂起）/ S3 分析框架与结论 / S4 话题演变线索 / S5 待跟进任务；
- 二次压缩：旧摘要与新增 middle 合并压缩，不嵌套；
- 窗口校验：压缩模型 context window ≥ 阈值 ×1.3，不足回退对话模型；middle 超窗按时间切段串行压缩、摘要链式归并；
- 摘要落盘：`sessions/{sid}.md`（frontmatter 含压缩水位 `last_compressed_message_id`）；
- 失败兜底：退化为 Head + 旧摘要 + Tail；连续失败 3 次推系统通知建议新建会话。

#### 3.2.2 会话管理

- 会话 CRUD：新建 / 重命名（manual 优先）/ 置顶 / 删除（级联清理消息、平台映射、摘要文件）；
- 自动标题：首条消息立即取前 15 字符兜底，并行调 LLM 生成 2-8 字标题（3 秒超时，晚到覆盖）；
- 列表：置顶优先 + 最近活跃排序，FTS5 全文关键词搜索，分页；
- 消息：持久化 citations（引用记忆）、安全进度摘要与分析元数据（请求模式、实际执行模式、问题模型摘要、质量状态）；历史可回看；
- 附件：上传解析为文本（20MB 上限，不截断，不入记忆库，仅供当轮上下文）。

### 3.3 记忆系统（核心竞争力）

#### 3.3.1 三层记忆架构

| 层 | 载体 | 生命周期 |
| --- | --- | --- |
| L1 工作记忆 | 当前上下文窗口（Head-Summary-Tail） | 请求级 |
| L2 会话记忆 | conversations 表原文 + sessions/{sid}.md 五段摘要 | 会话级；session_fact 只留摘要不入 L3 |
| L3 记忆宫殿 | data/memories/ 按领域分目录的 md 文件 + SQLite 六张派生索引表 + 向量 | 永久（含生命周期流转） |

另有**第 0 层意识提示**：重要记忆关键词写入 CONTEXT_ENTRY.md，每轮无条件注入 system prompt。

#### 3.3.2 记忆的产生（四条入口）

1. **主动记忆**：用户明确指令（"记住这个"）→ `memory_save` 工具直接写入，标题/摘要由 LLM 提炼（memory_card prompt），详情保留用户原文；
2. **半主动记忆**：用户表达"这很重要"→ 弹确认后写入；
3. **被动回顾**：回顾 Agent 每 N 天（默认 3）读近期对话原文（携带点赞/点踩修正信号）+ 会话摘要 + 新导入文档 → 提炼引擎；
4. **主动记忆检测**：含新事实句式（"我是/我喜欢/我决定…"）但未下记忆指令的消息 → 标记 review_candidates 表，下次被动回顾优先提炼（零 LLM 启发式）。

#### 3.3.3 提炼引擎（Distiller）

- **归属判定**（六分类，优先级：技能 > SOUL 反馈 > 会话级事实 > 待验证推断 > 已验证经验 > 外部导入）：
  - `verified`（用户明确陈述的稳定事实/偏好）→ confidence=medium 入库；
  - `inferred`（合理推断未确认）→ confidence=low 入库，后续在对话中向用户求证；
  - `soul_feedback`（对 AI 回复方式的反馈）→ 走 SOUL 待确认旁路；
  - `skill`（可复用方法论）→ 计数入 skill_patterns，同类 3 次生成技能草稿；
  - `imported`（外部文档知识）→ medium 入库，source_type=knowledge；
  - `session_fact`（一次性任务上下文）→ 丢弃不入 L3。
- **跨时间合并判定**：新记忆向量取 top-20 候选，相似度 ≥0.85 时 LLM 判定四类关系：
  - `same` → 合并（首次不升置信度，第 2 次起 medium→strong）；
  - `evolved` → 合并同一 md，详情分层"当前观点/历史观点"；
  - `contradicts` → 保留两条 + contradicts 边 + 双方降 disputed + 生成矛盾文件；
  - `related` → 各自独立成条 + related 边；
  - 0.6~0.85 区间新建 + related 引用；<0.6 直接新建。
- **降级去重**：Embedding 不可用时 BM25 双区间（0.75/0.5）+ `dedup_pending=1` 标记；向量补偿协程回填后自动回溯重判（幸存者取 id 更小者保证收敛）；另有离线 `--rededup` 全量清理命令。
- **预览模式**：文档导入关闭静默时只提炼不写入，用户勾选确认后落库。

#### 3.3.4 三级联动检索（Retriever）

- **第 1 层 Hybrid 预筛**（约 5ms 零 LLM）：向量路（余弦 ≥0.55 top-K）+ FTS5 路（BM25 相对下限 0.3）→ RRF 融合（k=60）；stale 记忆 ×0.7 降权；问题类型启发式分类（personal/knowledge/neutral）对个人记忆与知识库来源双向降权，防止知识噪音淹没个人记忆；
- **第 2 层 LLM 精筛**：agent 模型从候选中选最相关 2-3 条；
- **第 3 层 详情加载**：读 md 全文 + 沿出链追踪 1 跳（最多 2 条"关联记忆"）；
- **兜底重试**：明确回忆意图（"你还记得…"）时降阈值（0.35）重跑；
- **降级链**：Embedding 挂→FTS5 单路；精筛挂→top-3 直注；md 损坏→summary 兜底；LLM 全挂→跳过检索并提示降级。

#### 3.3.5 生命周期五态与置信度

生命周期：`active` / `stable` / `stale` / `archived` / `missing`；置信度：`low` / `medium` / `strong` / `disputed`（两轴独立）。

| 流转 | 触发 |
| --- | --- |
| active→stable | confidence=strong 且引用达 3 次；同时 is_important=1（用户手动移出过重要目录则有守卫标记不再置回） |
| active→stale | 90 天未访问（Lint 过期检测） |
| stale→active | 检索命中即恢复（用户手动标 stale 的除外） |
| stale→archived | 连续 2 个 Lint 周期未恢复自动归档（文件移入 _archived/，清向量） |
| 点踩"记忆过时" | 4 动作原子复合：→stale + 置信度降一级 + 移出重要 + user_marked_stale=1 |
| 点赞回复 | 引用记忆 medium→strong |
| low 待确认 | 超 30 天的 low 记忆在对话中自然求证（每轮最多 1 条，7 天不重复问）；确认→medium |

**频次统计**：被引用 access_count+1；加载未引用走 implicit_use_count，累计 3 次折 1 次 access_count。
**引用溯源**：每次引用落 citation_events 表（消息/会话/时间），源自知识库的记忆同步回溯 doc_id——记忆与知识库共用同一套"被引用记录"凭证体系。

#### 3.3.6 矛盾检测与裁决

- 检测入口：提炼时（写入前判定）+ 夜间 Lint（相似度 ≥0.8 且未判定过的候选对送 LLM）；
- 标记：双方置 disputed（原值存 confidence_before_dispute）+ contradicts 边 + `_conflicts/conflict_xxx.md` 文件 + 系统通知；
- 对话感知：检索命中 disputed 记忆时，AI 主动告知存在矛盾并引导到健康度 Tab 裁决；
- 裁决四选项：keep_a / keep_b（删一侧恢复另一侧置信度）、keep_both（恢复置信度 + contradicts 换 related）、delete_both；
- 自动闭环：矛盾一侧被删除时自动 resolved 并恢复对侧置信度；resolved 文件 30 天后清理。

#### 3.3.7 知识图谱

- 节点 = 实体（memory_entities，六分类 company/person/concept/technology/event/metric，AI 提炼时标注）；边 = 实体共现聚合（同记忆关联的实体对，权重为共现次数）；
- **双路布局策略**（对话零阻塞）：请求路径仅增量布点 place_missing（新实体放共现邻居质心 + 确定性随机偏移，孤立实体外环分布，毫秒级）；夜间维护链工作线程全量力导向精排（斥力+弹簧+中心重力+重叠消除，180 轮，上限 2000 节点）；坐标持久化 graph_layout 表，前端零布局直接渲染；
- 前端能力：Sigma WebGL 渲染（SVG 备选）、节点搜索定位、邻居扩展（共现 top-N + 集内边）、节点详情抽屉（关联记忆列表）；
- 上限：默认渲染 300 节点 / 2000 边（可配置）。

#### 3.3.8 记忆健康度（Lint）

六项检查 + 技能维护，健康分 100 分制后端唯一计算：

| 检查项 | 扣分 | 处置 |
| --- | --- | --- |
| disputed 矛盾 | 3/条 | 裁决界面 |
| low 超 30 天未确认 | 1/条 | 对话求证 |
| stale 过期 | 0.5/条 | 命中恢复 / 自动归档 |
| orphan 孤立（零连接） | 0.5/条 | 一键采纳建链（采纳=确认保留必定成功，建链尽力而为）+ 批量补链 |
| duplicate 疑似重复（≥0.9） | 1/条 | 采纳合并 / 标记非重复 |
| missing md 缺失 | 2/条 | 从备份恢复 |
| failed 写入积压 | 一次性 10 | 夜间重扫 |

附带：目录漂移修复（md 与索引不一致以 md 为准）、90 天未用技能归档、`lint.completed` 事件触发画像重建。

#### 3.3.9 记忆存储格式

- **md 文件**：YAML frontmatter（id/title/domain/confidence/lifecycle/source_type/access_count/links/entities/is_important/dedup_pending 等）+ 三段正文（## 摘要 / ## 详情（观点演变分层）/ ## 变更历史（倒序））；
- **命名**：`mem_{6位序号}_{标题前20字符}.md`，实体 id 为规范化名称 SHA1 前 10 位；
- **目录**：`data/memories/{domain}/`，特殊目录 `_archived/`（按领域分层）、`_conflicts/`，全局目录 `_index.md`（≤2000 全量重建，超出按 dirty domain 增量段替换，10 秒节流）；
- **领域标签**：domain 由 LLM 动态产生（英文 slug），DomainLabeler 维护中文展示映射（27 个内置种子 + 新领域首次出现异步 LLM 翻译一次入库，中文领域原样展示）；
- **schema 版本**：md frontmatter 版本化（当前 v1），升级脚本机制 + 升级前全量备份 + 失败整体回滚拒绝启动。

#### 3.3.10 时间线

memory_timeline 表记录每条记忆的事件流（created/updated/evolved/imported/archived/merged/missing），同秒同事件去重；前端时间线 Tab 按天数 / 事件类型筛选展示。

### 3.4 人格系统（SOUL）

#### 3.4.1 双层人格

- **SOUL_CORE.md（稳定层）**：身份 / 核心价值观 / 性格特征 / 禁止行为。仅用户手工编辑，AI 禁止自改；提供"恢复默认人格"；
- **SOUL_STYLE.md（演化层）**三段：
  - 对话风格 + 行为原则 → **dialog 版本序列**，由"对话确认"路径演化：用户的风格反馈实时经 soul_feedback 意图记入记忆层（事实记录）；后台提炼判定 soul_feedback 归属后进 CONTEXT_ENTRY 待确认区 → 下轮对话 AI 自然询问 → 用户确认后语义去重（embedding 余弦 ≥0.85 跳过）再落盘建版；
  - 输出样式 → **auto 版本序列**，由输出画像 Agent 静默演化（见 3.4.2）；
- 两条序列各保留 3 版，按序列独立回滚互不干扰；输出样式段固定附加防僵化元规则（用户显式格式要求优先于画像默认）。

#### 3.4.2 输出画像（response_signals 两阶段采集）

- 阶段一（本轮）：采集回复形态（字数/段落/bullet/代码块/表格/结论位置）+ 问题类型标签；
- 阶段二（下一轮）：回填用户隐式反应（追问澄清/继续新话题）与偏好关键词（"太长了/说人话/给个表格"等词表本地匹配，零成本）；显式点赞点踩直接覆盖；
- 提炼触发：信号 <50 冷启动不执行；首次达 50 立即；之后每 7 天或新增满 100 条提前；回滚后 14 天冷静期；可关闭自动演化 / 手动触发；
- 演化频率控制：与上一版 diff 相似度 >0.95 不占新版号；信号保留 90 天。

#### 3.4.3 注入防护

- 8 组中英文正则匹配越权指令（ignore previous instructions / you are now / 忽略之前指令 / 解除限制…）；
- SOUL_CORE 命中 → 回退内置默认人格并通知；SOUL_STYLE 命中 → 沿版本历史逐版回退取最近干净版本，全脏回退内置常量；回滚目标版本也须通过扫描；
- 外部编辑 SOUL 文件由 FileWatcher 感知，触发重新扫描 + 快照失效 + 通知。

#### 3.4.4 用户画像

- `data/profile/user_profile.md`：二级标题为维度（基本身份/沟通偏好/专业领域/决策风格/工作上下文/当前目标），标注 [已确认]/[部分推断]，条目行尾 [推断] 标记；
- 重建：Lint 完成事件触发（也可手动"立即重建"），取 access_count 最高的 100 条 active/stable 记忆经 LLM 重建，整文件覆盖写入；
- 注入：第 2 步无条件注入 identity 维度（约 50 token）；意图相关维度按需追加。

#### 3.4.5 技能系统

- 结构：`skills/{name}/`（SKILL.md + templates/ + references/），三级渐进加载：Level 0 目录（约 500 token 常驻 system prompt）→ Level 1 主文件（请求级按名匹配注入，上限 2 个）→ Level 2 模板参考（按需，上限 4000 字符）；
- 生命周期：`draft`（提炼引擎同类任务 3 次自动生成草稿 → 对话中 AI 提议 / 健康度页确认启用）→ `active`（记录使用次数）→ `archived`（90 天未用自动归档）；支持用户手动创建（直接 active）。

### 3.5 工具系统

#### 3.5.1 统一双路径架构

Agent 视角只有一种工具；底层分内置（进程内直调）与 MCP（协议标准化调用，前缀 `{connector_id}__{tool}`）两条路径。每轮对话把全部已注册工具 schema 注入 prompt；破坏性工具标记 destructive。

#### 3.5.2 内置工具（11 个）

| 工具 | 功能 | 破坏性 |
| --- | --- | --- |
| memory_save | 主动保存长期记忆（created_by=user_explicit） | ✓（用户主动触发跳过确认） |
| memory_search | 检索记忆宫殿（仅第 1 层预筛） | |
| memory_get | 按 ID 读单条记忆全文 | |
| file_read | 读工作区文件（1MB 上限） | |
| file_write | 写工作区文件（覆盖/追加，支持回复正文延迟写入） | ✓ |
| shell_exec | 工作区执行命令（30s 超时） | ✓ |
| web_fetch | 抓取网页正文（SSRF 防护 / readability 提取 / PDF 复用解析器 / 10MB 上限） | |
| web_search | 免 Key 联网搜索（Bing 优先 → DuckDuckGo 兜底） | |
| calculator | AST 白名单安全算术 | |
| datetime_now | 当前时间（默认 Asia/Shanghai） | |
| generate_document | 生成 Word/Markdown/PPT/Excel 文档落地 temp/exports 返回下载链接（7 天清理，支持 docx/md/pptx/xlsx 四格式） | |

#### 3.5.3 安全边界

- **沙箱**：文件访问限定 `data/workspace/` + 可配置白名单，realpath 规范化防穿越与符号链接逃逸；命令高危黑名单（rm -rf / mkfs / dd / format / Remove-Item…）；执行环境剥离含 KEY/TOKEN/SECRET 的环境变量；违规原因返回 LLM 不终止本轮；
- **pre_tool hook**：破坏性确认摘要（工具/意图/关键参数/影响范围/是否可逆/超时）+ 必填参数校验；
- **post_tool hook**：空结果自动重试 1 次 + 凭证泄漏扫描脱敏（sk- / ghp_ / Bearer / AKIA 等模式 → [REDACTED]）。

#### 3.5.4 MCP 连接器

- 传输：stdio（本地子进程，生命周期托管）+ Streamable HTTP（远程，认证头注入）；JSON-RPC 2.0（initialize / tools/list / tools/call）；
- 凭据：stdio env / http auth 敏感值 Fernet 加密入 credentials 表，config 中圆点占位；编辑时占位符保留原值；
- 工具管理：include 白名单 / exclude 黑名单过滤；按名称关键词猜测破坏性标记；软断开（工具下线）与彻底删除（含凭证）分离；启动自动重连；
- OAuth 2.1：state 暂存 5 分钟，回调换 token 加密存储；
- 文档导出：Markdown → DOCX 自研映射管线（markdown→HTML→python-docx，覆盖标题/列表/代码块/表格/引用/链接等全量语法，中文微软雅黑），CPU 密集强制工作线程执行；PPTX（python-pptx，h1 封面 + 每 h2 一页 + 页数/字符/表格行截断防护）与 XLSX（openpyxl，每个表格一个 sheet + 公式注入前置单引号转义）同管线扩展，缺失依赖时仅对应格式报错不影响其余格式。

### 3.6 知识库（文档导入 Ingest）

- **入口**：Web 上传 / URL 导入 / IM 发文件 / 对话中"把这张图存进知识库"（图片后台静默入库）；
- **格式**：PDF / DOCX / TXT / MD / 各类文本代码 / 图片（PNG/JPG/WEBP/BMP/GIF）/ 网页；单文件 50MB，raw_docs 总量 2GB 告警；
- **富解析**：DOCX 按文档流提取段落+表格转 Markdown+内嵌图片过 VLM；PDF 逐页文字+内嵌图片；图片走可配置引擎（`vlm` 视觉模型 / `ocr` 本地 RapidOCR / `off` 仅缓存），解析结果缓存 extracted_text 列避免重复调用视觉模型；
- **分块提炼**：按段落边界切 6000 token 块（重叠约 200 token），逐块走文档专用提炼 prompt（attribution 一律 imported，知识点粒度逐条抽取）；单块失败跳过，全部失败回滚删除落盘文件（防孤儿）；
- **两种模式**：静默导入（默认，完成推通知）/ 预览确认（提炼结果暂存 pending_imports，用户勾选后写入）；
- **管理**：文档列表 / 详情（提取的记忆清单 + 被引用记录）/ 删除（提示影响 --recompile 完整性）。

### 3.7 多端接入（IM 网关）

- **支持平台**：Telegram（long-poll）、飞书（webhook + tenant_access_token）、钉钉/企微（webhook）；同时只启用一个 IM 平台；
- **安全**：仅私聊 + 白名单用户；message_id 去重；凭据加密存储；未配置凭证不允许启用；
- **会话映射**：平台用户 ↔ session 持久映射；`/new` 命令开新会话；
- **消息处理**：调用同一 AgentCore 消费完整回复（300 秒超时）；超长回复（>4000 字符）转 .md 附件（Telegram/飞书上传文件，钉钉分段发送兜底）；
- **入站文件**：下载后触发 Ingest 提炼入库并回复提取条数；
- **熔断**：每适配器独立，连续失败 5 次置 paused + 通知，需手动恢复；
- **系统通知**：作为 system_notification 消息写入最近活跃会话（24h 同类去重，无会话暂存补发），Web + IM 双端推送。

### 3.8 系统 Agent 与定时任务

#### 3.8.1 五类系统 Agent

| Agent | 触发 | 职责 |
| --- | --- | --- |
| 压缩 Agent | 上下文超阈值 | 五段式语义压缩（独立 context） |
| 回顾 Agent | 每 N 天 03:00 | 近 N 天对话（带反馈信号）+ 会话摘要 + 新文档 + 回顾候选 → 提炼入库；超长按天切分 |
| Lint Agent | 回顾完成后 | 六项体检 + 矛盾判定 + 生命周期流转 + 技能提炼归档 → lint.completed |
| 画像 Agent | Lint 完成后 | top-100 记忆重建 user_profile.md；引导模式生成初始 SOUL 草稿 |
| 输出画像 Agent | 每天 03:30 自门控 | 信号分箱统计 + LLM 提炼输出样式 → soul_style auto 版本 |

Agent 注册表：内存心跳监控（任务超时 10 分钟 / 心跳卡死 3 分钟回收），不落库。

#### 3.8.2 定时任务（链式调度，Asia/Shanghai）

- **夜间维护链**（每天 02:00，前驱完成驱动）：自动备份 → 消息去重表清理 → 临时附件/导出清理（7 天）→ 日志清理（任务日志 30 天/操作日志 90 天/向量备份 30 天/信号 90 天）→ 已解决矛盾清理（30 天）→ failed 写入重扫 → 图谱布局全量重算；
- **记忆维护链**（每 N 天 03:00，04:00 兜底检查只记日志）：被动回顾 → Lint 体检 → 画像重建；
- **独立任务**：输出样式提炼（03:30 自门控）；
- 规则：链上任务失败重试 2 次后整链中断、后续记 skipped；同步任务丢线程池；手动触发写日志不改排期；设置页可查看任务清单 / 手动运行 / 执行日志。

### 3.9 模型与供应商管理

#### 3.9.1 Provider 与模型槽位

- Provider CRUD：display_name / provider_type（openai_compatible / anthropic / google / custom）/ base_url / model_id / API Key（加密存储，可回显）/ 单价（¥/M tokens）/ context_window；同 base_url+model_id 去重更新；保存与测试连接职责分离（探测先 chat 后 embed，兼容纯 embedding 模型）；
- **四个任务槽位**：`chat`（主对话链路）、`agent`（意图精筛/系统 Agent/标题生成，未配回退 chat）、`embedding`（所有向量化）、`vision`（图片解析，未配回退 agent→chat）；对话模型下拉隐藏 embedding 专用模型；
- 运行时以请求级 ProviderSnapshot 冻结快照调用，不在调用中读库。

#### 3.9.2 可靠性

- 按模型独立熔断器：连续 3 次失败 unavailable，60 秒半开探测恢复；
- 指数退避重试 1s→2s→4s 共 3 次；4xx（非 429）快速失败不计熔断并透出厂商错误体；
- 流式支持 reasoning_content（DeepSeek 类思考增量）与 annotations（mimo 内置搜索引用）回调；多模态图片 dataURL 注入；
- 所有调用统一记录 token_usage（真实 usage 优先，缺失 tiktoken 估算），火忘式写入零阻塞。

#### 3.9.3 本地 Embedding 与迁移

- 本地 BGE-M3 微服务：隔离 venv（sentence-transformers+torch），标准库 http.server 零框架，OpenAI 兼容 /embeddings，端口 8100，离线加载，CUDA 自动检测；启动时自动注册为 Provider 并在安全时接入（无存量向量直接切换，有则提示走迁移）；
- **Embedding 迁移**（换模型/换维度）：预估（条数/成本/耗时）→ 二次确认 → 双缓冲执行（旧向量继续供检索，新向量写 staging，批 32 串行，可暂停/续跑）→ 原子 commit 切换 + 切槽位分配；旧向量备份 30 天可回滚；迁移期新版本记忆 FTS 单路得分 ×1.5 补偿。

### 3.10 成本控制与用量统计

- 预算：日 / 月 token 预算（0=不限），达 80%（可配）与 100% 推系统通知（24h 去重）；当前策略 remind_only 仅提醒不阻断；
- 用量统计：今日/本月用量与预算比、按来源（main_chat/system_agent/title_gen/embedding…）与按模型分布、趋势（近 30 天/本月按天/当年按月）、可按来源与模型筛选；
- 本月费用：用量落库时冻结当时单价并折算金额（调价不追溯）；无快照的历史行按当前单价兜底折算（未配单价不计入，不做外推），会话级 token 用量单独可查。

### 3.11 数据可靠性与备份

#### 3.11.1 写入架构（双单写者）

- **FileWriter 单写者队列**：全系统 md + 派生索引唯一写者；6 类处理器（memory/profile/soul_style/context_entry/skill/index）；持久化类型先落 pending_writes 表再消费（崩溃恢复重放）；重试 3 次失败 → failed + 通知 + 夜间重扫；index 请求自动合并；写入幂等（同 mid 残留文件复用，防重试产生重复副本）；add_link 消费时读最新 md 原子追加（根治建链竞态丢边）；优雅停机排空队列；
- **SQLite 单写线程**：WAL + 写队列 + 组提交（批 64 条一次 commit，失败回滚逐条重放隔离坏语句）；execute_nowait 火忘式（token/日志高频小写）；显式事务与写线程共锁互斥；队列积压 200 条告警。

#### 3.11.2 备份 / 恢复 / 导出

- 自动备份：VACUUM INTO 一致性快照（不抢写锁）→ 快照 integrity_check → zip 打包（manifest + palace.db + config.yaml + 全部 md），排除 raw_docs/backups/temp/.master_key；保留最近 3 份（保护性备份不占名额）；
- 恢复：版本兼容检查 → 先做保护性备份 → 覆盖 → 重建索引 → 向量缓存重载（工作线程）；
- 导出/导入：zip（memories.json + conversations.json + config.yaml + md 副本），导入前强制保护性备份 + manifest 校验；
- 恢复命令：`--rebuild-index`（md 重建全部索引，不停机原子切换）、`--recompile`（从原始素材重跑提炼，停机+差异报告）、`--rededup`（离线全量回溯去重）；
- 一致性：启动自检（md/index count 比对 + 向量缓存比对）、健康检查接口持续监测、FileWatcher 外部编辑实时同步（1.5s 防抖，内部写入标记跳过防死循环，外部删除置 missing 并通知）。

### 3.12 可观测性

- **trace_id 全链路**：每请求 tr_ 前缀 id，contextvars 跨 await 传播，日志行携带，错误 toast 可复制；
- **Langfuse 全链路追踪**：自研批量协议客户端（队列 2000 上限 / 3 秒批量上报 / 失败仅告警绝不影响主链路）；层级 trace（chat.turn）→ span（八步流水线 7 个步骤 + 标题生成）→ generation（LLM 调用自动记录模型/输入输出/用量）；本地自托管 Langfuse v2（PostgreSQL 5433 + Redis 6379，页面 3001）；
- **运行防线**：EventLoopMonitor 事件循环卡顿哨兵（>0.5s warning / >2s error）、慢操作检测（3000ms）、写队列深度仪表、操作日志（90 天，仅排障）；
- **健康检查**：/api/health 九项子系统三级判定（healthy/degraded/unhealthy），前端侧栏状态灯 30 秒轮询。

### 3.13 安全与隐私

- 凭证：Fernet 加密 + data/.master_key（Windows icacls / Unix chmod 600 锁权限；不进备份；换机解密失败引导重输）；
- 网络：web_fetch 私网地址 SSRF 防护（可显式放行）、重定向/大小上限；后端仅监听 127.0.0.1；
- 内容：SOUL 注入扫描、工具输出凭证脱敏、shell 高危黑名单与环境净化、prompt 中附件与位置信息不持久化。

### 3.14 首次引导（Onboarding）

1. 配置对话模型（测试连接）与 Embedding（可用本地 BGE-M3）；
2. 欢迎对话：内置引导人格进行几轮相互了解（不落记忆库）；
3. 生成初始 SOUL 草稿（soul_core + 对话风格），用户确认/编辑后落盘；
4. 标记完成进入主界面；记录 first_installed 日期。

---

## 四、前端产品设计

### 4.1 页面结构（Vue3 三大页 + 全局壳）

- **全局壳 App.vue**：引导流程全屏接管；左侧会话栏（会话列表/搜索/置顶/健康灯）；全局 toast（错误带复制 trace_id）；系统自研确认弹窗（禁用原生 window.confirm）；
- **对话页 /chat**：默认“自动模式”，可覆盖为快速回答或深度思考；SSE 展示安全进度与完整回复，深度任务展示问题建模、分节交付和质量状态，不展示原生推理；其余交互保持现有规范。
- **记忆中心 /memory** 六 Tab：
  1. 知识图谱：WebGL 渲染 + 搜索定位 + 邻居扩展 + 节点详情抽屉；
  2. 记忆列表：分页/关键词（混合检索）/领域/生命周期/置信度/重要筛选，属性编辑、归档/恢复/删除；
  3. 时间线：事件流按类型与天数筛选（枚举中文化）；
  4. 用户画像：维度卡片 + 确认状态标注 + 立即重建；
  5. 健康度：健康分与扣分明细、Lint 建议处理（孤立采纳/重复合并/忽略）、矛盾裁决区、技能草稿启用、立即体检；
  6. 知识库：文档列表/上传/URL 导入/预览确认/详情（提取记忆 + 被引用记录）/删除；
- **设置页 /settings**：模型服务（Provider 管理 + 四槽位分配 + 测试连接 + Embedding 迁移）；参数配置（按 schema 分组动态渲染，中文 label/描述/校验/生效时机提示）；连接器（MCP 增删改测/工具过滤/启停）；接入渠道（IM 平台配置/测试/启停/恢复）；用量统计（预算/分布/趋势/本月费用）；备份（列表/创建/恢复/导出导入）；定时任务（清单/手动运行/日志）；系统状态（子系统面板）；SOUL 管理（核心人格编辑/重置、风格版本历史/diff/回滚、待确认项、输出画像开关与手动提炼）。

### 4.2 交互规范（已沉淀的设计约定）

- 全部枚举值/状态文案中文化展示（提交仍用英文值）；日期统一格式化；
- AI 回复链接新标签页打开；流式输出中代码块与思考面板自动吸底跟随；scroll-latest 按钮 sticky 定位；
- scrollbar-gutter: stable 防布局跳变；弹窗 z-index 分层规范；
- 破坏性操作一律走系统确认弹窗并等待真正落库（wait=True）后反馈。

---

## 五、API 总览（前缀 /api，统一响应 {code, data / message, trace_id, details}）

| 域 | 端点（方法 路径） |
| --- | --- |
| 对话 | POST /chat/send（SSE）、POST /chat/tool-confirm、GET /chat/session/{sid}/active-request、GET /chat/sessions、GET /chat/messages、POST /chat/feedback、POST /chat/session/rename·create·pin、POST /chat/attachment、DELETE /chat/session/{sid}、GET /chat/session/{sid}/usage |
| 记忆 | POST /memory/list、GET /memory/domains·domain-labels·detail、PUT /memory/{id}/attributes、POST /memory/archive·restore·delete、GET /memory/graph·graph/entity/{id}/memories·neighbors·graph/search、GET /memory/timeline、GET /memory/health、POST /memory/lint/run·suggestions/accept·dismiss、POST /memory/orphans/relink、GET /memory/conflicts、POST /memory/conflicts/resolve |
| 引导 | GET /onboarding/status、POST /onboarding/test-connection·test-embedding·welcome-chat/start·finish·soul/confirm |
| 知识库 | POST /import/document·url、GET /import/documents·/{id}、POST /import/documents/{id}/confirm、DELETE /import/documents/{id}、GET /files/{name} |
| 设置 | providers CRUD+test+key、model-assignment GET/PUT、embedding estimate/migrate/status/pause/resume、params GET/PUT/reset、connectors CRUD+test+toggle+refresh-tools、usage summary/distribution/trend/month-cost、backups list/create/restore/export/import、status、platforms CRUD+enable/disable/test/resume、tasks list/run/logs |
| 人格 | GET /profile、POST /profile/build-now、GET /soul、PUT /soul/core、POST /soul/core/reset、GET /soul/style/history·diff、POST /soul/style/rollback、GET /soul/pending、POST /soul/pending/confirm、GET /output-style、POST /output-style/toggle-auto·build-now |
| 技能 | GET /skills/drafts、POST /skills/drafts/activate·delete、POST /skills/create |
| 其他 | GET /tasks/{id}/status、POST /im/webhook/{platform}、GET /connectors/oauth/callback、GET /health |

---

## 六、数据模型

### 6.1 SQLite 表清单（migrations 001~011）

| 分组 | 表 |
| --- | --- |
| 供应商与凭证 | credentials（加密凭证）、providers、model_assignment（chat/agent/intent/deep_analysis/embedding/vision 槽位） |
| 接入渠道 | platforms、platform_sessions、message_dedup |
| 对话 | conversations（含 thinking/citations/analysis_metadata/feedback）+ conversations_fts + 触发器、sessions（含置顶/压缩水位）、delivery_jobs、delivery_sections |
| 记忆索引 | memories（主索引）、memories_fts、vectors（BLOB + pending/ready/failed）、memory_links（5 类边）、memory_entities、memory_entity_links、memory_timeline、lint_suggestions、graph_layout（预计算坐标）、domain_labels（中文标签缓存）、citation_events（引用溯源）、review_candidates（回顾候选）、pending_imports（导入预览暂存） |
| 素材与技能 | raw_docs（含 extracted_text 解析缓存/review_status）、skill_usage、skill_patterns（3 次计数） |
| 写入与信号 | pending_writes（写队列持久化）、response_signals（两阶段信号） |
| 运维 | token_usage、scheduled_tasks、task_logs、operation_logs、embedding_migration、vectors_old_backup、oauth_states、connectors、schema_migrations |

### 6.2 data/ 目录结构

```text
data/
├── config.yaml            # 配置（参数区 + 服务编排 + Langfuse + 本地 embedding）
├── palace.db              # SQLite（WAL）
├── CONTEXT_ENTRY.md       # 上下文入口（系统状态/阅读顺序/近期变化/待处理）
├── .master_key            # Fernet 主密钥（不进备份）
├── memories/{domain}/     # 记忆 md（唯一真相源）+ _index.md + _archived/ + _conflicts/
├── sessions/              # 会话压缩摘要 md
├── profile/user_profile.md
├── soul/                  # SOUL_CORE.md + SOUL_STYLE.md + SOUL_STYLE_HISTORY/
├── skills/{name}/         # SKILL.md + templates/ + references/ + _index.md
├── raw_docs/              # 导入原始文件（不可变）
├── backups/               # sp_backup_*.zip
├── temp/                  # attachments/ + exports/（7 天清理）
└── workspace/             # 工具沙箱工作区
```

---

## 七、配置体系（PARAM_SCHEMA 全参数）

全部参数声明类型/值域/默认值/生效时机（immediate/next_turn/next_session）/分组/中文说明，前端动态渲染，保存时统一校验（含跨参数约束）。

| 分组 | 参数（默认值） |
| --- | --- |
| 记忆 | 被动回顾间隔 3 天、过期检测 90 天、重要升级 3 次、过期降权 0.7、文档静默导入 on、图片解析引擎 vlm |
| 对话 | 缓冲 20 轮、头部保护 3 条、压缩阈值 80000 token |
| 成本 | 日预算 50 万、月预算 1000 万、告警 80%、超预算仅提醒、备份保留 3 份 |
| 检索 | top_k 10、向量阈值 0.55、兜底 0.35、BM25 下限 0.3、合并阈值 0.85、建链 0.6、重复提示 0.9、rrf_k 60、个人问题知识降权 0.7、知识问题记忆降权 0.85 |
| 输出画像 | 提炼间隔 7 天、批阈值 100、保留 90 天、窗口 30 天、自动演化 on |
| 可视化 | 图谱 300 节点 / 2000 边 |
| 其他 | 会话排队 3、常规回答表达密度、深度回答质量校验 on、抓取超时 15s、工具确认 300s、IM 单条 4000 字、向量缓存 512MB、浏览器定位 off、mimo 内置搜索 on（关键词上限 3） |

---

## 八、运行与部署

### 8.1 启动编排（start.py）

- 命令：`start / stop / status / restart / install-service` + `--port / --rebuild-index / --recompile / --rededup / --no-browser / --no-embedding / --no-services`；
- 启动九步：单实例判定（PID + 端口探测）→ 目录初始化 → SQLite 迁移 → md schema 迁移 → 完整性检查 → 端口绑定（8000，占用 fallback 8001-8010）→ 子系统启动 → 恢复残留 → 引导判定；
- ServiceSupervisor：按依赖拓扑拉起服务 + 就绪探测（port/http）+ 退出逆序终止（Windows taskkill 进程树）；外部服务一律 optional 失败不阻断；已运行则跳过接管；
- 编排的服务：本地 Embedding（8100）→ PostgreSQL（5433）→ Redis（6379）→ Langfuse（3001，后台启动不阻塞）→ 主应用（8000）；
- install-service 生成 systemd / launchd / schtasks 开机自启配置。

### 8.2 端口分配

| 端口 | 服务 |
| --- | --- |
| 8000（fallback 8001-8010） | 主应用（FastAPI + 静态前端） |
| 8100 | 本地 BGE-M3 Embedding |
| 3001 | Langfuse Web |
| 5433 / 6379 | PostgreSQL / Redis（Langfuse 依赖） |

### 8.3 前端构建

frontend/ 下 `npm run build`，产物部署至 `app/static/` 由主应用挂载根路径（构建验证强制通过）。

---

## 九、关键产品闭环一览

1. **记忆闭环**：对话 → 提炼（归属/合并/矛盾判定）→ 入库建链 → 检索引用 → 频次/生命周期流转 → Lint 体检 → 画像重建 → 反哺对话；
2. **人格闭环**：用户风格反馈 → 待确认 → 对话确认 → 语义去重落盘建版；回复信号两阶段采集 → 输出画像自动提炼 → 影响后续回复风格 → 新信号继续采集；
3. **可靠性闭环**：写入失败 → failed 标记 + 通知 → 夜间重扫重放；Embedding 不可用 → pending 占位 → 补偿协程回填 → 回溯去重；外部编辑 → watcher 感知 → 索引同步/注入扫描；
4. **成本闭环**：全调用 token 记录 → 用量统计/费用实算 → 预算告警 → （策略可扩展为阻断）。
5. **问题解决闭环**：自动路由 → 深度任务合同与问题模型 → 逐项方案 → 需求覆盖质量门 → 长文分节持久化与恢复 → 用户反馈校准。

---

## 十、当前边界与已知取舍

- 单用户本机部署，无多租户/鉴权体系（仅监听 127.0.0.1）；
- IM 同时只启用一个平台；钉钉 webhook 不支持文件上传（分段发送兜底）；
- 超预算策略当前仅 remind_only；
- 操作日志无查询界面（仅排障）；
- 插件机制（manifest.json + plugin.py，三条扩展路径）已就绪但暂无内置插件；
- 无 cryptography 库时凭证降级弱编码（仅本机，启动告警）。

---

## 附录 A：系统 Prompt 资产清单（全部外部化为 md，热重载）

| 文件 | 用途 |
| --- | --- |
| agent/prompts/intent_system.md | 意图解析器：11 种枚举、remember_intent/confirm 边界、实时信息必走 web_search 规则 |
| agent/prompts/memory_card.md | 主动记忆卡片：20 字含主语标题 + 30 字第三人称摘要 |
| agent/prompts/compress_system.md | 六段结构对话压缩（S0-S5，S1 绝对日期防重复执行） |
| agent/prompts/replan.md | 工具失败补救判定（retry_other_tool/retry_same_tool/skip/abort） |
| agent/prompts/response_synth.md | 响应合成：实时信息只依据工具结果、缺失如实告知、末尾 citations 声明 |
| agent/prompts/compact_prefix.md | 压缩摘要注入前缀（声明为历史参考非当前指令） |
| agent/prompts/initial_soul.md | 引导期从欢迎对话生成初始 SOUL 草稿 |
| agent/prompts/profile_rebuild.md | 用户画像重建（维度 + 已确认/部分推断标注） |
| agent/prompts/output_style.md | 输出样式画像提炼（50-150 字自然语言） |
| app/prompts/distill.md | 对话记忆提炼：六类归属判定 + 粒度四原则 + “下次对话仍有用不判 session_fact” |
| app/prompts/distill_document.md | 文档知识提炼：attribution 一律 imported，知识点粒度逐条抽取 |
| app/prompts/merge_judge.md | 记忆关系判定（same/evolved/contradicts/related + 保守裁决规则） |
| app/prompts/memory_refine.md | 检索第 2 层精筛（选最相关 2-3 条） |
| app/prompts/domain_label.md | 领域英文 slug → 2-8 字中文标签 |
| app/prompts/extract_image.md | 图片内容解析（VLM 完整转写 + 客观描述） |
| app/prompts/title_gen.md | 会话标题（2-8 字名词/动词短语，禁判断词开头） |

## 附录 B：EventBus 预置事件（11 个）

`memory.created` / `memory.updated` / `turn.completed` / `lint.completed` / `session.ended` / `review.completed` / `soul_style.updated` / `output_style.updated` / `profile.rebuilt` / `task.progress` / `embedding.migration.completed`
（订阅者异常隔离；同步/协程订阅者均支持；publish_nowait 供同步上下文投递。）

## 附录 C：系统通知类型（24h 同类去重，Web+IM 双端）

预算告警/超额、压缩连续失败、矛盾记忆发现、向量化失败、md 格式异常/被外部删除、SOUL 重载/注入重置、文档导入完成、图片未解析、raw_docs 容量告警、渠道熔断暂停、定时任务失败、Embedding 迁移完成/失败、本地 Embedding 注册提示。

## 附录 D：命名与标识规则（全系统唯一实现 memory/naming.py）

| 对象 | 规则 |
| --- | --- |
| 记忆 | mem_{6 位零填充序号}；文件名 mem_xxx_{标题前 20 字符}.md（冲突_2/_3） |
| 实体 | ent_{规范化名称 SHA1 前 10 位}（NFKC 全角转半角+去空格+小写） |
| 会话 / 文档 / Provider | sess_{4 位} / doc_{4 位} / prov_{3 位} |
| 备份 | sp_backup_{YYYYMMDD_HHMMSS}[_{label}].zip |
| 建议 / 确认 / 待办 / 任务 | sug_/ cf_ / pending_{uuid8} / {type}_{时间戳}_{uuid8} |
| trace | tr_{uuid12}（内部）；Langfuse trace 独立 uuid，metadata 关联 internal_trace_id |
| domain | 小写英文或中文，1-32 字符，非法字符与空格转下划线 |
