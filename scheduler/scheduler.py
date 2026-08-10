"""
定时任务调度器（产品文档 §定时任务框架 / 开发文档 §3.10）。

链式触发：只有链首配置触发时刻（Asia/Shanghai），后续由前驱完成事件驱动。
- 夜间维护链（每天 02:00）：备份 → dedup 清理 → 临时附件清理 → 日志清理
  → 已解决矛盾清理 → failed 写入重扫
- 记忆维护链（每 passive_review_interval_days 天 03:00，04:00 兜底检查）：
  被动回顾 → Lint（含技能提炼归档）→ 画像重建
- 独立任务：输出样式画像提炼（不入链）
链上任务失败重试 2 次后整链中断，后续任务跳过并记录。
手动触发写 task_logs（trigger_source=manual），不改下次定时时间。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.scheduler")
TASK_RETRY = 2


class TaskScheduler:
    def __init__(self, db, config, notifier=None):
        self.db = db
        self.config = config
        self.notify = notifier or (lambda t, m: None)
        self._tasks: dict[str, dict] = {}   # task_id -> {name, fn, schedule}
        self._chains: dict[str, list[str]] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    def register_task(self, task_id: str, name: str, fn, schedule: str = "") -> None:
        self._tasks[task_id] = {"name": name, "fn": fn, "schedule": schedule}
        self.db.execute(
            "INSERT OR REPLACE INTO scheduled_tasks(task_id,name,schedule,status,"
            "last_run,next_run) VALUES(?,?,?,COALESCE((SELECT status FROM scheduled_tasks "
            "WHERE task_id=?),'pending'),(SELECT last_run FROM scheduled_tasks WHERE task_id=?),?)",
            (task_id, name, schedule, task_id, task_id, ""))

    def register_chain(self, chain_id: str, task_ids: list[str]) -> None:
        self._chains[chain_id] = task_ids

    # ---- 执行 -------------------------------------------------------------
    async def run_task(self, task_id: str, trigger_source: str = "schedule") -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        start = now_cst()
        for attempt in range(TASK_RETRY + 1):
            try:
                # 同步任务丢线程池执行，避免备份/布局重算等重操作阻塞事件循环
                # （对话 SSE 与所有接口共享同一循环）；协程任务维持 await。
                result = await asyncio.to_thread(task["fn"])
                if asyncio.iscoroutine(result):
                    await result
                self._log(task_id, start, "success", None, trigger_source)
                self.db.execute(
                    "UPDATE scheduled_tasks SET status='completed', last_run=? WHERE task_id=?",
                    (start.isoformat(timespec="seconds"), task_id))
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("任务 %s 失败(第 %d 次)：%s", task_id, attempt + 1, e)
                if attempt >= TASK_RETRY:
                    self._log(task_id, start, "failed", str(e), trigger_source)
                    self.db.execute(
                        "UPDATE scheduled_tasks SET status='failed', last_run=? WHERE task_id=?",
                        (start.isoformat(timespec="seconds"), task_id))
                    self.notify("task_failed", f"定时任务 {task['name']} 失败：{e}")
                    return False
                await asyncio.sleep(1)
        return False

    async def run_chain(self, chain_id: str, trigger_source: str = "schedule") -> None:
        for task_id in self._chains.get(chain_id, []):
            ok = await self.run_task(task_id, trigger_source)
            if not ok:
                # 整链中断，后续跳过
                for skipped in self._chains[chain_id][self._chains[chain_id].index(task_id) + 1:]:
                    self._log(skipped, now_cst(), "skipped",
                              f"因前驱 {task_id} 失败而跳过", trigger_source)
                break

    def _log(self, task_id, start, result, fail_reason, trigger_source) -> None:
        dur = int((now_cst() - start).total_seconds() * 1000)
        self.db.execute(
            "INSERT INTO task_logs(task_id,run_time,duration_ms,result,fail_reason,"
            "trigger_source) VALUES(?,?,?,?,?,?)",
            (task_id, start.isoformat(timespec="seconds"), dur, result, fail_reason,
             trigger_source))

    # ---- 调度循环 ---------------------------------------------------------
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            now = now_cst()
            try:
                # 夜间维护链 02:00（用分钟窗口<5 而非==0：60s 循环漂移不至跳过整点，
                # 由 _ran_today 标记保证当日只跑一次）
                if now.hour == 2 and now.minute < 5:
                    if not self._ran_today("night_chain_marker"):
                        await self.run_chain("night_maintenance")
                        self._mark_ran("night_chain_marker")
                # 记忆维护链 03:00（按间隔天数）
                if now.hour == 3 and now.minute < 5:
                    interval = self.config.get(
                        "passive_review_interval_days", 3)
                    if self._should_run_memory_chain(interval) \
                            and not self._ran_today("memory_chain_marker"):
                        await self.run_chain("memory_maintenance")
                        self._mark_ran("memory_chain_marker")
                # 输出样式画像提炼 03:30（独立任务，build 内部按 should_build 自门控）
                if now.hour == 3 and 30 <= now.minute < 35:
                    if not self._ran_today("output_style_marker"):
                        await self.run_task("output_style_build")
                        self._mark_ran("output_style_marker")
                # 本地目录扫描（按间隔小时自门控：复用任务 last_run 记录，
                # 失败也推进 last_run，避免窗口内每分钟重复触发整轮重扫）
                if now.minute < 5:
                    interval = self.config.get(
                        "local_dir_scan_interval_hours", 24)
                    due = True
                    row = self.db.query_one(
                        "SELECT last_run FROM scheduled_tasks "
                        "WHERE task_id='local_dir_scan'")
                    if row and row["last_run"]:
                        try:
                            due = (now - datetime.fromisoformat(
                                row["last_run"])) >= timedelta(hours=interval)
                        except ValueError:
                            due = True
                    if due:
                        ok = await self.run_task("local_dir_scan")
                        if not ok:
                            self.db.execute(
                                "UPDATE scheduled_tasks SET last_run=? "
                                "WHERE task_id='local_dir_scan'",
                                (now.isoformat(timespec="seconds"),))
                # 04:00 兜底检查：若记忆链应跑但未跑成（链首未启动/失败），写日志不补跑
                if now.hour == 4 and now.minute < 5:
                    if not self._ran_today("fallback_check_marker"):
                        interval = self.config.get(
                            "passive_review_interval_days", 3)
                        if self._should_run_memory_chain(interval) \
                                and not self._ran_today("memory_chain_marker"):
                            self._log("memory_maintenance", now_cst(),
                                      "skipped", "03:00 链首未启动/失败，本轮整链跳过",
                                      "schedule")
                        self._mark_ran("fallback_check_marker")
                # 画像审核队列维护 04:30（清理过期 + 通知）
                if now.hour == 4 and 30 <= now.minute < 35:
                    if not self._ran_today("profile_review_scan_marker"):
                        await self.run_task("profile_review_scan")
                        self._mark_ran("profile_review_scan_marker")
            except Exception:  # noqa: BLE001
                logger.exception("调度循环异常")
            await asyncio.sleep(60)

    def _ran_today(self, marker: str) -> bool:
        row = self.db.query_one(
            "SELECT last_run FROM scheduled_tasks WHERE task_id=?", (marker,))
        if not row or not row["last_run"]:
            return False
        try:
            return datetime.fromisoformat(row["last_run"]).date() == now_cst().date()
        except ValueError:
            return False

    def _mark_ran(self, marker: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO scheduled_tasks(task_id,name,schedule,status,last_run,next_run) "
            "VALUES(?,?,?,'completed',?,'')",
            (marker, marker, "", now_cst().isoformat(timespec="seconds")))

    def _should_run_memory_chain(self, interval_days: int) -> bool:
        row = self.db.query_one(
            "SELECT last_run FROM scheduled_tasks WHERE task_id='memory_chain_marker'")
        if not row or not row["last_run"]:
            return True
        try:
            last = datetime.fromisoformat(row["last_run"])
            return (now_cst() - last) >= timedelta(days=interval_days)
        except ValueError:
            return True

    # ---- 列表 / 日志 ------------------------------------------------------
    def list_tasks(self) -> list[dict]:
        rows = self.db.query_all(
            "SELECT * FROM scheduled_tasks WHERE task_id NOT LIKE '%_marker' "
            "AND task_id != 'output_style_last_built' ORDER BY task_id")
        return [dict(r) for r in rows]

    def task_logs(self, task_id: str) -> list[dict]:
        rows = self.db.query_all(
            "SELECT run_time,duration_ms,result,fail_reason FROM task_logs "
            "WHERE task_id=? ORDER BY run_time DESC LIMIT 100", (task_id,))
        return [dict(r) for r in rows]

    def purge_old_logs(self) -> int:
        cutoff = (now_cst() - timedelta(days=30)
                  ).isoformat(timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM task_logs WHERE run_time < ?", (cutoff,))
        return cur.rowcount if cur else 0
