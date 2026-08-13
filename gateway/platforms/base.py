"""
Platform Adapter 抽象基类（产品文档 §Platform Adapter / 开发文档 §6.13-6.14）。

BasePlatformAdapter：on_message / send_message / health_check / connect / disconnect
每个 adapter 独立熔断器（连续失败 5 次 paused，需手动恢复）。
消息处理：只处理私聊 + 白名单；message_id 去重；调用调度引擎 await 完整回复；
超长回复转 .md 附件；出站 MEDIA: 标记提取。

入站统一语义：图片 → 多模态对话（与网页端一致，dataURL 走 core.run images）；
文档 → handle_inbound_file 提炼入知识库；不支持类型 → handle_unsupported 明确提示，
杜绝静默丢弃。媒体下载通过 media_fetch 懒回调注入，仅在白名单+去重通过后执行。
"""
from __future__ import annotations

import logging
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.adapter")

CIRCUIT_THRESHOLD = 5
# 入站媒体约束：单图上限对齐 ingest.MAX_FILE_MB；单条消息最多取前 5 张图
MAX_MEDIA_MB = 50
MAX_IMAGES_PER_MSG = 5
UNSUPPORTED_HINT = "暂不支持该消息类型（当前支持：文字 / 图片 / pdf / docx / txt / md）"


class BasePlatformAdapter:
    platform_type = "base"

    def __init__(self, platform_id: str, config: dict, *, core, sessions, db,
                 notifier, im_max_chars: int = 4000, data_dir=None, ingest=None):
        self.platform_id = platform_id
        self.config = config
        self.core = core
        self.sessions = sessions
        self.db = db
        self.notifier = notifier
        self.im_max_chars = im_max_chars
        self.data_dir = data_dir
        self.ingest = ingest
        self.whitelist_user_id = config.get("whitelist_user_id")
        self._failures = 0
        self.paused = False

    async def connect(self) -> None:
        raise NotImplementedError

    async def disconnect(self) -> None:
        pass

    async def send_message(self, chat_id: str, text: str) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return not self.paused

    # ---- 入站消息处理（子类收到消息后调用） ------------------------------
    async def on_message(self, platform_user_id: str, chat_id: str,
                         message_id: str, text: str, is_group: bool = False,
                         images: list[str] | None = None,
                         media_fetch=None) -> None:
        """images：图片 dataURL 列表（多模态对话）；media_fetch：懒下载回调
        async () -> list[str]，仅在白名单+去重通过后才执行，避免重推事件重复下载。"""
        # 群聊过滤
        if is_group:
            return
        # 白名单
        if self.whitelist_user_id and platform_user_id != self.whitelist_user_id:
            return
        # 去重
        if self._is_duplicate(message_id):
            return
        self._mark_processed(message_id)
        # 懒下载媒体（去重之后执行；失败明确提示而非静默）
        if media_fetch is not None:
            try:
                images = (images or []) + list(await media_fetch() or [])
            except Exception as e:  # noqa: BLE001
                logger.warning("入站媒体下载失败 platform=%s message_id=%s: %s",
                               self.platform_type, message_id, e)
                await self.send_message(chat_id, "图片下载失败，请重新发送")
                return
        if images and len(images) > MAX_IMAGES_PER_MSG:
            images = images[:MAX_IMAGES_PER_MSG]
            await self.send_message(
                chat_id, f"图片较多，仅处理前 {MAX_IMAGES_PER_MSG} 张")
        # 映射 session
        sid = self._resolve_session(platform_user_id)
        # 追问上下文：有 pending elicitation 时，解析为答案而非路由到 Agent
        if sid:
            elicit_answer = await self._check_elicit_and_parse(sid, text)
            if elicit_answer is not None:
                await self._deliver_elicit_answer(sid, chat_id, elicit_answer)
                return
        # /new 命令
        if text.strip() == "/new":
            sid = self.sessions.create_session(channel=self.platform_type)
            self._update_mapping(platform_user_id, sid)
            await self.send_message(chat_id, "已开启新会话")
            return
        # 调用调度引擎交付回复（接入组：单消息处理超时 300 秒）
        try:
            import asyncio
            await asyncio.wait_for(
                self._deliver(chat_id, sid, text, images=images), timeout=300)
            self._reset_circuit()
        except Exception as e:  # noqa: BLE001
            logger.exception("IM 消息处理失败")
            self._record_failure(str(e))

    async def _deliver(self, chat_id: str, sid: str, text: str,
                       images: list[str] | None = None) -> None:
        """默认交付：收齐完整回复后一次性发送（子类可覆写为流式）。"""
        full = []
        async for evt in self.core.run(sid, text, images=images):
            if evt["event"] == "content_delta":
                full.append(evt["data"]["text"])
        await self._send_reply(chat_id, "".join(full))

    async def handle_unsupported(self, platform_user_id: str, chat_id: str,
                                 message_id: str, msg_type: str,
                                 is_group: bool = False,
                                 hint: str | None = None) -> None:
        """不支持的消息类型：过白名单+去重后明确回复提示，消灭静默黑洞。"""
        if is_group:
            return
        if self.whitelist_user_id and platform_user_id != self.whitelist_user_id:
            return
        if message_id and self._is_duplicate(message_id):
            return
        if message_id:
            self._mark_processed(message_id)
        logger.info("入站不支持类型 platform=%s type=%s message_id=%s",
                    self.platform_type, msg_type, message_id)
        await self.send_message(chat_id, hint or UNSUPPORTED_HINT)

    async def _send_reply(self, chat_id: str, reply: str) -> None:
        # 超长转附件
        if len(reply) > self.im_max_chars and self.data_dir:
            from pathlib import Path
            from memory.naming import im_attachment_name
            from infrastructure.observability import get_trace_id
            fname = im_attachment_name(get_trace_id() or "im")
            fpath = Path(self.data_dir) / "temp" / "attachments" / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(reply, encoding="utf-8")
            await self.send_message(chat_id, f"回复内容较长，已作为附件发送\nMEDIA:{fpath}")
        else:
            await self.send_message(chat_id, reply)

    # ---- 入站文件处理（下载后触发文档 Ingest） ----------------------
    SUPPORTED_FILE_EXT = (".pdf", ".docx", ".txt", ".md", ".markdown",
                          ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

    async def handle_inbound_file(self, platform_user_id: str, chat_id: str,
                                  message_id: str, filename: str,
                                  content: bytes, is_group: bool = False) -> None:
        """入站文件：白名单/去重校验 → 触发文档 Ingest 提炼 → 回复提取条数。
        图片走 image_parse_engine（VLM/OCR/off）解析入库，off 时仅缓存不解析。"""
        if is_group:
            return
        if self.whitelist_user_id and platform_user_id != self.whitelist_user_id:
            return
        if message_id and self._is_duplicate(message_id):
            return
        if message_id:
            self._mark_processed(message_id)
        if not self.ingest:
            await self.send_message(chat_id, "当前不支持文件处理")
            return
        import os
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in self.SUPPORTED_FILE_EXT:
            await self.send_message(
                chat_id, f"文件类型 {ext or '未知'} 暂不支持解析")
            return
        is_image = ext in self.IMAGE_EXTS
        try:
            r = await self.ingest.ingest_file(filename, content, source="im_platform")
            if is_image:
                await self.send_message(chat_id,
                                        f"已解析图片并提取 {r.get('extracted', 0)} 条信息"
                                        if r.get("extracted") else "图片已缓存")
            else:
                await self.send_message(
                    chat_id, f"已从文档提取 {r.get('extracted', 0)} 条记忆")
            self._reset_circuit()
        except Exception as e:  # noqa: BLE001
            logger.exception("入站文件 Ingest 失败")
            await self.send_message(chat_id, f"文件处理失败：{e}")

    # ---- 去重 / 映射 / 熔断 ----------------------------------------------
    def _is_duplicate(self, message_id: str) -> bool:
        return bool(self.db.query_one(
            "SELECT 1 FROM message_dedup WHERE platform=? AND message_id=?",
            (self.platform_type, message_id)))

    def _mark_processed(self, message_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO message_dedup(platform,message_id,processed_at) "
            "VALUES(?,?,?)", (self.platform_type, message_id,
                              now_cst().isoformat(timespec="seconds")))

    def _resolve_session(self, platform_user_id: str) -> str:
        row = self.db.query_one(
            "SELECT session_id FROM platform_sessions WHERE platform=? AND platform_user_id=?",
            (self.platform_type, platform_user_id))
        if row:
            return row["session_id"]
        sid = self.sessions.create_session(channel=self.platform_type)
        self._update_mapping(platform_user_id, sid)
        return sid

    def _update_mapping(self, platform_user_id: str, sid: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO platform_sessions(platform,platform_user_id,"
            "session_id,created_at) VALUES(?,?,?,?)",
            (self.platform_type, platform_user_id, sid,
             now_cst().isoformat(timespec="seconds")))

    # ---- 追问上下文（elicitation）------------------------------------------

    async def _check_elicit_and_parse(self, sid: str, text: str) -> dict | None:
        """检查是否有 pending elicitation，有则解析答案。"""
        import json as _json
        row = self.db.query_one(
            "SELECT id, questions_json FROM elicitations "
            "WHERE session_id=? AND status='pending' LIMIT 1", (sid,))
        if not row:
            return None
        questions = _json.loads(row["questions_json"])
        from gateway.elicitation_parser import parse_im_elicitation
        parsed = parse_im_elicitation(text, questions)
        parsed["tool_use_id"] = row["id"]
        return parsed

    async def _deliver_elicit_answer(self, sid: str, chat_id: str,
                                     parsed: dict) -> None:
        """提交 IM 追问答案并触发后续回复。"""
        import json as _json
        import time as _time
        now = int(_time.time())
        if parsed["action"] == "close":
            self.db.execute(
                "UPDATE elicitations SET status='closed', close_reason='user_x', "
                "resolved_at=? WHERE id=?", (now, parsed["tool_use_id"]))
            await self.send_message(chat_id, "已关闭追问")
        else:
            answers = parsed["answers"]
            answers_json = _json.dumps(answers, ensure_ascii=False)
            from agent.elicitation_state import get as get_state
            state = get_state(parsed["tool_use_id"])
            pipeline_alive = False
            if state and not state.is_resolved:
                state.answer(answers_json)
                pipeline_alive = True
            self.db.execute(
                "UPDATE elicitations SET status='answered_all', answers_json=?, "
                "resolved_at=? WHERE id=?",
                (answers_json, now, parsed["tool_use_id"]))
            if not pipeline_alive:
                summary = "; ".join(
                    f"{a.get('question','')}: {a.get('answer','')}"
                    for a in answers if isinstance(a, dict))
                await self._deliver(chat_id, sid, summary or "（已回答追问）")

    def _record_failure(self, reason: str) -> None:
        self._failures += 1
        self.db.execute(
            "UPDATE platforms SET failure_count=?, last_failure_time=?, last_failure_reason=? "
            "WHERE id=?", (self._failures, now_cst().isoformat(timespec="seconds"),
                           reason, self.platform_id))
        if self._failures == CIRCUIT_THRESHOLD:
            # 首次达到阈值才通知（== 而非 >=）：熔断后连续失败不再重复推送，
            # 只有新一轮熔断（reset 后再次失败累计到阈值）才再次提醒
            self.paused = True
            self.db.execute("UPDATE platforms SET status='paused' WHERE id=?",
                            (self.platform_id,))
            self.notifier("platform_paused",
                          f"接入渠道 {self.platform_type} 连续失败已暂停")

    def _reset_circuit(self) -> None:
        # 内存与库同步清零：避免前端展示残留旧失败计数
        if self._failures:
            self.db.execute(
                "UPDATE platforms SET failure_count=0 WHERE id=?", (self.platform_id,))
        self._failures = 0

    def resume(self) -> None:
        self.paused = False
        self._failures = 0
        self.db.execute("UPDATE platforms SET status='healthy', failure_count=0 WHERE id=?",
                        (self.platform_id,))
