# Prompt 注册清单（PROMPT_REGISTRY）

本清单登记项目全部外部化 prompt（.md 文件）与全部 LLM 调用点。
`tests/test_prompt_registry.py` 会做双层机器对账：

- **md 文件层**（A/B 类）：代码引用 ↔ md 文件 ↔ 注册表一一对应，
  `${var}` 占位符与 render 传参一致；
- **调用点层**（A/B/C 全部）：全库 `llm.chat/stream/function_call` 调用点
  与调用点注册表双向对账，构成列登记的 md 必须在同文件内被引用。

**新增/删除/改名 prompt 或 LLM 调用点时必须同步更新本清单，否则测试失败**。

## 归类约定

1. **放置**：prompt 按归属模块放入该模块的 `prompts/` 目录——
   - `agent/prompts/`：Agent 运行时链路（意图、压缩、合成、Replan 等）
   - `app/prompts/`：container 装配回调与路由（提炼、判定、标题等）
   - `soul/prompts/`：人格基线常量（SOUL 默认值、引导人格、元规则）
2. **命名**：用途语义名（如 `merge_judge`）；同一能力的 system/user 成对
   prompt，user 侧加 `_user` 后缀（如 `extract_image` / `extract_image_user`）。
3. **加载**：一律经 `infrastructure.prompt_loader.PROMPTS`；无变量用
   `load_raw`，有变量用 `render`（`${var}` 占位符，禁用 str.format）。
4. **不外部化（C 类）判定标准**：文本含运行时才能确定的内容（工具 schema、
   记忆列表、错误信息等动态拼装），保留在代码中。

## 注册表

| # | 文件 | 用途 | 加载点 | 加载时机 | 变量 | 分类 |
| --- | ------ | ------ | -------- | ---------- | ------ | ------ |
| 1 | agent/prompts/intent_system.md | 意图识别 system | agent/intent_parser.py | 惰性 | intent_shared,tool_names,recent_history | A |
| 2 | agent/prompts/intent_shared.md | 意图枚举与工具边界共享片段（意图解析/收敛双通道复用） | agent/intent_parser.py | 惰性 | - | A |
| 3 | agent/prompts/compress_system.md | 上下文压缩 system | agent/compression.py | 导入时 | - | A |
| 4 | agent/prompts/compact_prefix.md | 压缩摘要注入前缀 | agent/compression.py、agent/core.py、agent/session_context.py | 惰性 | - | A |
| 5 | agent/prompts/replan.md | 工具失败 Replan 判定 system | agent/core.py | 惰性 | - | A |
| 6 | agent/prompts/memory_card.md | 主动记忆标题/摘要提炼 system | agent/core.py | 惰性 | - | A |
| 7 | agent/prompts/profile_rebuild.md | 用户画像重建 system | agent/system_agents.py | 导入时 | - | A |
| 8 | agent/prompts/initial_soul.md | 引导期初始 SOUL 生成 system | agent/system_agents.py | 导入时 | - | A |
| 9 | agent/prompts/output_style.md | 输出画像提炼 system | agent/system_agents.py | 导入时 | - | A |
| 10 | agent/prompts/response_synth.md | 最终回复合成 system 基底 | agent/response_synthesizer.py | 惰性 | - | A |
| 11 | agent/prompts/synth_disputed_notice.md | disputed 记忆告知指令（条件注入） | agent/response_synthesizer.py | 惰性 | names | A |
| 12 | agent/prompts/synth_doc_export.md | 文档导出只写正文指令（条件注入） | agent/response_synthesizer.py | 惰性 | - | A |
| 13 | agent/prompts/profile_conflict_scan.md | 画像双版对比冲突识别 system | soul/profile_conflict_scanner.py | 惰性 | old_profile,new_profile | A |
| 14 | app/prompts/distill.md | 对话记忆提炼 system | app/container.py | 导入时 | - | A |
| 15 | app/prompts/distill_document.md | 文档导入专用提炼 system | app/container.py | 导入时 | - | A |
| 16 | app/prompts/extract_image.md | VLM 图片解析 system | app/container.py | 导入时 | - | A |
| 17 | app/prompts/extract_image_user.md | VLM 图片解析 user 话术 | app/container.py | 导入时 | - | A |
| 18 | app/prompts/domain_label.md | 领域名中文化翻译 system | app/container.py | 惰性 | - | A |
| 19 | app/prompts/memory_refine.md | 第 2 层记忆精筛 system | app/container.py | 惰性 | - | A |
| 20 | app/prompts/merge_judge.md | 记忆合并关系判定 system | app/container.py | 惰性 | - | A |
| 21 | app/prompts/title_gen.md | 会话标题生成 system | app/routes/chat.py | 惰性 | - | A |
| 22 | soul/prompts/onboarding_persona.md | 引导期临时人格 | soul/constants.py | 导入时 | - | B |
| 23 | soul/prompts/default_soul_core.md | SOUL_CORE 基线/兜底 | soul/constants.py | 导入时 | - | B |
| 24 | soul/prompts/default_soul_style_dialog.md | SOUL_STYLE 对话风格+行为原则基线 | soul/constants.py | 导入时 | - | B |
| 25 | soul/prompts/default_soul_style_output.md | SOUL_STYLE 输出样式空段 | soul/constants.py | 导入时 | - | B |
| 26 | soul/prompts/output_style_meta_rule.md | 输出样式防僵化元规则 | soul/constants.py | 导入时 | - | B |
| 27 | agent/prompts/mood.md | 情绪注入模板（双源） | soul/mood_manager.py | 惰性 | strength_hint,user_mood,user_intensity,user_time_hint,ai_mood,ai_intensity,ai_time_hint,ai_attribution_hint | A |
| 28 | agent/prompts/mood_judge_v2.md | 情绪判定 v2（双源归因+平复事件） | agent/core.py | 惰性 | rule_triggers_summary,prev_user_mood,prev_user_intensity,prev_ai_mood,prev_ai_intensity,recent_history,user_message,assistant_reply | A |
| 29 | agent/prompts/quick_intent.md | 快速预判 system（§3.1） | agent/intent_parser.py | 惰性 | - | A |
| 30 | agent/prompts/converge_intent.md | 意图收敛 system（§3.3） | agent/intent_parser.py | 惰性 | intent_shared,tool_names | A |
| 31 | agent/prompts/attention_focus.md | 注意力聚焦 system（§3.4） | agent/intent_parser.py | 惰性 | - | A |
| 32 | agent/prompts/gap_detect.md | 缺口检测 system（§4.1；含 material_gap 材料缺口与画像已知信息对照判定） | agent/intent_parser.py | 惰性 | - | A |
| 33 | agent/prompts/honest_clarify.md | 诚实澄清输出模板（§5.2 态二） | agent/core.py | 惰性 | gap_description | A |
| 34 | agent/prompts/response_depth.md | 场景化回复篇幅档位指令（按场景注入） | agent/response_synthesizer.py | 惰性 | - | A |
| 35 | agent/prompts/strategy_decide.md | 响应策略决策 system（v3 §四） | agent/strategy_engine.py | 惰性 | - | A |
| 36 | agent/prompts/default_strategy_priors.md | 策略先验冷启动默认模板（行业通用启发，v3 §八） | agent/strategy_engine.py | 惰性 | - | B |
| 37 | agent/prompts/meta_cognitive.md | 元认知五步协议 system（v3 §六） | agent/meta_cognitive.py | 惰性 | examples | A |
| 38 | agent/prompts/meta_cognitive_examples.md | 五种典型答案形态 few-shot 共享片段 | agent/meta_cognitive.py | 惰性 | - | A |
| 39 | agent/prompts/handoff_summary.md | handoff 摘要生成 system | memory/handoff_summary.py | 惰性 | from_session_id | A |
| 40 | agent/prompts/handoff_converge.md | 摘要二次收敛 system | memory/handoff_summary.py | 惰性 | - | A |
| 41 | app/prompts/format_skeleton.md | 文档格式骨架提取 system（格式绑定） | tools/builtin.py | 惰性 | - | A |
| 42 | agent/prompts/format_scenario.md | 格式绑定适用场景提取 system | agent/core.py | 惰性 | - | A |
| 43 | agent/prompts/next_step_suggest.md | 下一步建议指令（评分规则/禁词/句式/分隔符，条件注入） | agent/response_synthesizer.py | 惰性 | seeds_text | A |
| 44 | agent/prompts/elicitation_decision.md | 追问判定（clarification_router 可枚举/发散二分；注入画像摘要，已知信息不追问） | agent/strategy_engine.py | 惰性 | - | A |
| 45 | agent/prompts/elicitation_supplement.md | 关闭追问后新消息临时决策指令 | agent/core.py | 惰性 | - | A |
| 46 | agent/prompts/synth_elicitation_answered.md | 追问已作答闭环约束（已确认事实回填/推断注明/仅未知占位，条件注入） | agent/response_synthesizer.py | 惰性 | - | A |

分类说明：A = LLM 调用的 system/user 指令；B = 人格基线/默认值常量。

## LLM 调用点注册表（含 C 类动态拼装）

注册单位：`文件::函数` 的 chat/stream/function_call 调用。C 类（动态拼装）
没有 md 文件可对账，但调用点本身受机器对账约束：新增 LLM 调用不登记即
测试失败。构成列只登记**同文件内直接引用**的 md；跨文件组合、运行时拼装
均计入“动态”。

| # | 调用点（文件::函数） | 方法 | source | prompt 构成 | 用途 |
| --- | ---------------------- | ------ | -------- | ------------- | ------ |
| 1 | agent/compression.py::_compress_once | chat | system_agent | agent/prompts/compress_system.md | 上下文压缩 |
| 2 | agent/core.py::_pipeline_impl | stream | main_chat | agent/prompts/compact_prefix.md + SOUL/合成动态拼装（C） | 主对话回复 |
| 3 | agent/core.py::_replan_fn | chat | replan | agent/prompts/replan.md | 工具失败 Replan 判定 |
| 4 | agent/core.py::_memory_save_params | chat | system_agent | agent/prompts/memory_card.md | 主动记忆标题/摘要提炼 |
| 5 | agent/core.py::_infer_params_llm | function_call | tool_infer | 动态（工具 schema 拼装，C） | 工具参数推断 |
| 6 | agent/intent_parser.py::parse | chat | intent_parse | agent/prompts/intent_system.md + agent/prompts/intent_shared.md | 意图识别 |
| 7 | agent/system_agents.py::rebuild | chat | system_agent | agent/prompts/profile_rebuild.md | 用户画像重建 |
| 8 | agent/system_agents.py::build_initial_soul | chat | system_agent | agent/prompts/initial_soul.md | 引导期初始 SOUL 生成 |
| 9 | agent/system_agents.py::build | chat | system_agent | agent/prompts/output_style.md | 输出画像提炼 |
| 10 | app/container.py::domain_translate_fn | chat | system_agent | app/prompts/domain_label.md | 领域名中文化翻译 |
| 11 | app/container.py::llm_refine | chat | agent | app/prompts/memory_refine.md | 第 2 层记忆精筛 |
| 12 | app/container.py::extract_fn | chat | system_agent | app/prompts/distill.md、app/prompts/distill_document.md | 对话/文档记忆提炼 |
| 13 | app/container.py::image_extract_fn | chat | vision | app/prompts/extract_image.md、app/prompts/extract_image_user.md | VLM 图片解析 |
| 14 | app/container.py::merge_judge_fn | chat | system_agent | app/prompts/merge_judge.md | 记忆合并关系判定 |
| 15 | app/routes/chat.py::_call_llm | chat | title_gen | app/prompts/title_gen.md | 会话标题生成 |
| 16 | app/routes/misc.py::test_connection | chat | main_chat | 无（连通性 ping） | 引导页模型连通测试 |
| 17 | app/routes/settings.py::_probe_snapshot | chat | main_chat | 无（连通性 ping） | 设置页模型探活 |
| 18 | agent/core.py::_update_mood | chat | mood | agent/prompts/mood_judge_v2.md | 情绪判定 v2（双源归因+平复事件） |
| 19 | agent/intent_parser.py::quick_intent | chat | quick_intent | agent/prompts/quick_intent.md | 快速预判（§3.1） |
| 20 | agent/intent_parser.py::converge_intent | chat | converge_intent | agent/prompts/converge_intent.md + agent/prompts/intent_shared.md | 意图收敛（§3.3） |
| 21 | agent/intent_parser.py::focus | chat | attention_focus | agent/prompts/attention_focus.md | 注意力聚焦（§3.4） |
| 22 | agent/intent_parser.py::detect | chat | gap_detect | agent/prompts/gap_detect.md | 缺口检测（§4.1） |
| 23 | agent/core.py::_emit_honest_clarify | chat | honest_clarify | agent/prompts/honest_clarify.md | 诚实澄清输出（§5.2 态二） |
| 24 | soul/profile_conflict_scanner.py::scan_profile_rebuild | chat | profile_conflict | agent/prompts/profile_conflict_scan.md | 画像双版对比冲突识别 |
| 25 | agent/strategy_engine.py::_llm_decide | chat | strategy_decide | agent/prompts/strategy_decide.md + agent/prompts/default_strategy_priors.md（先验动态拼装） | 响应策略决策（v3 §四） |
| 26 | agent/meta_cognitive.py::_extract | chat | meta_cognitive | agent/prompts/meta_cognitive.md + agent/prompts/meta_cognitive_examples.md | 元认知思考骨架提取（v3 §六） |
| 27 | memory/handoff_summary.py::_llm_generate | chat | handoff_summary | agent/prompts/handoff_summary.md | handoff 摘要生成 |
| 28 | memory/handoff_summary.py::_llm_converge | chat | handoff_summary | agent/prompts/handoff_converge.md | 摘要二次收敛 |
| 29 | tools/builtin.py::format_template_save | chat | system_agent | app/prompts/format_skeleton.md | 格式骨架提取（格式绑定） |
| 30 | agent/core.py::_format_template_save_params | chat | intent | agent/prompts/format_scenario.md | 格式绑定适用场景提取 |
| 31 | agent/strategy_engine.py::clarification_router | chat | elicitation_decision | agent/prompts/elicitation_decision.md | 追问可枚举/发散二分判定 |

备注：`agent/response_synthesizer.py` 的 response_synth/synth_disputed_notice/
synth_doc_export/synth_elicitation_answered 不是独立调用点，其合成结果经 `_build_final_prompt` 并入
调用点 2；md 存在性由上方 md 文件层对账覆盖。
