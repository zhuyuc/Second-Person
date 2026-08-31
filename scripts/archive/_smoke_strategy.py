"""阶段 1 冒烟：响应策略引擎双通道接线 + 快照落库 + narrative 外露。"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

BASE = "http://localhost:8000/api"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" +
          (f" | {detail}" if detail else ""))


def sse_send(payload, timeout=300):
    content, thinking, evt, completed = [], [], "", False
    resp = requests.post(f"{BASE}/chat/send", json=payload,
                         stream=True, timeout=timeout)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("event:"):
            evt = line[6:].strip()
            if evt == "turn_completed":
                completed = True
        elif line and line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if evt == "content_delta":
                content.append(d.get("text", ""))
            elif evt == "thinking_delta":
                thinking.append(d.get("text", ""))
        if completed:
            break
    return "".join(content), "".join(thinking), completed


sid = requests.post(f"{BASE}/chat/session/create").json()["data"]["session_id"]

# ---- 1. 简单消息：快速通道（规则短路或 LLM 决策） ----
c1, t1, ok1 = sse_send({"session_id": sid, "message": "你好呀"})
check("1a.简单消息完成", ok1 and len(c1) > 0)
check("1b.策略决策 narrative 外露", "【策略决策】" in t1,
      t1[:80].replace("\n", " "))

# ---- 2. 复杂消息：收敛通道或快速通道 LLM 决策 ----
c2, t2, ok2 = sse_send({"session_id": sid, "message":
                        "我最近工作效率很低，想分析一下原因并给出改进方案，你觉得我应该从哪几个维度入手？"})
check("2a.复杂消息完成", ok2 and len(c2) > 0)
check("2b.策略决策 narrative 外露", "【策略决策】" in t2,
      t2[:80].replace("\n", " "))

# ---- 3. DB 快照：assistant 消息携带 5 字段瘦身快照 ----
con = sqlite3.connect("data/palace.db")
rows = con.execute(
    "SELECT response_strategy_json FROM conversations "
    "WHERE session_id=? AND role='assistant' AND response_strategy_json IS NOT NULL "
    "ORDER BY id DESC LIMIT 2", (sid,)).fetchall()
check("3a.两条回复均有策略快照", len(rows) == 2, f"{len(rows)} 条")
if rows:
    snap = json.loads(rows[0][0])
    check("3b.快照为瘦身 5 字段",
          set(snap.keys()) == {"angle", "depth",
                               "form", "tone", "complexity_score"},
          str(sorted(snap.keys())))
    check("3c.枚举值合法",
          snap["form"] in ("结论型", "分析型", "确认型", "对话型", "共情型")
          and snap["tone"] in ("严肃", "轻松", "共情", "克制", "激励", "中性")
          and 0 <= snap["depth"] <= 3 and 0 <= snap["complexity_score"] <= 10,
          json.dumps(snap, ensure_ascii=False))

# ---- 4. 引导期/开关关闭路径不受影响（此处仅验证健康） ----
h = requests.get(f"{BASE}/health").json()
check("4.健康检查", h["data"]["status"] == "healthy")

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
