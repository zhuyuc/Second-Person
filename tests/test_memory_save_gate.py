"""P1-B: memory_save 显式路径最小闸口。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.palace import Palace
from memory.write_gate import MemoryWriteGate
from tools.base import ToolRegistry
from tools.builtin import register_builtins
from tools.sandbox import Sandbox

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    def add(self, *a, **k): pass
    def remove(self, *a, **k): pass


class _FakeRetriever:
    embed_fn = None
    async def hybrid_presearch(self, *a, **k):
        class _R: candidates = []
        return _R()


def _wire(tmp_path):
    db = Database(tmp_path / "sp.db")
    db.run_migrations(ROOT / "migrations")
    (tmp_path / "memories").mkdir(exist_ok=True)
    palace = Palace(db)
    fw = FileWriter(db, palace, _FakeVS(), tmp_path)
    gate = MemoryWriteGate(db, {"memory_write_strictness": "loose"})
    sandbox = Sandbox(tmp_path)
    registry = ToolRegistry()
    register_builtins(
        registry, palace=palace, retriever=_FakeRetriever(), file_writer=fw,
        sandbox=sandbox, data_dir=tmp_path,
        config={"memory_write_strictness": "loose"}, memory_gate=gate)
    return db, fw, registry


def test_memory_save_rejects_empty_title(tmp_path):
    async def scenario():
        db, fw, reg = _wire(tmp_path)
        await fw.start()
        try:
            tool = reg.get("memory_save")
            with pytest.raises(ValueError, match="不能为空"):
                await tool.run(title="", summary="有内容", detail="有内容",
                                domain="work")
        finally:
            await fw.stop(drain_timeout=5)
            db.close()
    asyncio.run(scenario())


def test_memory_save_rejects_too_short(tmp_path):
    async def scenario():
        db, fw, reg = _wire(tmp_path)
        await fw.start()
        try:
            tool = reg.get("memory_save")
            with pytest.raises(ValueError, match="过短"):
                await tool.run(title="标题", summary="嗯", detail="是",
                                domain="work")
        finally:
            await fw.stop(drain_timeout=5)
            db.close()
    asyncio.run(scenario())
