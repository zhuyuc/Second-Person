"""
Handoff 摘要生成器（会话上下文管理方案 v2 §摘要生成）。

跨会话 handoff 摘要：在用户点击"开启新会话"时异步生成，将旧会话的脉络浓缩为
一份 10K token 以内的 Markdown 摘要附件，注入新会话首条消息的上下文。

与现有 compression.py 的分工：
- compression.py：会话内上下文窗口管理（轮次驱动，六段 JSON，注入 system context）
- handoff_summary.py：跨会话衔接（按钮触发，五段 Markdown，作为附件随消息发送）

两套系统并行，互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from infrastructure.llm_provider import estimate_tokens
from infrastructure.prompt_loader import PROMPTS
from memory.md_file import dump_frontmatter_doc
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.handoff")


def _mark_internal_if_possible(path: Path) -> None:
    """P4-F：写盘前通知 FileWriter watcher 该路径是内部写入，避免误判为
    外部修改触发重扫（handoff 摘要被当成新记忆源蒸馏）。

    获取 container 失败时静默跳过（测试/独立调用兼容）。
    """
    try:
        from app.main import get_container
        c = get_container()
        if c and getattr(c, "fw", None):
            c.fw.mark_internal(path)
    except Exception:  # noqa: BLE001
        pass


class HandoffSummaryGenerator:
    """跨会话 handoff 摘要生成器。"""

    def __init__(self, llm, db, data_dir, config, bus, tracer):
        self.llm = llm
        self.db = db
        self.data_dir = Path(data_dir)
        self.config = config
        self.bus = bus
        self.tracer = tracer

    async def generate(self, from_session_id: str, to_session_id: str,
                       timeout: float = 60.0) -> Path:
        """生成 handoff 摘要，返回文件路径。

        失败时生成 status=failed 的占位文件，不抛异常。
        超时 timeoout 秒后降级为 failed。
        """
        from . import _constants as _mem_const
        limit = _mem_const.HANDOFF_SUMMARY_TOKEN_LIMIT
        handoff_dir = self.data_dir / "artifacts" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        output_path = handoff_dir / f"{to_session_id}.md"
        rel = f"artifacts/handoffs/{to_session_id}.md"

        # 先写入占位文件（status=generating），供前端即时感知
        _write_placeholer(output_path, from_session_id, to_session_id)

        span = self.tracer.span_start("handoff.summary_generation", input={
            "from_session": from_session_id,
            "to_session": to_session_id,
        })
        try:
            # 1. 读取旧会话全部消息
            rows = self.db.query_all(
                "SELECT id, role, content, create_time FROM conversations "
                "WHERE session_id=? AND message_type='normal' "
                "ORDER BY id",
                (from_session_id,))
            if not rows:
                return _write_failed(output_path, from_session_id, to_session_id,
                                     span, rel)

            # 2. 统计原始数据
            original_turns = len([r for r in rows if r["role"] == "user"])
            original_tokens = sum(estimate_tokens([{"role": r["role"],
                                                    "content": r["content"] or ""}])
                                  for r in rows)

            # 3. LLM 生成摘要（30s 超时）
            summary_text = await asyncio.wait_for(
                self._llm_generate(rows, from_session_id),
                timeout=min(timeout - 5, 30))

            # 4. 检查 token → 必要时二次收敛
            summary_tokens = estimate_tokens([{"role": "user",
                                               "content": summary_text}])
            if summary_tokens > limit:
                converge_span = self.tracer.span_start(
                    "handoff.convergence",
                    input={"first_summary_tokens": summary_tokens})
                try:
                    summary_text = await self._llm_converge(summary_text)
                    summary_tokens = estimate_tokens(
                        [{"role": "user", "content": summary_text}])
                    converge_span.end(
                        output={"final_summary_tokens": summary_tokens})
                except Exception:  # noqa: BLE001
                    converge_span.end(level="ERROR")

            # 5. 写入最终文件
            _write_ready(output_path, from_session_id, to_session_id,
                         original_turns, original_tokens, summary_tokens,
                         summary_text)

            # 6. 更新 DB
            self.db.execute(
                "UPDATE sessions SET handoff_summary_path=? "
                "WHERE session_id=?", (rel, to_session_id))

            # 7. 发布 ready 事件
            from infrastructure.event_bus import EVT_HANDOFF_READY
            self.bus.publish_nowait(EVT_HANDOFF_READY, {
                "session_id": to_session_id,
                "status": "ready",
                "summary_tokens": summary_tokens,
                "original_turns": original_turns,
            })

            span.end(output={
                "status": "ready",
                "summary_tokens": summary_tokens,
                "file_path": rel,
            })
            return output_path

        except asyncio.TimeoutError:
            logger.warning("handoff 摘要生成超时：from=%s to=%s",
                           from_session_id, to_session_id)
            return _write_failed(output_path, from_session_id, to_session_id,
                                 span, rel)
        except Exception as e:  # noqa: BLE001
            logger.warning("handoff 摘要生成失败：%s", e)
            return _write_failed(output_path, from_session_id, to_session_id,
                                 span, rel)

    async def _llm_generate(self, rows: list[dict],
                            from_session_id: str) -> str:
        """调用 LLM 生成五段式 handoff 摘要。"""
        convo = "\n".join(
            f"{r['role']}: {r['content'] or ''}" for r in rows)
        prompt = [{"role": "system",
                   "content": PROMPTS.render(
                       "agent/prompts/handoff_summary",
                       from_session_id=from_session_id)},
                  {"role": "user", "content": convo}]
        resp = await self.llm.chat(self._pick_snapshot(), prompt,
                                   source="handoff_summary",
                                   session_id=None)
        return resp["content"].strip()

    async def _llm_converge(self, first_summary: str) -> str:
        """二次收敛：将超出 token 限制的摘要压缩到目标以内。"""
        prompt = [{"role": "system",
                   "content": PROMPTS.load_raw("agent/prompts/handoff_converge")},
                  {"role": "user", "content": first_summary}]
        resp = await self.llm.chat(self._pick_snapshot(), prompt,
                                   source="handoff_summary",
                                   session_id=None)
        return resp["content"].strip()

    def _pick_snapshot(self):
        """选择合适的模型用于摘要生成（复用 agent 模型）。"""
        # 延迟导入避免循环依赖
        from app.main import get_container
        c = get_container()
        return c.providers.snapshot_for("agent") or \
            c.providers.snapshot_for("chat")


def _write_placeholer(p: Path, from_sid: str, to_sid: str) -> None:
    """写入手 off 占位文件（status=generating）。"""
    fm = {
        "type": "handoff_summary",
        "from_session": from_sid,
        "to_session": to_sid,
        "created_at": now_cst().isoformat(timespec="seconds"),
        "status": "generating",
    }
    _mark_internal_if_possible(p)
    p.write_text(dump_frontmatter_doc(fm, ""), encoding="utf-8")


def _write_ready(p: Path, from_sid: str, to_sid: str,
                 original_turns: int, original_tokens: int,
                 summary_tokens: int, body: str) -> None:
    """写入最终摘要文件（status=ready）。"""
    fm = {
        "type": "handoff_summary",
        "from_session": from_sid,
        "to_session": to_sid,
        "created_at": now_cst().isoformat(timespec="seconds"),
        "original_turns": original_turns,
        "original_tokens": original_tokens,
        "summary_tokens": summary_tokens,
        "status": "ready",
    }
    _mark_internal_if_possible(p)
    p.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")


def _write_failed(p: Path, from_sid: str, to_sid: str,
                  span, rel: str) -> Path:
    """写入降级文件（status=failed），不阻塞用户操作。"""
    fm = {
        "type": "handoff_summary",
        "from_session": from_sid,
        "to_session": to_sid,
        "created_at": now_cst().isoformat(timespec="seconds"),
        "status": "failed",
    }
    body = (f"摘要生成失败，可直接查看完整对话：{from_sid}")
    _mark_internal_if_possible(p)
    p.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")

    # 发布 failed 事件（前端据此切换附件状态）
    from infrastructure.event_bus import EVT_HANDOFF_READY
    try:
        from app.main import get_container
        bus = get_container().bus
        bus.publish_nowait(EVT_HANDOFF_READY, {
            "session_id": to_sid,
            "status": "failed",
        })
    except Exception:  # noqa: BLE001
        pass

    span.end(level="ERROR", output={
        "status": "failed", "file_path": rel})
    return p
