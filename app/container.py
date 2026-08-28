"""
AppContainer —— 依赖装配容器。按依赖顺序实例化并连接全部子系统。

这是把 P0-P10 各模块组装成可运行系统的中枢：注入 embed_fn / notifier /
index_rebuild_fn / context_entry_apply_fn / extract_fn / snapshot_fn 等回调。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from agent.core import AgentCore
from agent.system_agents import (LintAgent, OutputStyleBuilder,
                                 ProfileBuilder, ReviewAgent)
from agent.tool_executor import ToolExecutor
from connectors.credential_store import CredentialStore
from connectors.manager import ConnectorManager
from gateway.notifications import NotificationManager
from infrastructure.config_manager import ConfigManager
from infrastructure.context_entry import ContextEntryManager
from infrastructure.db import Database
from infrastructure.event_bus import EventBus
from infrastructure.json_repair import repair_json
from infrastructure.llm_provider import LLMClient, TokenRecorder
from infrastructure.observability import OperationLogger
from infrastructure.prompt_loader import PROMPTS
from infrastructure.provider_registry import ProviderRegistry
from memory.conflict_detector import ConflictDetector
from memory.distiller import Distiller
from memory.file_writer import FileWriter
from memory.index_builder import IndexBuilder
from memory.lifecycle import LifecycleManager
from memory.linker import Linker
from memory.lint import LintEngine
from memory.palace import Palace
from memory.retriever import Retriever
from memory.vector_compensator import VectorCompensator
from memory.vector_store import VectorStore
from memory.write_gate import MemoryWriteGate
from user_profile.profile_manager import ProfileManager
from scheduler.backup import BackupManager
from scheduler.folder_scan import FolderScanner
from scheduler.ingest import IngestManager
from scheduler.scheduler import TaskScheduler
from agent.session_context import SessionStore
from agent.response_signals import SignalCollector
from soul.skill_manager import SkillManager
from soul.soul_manager import SoulManager
from soul.mood_manager import MoodManager
from soul.mood_trigger_recorder import MoodTriggerRecorder
from soul.mood_action_dispatcher import MoodActionDispatcher
from soul.mood_pattern_extractor import MoodPatternExtractor
from soul.profile_conflict_scanner import ProfileConflictScanner
from soul.profile_review_scanner import ProfileReviewScanner
from tools.base import ToolRegistry
from tools.builtin import register_builtins
from tools.sandbox import Sandbox
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.container")

DISTILL_PROMPT = PROMPTS.load_raw("app/prompts/distill")
MEMORY_CANDIDATE_PROMPT = PROMPTS.load_raw("agent/prompts/memory_candidate_extract")
DISTILL_DOC_PROMPT = PROMPTS.load_raw("app/prompts/distill_document")
EXTRACT_IMAGE_PROMPT = PROMPTS.load_raw("app/prompts/extract_image")
EXTRACT_IMAGE_USER_PROMPT = PROMPTS.load_raw("app/prompts/extract_image_user")


class AppContainer:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._build()

    def _build(self) -> None:
        d = self.data_dir
        # ---- 基础设施 ----
        self.config = ConfigManager(d / "config.yaml")
        # Langfuse 可观测性（默认禁用，配置密钥后自动启用）
        from langfuse.integration import init_tracer
        from langfuse.integration.config import LangfuseConfig
        self.tracer = init_tracer(LangfuseConfig.from_sources(self.config.get))
        self.db = Database(d / "palace.db")
        # 先跑迁移，确保后续组件（如调度器注册）可直接写表
        _migrations = Path(__file__).parent.parent / "migrations"
        self.db.run_migrations(str(_migrations))
        self.bus = EventBus()
        # 事件总线常驻订阅：全事件审计日志（保证总线有真实消费者，
        # 事件流转可观测；后续新功能通过 subscribe 挂接，不改发布方）
        from infrastructure.event_bus import KNOWN_EVENTS

        def _make_audit(evt: str):
            def _audit(payload: dict) -> None:
                logger.info("事件流转：%s payload=%s", evt, payload)
            return _audit
        for _evt in sorted(KNOWN_EVENTS):
            self.bus.subscribe(_evt, _make_audit(_evt))
        self.oplog = OperationLogger(self.db)
        self.creds = CredentialStore(self.db, d)
        self.providers = ProviderRegistry(
            self.db, self.creds,
            slow_model_ids=set(self.config.get_raw("slow_model_ids", [])))
        # 槽位治理：缺失槽位幂等补齐（仅补缺失不覆盖用户配置）+ 启动健康检查
        from infrastructure.provider_registry import (audit_slot_assignments,
                                                      ensure_slot_assignments)
        ensure_slot_assignments(self.providers)
        audit_slot_assignments(self.providers)
        self.token_recorder = TokenRecorder(self.db)
        self.llm = LLMClient(self.token_recorder)

        # ---- 存储与写入 ----
        self.palace = Palace(self.db)

        # 领域中文标签缓存（方案 B）：新领域首次出现时 LLM 翻译一次并入库
        from memory.domain_labeler import DomainLabeler

        async def domain_translate_fn(domains: list[str]) -> dict:
            snap = self.providers.snapshot_for("agent")
            if snap is None:
                return {}
            resp = await self.llm.chat(
                snap, [{"role": "system",
                        "content": PROMPTS.load_raw("app/prompts/domain_label")},
                       {"role": "user", "content": "\n".join(domains)}],
                source="system_agent", json_mode=True)
            return repair_json(resp["content"]).get("labels", {})
        self.domain_labeler = DomainLabeler(self.db, domain_translate_fn)

        self.vs = VectorStore(
            self.db, self.config.get("vector_cache_max_mb", 512))
        self.index_builder = IndexBuilder(self.db, self.palace, d)
        self.ctx_entry = ContextEntryManager(d)
        self.sessions = SessionStore(self.db, d)
        self.notifications = NotificationManager(self.db, self.sessions)

        def notifier(ntype: str, msg: str) -> None:
            self.notifications.push(ntype, msg)
        self.notifier = notifier
        # 后台火忘式写入持续失败时上浮系统告警（防静默丢统计/日志）
        self.db.alert_hook = notifier

        self.fw = FileWriter(self.db, self.palace,
                             self.vs, d, self.bus, notifier)
        # 失败写入重放成功后撤回历史失败横幅（原位改写为已恢复文案）
        self.fw.resolve_failed_fn = lambda: self.notifications.resolve(
            "filewriter_failed", "✅ 此前的写入失败已自动重试成功，数据已恢复，无需处理")

        def _refresh_consciousness_hint() -> None:
            # 第 0 层意识提示：重要记忆关键词 → CONTEXT_ENTRY.md（产品文档 §重要记忆目录）
            try:
                self.ctx_entry.set_consciousness_hint(
                    self.index_builder.important_keywords())
            except Exception:  # noqa: BLE001
                logger.warning("意识提示刷新失败", exc_info=True)
        self.refresh_consciousness_hint = _refresh_consciousness_hint

        def _index_rebuild() -> None:
            # is_important 的三条写入入口最终都触发 index 重建，
            # 在此统一同步意识提示，天然覆盖全部变更路径
            self.index_builder.rebuild()
            _refresh_consciousness_hint()
        self.fw.index_rebuild_fn = _index_rebuild
        self.fw.context_entry_apply_fn = lambda patch: self.ctx_entry.apply_patch(
            patch)

        def _mark_dirty(domain: str) -> None:
            self.index_builder.mark_dirty(domain)
            # 新领域首次写入时异步翻译中文标签（已缓存则直接跳过）
            self.domain_labeler.schedule(domain)
        self.fw.mark_dirty_fn = _mark_dirty

        # ---- 文件 watcher（外部编辑 md/soul/profile 实时同步） ----
        from memory.file_watcher import FileWatcher
        from memory.recovery import reindex_changed

        def _on_memory_change(paths) -> None:
            try:
                r = reindex_changed(self.db, d, paths, vector_store=self.vs)
                # _index.md 重建 + 意识提示刷新交由 FileWriter 单写者串行执行，
                # 避免在 watcher 的 Timer 线程直接写盘与写线程产生竞态
                loop = getattr(self, "_loop", None)
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self.fw.submit("index", {})))
                else:
                    self.index_builder.rebuild(force=True)
                    _refresh_consciousness_hint()
                if r.get("invalid_files"):
                    notifier("md_format_error",
                             "以下记忆文件格式异常，未更新索引："
                             + "、".join(r["invalid_files"][:5]))
                if r.get("missing"):
                    notifier("md_missing",
                             f"检测到 {r['missing']} 个记忆文件被外部删除，可从备份恢复")
            except Exception:  # noqa: BLE001
                logger.exception("记忆文件变更处理失败")

        def _on_soul_change(path) -> None:
            # 读取时已做注入扫描；此处仅告知用户外部修改已重新加载
            notifier("soul_reloaded", "人格文件已被外部修改，已重新加载")

        self.file_watcher = FileWatcher(
            d, on_memory_change=_on_memory_change,
            on_soul_change=_on_soul_change, on_profile_change=None)
        self.fw.mark_internal_fn = self.file_watcher.mark_internal

        # ---- embed 回调 ----
        async def embed_fn(texts: list[str]) -> list[list[float]]:
            snap = self.providers.snapshot_for("embedding")
            if snap is None:
                raise RuntimeError("Embedding 未配置")
            return await self.llm.embed(snap, texts)
        self.embed_fn = embed_fn

        self.vector_compensator = VectorCompensator(
            self.db, self.vs, embed_fn, notifier)

        # ---- 记忆核心 ----
        # 第 2 层精筛回调（agent_model）：带最近对话上下文的相关性守门员，
        # 允许判空（返回空 ids = 本轮无相关记忆，由 Retriever 注入 0 条）
        async def llm_refine(query: str, candidates: list[dict],
                             session_id: str | None = None,
                             context_text: str | None = None) -> list[str]:
            snap = self.providers.snapshot_for("agent")
            if snap is None:
                # 精筛不可用：抛出令 Retriever 走其基于得分的降级挑选（过滤弱尾），
                # 而非盲目返回 top-3 引入噪声
                raise RuntimeError("agent 槽位未配置，第 2 层精筛不可用")
            listing = "\n".join(f"{c['id']}: {c['title']} - {c['summary']}"
                                for c in candidates)
            ctx_part = f"最近对话：\n{context_text}\n\n" if context_text else ""
            prompt = [{"role": "system", "content":
                       PROMPTS.load_raw("app/prompts/memory_refine")},
                      {"role": "user", "content":
                       f"{ctx_part}当前问题：{query}\n候选：\n{listing}"}]
            resp = await self.llm.chat(snap, prompt, source="agent",
                                       session_id=session_id, json_mode=True)
            data = repair_json(resp["content"])
            return data.get("ids", [])[:3]

        self.retriever = Retriever(self.db, self.vs, self.palace, self.config, d,
                                   embed_fn=embed_fn, llm_refine_fn=llm_refine)
        self.linker = Linker(self.db, self.palace, self.vs,
                             self.fw, d, self.config)
        self.conflict = ConflictDetector(
            self.db, self.palace, self.fw, self.linker, d)
        self.lifecycle = LifecycleManager(
            self.db, self.palace, self.fw, d, self.config)
        self.lint = LintEngine(self.db, self.palace, self.vs, self.config)
        self.memory_gate = MemoryWriteGate(self.db, self.config)

        # ---- Distiller 提取回调 ----
        async def extract_fn(text: str, source_type: str = "memory") -> dict:
            snap = self.providers.snapshot_for("agent")
            if snap is None:
                return {"items": []}
            # 文档/知识导入用专用 prompt；对话提炼只产出候选，不直接写 L3。
            prompt = (DISTILL_DOC_PROMPT if source_type == "knowledge"
                      else MEMORY_CANDIDATE_PROMPT if source_type == "memory"
                      else DISTILL_PROMPT)
            resp = await self.llm.chat(
                snap, [{"role": "system", "content": prompt},
                       {"role": "user", "content": text}], source="system_agent",
                json_mode=True)
            # P1-E：JSON 修复可观测 —— 失败时记结构化日志 + 连续失败推系统通知
            try:
                data = repair_json(resp["content"])
            except ValueError as exc:
                from infrastructure.json_repair import REPAIR_STATS
                logger.warning(
                    "extract_fn JSON 修复失败：source_type=%s consecutive=%d err=%s",
                    source_type, REPAIR_STATS.consecutive_failures, exc)
                from memory import _constants as _mem_const
                threshold = _mem_const.JSON_REPAIR_ALERT_THRESHOLD
                if (REPAIR_STATS.consecutive_failures >= threshold
                        and hasattr(self, "notifications")):
                    try:
                        self.notifications.push(
                            "json_repair_failed",
                            f"提取器 JSON 连续 {REPAIR_STATS.consecutive_failures} "
                            "次修复失败，请检查模型输出质量")
                    except Exception:  # noqa: BLE001
                        pass
                return {"items": []}
            # P1-E：修复成功但 items 为空/结构不合规 → 结构化告警但不阻断
            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                logger.debug("extract_fn 无候选：source_type=%s len=%d",
                             source_type, len(text))
            return data

        self.skills = SkillManager(d, self.db, self.fw)

        # ---- 图片解析回调（image_parse_engine: vlm | ocr | off）----
        async def image_extract_fn(path) -> str:
            engine = self.config.get("image_parse_engine", "vlm")
            if engine == "off":
                return ""
            if engine == "ocr":
                from scheduler.ingest import ocr_extract_text
                return await ocr_extract_text(path)
            # vlm：复用已有多模态链路，图片 dataURL 交给视觉模型提取文字/语义
            # 取模链：vision 槽位 → 未配则回退 agent/chat（registry 内处理）
            snap = self.providers.snapshot_for("vision")
            if snap is None:
                return ""
            try:
                from scheduler.ingest import image_to_data_url
                url = image_to_data_url(path)
                resp = await self.llm.chat(
                    snap, [{"role": "system", "content": EXTRACT_IMAGE_PROMPT},
                           {"role": "user", "content": EXTRACT_IMAGE_USER_PROMPT}],
                    images=[url], source="vision")
                return (resp.get("content") or "").strip()
            except Exception:  # noqa: BLE001
                logger.warning("图片 VLM 解析失败：%s", path)
                return ""
        self.image_extract_fn = image_extract_fn

        async def skill_draft_fn(item: dict) -> None:
            # 同类任务出现 skill_draft_threshold（默认 3）次以上才生成 draft
            title = item.get("title", "skill")
            key = title.strip().lower()[:40]
            if not key:
                return
            now = now_cst().isoformat(timespec="seconds")
            row = self.db.query_one(
                "SELECT occurrences, drafted FROM skill_patterns WHERE pattern_key=?",
                (key,))
            if row:
                occ = (row["occurrences"] or 0) + 1
                self.db.execute(
                    "UPDATE skill_patterns SET occurrences=?, last_seen=?, "
                    "title=?, detail=? WHERE pattern_key=?",
                    (occ, now, title, item.get("detail", ""), key))
                drafted = row["drafted"]
            else:
                occ, drafted = 1, 0
                self.db.execute(
                    "INSERT INTO skill_patterns(pattern_key,title,detail,occurrences,"
                    "drafted,first_seen,last_seen) VALUES(?,?,?,1,0,?,?)",
                    (key, title, item.get("detail", ""), now, now))
            threshold = self.config.get("skill_draft_threshold", 3)
            if occ >= threshold and not drafted:
                name = title[:20].replace(" ", "_")
                md = f"---\nstatus: draft\n---\n# {title}\n{item.get('detail', '')}"
                await self.skills.create_draft(name, md)
                self.db.execute(
                    "UPDATE skill_patterns SET drafted=1 WHERE pattern_key=?", (key,))

        async def soul_feedback_fn(item: dict) -> None:
            """SOUL 反馈处理：style 即时生效，persona 走频次累积 → 达阈值入队列。"""
            import hashlib

            feedback_kind = item.get("feedback_kind", "persona")
            proposed = item.get("detail", "")
            canonical_dim = item.get("canonical_dim", "")

            # style 反馈入审核队列，由用户确认后生效
            if feedback_kind == "style" and canonical_dim and canonical_dim != "other":
                import hashlib as _hashlib
                style_key = f"style:{canonical_dim}:{proposed[:50].strip()}"
                style_change_key = _hashlib.md5(
                    style_key.encode()).hexdigest()[:16]
                now_str = now_cst().isoformat(timespec="seconds")
                if self.conflict_scanner.check_rejection_protection(style_change_key, now_str):
                    return
                occ, newly_enqueued = self.conflict_scanner.accumulate_feedback(
                    style_change_key, proposed, item.get(
                        "summary", ""), "style"
                )
                if newly_enqueued:
                    current_dialog = self.soul.read_style().get("对话风格", "")
                    self.conflict_scanner.enqueue_persona_review(
                        style_change_key, proposed, item.get("summary", ""),
                        occ, current_dialog
                    )
                return

            # persona 反馈走频次累积
            ptype = "behavior" if feedback_kind == "behavior" else "persona"
            raw_key = f"persona:{proposed[:50].strip()}"
            change_key = hashlib.md5(raw_key.encode()).hexdigest()[:16]
            now_str = now_cst().isoformat(timespec="seconds")

            # 检查拒绝保护
            if self.conflict_scanner.check_rejection_protection(change_key, now_str):
                return

            # 频次门累积 + 入队
            occ, newly_enqueued = self.conflict_scanner.accumulate_feedback(
                change_key, proposed, item.get("summary", ""), ptype
            )
            if newly_enqueued:
                current_dialog = self.soul.read_style().get("对话风格", "")
                self.conflict_scanner.enqueue_persona_review(
                    change_key, proposed, item.get(
                        "summary", ""), occ, current_dialog
                )

        # 合并前关系判定（相同/演变/矛盾/相关）：防止高相似矛盾被静默合并
        async def merge_judge_fn(new_item: dict, existing: dict) -> dict:
            snap = self.providers.snapshot_for("agent")
            if snap is None:
                return {"relation": "same"}
            existing_detail = (existing.get("detail") or "")[:300]
            content = (f"新信息：{new_item.get('title', '')} — "
                       f"{new_item.get('summary', '')}\n{new_item.get('detail', '')}\n\n"
                       f"已有记忆：{existing.get('title', '')} — "
                       f"{existing.get('summary', '')}"
                       + (f"\n{existing_detail}" if existing_detail else ""))
            resp = await self.llm.chat(
                snap, [{"role": "system",
                        "content": PROMPTS.load_raw("app/prompts/merge_judge")},
                       {"role": "user", "content": content}], source="system_agent",
                json_mode=True)
            return repair_json(resp["content"])
        self.merge_judge_fn = merge_judge_fn

        self.distiller = Distiller(
            self.db, self.palace, self.vs, self.fw, self.linker, self.conflict,
            self.config, extract_fn=extract_fn, embed_fn=embed_fn,
            skill_draft_fn=skill_draft_fn, soul_feedback_fn=soul_feedback_fn,
            merge_judge_fn=merge_judge_fn, memory_gate=self.memory_gate)
        # 向量补偿协程回填向量后回调 Distiller 做回溯去重（修复提炼当刻
        # Embedding 不可用造成的重复记忆）。compensator 先于 distiller 构造，故在此接线。
        self.vector_compensator.rededup_fn = self.distiller.rededup_memory

        # ---- Soul / Profile ----
        self.soul = SoulManager(d, self.fw, self.oplog, notifier)
        self.mood = MoodManager(self.db, self.config)
        self.mood_trigger = MoodTriggerRecorder(self.db)
        self.mood_action_dispatcher = MoodActionDispatcher(
            self.db, self.config)
        self.mood_pattern_extractor = MoodPatternExtractor(
            self.db, self.distiller, self.config)
        self.profile = ProfileManager(d)

        # ---- 画像审核队列 ----
        self.conflict_scanner = ProfileConflictScanner(
            self.db, self.llm, self.providers, self.config)
        self.profile_review_scanner = ProfileReviewScanner(
            self.db, self.conflict_scanner, self.config, notifier)

        # ---- 工具系统 ----
        self.registry = ToolRegistry()
        self.sandbox = Sandbox(d / "workspace",
                               self.config.get_raw("workspace_whitelist", []))
        register_builtins(self.registry, palace=self.palace, retriever=self.retriever,
                          file_writer=self.fw, sandbox=self.sandbox, data_dir=d,
                          config=self.config, llm=self.llm, providers=self.providers,
                          memory_gate=self.memory_gate)
        self.connectors = ConnectorManager(self.db, self.creds, self.registry)

        # ---- Agent Core ----
        self.tool_executor = ToolExecutor(self.registry, self.config, notifier)
        self.signals = SignalCollector(self.db)
        self.core = AgentCore(
            db=self.db, config=self.config, session_store=self.sessions,
            context_entry=self.ctx_entry, soul_manager=self.soul,
            profile_manager=self.profile, retriever=self.retriever,
            tool_registry=self.registry, tool_executor=self.tool_executor,
            lifecycle=self.lifecycle, signal_collector=self.signals,
            llm_client=self.llm, provider_registry=self.providers,
            file_writer=self.fw, skill_manager=self.skills, event_bus=self.bus,
            notifier=notifier, mood_manager=self.mood,
            mood_trigger=self.mood_trigger,
            mood_action_dispatcher=self.mood_action_dispatcher,
            memory_gate=self.memory_gate)

        # ---- 系统 Agent ----
        self.reviewer = ReviewAgent(self.db, self.distiller, self.config, d,
                                    memory_gate=self.memory_gate)
        self.lint_agent = LintAgent(self.lint, self.lifecycle, self.skills,
                                    self.palace, self.conflict, self.bus,
                                    judge_fn=self.merge_judge_fn, data_dir=d,
                                    notifier=notifier)
        self.profile_builder = ProfileBuilder(
            self.db, self.palace, self.fw, self.llm,
            lambda: self.providers.snapshot_for("agent"), d)
        self.output_style_builder = OutputStyleBuilder(
            self.db, self.fw, self.soul, self.llm,
            lambda: self.providers.snapshot_for("agent"), self.config)

        # ---- 调度 / 备份 / 导入 ----
        self.backup = BackupManager(
            self.db, d, self.config, self.fw, self.palace)
        self.ingest = IngestManager(
            self.db, d, self.distiller, self.config, notifier,
            image_extract_fn=self.image_extract_fn)
        # 本地目录全域接入：扫描用户目录增量导入（依赖 ingest 管线）
        self.folder_scanner = FolderScanner(
            self.db, d, self.ingest, self.config, notifier)

        # 图片入库回调：把当轮多模态图片 dataURL 解码后走 ingest 入库（静默）。
        # 由 AgentCore 在“显式记忆指令 + 存在图片”时 fire-and-forget 调用。
        async def image_kb_fn(images) -> None:
            import base64
            import re
            for idx, url in enumerate(images or []):
                m = re.match(
                    r"data:(image/[\w.+-]+);base64,(.+)", url or "", re.S)
                if not m:
                    continue
                ext = m.group(1).split("/")[1].split("+")[0]
                try:
                    content = base64.b64decode(m.group(2))
                except Exception:  # noqa: BLE001
                    continue
                fn = f"chat-image-{now_cst():%Y%m%d%H%M%S}-{idx+1}.{ext}"
                await self.ingest.ingest_file(fn, content, source="chat_image")
        self.core.image_kb_fn = image_kb_fn
        from scheduler.embedding_migration import MigrationRunner
        self.migration_runner = MigrationRunner(
            self.db, self.vs, self.providers, self.llm, self.bus, notifier)
        self.scheduler = TaskScheduler(self.db, self.config, notifier)
        self._register_scheduled_tasks()

        # ---- 插件 ----
        from plugins import PluginManager
        self.plugins = PluginManager(Path(__file__).parent.parent / "plugins",
                                     tool_registry=self.registry, event_bus=self.bus)

        # ---- Gateway IM 适配器管理器 ----
        from gateway.adapter_manager import AdapterManager
        self.adapters = AdapterManager(self.db, self.creds, self.core, self.sessions,
                                       self.notifications, self.config, self.ingest)

    def _purge_old_signals(self) -> int:
        """清理超 output_style_signal_retention_days（默认 90）天的 response_signals。"""
        days = self.config.get("output_style_signal_retention_days", 90)
        cutoff = (now_cst() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM response_signals WHERE create_time < ?", (cutoff,))
        return cur.rowcount if cur else 0

    def _recalc_graph_layout(self) -> None:
        """夜间链兜底：全量重算知识图谱布局坐标（v3.0 方案 C）。"""
        from memory.graph_layout import compute_layout
        compute_layout(self.db)

    def _cleanup_exports(self, days: int = 7) -> int:
        """清理 temp/exports 下超期的 generate_document 产物（夜间链）。"""
        import time
        d = Path(self.data_dir) / "temp" / "exports"
        if not d.exists():
            return 0
        cutoff = time.time() - days * 86400
        n = 0
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    def _register_scheduled_tasks(self) -> None:
        s = self.scheduler
        # 夜间维护链
        s.register_task("auto_backup", "自动备份", lambda: self.backup.create(),
                        "每天 02:00")
        s.register_task("dedup_cleanup", "去重表清理",
                        lambda: self.db.execute(
                            "DELETE FROM message_dedup WHERE processed_at < ?",
                            ((now_cst() - timedelta(days=1))
                             .isoformat(timespec="seconds"),)))
        s.register_task("temp_cleanup", "接收文件缓存清理",
                        lambda: (self.ingest.cleanup_temp_attachments(7),
                                 self._cleanup_exports(7)))
        s.register_task("log_cleanup", "日志清理", lambda: (
            s.purge_old_logs(), self.oplog.purge_expired(90),
            self.migration_runner.purge_old_backups(30),
            self._purge_old_signals(),
            self.db.execute(
                "DELETE FROM pending_writes WHERE status='done' "
                "AND created_at < ?",
                ((now_cst() - timedelta(days=7)
                  ).isoformat(timespec="seconds"),))))
        s.register_task("conflict_cleanup", "已解决矛盾清理",
                        lambda: self.conflict.purge_resolved(30))
        s.register_task("failed_rescan", "failed 写入重扫",
                        lambda: self.fw.rescan_failed())
        s.register_task("graph_layout_recalc", "知识图谱布局重算",
                        lambda: self._recalc_graph_layout())
        s.register_chain("night_maintenance",
                         ["auto_backup", "dedup_cleanup", "temp_cleanup",
                          "log_cleanup", "conflict_cleanup", "failed_rescan",
                          "graph_layout_recalc"])
        # 记忆维护链
        s.register_task("passive_review", "被动记忆回顾", lambda: self.reviewer.run(),
                        "每 N 天 03:00")
        s.register_task("lint_check", "Lint 健康检查",
                        lambda: self.lint_agent.run("lint_scheduled"))
        s.register_task("profile_rebuild", "用户画像重建",
                        lambda: self._profile_rebuild_with_scan())
        s.register_chain("memory_maintenance",
                         ["passive_review", "lint_check", "profile_rebuild"])
        # 独立任务
        s.register_task("output_style_build", "输出样式画像提炼",
                        lambda: self.output_style_builder.build(), "每 7 天")
        s.register_task("local_dir_scan", "本地目录扫描",
                        lambda: self.folder_scanner.scan_all(
                            trigger="schedule"),
                        "每 N 小时（自门控）")
        # v2 情绪模式提取：每周一 04:00 分析 mood_history 沉淀为记忆
        s.register_task("mood_pattern_extract", "情绪模式提取",
                        lambda: asyncio.run(
                            self.mood_pattern_extractor.extract()),
                        "每周一 04:00")
        # 画像审核队列维护：每日 04:30 清理 + 通知
        s.register_task("profile_review_scan", "画像审核队列维护",
                        lambda: self.profile_review_scanner.daily_scan(),
                        "每天 04:30")

    async def _profile_rebuild_with_scan(self) -> bool:
        """画像重建 + 冲突检测（整合版）。

        先保存旧版画像内容，再执行 rebuild，最后对比新旧做冲突扫描。
        这样冲突检测与重建在同一调用链中，避免每日扫描独立调用 rebuild
        与记忆维护链的 profile_rebuild 冲突。
        """
        old_content = self.profile.read_raw()
        ok = await self.profile_builder.rebuild()
        if not ok:
            return False
        # 重建成功后触发冲突检测
        if self.conflict_scanner and old_content:
            try:
                new_content = self.profile.read_raw()
                added = await self.conflict_scanner.scan_profile_rebuild(
                    old_content, new_content
                )
                if added:
                    logger.info("画像冲突检测：%d 条入队", added)
            except Exception:
                logger.warning("画像冲突检测失败", exc_info=True)
        return True

    # ---- 生命周期 ----
    def _ensure_local_embedding_provider(self) -> None:
        """自动按标准 Provider 字段注册本地 BGE-M3，并在安全时接入为 embedding 模型。

        - 幂等：按 base_url+model_id 去重（与设置页手动添加一致），已存在则复用
        - 无 ready 向量时：直接切换 embedding 分配（无需迁移，待向量补偿回填 pending）
        - 已有 ready 向量且当前分配为其它模型：仅注册并提示需走迁移，不静默切换
        """
        cfg = self.config.get_raw("local_embedding", {}) or {}
        if cfg.get("enabled") is False:
            return
        base_url = cfg.get("base_url", "http://127.0.0.1:8100")
        model_id = cfg.get("model_id", "bge-m3")
        display_name = cfg.get("display_name", "本地 BGE-M3")
        context_window = int(cfg.get("context_window", 8192))
        try:
            existing = next(
                (p for p in self.providers.list_providers()
                 if p["base_url"] == base_url and p["model_id"] == model_id), None)
            if existing:
                pid = existing["id"]
            else:
                from memory.naming import provider_id as mk
                pid = mk(self.providers.next_provider_seq())
                self.providers.add_provider(
                    pid, display_name, "openai_compatible", base_url, model_id,
                    "local", 0.0, 0.0, context_window)
                self.oplog.log("provider_add", f"{pid} (本地 embedding 自动注册)")
                logger.info("已自动注册本地 Embedding Provider：%s (%s)",
                            pid, model_id)
            if self.providers.assignment("embedding") == pid:
                self._reheal_failed_vectors()
                return
            ready = self.db.query_one(
                "SELECT count(*) c FROM vectors WHERE vector_status='ready'")["c"]
            if ready == 0:
                self.providers.set_assignment("embedding", pid)
                logger.info("embedding 分配已切换到本地模型 %s（无存量向量，无需迁移）", pid)
                self._reheal_failed_vectors()
            else:
                self.notifier(
                    "embedding_local_ready",
                    f"本地 Embedding 模型已注册（{model_id}）。当前已有 {ready} 条向量，"
                    "切换到本地模型需在设置页触发 Embedding 迁移。")
        except Exception:  # noqa: BLE001
            logger.warning("自动注册本地 Embedding Provider 失败", exc_info=True)

    def _reheal_failed_vectors(self) -> None:
        """本地模型为当前 embedding 时，将历史 failed 占位行重置为 pending。

        旧 mimo 模型无 embeddings 端点导致的 failed 向量，切到可用本地模型后
        交由向量补偿协程重新回填（幂等：成功后置 ready，不会再被重置）。
        """
        cur = self.db.execute(
            "UPDATE vectors SET vector_status='pending' WHERE vector_status='failed'")
        if cur and cur.rowcount:
            logger.info("重置 %d 条 failed 向量为 pending，待本地模型回填", cur.rowcount)

    async def startup(self) -> None:
        # 主事件循环引用：供 watcher 的 Timer 线程将索引重建投递回单写者
        self._loop = asyncio.get_running_loop()
        await self.tracer.start()
        self._ensure_local_embedding_provider()
        self.vs.load()
        try:
            self.plugins.discover_and_load()
        except Exception:  # noqa: BLE001
            logger.warning("插件加载出错")
        await self.fw.start()
        await self.vector_compensator.start()
        try:
            self.file_watcher.start()
        except Exception:  # noqa: BLE001
            logger.warning("文件 watcher 启动出错")
        await self.scheduler.start()
        try:
            await self.connectors.reconnect_all()
        except Exception:  # noqa: BLE001
            logger.warning("连接器重连出错")
        self.notifications.flush_pending()
        try:
            await self.adapters.load_enabled()
        except Exception:  # noqa: BLE001
            logger.warning("IM 适配器加载出错")
        # 启动自检：一致性校验 + 子系统状态概览
        try:
            from memory.recovery import consistency_check
            cc = consistency_check(self.db, self.data_dir)
            if not cc["consistent"]:
                logger.warning("启动一致性校验：md=%s index=%s 不一致，建议 --rebuild-index",
                               cc["md"], cc["index"])
            vc = self.vs.consistency_check()
            if not vc["consistent"]:
                logger.warning("向量缓存与 vectors 表不一致：%s", vc)
            logger.info("启动自检：记忆 md=%s/index=%s，向量缓存 %s 条",
                        cc["md"], cc["index"], vc.get("memory"))
        except Exception:  # noqa: BLE001
            logger.warning("启动自检出错", exc_info=True)
        # 启动时初始化一次意识提示（存量重要记忆无需等待首次写入）
        self.refresh_consciousness_hint()
        # tiktoken 编码器预热（首次加载数百毫秒，避免首个无 usage 的流式响应
        # 在事件循环上触发同步加载），后台线程执行不阻塞启动

        async def _warm_tiktoken() -> None:
            try:
                import tiktoken
                await asyncio.to_thread(tiktoken.get_encoding, "cl100k_base")
            except Exception:  # noqa: BLE001
                pass  # 未安装/加载失败时 estimate_tokens 自有降级
        asyncio.create_task(_warm_tiktoken())
        # 事件循环卡顿哨兵：任何同步重操作阻塞循环（会冻结对话 SSE）
        # 都会在日志中立即现形，防未来回归
        from infrastructure.observability import EventLoopMonitor
        self.loop_monitor = EventLoopMonitor()
        await self.loop_monitor.start()
        logger.info("AppContainer 启动完成")

    async def shutdown(self) -> None:
        if getattr(self, "loop_monitor", None):
            await self.loop_monitor.stop()
        await self.adapters.stop()
        try:
            self.file_watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        await self.scheduler.stop()
        await self.vector_compensator.stop()
        await self.fw.stop()
        await self.tracer.stop()
        self.db.wal_checkpoint("TRUNCATE")
        # 排空写队列并停止单写线程（火忘式写入全部落盘后才退出）
        self.db.close()
        logger.info("AppContainer 已优雅停机")
