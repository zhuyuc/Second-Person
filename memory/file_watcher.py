"""
FileWatcher 文件监控（产品文档 §文件 watcher 实时同步 / 开发文档 §6.6）。

三目录差异化处理：
- data/memories/（忽略 _archived/ _conflicts/ 与 _index.md）：走记忆索引重建路径
  1.5s 防抖 batch；frontmatter 校验；source=internal 跳过；删除事件置 missing
- data/soul/（SOUL_CORE/SOUL_STYLE）：注入扫描 → 使会话级快照失效 → 推通知
- data/profile/（user_profile.md）：仅重新解析
FileWriter 内部写入标记 source=internal，watcher 收到后跳过避免死循环。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("second_person.watcher")

DEBOUNCE_SECONDS = 1.5
# 内部写入抑制窗口：FileWriter/程序自身一次写盘常触发多个 watchdog 事件
# （created/modified 等事件簇），窗口内统一忽略，避免误报“外部修改”或重复处理
INTERNAL_SUPPRESS_SECONDS = 2.0


class FileWatcher:
    def __init__(self, data_dir, *, on_memory_change=None, on_soul_change=None,
                 on_profile_change=None):
        self.data_dir = Path(data_dir)
        # (changed_paths: list[Path]) -> None
        self.on_memory_change = on_memory_change
        self.on_soul_change = on_soul_change          # (path: Path) -> None
        self.on_profile_change = on_profile_change    # (path: Path) -> None
        self._observer = None
        # path -> 抑制截止时间（monotonic）：窗口内事件簇全部忽略
        self._internal_writes: dict[str, float] = {}
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._debounce_timer: threading.Timer | None = None
        self._soul_pending: set[str] = set()
        self._soul_timer: threading.Timer | None = None

    def mark_internal(self, path: str) -> None:
        """FileWriter 写入前标记：抑制窗口内该路径的 watcher 事件全部忽略。"""
        with self._lock:
            self._internal_writes[str(Path(path).resolve())] = (
                time.monotonic() + INTERNAL_SUPPRESS_SECONDS)

    def start(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog 未安装，文件监控禁用")
            return

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                watcher._dispatch(Path(event.src_path), event.event_type)

        self._observer = Observer()
        handler = _Handler()
        for sub in ("memories", "soul", "profile"):
            d = self.data_dir / sub
            if d.exists():
                self._observer.schedule(handler, str(d), recursive=True)
        self._observer.start()
        logger.info("FileWatcher 已启动")

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            if self._soul_timer:
                self._soul_timer.cancel()

    def _dispatch(self, path: Path, event_type: str) -> None:
        rp = str(path.resolve())
        with self._lock:
            exp = self._internal_writes.get(rp)
            if exp:
                if exp > time.monotonic():
                    return
                self._internal_writes.pop(rp, None)
        parts = path.parts
        if "soul" in parts:
            # 防抖合并：一次保存常触发多个 watchdog 事件（created/modified…），
            # 同路径短窗内只回调一次，避免重复通知/重复推送
            if path.name in ("SOUL_CORE.md", "SOUL_STYLE.md") and self.on_soul_change:
                self._enqueue_soul(rp)
            return
        if "profile" in parts:
            if self.on_profile_change:
                self.on_profile_change(path)
            return
        if "memories" in parts:
            if path.name == "_index.md" or "_conflicts" in parts or "_archived" in parts:
                return  # 忽略（_index.md 避免死循环；_archived 移动由 FileWriter 负责）
            self._enqueue_memory(rp)

    def _enqueue_soul(self, rp: str) -> None:
        with self._lock:
            self._soul_pending.add(rp)
            if self._soul_timer:
                self._soul_timer.cancel()
            self._soul_timer = threading.Timer(
                DEBOUNCE_SECONDS, self._flush_soul)
            self._soul_timer.start()

    def _flush_soul(self) -> None:
        with self._lock:
            paths = [Path(p) for p in self._soul_pending]
            self._soul_pending.clear()
        for p in paths:
            try:
                self.on_soul_change(p)
            except Exception:  # noqa: BLE001
                logger.exception("人格变更处理失败")

    def _enqueue_memory(self, rp: str) -> None:
        with self._lock:
            self._pending.add(rp)
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                DEBOUNCE_SECONDS, self._flush_memory)
            self._debounce_timer.start()

    def _flush_memory(self) -> None:
        with self._lock:
            paths = [Path(p) for p in self._pending]
            self._pending.clear()
        if paths and self.on_memory_change:
            try:
                self.on_memory_change(paths)
            except Exception:  # noqa: BLE001
                logger.exception("记忆变更处理失败")
