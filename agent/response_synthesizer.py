"""
响应合成与输出信号采集（开发文档 §1.1 第 7/8 步）。

- 建构含上下文（工具结果/命中记忆）的合成 prompt 交 LLM 生成最终回复
- LLM 结构化输出 citations 数组按使用顺序列 memory_id，服务端按下标从 1 编号
- response_signal 两阶段采集：本轮形态维度 + 下一轮隐式反应/关键词
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst
from .meta_cognitive import ProblemModel, RequirementItem

# 隐式关键词词表（本地正则匹配，零成本）
IMPLICIT_KEYWORDS = [
    "太长了", "太短了", "说人话", "别啰嗦", "继续说", "展开讲讲", "给个表格",
    "别列 bullet", "简短", "详细点",
]


@dataclass
class QualityReport:
    """可验证的交付质量摘要，不保存模型原生推理。"""
    passed: bool
    coverage: dict[str, bool]
    missing_requirements: list[str] = field(default_factory=list)
    missing_dimensions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def safe_summary(self) -> dict:
        return {
            "passed": self.passed,
            "covered": sum(1 for covered in self.coverage.values() if covered),
            "total": len(self.coverage),
            "missing_requirements": self.missing_requirements[:8],
            "missing_dimensions": self.missing_dimensions[:5],
        }


def _problem_keywords(text: str) -> set[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    result: set[str] = set()
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            result.update(chunk[index:index + 2] for index in range(max(0, len(chunk) - 1)))
        else:
            result.add(chunk.lower())
    return result


class QualityGate:
    """在交付层检查需求是否真的得到解法，而非仅被复述。"""
    _SOLUTION_MARKERS = (
        "做法", "实现", "机制", "步骤", "处理", "采用", "使用", "通过",
        "建立", "接入", "配置", "拆分", "生成", "校验", "执行", "设计",
    )
    _RISK_MARKERS = ("风险", "边界", "依赖", "前提", "验收", "验证")

    @classmethod
    def _has_requirement_solution(cls, response: str, requirement: RequirementItem,
                                  referenced: bool) -> bool:
        if not referenced:
            return False
        marker_re = "|".join(re.escape(marker) for marker in cls._SOLUTION_MARKERS)
        for match in re.finditer(re.escape(requirement.id), response, re.I):
            if re.search(marker_re, response[match.start():match.start() + 600]):
                return True
        requirement_words = _problem_keywords(
            requirement.raw_request + " " + requirement.expected_outcome)
        for paragraph in re.split(r"[\n。；;]+", response):
            words = _problem_keywords(paragraph)
            if (len(requirement_words & words) / max(len(requirement_words), 1) >= 0.24
                    and re.search(marker_re, paragraph)):
                return True
        return False

    def validate(self, response: str, model: ProblemModel) -> QualityReport:
        response = response or ""
        response_words = _problem_keywords(response)
        coverage: dict[str, bool] = {}
        missing: list[str] = []
        for requirement in model.contract.explicit_requirements:
            requirement_words = _problem_keywords(
                requirement.raw_request + " " + requirement.expected_outcome)
            overlap = len(requirement_words & response_words)
            referenced = (requirement.id.lower() in response.lower()
                          or overlap / max(len(requirement_words), 1) >= 0.24
                          or (len(requirement_words) <= 2 and overlap > 0))
            covered = self._has_requirement_solution(response, requirement, referenced)
            coverage[requirement.id] = covered
            if requirement.solution_required and not covered:
                missing.append(requirement.id)
        dimensions: list[str] = []
        structured = model.contract.delivery_form in ("structured", "long_document")
        if structured and not any(marker in response for marker in self._SOLUTION_MARKERS):
            dimensions.append("缺少可执行解法")
        if (structured and len(model.contract.explicit_requirements) > 1
                and not any(marker in response for marker in self._RISK_MARKERS)):
            dimensions.append("缺少依赖、风险或验收说明")
        if model.assumptions and not any(marker in response for marker in ("假设", "待验证", "不确定")):
            dimensions.append("未区分待验证假设")
        return QualityReport(
            passed=not missing and not dimensions,
            coverage=coverage,
            missing_requirements=missing,
            missing_dimensions=dimensions,
            notes=[] if not (missing or dimensions) else ["质量门发现可修复的交付缺口"],
        )


async def generate_complete_section(llm, snap, system: str, user: str,
                                    session_id: str) -> str:
    """按明确完成标记续写，使模型单次上限不截断整节内容。"""
    system = system + "\n\n" + PROMPTS.load_raw("agent/prompts/delivery_section")
    parts: list[str] = []
    continuation = ""
    while True:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if continuation:
            messages.extend([
                {"role": "assistant", "content": "".join(parts)},
                {"role": "user", "content": continuation},
            ])
        response = await llm.chat(
            snap, messages, source="delivery_section", session_id=session_id,
            extra_body={"thinking_enabled": False},
        )
        chunk = str(response.get("content") or "")
        if not chunk or chunk in parts:
            break
        complete = "<!-- SECTION_COMPLETE -->" in chunk
        cleaned = chunk.replace("<!-- SECTION_COMPLETE -->", "").strip()
        parts.append(cleaned)
        if complete:
            break
        if not cleaned:
            raise RuntimeError("长文分节生成未返回可续写内容")
        continuation = "请从刚才中断的位置继续本节，不要重复已给出的内容；完成后附上 <!-- SECTION_COMPLETE -->。"
    return "\n\n".join(parts).strip()


def _truncate_at_boundary(text: str, max_chars: int) -> str:
    """在语义边界截断：优先 ## 标题边界，其次 \n\n 段落边界。"""
    if len(text) <= max_chars:
        return text
    headings = list(re.finditer(r'^##\s+', text, re.MULTILINE))
    for h in reversed(headings):
        if h.start() < max_chars:
            return text[:h.start()].rstrip() + "\n\n…（后续内容已省略）"
    paras = list(re.finditer(r'\n\n', text))
    for p in reversed(paras):
        if p.start() < max_chars:
            return text[:p.start()].rstrip() + "\n\n…（后续内容已省略）"
    return text[:max_chars] + "…"


# 单轮工具输出截断上限（字符）：工具结果不入 L2 历史，超大输出在合成层截断兑底。
# 30000 对齐 Langfuse _trim 兜底：绝大多数工具结果完整进入主 prompt，
# 仅极端超大输出（>30K 字符）截断并标注原始长度，保证 Langfuse 记录与业务输入一致
TOOL_RESULT_MAX_CHARS = 30000


def build_response_prompt(user_message: str, tool_results: list[dict],
                           memories: list[dict],
                           depth_level: str = "normal",
                           conversation_context: list[dict] | None = None,
                           next_step_seeds: list | None = None,
                           problem_model=None) -> list[dict]:
    """合成 prompt：把工具结果 + 命中记忆 + 会话上下文交给 LLM，要求输出 citations。
    depth_level（brief/normal/detailed）：场景化篇幅档位，normal 为默认不额外注入。
    conversation_context：FTS5 双通道检索命中的对话历史原文。
    next_step_seeds：下一步建议候选种子列表（非空时注入建议指令段）。"""
    ctx_parts = []
    if memories:
        mem_txt = "\n".join(
            f"[{i+1}] {m['title']}（id={m['id']}）：{m.get('detail', m.get('summary', ''))}"
            for i, m in enumerate(memories))
        ctx_parts.append(
            "已检索到的相关记忆（可直接引用其中的事实、偏好或背景）：\n" + mem_txt)
    # 会话上下文检索：按 token 预算语义边界截断
    if conversation_context:
        _budget = 12000  # ~3000 tokens
        _per = _budget // max(len(conversation_context), 1)
        ctx_text = "\n---\n".join(
            f"[会话上下文 {i+1}] {c['role']}：{_truncate_at_boundary(c['content'], _per)}"
            for i, c in enumerate(conversation_context)
        )
        ctx_parts.append(
            "从当前对话历史中检索到的相关内容（用于理解用户指代和复现之前讨论的方案）：\n"
            + ctx_text
        )
    if tool_results:
        def _clip(v) -> str:
            s = str(v)
            if len(s) > TOOL_RESULT_MAX_CHARS:
                return s[:TOOL_RESULT_MAX_CHARS] + f"\n…（已截断，原 {len(s)} 字）"
            return s
        tr_parts = []
        for r in tool_results:
            tn = r.get('tool', '')
            if tn in ('render_flowchart', 'render_mermaid') and r.get('ok'):
                vd = r.get('result', {})
                nc = len(vd.get('nodes', [])) if isinstance(vd, dict) else 0
                tr_parts.append(
                    f"- {tn}: 图表已生成（{nc} 个节点），将在前端渲染。"
                    "回复中【禁止】重复输出 Mermaid/流程图代码，仅用自然语言解释即可。")
            else:
                tr_parts.append(f"- {tn}: {_clip(r.get('result', ''))}")
        ctx_parts.append("工具执行结果：\n" + "\n".join(tr_parts))
    # 追问已作答闭环：ask_user 结果存在时注入闭环硬约束
    # （禁止二次追问、禁止编造用户事实、未提供信息用占位符）
    if any(r.get("tool") == "ask_user" and r.get("ok")
           for r in (tool_results or [])):
        ctx_parts.append(PROMPTS.load_raw(
            "agent/prompts/synth_elicitation_answered"))
    # disputed 记忆命中：要求 AI 主动告知矛盾并引导到健康度 Tab 裁决
    disputed = [m for m in memories if m.get("confidence") == "disputed"]
    if disputed:
        names = "、".join(m.get("title", m.get("id", "")) for m in disputed[:3])
        ctx_parts.append(PROMPTS.render(
            "agent/prompts/synth_disputed_notice", names=names))
    # 延迟导出文档：本次回复正文只会被导出为文档，不会在对话中展示，
    # 故正文只写文档内容本身，禁止对话式开场白/结尾或“手动另存为”等替代步骤
    if any(r.get("tool") == "generate_document" and r.get("deferred")
           for r in (tool_results or [])):
        ctx_parts.append(PROMPTS.load_raw("agent/prompts/synth_doc_export"))
    # 场景化篇幅档位只服务快速/常规回答；深度问题模型的任务合同优先，
    # 不用档位截短用户明确要求的完整交付。
    # 优先于输出画像的全局长度倾向（normal 为默认行为，不注入）
    if problem_model is None and depth_level in ("brief", "detailed"):
        ctx_parts.append(
            PROMPTS.load_raw("agent/prompts/response_depth")
            + f"\n\n本轮判定档位：{depth_level}，必须按该档位控制回复篇幅。")
    # 下一步建议种子注入：有候选则追加建议指令段（doc_only/brief 场景由调用方保证 seeds 为空）
    if next_step_seeds:
        seeds_text = "\n".join(
            f"- [{s.kind}] {s.text}（锚点：{s.anchor_ref}）"
            for s in next_step_seeds)
        ctx_parts.append(PROMPTS.render(
            "agent/prompts/next_step_suggest", seeds_text=seeds_text))
    system = (PROMPTS.load_raw("agent/prompts/response_synth")
              + "\n\n" + "\n\n".join(ctx_parts))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user_message}]


def _strip_empty_fences(text: str) -> str:
    """Clean empty code fences left after declaration extraction."""
    return re.sub(r"```[a-zA-Z]*\s*```", "", text).rstrip()


def strip_tool_call_blocks(text: str) -> str:
    """Strip inline tool-call markup that the model sometimes emits."""
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"<工具调用>[\s\S]*?</工具调用>", "", text)
    return text.strip()


def strip_mermaid_blocks(text: str) -> str:
    """Strip Mermaid code blocks from response text.

    When a diagram tool (render_flowchart / render_mermaid) has already
    rendered the chart via tool_visual, remove any ```mermaid or ```flowchart
    blocks from the text response to prevent duplicate rendering by marked.
    """
    return re.sub(
        r"```(?:mermaid|flowchart)\s*\n[\s\S]*?\n```",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def extract_citations(text: str, valid_ids: set[str]) -> tuple[str, list[str]]:
    """从回复中提取 citations JSON 声明（兼容旧格式），返回 (去除声明后的正文, 有序去重的 memory_id)。"""
    m = re.search(r'\{\s*"citations"\s*:\s*\[(.*?)\]\s*\}', text, flags=re.S)
    cites: list[str] = []
    if m:
        raw = re.findall(r'"(mem_\w+)"', m.group(1))
        seen = set()
        for mid in raw:
            if mid in valid_ids and mid not in seen:
                seen.add(mid)
                cites.append(mid)
        text = text[:m.start()].rstrip() + text[m.end():].rstrip()
        text = _strip_empty_fences(text)
    return text, cites


def _tokenize_for_match(text: str) -> set[str]:
    """提取文本中 ≥2 字符的中文/英文词用于引用匹配。"""
    tokens: set[str] = set()
    for seg in re.findall(r'[一-鿿]{2,}', text):
        for i in range(len(seg) - 1):
            tokens.add(seg[i:i + 2])
    for word in re.findall(r'[a-zA-Z0-9_]{3,}', text.lower()):
        tokens.add(word)
    return tokens


def detect_citations(response_text: str, memories: list[dict]) -> list[str]:
    """确定性引用检测：通过内容匹配判断回复实际引用了哪些记忆。

    对每条记忆，从 title+summary+detail 提取关键词，计算在回复中的命中率。
    命中率超过阈值则视为引用。返回按匹配强度排序的 memory_id 列表。"""
    if not memories or not response_text:
        return []
    resp_tokens = _tokenize_for_match(response_text)
    scored: list[tuple[str, float]] = []
    for mem in memories:
        mid = mem.get("id", "")
        title = mem.get("title", "")
        summary = mem.get("summary", "")
        detail = mem.get("detail", "")
        source_text = f"{title} {summary} {detail}"
        mem_tokens = _tokenize_for_match(source_text)
        if not mem_tokens:
            continue
        hits = mem_tokens & resp_tokens
        ratio = len(hits) / len(mem_tokens)
        title_in_resp = title and title in response_text
        if title_in_resp:
            ratio = max(ratio, 0.5)
        if ratio >= 0.3:
            scored.append((mid, ratio))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [mid for mid, _ in scored]


def extract_memory_confirm(text: str) -> tuple[str, dict | None]:
    """从回复中提取 memory_confirm 声明（兼容旧格式 JSON 嵌入）。
    返回 (去除声明后的正文, {"id":..., "confirmed": bool} | None)。"""
    m = re.search(
        r'\{\s*"memory_confirm"\s*:\s*\{\s*"id"\s*:\s*"(mem_\w+)"\s*,\s*'
        r'"confirmed"\s*:\s*(true|false)\s*\}\s*\}', text, flags=re.S)
    if not m:
        return text, None
    text = (text[:m.start()].rstrip() + text[m.end():]).rstrip()
    text = _strip_empty_fences(text)
    return text, {"id": m.group(1), "confirmed": m.group(2) == "true"}


_CONFIRM_WORDS = re.compile(
    r"确认|是的|没错|对的|属实|正确|确实|嗯是|对啊|是啊|记得没错|"
    r"confirmed|yes|right|correct", re.I)
_DENY_WORDS = re.compile(
    r"不是|不对|不准确|有误|不太对|已经变了|不再是|不确定|记错了|"
    r"不正确|否认|denied|no|wrong|incorrect", re.I)


def detect_memory_confirm(
    user_message: str, response_text: str,
    candidate: dict | None,
) -> dict | None:
    """确定性检测用户对 low 待确认记忆的确认/否认。

    基于用户消息和 AI 回复中的关键词匹配判断：
    - 用户消息含确认词 → confirmed=True
    - 用户消息含否认词 → confirmed=False
    - AI 回复中引用了记忆标题且含确认/否认语境 → 相应判定
    - 无法判定 → 返回 None（不做处理，等下轮再问）
    """
    if not candidate:
        return None
    mid = candidate.get("id", "")
    if not mid:
        return None
    combined = f"{user_message} {response_text}"
    has_confirm = bool(_CONFIRM_WORDS.search(combined))
    has_deny = bool(_DENY_WORDS.search(combined))
    if has_confirm and not has_deny:
        return {"id": mid, "confirmed": True}
    if has_deny and not has_confirm:
        return {"id": mid, "confirmed": False}
    return None


def collect_signal_shape(content: str) -> dict:
    """阶段一：采集回复形态维度。"""
    paragraphs = [p for p in content.split("\n\n") if p.strip()]
    bullets = len(re.findall(r"^\s*[-*]\s", content, flags=re.M))
    code_blocks = content.count("```") // 2
    tables = len(re.findall(r"^\|.*\|", content, flags=re.M))
    # 结论位置
    pos = "middle"
    if paragraphs:
        first = paragraphs[0]
        if any(k in first for k in ("建议", "结论", "总的来说", "简单说")):
            pos = "start"
        elif any(k in paragraphs[-1] for k in ("建议", "结论", "综上")):
            pos = "end"
    return {"char_count": len(content), "paragraph_count": len(paragraphs),
            "bullet_count": bullets, "code_block_count": code_blocks,
            "table_count": tables, "conclusion_position": pos}


def detect_implicit_keywords(next_user_message: str) -> str:
    hits = [k for k in IMPLICIT_KEYWORDS if k in next_user_message]
    return ";".join(hits)


class SignalCollector:
    def __init__(self, db):
        self.db = db

    def record_shape(self, message_id: int, shape: dict, context_label: str) -> int:
        cur = self.db.execute(
            "INSERT INTO response_signals(message_id,char_count,paragraph_count,"
            "bullet_count,code_block_count,table_count,conclusion_position,"
            "context_label,create_time) VALUES(?,?,?,?,?,?,?,?,?)",
            (message_id, shape["char_count"], shape["paragraph_count"],
             shape["bullet_count"], shape["code_block_count"], shape["table_count"],
             shape["conclusion_position"], context_label,
             now_cst().isoformat(timespec="seconds")))
        return cur.lastrowid

    def backfill_reaction(self, message_id: int, implicit_reaction: str,
                          keywords: str) -> None:
        """阶段二：下一轮回填隐式反应与关键词（explicit_keywords 分号拼接不覆盖）。"""
        row = self.db.query_one(
            "SELECT id,explicit_keywords FROM response_signals WHERE message_id=? "
            "ORDER BY id DESC LIMIT 1", (message_id,))
        if not row:
            return
        existing = row["explicit_keywords"] or ""
        merged = ";".join(filter(None, [existing, keywords]))
        self.db.execute(
            "UPDATE response_signals SET implicit_reaction=?, explicit_keywords=? WHERE id=?",
            (implicit_reaction, merged, row["id"]))

    def set_explicit_reaction(self, message_id: int, reaction: int) -> None:
        self.db.execute(
            "UPDATE response_signals SET explicit_reaction=? WHERE message_id=?",
            (reaction, message_id))
