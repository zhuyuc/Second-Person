# Prompt 注册清单

本清单登记当前生产代码实际加载的外部 prompt 与全部 LLM 调用点。
`tests/test_prompt_registry.py` 对 prompt 文件、代码引用、变量和调用点做双向对账。
普通对话的系统提示词由 `agent/prompt_assembler.py` 结构化拼装，不依赖外部 md 文件；
工具 schema 由宿主通过 API `tools` 参数传递，工具运行规则属于系统提示词固定 block。

## Prompt 文件

| # | 文件 | 用途 | 加载点 | 分类 |
| --- | ------ | ------ | ------ | ------ |
| 1 | agent/prompts/compact_prefix.md | 会话压缩摘要前缀 | agent/session_context.py | A |
| 2 | agent/prompts/handoff_converge.md | handoff 摘要二次收敛 | memory/handoff_summary.py | A |
| 3 | agent/prompts/handoff_summary.md | handoff 摘要生成 | memory/handoff_summary.py | A |
| 4 | agent/prompts/initial_soul.md | 引导期初始 SOUL 生成 | agent/system_agents.py | A |
| 5 | agent/prompts/memory_candidate_extract.md | 长期记忆候选提取 | app/container/wiring.py | A |
| 6 | agent/prompts/mood_rules.md | 情绪表达规则（system 静态） | soul/mood_manager.py | A |
| 7 | agent/prompts/mood_state.md | 本轮情绪状态（messages 尾） | soul/mood_manager.py | A |
| 8 | agent/prompts/mood_judge.md | turn 结束双源情绪判定 | soul/mood_judge.py | A |
| 9 | agent/prompts/output_style.md | 输出画像提炼 | agent/system_agents.py | A |
| 10 | agent/prompts/profile_conflict_scan.md | 画像冲突扫描 | soul/profile_conflict_scanner.py | A |
| 11 | agent/prompts/profile_rebuild.md | 用户画像重建 | agent/system_agents.py | A |
| 12 | app/prompts/distill.md | 对话记忆提炼 | app/container | A |
| 13 | app/prompts/distill_document.md | 文档记忆提炼 | app/container | A |
| 14 | app/prompts/domain_label.md | 领域名称翻译 | app/container | A |
| 15 | app/prompts/extract_image.md | 图片解析 system | app/container | A |
| 16 | app/prompts/extract_image_user.md | 图片解析 user | app/container | A |
| 17 | app/prompts/format_skeleton.md | 格式骨架提取 | tools/builtin.py | A |
| 18 | app/prompts/memory_refine.md | 记忆精筛 | app/container | A |
| 19 | app/prompts/merge_judge.md | 记忆合并判定 | app/container | A |
| 20 | app/prompts/title_gen.md | 会话标题生成 | app/services/chat_service.py | A |
| 21 | app/prompts/base_rules_fs.md | fs 工具族使用规则（M3 项目工作区） | agent/core.py | B |
| 22 | app/prompts/base_rules_image_gen.md | 文生图工具使用规则（本地 ComfyUI） | agent/core.py | B |
| 23 | agent/prompts/image_prompt_refine.md | 文生图 prompt 中英润色 | tools/builtin.py | A |
| 24 | app/prompts/base_rules_video_gen.md | 文生视频工具使用规则（本地 ComfyUI） | agent/core.py | B |
| 25 | agent/prompts/video_prompt_refine.md | 文生视频 prompt 中英润色 | tools/builtin.py | A |
| 26 | soul/prompts/default_soul_core.md | SOUL 核心默认值 | soul/constants.py | B |
| 27 | soul/prompts/default_soul_style_dialog.md | SOUL 对话风格默认值 | soul/constants.py | B |
| 28 | soul/prompts/default_soul_style_output.md | SOUL 输出样式默认值 | soul/constants.py | B |
| 29 | soul/prompts/onboarding_persona.md | 引导期人格 | soul/constants.py | B |
| 30 | soul/prompts/output_style_meta_rule.md | 输出样式元规则 | soul/constants.py | B |
| 31 | agent/prompts/compact_instruction.md | 自动压缩 8 段摘要指令（v7 CompactionEngine） | agent/compaction_engine.py | A |
| 32 | agent/prompts/compact_preamble.md | 压缩摘要 checkpoint 前言 + `<compacted-summary>` 框架 | agent/compaction_engine.py | A |

## LLM 调用点

| # | 调用点 | 方法 | source | prompt 构成 | 用途 |
| --- | ------ | ------ | ------ | ------ | ------ |
| 1 | agent/system_agents.py::build | chat | system_agent | agent/prompts/output_style.md | 输出画像提炼 |
| 2 | agent/system_agents.py::build_initial_soul | chat | system_agent | agent/prompts/initial_soul.md | 初始 SOUL |
| 3 | agent/system_agents.py::rebuild | chat | system_agent | agent/prompts/profile_rebuild.md | 用户画像重建 |
| 4 | agent/turn_runtime.py::run | stream_chat | agent_step | 动态系统提示词与宿主工具 schema | 正常对话步骤 |
| 5 | app/container/wiring.py::domain_translate_fn | chat | system_agent | app/prompts/domain_label.md | 领域翻译 |
| 6 | app/container/wiring.py::extract_fn | chat | system_agent | app/prompts/distill.md、app/prompts/distill_document.md | 记忆提炼 |
| 7 | app/container/wiring.py::image_extract_fn | chat | vision | app/prompts/extract_image.md、app/prompts/extract_image_user.md | 图片解析 |
| 8 | app/container/wiring.py::llm_refine | chat | agent | app/prompts/memory_refine.md | 记忆精筛 |
| 9 | app/container/wiring.py::merge_judge_fn | chat | system_agent | app/prompts/merge_judge.md | 记忆合并 |
| 10 | app/services/chat_service.py::_call_llm | chat | title_gen | app/prompts/title_gen.md | 会话标题 |
| 11 | memory/handoff_summary.py::_llm_converge | chat | handoff_summary | agent/prompts/handoff_converge.md | handoff 收敛 |
| 12 | memory/handoff_summary.py::_llm_generate | chat | handoff_summary | agent/prompts/handoff_summary.md | handoff 生成 |
| 13 | soul/profile_conflict_scanner.py::scan_profile_rebuild | chat | profile_conflict | agent/prompts/profile_conflict_scan.md | 画像冲突识别 |
| 14 | soul/mood_judge.py::judge_turn_moods | chat | system_agent | agent/prompts/mood_judge.md | turn 结束情绪判定 |
| 15 | tools/builtin.py::format_template_save | chat | system_agent | app/prompts/format_skeleton.md | 格式骨架提取 |
| 16 | agent/compaction_engine.py::_summarize | chat | system_agent | agent/prompts/compact_instruction.md | v7 自动压缩摘要生成 |
| 17 | tools/builtin.py::generate_image | chat | image_prompt_refine | agent/prompts/image_prompt_refine.md | 文生图 prompt 润色 |
| 18 | tools/builtin.py::generate_video | chat | video_prompt_refine | agent/prompts/video_prompt_refine.md | 文生视频 prompt 润色 |
