# 长文档注入与 PDF 解析完整方案

> 状态：已落地（开发 + 功能测试通过）  
> 决策依据：总额预算第二种策略 + 既有 PDF 解析缺口补全  
> 对照：DeepSeek Harness（句柄 + 按需读）；本方案在预算内仍走注入，超预算才句柄续读

---

## 1. 目标与原则

### 1.1 要解决的问题

1. **长文档「读不全」**：聊天侧把正文硬塞进 prompt，超预算静默截断，模型无法续读后半。  
2. **PDF「解不出」**：扫描件无页 OCR、聊天弱解析、加密/空文本静默、`web_fetch` 未接线。

### 1.2 设计原则

| # | 原则 |
|---|------|
| 1 | **总额 35 000 字**内的附件正文直接进 prompt（保持现有「上传就能聊」体验） |
| 2 | **超过部分不假装已读**：明确告知还有剩余，并给出可用工具续读的路径 |
| 3 | 续读复用现有 **`fs_read(offset/limit)`**（及 `fs_grep`），不另造平行读工具 |
| 4 | **先解析出可靠正文**，再谈注入/续读；解析缺口与注入策略正交，必须一并补全 |
| 5 | 短文路径不变；长文路径对用户与模型都**可预期** |

### 1.3 非目标（本方案不做）

- 上传即全文 RAG / 向量检索附件  
- 把知识库 Distill 记忆条数当成「解析完整度」  
- 无页 OCR 时宣称支持扫描 PDF

---

## 2. 总体架构

```
上传附件
  → 统一富解析（与知识库对齐；扫描件可走页 OCR）
  → 落盘：source + parsed.txt（全文，content-addressed）
  → 本轮拼装 context_for：
        按附件顺序消耗 INLINE_BUDGET_CHARS = 35_000
        ├─ 预算内：正文直接写入 prompt
        └─ 预算外：不写入正文；写入「剩余说明 + 可读路径」
  → Agent 回答不全时：fs_read / fs_grep 续读 parsed.txt
```

与现状差异：

| 环节 | 现状 | 本方案 |
|------|------|--------|
| 解析 | 聊天 `extract_text`（弱） | 聊天改 `extract_text_async` 富解析 + 扫描回退 |
| 注入帽 | 约 48k，截断后难续读 | **总额 35k 注入**；超出改句柄续读 |
| 续读 | 基本无 | `fs_read` 读 `parsed.txt`（纳入 read_roots） |
| web_fetch PDF | 「未配置解析器」 | 注入解析器，大结果可 spill + 续读 |

---

## 3. 注入与续读规则（第二种策略）

### 3.1 常量

| 常量 | 建议值 | 说明 |
|------|--------|------|
| `INLINE_BUDGET_CHARS` | **35_000** | 本轮所有附件注入正文的**总额**（可配置） |
| `CHAT_CONTEXT_MAX_CHARS` | 48_000（可保留） | 安全上限；正常应由 35k 先触发句柄，不再顶满截断 |
| `PREVIEW_MAX_CHARS` | 8_000 | 仅前端预览，**不参与**长短判定 |
| `HANDLE_HEAD_CHARS` | 0～2_000（可选） | 超预算附件可先注入极短开头/目录；默认 **0**（只给句柄，避免再吃预算） |

判定单位：**解析后的 Unicode 字符数**（`len(text)`），不是文件 MB、不是页数。

### 3.2 分流算法

对 `attachment_ids` 按顺序处理，维护 `remaining = INLINE_BUDGET_CHARS`：

```
for each attachment:
  if not parsed or chars == 0:
      注入失败块：「未能解析…」；不占正文预算（或只占固定提示长）
      continue

  if remaining <= 0:
      全部走句柄块（见 §3.3）
      continue

  if chars <= remaining:
      注入全文
      remaining -= chars
  else:
      # 本附件只有前 remaining 字进 prompt，后半必须可续读
      注入 text[:remaining]
      追加剩余告知（§3.3），路径指向该附件完整 parsed.txt
      remaining = 0
```

要点：

- **总额**维度：多附件共享 35k，不是单附件 35k。  
- **半截附件**：前半注入 + 明确「全文 N 字，已注入 M 字，其余用 fs_read」；句柄指向**整份** `parsed.txt`（模型用 offset 跳过已读行，或从文件头自行定位）。  
- 后续附件若预算已尽：整份只给句柄，不再塞正文。

### 3.3 剩余告知文案（模型可见，须稳定可解析）

单附件部分注入示例：

```text
【附件：报告.pdf】
…（已注入的前 M 字）…

（附件全文共 N 字；本轮仅注入前 M 字。剩余内容未进入上下文。
完整解析文本只读路径：<absolute-or-sandbox-path>/parsed.txt
请使用 fs_read(path, offset, limit) 或 fs_grep 继续读取未覆盖部分后再作答；
不要假设未注入部分不存在。）
引用：sha256:…
```

整份未注入（预算已尽）示例：

```text
【附件：附录.pdf】
（全文共 N 字，本轮附件预算已用尽，正文未注入。
完整解析文本只读路径：…/parsed.txt
请使用 fs_read / fs_grep 按需读取。）
引用：sha256:…
```

### 3.4 与 `fs_read` 的衔接

现有能力（已具备，方案复用）：

- `tools/fs/io.py`：`read_file` 支持 `offset` / `limit`，默认约 2000 行 / 50KiB  
- Prompt 已有 spill 续读指引（`prompt_assembler` / `web_fetch`）

本方案需补：

1. **权限**：将会话用到的 `temp/attachments/.../parsed.txt`（及可选 `source*`）加入该回合 `read_roots`。  
2. **路径稳定性**：注入文案中的 path 必须与 `_resolve_input_path` 可解析形式一致（绝对路径或约定相对根）。  
3. **系统提示加一句**：当附件块含「剩余未注入」时，回答涉全文/后半/统计前必须先 `fs_read`。  
4. **不要对 `fs_read` 再做「读完又截断且无路径」**；若单次 read 触顶，返回中已有行号，模型继续 offset（与 Harness 一致）。

### 3.5 用户侧体验

| 场景 | UI / 行为 |
|------|-----------|
| Σ chars ≤ 35k | 与现在类似，可正常对话 |
| 有附件被部分/全未注入 | Toast 或附件卡片：`已注入前 M/N 字，其余可由助手按需读取` |
| 预览仍 8k | 文案区分「预览截断」≠「对话未注入」 |
| 解析失败 | 保持/强化现有 toast；不进入续读路径 |

---

## 4. 文档解析补全（与注入正交）

注入再完善，解析为空也无意义。以下补齐此前审计缺口。

### 4.1 根因回顾

| ID | 问题 | 影响 |
|----|------|------|
| P0-a | 扫描 PDF 无页级 OCR | 正文为空 |
| P0-b | 聊天附件只用弱 `extract_text` | 丢内嵌图信息；与知识库不一致 |
| P0-c | 超长注入截断不可续读 | 假「读完」（由 §3 解决） |
| P0-d | `web_fetch` 未传 `pdf_extract_fn` | Agent 抓 PDF 失败 |
| P1-a | 加密/损坏 → 空串静默 | 假成功 |
| P1-b | 详情/回顾重跑弱解析 | 与入库不一致 |
| P1-c | `pypdf` 等依赖声明不齐 | 环境漂移 |

### 4.2 解析统一

**聊天附件**与**知识库 Ingest**统一走：

```text
extract_text_async(path, image_fn)
  → PDF: _extract_pdf_rich
  → 若全文过空且疑似扫描件：页渲染 → OCR/VLM 回退（§4.3）
```

`AttachmentStore.upload_and_parse` 改为调用异步富解析（注意 `PARSE_CONCURRENCY` 与 VLM 成本）。

### 4.3 扫描件页级 OCR（P0-a）

策略：

1. 富解析后若 `空白页占比过高`（如 >50% 页 `extract_text` 为空）或总 `chars` 过低相对页数 → 触发回退。  
2. 回退：逐页（或按需页）渲染为图 → 现有 `image_parse_engine`（vlm / ocr）。  
3. 配置项建议：`pdf_page_ocr: off | auto | always`（默认 `auto`）。  
4. 产品若暂不上 OCR：`auto` 在空文本时返回明确错误 `empty_text_scanned_pdf`，禁止「导入成功 0 字」。

### 4.4 错误分类（禁止静默空串）

| code | 含义 | 聊天 | 知识库 |
|------|------|------|--------|
| `ok` | 有正文 | `parsed=true` | 正常分块提炼 |
| `empty_text` | 无字非扫描 | 失败提示 | 失败/强告警 |
| `empty_text_scanned_pdf` | 疑似扫描 | 提示开 OCR 或转换 | 同上 |
| `encrypted` | 加密 | 提示解密后上传 | 同上 |
| `corrupt` | 损坏/无法打开 | 失败 | 失败 |
| `too_large` | >50MB | 现有拒绝 | 现有拒绝 |

### 4.5 web_fetch PDF（P0-d）

- `web_fetch_tool` 传入基于 tempfile + `extract_text` / 富解析的 `pdf_extract_fn`。  
- 解析结果若超大：走现有 spill，prompt 指引 `fs_read` 续读（与附件超预算同一套心智）。

### 4.6 其它一致性

- `get_document_detail` / 回顾补提炼：优先读缓存的 `extracted_text`，否则富解析，避免弱路径。  
- 非图片 PDF/DOCX 将解析全文写入 `raw_docs.extracted_text`（或附件 `parsed.txt` 已存在则复用）。  
- `pyproject.toml` 对齐声明 `pypdf`；RapidOCR 作可选 extras。

---

## 5. 端到端流程

```
[上传] POST /chat/attachment
   → 富解析 +（可选）页 OCR
   → 写 objects/{sha}/source* + parsed.txt + meta.json
      meta: chars, parsed, parse_code, pages?, inline_eligible

[发送] POST /chat/send + attachment_ids
   → context_for(budget=35000)
      预算内正文 + 超预算句柄块
   → 本回合 read_roots ∪= 本批 attachments 的 object 目录
   → 用户消息 = attachment_context + 用户正文

[Agent]
   → 直接依据已注入正文回答；若需后半 / 其它附件
   → fs_read(parsed.txt, offset, limit) / fs_grep
```

知识库路径不改「35k 注入」逻辑：仍分块 Distill；但共享同一套解析与错误码。

---

## 6. 关键改动面

| 模块 | 改动 |
|------|------|
| `app/attachment_store.py` | 富解析；`context_for` 总额 35k + 句柄剩余告知；meta 增加 `parse_code` |
| `app/routes/chat.py` | 发送时把附件目录加入 sandbox `read_roots` |
| `tools/fs/policy.py` / agent ctx | 允许读 `temp/attachments/...` |
| `agent/prompt_assembler.py` | 附件续读系统提示 |
| `scheduler/ingest.py` | 页 OCR 回退；错误分类；详情用缓存/富解析 |
| `tools/builtin.py` + `web_fetch.py` | 接线 `pdf_extract_fn` |
| `frontend/ChatView.vue` | 注入比例提示；区分预览截断 / 对话未注入 / 解析失败 |
| 配置 | `INLINE_BUDGET_CHARS=35000`；`pdf_page_ocr` |
| 测试 | 单附件半截、多附件吃满预算、续读路径权限、扫描件、web_fetch PDF |

---

## 7. 分阶段落地

### Phase A — 注入/续读闭环（优先体感）

1. `INLINE_BUDGET_CHARS=35000` 总额算法 + 剩余告知文案  
2. 附件 `parsed.txt` 进 `read_roots`  
3. 系统提示：有剩余则须 `fs_read`  
4. 前端卡片显示 `已注入 M/N`  
5. 聊天改富解析（先不做页 OCR 也可上）

### Phase B — 解析补全

1. 页渲染 OCR/VLM（`pdf_page_ocr=auto`）  
2. `parse_code` 错误分类；知识库禁止空成功  
3. `web_fetch` PDF 解析器接线  
4. `extracted_text` 缓存 + 详情一致

### Phase C — 打磨

1. 依赖声明对齐  
2. 观测：注入字数、句柄次数、续读次数、空页占比、OCR 耗时  
3. 按模型上下文调 35k 默认值

---

## 8. 验收标准

| # | 场景 | 期望 |
|---|------|------|
| 1 | 单附件 20k 字 | 全文进 prompt，无「剩余」句柄 |
| 2 | 单附件 80k 字 | 前 35k 进 prompt；明确剩余 45k + 路径；`fs_read` 可读全文 |
| 3 | 附件 30k + 20k | 第一个全文注入；第二个注入 5k + 剩余句柄（或按实现：第二份仅句柄——以算法 §3.2 为准：第二份注入 min(20k,5k)=5k 并句柄） |
| 4 | 三附件各 15k | 前两个全文（30k），第三个注入 5k + 句柄 |
| 5 | 模型问「最后一章」且未注入 | 应出现 `fs_read`（或等价）后再答，而非臆造 |
| 6 | 有文字层多页 PDF | 解析 chars≈全文 |
| 7 | 扫描 PDF + OCR on | 有正文或明确 `empty_text_scanned_pdf` |
| 8 | web_fetch arXiv PDF | 非「未配置解析器」 |
| 9 | 加密 PDF | 明确错误，非静默空 |
| 10 | UI 预览 8k | 不提示为「解析失败」 |

---

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| 模型不调用 `fs_read` | 系统提示强化；关键问答可在服务端检测「未注入却声称已读全文」（后期） |
| 路径沙箱拒读 | 发送回合显式挂 read_roots；单测覆盖 |
| 页 OCR 成本/延迟 | 默认 `auto`；并发帽；可 off |
| 35k 对小上下文模型仍偏大 | 配置化；按模型档案覆盖 |
| 半截注入导致行号难对齐 | 告知用「全文路径 + 字/行 offset」；或超预算附件改为「零正文 + 全句柄」简化（可配置 `partial_inline: true/false`） |

**可选简化开关**：`partial_inline=false` 时，任一附件不能整篇装入剩余预算 → **该附件整份改句柄**（不半截注入）。总额仍是 35k，实现更简单，续读语义更清晰。默认建议 **`partial_inline=true`**（贴合第二种「吃满预算」），若续读质量差再改 false。

---

## 10. 决策摘要

| 项 | 决定 |
|----|------|
| 长短划分 | **本轮附件解析字数总额**，阈值 **35 000** |
| 预算内 | 直接进 prompt（现逻辑） |
| 预算外 | **明确告知剩余** + **完整 parsed 路径** + **`fs_read`/`fs_grep` 续读** |
| 多附件 | 按顺序吃预算；先到先得 |
| 解析 | 聊天与知识库富解析对齐；扫描页 OCR；错误分类；web_fetch 接线 |
| 不做 | 静默截断当读完；空解析假成功 |

---

## 11. 相关文档

- 问题审计原稿：`docs/PDF解析完整性问题方案.md`（根因与证据；本方案为落地版）  
- 现有续读先例：spill + `fs_read`（`tools/web_fetch.py`、`agent/prompt_assembler.py`）  
- 对照实现：DeepSeek Harness 文件句柄 + `read` 窗口（本方案仅在超 35k 时对齐其续读侧）
