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

import asyncio
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
    def __init__(self, db, distiller, config, data_dir=None):
        self.db = db
        self.distiller = distiller
        self.config = config
        from pathlib import Path
        self.data_dir = Path(data_dir) if data_dir else None

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
                fb = ""
                if r["feedback"] == 2:
                    fb = "[用户点踩此回复，内容可能有误，提炼时勿采信] "
                elif r["feedback"] == 1:
                    fb = "[用户点赞此回复] "
                by_day.setdefault(day, []).append(
                    f"{r['role']}: {fb}{r['content']}")
            for day, lines in by_day.items():
                text = "\n".join(lines)
                for sub in self._split_if_oversized(text):
                    try:
                        written = await self.distiller.distill(sub, source_type="memory")
                        total += len(written)
                    except Exception:  # noqa: BLE001 - 单块失败不拖垮整任务/整链
                        logger.warning("回顾提炼失败（%s 分块），跳过继续",
                                       day, exc_info=True)
        # 2) 会话压缩摘要（session_fact 跨会话复现升级：摘要中反复出现的事实经去重合并）
        total += await self._distill_session_summaries(cutoff)
        # 3) 新导入文档（回顾链补提炼，依赖去重避免与 Ingest 重复）
        total += await self._distill_recent_docs(cutoff)
        # 4) 主动记忆检测候选：窗口外的单独补提炼（窗口内已被第 1 步覆盖）
        total += await self._distill_review_candidates(cutoff)
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
        """消费 review_candidates：第 8 步标记的含新事实消息优先提炼后清表。"""
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
                written = await self.distiller.distill(
                    f"user: {r['content']}", source_type="memory")
                total += len(written)
            except Exception:  # noqa: BLE001
                logger.warning("回顾候选提炼失败：msg=%s",
                               r["message_id"], exc_info=True)
        self.db.execute("DELETE FROM review_candidates")
        return total

    async def _distill_session_summaries(self, cutoff: str) -> int:
        if not self.data_dir:
            return 0
        rows = self.db.query_all(
            "SELECT compressed_summary_path FROM sessions "
            "WHERE last_active>=? AND compressed_summary_path IS NOT NULL", (cutoff,))
        total = 0
        from memory.md_file import split_frontmatter
        for r in rows:
            p = self.data_dir / r["compressed_summary_path"]
            if not p.exists():
                continue
            try:
                _, body = split_frontmatter(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if body.strip():
                try:
                    written = await self.distiller.distill(body, source_type="memory")
                    total += len(written)
                except Exception:  # noqa: BLE001 - 单个摘要失败不拖垮整任务
                    logger.warning("会话摘要提炼失败：%s，跳过继续",
                                   r["compressed_summary_path"], exc_info=True)
        return total

    async def _distill_recent_docs(self, cutoff: str) -> int:
        if not self.data_dir:
            return 0
        rows = self.db.query_all(
            "SELECT file_path, extracted_text FROM raw_docs WHERE imported_at>=? "
            "AND review_status IS NULL", (cutoff,))
        if not rows:
            return 0
        from pathlib import Path as _P
        from scheduler.ingest import extract_text
        total = 0
        for r in rows:
            # 优先复用导入时缓存的解析文本（图片 VLM/OCR 结果即在此），避免重复视觉调用；
            # 无缓存再回退同步 extract_text（图片无缓存则得空，跳过）
            text = r["extracted_text"] or ""
            if not text:
                fp = _P(r["file_path"])
                if not fp.exists():
                    continue
                try:
                    text = extract_text(fp)
                except Exception:  # noqa: BLE001
                    continue
            for sub in self._split_if_oversized(text):
                if sub.strip():
                    try:
                        written = await self.distiller.distill(sub, source_type="knowledge")
                        total += len(written)
                    except Exception:  # noqa: BLE001 - 单文档分块失败不拖垮整任务
                        logger.warning("文档补提炼失败：%s，跳过继续",
                                       r["file_path"], exc_info=True)
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
        # 过期检测 → 标 stale
        for mid in self.lifecycle.detect_stale_candidates():
            await self.lifecycle.mark_stale(mid)
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
                "drift_fixed": drift_fixed}

    async def _auto_archive_stale(self) -> int:
        """stale_lint_runs 计数：每个 Lint 周期对处于 stale 的记忆 +1，
        达 2（连续两周期未被恢复）即自动归档；恢复时由 lifecycle 清零。"""
        db = self.lifecycle.db
        db.execute(
            "UPDATE memories SET stale_lint_runs=COALESCE(stale_lint_runs,0)+1 "
            "WHERE lifecycle='stale'")
        rows = db.query_all(
            "SELECT id FROM memories WHERE lifecycle='stale' AND stale_lint_runs>=2")
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
        snap = self.snapshot_fn()
        if snap is None:
            return False
        rows = self.db.query_all(
            "SELECT title,summary,domain FROM memories "
            "WHERE lifecycle IN ('active','stable') ORDER BY access_count DESC LIMIT 100")
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
        fm_head = (f"---\nlast_rebuilt: {now_cst().isoformat(timespec='seconds')}\n"
                   f"source_memory_count: {len(rows)}\n---\n")
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
        interval = self.config.get("output_style_review_interval_days", 7)
        if (now_cst() - last_dt) >= timedelta(days=interval):
            return True
        batch = self.config.get("output_style_signal_batch_threshold", 100)
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
        window = self.config.get("output_style_signal_window_days", 30)
        cutoff = (now_cst() - timedelta(days=window)
                  ).isoformat(timespec="seconds")
        rows = self.db.query_all(
            "SELECT char_count,bullet_count,table_count,conclusion_position,"
            "explicit_reaction,explicit_keywords FROM response_signals "
            "WHERE create_time>=?", (cutoff,))
        if not rows:
            return False
        # 分箱统计
        liked = [r for r in rows if r["explicit_reaction"] == 1]
        disliked = [r for r in rows if r["explicit_reaction"] == 2]
        avg_like_len = sum(r["char_count"]
                           for r in liked) / len(liked) if liked else 0
        keywords = ";".join(r["explicit_keywords"]
                            for r in rows if r["explicit_keywords"])
        stat = (f"样本 {len(rows)} 条；点赞平均字数 {avg_like_len:.0f}；"
                f"点赞 {len(liked)} 踩 {len(disliked)}；偏好关键词：{keywords or '无'}")
        try:
            resp = await self.llm.chat(
                snap, [{"role": "system", "content": OUTPUT_STYLE_PROMPT},
                       {"role": "user", "content": stat}], source="system_agent",
                session_id=session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("输出画像提炼失败：%s", e)
            return False
        new_text = resp["content"].strip()
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
                from infrastructure.event_bus import EVT_OUTPUT_STYLE_UPDATED
                await c.bus.publish(EVT_OUTPUT_STYLE_UPDATED,
                                    {"section": "auto"})
        except Exception:  # noqa: BLE001
            pass
        return True


def _similarity(a: str, b: str) -> float:
    """基于 difflib 的文本 diff 相似度，用于演化频率控制：> 0.95 不建新版。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()
