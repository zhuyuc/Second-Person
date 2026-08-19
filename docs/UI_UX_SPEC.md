# Second Person 前端 UI/UX 设计规范（SP-UI v4）

> 本规范为**强制约定**。后续所有新增/改动的前端功能必须遵循本规范。
> 若确需偏离，开发者（或 AI 助手）必须**先明确告知本规范的存在与要求**；
> 只有在用户明确确认要改的前提下，才可按单独处理执行，并在本文件"例外登记"中登记。
>
> 落地位置：全部 token 与公共类定义在 `frontend/src/style.css` 顶部 `:root` 与
> "SP-UI v4 规范层"区块；统一弹窗组件为 `frontend/src/components/BaseModal.vue`。

---

## 0. 核心原则

1. **禁止用内联 `style` 复刻已有类的视觉**。凡是"字号、间距、圆角、颜色、层级、
   弹窗尺寸、按钮尺寸、分组标题、表单字段、筛选控件"等已有 token/公共类的场景，
   一律复用；内联 `style` 仅允许用于**一次性的、无法归类的布局微调**（如某处特定的
   `flex:1;min-width:0`）。
2. **禁止硬编码设计数值**。字号/间距/圆角/时长/层级必须引用 `:root` 的 CSS 变量。
3. **颜色只能用 token**，禁止写死 `#fff`、`#333` 等（图表数据色板 `MODEL_PALETTE`
   除外，属数据可视化专用）。禁止引用未定义变量（`--s1`/`--line`/`--txt` 等已废止）。
4. **中文优先**：面向用户的枚举值一律走中文映射表，禁止把英文 key（`archived`/
   `active`/`dialog`）直接渲染到界面。

---

## 1. 设计 Token（唯一来源，见 `:root`）

### 1.1 颜色

- 前景/次级/弱化：`--fg` / `--sec` / `--muted`
- 背景分层：`--bg` / `--surface` / `--surface-2` / `--surface-3`
- 边框：`--bd`（常规）/ `--bd-strong`（强调）
- 品牌：`--brand-solid` / `--brand-grad` / `--brand-soft` / `--acctx`
- 语义：成功 `--succtx/--succbg`、警告 `--warntx/--warnbg`、危险 `--dangtx/--dangbg`
- **用色纪律**：品牌渐变仅限 logo / 新建对话 / 发送按钮 / 主按钮 `.btn-primary` 四处。

### 1.2 字号阶梯（禁止写死 px 字号）

| token | 值 | 用途 |
| --- | --- | --- |
| `--fs-xs` | 11px | 角标、次要计数、迷你按钮 |
| `--fs-sm` | 12px | 辅助说明、筛选胶囊、小按钮 |
| `--fs-base` | 13px | 正文默认（body 基准） |
| `--fs-md` | 14px | 卡片正文、分组标题 |
| `--fs-lg` | 15px | 对话气泡正文、输入框 |
| `--fs-xl` | 17px | 弹窗标题 `.mt` |
| `--fs-2xl` | 20px | 页面 `h1` |
| `--fs-3xl` | 22px | 大数值 `.val`、hero |

### 1.3 间距阶梯（4 的倍数制）

`--sp-1:4` `--sp-2:8` `--sp-3:12` `--sp-4:16` `--sp-5:20` `--sp-6:24`
用于 margin / padding / gap；提供工具类 `.mt-1..4` `.mb-2..4`。

### 1.4 圆角 / 阴影 / 时长

- 圆角：`--radius-xs:6` `--radius-sm:10` `--radius:16` `--radius-lg:20`（弹窗）`--radius-pill`
- 阴影：`--shadow-1`（悬浮）`--shadow-2`（弹窗/抽屉）`--shadow-brand`
- 时长：`--dur-fast:.15s`（hover/微交互）`--dur:.2s`（面板）`--dur-slow:.3s`（进度/布局）

### 1.5 z-index 分层（禁止随手写魔法数字）

| token | 值 | 用途 |
| --- | --- | --- |
| `--z-sticky` | 10 | 吸顶 Tab 等 |
| `--z-menu` | 50 | 下拉菜单、会话更多菜单 |
| `--z-drawer` | 200 | 侧滑抽屉（HTML 预览、节点详情） |
| `--z-modal` | 1000 | 常规弹窗（BaseModal 默认） |
| `--z-modal-2` | 1100 | 叠加在弹窗之上的二级弹窗（`stacked`） |
| `--z-toast` | 2500 | 全局 Toast |
| `--z-confirm` | 3000 | 全局确认框（最高，永远压顶） |

### 1.6 内容宽度

- 文档型页面（记忆中心 / 系统设置）：`--content-max`（960px），由 `.main>*` 统一。
- **对话页为专门的阅读列宽（820px）**，属刻意设计（长文可读性），不并入 `--content-max`。

---

## 2. 组件与公共类（必须复用）

### 2.1 按钮

- 主按钮：`.btn-primary`（品牌渐变，页面主操作/弹窗确认）。
- 常规按钮：`<button>`（默认样式）。
- 尺寸：`.btn-sm`（12px 紧凑）、`.btn-xs`（11px 迷你）。**禁止只改内联 `font-size`
  而不改 padding 造成畸形小按钮**——要小按钮就用 `.btn-sm/.btn-xs`。
- 变体：`.btn-ghost`（透明底，卡片内轻量操作）、`.btn-danger` 或 `.dang`（危险操作）。
- **异步操作**：请求期间必须 `:disabled` 并显示 `<i class="ti ti-loader-2">` spinner，
  防止重复提交。统一走 `frontend/src/composables/useBusy.js`：
  `const { busy, run } = useBusy()`，按钮 `:disabled="busy('key')" @click="run('key', fn)"`，
  列表项用 `'key'+id` 做每行独立 key。`run()` 对同 key 进行中调用自动忽略，杜绝双提交。

### 2.2 弹窗（统一走 `BaseModal.vue`）

- 新增弹窗**一律使用 `BaseModal`**，它统一提供：遮罩点击关闭、右上角 X、ESC 关闭、
  焦点捕获与归还、`role="dialog"`、尺寸档位。
- 尺寸档位（`size` 属性，或 `.modal-sm/md/lg/xl` 类）：
  `sm`=400（确认/简单表单）、默认=500、`md`=560（详情）、`lg`=720（预览/表单）、
  `xl`=960/92vw（并排对比）。**禁止内联 `max-width`**。
- **关闭一致性**：所有弹窗都必须能通过 遮罩点击 / X / ESC 三种方式关闭。
- **例外**：需要用户做出明确抉择、且关闭语义不明的确认流程（如"导入预览确认"），
  可禁用遮罩/ESC 关闭（`closeOnOverlay=false` `closeOnEsc=false`），此为规范内例外。
- 存量弹窗沿用 `.overlay + .modal` 结构，改动时逐步迁移到 `BaseModal`。

### 2.3 分组标题

- 卡片/区块级标题：`.section-title`（14px/600，`.mt` 修饰上间距）。
- 带品牌强调的子区标题（如"区一/区二/区三"）：`.section-sub`。
- **禁止**再用裸 `<div style="font-weight:500;margin-bottom:12px">` 表达分组标题。

### 2.4 表单

- 每个字段用 `.form-group` 包裹（label + 控件 + 统一下间距），控件自动 100% 宽。
- 双列表单用 `.form-grid`。
- 带右侧图标（如密码显隐眼睛）用 `.input-affix` + `.input-affix-icon`。

### 2.5 导航 Tab vs 筛选 Chip（语义严格区分）

- `.tab`：**仅用于页面级/区块级导航**（"我在哪个页/哪个子视图"），胶囊导航条 `.tabs` 容器。
- `.chip`：**仅用于筛选/切换**（时间线事件筛选、用量周期、来源筛选等）。
- **禁止**把 `.tab` 当筛选按钮混用。

### 2.6 键值对与徽标

- 元数据罗列（领域/来源/创建时间/引用次数等）用 `.kv`（`<dl><dt>/<dd>`），
  **禁止用一排 `.badge` 堆成"标签墙"** 表达键值信息。
- `.badge` 仅用于状态标记与计数（`.badge-g/y/r/a/n`）。

### 2.7 空状态 / 卡片

- 空状态统一 `.empty`（图标 + 文案），padding 用类默认值，避免逐处内联覆盖。
- 卡片：静态用 `.card`，可悬浮/可点击用 `.cw`。

### 2.8 回复内容语义展示

AI 回复正文统一支持以下 Markdown 语义结构，是否使用由内容语义决定，与回复长度无关：

| 类型 | 标记 | 视觉语义 |
| --- | --- | --- |
| 信息 | `[!INFO]` | 品牌信息色 |
| 结论/建议 | `[!DECISION]` / `[!CONCLUSION]` | 品牌强调色 |
| 前提/假设 | `[!ASSUMPTION]` | 中性灰 |
| 风险 | `[!RISK]` | 警告色 |
| 阻塞事项 | `[!BLOCKER]` | 危险色 |

格式为引用块第一行标记类型和标题，后续引用行作为正文。前端只将受控标记转换为固定语义 class，未知标记降级为普通引用，代码块中的同名文本不转换。表格统一使用横向滚动容器，阶段标题统一使用 `阶段 N｜标题` 形式，并支持目标、产出、验收等字段标识。回复内容继续经过 `sanitizeHtml()`，禁止模型输出 HTML、style、class 或自定义组件。

---

## 3. 可访问性（A11y）

1. 图标按钮必须有可访问名称：`title`（原生 tooltip）+ 必要时 `aria-label`。
2. 自研可点击元素（`.cw`/`.tab`/`.chip`/`.sess-item`/`.card`）已提供 `:focus-visible`
   焦点环；交互元素应可键盘聚焦（必要时补 `tabindex="0"` 与回车/空格处理）。
3. 弹窗走 `BaseModal`，自带 `role="dialog"`、焦点捕获与归还、ESC。
4. 状态不得**仅**用颜色表达，应辅以文字/图标（色盲友好）。

---

## 4. 响应式

- `.g2/.g3/.form-grid` 在 ≤900px 自动降为单列（已在 `style.css` 媒体查询处理）。
- 侧栏在窄屏收窄；大弹窗用 `xl`（含 `92vw`）自适应。
- 新增栅格优先用 `.g4`（`auto-fit minmax`）或遵循上述降列规则。

---

## 5. 本次一次性优化已完成的开发任务清单

| # | 类别 | 任务 | 状态 |
| --- | --- | --- | --- |
| 1 | Bug | 补/替换未定义变量 `--s1`/`--line`/`--txt`/`--ok`/`--danger` | ✅ |
| 2 | Bug | 修复 `.think-body` 白字（亮色模式不可见）→ `var(--fg)` | ✅ |
| 3 | Bug | 枚举中文化：`archived`、画像维度 `status`、SOUL `dialog/auto` | ✅ |
| 4 | Token | 新增字号/间距/圆角/时长/z-index/内容宽度 token | ✅ |
| 5 | 组件 | 新增 `BaseModal`（遮罩/X/ESC/焦点/尺寸档位一体化） | ✅ |
| 6 | 弹窗 | 弹窗尺寸档位类 `.modal-sm/md/lg/xl`，`modal` 圆角走 token | ✅ |
| 7 | 按钮 | 按钮尺寸/变体类 `.btn-sm/.btn-xs/.btn-ghost/.btn-danger` | ✅ |
| 8 | 结构 | `.section-title/.section-sub/.form-group/.form-grid/.input-affix/.kv` | ✅ |
| 9 | 语义 | 筛选控件由 `.tab` 改 `.chip`（时间线/用量周期） | ✅ |
| 10 | 结构 | Settings/Memory 分组标题迁移到 `.section-title/.section-sub` | ✅ |
| 11 | A11y | `:focus-visible` 焦点环；`BaseModal` 焦点管理 | ✅ |
| 12 | 响应式 | `.g2/.g3/.form-grid` 窄屏降列、侧栏收窄 | ✅ |
| 13 | 布局 | `.main>*` 内容宽度统一走 `--content-max` | ✅ |
| 14 | 动效 | 全部 `transition:.15s` 迁移为 `var(--dur-fast)`（token 定义层除外） | ✅ |
| 15 | 交互 | 新增 `useBusy` 组合式；全站异步按钮统一 loading/禁用/防双提 | ✅ |
| 16 | 弹窗 | 8 处内联 `max-width` 全量迁移到 `.modal-sm/md/lg/xl` 档位 | ✅ |
| 17 | 结构 | 全站按钮内联 `font-size` 清零 → `.btn-sm/.btn-xs`；`.5px` 分隔线统一 1px | ✅ |
| 18 | 结构 | Settings 全部弹窗表单迁移 `.form-group/.form-grid/.input-affix` | ✅ |
| 19 | 结构 | 弹窗二级标题统一 `.modal-subtitle`；残留内联字号改用字号 token | ✅ |
| 20 | A11y | 字号统一走 token | ✅ |
| 21 | 层级 | 全部内联遮罩 z-index 魔法数字迁移到 `--z-*` token（保序重映射） | ✅ |
| 22 | 结构 | 记忆详情元数据 badge 标签墙→`.kv` 键值对 | ✅ |

> 层级说明：内联遮罩按嵌套层叠语义映射——底层弹窗（文档/对比/图例等）=`--z-modal`；
> 可在其上打开的详情/预览（记忆详情/附件/导入预览）=`--z-modal-2`；toast=`--z-toast`；
> 确认框/反馈框=`--z-confirm`（永远压顶）；侧滑抽屉=`--z-drawer`；菜单=`--z-menu`。

> 说明：token 与公共类是"根因层"修复，覆盖了字体/字号/间距/圆角/色调/层级/
> 弹窗尺寸/按钮尺寸/交互一致性等全部审计维度；在此基础上已完成全站按钮尺寸、
> 弹窗档位、异步 loading、表单结构、分隔线的**存量清零迁移**。仅保留纯布局微调
> 类内联样式（如 `flex:1;min-width:0`、一次性 `margin`），符合第 0 条原则。
> 新增代码必须直接达标。

---

## 6. 例外登记（偏离本规范需登记）

| 日期 | 位置 | 偏离项 | 原因 | 确认人 |
| --- | --- | --- | --- | --- |
| - | 导入预览确认弹窗 | 禁用遮罩/ESC 关闭 | 关闭语义不明，需用户明确抉择 | 规范内置例外 |
| - | 对话页阅读列宽 820px | 不并入 `--content-max` | 长文可读性刻意设计 | 规范内置例外 |
| 2026-08-08 | Onboarding 首次引导弹窗（BaseModal） | 隐藏 X、禁用遮罩/ESC 关闭 | 线性强制流程，必须完成四步才能进入主界面 | 用户确认的前端全量优化 |
| 2026-08-08 | App.vue 系统确认弹窗（useConfirm） | 自研 overlay+modal，不走 BaseModal | 全局确认层（--z-confirm）需高于一切弹窗，且支持 checkbox 选项 | 用户确认的前端全量优化 |
| 2026-08-08 | 微信扫码二维码白底（`#fff`） | 硬编码颜色 | 扫码识别需要固定高对比白底，不随深浅色主题变化 | 用户确认的前端全量优化 |
| 2026-08-19 | 设置页连接器工具清单弹窗（工具描述/参数说明） | 中文优先原则例外：原样渲染第三方 MCP Server 的英文 docstring | 描述来自外部工具源无法实时翻译；已截断 Args/Returns 段只留摘要，状态/必填/默认等界面标签仍为中文 | 用户确认的工具清单弹窗规范整改 |
