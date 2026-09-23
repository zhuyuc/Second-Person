# PDF 导出接入调研方案

> 状态：**Phase 1 已落地**（`md_to_pdf_bytes` + `generate_document` 白名单/schema + MIME 修复 + 回归测试通过）。

## 目标

让 Agent 通过 `generate_document(format="pdf")` **直接产出可下载 PDF**，接入点与 docx / pptx / xlsx 对齐，前端尽量零改动。

| 指标 | 现状 |
|------|------|
| 已支持导出格式 | 4（docx / md / pptx / xlsx） |
| PDF 导出 | 无（工具白名单显式拒绝） |
| PDF 入站解析 | 有（pdfplumber / pypdf / ingest） |
| 前端格式契约 | schema 驱动，无格式硬编码 |

---

## 一、现状结论

导出已稳定运行在 `tools/doc_export.py`：

```
Markdown → markdown 库 → HTML → BeautifulSoup → 各格式渲染器
```

工具入口 `tools/builtin.py::generate_document` 白名单仅 `docx | md | pptx | xlsx`，`tests/test_doc_export.py` 显式断言 `format=pdf` 抛「不支持的格式」。PDF 能力停在**入站解析**，与出站无关。

| 环节 | 现状 | 对 PDF 的含义 |
|------|------|----------------|
| 转换核心 | `md_to_docx/pptx/xlsx_bytes` | 缺 `md_to_pdf_bytes`，可复用同一 MD→HTML 前半段 |
| 工具 schema | enum 四元 + 别名归一 | 加 `pdf` 即可被模型调用；前端无格式硬编码 |
| 下载路由 | `GET /api/files/…` | MIME 仅区分 docx vs 其余→markdown，加 PDF 必须修 MIME 表 |
| 前端 | file-card 下载卡 | 无应用内预览；图标可加 pdf 特判（可选） |
| 中文 | DOCX/PPTX 绑微软雅黑 | PDF 必须**嵌入字体文件**，不能只写字体名 |

---

## 二、需求边界（先定再选型）

### P0 建议纳入（直接输出）

- `format=pdf` 落盘 `temp/exports`，返回 `/api/files/` 链接
- 覆盖与 DOCX 同级语法：标题 / 段落 / 列表 / 代码 / 表格 / 引用 / 链接
- 中文可读（嵌入或捆绑开源 CJK 字体）
- `asyncio.to_thread`，不堵事件循环

### 明确不做 / 后置

- 应用内 PDF.js 预览（与「输出」解耦）
- Mermaid / 位图嵌入（现有导出也不做）
- KaTeX 数学公式（聊天侧亦未支持）
- 复杂排版模板 / 页眉页脚品牌套版（可 P2）

---

## 三、技术方案对比

评价维度：复用 MD→HTML、中文、依赖体积、Windows/Linux 可装性、与「零系统二进制优先」的契合度。

| 方案 | 路径 | 中文 | 依赖代价 | 与现管线契合 | 推荐 |
|------|------|------|----------|--------------|------|
| A. WeasyPrint | HTML+CSS → PDF | 需 `@font-face` + 字体文件；部分阅读器对子集字体挑剔 | Python 包 + 系统 Pango/Cairo（Win 较痛） | 高（已有 HTML） | 次选 |
| **B. ReportLab 自研映射** | HTML/DOM → RL flowables | TTFont 嵌入可靠 | 纯 pip，无浏览器 | 中（要写第二套渲染器，像 docx） | **首选** |
| C. fpdf2 | 轻量 API / 有限 HTML | `add_font` 嵌入 TTF | 最轻 | 中低（复杂表格/嵌套列表弱） | 备选 MVP |
| D. Playwright/Chrome | 打印 HTML | 浏览器渲染最稳 | 捆绑 Chromium，运维重 | 高保真但重 | 不推荐首期 |
| E. pandoc / wkhtmltopdf | 外置二进制 | 取决于引擎 | 安装与权限不可控 | 低 | 否决 |
| F. docx→PDF 转码 | 先 docx 再转 | 依赖 LibreOffice 等 | 系统依赖大 | 复用排版但链路长 | 否决 |

**首期推荐 B（ReportLab）**：与现有「BeautifulSoup 遍历 → 目标格式」模式一致，纯 pip、无系统 GUI/浏览器依赖，CJK 嵌入路径成熟。若验收要求「打印级 CSS 版式」再评估 A/D。

---

## 四、推荐架构（接入点）

```
generate_document(format="pdf")
        │
        ├─ 白名单 + 别名（pdf）
        ├─ asyncio.to_thread(md_to_pdf_bytes)
        │         │
        │         ├─ markdown → HTML（复用 extensions）
        │         ├─ BeautifulSoup 遍历
        │         └─ ReportLab：段落/标题/列表/表/代码/引用
        │              + 注册并嵌入 CJK TTF（捆绑或可配置路径）
        ▼
temp/exports/{uuid}_{title}.pdf
        │
        ▼
download_url → MIME application/pdf
        │
        ▼
前端 file-card（schema 驱动，基本零改）
```

### 须改文件（预估）

| 文件 | 改动 |
|------|------|
| `tools/doc_export.py` | 新增 `md_to_pdf_bytes` + 字体解析 |
| `tools/builtin.py` | 白名单 / schema enum / 写盘分支 |
| `app/routes/misc.py` | 按后缀映射 MIME（顺带修 pptx/xlsx） |
| `requirements.txt` / `pyproject.toml` | reportlab + 字体策略说明 |
| `tests/test_doc_export.py` | 倒转：pdf 从拒绝改为可打开校验 |
| `frontend/.../chatMarkedRenderer.js` | 可选：pdf 图标；非必须 |

---

## 五、关键风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 中文字体缺失 / 版权 | 方框或缺字 | 捆绑开源字体（Noto Sans SC / Source Han）于 `assets/fonts`；配置可覆盖路径；Win 可回退本机雅黑文件 |
| 大文档 CPU | 卡顿 | 继续 `to_thread`；可加页数/字符软上限（对齐 pptx 截断思想） |
| 表格过宽 | 溢出页边 | 列宽均分 + 字号缩小；超宽转「表格已截断」注记 |
| WeasyPrint 系统依赖 | Win/容器装失败 | 首期不用 A；若后期上 A，做可选 extra + 探测降级文案 |
| MIME 错误 | 浏览器当 MD 打开 | 扩展名→media_type 字典：pdf/pptx/xlsx/docx/md |
| 语法 parity | PDF 与 DOCX 观感不一 | 同一套 soup 遍历契约 + 对照 fixture 黄金样例 |

---

## 六、分期落地建议

### Phase 1 — MVP 输出（约 1～2 人日）

- ReportLab + 捆绑 CJK 字体
- 语法对齐 DOCX 主路径
- schema / MIME / 测试翻转

### Phase 2 — 体验增强（约 0.5～1 人日）

- 页眉页脚 / 页码 / 标题页
- 简单主题（若切 Weasy）
- file-card PDF 图标

### Phase 3 — 可选预览（独立 2～3 人日）

- PDF.js 侧栏预览
- 与导出解耦立项，不阻塞「能输出」

---

## 七、验收标准（Phase 1）

- [ ] `format=pdf` 返回 `download_url`
- [ ] 中文标题 / 正文 / 表格无方框
- [ ] pypdf 可打开且页数 ≥ 1
- [ ] `Content-Type: application/pdf`
- [ ] 非法格式仍报「不支持的格式」
- [ ] 大文档不阻塞事件循环

---

## 八、决策建议

按 **Phase 1 走 ReportLab 自研映射**，复用 MD→HTML→soup 管线；字体捆绑进仓库或 `data/fonts`；同步修 `/api/files` MIME。预览与 Mermaid 进 PDF **不进首期**。

确认后可直接按本方案开工实现。

**依据文件：** `tools/doc_export.py` · `tools/builtin.py` · `app/routes/misc.py` · `tests/test_doc_export.py` · 产品方案「导出四格式」与优化方案「目标含 PDF」差距。
