# 项目工作区 · 参考文档

Second-Person 从 v1.1 起引入「项目工作区」概念：加载本地目录作为项目，
在项目下的会话里，模型可读写目录内文件、检索项目专属记忆与知识；不加载
项目也能像旧版本一样对话（IM 侧完全兼容）。

规范起草 v5 参见项目讨论；本文档保留最终形态与已交付里程碑摘要，作为
后续维护的入口。

## 交付里程碑

- **M1** 项目地基 + 归档 + 目录对话框
- **M2** 记忆 / 知识 / 图谱严格隔离
- **M3** fs 工具族 + 沙箱四档
- **M4** 项目浏览 + 目录丢失感知 + 手动会话归档
- **M5** 备份 / --rebuild-index / IM 归档兼容

## 顶层业务规则（不可违反）

1. **ID 与枚举**
   - `project_id` 形如 `proj_[0-9a-f]{8}`（`memory.naming.project_id()`）
   - `projects.status` ∈ `{active, archived}`
   - `projects.sandbox_mode` ∈ `{read-only, workspace-write, danger-full-access}`
   - 会话派生档位加 `legacy-workspace`（无项目/IM 专用）
   - `sessions.archived_source` ∈ `{project, manual}`（NULL = 未归档）

2. **路径归一化**
   - `agent.projects.normalize_path` / `validate_project_path` 是唯一入口
   - 存储：display（正斜杠、原大小写）+ path_key（全小写用于唯一约束）
   - 拒：磁盘根、系统目录（`C:/Windows` `/etc` 等）、非目录

3. **记忆 / 知识 / 图谱硬过滤**（M2）
   - 项目 A 会话：`project_id = 'A' OR project_id IS NULL`
   - 无项目 / IM：`project_id IS NULL`
   - 归档项目：完全冷藏（不参与检索、不进 reviewer/lint/lifecycle）
   - Distiller 归属白名单：`preference / profile / style / output_format /
     interaction_habit / general_knowledge` → 强制全局

4. **沙箱四档权能矩阵**（M3）

   | 档位 | fs_read/list/... | fs_write/edit | shell |
   |---|---|---|---|
   | `read-only` | 项目根 | 拒 | 拒 |
   | `legacy-workspace`（无项目默认） | `data/workspace/` | `data/workspace/` | ✓（黑名单+30s） |
   | `workspace-write`（项目默认） | 项目根 | 项目根 | 拒 |
   | `danger-full-access` | 全盘 | 全盘 | ✓（cwd=项目根） |

5. **归档 vs 永久删除**
   - 归档项目 → 联动归档 `archived_source='project'` 的会话
   - 恢复 → 仅恢复联动归档，不动 `archived_source='manual'` 的会话
   - 永久删除仅对 `status='archived'` 允许；全删该项目所有相关数据；
     **本地项目目录本身永不删除**

## 数据模型

### 新表（migration 044）

- `projects` (id / path / path_key / title / display_order / sandbox_mode /
  ignore_extra / status / archived_at / created_at / updated_at)
- `session_policy_events` (id / session_id / event_type / payload / created_at)
- `fs_observations` (session_id / target_key / version / observed_at)

### 增字段

- `sessions` + `project_id / archived / archived_source / archived_at /
  sandbox_mode`
- `memories / raw_docs / local_dirs / memory_entities /
  memory_entity_links / memory_links / graph_layout` + `project_id`
- `memories_fts` 重建含 `project_id UNINDEXED` 列
- `memory_entities.entity_id` 通过 `naming.entity_id(name, disamb, project_id)`
  的 sha1 隔离，同名不同项目自然独立成两条记录

### md 主副本

- `data/projects/{id}.md`：frontmatter 存全字段；`--rebuild-index` 可回灌
  `projects` 表
- `data/memory/{id}.md`：frontmatter 增 `project_id` 字段

## API 面

| 端点 | 用途 |
|---|---|
| `GET /projects?status=active\|archived\|all` | 列表 |
| `POST /projects` | 创建（幂等 by path_key） |
| `PATCH /projects/{id}` | 改名 / 排序 / 档位 / ignore_extra |
| `POST /projects/{id}/archive` | 归档（联动会话） |
| `POST /projects/{id}/unarchive` | 恢复 |
| `DELETE /projects/{id}` | 永久删除（仅 archived） |
| `POST /projects/{id}/relocate` | 重定位 |
| `GET /projects/browse?path=` | 目录对话框 API |
| `POST /projects/browse/mkdir` | 新建文件夹 |
| `GET /projects/{id}/tree?path=&depth=` | 项目文件树 |
| `GET /projects/{id}/preview?path=&offset=&limit=` | 文件预览 |
| `POST /projects/{id}/search` | glob / grep 搜索 |
| `POST /chat/session/{sid}/sandbox-mode` | 切档 |
| `GET /chat/session/{sid}/sandbox-mode` | 查档 |
| `POST /chat/session/archive` | 手动归档单会话 |
| `POST /chat/session/unarchive` | 手动恢复 |

## 工具层（M3 + M4）

`tools/fs/` 目录：

- `errors.py`：FsError + 10 个错误码枚举
- `resolver.py`：realpath + 双次围栏（防 TOCTOU）
- `ignore.py`：默认 + 项目 .gitignore 合并的忽略匹配
- `io.py`：分页 read、原子写、字面 edit、二进制拒、版本乐观锁
- `diff.py`：unified diff + 增删行数摘要
- `observation.py`：fs_observations 表读写
- `policy.py`：四档 PolicyStore（含 session_policy_events fold）
- `workspace.py`：WorkspaceResolver + WorkspaceContext
- `tools.py`：7 个 fs 工具 + register_fs_tools

ToolExecutor 通过 `ToolSpec.needs_workspace = True` 识别 fs 工具，
调用前自动注入 `_ws_ctx`。

## 前端组件

- `SessionSidebar.vue`：新增「工作区」段（basename 显示、hover 出全路径）
- `AddProjectModal.vue`：自研目录对话框（面包屑 / 编辑路径 / 新建文件夹）
- `SandboxModeChip.vue`：项目会话档位切换（含 danger 二次 confirm）
- `FsDiffCard.vue`：unified / side-by-side diff 卡
- `FilePickerPanel.vue`：@ 文件面板（Ctrl+P 项目切换器同款交互）
- `stores/projects.js`：项目 CRUD 共享状态
- `api/projects.js`：完整 API 客户端

## 快捷键

- `Ctrl/⌘+K` 搜索
- `Ctrl/⌘+P` 项目切换器
- `Ctrl/⌘+Shift+N` 项目内新建会话

## 集成点

- **Backup**：`data/projects/` 已入包（M5-2）；恢复后 `--rebuild-index`
  可从 md 重建 `projects` 表
- **--rebuild-index**：新增 `rebuild_projects_from_md()` 从 md
  frontmatter 回灌项目表（M5-3）
- **IM 适配器**：归档会话下一条消息新建替代会话（M5-4）
- **调度器**：每小时扫描目录丢失，命中推 `project_dir_missing` 通知（M4）
- **Retriever**：`hybrid_presearch(query, project_id=?)` 硬过滤 + 排除归档项目
- **Distiller**：`resolve_memory_project_id(item, session_project_id)`
  按 domain 白名单归属
- **Palace**：`upsert_index / sync_fts / replace_links / sync_entities` 全部
  接受 `project_id` 参数
- **AgentCore._runtime_context**：拉 `session.project_id` 传给 Retriever，
  组装 `[项目] / [路径] / [沙箱策略]` dynamic 段并追加为 `context.project`
  事件（不入 static，KV cache 不击穿）

## 性能与 KV cache

- 新 fs 工具族 schema 一次性击穿（上线时无法避免）
- `base_rules_fs.md` 加入 static rules 一次性击穿
- 「项目 + 策略」信息只进 dynamic context 尾部，等价 memory/handoff 同层
- `shell_exec` **始终注册**，档位不足在 handler deny，tools schema 字节稳定

## 测试

`tests/test_projects_m1.py` / `m2.py` / `m3.py` / `m4.py`：全部 76+ 用例
覆盖数据模型、API 契约、Retriever 隔离、entity_id 分裂、fs 工具 + 沙箱
四档矩阵、目录浏览、手动归档幂等。全库回归 240+ tests，零失败。

## 已知不做

- 内核级沙箱（cgroups / Seatbelt / AppContainer）
- 桌面客户端目录选择器（用自研 `browse` API 兜浏览器无绝对路径的限制）
- 多 workspace root
- 项目根 recursive FileWatcher（大代码库开销不划算；`fs_write/edit` 用
  `make_version()` 自然触发 `FS_STALE_VERSION`）
- 模型级 `fs_delete / rename / move`（避免灾难性误操作，让用户去 Explorer）
- 跨项目实体消歧 / 图谱合并
