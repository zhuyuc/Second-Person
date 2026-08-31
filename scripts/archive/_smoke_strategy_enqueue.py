"""阶段 3 冒烟：OutputStyleBuilder 策略候选入队双保险门槛。"""
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from agent.system_agents import OutputStyleBuilder  # noqa: E402


class FakeDB:
    def __init__(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
CREATE TABLE profile_review_queue(id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_type TEXT, change_key TEXT, title TEXT, proposed_content TEXT,
  evidence TEXT, priority INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
CREATE TABLE profile_review_rejections(id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_type TEXT, change_key TEXT UNIQUE, proposed_content_summary TEXT,
  rejected_at TEXT, protected_until TEXT);
""")

    def query_one(self, sql, params=()):
        return self.con.execute(sql, params).fetchone()

    def query_all(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        cur = self.con.execute(sql, params)
        self.con.commit()
        return cur


b = OutputStyleBuilder.__new__(OutputStyleBuilder)
b.db = FakeDB()
ev3 = [{"message_id": i, "reaction": "like"} for i in range(1, 4)]

# TC1: 样本不足（evidence<3）不入队
n = b._enqueue_strategy_candidates([
    {"title": "t", "scene": "opinion", "param": "depth", "direction": "更深",
     "proposed_content": "opinion 场景 depth=3", "evidence": ev3[:2]}])
assert n == 0, "TC1 fail"

# TC2: 样本足够入队
n = b._enqueue_strategy_candidates([
    {"title": "观点场景偏好深度", "scene": "opinion", "param": "depth",
     "direction": "更深", "proposed_content": "opinion 场景默认 depth=3",
     "evidence": ev3}])
assert n == 1, "TC2 fail"
row = b.db.query_one(
    "SELECT * FROM profile_review_queue WHERE review_type='strategy_preference'")
assert row and row["status"] == "pending", "TC2b fail"
assert row["title"] == "观点场景偏好深度"

# TC3: pending 重复候选去重
n = b._enqueue_strategy_candidates([
    {"title": "观点场景偏好深度", "scene": "opinion", "param": "depth",
     "direction": "更深", "proposed_content": "x", "evidence": ev3}])
assert n == 0, "TC3 fail"

# TC4: 拒绝保护期内不重提
b.db.execute(
    "INSERT INTO profile_review_rejections(review_type,change_key,"
    "rejected_at,protected_until) VALUES('strategy_preference',?,"
    "datetime('now'),'2099-01-01T00:00:00')", (row["change_key"],))
b.db.execute("UPDATE profile_review_queue SET status='rejected' WHERE change_key=?",
             (row["change_key"],))
n = b._enqueue_strategy_candidates([
    {"title": "观点场景偏好深度", "scene": "opinion", "param": "depth",
     "direction": "更深", "proposed_content": "y", "evidence": ev3}])
assert n == 0, "TC4 fail"

# TC5: 非法枚举归一化（scene 非法→other，param 非法→空串）
n = b._enqueue_strategy_candidates([
    {"title": "x", "scene": "invalid_scene", "param": "weird",
     "direction": "d", "proposed_content": "p", "evidence": ev3}])
assert n == 1, "TC5 fail"

# TC6: 弱负向 reaction 允许入 evidence（数据契约不拦截）
n = b._enqueue_strategy_candidates([
    {"title": "追问信号", "scene": "tech_help", "param": "depth",
     "direction": "更详尽", "proposed_content": "p2",
     "evidence": [{"message_id": 9, "reaction": "weak_negative"}] * 3}])
assert n == 1, "TC6 fail"

print("TC1-TC6 all passed")
