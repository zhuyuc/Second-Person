"""
系统 Agent（产品文档 §系统 Agent / 开发文档 §6.9）。

5 个系统 Agent（压缩 Agent 见 compression.py）：
- 回顾 Agent（reviewer）：每 3 天读对话+文档 → Distiller 提炼
- Lint Agent（lint-worker）：六项检查 + lifecycle 流转 + 第七项技能提炼归档 → lint.completed
- 画像 Agent（profile-builder）：lint.completed 触发重建 user_profile.md；引导模式生成初始 SOUL
- 输出画像 Agent（output-style-builder）：signal 分箱统计 + LLM 提炼 → soul_style auto 写入
Agent 失败隔离：单个 Agent 失败不影响主流程（调度器包裹异常）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.sysagent")


# ---------------------------------------------------------------------------
# 回顾 Agent
# ---------------------------------------------------------------------------
class ReviewAgent:
    def __init__(self, db, distiller, config, data_dir=None, memory_gate=None):
        self.db = db
        self.distiller = distiller
        self.config = config
        from pathlib import Path
        self.data_dir = Path(data_dir) if data_dir else None
        self.memory_gate = memory_gate

    async def run(self) -> int:
        """读近 N 天对话原文 + 会话压缩摘要 + 新导入文档 → Distiller。
        超 context 上限时按时间（按天）切分为多个子任务串行。"""
        days = self.config.get("passive_review_interval_days", 3)
        cutoff = (now_cst() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        total = 0
        # 1) 近 N 天对话原文（按天切分）；携带用户赞/踩反馈作为提炼修正信号
        rows = self.db.query_all(
            "SELECT role,content,create_time,feedback FROM conversations "
            "WHERE create_time>=? AND message_type='normal' ORDER BY id", (cutoff,))
        if rows:
            from collections import OrderedDict
            by_day: "OrderedDict[str, list]" = OrderedDict()
            for r in rows:
                day = (r["create_time"] or "")[:10]
                # T1-D 说话人硬分离：只把用户发言喂给提炼器。
                # AI 的推断如果被回喂给提炼器，会被 LLM 当作"用户说过"再蒸馏一次
                # （回声环）。点踩反馈只保留 assistant 提示作为下轮线索，不进入
                # 事实提取语料；点赞反馈仍只标注 user 侧，不复用 assistant 回复。
                if r["role"] != "user":
                    continue
                fb = ""
                if r["feedback"] == 2:
                    fb = "[本轮 AI 回复曾被用户点踩，请谨慎提炼] "
                elif r["feedback"] == 1:
                    fb = "[本轮 AI 回复曾被用户点赞] "
                content = str(r["content"] or "").strip()
                if not content:
                    continue
                by_day.setdefault(day, []).append(f"用户: {fb}{content}")
            for day, lines in by_day.items():
                text = "\n".join(lines)
                for sub in self._split_if_oversized(text):
                    try:
                        written = await self.distiller.distill(sub, source_type="memory")
                        total += len(written)
                    except Exception:  # noqa: BLE001 - 单块失败不拖垮整任务/整链
                        logger.warning("回顾提炼失败（%s 分块），跳过继续",
                                       day, exc_info=True)
        # 2) 会话压缩摘要：P2-A 下线 —— 摘要是 L1 派生视图，不作为长期记忆提炼源
        #    产品方案 §13 明确"不把上下文压缩摘要直接写入 L3"；旧路径会造成
        #    AI 推断被回喂给提炼器的回声环。摘要仍供 handoff/上下文注入使用。
        # 3) 新导入文档（回顾链补提炼，依赖去重避免与 Ingest 重复）
        total += await self._distill_recent_docs(cutoff)
        # 4) 主动记忆检测候选：窗口外的单独补提炼（窗口内已被第 1 步覆盖）
        total += await self._distill_review_candidates(cutoff)
        # 候选经过跨会话证据/用户确认后才允许进入 L3。
        if self.memory_gate is not None:
            total += await self.memory_gate.promote_ready(self.distiller)
        # 发布 review.completed 事件（提炼出记忆时视为有效完成）
        if total > 0:
            from app.main import get_container
            try:
                c = get_container()
                if c.bus:
                    from infrastructure.event_bus import EVT_REVIEW_COMPLETED
                    await c.bus.publish(EVT_REVIEW_COMPLETED, {"count": total})
            except Exception:  # noqa: BLE001
                pass
        return total

    async def _distill_review_candidates(self, cutoff: str) -> int:
        """兼容消费旧 review_candidates，并转换为新候选池。"""
        try:
            rows = self.db.query_all(
                "SELECT rc.message_id, c.content, c.create_time FROM review_candidates rc "
                "JOIN conversations c ON rc.message_id=c.id")
        except Exception:  # noqa: BLE001 - 表未建（旧库未迁移）时静默跳过
            return 0
        total = 0
        for r in rows:
            if (r["create_time"] or "") >= cutoff:
                continue  # 窗口内的已由全量扫描覆盖
            try:
                if self.memory_gate is not None:
                    self.memory_gate.enqueue(
                        {"title": str(r["content"] or "")[:30],
                         "summary": str(r["content"] or "")[:200],
                         "detail": str(r["content"] or ""),
                         "attribution": "verified", "domain": "general"},
                        "memory", evidence={"source_type": "conversation",
                                              "source_ref": str(r["message_id"]),
                                              "excerpt": str(r["content"] or "")[:500],
                                              "captured_at": r["create_time"]})
                else:
                    written = await self.distiller.distill(
                        f"user: {r['content']}", source_type="memory")
                    total += len(written)
            except Exception:  # noqa: BLE001
                logger.warning("回顾候选提炼失败：msg=%s",
                               r["message_id"], exc_info=True)
        self.db.execute("DELETE FROM review_candidates")
        return total

    async def _distill_session_summaries(self, cutoff: str) -> int:
        """P2-A：已下线。方法保留仅为兼容旧调用者，直接返回 0。"""
        return 0

    async def _distill_recent_docs(self, cutoff: str) -> int:
        """P2-E：raw_docs 补提炼加幂等标记。完成后 review_status='distilled'
        并记录 last_distilled_at，避免下次回顾链再跑一遍 LLM。"""
        if not self.data_dir:
            return 0
        rows = self.db.query_all(
            "SELECT id, file_path, extracted_text FROM raw_docs "
            "WHERE imported_at>=? AND review_status IS NULL", (cutoff,))
        if not rows:
            return 0
        from pathlib import Path as _P
        from scheduler.ingest import extract_text
        total = 0
        for r in rows:
            text = r["extracted_text"] or ""
            if not text:
                fp = _P(r["file_path"])
                if not fp.exists():
                    continue
                try:
                    text = extract_text(fp)
                except Exception:  # noqa: BLE001
                    continue
            distilled_ok = False
            for sub in self._split_if_oversized(text):
                if sub.strip():
                    try:
                        written = await self.distiller.distill(sub, source_type="knowledge")
                        total += len(written)
                        distilled_ok = True
                    except Exception:  # noqa: BLE001
                        logger.warning("文档补提炼失败：%s，跳过继续",
                                       r["file_path"], exc_info=True)
            # 幂等标记：无论是否有候选产出，只要蒸馏跑过就置 distilled
            if distilled_ok:
                try:
                    self.db.execute(
                        "UPDATE raw_docs SET review_status='distilled' WHERE id=?",
                        (r["id"],))
                except Exception:  # noqa: BLE001
                    pass
        return total

    @staticmethod
    def _split_if_oversized(text: str, limit_chars: int = 16000) -> list[str]:
        if len(text) <= limit_chars:
            return [text]
        parts, buf, size = [], [], 0
        for line in text.split("\n"):
            buf.append(line)
            size += len(line)
            if size >= limit_chars:
                parts.append("\n".join(buf))
                buf, size = [], 0
        if buf:
            parts.append("\n".join(buf))
        return parts


# ---------------------------------------------------------------------------
# Lint Agent 运行器
# ---------------------------------------------------------------------------
class LintAgent:
    def __init__(self, lint_engine, lifecycle, skill_manager, palace,
                 conflict_detector, event_bus=None, judge_fn=None,
                 data_dir=None, notifier=None):
        self.lint = lint_engine
        self.lifecycle = lifecycle
        self.skills = skill_manager
        self.palace = palace
        self.conflict = conflict_detector
        self.bus = event_bus
        self.judge_fn = judge_fn          # LLM 关系判定（same/evolved/contradicts/related）
        self.data_dir = data_dir
        self.notify = notifier or (lambda t, m: None)

    async def run(self, task_id: str) -> dict:
        # T2-A：先跑 is_important 衰减，避免"过期 + 重要"记忆被反复保护
        try:
            self.lifecycle.decay_is_important()
        except Exception:  # noqa: BLE001
            logger.warning("is_important 衰减失败，跳过", exc_info=True)
        # 过期检测 → 标 stale
        for mid in self.lifecycle.detect_stale_candidates():
            await self.lifecycle.mark_stale(mid)
        # 记忆自身复核周期到期 → 降为待复核，避免旧事实继续作为当前事实使用
        review_due = 0
        for mid in self.lifecycle.detect_review_due():
            review_due += int(await self.lifecycle.mark_review_due(mid))
        # stale → archived：连续两个 Lint 周期未恢复则自动归档
        archived = await self._auto_archive_stale()
        # 矛盾检测（先于重复检测：contradicts 对会被重复检测排斥）
        conflicts = await self._detect_conflicts()
        # 孤立 / 重复检测 → 生成建议
        self.lint.detect_orphans(task_id)
        self.lint.detect_duplicates(task_id)
        # 目录漂移修复：md 与索引不一致时以 md 为准同步
        drift_fixed = self.lint.fix_index_drift(self.palace, self.data_dir) \
            if self.data_dir else 0
        # 第七项：技能归档（90 天未用）
        for name in self.skills.archive_unused(90):
            await self.skills.fw.submit("skill", {"op": "archive", "skill_name": name}) \
                if self.skills.fw else None
        counts = self.lint.counts()
        score, breakdown = self.lint.health_score(counts)
        if self.bus:
            from infrastructure.event_bus import EVT_LINT_COMPLETED
            await self.bus.publish(EVT_LINT_COMPLETED, {"score": score})
        return {"health_score": score, "counts": counts,
            "auto_archived": archived, "conflicts_found": conflicts,
                "drift_fixed": drift_fixed, "review_due": review_due}

    async def _auto_archive_stale(self) -> int:
        """P3-C：只归档 confidence != strong 且 is_important != 1 的记忆。
        strong / important 记忆（用户姓名/生日等低频事实典型）即便 stale 也
        不自动归档；只有用户手动才归档。"""
        db = self.lifecycle.db
        db.execute(
            "UPDATE memories SET stale_lint_runs=COALESCE(stale_lint_runs,0)+1 "
            "WHERE lifecycle='stale'")
        rows = db.query_all(
            "SELECT id FROM memories WHERE lifecycle='stale' "
            "AND stale_lint_runs>=2 "
            "AND confidence != 'strong' "
            "AND COALESCE(is_important, 0) = 0")
        for r in rows:
            await self.lifecycle.fw.submit(
                "memory", {"op": "archive", "memory_id": r["id"]})
        return len(rows)

    async def _detect_conflicts(self) -> int:
        """高相似候选对送 LLM 判定；contradicts → mark_conflict（disputed+
        contradicts+conflict 文件），evolved → 建 evolved_from 保留时间线，
        related → 建 related 引用（同主题不同侧面，不构成矛盾）。"""
        if not self.judge_fn:
            return 0
        found = 0
        for mid_a, mid_b, _score in self.lint.conflict_candidate_pairs(
                floor=0.8, limit=10):
            ra, rb = self.palace.get(mid_a), self.palace.get(mid_b)
            if not ra or not rb:
                continue
            try:
                data = await self.judge_fn(
                    {"title": ra["title"], "summary": ra["summary"] or "",
                     "detail": self._fetch_detail(mid_a)},
                    {"title": rb["title"], "summary": rb["summary"] or "",
                     "detail": self._fetch_detail(mid_b)})
                relation = (data or {}).get("relation", "same")
            except Exception:  # noqa: BLE001
                continue
            if relation == "contradicts":
                await self.conflict.mark_conflict(mid_a, mid_b, ra["title"])
                self.notify("disputed_memory",
                            f"发现矛盾记忆：「{ra['title']}」与「{rb['title']}」，"
                            "请到记忆中心·健康度裁决")
                found += 1
            elif relation == "evolved":
                # id 更大 = 创建更晚，新观点 evolved_from 旧观点
                newer, older = (mid_a, mid_b) if mid_a > mid_b else (
                    mid_b, mid_a)
                await self.conflict.linker.add_link(
                    newer, older, "evolved_from", bidirectional=False)
            elif relation == "related":
                # 建 related 引用，同时避免下次扫描重复送判这一对
                await self.conflict.linker.add_link(mid_a, mid_b, "related")
        return found

    def _fetch_detail(self, memory_id: str, max_len: int = 300) -> str:
        """从 FTS 索引取 detail 正文片段（避免读盘解析 md）。"""
        try:
            row = self.lint.db.query_one(
                "SELECT detail FROM memories_fts WHERE memory_id=?", (memory_id,))
            return ((row["detail"] or "") if row else "")[:max_len]
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------
# 画像 Agent
# ---------------------------------------------------------------------------
PROFILE_PROMPT = PROMPTS.load_raw("agent/prompts/profile_rebuild")

INITIAL_SOUL_PROMPT = PROMPTS.load_raw("agent/prompts/initial_soul")


class ProfileBuilder:
    def __init__(self, db, palace, file_writer, llm_client, agent_snapshot_fn, data_dir):
        self.db = db
        self.palace = palace
        self.fw = file_writer
        self.llm = llm_client
        self.snapshot_fn = agent_snapshot_fn
        self.data_dir = data_dir

    async def rebuild(self, session_id: str | None = None) -> bool:
        """P4-E：只用 confidence != low 且 verification_state != inferred 的
        记忆重建画像，避免"画像 → identity 注入 → 强化 inferred 记忆"的自我
        强化环。frontmatter 里保留 source_memory_ids 数组供审计追溯。"""
        snap = self.snapshot_fn()
        if snap is None:
            return False
        rows = self.db.query_all(
            "SELECT id,title,summary,domain FROM memories "
            "WHERE lifecycle IN ('active','stable') "
            "AND confidence != 'low' "
            "AND COALESCE(verification_state,'unverified') != 'inferred' "
            "ORDER BY access_count DESC LIMIT 100")
        if not rows:
            return False
        mem_text = "\n".join(
            f"- [{r['domain']}] {r['title']}：{r['summary']}" for r in rows)
        try:
            resp = await self.llm.chat(
                snap, [{"role": "system", "content": PROFILE_PROMPT},
                       {"role": "user", "content": mem_text}], source="system_agent",
                session_id=session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("画像重建失败：%s", e)
            return False
        # 记录来源记忆 ID（前 200 个字符），供 UI/审计快速回溯到底哪些 memory
        # 参与了画像构建；避免"画像里说用户是产品经理但找不到出处"
        source_ids = [r["id"] for r in rows]
        source_ids_str = ",".join(source_ids)[:512]
        fm_head = (f"---\nlast_rebuilt: {now_cst().isoformat(timespec='seconds')}\n"
                   f"source_memory_count: {len(rows)}\n"
                   f"source_memory_ids: \"{source_ids_str}\"\n"
                   f"source_filter: confidence!=low,verification_state!=inferred\n---\n")
        await self.fw.submit("profile", {"content": fm_head + resp["content"],
                                         "rebuilt_at": now_cst().isoformat()})
        return True

    async def build_initial_soul(self, welcome_conversation: str,
                                 session_id: str | None = None) -> dict | None:
        """引导模式：从欢迎对话生成 SOUL_CORE + SOUL_STYLE 前两段草稿。"""
        snap = self.snapshot_fn()
        if snap is None:
            return None
        try:
            resp = await self.llm.chat(
                snap, [{"role": "system", "content": INITIAL_SOUL_PROMPT},
                       {"role": "user", "content": welcome_conversation}],
                source="system_agent", session_id=session_id)
            return repair_json(resp["content"])
        except Exception as e:  # noqa: BLE001
            logger.warning("初始 SOUL 生成失败：%s", e)
            return None


# ---------------------------------------------------------------------------
# 输出画像 Agent
# ---------------------------------------------------------------------------
OUTPUT_STYLE_PROMPT = PROMPTS.load_raw("agent/prompts/output_style")


class OutputStyleBuilder:
    def __init__(self, db, file_writer, soul_manager, llm_client, agent_snapshot_fn, config):
        self.db = db
        self.fw = file_writer
        self.soul = soul_manager
        self.llm = llm_client
        self.snapshot_fn = agent_snapshot_fn
        self.config = config

    def signal_count(self) -> int:
        return self.db.query_one("SELECT count(*) c FROM response_signals")["c"]

    def _last_build_time(self) -> str | None:
        row = self.db.query_one(
            "SELECT last_run FROM scheduled_tasks WHERE task_id='output_style_last_built'")
        return row["last_run"] if row and row["last_run"] else None

    def _mark_built(self) -> None:
        """仅在实际完成一次提炼时记录（与 run_task 的 last_run 区分）。"""
        self.db.execute(
            "INSERT OR REPLACE INTO scheduled_tasks(task_id,name,schedule,status,"
            "last_run,next_run) VALUES('output_style_last_built','output_style_last_built',"
            "'','completed',?,'')",
            (now_cst().isoformat(timespec="seconds"),))

    def should_build(self) -> bool:
        """触发条件（统一表述）：signal<50 不执行；首次达 50 立即；
        之后每 N 天或新增满 batch 条提前。暂停自动演化时不自动执行。"""
        if not self.config.get("output_style_auto_evolve_enabled", True):
            return False
        # 回滚后 14 天内不自动更新（避免刚回滚又被覆盖）
        cooldown = (now_cst() - timedelta(days=14)
                    ).isoformat(timespec="seconds")
        rolled = self.db.query_one(
            "SELECT 1 FROM operation_logs WHERE operation='soul_style_rollback' "
            "AND detail LIKE 'auto%' AND create_time > ? LIMIT 1", (cooldown,))
        if rolled:
            return False
        count = self.signal_count()
        if count < 50:
            return False
        last = self._last_build_time()
        if not last:
            return True  # 首次达 50 立即
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        from memory import _constants as _mem_const
        interval = _mem_const.OUTPUT_STYLE_REVIEW_INTERVAL_DAYS
        if (now_cst() - last_dt) >= timedelta(days=interval):
            return True
        from memory import _constants as _mem_const
        batch = _mem_const.OUTPUT_STYLE_SIGNAL_BATCH_THRESHOLD
        new_signals = self.db.query_one(
            "SELECT count(*) c FROM response_signals WHERE create_time > ?",
            (last,))["c"]
        return new_signals >= batch

    async def build(self, force: bool = False,
                    session_id: str | None = None) -> bool:
        if not force and not self.should_build():
            return False
        count = self.signal_count()
        if not force and count < 50:
            return False  # 冷启动
        snap = self.snapshot_fn()
        if snap is None:
            return False
        from memory import _constants as _mem_const
        window = _mem_const.OUTPUT_STYLE_SIGNAL_WINDOW_DAYS
        cutoff = (now_cst() - timedelta(days=window)
                  ).isoformat(timespec="seconds")
        # 双块归因数据源（v3 §反馈闭环）：response_signals JOIN 策略快照；
        # next_user_time 用于归因侧计算追问间隔（弱信号窗口过滤），不改采集逻辑
        rows = self.db.query_all(
            "SELECT rs.context_label,rs.char_count,rs.bullet_count,rs.table_count,"
            "rs.conclusion_position,rs.explicit_reaction,rs.explicit_keywords,"
            "rs.implicit_reaction,rs.message_id,rs.create_time AS signal_time,"
            "c.response_strategy_json,c.session_id,"
            "(SELECT MIN(c2.create_time) FROM conversations c2 "
            " WHERE c2.session_id=c.session_id AND c2.id>c.id AND c2.role='user')"
            " AS next_user_time "
            "FROM response_signals rs "
            "JOIN conversations c ON c.id=rs.message_id "
            "WHERE rs.create_time>=?", (cutoff,))
        if not rows:
            return False
        from memory import _constants as _mem_const
        followup_window = _mem_const.STRATEGY_FOLLOWUP_WINDOW_SECONDS
        # 分场景统计（context_label：chat/opinion/fact_query/tech_help/other）：
        # 避免把不同场景混在一起得出“点赞平均字数”式的一刀切结论
        scenes: dict[str, dict] = {}
        strategy_rows: list[str] = []  # 策略维度归因素材（含弱信号标注）
        for r in rows:
            label = r["context_label"] or "other"
            s = scenes.setdefault(
                label, {"n": 0, "like": 0, "dislike": 0, "like_chars": 0,
                        "keywords": []})
            s["n"] += 1
            if r["explicit_reaction"] == 1:
                s["like"] += 1
                s["like_chars"] += r["char_count"] or 0
            elif r["explicit_reaction"] == 2:
                s["dislike"] += 1
            if r["explicit_keywords"]:
                s["keywords"].append(r["explicit_keywords"])
            # 策略归因素材：仅收有策略快照的消息；追问弱信号按窗口过滤
            snap_txt = r["response_strategy_json"]
            if snap_txt:
                weak = ""
                if (r["explicit_keywords"] or r["implicit_reaction"] == "follow_up_clarify") \
                        and r["next_user_time"]:
                    try:
                        gap = (datetime.fromisoformat(r["next_user_time"])
                               - datetime.fromisoformat(r["signal_time"]))
                        if gap.total_seconds() <= followup_window:
                            weak = f"追问弱信号[{r['explicit_keywords'] or '追问'}]"
                    except ValueError:
                        pass
                reaction = {1: "like", 2: "dislike"}.get(
                    r["explicit_reaction"], "none")
                strategy_rows.append(
                    f"- message_id={r['message_id']} 场景={label} "
                    f"反应={reaction}{' ' + weak if weak else ''} "
                    f"策略={snap_txt}")
        scene_names = {"chat": "闲聊寒暄", "opinion": "观点征询",
                       "fact_query": "事实/知识查询",
                       "tech_help": "计算/文件/技术任务", "other": "其他"}
        scene_lines = []
        for label in ("chat", "opinion", "fact_query", "tech_help", "other"):
            s = scenes.get(label)
            if not s:
                continue
            avg_like = s["like_chars"] / s["like"] if s["like"] else 0
            kws = ";".join(s["keywords"]) or "无"
            scene_lines.append(
                f"- {scene_names.get(label, label)}：样本 {s['n']} 条，"
                f"点赞 {s['like']} 踩 {s['dislike']}，"
                f"点赞平均字数 {avg_like:.0f}，偏好关键词：{kws}")
        stat = "\n".join(scene_lines)
        # 双块输入组装：形态维度统计 + 策略决策维度记录（v3，两类归因独立）
        user_content = f"## 输出形态维度统计\n{stat}"
        if strategy_rows:
            user_content += ("\n\n## 策略决策维度记录（供策略偏好归因）\n"
                             + "\n".join(strategy_rows[:60]))
        else:
            user_content += ("\n\n## 策略决策维度记录\n无（仅做输出样式归因，"
                             "strategy_preference_candidates 输出空数组）")
        try:
            resp = await self.llm.chat(
                snap, [{"role": "system", "content": OUTPUT_STYLE_PROMPT},
                       {"role": "user", "content": user_content}],
                source="system_agent",
                session_id=session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("输出画像提炼失败：%s", e)
            return False
        # 双块解析：JSON 两键；解析失败兼容回退旧行为（整体作为样式文本）
        data = repair_json(resp["content"])
        strategy_count = 0
        if isinstance(data, dict) and (
                "output_style_text" in data
                or "strategy_preference_candidates" in data):
            new_text = str(data.get("output_style_text") or "").strip()
            strategy_count = self._enqueue_strategy_candidates(
                data.get("strategy_preference_candidates") or [])
        else:
            new_text = resp["content"].strip()
        if new_text:
            # 演化频率控制：与上一版 diff 相似度 > 0.95 不占新版号（difflib 真实 diff）
            cur = self.soul.read_style().get("输出样式", "")
            create_version = _similarity(cur, new_text) <= 0.95
            await self.fw.submit("soul_style", {
                "section": "auto", "content": new_text, "create_version": create_version,
                "diff_summary": "输出画像自动更新"})
        self._mark_built()
        # 发布 output_style.updated 事件
        from app.main import get_container
        try:
            c = get_container()
            if c and getattr(c, "bus", None):
                from infrastructure.event_bus import (
                    EVT_OUTPUT_STYLE_UPDATED, EVT_STRATEGY_REFLECTED)
                if new_text:
                    await c.bus.publish(EVT_OUTPUT_STYLE_UPDATED,
                                        {"section": "auto"})
                # 策略反思完成事件（v3 §事件总线）：无论候选是否产出均广播
                await c.bus.publish(EVT_STRATEGY_REFLECTED,
                                    {"candidates_enqueued": strategy_count,
                                     "signal_count": len(rows)})
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---- 策略偏好候选入队（v3 §反馈闭环） ----------------------------------

    _STRATEGY_SCENES = {"chat", "opinion", "fact_query", "tech_help", "other"}
    _STRATEGY_PARAMS = {"depth", "form", "tone", "angle"}

    def _enqueue_strategy_candidates(self, candidates) -> int:
        """策略偏好候选入 profile_review_queue（review_type=strategy_preference）。

        双保险门槛：样本 < 3 或缺关键字段不入队（prompt 约束之外的代码兜底）；
        拒绝保护期内不重提同方向候选。
        """
        count = 0
        now_str = now_cst().isoformat(timespec="seconds")
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            scene = cand.get("scene") if cand.get(
                "scene") in self._STRATEGY_SCENES else "other"
            param = cand.get("param") if cand.get(
                "param") in self._STRATEGY_PARAMS else ""
            proposed = str(cand.get("proposed_content") or "").strip()
            title = str(cand.get("title") or "").strip()
            evidence = cand.get("evidence") or []
            if not proposed or not title or len(evidence) < 3:
                continue
            change_key = hashlib.md5(
                f"strategy:{scene}:{param}:{str(cand.get('direction', ''))[:80]}"
                .encode()).hexdigest()[:16]
            prot = self.db.query_one(
                "SELECT 1 FROM profile_review_rejections "
                "WHERE change_key=? AND protected_until>?", (change_key, now_str))
            if prot:
                continue
            dup = self.db.query_one(
                "SELECT 1 FROM profile_review_queue "
                "WHERE change_key=? AND status='pending'", (change_key,))
            if dup:
                continue
            self.db.execute(
                "INSERT INTO profile_review_queue"
                "(review_type,change_key,title,proposed_content,evidence,priority,"
                "status,created_at) "
                "VALUES('strategy_preference',?,?,?,?,3,'pending',?)",
                (change_key, title[:200], proposed[:2000],
                 json.dumps({"scene": scene, "param": param,
                             "direction": str(cand.get("direction", ""))[:80],
                             "items": evidence}, ensure_ascii=False)[:2000],
                 now_str))
            count += 1
        if count:
            logger.info("策略偏好候选入队 %d 条", count)
        return count


def _similarity(a: str, b: str) -> float:
    """基于 difflib 的文本 diff 相似度，用于演化频率控制：> 0.95 不建新版。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()
