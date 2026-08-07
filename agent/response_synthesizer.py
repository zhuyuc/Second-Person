"""
响应合成与输出信号采集（开发文档 §1.1 第 7/8 步）。

- 建构含上下文（工具结果/命中记忆）的合成 prompt 交 LLM 生成最终回复
- LLM 结构化输出 citations 数组按使用顺序列 memory_id，服务端按下标从 1 编号
- response_signal 两阶段采集：本轮形态维度 + 下一轮隐式反应/关键词
"""
from __future__ import annotations

import re
from datetime import datetime

from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

# 隐式关键词词表（本地正则匹配，零成本）
IMPLICIT_KEYWORDS = [
    "太长了", "太短了", "说人话", "别啰嗦", "继续说", "展开讲讲", "给个表格",
    "别列 bullet", "简短", "详细点",
]


# 单轮工具输出截断上限（字符）：工具结果不入 L2 历史，超大输出在合成层截断兑底。
# 30000 对齐 Langfuse _trim 兜底：绝大多数工具结果完整进入主 prompt，
# 仅极端超大输出（>30K 字符）截断并标注原始长度，保证 Langfuse 记录与业务输入一致
TOOL_RESULT_MAX_CHARS = 30000


def build_response_prompt(user_message: str, tool_results: list[dict],
                          memories: list[dict]) -> list[dict]:
    """合成 prompt：把工具结果 + 命中记忆交给 LLM，要求输出 citations。"""
    ctx_parts = []
    if memories:
        mem_txt = "\n".join(
            f"[{i+1}] {m['title']}（id={m['id']}）：{m.get('detail', m.get('summary', ''))}"
            for i, m in enumerate(memories))
        ctx_parts.append(
            "已检索到的相关记忆（若用到其中任何一条，必须在回复末尾声明 citations）：\n" + mem_txt)
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
    system = (PROMPTS.load_raw("agent/prompts/response_synth")
              + "\n\n" + "\n\n".join(ctx_parts))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user_message}]


def _strip_empty_fences(text: str) -> str:
    """Clean empty code fences left after declaration extraction."""
    return re.sub(r"```[a-zA-Z]*\s*```", "", text).rstrip()


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
    """从回复中提取 citations JSON 声明，返回 (去除声明后的正文, 有序去重的 memory_id)。"""
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


def extract_memory_confirm(text: str) -> tuple[str, dict | None]:
    """从回复中提取 memory_confirm 声明（low 待确认记忆的对话确认结果）。
    返回 (去除声明后的正文, {"id":..., "confirmed": bool} | None)。"""
    m = re.search(
        r'\{\s*"memory_confirm"\s*:\s*\{\s*"id"\s*:\s*"(mem_\w+)"\s*,\s*'
        r'"confirmed"\s*:\s*(true|false)\s*\}\s*\}', text, flags=re.S)
    if not m:
        return text, None
    text = (text[:m.start()].rstrip() + text[m.end():]).rstrip()
    text = _strip_empty_fences(text)
    return text, {"id": m.group(1), "confirmed": m.group(2) == "true"}


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
