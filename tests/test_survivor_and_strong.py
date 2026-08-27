"""P3-A dedup 幸存者优先级 + P3-C auto_archive_stale 保护 strong。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.system_agents import LintAgent
from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.lifecycle import LifecycleManager
from memory.lint import LintEngine
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    loaded = True
    dim = 4
    def add(self, *a, **k): pass
    def remove(self, *a, **k): pass
    def top_similar(self, *a, **k): return []


def _mem(mid, confidence="medium", write_channel="system", important=False):
    return {
        "id": mid, "title": f"标题{mid}", "domain": "work",
        "confidence": confidence, "lifecycle": "active", "source_type": "memory",
        "access_count": 0, "created_at": "2026-08-19", "updated_at": "2026-08-19",
        "links": [], "entities": [], "created_by": "user_explicit",
        "verification_state": "direct", "freshness_state": "current",
        "write_channel": write_channel, "evidence_count": 1,
        "is_important": important,
    }


def test_survivor_prefers_explicit_over_early():
    async def scenario():
        tmp = Path(__file__).parent / "_tmp_survivor"
        tmp.mkdir(exist_ok=True)
        (tmp / "memories").mkdir(exist_ok=True)
        db = Database(tmp / "sp.db")
        try:
            db.run_migrations(ROOT / "migrations")
            palace = Palace(db)
            fw = FileWriter(db, palace, _FakeVS(), tmp)
            await fw.start()
            try:
                # a: 早，垃圾；b: 晚，explicit
                for mid, ch in (("mem_000001", "system"),
                                ("mem_000009", "explicit")):
                    await fw.submit("memory", {
                        "op": "create",
                        "frontmatter": _mem(mid, write_channel=ch),
                        "summary": "s", "detail": "d",
                        "reason": "t"}, wait=True)

                # 直接调用 _pick_survivor
                from memory.distiller import Distiller

                class _Stub:
                    pass
                stub = _Stub()
                stub.palace = palace  # 避免类体作用域捕获问题
                survivor, dup = Distiller._pick_survivor(
                    stub, "mem_000001", "mem_000009")
                assert survivor == "mem_000009"
                assert dup == "mem_000001"
            finally:
                await fw.stop(drain_timeout=5)
        finally:
            db.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    asyncio.run(scenario())


def test_auto_archive_skips_strong_and_important(tmp_path: Path):
    async def scenario():
        (tmp_path / "memories").mkdir(exist_ok=True)
        db = Database(tmp_path / "sp.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        fw = FileWriter(db, palace, _FakeVS(), tmp_path)
        lifecycle = LifecycleManager(db, palace, fw, tmp_path, {})
        lint = LintEngine(db, palace, _FakeVS(), {})
        agent = LintAgent(lint, lifecycle, skill_manager=None,
                          palace=palace, conflict_detector=None)
        await fw.start()
        try:
            for mid, conf, imp in (
                ("mem_000001", "strong", False),   # 保护
                ("mem_000002", "medium", True),    # 保护（important）
                ("mem_000003", "medium", False),   # 归档
            ):
                await fw.submit("memory", {
                    "op": "create",
                    "frontmatter": _mem(mid, confidence=conf, important=imp),
                    "summary": "s", "detail": "d",
                    "reason": "t"}, wait=True)
                # 手动置 stale + stale_lint_runs=2 触发归档判定
                db.execute("UPDATE memories SET lifecycle='stale',"
                           "stale_lint_runs=2 WHERE id=?", (mid,))
            n = await agent._auto_archive_stale()
            await fw.drain()
            assert n == 1  # 只归档 mem_000003
            assert palace.get("mem_000001")["lifecycle"] == "stale"
            assert palace.get("mem_000002")["lifecycle"] == "stale"
            assert palace.get("mem_000003")["lifecycle"] == "archived"
        finally:
            await fw.stop(drain_timeout=5)
            db.close()
    asyncio.run(scenario())
