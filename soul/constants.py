"""
代码内置常量（开发文档 §6.3 代码内置常量）。

常量文本已外部化至 soul/prompts/*.md（B 类 prompt md 化），本模块在导入时
经 PromptLoader 加载（启动即 fail-fast），常量名与语义保持不变：

ONBOARDING_PERSONA —— 引导期临时人格，不落盘
DEFAULT_SOUL_CORE  —— SOUL_CORE 基线（跳过引导初始值 / 文件丢失兜底 / 恢复默认目标）
DEFAULT_SOUL_STYLE —— SOUL_STYLE 三段基线（输出样式段默认为空）

注意：load_raw 会去除首尾空白，而消费点依赖既有换行语义（写盘文件以换行
结尾、元规则前置空行分隔），故此处显式补回，勿删。
"""
from __future__ import annotations

from infrastructure.prompt_loader import PROMPTS

ONBOARDING_PERSONA = PROMPTS.load_raw("soul/prompts/onboarding_persona")

DEFAULT_SOUL_CORE = PROMPTS.load_raw("soul/prompts/default_soul_core") + "\n"

# SOUL_STYLE 三段：对话风格 / 行为原则 / 输出样式（输出样式初始为空）
DEFAULT_SOUL_STYLE_DIALOG = (
    PROMPTS.load_raw("soul/prompts/default_soul_style_dialog") + "\n")

DEFAULT_SOUL_STYLE_OUTPUT = (
    PROMPTS.load_raw("soul/prompts/default_soul_style_output") + "\n")

# 输出样式段末尾固定附加的防僵化元规则（前置换行用于与正文分隔）
OUTPUT_STYLE_META_RULE = (
    "\n" + PROMPTS.load_raw("soul/prompts/output_style_meta_rule"))
