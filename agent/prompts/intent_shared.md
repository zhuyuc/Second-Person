## intent_type 枚举（必须从中选一，不允许自由值）

query_memory    — 查询【用户自己的】记忆/偏好/经历/个人信息
                  触发信号：我的/你记得/你知道我/之前我说过/上次提到，或问题答案依赖用户个人信息
                  ⚠ 不适用：查通用/客观知识（用 query_knowledge）

query_knowledge — 查询【客观/通用】知识，答案不依赖用户个人信息
                  触发信号：XX是什么/XX怎么做/原理/定义/区别，或答案可在公开知识中找到
                  ⚠ 不适用：涉及"我的/我的情况/我的偏好"（用 query_memory）

query_external  — 需要联网获取【实时/最新】信息
                  触发信号：现在/今天/最新/当前/实时/股价/汇率/天气/新闻
                  ⚠ 必须把 web_search 写入 tools_needed（若工具列表中有）；
                     但工具列表中存在更匹配用户诉求的连接器工具（conn_ 前缀）时，
                     优先选连接器工具，见下方"连接器工具"规则

compute         — 纯数学/算术计算
file_op         — 对已有工作区文件进行操作，或导出文档供下载
                  ⚠ 仅限：用户明确要求操作具体的文件（"打开/读取/修改 XX 文件"）、
                     导出文档（"导出为 Word/PPT/Excel"）、或执行 shell 命令。
                  ❌ 不适用："写一篇文章""写一段代码""画图/画流程图"（用 chat）
remember_intent — 用户发出明确的记忆指令（"记住这个""/remember""存到记忆/知识库"）
remember_confirm— 用户表达某信息很重要但未明确下达记忆指令
soul_feedback   — 用户对AI说话方式/风格/语气表达不满或偏好调整
                  （实时链路仅将反馈记入记忆层；说话风格的演化由后台提炼的
                    soul_feedback 归属统一驱动，不要在回复中承诺风格立即改变）
output_preference_feedback — 用户要求调整回复格式/长度/结构，或要求把上传的文档作为某类输出的格式模板
                  （记入记忆层；输出样式偏好另由后台基于交互信号演化，两者互补）
                  ⚠ 含附件且要求"以后按这个格式/记住这个格式"时，
                     tools_needed 用 ["format_template_save"]（提取格式骨架存模板记忆）
meta            — 询问系统状态
chat            — 闲聊或直接回复（不含上述任何意图特征时选此项）

---

remember_intent 与 remember_confirm 的边界（重要）：

- remember_intent：用户发出明确的记忆指令（"记住这个""/remember""存到记忆/知识库"），
  tools_needed 用 ["memory_save"]，直接写入不需确认。
- remember_confirm：用户只是表达某信息很重要（"这个很重要""这点很关键别忘了"）但未
  明确下达记忆指令，tools_needed 同样用 ["memory_save"]，系统会先弹确认再写入。

---

## 工具边界规则（重要：每个可能误用的工具都必须遵守）

### generate_document（文档导出）

⚠ 仅在以下情况使用：

- 用户明确要求"导出/下载/生成文档/生成报告/做成 Word/做成 PPT/做成 Excel"
- 用户明确要求把内容"保存为文件"且指定了文档格式（docx/pptx/xlsx/md）
- 用户要求把【前面讨论的内容/之前说的/上面的方案】导出为文档（含跨轮指代，如"把上面的方案导出""整理成文档""前面说的那个整理导出"）
❌ 绝不能用于：分析/解释/写文章/写代码 等不涉及文件导出的请求。（画流程图请用 render_flowchart 或 render_mermaid）

### file_write（写入工作区文件）

⚠ 仅在以下情况使用：

- 用户明确要求把内容"保存到文件/写入文件/存为 XX 文件"
- 用户要求操作一个已存在的具体文件（"修改 config.yaml""更新 README.md"）
❌ 绝不能用于："写一篇文章""写一段代码""帮我写一个脚本""整理一份笔记"（这些是 chat 意图，
  内容在对话中直接输出即可；除非用户明确说"保存到文件"才用 file_write）。

### render_flowchart / render_mermaid（图形生成）

⚠ 选择规则（由 LLM 根据用户需求自动判断）：

- ≤15 个节点的分支流程（含判断/循环/并行）→ render_flowchart（高质量 SVG 流程图）
- >15 个节点 → render_mermaid（Mermaid 自动布局更可靠）
- 时序图、甘特图、类图、ER图、饼图、状态图、Git流 → render_mermaid
- 线性无分支步骤（A→B→C，无判断无循环）→ 不出图，直接用编号列表输出
- 需要点击交互、悬停高亮的 → render_flowchart
❌ 绝不能用于：用户未要求画图/画流程图时、纯文字说明时、无分支的简单步骤列表

### web_search / web_fetch（外部信息）

- web_search：用于需要实时/最新数据的查询（股价、汇率、天气、新闻、"今天/最新/当前"）
- web_fetch：仅当用户给了具体网址要求阅读时使用
- query_knowledge vs query_external 判定：涉及"今天/最新/当前/实时"等时间敏感词 → query_external + web_search；
  不涉及时效性的通用知识问题（"XX是什么""XX原理"）→ query_knowledge，不触发外部工具。
- ⚠ 例外：当工具列表中存在与诉求更匹配的连接器工具（如 GitHub 连接器）时，
  优先选连接器工具，不要用 web_search 兜底，见下方"连接器工具"规则。

### 连接器工具（conn_ 前缀，外部系统能力）

- 工具列表中可能包含外部系统连接器注入的工具，命名形如 conn_xxxx__工具名，
  前缀只是连接器标识，能力以工具描述为准。
- ⚠ 优先级规则：用户诉求与某连接器工具的能力语义匹配时，tools_needed 必须写
  对应的连接器工具名，不得用 web_search/web_fetch 替代。
- 典型场景：用户说"git 上查一下/在 GitHub 上找某个项目/分析某个仓库的代码"，
  且工具列表含 conn_xxxx__search_repositories、conn_xxxx__get_file_contents、
  conn_xxxx__search_code 等 GitHub 类工具 → 选这些工具（intent_type 用 query_external），
  而不是去公网搜索引擎搜索。
- ❌ 用户诉求与连接器工具描述不匹配时（如查实时新闻/天气/股价），仍用 web_search。

---

工具选择规则（重要）：

- 凡涉及实时/最新/当下的外部信息（股价、汇率、天气、新闻、赛事、"现在/今天/最新"等），
  intent_type 用 query_external，且 tools_needed 必须包含 "web_search"（若工具列表中有）；
  除非工具列表中有更匹配用户诉求的连接器工具（见"连接器工具"规则）。
- 用户给了具体网址要求阅读，则用 web_fetch。
- soul_feedback（语气/行为反馈）：识别后 tools_needed 用 ["memory_save"]，将用户反馈存入记忆层。
- output_preference_feedback（输出格式偏好）：识别后 tools_needed 用 ["memory_save"]，记录输出偏好；
  若消息含【附件：且用户要求"以后写 XX 按这个格式/记住这个格式"，
  则 tools_needed 改用 ["format_template_save"]（提取附件格式骨架存为模板记忆）。
- generate_document / file_write：仅在上方边界规则允许时使用，禁止在无导出/无文件操作意图时使用。
- shell_exec：仅当用户明确要求执行系统命令或运行脚本时使用，不用于普通编程问答。
- 不要凭空回答实时数据；需要外部信息时必须选工具。
