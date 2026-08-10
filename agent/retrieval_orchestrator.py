"""
双通道检索编排器 —— 完全并行，零延迟叠加。

通道 1（记忆宫殿）：长期记忆背景、知识库、跨会话信息
通道 2（会话上下文）：当前会话原文、本轮讨论但未保存的内容
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .context_signals import detect_context_reference

logger = logging.getLogger("second_person.retrieval_orch")


@dataclass
class RetrievalResult:
    memories: list[dict] = field(default_factory=list)
    loaded_ids: list[str] = field(default_factory=list)
    conversation_context: list[dict] = field(default_factory=list)


class RetrievalOrchestrator:
    """双通道检索编排：通道 1 + 通道 2 完全并行，各司其职，不做串行增强。"""

    def __init__(self, retriever, session_store):
        self.retriever = retriever
        self.sessions = session_store

    async def retrieve(
        self,
        query: str,
        session_id: str,
        llm_available: bool,
        context_text: str = "",
        role_filter: str | None = None,
    ) -> RetrievalResult:
        """双通道并行检索。

        Args:
            query: 检索查询（用户当前消息）
            session_id: 会话 ID
            llm_available: LLM 是否可用
            context_text: 近期对话上下文（用于增强记忆检索）
            role_filter: 会话检索角色过滤（调用方按意图传入，不在此写死）

        Returns:
            RetrievalResult 包含双通道结果
        """
        need_conv = detect_context_reference(query)

        mem_task = asyncio.create_task(
            self.retriever.retrieve(
                query, llm_available, session_id=session_id,
                context_text=context_text,
            )
        )
        conv_task = (
            asyncio.create_task(
                self.sessions.search_history(session_id, query, top_k=5,
                                             role_filter=role_filter)
            )
            if need_conv else None
        )

        # 通道 1：记忆宫殿
        try:
            retrieval = await mem_task
            memories = retrieval.hits + retrieval.related
            loaded_ids = retrieval.loaded_ids
        except Exception:
            logger.warning("记忆宫殿检索失败", exc_info=True)
            memories = []
            loaded_ids = []

        # 通道 2：会话上下文
        conversation_context = []
        if conv_task:
            try:
                conversation_context = await conv_task
            except Exception:
                logger.warning("会话上下文检索失败", exc_info=True)

        return RetrievalResult(
            memories=memories,
            loaded_ids=loaded_ids,
            conversation_context=conversation_context,
        )
