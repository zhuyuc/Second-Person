# Notepad++ 调研与 Second Person 记事本功能分析报告

> 调研日期：2026-10-09  
> 参考仓库：[notepad-plus-plus/notepad-plus-plus](https://github.com/notepad-plus-plus/notepad-plus-plus)（GPL）  
> 官网：[notepad-plus-plus.org](https://notepad-plus-plus.org/)  
> 用户手册：[npp-user-manual.org](https://npp-user-manual.org/)  
> 状态：**调研结论**，非已交付功能；不修改当前产品契约。

## 1. 结论摘要

Notepad++ 值得借鉴的是**产品心智与文档模型**，不是直接移植其 Win32/Scintilla/GPL 实现。

对 Second Person 做「记事本」时，建议：

1. **定位**：轻量多标签文本/Markdown 工作台，服务「随手记 → 可被 Agent 引用 → 可沉淀进记忆/知识库」，而不是做成完整 IDE 或 Notepad++ 克隆。
2. **架构映射**：把 N++ 的 `Buffer` / `FileManager` / `DocTabView` / 双视图，映射为 Web 侧的「笔记实体 + 会话态标签 + 主编辑区（可选分屏）」。
3. **编辑器选型**：前端用 **CodeMirror 6（首选）或 Monaco（备选）**；**不要**嵌入 N++ 源码或 Scintilla 原生组件（许可证、平台、工程成本都不匹配）。
4. **落地路径**：先做「全局草稿 + 项目内笔记 + 自动保存 + 对话引用」MVP；语法高亮/查找替换/分屏放二期；插件体系、宏录制、Find in Files 等暂不纳入。

一句话：学 N++ 的「多文档缓冲 + 标签切换 + 脏标记 + 会话恢复」，用本仓库已有的 `data/` md 主副本、项目工作区与 Agent 工具链接起来。

## 2. 调研范围

| 项 | 说明 |
| --- | --- |
| 调研对象 | Notepad++ 官方 GitHub 仓库、BUILD、用户手册（插件/消息通信）、公开架构资料 |
| 目标 | 指导当前程序增加记事本能力，而非在本机再装一个桌面编辑器 |
| 非目标 | 复刻全部语言 Lexer、Plugin Admin、宏系统、打印机、注册表关联等桌面专用能力 |
| 对照基线 | Second Person：Vue3 前端 + FastAPI + `data/` md 主副本 + 项目工作区 + 记忆/知识库 |

## 3. Notepad++ 项目速览

### 3.1 定位与许可证

- **定位**：Windows 上的免费源码编辑器 / 记事本替代品；强调轻量、低 CPU、多语言高亮。
- **技术栈**：C++ + 纯 Win32 API + STL；编辑引擎 **Scintilla**；词法 **Lexilla**；高级正则 **Boost.Regex**。
- **许可证**：GNU GPL。衍生/移植若拷贝其代码或强绑定其 Lua/语言定义（如部分 NotepadNext / notepad-web 路线），通常会把 GPL 传染到整个产品。对 Second Person（当前非 GPL 产品形态）应 **只做概念参考，不拷贝源码与资源包**。

### 3.2 仓库结构（与实现相关）

```text
notepad-plus-plus/
├── PowerEditor/          # 应用主体（notepad++.exe）
│   ├── src/              # Notepad_plus、Buffer、DocTab、WinControls、Plugins…
│   ├── visual.net/       # VS 解决方案
│   └── installer/        # 安装器
├── scintilla/            # 编辑引擎（静态库 libScintilla）
├── lexilla/              # 语言词法（静态库 libLexilla）
└── boost/                # 裁剪后的 Boost.Regex
```

构建入口见官方 `BUILD.md`：一个 VS solution 同时产出 `notepad++.exe` + Scintilla + Lexilla。

### 3.3 核心架构（可抽象复用的部分）

```text
Notepad_plus_Window（Win32 主窗口 / 消息泵）
        │
        ▼
Notepad_plus（应用协调器）
        ├─ DocTabView ×2（MAIN_VIEW / SUB_VIEW）  ← 标签 UI
        ├─ ScintillaEditView ×2                   ← 编辑视图包装
        ├─ FileManager + Buffer                   ← 文档生命周期
        ├─ FindReplaceDlg / Session / Macro…
        └─ PluginsManager（NPPM_* 消息 + WM_NOTIFY）
                │
                ▼
          Scintilla Document（实际文本与撤销栈）
```

关键抽象：

| N++ 概念 | 职责 | 对本项目的启发 |
| --- | --- | --- |
| **Buffer** | 一份文档：路径、脏标记、编码、语言类型、Scintilla Document 句柄、大文件标记 | 「笔记实体」与「编辑器实例」分离；未命名文档 / 已落盘文档状态机 |
| **FileManager** | 打开/保存/关闭/重载；全局 `MainFileManager` | 后端笔记 CRUD + 冲突检测（外部修改）的唯一入口 |
| **DocTabView** | 标签 ↔ BufferID 映射；激活/关闭/按路径查找 | 前端多标签状态；同一路径只开一个 tab |
| **ScintillaEditView** | 对 Scintilla 的 `SCI_*` 封装：边距、折叠、书签、高亮 | 编辑器适配层：换 Monaco/CM6 时业务层不感知 |
| **双视图** | 主/副编辑区，可对比、拖标签 | 二期：分屏对照（笔记 ↔ 对话引用 / 两笔记） |
| **Session** | XML 记录打开文件集，可恢复 | `data/` 下会话态 JSON/md：上次打开的笔记列表与光标 |
| **Plugin 消息** | `NPPM_*` 查询 / `NPPN_*` 通知 | Agent 侧「读当前笔记 / 插入选区」用事件或工具，而不是 Win32 消息 |

### 3.4 功能能力分层（按「该不该学」）

**强相关（MVP 应学）**

- 多文档标签；未命名新页；脏标记（`*`）与关闭前提示
- 打开/保存/另存；编码与换行感知（至少 UTF-8 + LF/CRLF）
- 撤销/重做、查找/替换（单文件）
- 会话恢复（上次打开的文档集合）
- 轻量偏好：字体、字号、主题、自动换行、Tab 宽度

**中相关（二期）**

- 分屏（双视图）
- 语法高亮（按扩展名：md / txt / json / py …）
- 书签 / 行号 / 折叠
- 目录树侧栏（项目内笔记浏览）
- 文件被外部修改时的 reload 提示

**弱相关 / 刻意不做（至少一期不做）**

- 80+ 语言 Lexer、用户自定义语言、Lexer 插件
- Plugin Admin / DLL 插件生态
- 宏录制与快捷键映射器（完整版）
- Find in Files（仓库级搜索：可复用已有 `fs_grep`，不必仿 N++ UI）
- 打印、系统文件关联、注册表扩展

### 3.5 生态旁证（仅作边界参考）

| 项目 | 关系 | 启示 |
| --- | --- | --- |
| NotepadNext | Qt 重实现 N++ 体验 | 「体验可移植，实现不必同构」 |
| notepad-web | CM6 + NotepadNext Lua 定义，GPL | Web 可仿外观，但 GPL 资源包慎用 |
| NotepadAI | N++ 风格 + AI/终端 | 与 SP「Agent 中心」方向接近，但我们应 **AI 是宿主、编辑器是工具面**，不是反过来 |

## 4. Second Person 现状对照

### 4.1 已有、可复用

| 能力 | 现状 | 记事本可怎么用 |
| --- | --- | --- |
| 前端路由 | `/chat` `/memory` `/workshop` `/settings` | 新增 `/notes` 或聊天侧「记事本」面板 |
| md 主副本 | `data/` 为权威；SQLite 可重建 | 笔记落盘为 `data/notes/*.md`（或项目子目录） |
| 项目工作区 | `projects` + 沙箱四档 + `fs_*` | 项目笔记可落在项目根或 `data/projects/{id}/notes/` |
| 知识库 Ingest | `raw_docs` / Distill | 「提升为知识」一键导入，不做自动污染记忆 |
| 记忆宫殿 | 检索 / 候选门禁 | 笔记默认是工作草稿，不自动进 L3 |
| 对话附件 / 引用 | Chat 附件、划词侧边会话 | 把当前笔记选区「发送到对话」 |
| FilePicker 等 UI | 已有文件选择模式 | 可复用打开本地文件的交互 |

### 4.2 缺口（相对 N++ 心智）

- 没有一等公民的「多标签文本缓冲」UI。
- 没有「未命名草稿 / 脏状态 / 关闭确认 / 会话恢复」产品流。
- Agent 能通过 `fs_read/write` 改文件，但用户缺少稳定的人工编辑入口与「当前正在写的笔记」上下文注入。
- MemoryView 偏治理与图谱，不是随手记事本。

### 4.3 产品边界建议（避免和现有域打架）

```text
记事本（工作草稿）  ≠  记忆宫殿（已确认个人事实）
                    ≠  知识库 raw_docs（导入文档）
                    ≠  Chat 输入框草稿（composerDraft，会话级临时）
```

| 内容类型 | 默认去向 | 用户动作 |
| --- | --- | --- |
| 随手笔记 | `data/notes/` | 自动保存 |
| 项目备忘 | 项目绑定笔记 | 自动保存；Agent 在项目会话可读写 |
| 要长期记住 | — | 「提炼为记忆候选」→ 走现有 write gate |
| 要当资料 | — | 「导入知识库」→ Ingest |

## 5. 推荐目标架构（映射 N++，贴合本仓库）

```text
Vue NotesView / NotesPanel
  ├─ NoteTabBar          ≈ DocTabView
  ├─ NoteEditorHost      ≈ ScintillaEditView（内部 CodeMirror 6）
  └─ NoteSidebar（可选） ≈ Project Panel / 文件树
        │ HTTP / SSE
        ▼
FastAPI notes API
  ├─ NoteStore（文件主副本）  ≈ FileManager
  ├─ NoteMeta（索引，可 SQLite）≈ Buffer 元数据
  └─ SessionState（打开集/光标）≈ Session XML
        │
        ├─ 可选：注入 PromptAssembler context.notes（当前打开/选中笔记摘要）
        └─ 可选：工具 notes_list / notes_read / notes_append（或复用 fs_*）
```

### 5.1 数据建议

```text
data/notes/
  index.json                 # 可选：列表缓存；权威仍是各 md
  {note_id}.md               # frontmatter: id, title, project_id?, updated_at, tags?
data/notes_sessions/
  default.json               # 打开的 note_id 列表、active、cursor/scroll
```

原则与现有记忆系统一致：**md（或明文文件）为主副本**；索引可丢可重建。

### 5.2 编辑器选型

| 方案 | 优点 | 缺点 | 建议 |
| --- | --- | --- | --- |
| **CodeMirror 6** | MIT、模块化、适合「记事本 + Markdown」、包体可控 | 要自己拼搜索/高亮扩展 | **首选** |
| Monaco | VS Code 级体验、diff/折叠强 | 体积大、Worker/打包成本高 | 若二期强代码编辑再上 |
| textarea | 零依赖 | 无高亮/差体验 | 仅原型 |
| 嵌入 N++/Scintilla | 体验「正宗」 | GPL、仅 Windows 原生、与 Vue 栈冲突 | **否决** |

### 5.3 与 Agent 的耦合（差异化价值）

N++ 没有一等 AI；Second Person 应把记事本当 **可引用上下文**：

1. **当前笔记摘要**（标题 + 前后 N 字 / 大纲）进入 `context.*`，受 token 预算限制。
2. 工具或 UI：「把选区插入对话」「根据对话结果写回笔记」「从笔记生成待办」。
3. 遵守现有沙箱：无项目会话写 `data/notes/`；项目会话可写项目内笔记路径；`danger-full-access` 才放开任意路径。
4. Langfuse：笔记 CRUD 可挂独立操作或挂到触发它的 `agent.turn`；**不要**为「自动保存」每键一开 trace。

## 6. 分阶段落地建议

### P0 — MVP（建议 1～2 周量级）

- 新路由或 Chat 内抽屉：「记事本」
- 多标签：新建 / 重命名 / 关闭（脏提示）
- 纯文本或 Markdown 编辑（CM6 最小集）
- 防抖自动保存到 `data/notes/`
- 列表 API：`GET/POST/PUT/DELETE /notes`
- 打开集恢复（刷新不丢 tab）
- UI：「发送选区到对话」

验收：用户能开 3 个笔记标签，刷新后恢复，内容落盘可备份。

### P1 — 工作区与发现

- `project_id` 绑定；侧栏树
- Markdown 预览分栏
- 单文件查找替换
- 「导入知识库」「生成记忆候选」按钮（复用现有管线）
- 外部修改探测（mtime）

### P2 — 增强编辑（按需）

- 有限语法高亮（md/json/py）
- 双栏分屏
- 与 `fs_grep` 联动的「在笔记中搜索」
- 快捷键体系（Ctrl+S / Ctrl+F / Ctrl+Tab）

### 明确不做（除非产品另行立项）

- N++ 插件 DLL 兼容
- 完整宏系统
- 100+ 语言高亮包（尤其 GPL 来源的语言定义）
- 替换 Chat / Memory 主界面

## 7. 风险与约束

| 风险 | 说明 | 应对 |
| --- | --- | --- |
| GPL 传染 | 直接使用 N++/NotepadNext 代码或捆绑其语言定义 | 只参考架构；编辑器用 MIT 组件 |
| 与记忆写通道混淆 | 笔记被误当长期记忆写入 | 默认不进 Distiller；显式按钮才提升 |
| Token 膨胀 | 每轮注入全文 | 只注入活跃笔记摘要/选区 |
| 大文件 | N++ 有 large-file 路径 | MVP 限制体积（如 2～5MB），超限只读或拒开 |
| 与客户端化路线 | `客户端化架构调研报告` 指向 Tauri | Notes 先做 Web；桌面壳阶段复用同一前端模块 |
| 自动保存冲突 | Agent `fs_write` 与用户同编 | mtime/etag + 冲突弹窗；同一文件单写者优先 |

## 8. 对实现团队的直接建议

1. **不要 fork Notepad++ 进本仓库。** 读架构、抄交互，不抄代码。
2. **笔记实体独立于 chat composerDraft 与 memories。** 三个状态机分开。
3. **API 与存储先定契约，再选编辑器皮肤。** Buffer/FileManager 心智比「像不像 N++」重要。
4. **第一期交付「能记、能恢复、能丢进对话」**，语法高亮与分屏不要挡 MVP。
5. 若产品文案要「像记事本」：对标的是 N++ 的**多标签 + 脏标记 + 会话恢复**，不是其插件商店。

## 9. 参考链接

- 仓库：https://github.com/notepad-plus-plus/notepad-plus-plus  
- 构建：https://github.com/notepad-plus-plus/notepad-plus-plus/blob/master/BUILD.md  
- 插件与消息：https://npp-user-manual.org/docs/plugins/ 、https://npp-user-manual.org/docs/plugin-communication/  
- Scintilla：https://www.scintilla.org/  
- 本仓库相关：`README.md`、`docs/CURRENT_PRODUCT_CAPABILITIES.md`、`docs/PROJECTS.md`、`docs/客户端化架构调研报告.md`

---

## 附录 A：N++ → SP 概念对照速查

| Notepad++ | Second Person 建议实现 |
| --- | --- |
| Buffer | `Note` 记录 + 文件路径 |
| FileManager | `notes` 路由服务 / NoteStore |
| DocTabView | `NoteTabBar` + pinia/store |
| ScintillaEditView | `NoteEditorHost`（CM6） |
| Session XML | `data/notes_sessions/*.json` |
| FindReplaceDlg | CM6 search 扩展或自研轻量对话框 |
| Plugins NPPM_* | Agent tools / 前端事件总线 |
| Dual view | P2 分屏组件 |

## 附录 B：建议的最小 API 草图（非契约，供评审）

```text
GET    /notes?project_id=                 → 列表
POST   /notes                             → 创建 {title?, project_id?, body?}
GET    /notes/{id}                        → 正文 + meta
PUT    /notes/{id}                        → 更新正文/标题（带 if-match/mtime）
DELETE /notes/{id}                        → 删除（可软删）
GET/PUT /notes/session                    → 打开集与光标
POST   /notes/{id}/promote                → {target: memory_candidate|knowledge}
```

正式字段以未来写入 `API_CONTRACT.md` 为准；上表仅服务方案讨论。
