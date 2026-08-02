"""情绪模块冒烟测试：参数注册 → 情绪感知 → 双源状态 → 融合更新 → 关闭开关。

覆盖：
1. 参数注册（3 项情绪参数在 schema 中，默认值正确）
2. 初始 neutral 状态（对既有对话零影响）
3. 真实对话触发情绪感知（turn 后异步判定 → mood_state 更新）
4. 双源情绪（user + ai 各自状态）
5. 连续对话平滑融合（状态持续更新）
6. 关闭验证（strength=0 时情绪不更新不注入）
7. mood_history 留痕
8. 清理（测试会话/参数/情绪状态复位）
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
API = "http://localhost:8000/api"
DB_PATH = BASE / "data" / "palace.db"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def sse_send(payload, timeout=300):
    """发送 /chat/send 并收流，返回回复文本。"""
    content = []
    resp = requests.post(f"{API}/chat/send", json=payload, stream=True,
                         timeout=timeout)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("event:") \
                and line[6:].strip() == "turn_completed":
            break
        if line and line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
                if d.get("text"):
                    content.append(d["text"])
            except json.JSONDecodeError:
                pass
    return "".join(content)


def put_params(**kw):
    r = requests.put(f"{API}/settings/params", json=kw)
    assert r.status_code == 200, f"参数设置失败 {r.status_code}: {kw}"


def get_params():
    return requests.get(f"{API}/settings/params").json()["data"]["params"]


def mood_row():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    r = db.execute("SELECT * FROM mood_state WHERE id=1").fetchone()
    n = db.execute("SELECT COUNT(*) n FROM mood_history").fetchone()["n"]
    db.close()
    return (dict(r) if r else None), n


def mood_reset():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("UPDATE mood_state SET user_mood='neutral', user_intensity=0, "
               "ai_mood='neutral', ai_intensity=0, "
               "user_source='reset', ai_source='reset' WHERE id=1")
    db.commit()
    db.close()


# ---- 1. 参数注册 ----
schema = requests.get(f"{API}/settings/params").json()["data"]["schema"]
keys = {p["key"] for p in schema}
check("1a.mood_enabled 已注册", "mood_enabled" in keys)
check("1b.mood_decay_hours 已注册", "mood_decay_hours" in keys)
check("1c.mood_influence_strength 已注册", "mood_influence_strength" in keys)
p = get_params()
check("1d.默认值正确",
      p.get("mood_enabled") is True and p.get("mood_influence_strength") == 0.5
      and p.get("mood_decay_hours") == 2.0,
      f"enabled={p.get('mood_enabled')} decay={p.get('mood_decay_hours')} "
      f"strength={p.get('mood_influence_strength')}")

# ---- 2. 初始 neutral ----
put_params(mood_enabled=True, mood_influence_strength=0.5)
time.sleep(1)
mood_reset()
row0, _ = mood_row()
check("2a.初始 user 情绪 neutral", row0 and row0["user_mood"] == "neutral")
check("2b.初始 ai 情绪 neutral", row0 and row0["ai_mood"] == "neutral")

# ---- 3. 真实对话触发情绪感知 ----
sid = requests.post(f"{API}/chat/session/create").json()["data"]["session_id"]
c = sse_send({"session_id": sid, "message": "今天真的太开心了！项目终于上线了，忙了两个月的成果！"})
check("3a.对话回复正常", len(c) > 0, f"回复 {len(c)} 字符")
print("  等待异步情绪判定（LLM 调用）...")
time.sleep(20)
row1, n1 = mood_row()
check("3b.情绪感知生效（user 非 neutral）",
      row1 and row1["user_mood"] != "neutral",
      f"user={row1['user_mood'] if row1 else None}"
      f"({row1['user_intensity'] if row1 else 0})")

# ---- 4. 双源情绪 ----
check("4a.ai 情绪有状态", row1 and row1["ai_mood"] != "",
      f"ai={row1['ai_mood'] if row1 else None}")
check("4b.强度在 0~1", row1 and 0 <= row1["user_intensity"] <= 1
      and 0 <= row1["ai_intensity"] <= 1)

# ---- 5. 连续对话平滑融合 ----
c2 = sse_send({"session_id": sid, "message": "帮我总结一下上线后的重点工作"})
check("5a.含情绪注入的对话正常", len(c2) > 0, f"回复 {len(c2)} 字符")
time.sleep(20)
row2, n2 = mood_row()
check("5b.状态持续更新（history 增长）", n2 > n1, f"history {n1} -> {n2}")

# ---- 6. 关闭验证 ----
put_params(mood_influence_strength=0)
time.sleep(1)
row_b, hist_b = mood_row()
c3 = sse_send({"session_id": sid, "message": "随便聊几句"})
time.sleep(20)
row_a, hist_a = mood_row()
check("6a.关闭后情绪不更新",
      row_a["user_updated_at"] == row_b["user_updated_at"] and hist_a == hist_b,
      "updated_at 未变" if row_a["user_updated_at"] == row_b["user_updated_at"] else "已变化")
put_params(mood_influence_strength=0.5)
time.sleep(1)

# ---- 7. mood_history 留痕 ----
_, n3 = mood_row()
check("7.mood_history 留痕", n3 >= 1, f"{n3} 条")

# ---- 8. 清理 ----
requests.delete(f"{API}/chat/session/{sid}")
put_params(mood_enabled=True, mood_influence_strength=0.5)
mood_reset()
left = [s["session_id"] for s in
        requests.get(f"{API}/chat/sessions").json()["data"]["list"]]
check("8.测试会话已清理", sid not in left)

# ---- 汇总 ----
print("\n========== 情绪模块冒烟测试汇总 ==========")
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
if failed:
    print("失败项：", "; ".join(failed))
sys.exit(1 if failed else 0)
