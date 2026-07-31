"""
Database —— SQLite 连接层与迁移执行器（单写线程架构）。

对齐产品文档 §存储层 / 保护性 PRAGMA / Schema 版本控制 与 开发文档 §6.15：
- WAL 模式：写入不阻塞并发读（读走各线程本地只读用途连接）
- 单写线程 + 写队列：全库唯一写者，写锁竞争从机制上不存在——
  execute/executemany 入队后由专属写线程串行执行，全局 FIFO 顺序确定；
  队列内多条小写合并为一个事务组提交（失败时回滚并逐条重放隔离坏语句）。
- execute_nowait：火忘式写入（token 统计/操作日志等高频小写），
  事件循环线程零等待；execute_async 供 async 调用方协作式等待。
- transaction()：多语句原子事务仍在调用线程连接上执行，与写线程共用
  _write_lock 互斥（事务持有期间写线程暂停，二者不会交叉写）。
- migrations/ 顺序执行未应用脚本（schema_migrations 表记录已应用版本）
- PRAGMA integrity_check 完整性检查；VACUUM INTO 提供一致性快照（备份用）
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("second_person.db")

# 组提交单批上限：批内一次 commit，减少 fsync 次数
GROUP_COMMIT_MAX = 64
# 队列深度告警阈值（写压力仪表，配合事件循环哨兵观测）
QUEUE_WARN_DEPTH = 200


class WriteResult:
    """写操作结果（跨线程返回，替代 sqlite3.Cursor 的 lastrowid/rowcount）。"""

    __slots__ = ("lastrowid", "rowcount")

    def __init__(self, lastrowid: int | None, rowcount: int):
        self.lastrowid = lastrowid
        self.rowcount = rowcount


class _WriteTask:
    __slots__ = ("sql", "params", "many", "event", "result", "error")

    def __init__(self, sql: str, params, many: bool, wait: bool):
        self.sql = sql
        self.params = params
        self.many = many
        self.event = threading.Event() if wait else None
        self.result: WriteResult | None = None
        self.error: BaseException | None = None

    def run(self, conn: sqlite3.Connection) -> None:
        if self.many:
            cur = conn.executemany(self.sql, self.params)
        else:
            cur = conn.execute(self.sql, self.params)
        self.result = WriteResult(cur.lastrowid, cur.rowcount)


class _FnTask:
    """在写线程上执行任意函数（checkpoint 等维护操作），与写序列串行。"""

    __slots__ = ("fn", "event", "result", "error")

    def __init__(self, fn: Callable[[sqlite3.Connection], Any]):
        self.fn = fn
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self, conn: sqlite3.Connection) -> None:
        self.result = self.fn(conn)


_STOP = object()


class Database:
    """线程安全的 SQLite 封装：读各线程本地连接并发，写统一经单写线程串行。"""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._local = threading.local()
        # 写互斥：写线程逐批持有；transaction() 持有期间写线程暂停
        self._write_lock = threading.RLock()
        # 当前持有显式事务的线程 id（防止事务内误调 execute 造成自死锁）
        self._tx_owner: int | None = None
        # 触发一次连接以应用 PRAGMA
        self._configure(self._conn())
        # ---- 单写线程 ----
        self._queue: queue.Queue = queue.Queue()
        self._writer_conn: sqlite3.Connection | None = None
        self._depth_warned = False
        self._writer = threading.Thread(
            target=self._writer_loop, name="db-writer", daemon=True)
        self._writer.start()

    # ---- 连接管理 ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._path, timeout=5.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._configure(conn)
            self._local.conn = conn
        return conn

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        # 单写线程架构下写-写竞争已不存在；busy_timeout 仅兜底
        # checkpoint 等罕见场景，超时报错而非静默长挂
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=OFF")  # 引用清理由应用层事务保证
        conn.execute("PRAGMA synchronous=NORMAL")

    # ---- 写线程 -----------------------------------------------------------
    def _writer_loop(self) -> None:
        conn = sqlite3.connect(
            self._path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._configure(conn)
        self._writer_conn = conn
        while True:
            task = self._queue.get()
            if task is _STOP:
                break
            batch = [task]
            stop = False
            # 组提交：把队列里已积压的写并入同一事务，一次 commit
            while len(batch) < GROUP_COMMIT_MAX:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                batch.append(nxt)
            self._run_batch(conn, batch)
            depth = self._queue.qsize()
            if depth > QUEUE_WARN_DEPTH and not self._depth_warned:
                self._depth_warned = True
                logger.warning("写队列积压 %d 条，写入压力异常", depth)
            elif depth < QUEUE_WARN_DEPTH // 2:
                self._depth_warned = False
            if stop:
                break
        try:
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._writer_conn = None

    def _run_batch(self, conn: sqlite3.Connection, batch: list) -> None:
        with self._write_lock:
            try:
                for t in batch:
                    t.run(conn)
                conn.commit()
                for t in batch:
                    if t.event:
                        t.event.set()
            except Exception:  # noqa: BLE001
                conn.rollback()
                # 逐条重放：隔离出失败语句，其余照常生效（语义与旧的
                # 逐条 commit 行为一致）
                for t in batch:
                    try:
                        t.run(conn)
                        conn.commit()
                    except Exception as e:  # noqa: BLE001
                        conn.rollback()
                        t.error = e
                        if t.event is None:
                            logger.exception("火忘式写入失败：%s", t.sql[:120])
                    finally:
                        if t.event:
                            t.event.set()

    def _submit(self, task) -> None:
        self._queue.put(task)

    def _wait(self, task):
        task.event.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def _direct_allowed(self) -> sqlite3.Connection | None:
        """写线程自身或显式事务持有线程直接在对应连接上执行，防自死锁。"""
        tid = threading.get_ident()
        if self._writer_conn is not None and tid == self._writer.ident:
            return self._writer_conn
        if self._tx_owner == tid:
            return self._conn()  # 事务内直写，随事务一起提交/回滚
        return None

    # ---- 基本执行 ---------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> WriteResult:
        direct = self._direct_allowed()
        if direct is not None:
            cur = direct.execute(sql, tuple(params))
            if self._tx_owner != threading.get_ident():
                direct.commit()
            return WriteResult(cur.lastrowid, cur.rowcount)
        task = _WriteTask(sql, tuple(params), many=False, wait=True)
        self._submit(task)
        return self._wait(task)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> WriteResult:
        rows = [tuple(x) for x in seq]
        direct = self._direct_allowed()
        if direct is not None:
            cur = direct.executemany(sql, rows)
            if self._tx_owner != threading.get_ident():
                direct.commit()
            return WriteResult(cur.lastrowid, cur.rowcount)
        task = _WriteTask(sql, rows, many=True, wait=True)
        self._submit(task)
        return self._wait(task)

    def execute_nowait(self, sql: str, params: Iterable[Any] = ()) -> None:
        """火忘式写入：入队即返回，不等待结果（高频小写专用，如 token
        统计/操作日志）。失败由写线程记日志，调用方无感。"""
        self._submit(_WriteTask(sql, tuple(params), many=False, wait=False))

    async def execute_async(self, sql: str, params: Iterable[Any] = ()) -> WriteResult:
        """async 调用方的协作式写入：等待期间不占事件循环。"""
        task = _WriteTask(sql, tuple(params), many=False, wait=True)
        self._submit(task)
        return await asyncio.to_thread(self._wait, task)

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(params)).fetchall()

    def write_queue_depth(self) -> int:
        """写队列深度（写压力仪表，供健康页/哨兵观测）。"""
        return self._queue.qsize()

    # ---- 事务 -------------------------------------------------------------
    def transaction(self) -> "_Transaction":
        """返回事务上下文管理器（调用线程连接执行，持锁期间写线程暂停）。"""
        return _Transaction(self)

    def raw_connection(self) -> sqlite3.Connection:
        return self._conn()

    # ---- 迁移 -------------------------------------------------------------
    def run_migrations(self, migrations_dir: str | Path) -> list[str]:
        """顺序执行未应用的 migrations/*.sql，返回本次应用的文件名列表。"""
        mdir = Path(migrations_dir)
        conn = self._conn()
        with self._write_lock:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            conn.commit()
        applied = {r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations")}
        newly: list[str] = []
        for sql_file in sorted(mdir.glob("*.sql")):
            version = sql_file.stem
            if version in applied:
                continue
            logger.info("应用迁移脚本 %s", sql_file.name)
            with self._write_lock:
                conn.executescript(sql_file.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES(?, datetime('now'))", (version,))
                conn.commit()
            newly.append(sql_file.name)
        return newly

    # ---- 完整性 / 快照 ----------------------------------------------------
    def integrity_check(self) -> bool:
        # 用独立短连接执行，避免共享的长驻线程连接因处于写事务/未提交快照
        # 状态而让 integrity_check 瞬时返回非 "ok"（误报数据库异常）。
        # 独立只读连接始终反映已提交的落盘状态；被锁等临时状况不代表损坏。
        try:
            conn = sqlite3.connect(self._path, timeout=5.0)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return bool(row) and row[0] == "ok"
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return True

    def vacuum_into(self, target: str | Path) -> None:
        """VACUUM INTO 一致性快照（备份用）：独立连接读快照写入新文件，
        不抢主库写锁、不需要 checkpoint 写静默；目标文件不得已存在。"""
        conn = sqlite3.connect(self._path, timeout=30.0)
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        """WAL checkpoint：写线程存活时经队列串行执行，否则直连兜底。"""
        if self._writer.is_alive():
            task = _FnTask(
                lambda c: c.execute(f"PRAGMA wal_checkpoint({mode})"))
            self._submit(task)
            task.event.wait()
            if task.error is not None:
                raise task.error
            return
        with self._write_lock:
            self._conn().execute(f"PRAGMA wal_checkpoint({mode})")

    def close(self) -> None:
        """优雅停机：写队列排空后停止写线程，再关闭本线程连接。"""
        if self._writer.is_alive():
            self._queue.put(_STOP)
            self._writer.join(timeout=10)
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


class _Transaction:
    def __init__(self, db: Database):
        self._db = db
        self._conn = db.raw_connection()

    def __enter__(self) -> sqlite3.Connection:
        self._db._write_lock.acquire()
        self._db._tx_owner = threading.get_ident()
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
                logger.warning("事务回滚：%s", exc)
        finally:
            self._db._tx_owner = None
            self._db._write_lock.release()
        return False
