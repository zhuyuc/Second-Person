"""阶段 2 冒烟：元认知协议触发链路（高复杂度消息 → 骨架 → 注入生成 → 落库）。"""
import json
import sqlite3

import requests

BASE = "http://localhost:8000/api"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" +
          (f" | {detail}" if detail else ""))


def sse_send(payload, timeout=400):
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

# 两条高复杂度消息（多诉求+价值判断+深度分析），至少一条应触发元认知
MSG = [
    "我35岁，在考虑要不要离开工作十年的大公司去创业，家人反对，我既担心资金又担心市场时机，"
    "请帮我深入分析这个决定的隐藏风险并给出判断框架",
    "我团队里老员工和新员工矛盾很深，我试过调解但失败了，现在项目交付受影响，"
    "帮我从组织行为的角度分析根本原因，并给出可落地的解决路径",
]

skeleton_hit = False
for i, m in enumerate(MSG):
    c, t, ok = sse_send({"session_id": sid, "message": m})
    check(f"{i+1}a.高复杂消息完成", ok and len(c) > 0)
    check(f"{i+1}b.策略决策外露", "【策略决策】" in t)
    if "【思考骨架】" in t:
        skeleton_hit = True
        check(f"{i+1}c.骨架外露", True, t.split("【思考骨架】")
              [1][:60].replace("\n", " "))

check("2.至少一条消息触发元认知", skeleton_hit,
      "未触发则说明 complexity 未达 7，链路无法验证")

# DB：骨架落库验证
con = sqlite3.connect("data/palace.db")
rows = con.execute(
    "SELECT cognitive_skeleton_json FROM conversations "
    "WHERE session_id=? AND role='assistant' AND cognitive_skeleton_json IS NOT NULL",
    (sid,)).fetchall()
if skeleton_hit:
    check("3a.骨架已落库", len(rows) >= 1, f"{len(rows)} 条")
    if rows:
        sk = json.loads(rows[0][0])
        check("3b.骨架结构完整",
              all(k in sk for k in
                  ("reframe", "decompose", "hidden_assumptions",
                   "expert_lens", "answer_shape")),
              str(sorted(sk.keys())))

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
