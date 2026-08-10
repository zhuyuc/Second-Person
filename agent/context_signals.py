"""
跨轮上下文指代信号常量 —— 共享基础设施，单一事实源。

被 intent_parser.py（规则收敛/工具校正）、retrieval_orchestrator.py（检索触发）
等多处引用，避免正则表漂移。
"""

import re


# ---- 跨轮指代信号 -----------------------------------------------------------

# 强信号：必定触发收敛通道 + 会话检索
STRONG_CONTEXT_REF: list[str] = [
    r"前面(说|聊|沟通|讨论|讲|提到)的",
    r"之前(说|聊|讨论|提到)的",
    r"上次(说|提到|讨论)的",
    r"那个(方案|问题|话题|事情|内容)",
    r"上面(说|提到|讨论)的",
    r"刚才(说|聊)的",
    r"咱.*(之前|上次|前面).*的",
    r"接着(之前|上面).*说",
    r"就是那个",
    r"总结.*(前面|之前|上面)",
    r"继续.*说",
    r"展开讲讲",
]

# 中信号：含导出/整理关键词，需同时出现指代词才升级为强信号（触发收敛）
EXPORT_REF: list[str] = [
    r"导出.*(为|成).*(word|文档|docx|ppt|excel|md|markdown)",
    r"整理.*成文档",
    r"生成.*报告",
    r"保存为|另存为",
    r"(生成|做成).*(word|文档|docx|ppt|excel|报告)",
]

# 弱信号：仅触发会话检索，不强制收敛
WEAK_CONTEXT_REF: list[str] = [
    r"再.*说.*(那个|这个)",
    r"补充.*(前面|之前).*的",
    r"重新.*(说|整理|总结)",
]


# ---- 工具分配规则 -----------------------------------------------------------

# 组合模式：必须同时出现 导出动词 + 目标格式/动作
TOOL_RULE_MAP: list[tuple[str, str, str]] = [
    (
        r"导出.*(为|成).*(word|文档|docx|ppt|excel|md|markdown)",
        "generate_document", "file_op",
    ),
    (
        r"(生成|做成).*(word|文档|docx|ppt|excel|报告).*(导出|下载)",
        "generate_document", "file_op",
    ),
    (
        r"(保存为|另存为).*(word|文档|docx|ppt|excel)",
        "generate_document", "file_op",
    ),
]


# ---- 假声明检测模式 ---------------------------------------------------------

# 窄化：仅匹配"过去式断言"，且限制在回复末尾使用
FAKE_GENERATION_PATTERNS: list[str] = [
    r"已生成.*(文件|文档)",
    r"已导出.*(文件|文档)",
    r"已保存为.*(文件|文档)",
    r"文件已(生成|导出)",
    r"文档已(生成|导出|保存)",
]


# ---- 工具方法 ---------------------------------------------------------------

def match_any(text: str, patterns: list[str]) -> bool:
    """任一模式匹配。"""
    return any(re.search(p, text) for p in patterns)


def match_with_ref(text: str, export_patterns: list[str]) -> bool:
    """中信号 + 指代词组合检测。"""
    has_export = any(re.search(p, text) for p in export_patterns)
    has_ref = bool(re.search(r"前面|之前|上次|那个|上面|刚才", text))
    return has_export and has_ref


def detect_context_reference(message: str) -> bool:
    """判断消息是否包含需要会话检索的信号。

    强信号/中信号+指代词/弱信号 任一命中则触发。
    """
    if match_any(message, STRONG_CONTEXT_REF):
        return True
    if match_with_ref(message, EXPORT_REF):
        return True
    if match_any(message, WEAK_CONTEXT_REF):
        return True
    return False


def needs_convergence_rule(message: str) -> tuple[bool, str]:
    """判断是否应强制收敛（规则侧）。"""
    for pattern in STRONG_CONTEXT_REF:
        m = re.search(pattern, message)
        if m:
            return True, f"强信号匹配：{m.group()}"
    if match_with_ref(message, EXPORT_REF):
        return True, "导出+指代词组合信号"
    return False, ""


def correct_tools_rule(message: str) -> tuple[str | None, str | None]:
    """根据消息规则校正 tools_needed。返回 (tool_name, intent_type) 或 (None, None)。"""
    for pattern, tool, itype in TOOL_RULE_MAP:
        if re.search(pattern, message):
            return tool, itype
    return None, None


def detect_fake_claim(text: str) -> bool:
    """检测回复末尾是否存在虚假的工具声明。

    仅匹配"过去式断言"且在回复末尾 200 字内。
    """
    tail = text[-200:] if len(text) > 200 else text
    return any(re.search(p, tail) for p in FAKE_GENERATION_PATTERNS)
