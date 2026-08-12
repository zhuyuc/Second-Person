"""阶段 4 端到端：候选入队 → pending API → 确认写入 RESPONSE_STRATEGY.md → 策略引擎读先验。"""
import json
import os
import pathlib
import sqlite3
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from agent.strategy_engine import StrategyEngine  # noqa: E402
from infrastructure.config_manager import ConfigManager  # noqa: E402

BASE = "http://localhost:8000/api"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" +
          (f" | {detail}" if detail else ""))


# ---- 1. 造一条 strategy_preference 候选（模拟 OutputStyleBuilder 归因产出） ----
con = sqlite3.connect("data/palace.db")
# 幂等清理：重复跑脚本时先清旧测试数据（唯一索引 change_key+status）
con.execute(
    "DELETE FROM profile_review_queue WHERE change_key IN ('e2e_smoke_key','e2e_reject_key')")
con.execute(
    "DELETE FROM profile_review_rejections WHERE change_key='e2e_reject_key'")
con.commit()
ev = json.dumps({"scene": "opinion", "param": "depth", "direction": "偏好更深",
                 "items": [{"message_id": i, "reaction": "like"} for i in (1, 2, 3)]},
                ensure_ascii=False)
con.execute(
    "INSERT INTO profile_review_queue(review_type,change_key,title,proposed_content,"
    "evidence,priority,status,created_at) "
    "VALUES('strategy_preference','e2e_smoke_key','观点征询场景偏好更深分析',"
    "'观点征询（opinion）场景：depth=3，form=分析型，tone=克制；"
    "用户在观点类问题上偏好深入论证而非简短结论。',?,3,'pending',datetime('now'))",
    (ev,))
con.commit()
con.close()

# ---- 2. pending 列表 API ----
d = requests.get(
    f"{BASE}/profile-review/pending?review_type=strategy_preference").json()
lst = d["data"]["list"]
check("1.pending列表含候选", any(x["change_key"] == "e2e_smoke_key" for x in lst),
      f"{len(lst)} 条")
check("1b.counts含strategy_preference轨道",
      "strategy_preference" in d["data"]["counts"])
cand = next(x for x in lst if x["change_key"] == "e2e_smoke_key")

# ---- 3. 确认候选 ----
r = requests.post(f"{BASE}/profile-review/confirm",
                  json={"id": cand["id"]}).json()
check("2.确认成功", r.get("code") == 200, str(r))

# ---- 4. RESPONSE_STRATEGY.md 落盘（场景分区） ----
p = pathlib.Path("data/profile/RESPONSE_STRATEGY.md")
txt = p.read_text(encoding="utf-8") if p.exists() else ""
check("3.RESPONSE_STRATEGY.md已写入", "## opinion" in txt and "depth=3" in txt,
      txt[:60].replace("\n", " "))

# ---- 5. 策略引擎先验加载含用户偏好段 ----
cm = ConfigManager("data/config.yaml")
cm.load()
eng = StrategyEngine(None, lambda: None, cm, "data")
priors = eng.load_priors()
check("4.先验含用户已确认偏好", "用户已确认偏好" in priors and "opinion" in priors)

# ---- 6. 幂等：再次确认同一候选返回 404（已处理） ----
r2 = requests.post(f"{BASE}/profile-review/confirm",
                   json={"id": cand["id"]}).json()
check("5.重复确认拦截", r2.get("code") == 404, str(r2))

# ---- 7. 拒绝路径：造候选 → 拒绝 → 保护期记录 ----
con = sqlite3.connect("data/palace.db")
con.execute(
    "INSERT INTO profile_review_queue(review_type,change_key,title,proposed_content,"
    "evidence,priority,status,created_at) "
    "VALUES('strategy_preference','e2e_reject_key','测试拒绝','x',?,3,'pending',"
    "datetime('now'))", (ev,))
con.commit()
con.close()
d = requests.get(
    f"{BASE}/profile-review/pending?review_type=strategy_preference").json()
cand2 = next(x for x in d["data"]["list"]
             if x["change_key"] == "e2e_reject_key")
r3 = requests.post(f"{BASE}/profile-review/reject",
                   json={"id": cand2["id"]}).json()
check("6.拒绝成功", r3.get("code") == 200, str(r3))
con = sqlite3.connect("data/palace.db")
prot = con.execute(
    "SELECT 1 FROM profile_review_rejections WHERE change_key='e2e_reject_key'").fetchone()
check("6b.拒绝保护记录存在", prot is not None)
con.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
