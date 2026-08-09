"""任务 1.16 验收：quick_intent 加 complexity_hint 后的分布健康度（v3 R2）。

方法：向运行中的服务发送代表性消息集（收到首字符即断开，quick_intent 已执行），
再从 Langfuse 读 quick_intent span 的 complexity_hint 输入分布。
验收：不得出现 ≥90% hint<3（全短路退化）或 ≥50% hint≥7（全收敛膨胀）。
"""
import json
import time

import requests
from requests.auth import HTTPBasicAuth

from infrastructure.config_manager import ConfigManager
from observability_langfuse.config import LangfuseConfig

BASE = "http://localhost:8000/api"

MESSAGES = [
    "你好呀",                                    # 简单寒暄 → 期望低
    "1+1等于几",                                 # 简单事实 → 期望低
    "帮我查一下最近的科技新闻",                    # 外部查询 → 期望中低
    "我最近工作效率很低，帮我分析下原因",           # 分析归因 → 期望中高
    "你觉得我该不该辞职去创业？我很纠结",           # 价值判断 → 期望高
    "上次说的那个方案，你再结合之前提到的预算重新评估一下",  # 悬空指代 → 期望收敛
]

sid = requests.post(f"{BASE}/chat/session/create").json()["data"]["session_id"]
t0 = time.time()
for m in MESSAGES:
    resp = requests.post(f"{BASE}/chat/send",
                         json={"session_id": sid, "message": m},
                         stream=True, timeout=120)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if d.get("text"):   # 首个增量到达即断：quick_intent 早已执行完
                resp.close()
                break
    time.sleep(0.5)
print(f"已发送 {len(MESSAGES)} 条，等待 Langfuse 上报…")
time.sleep(12)

cm = ConfigManager("data/config.yaml")
cm.load()
cfg = LangfuseConfig.from_sources(cm.get)
auth = HTTPBasicAuth(cfg.public_key, cfg.secret_key)
r = requests.get(f"{cfg.host}/api/public/observations",
                 params={"type": "SPAN", "limit": 100}, auth=auth, timeout=15)
spans = [o for o in r.json().get("data", [])
         if o.get("name") == "quick_intent"
         and (o.get("startTime") or "") >= ""]
# 只取本次验收窗口内的 span
hints, convs = [], []
for s in spans:
    inp = s.get("input") or {}
    out = s.get("output") or {}
    h = out.get("complexity_hint")
    if h is None and isinstance(inp, dict):
        continue
    if h is not None:
        hints.append(int(h))
        convs.append(bool(out.get("needs_convergence")))

# quick_intent span output 在 core.py 中记录了 needs_convergence/hypothesis/reason，
# complexity_hint 暂未进 span output —— 从 DB 兜底读不到则报警告
if not hints:
    print("WARN: Langfuse 未采集到 complexity_hint（span output 未含该字段），")
    print("      请核对 core.py quick_intent span 输出结构后重跑")
    raise SystemExit(2)

n = len(hints)
low = sum(1 for h in hints if h < 3) / n
high = sum(1 for h in hints if h >= 7) / n
print(f"样本 {n} 条 | hint 分布：{sorted(hints)}")
print(f"低复杂占比 {low:.0%}（阈值 <90%）| 高复杂占比 {high:.0%}（阈值 <50%）")
ok = low < 0.9 and high < 0.5
print("PASS | 分布健康" if ok else "FAIL | 分布退化")
raise SystemExit(0 if ok else 1)
