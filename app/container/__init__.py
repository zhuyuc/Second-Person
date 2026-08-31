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
from memory.context_entry import ContextEntryManager
from infrastructure.db import Database
from infrastructure.event_bus import EventBus
from infrastructure.llm_provider import LLMClient, TokenRecorder
from infrastructure.observability import OperationLogger
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
from agent.projects import ProjectStore
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
from tools.fs import register_fs_tools
from tools.fs.observation import FsObservationStore
from tools.fs.policy import PolicyStore
from tools.fs.workspace import WorkspaceResolver
from tools.sandbox import Sandbox
from infrastructure.timeutil import now_cst

from .wiring import (
    build_container_layers,
    make_extract_fn,
    make_image_extract_fn,
    make_llm_refine_fn,
    make_merge_judge_fn,
    make_skill_draft_fn,
    make_soul_feedback_fn,
    setup_domain_labeler,
    setup_image_kb_fn,
    setup_notifier,
)

logger = logging.getLogger("second_person.container")


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
        _migrations = Path(__file__).parent.parent.parent / "migrations"
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

        setup_domain_labeler(self)

        self.vs = VectorStore(
            self.db, __import__("memory._constants", fromlist=["VECTOR_CACHE_MAX_MB"]).VECTOR_CACHE_MAX_MB)
        self.index_builder = IndexBuilder(self.db, self.palace, d)
        self.ctx_entry = ContextEntryManager(d)
        self.sessions = SessionStore(self.db, d)
        self.projects = ProjectStore(self.db, d)
        self.notifications = NotificationManager(self.db, self.sessions)

        setup_notifier(self)

        self.fw = FileWriter(self.db, self.palace,
                             self.vs, d, self.bus, self.notifier)
        build_container_layers(self, d)

        self.vector_compensator = VectorCompensator(
            self.db, self.vs, self.embed_fn, self.notifier)

        # ---- 记忆核心 ----
        self.retriever = Retriever(self.db, self.vs, self.palace, self.config, d,
                                   embed_fn=self.embed_fn,
                                   llm_refine_fn=make_llm_refine_fn(self))
        self.linker = Linker(self.db, self.palace, self.vs,
                             self.fw, d, self.config)
        self.conflict = ConflictDetector(
            self.db, self.palace, self.fw, self.linker, d)
        self.lifecycle = LifecycleManager(
            self.db, self.palace, self.fw, d, self.config)
        self.lint = LintEngine(self.db, self.palace, self.vs, self.config)
        self.memory_gate = MemoryWriteGate(self.db, self.config)

        self.skills = SkillManager(d, self.db, self.fw)

        extract_fn = make_extract_fn(self)
        self.image_extract_fn = make_image_extract_fn(self)
        skill_draft_fn = make_skill_draft_fn(self)
        soul_feedback_fn = make_soul_feedback_fn(self)
        self.merge_judge_fn = make_merge_judge_fn(self)

        self.distiller = Distiller(
            self.db, self.palace, self.vs, self.fw, self.linker, self.conflict,
            self.config, extract_fn=extract_fn, embed_fn=self.embed_fn,
            skill_draft_fn=skill_draft_fn, soul_feedback_fn=soul_feedback_fn,
            merge_judge_fn=self.merge_judge_fn, memory_gate=self.memory_gate)
        # 向量补偿协程回填向量后回调 Distiller 做回溯去重（修复提炼当刻
        # Embedding 不可用造成的重复记忆）。compensator 先于 distiller 构造，故在此接线。
        self.vector_compensator.rededup_fn = self.distiller.rededup_memory

        # ---- Soul / Profile ----
        self.soul = SoulManager(d, self.fw, self.oplog, self.notifier)
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
            self.db, self.conflict_scanner, self.config, self.notifier)

        # ---- 工具系统 ----
        self.registry = ToolRegistry()
        self.sandbox = Sandbox(d / "workspace",
                               self.config.get_raw("workspace_whitelist", []))
        register_builtins(self.registry, palace=self.palace, retriever=self.retriever,
                          file_writer=self.fw, sandbox=self.sandbox, data_dir=d,
                          config=self.config, llm=self.llm, providers=self.providers,
                          memory_gate=self.memory_gate)
        # ---- M3：fs 工具族 + 沙箱四档 -------------------------------------
        self.fs_observations = FsObservationStore(self.db)
        self.policy_store = PolicyStore(
            self.db, self.projects, self.config,
            legacy_workspace=self.sandbox.workspace,
            legacy_whitelist=self.sandbox.whitelist)
        self.workspace_resolver = WorkspaceResolver(self.policy_store)
        register_fs_tools(self.registry,
                          observation_store=self.fs_observations,
                          config=self.config)
        self.connectors = ConnectorManager(self.db, self.creds, self.registry)

        # ---- v7：token 度量 + 自动压缩 --------------------------------------
        from agent.token_meter import TokenMeter
        from agent.compaction_engine import CompactionEngine
        from memory import _constants as _mem_const
        self.token_meter = TokenMeter()
        self.compaction_engine = CompactionEngine(
            db=self.db, sessions=self.sessions, llm=self.llm,
            providers=self.providers, meter=self.token_meter,
            threshold_ratio=self.config.get(
                "compaction_threshold_ratio", _mem_const.COMPACTION_THRESHOLD_RATIO),
            retain_ratio=self.config.get(
                "compaction_retain_ratio", _mem_const.COMPACTION_RETAIN_RATIO),
            max_retries=self.config.get(
                "compaction_max_retries", _mem_const.COMPACTION_MAX_RETRIES),
        )

        # ---- Agent Core ----
        self.tool_executor = ToolExecutor(
            self.registry, self.config, self.notifier,
            workspace_resolver=self.workspace_resolver)
        self.signals = SignalCollector(self.db)
        self.core = AgentCore(
            db=self.db, config=self.config, session_store=self.sessions,
            context_entry=self.ctx_entry, soul_manager=self.soul,
            profile_manager=self.profile, retriever=self.retriever,
            tool_registry=self.registry, tool_executor=self.tool_executor,
            lifecycle=self.lifecycle, signal_collector=self.signals,
            llm_client=self.llm, provider_registry=self.providers,
            file_writer=self.fw, skill_manager=self.skills, event_bus=self.bus,
            notifier=self.notifier, mood_manager=self.mood,
            mood_trigger=self.mood_trigger,
            mood_action_dispatcher=self.mood_action_dispatcher,
            memory_gate=self.memory_gate,
            projects=self.projects,
            workspace_resolver=self.workspace_resolver,
            token_meter=self.token_meter,
            compaction_engine=self.compaction_engine)

        # ---- 系统 Agent ----
        self.reviewer = ReviewAgent(self.db, self.distiller, self.config, d,
                                    memory_gate=self.memory_gate)
        self.lint_agent = LintAgent(self.lint, self.lifecycle, self.skills,
                                    self.palace, self.conflict, self.bus,
                                    judge_fn=self.merge_judge_fn, data_dir=d,
                                    notifier=self.notifier)
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
            self.db, d, self.distiller, self.config, self.notifier,
            image_extract_fn=self.image_extract_fn)
        # 本地目录全域接入：扫描用户目录增量导入（依赖 ingest 管线）
        self.folder_scanner = FolderScanner(
            self.db, d, self.ingest, self.config, self.notifier)

        setup_image_kb_fn(self)
        from scheduler.embedding_migration import MigrationRunner
        self.migration_runner = MigrationRunner(
            self.db, self.vs, self.providers, self.llm, self.bus, self.notifier)
        self.scheduler = TaskScheduler(self.db, self.config, self.notifier)
        self._register_scheduled_tasks()

        # ---- 插件 ----
        from plugins import PluginManager
        self.plugins = PluginManager(Path(__file__).parent.parent.parent / "plugins",
                                     tool_registry=self.registry, event_bus=self.bus)

        # ---- Gateway IM 适配器管理器 ----
        from gateway.adapter_manager import AdapterManager
        self.adapters = AdapterManager(self.db, self.creds, self.core, self.sessions,
                                       self.notifications, self.config, self.ingest)

        # ---- App 业务服务层 ----
        from app.services.settings_service import SettingsService
        from app.services.chat_service import ChatService
        from app.services.memory_service import MemoryService
        self.settings_svc = SettingsService(self)
        self.chat_svc = ChatService(self)
        self.memory_svc = MemoryService(self)

    def _purge_old_signals(self) -> int:
        """清理超保留期的 response_signals。"""
        from memory import _constants as _mem_const
        days = _mem_const.OUTPUT_STYLE_SIGNAL_RETENTION_DAYS
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
        # 项目目录丢失检测（每小时轻量扫描；命中推 project_dir_missing 通知）
        s.register_task("project_dir_missing_scan", "项目目录丢失检测",
                        lambda: self.projects.scan_missing_dirs(
                            notifier=self.notifier),
                        "每 1 小时")
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
        # 取消未完成的后台任务（mood/标题/handoff/索引重建等 fire-and-forget）
        from infrastructure import background_tasks
        await background_tasks.shutdown(timeout=5.0)
        # 关闭 LLM 共享连接池
        try:
            await self.llm.aclose()
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
