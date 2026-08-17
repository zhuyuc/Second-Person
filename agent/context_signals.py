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
    # 未来式假承诺：工具在回复生成前已执行完毕，本轮无工具执行时
    # "稍后去执行"类措辞必为空头承诺（提议—确认闭环 §合成层铁律配套检测）
    r"稍等.{0,12}(贴|给).{0,6}(结论|结果|答案)",
    r"我(现在|这就|马上)就?去(查|看|拆|搜|拉)(一?下)?",
    r"(先|这就)去.{2,20}(再|回头).{0,6}(告诉|回复|贴|给)",
    r"查完(直接)?(贴|给).{0,6}(结论|结果)",
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

    仅匹配"过去式断言"或"未来式假承诺"，且在回复末尾 200 字内。
    """
    tail = text[-200:] if len(text) > 200 else text
    return any(re.search(p, tail) for p in FAKE_GENERATION_PATTERNS)


# ---- 提议—确认闭环信号 -------------------------------------------------------

# 短确认词：用户对 AI 上一轮主动提议的承接应答（整句匹配，剔除标点/空白后 ≤15 字）
CONFIRM_ACK_PATTERNS: list[str] = [
    r"^可\s*以[呀啊吧的]?[。！!.，,]?\s*$",
    r"^好[的啊呀吧嘛]?[。！!.，,]?\s*$",
    r"^行[啊吧的]?[。！!.，,]?\s*$",
    r"^嗯[嗯]?[。！!.，,]?\s*$",
    r"^(OK|ok|Ok)[。！!.]?\s*$",
    r"^去[吧啊][。！!.]?\s*$",
    r"^(开始|动手|继续)[吧啊]?[。！!.]?\s*$",
    r"^(就|那|那你)?(按这个|这么)(干|做|来)[吧啊]?[。！!.]?\s*$",
    r"^(帮我)?(拆|查|看|做|执行)(一下)?[吧啊]?[。！!.]?\s*$",
]

# 确认绑定消息长度上限（字符）：超过则视为带新诉求，不触发绑定
CONFIRM_ACK_MAX_LEN = 15

# 散文式主动提议句式（合成后处理兜底扫描）：
# 模型未按 ↳ 建议句格式输出提议时，尽量捕获末尾"我可以帮你做 X"类表述，
# 补落 pending，保证用户下一轮确认时闭环可触发（主通道为轮末 LLM 语义提取）
PROPOSAL_PATTERNS: list[str] = [
    r"我(?:下一步|接下来)?可以(?:帮你|继续)?[^。！？!?\n]{4,60}"
    r"(?:，|,|。|.)?(?:需要|想|要)[^。！？!?\n]{0,20}(?:就说|告诉我|说一声|直接说)",
    r"(?:如果|若)(?:你)?(?:愿意|需要|想|要)[^。！？!?\n]{0,40}"
    r"(?:我|帮你)[^。！？!?\n]{4,60}",
]

# 指导句排除：教用户"说什么话才能触发"的句式不是待确认提议，
# 命中则不落 pending（避免把触发指引误当承诺）
PROPOSAL_INSTRUCTION_EXCLUDE: list[str] = [
    r"明确说(一声|一?句)?",
    r"直接说(一声|一?句)?[\"“']",
    r"你(可以|只需|直接)说[\"“']",
    r"说一?声[\"“'][^\"”']+[\"”'](就|才能|即可)?(触发|执行|启动)",
]

# 确认绑定的确定性工具映射（提议—确认闭环 §确定性流转）：
# 绑定命中且意图解析仍为纯 chat 无工具时，按提议文本关键词强制补工具，
# 不再依赖 LLM 对注入文本的二次理解
PROPOSAL_TOOL_MAP: list[tuple[str, str, list[str]]] = [
    (r"导出.*(word|文档|docx|ppt|excel|报告)|整理成(文档|报告)",
     "file_op", ["generate_document"]),
    (r"记忆|你记得|我说过|我的(偏好|习惯|情况)",
     "query_memory", ["memory_search"]),
    (r"仓库|github|源码|目录结构|联网|搜一?下|查一?下|最新|官网",
     "query_external", ["web_search", "web_fetch"]),
]


def map_proposal_tools(proposal_text: str,
                       tool_names: list[str]) -> tuple[str, list[str]]:
    """提议文本 → (intent_type, tools_needed)；无命中默认 query_external+web_search。

    只保留当前注册表实际可用的工具，全部被滤空时回退 web_search（若可用）。
    """
    itype, tools = "query_external", ["web_search"]
    for pattern, t, ts in PROPOSAL_TOOL_MAP:
        if re.search(pattern, proposal_text or ""):
            itype, tools = t, list(ts)
            break
    available = [t for t in tools if t in (tool_names or [])]
    if not available and "web_search" in (tool_names or []):
        available = ["web_search"]
    return itype, available


def is_confirm_ack(message: str) -> bool:
    """判断消息是否为对 AI 提议的短确认应答。

    必须与 pending 提议同时命中才构成绑定条件（调用方负责合取），
    单独命中不改变任何行为，避免误伤礼貌性应答。
    """
    text = (message or "").strip()
    if not text or len(text) > CONFIRM_ACK_MAX_LEN:
        return False
    return any(re.search(p, text) for p in CONFIRM_ACK_PATTERNS)


def detect_proposal_sentence(text: str) -> str | None:
    """检测回复末尾 200 字内的散文式主动提议，返回捕获的提议句（无则 None）。

    指导句（教用户说什么话触发）不是待确认提议，命中排除模式则返回 None。
    """
    tail = text[-200:] if len(text) > 200 else text
    if any(re.search(p, tail) for p in PROPOSAL_INSTRUCTION_EXCLUDE):
        return None
    for p in PROPOSAL_PATTERNS:
        m = re.search(p, tail)
        if m:
            proposal = m.group().strip().rstrip("。！!.，,")
            if len(proposal) >= 10:
                return proposal[:80]
    return None
