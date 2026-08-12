"""FileWatcher 快照过滤冒烟：读取类事件不触发 / 真实修改触发 / 内部写入抑制。"""
import hashlib
import os
import pathlib
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from memory.file_watcher import FileWatcher  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "soul").mkdir()
style = tmp / "soul" / "SOUL_STYLE.md"
style.write_text("## 对话风格\n- 测试", encoding="utf-8")
(tmp / "memories").mkdir()
mem = tmp / "memories" / "x.md"
mem.write_text("hello", encoding="utf-8")

soul_events, mem_events = [], []


def on_soul(p):
    soul_events.append(str(p))


def on_mem(paths):
    mem_events.extend(str(p) for p in paths)


w = FileWatcher(tmp, on_soul_change=on_soul, on_memory_change=on_mem)

# 模拟 start() 的 soul 快照预建（真实场景由 watcher.start() 完成）
for _name in ("SOUL_CORE.md", "SOUL_STYLE.md"):
    _p = tmp / "soul" / _name
    if _p.is_file():
        w._snapshots[str(_p.resolve())] = hashlib.sha256(
            _p.read_bytes()).hexdigest()

# 场景 1：读取类事件（内容未变）→ 不触发
w._dispatch(style, "modified")
w._flush_soul()
assert soul_events == [], soul_events
print("[1] 读取类事件（内容未变）不触发 OK")

# 场景 2：真实外部修改（内容变化）→ 触发
style.write_text("## 对话风格\n- 已修改", encoding="utf-8")
w._dispatch(style, "modified")
w._flush_soul()
assert len(soul_events) == 1, soul_events
print("[2] 真实内容修改触发 OK")

# 场景 3：修改后再读取 → 不触发（快照已更新）
w._dispatch(style, "modified")
w._flush_soul()
assert len(soul_events) == 1, soul_events
print("[3] 修改后读取不再触发 OK")

# 场景 4：内部写入抑制（mark_internal 后写盘 + 立即事件）→ 不触发
w.mark_internal(style)
style.write_text("## 对话风格\n- 内部写入", encoding="utf-8")
w._dispatch(style, "modified")
w._flush_soul()
assert len(soul_events) == 1, soul_events
print("[4] 内部写入抑制 OK")

# 场景 5：data/memories/soul 领域目录事件不再劫持为灵魂事件
(tmp / "memories" / "soul").mkdir()
m2 = tmp / "memories" / "soul" / "mem_000001_test.md"
m2.write_text("领域记忆", encoding="utf-8")
w._dispatch(m2, "created")
w._flush_soul()
assert len(soul_events) == 1, soul_events  # 未被劫持
w._flush_memory()
assert any("mem_000001" in p for p in mem_events), mem_events
print("[5] memories/soul 领域目录正确走记忆分支 OK")

# 场景 6：记忆首次事件（无快照）建档触发一次，后续读取类事件不触发
w._dispatch(mem, "modified")
w._flush_memory()
assert len(mem_events) == 2, mem_events
w._dispatch(mem, "modified")
w._flush_memory()
assert len(mem_events) == 2, mem_events
print("[6] 记忆首次建档一次、读取类事件不触发 OK")

# 场景 7：记忆真实修改触发
mem.write_text("hello world", encoding="utf-8")
w._dispatch(mem, "modified")
w._flush_memory()
assert len(mem_events) == 3, mem_events
print("[7] 记忆真实修改触发 OK")

# 场景 8：记忆删除必须触发（置 missing）
mem.unlink()
w._dispatch(mem, "deleted")
w._flush_memory()
assert len(mem_events) == 4, mem_events
print("[8] 记忆删除触发 OK")

print("\n=== FileWatcher 快照过滤冒烟全部通过 ===")
