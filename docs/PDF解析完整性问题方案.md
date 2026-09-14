# PDF 解析完整性问题方案

> 状态：问题分析（未改码）  
> **落地版（注入 35k + 续读 + 解析补全）见：[长文档注入与PDF解析完整方案.md](./长文档注入与PDF解析完整方案.md)**  
> 范围：知识库 Ingest / 聊天附件 / Agent `web_fetch`  
> 结论先行：**系统没有「只解析前 N 页」的硬限制**；用户感知的「无法解析完整 PDF」主要来自扫描件无页级 OCR、聊天弱解析 + 48k 字截断、以及 `web_fetch` 解析器未接线。

---

## 1. 现象与结论

用户侧常见表现包括：

| 表现 | 常见真实原因 |
|------|----------------|
| 上传后提示「未能提取出文字」 | 扫描件 / 加密 PDF / 损坏；`pdfplumber.extract_text()` 为空 |
| 预览只有前几页 / 「已截断」 | 预览帽 8 000 字，不等于解析失败 |
| 对话里模型只答前半本 | 注入预算约 48 000 字截断（解析可能已完整） |
| 知识库导入成功但 0 条记忆 | 文本层为空仍落盘；或 distill 全跳过 |
| Agent 抓论文 PDF 无正文 | `web_fetch` 未传入 `pdf_extract_fn` |

**一句话**：有文字层的 PDF 会尽量读完全部页面；扫描件、加密件、聊天上下文预算、未接线的 web_fetch 会系统性表现为「解不完整」。

---

## 2. 现状架构（三条不一致路径）

```
A. 知识库 Ingest（富解析）
   /import/document | IM 入站 | FolderScanner
        → IngestManager.ingest_file
        → extract_text_async → _extract_pdf_rich (pdfplumber 文字 + pypdf 内嵌图)
        → chunk_text → Distiller → memories + raw_docs

B. 聊天附件（弱解析 + 上下文截断）
   POST /chat/attachment
        → AttachmentStore.upload_and_parse
        → extract_text（仅 pdfplumber 纯文字）
        → context_for（≤48k 字注入模型）

C. Agent web_fetch（解析器未装配）
   web_fetch_tool → web_fetch_result
        → Content-Type=pdf 时依赖 pdf_extract_fn
        → 实际未传入 → 固定「[PDF 内容，未配置解析器]」
```

核心文件：

| 路径 | 职责 |
|------|------|
| `scheduler/ingest.py` | PDF 富/弱解析、分块、入库 |
| `app/attachment_store.py` | 聊天附件上传与上下文拼装 |
| `tools/web_fetch.py` / `tools/builtin.py` | URL 抓取 PDF 分支 |
| `frontend/src/views/ChatView.vue` | 解析失败 toast（扫描件/加密） |

---

## 3. 根因分析（按优先级）

### P0-1 扫描件 / 纯图 PDF：无页级 OCR

**证据**：`_extract_pdf_rich` 仅调用 `page.extract_text()`；OCR/VLM 只处理 `pypdf` 抽出的 **Image XObject**，不会把整页渲染成图再 OCR。

```199:240:scheduler/ingest.py
async def _extract_pdf_rich(path: Path, image_fn) -> str:
    def _pdf_texts() -> list[str]:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return [p.extract_text() or "" for p in pdf.pages]
    # ... page.images → _image_blob_to_text ...
```

多数扫描 PDF 是整页位图，且未必出现在 `page.images` → **富解析也救不了**。  
前端已写明该假设：`ChatView.vue` toast「可能是扫描件、加密 PDF 或空文档」。

### P0-2 聊天附件刻意走弱路径

```103:108:app/attachment_store.py
            from scheduler.ingest import extract_text
            async with self._parse_slots:
                text = await asyncio.to_thread(extract_text, source)
```

与知识库 `extract_text_async` / `_extract_pdf_rich` 能力不对齐：无内嵌图 VLM，扫描件必空。

### P0-3 对话「完整」被 48k 字预算截断

```160:166:app/attachment_store.py
                suffix = (f"\n\n（附件全文共 {len(text)} 字；本轮内容已按上下文预算截断，"
                          f"引用：{attachment_id}）")
                allowance = max(0, remaining - len(header) - len(suffix))
                block = f"{header}{text[:allowance]}{suffix}"
```

常量：`CHAT_CONTEXT_MAX_CHARS = 48_000`，预览另有 `PREVIEW_MAX_CHARS = 8_000`。  
**解析可能已是全文**，模型侧只能看到前半 → 用户易误判为「解析不全」。

### P0-4 `web_fetch` PDF 解析器未接线

`tools/web_fetch.py` 在 `kind == "pdf"` 时若无 `pdf_extract_fn` 则返回「未配置解析器」；`tools/builtin.py` 的 `web_fetch_tool` **从未传入**该回调。另有 8 MB / 150 000 字抓取帽。

### P1 其它系统性缺口

| 问题 | 说明 |
|------|------|
| 加密 PDF | 无 password/decrypt；异常 → `""` |
| 空解析不失败 | 知识库可 0 条记忆仍「导入成功」 |
| 详情/回顾用弱解析 | `get_document_detail` 再调 `extract_text`，与入库富解析不一致 |
| 提炼损耗 | 每块 `DISTILL_ITEMS_CAP=8`；失败块可跳过 → 「有记忆但缺后半」 |
| 依赖声明 | `pypdf` / RapidOCR 不在 `pyproject.toml` 主依赖时行为漂移 |
| 体积/超时 | 单文件 50 MB；附件前端超时 120 s；知识库导入 600 s |

**未发现**：硬编码「只解析前 N 页」。

---

## 4. 诊断决策树

```
用户：「无法解析完整 PDF」
        │
        ├─ 聊天上传？
        │     ├─ parsed=false → 扫描件 / 加密 / 损坏 / pdfplumber 空结果
        │     ├─ parsed=true + truncated 预览 → 仅 UI 8k，正文在服务端
        │     └─ 回答只覆盖前半 → CHAT_CONTEXT 48k 截断（解析可能已完整）
        │
        ├─ 知识库导入？
        │     ├─ 0 条记忆 + 文件在 raw_docs → 文本层为空或 distill 全跳过
        │     ├─ 有记忆但缺后半 → 提炼 cap/失败块，或正文缺扫描页
        │     └─ 400 / 超时 → >50MB 或 LLM 提炼过慢
        │
        └─ Agent web_fetch PDF？
              └─ 「未配置解析器」或 8MB 截断
```

---

## 5. 硬限制一览

| 限制 | 值 | 位置 |
|------|-----|------|
| 单文件上传 | 50 MB | ingest / attachment_store / misc |
| 聊天预览 | 8 000 字 | `PREVIEW_MAX_CHARS` |
| 聊天注入 | ≤48 000 字 | `CHAT_CONTEXT_MAX_CHARS` |
| Ingest 分块 | ~12 000 字符 | `INGEST_CHUNK_TOKENS * 2` |
| Distill 每块条目 | ≤8 | `DISTILL_ITEMS_CAP` |
| web_fetch 响应 | 8 MB | `WEB_FETCH_MAX_RESPONSE_BYTES` |
| web_fetch 正文 | 150 000 字 | `WEB_FETCH_MAX_BODY_CHARS` |
| 页数上限 | **无** | — |
| 页级 OCR | **不支持** | OCR 仅独立图片 / PDF 内嵌图 |
| PDF 密码 | **不支持** | — |

---

## 6. 解决方案（建议分阶段）

### Phase A — 对齐能力（高收益 / 低风险）

1. **聊天附件改用** `extract_text_async` / `_extract_pdf_rich`，与知识库对齐。  
2. **`web_fetch_tool` 注入** `pdf_extract_fn`（bytes → tempfile → `extract_text` 或富解析）。  
3. **空解析显式告警**：知识库返回 `empty_text` / `encrypted` / `corrupt` 分类；禁止「成功但 0 字」无提示。  
4. UI 区分：`parsed=false` vs `truncated`（预览/上下文截断），避免误判。

### Phase B — 扫描件与长文档（产品关键）

1. **页渲染 → OCR/VLM 回退**（pymupdf / pdf2image + RapidOCR 或现有 VLM）；或产品明确「不支持扫描 PDF」。  
2. **长附件 spill + 工具分段读**，替代 48k 硬截断（模型通过工具按需拉取后半）。  
3. **加密 PDF**：检测后提示用户提供密码或先解密再传。

### Phase C — 一致性与可观测

1. `raw_docs.extracted_text` 缓存 PDF/DOCX 全文，详情/回顾复用，避免弱路径重解析。  
2. `pyproject.toml` 对齐声明 `pypdf`；RapidOCR 作 extras。  
3. 解析指标：页数、空页占比、是否触发 OCR、截断位置、耗时；Langfuse 挂在现有 `agent.turn` / `ingest.file` trace。

### 明确不做（除非产品改口径）

- 把「记忆条数少」等同于「PDF 没解析完」（提炼有独立 cap）。  
- 无评测地默认全开页级 VLM（成本与延迟极高）。

---

## 7. 验收标准（修完后）

| 场景 | 期望 |
|------|------|
| 有文字层的多页 PDF（>50 页） | 解析字符数 ≈ 全文；聊天可说明截断点 |
| 扫描 PDF | 页 OCR 后有正文，或明确报「不支持扫描件」 |
| 聊天长 PDF | `parsed=true`；模型可见前 48k 且提示全文长度；或工具可读后半 |
| web_fetch arXiv PDF | 返回可检索正文，不再是「未配置解析器」 |
| 加密 PDF | 明确错误，而非静默空串 |
| 知识库空文本 | 导入失败或强告警，不伪装成功 |

---

## 8. 建议落地顺序

1. **立刻可做**：接线 `web_fetch` PDF 解析器；聊天改用富解析；错误分类 + UI 文案区分截断/失败。  
2. **下一迭代**：扫描页 OCR 回退；长附件分段读取。  
3. **收尾**：全文缓存、依赖对齐、观测指标。

若需按某一入口（仅聊天 / 仅知识库 / 仅 web_fetch）先改，可指定优先级后直接开工改码。
