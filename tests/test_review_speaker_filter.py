"""回顾 Agent 说话人硬分离契约：assistant 侧内容不进提炼语料。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.system_agents import ReviewAgent
from infrastructure.db import Database

ROOT = Path(__file__).resolve().parent.parent


class _FakeDistiller:
    def __init__(self):
        self.calls: list[str] = []

    async def distill(self, text: str, source_type: str = "memory"):
        self.calls.append(text)
        return []


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_only_user_content_flows_to_distiller(tmp_path: Path):
    db = Database(tmp_path / "sp.db")
    db.run_migrations(ROOT / "migrations")
    # 灌两条对话：user 说了个偏好，assistant 猜了个身份
    from infrastructure.timeutil import now_cst
    now = now_cst().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO conversations(session_id,role,content,create_time,message_type,feedback) "
        "VALUES('s1','user','我偏好每天早上跑步',?, 'normal',0)", (now,))
    db.execute(
        "INSERT INTO conversations(session_id,role,content,create_time,message_type,feedback) "
        "VALUES('s1','assistant','听起来你是自律的产品经理',?, 'normal',0)", (now,))

    fake = _FakeDistiller()
    agent = ReviewAgent(db, fake, _Cfg(passive_review_interval_days=30),
                        data_dir=tmp_path, memory_gate=None)
    asyncio.run(agent.run())
    joined = "\n".join(fake.calls)
    assert "我偏好每天早上跑步" in joined
    assert "自律的产品经理" not in joined, (
        "assistant 推断不应回喂给提炼器，产生自我强化环")
    db.close()
