"""
L1 压缩（产品文档 §L1 短期工作记忆 / 开发文档 §6.17 压缩 Agent 五段输出）。

五段式结构化压缩：spawn 压缩 Agent（独立 context）执行 LLM 语义压缩。
（原"阶段一工具输出裁剪"随 v7.3 文档回写移除：工具结果不入 L2 历史，
  单轮内的超大工具输出由工具执行层做截断兜底。）
Head-Middle-Tail 拼装：Protected Head + Compacted Middle(Summary) + Protected Tail
二次压缩：把已有摘要与新增 middle 一起送 LLM 合并（不嵌套）
窗口校验：压缩模型 context window 须 ≥ 压缩阈值 × 1.3，不满足回退对话模型；
middle 仍超窗时按时间切分为多段串行压缩再归并。
失败兜底（由 core 执行）：只保留 Head + Tail，摘要 frontmatter 记
compression_failed: true，连续 3 次失败推系统通知建议新建会话。
"""
from __future__ import annotations

import logging

from infrastructure.json_repair import repair_json
from infrastructure.llm_provider import estimate_tokens
from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.compression")

COMPRESS_SYSTEM_PROMPT = PROMPTS.load_raw("agent/prompts/compress_system")

# 窗口校验系数：压缩模型 context window ≥ 阈值 × 1.3（产品文档 §压缩 Agent）
WINDOW_FACTOR = 1.3


def assemble_context(head: list[dict], summary: dict | None, tail: list[dict]) -> list[dict]:
    """Head-Middle-Tail 拼装。"""
    result = list(head)
    if summary:
        result.append({
            "role": "system",
            "content": PROMPTS.load_raw("agent/prompts/compact_prefix") + "\n"
            + render_summary_body(summary),
        })
    result.extend(tail)
    return result


def render_summary_body(s: dict) -> str:
    """六段摘要渲染为文本（注入 context / 落盘 md 正文共用）。"""
    lines = []
    if s.get("S0_constraints"):
        lines.append(
            "【本会话用户约束（必须遵守，不可绕过）】"
            + "；".join(s["S0_constraints"]))
    if s.get("S1_decisions"):
        lines.append("关键决策：" + "；".join(
            f"[{d.get('date', '')}] {d.get('content', '')}" for d in s["S1_decisions"]))
    if s.get("S2_topic_stack"):
        ts = s["S2_topic_stack"]
        lines.append(
            f"当前话题：{ts.get('current', '')}；挂起：{ts.get('suspended', [])}")
    if s.get("S3_frameworks"):
        lines.append("分析框架：" + "；".join(s["S3_frameworks"]))
    if s.get("S4_thread"):
        lines.append("对话脉络：" + s["S4_thread"])
    if s.get("S5_followups"):
        lines.append("待跟进：" + "；".join(s["S5_followups"]))
    return "\n".join(lines)


class Compressor:
    """压缩 Agent：agent 模型执行，窗口不足时回退对话模型。"""

    def __init__(self, llm_client, agent_snapshot_fn, chat_snapshot_fn=None):
        self.llm = llm_client
        self.snapshot_fn = agent_snapshot_fn
        self.chat_snapshot_fn = chat_snapshot_fn

    def _pick_snapshot(self, threshold_tokens: int):
        """窗口校验：agent 模型窗口不足阈值×1.3 时告警并回退对话模型。"""
        required = int(threshold_tokens * WINDOW_FACTOR)
        snap = self.snapshot_fn()
        if snap is not None and (snap.context_window or 0) >= required:
            return snap
        chat_snap = self.chat_snapshot_fn() if self.chat_snapshot_fn else None
        if snap is not None and chat_snap is not None and \
                (chat_snap.context_window or 0) > (snap.context_window or 0):
            logger.warning("压缩模型窗口 %s < 需求 %s，回退对话模型 %s",
                           snap.context_window, required, chat_snap.model_id)
            return chat_snap
        return snap or chat_snap

    async def compress(self, middle_messages: list[dict],
                       prev_summary_text: str | None = None,
                       threshold_tokens: int = 80000,
                       session_id: str | None = None) -> tuple[dict | None, bool]:
        """返回 (五段 summary, 是否成功)。失败返回 (None, False)。

        prev_summary_text：已有摘要文本（二次压缩合并，不嵌套）。
        middle 超出压缩模型窗口时按时间切段串行压缩，段间摘要链式归并。
        """
        snap = self._pick_snapshot(threshold_tokens)
        if snap is None:
            return None, False
        # 分段压缩兜底：middle 超窗（预留 40% 给 prompt 与输出）按时间切段
        budget = max(int((snap.context_window or 128000) * 0.6), 8000)
        segments = self._split_by_budget(middle_messages, budget)
        summary: dict | None = None
        prev_text = prev_summary_text
        for seg in segments:
            summary, ok = await self._compress_once(snap, seg, prev_text,
                                                    session_id=session_id)
            if not ok:
                return None, False
            prev_text = render_summary_body(summary)  # 链式归并到下一段
        return summary, summary is not None

    async def _compress_once(self, snap, messages: list[dict],
                             prev_summary_text: str | None,
                             session_id: str | None = None) -> tuple[dict | None, bool]:
        convo = "\n".join(
            f"{m['role']}: {m.get('content', '')}" for m in messages)
        if prev_summary_text:
            convo = f"[已有摘要]\n{prev_summary_text}\n\n[新增对话]\n{convo}"
        prompt = [{"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
                  {"role": "user", "content": convo}]
        try:
            resp = await self.llm.chat(snap, prompt, source="system_agent",
                                       session_id=session_id, json_mode=True)
            summary = repair_json(resp["content"])
            return (summary, True) if isinstance(summary, dict) and summary \
                else (None, False)
        except Exception as e:  # noqa: BLE001
            logger.warning("压缩失败：%s", e)
            return None, False

    @staticmethod
    def _split_by_budget(messages: list[dict], budget_tokens: int) -> list[list[dict]]:
        """按 token 预算把 middle 切成时间连续的段；正常情况只有 1 段。"""
        segments: list[list[dict]] = []
        buf: list[dict] = []
        size = 0
        for m in messages:
            t = estimate_tokens([m])
            if buf and size + t > budget_tokens:
                segments.append(buf)
                buf, size = [], 0
            buf.append(m)
            size += t
        if buf:
            segments.append(buf)
        return segments or [[]]
