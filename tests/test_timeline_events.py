"""时间线语义事件契约测试（对话零阻塞架构无关，纯 FileWriter 层集成）。

保护契约：FileWriter 写入时间线时，payload.timeline_event 语义值
（evolved/merged/imported）必须落库，缺省时按 op 二分 created/updated。
运行：python tests/test_timeline_events.py（退出码 0 = 全部通过）
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.db import Database  # noqa: E402
from memory.file_writer import FileWriter  # noqa: E402
from memory.palace import Palace  # noqa: E402


class _FakeVS:
    def add(self, *a, **kw):
        pass

    def remove(self, *a, **kw):
        pass


def _fm(mid: str) -> dict:
    return {"id": mid, "title": "测试记忆", "domain": "general",
            "confidence": "medium", "lifecycle": "active",
            "source_type": "memory", "access_count": 0,
            "created_at": "2026-01-01", "updated_at": "2026-01-01",
            "links": [], "entities": [], "created_by": "test"}


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sp_tl_test_"))
    (tmp / "memories").mkdir(parents=True)
    db = Database(tmp / "test.db")
    db.run_migrations(Path(__file__).parent.parent / "migrations")
    palace = Palace(db)
    fw = FileWriter(db, palace, _FakeVS(), tmp)
    await fw.start()

    # 用例：<memory_id, op, timeline_event, 期望落库事件>
    cases = [
        ("mem-t1", "create", None, "created"),
        ("mem-t2", "create", "imported", "imported"),
        ("mem-t1", "update", None, "updated"),
        ("mem-t1", "update", "evolved", "evolved"),
        ("mem-t1", "update", "merged", "merged"),
    ]
    failed = []
    for mid, op, evt, expect in cases:
        payload = {"op": op, "memory_id": mid, "frontmatter": _fm(mid),
                   "summary": "摘要", "detail": f"详情 {op} {evt}",
                   "reason": f"test-{expect}"}
        if evt:
            payload["timeline_event"] = evt
        await fw.submit("memory", payload, wait=True)
        row = db.query_one(
            "SELECT 1 FROM memory_timeline WHERE memory_id=? AND event_type=? "
            "AND detail=?", (mid, expect, f"test-{expect}"))
        status = "PASS" if row else "FAIL"
        if not row:
            failed.append((op, evt, expect))
        print(f"[{status}] op={op} timeline_event={evt} → 期望 {expect}")

    await fw.stop(drain_timeout=5)
    db.close()
    if failed:
        print(f"FAILED: {failed}")
        return 1
    print("时间线语义事件契约：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
